# Preparación para producción — Odranid

Brief para Codex. Objetivo: dejar el microservicio **seguro**, **rápido** y capaz de
aguantar **tráfico real** de personas (muchas conversaciones simultáneas por Chatwoot/WhatsApp).

Este documento es autocontenido: tiene el contexto del servicio, lo que YA está resuelto
(no rehacer), y las tareas con archivos exactos y criterios de aceptación.

---

## 0. Contexto del servicio

Odranid es un microservicio Python (FastAPI + Celery) que responde un chatbot de WhatsApp
vía Chatwoot, para una empresa argentina de productos de goma/caucho/PVC (pisos, mangueras,
calzado). Reemplaza un flujo viejo de n8n.

**Arquitectura actual:**
- **FastAPI** ([app/main.py](app/main.py)) expone la API y el webhook de Chatwoot.
- **Celery** (workers `worker_messages` y `worker_outbound`, `beat` para tareas programadas)
  procesa los mensajes de forma asíncrona. Broker/Result = **Dragonfly** (compatible Redis).
- **PostgreSQL + pgvector** = system of record del catálogo.
- **Typesense** = índice de búsqueda (scoring híbrido keyword + vector). Es el motor de
  búsqueda activo ([app/typesense_search.py](app/typesense_search.py)).
- **OpenAI**: embeddings (`text-embedding-3-small`) + chat (`gpt-4.1-mini`) para el agente
  PydanticAI ([app/agents/pydantic_agent.py](app/agents/pydantic_agent.py)).
- **Logfire** para observabilidad.
- Configuración por entorno: [app/config.py](app/config.py), prefijo `ODRANID_`, lee `.env`.
- Deploy local/VPS: [docker-compose.yml](docker-compose.yml).

**Flujo de un mensaje:** Chatwoot → `POST /webhooks/chatwoot` (verifica firma, encola en
Celery y responde rápido) → worker arma el contexto del catálogo + corre el agente
(busca productos, calcula cobertura) → encola la respuesta saliente → outbox la manda a Chatwoot.

---

## 1. Lo que YA está bien (NO rehacer)

- ✅ **Webhook asíncrono**: no bloquea, encola en Celery.
- ✅ **Lock por conversación + debounce** (`chatwoot_lock_seconds`, `chatwoot_debounce_seconds`
  en [app/config.py](app/config.py)): mensajes seguidos del mismo cliente no se pisan ni
  duplican respuesta.
- ✅ **Reintentos + outbox** (`chatwoot_job_max_retries`, `chatwoot_outbox_max_retries`).
- ✅ **Pool de conexiones** a Postgres ([app/db_search.py:20](app/db_search.py#L20), `ConnectionPool`).
- ✅ **Firma HMAC** del webhook ([app/chatwoot.py:77](app/chatwoot.py#L77) `verify_chatwoot_signature`).
- ✅ **Healthchecks** de db/typesense/dragonfly en [docker-compose.yml](docker-compose.yml).
- ✅ Búsqueda, cobertura de pisos (rollos / cortado a medida) y filtro de talles de calzado:
  resueltos y testeados. No tocar esa lógica.

Suite de tests: `pytest tests/` (corre con `.venv/bin/python -m pytest tests/ -q`). Hoy: **104 pasan**.
Toda tarea de código debe dejar la suite verde y sumar tests donde aplique.

---

## PARTE A — Tareas de CÓDIGO (Codex implementa en el repo)

> Crear una rama nueva desde el estado actual, p. ej. `hardening-produccion`.
> Cada tarea con su test. No romper los 104 tests existentes.

### A1. Cache del contexto de catálogo (PERFORMANCE — máxima prioridad)

**Problema.** [`current_catalog_context()`](app/main.py#L307) llama
`db_search_engine.catalog_context()`, que ejecuta la función SQL `catalog_facets` contra
Postgres ([app/db_search.py:62](app/db_search.py#L62)). Esto corre **en cada mensaje entrante**
(se invoca desde el procesamiento del webhook, ~3 s por llamada) y **no está cacheado** en el
camino de DB. El `CatalogContextCache` existente solo cubre el catálogo local, no el de Postgres.

**Qué hacer.**
- Cachear el resultado de `current_catalog_context()` (string) con un **TTL** corto
  (nueva setting `catalog_context_ttl_seconds: int = 300` en [app/config.py](app/config.py)).
- El contexto solo cambia cuando se re-sincroniza el catálogo. **Invalidar** el cache en los
  puntos donde hoy ya se llama `context_cache.invalidate()` ([app/main.py:417](app/main.py#L417))
  y al final de `/admin/typesense-sync` y `/admin/reload`.
- Implementación simple: un wrapper con `{valor, timestamp}` a nivel módulo, o extender
  `CatalogContextCache` para soportar también el camino de DB. Thread-safe (hay múltiples
  requests/uvicorn workers).

**Criterio de aceptación.**
- Dos llamadas seguidas a `current_catalog_context()` dentro del TTL ejecutan la query de
  Postgres **una sola vez** (testear con un fake/contador de `catalog_context`).
- Tras invalidar (sync/reload), la siguiente llamada vuelve a consultar.
- La latencia de respuesta a un mensaje deja de incluir los ~3 s de facets salvo la primera vez.

---

### A2. Autenticación en endpoints `/admin/*` (SEGURIDAD — máxima prioridad)

**Problema.** Los endpoints administrativos **no tienen auth**:
- [`/admin/reload`](app/main.py#L328)
- [`/admin/fetch-woocommerce`](app/main.py#L334)
- [`/admin/typesense-sync`](app/main.py#L342) → hace `run_typesense_sync(recreate=True)`,
  que **recrea el índice y consume embeddings de OpenAI** (cuesta plata y es un DoS trivial).

Cualquiera que alcance el puerto 8000 puede dispararlos.

**Qué hacer.**
- Nueva setting `admin_api_token: str | None = None` en [app/config.py](app/config.py)
  (env `ODRANID_ADMIN_API_TOKEN`).
- Dependencia FastAPI que exija header `X-Admin-Token` (o `Authorization: Bearer ...`) y lo
  compare con `settings.admin_api_token` usando `hmac.compare_digest`.
- Aplicarla a **todos** los `/admin/*`.
- **Fail-closed**: si `admin_api_token` es `None` o el header no coincide → `HTTPException(401)`
  (o 503 "admin deshabilitado" si el token no está configurado). Nunca dejar admin abierto.
- Documentar la variable en `.env.example`.

**Criterio de aceptación.**
- `POST /admin/typesense-sync` sin header o con token incorrecto → 401/503.
- Con el token correcto → funciona igual que hoy.
- Tests para ambos casos.

---

### A3. Exigir secret del webhook en producción (SEGURIDAD)

**Problema.** [`verify_chatwoot_signature`](app/chatwoot.py#L77) hace `if not secret: return True`.
Es cómodo para desarrollo local, pero si en prod `chatwoot_webhook_secret` queda vacío, el
webhook **acepta cualquier POST falso** → gasto de OpenAI y respuestas a tráfico trucho.

**Qué hacer.**
- No cambiar el comportamiento por defecto para no romper dev. Agregar setting
  `require_webhook_secret: bool = False`.
- En el startup ([app/main.py:57](app/main.py#L57) `@app.on_event("startup")`): si
  `require_webhook_secret` es `True` y `chatwoot_webhook_secret` está vacío → **abortar el
  arranque** con error claro. Si es `False` pero el secret está vacío → **log de WARNING**
  bien visible ("webhook sin firmar: inseguro para producción").
- Documentar en `.env.example`: en prod, setear `ODRANID_CHATWOOT_WEBHOOK_SECRET` y
  `ODRANID_REQUIRE_WEBHOOK_SECRET=true`.

**Criterio de aceptación.**
- Con `require_webhook_secret=true` y sin secret, la app no levanta.
- Test del path de verificación con/sin secret y con `require_webhook_secret`.

---

### A4. (Opcional, si sobra tiempo) Cache de embeddings de query repetidas

**Problema.** Cada búsqueda con texto genera un embedding de OpenAI
([app/typesense_search.py](app/typesense_search.py) → `embedder.embed_many`). Consultas
idénticas frecuentes ("piso de goma", "manguera jardín") pagan embedding cada vez.

**Qué hacer.** Cache LRU pequeño (p. ej. `functools.lru_cache` o dict acotado) por texto de
query normalizado, con tope de tamaño. No cachear si el texto es muy largo/único.

**Criterio de aceptación.** Dos búsquedas con el mismo texto piden el embedding una sola vez.

---

## PARTE B — Tareas de INFRA (Codex prepara los archivos; Augusto aplica en el VPS)

> Estas tocan [docker-compose.yml](docker-compose.yml) y archivos de deploy. Codex deja todo
> listo en el repo; Augusto corre el rebuild/deploy en el VPS.

### B1. Cerrar puertos internos (SEGURIDAD)

Hoy [docker-compose.yml](docker-compose.yml) publica al host: Postgres 5432, Typesense 8108,
Dragonfly 6379 y **Flower 5555 (sin password)**. En el VPS solo debería quedar expuesto el
**8000 de la API** (y aun ese, detrás de un reverse proxy).

**Qué hacer.**
- Para `db`, `typesense`, `dragonfly`, `flower`: quitar el mapeo `ports:` al host, o
  bindearlo a `127.0.0.1` (`"127.0.0.1:5432:5432"`). Los servicios igual se ven entre sí por
  la red interna de compose por nombre de servicio.
- **Flower**: agregar basic auth (`--basic-auth=usuario:password` vía env) si se deja accesible.
- Documentar que en prod la API se publica solo a través del reverse proxy.

### B2. Reverse proxy + HTTPS (SEGURIDAD / OPS)

No hay TLS hoy. Agregar **Caddy** (más simple, TLS automático) o **nginx** delante del 8000.

**Qué hacer.** Servicio de reverse proxy en compose (o instrucciones standalone) que:
- Termine TLS para el dominio del webhook (Let's Encrypt automático con Caddy).
- Proxee al `api:8000`.
- Opcional: **rate-limit por IP** en los endpoints públicos (`/search`, `/agent/respond`,
  `/intake/analyze`, `/webhooks/chatwoot`).
- Dejar un `Caddyfile` (o `nginx.conf`) de ejemplo en el repo.

### B3. Escalar la API y los workers (TRÁFICO)

**Qué hacer.**
- API: hoy [command](docker-compose.yml#L53) es `uvicorn ... ` en **un proceso**. Cambiar a
  varios workers: `uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4` (o
  `gunicorn -k uvicorn.workers.UvicornWorker -w 4`). Verificar que el cache de A1 sea
  consistente entre workers (TTL por proceso está OK; si se quiere compartido, usar Dragonfly).
- `worker_messages`: hoy `--concurrency=4`. El cuello es la latencia de OpenAI por mensaje;
  para picos, **escalar horizontalmente** (más réplicas del worker) además de subir concurrency.
  Dejar documentado cómo levantar réplicas (`docker compose up --scale worker_messages=3`).
- NOTA: ya existe `worker_catalog` (cola `catalog`, concurrency 1) aislando el sync del catálogo
  de `chatwoot_messages` para que un sync pesado no demore las respuestas. Mantener esa separación
  al escalar.

### B4. Límites de recursos (RESILIENCIA)

Agregar límites por servicio en compose (`mem_limit`/`cpus`, o `deploy.resources` en swarm)
para que un worker desbocado o Typesense no tiren el VPS. Sugerido: topes de memoria a
Postgres, Typesense, Dragonfly y cada worker según RAM del VPS.

### B5. Backups automáticos de Postgres (RESILIENCIA)

Postgres es el system of record. Dejar un script + cron (o servicio sidecar) que haga
`pg_dump` periódico a un volumen/almacenamiento externo, con rotación. Documentar el restore.

---

## PARTE C — Sincronización del catálogo (WooCommerce → Postgres → Typesense)

> Esta parte es importante: el estado actual tiene **dos fuentes de verdad** y el sync a
> Postgres **no está automatizado**. Hay que unificarlo.

### C0. Cómo funciona HOY (diagnóstico)

- **WooCommerce → Postgres: MANUAL.** Lo hace el script [scripts/sync_catalog.py](scripts/sync_catalog.py)
  corrido a mano: baja productos de WooCommerce (o de un snapshot `--from-file`), normaliza,
  genera embeddings (con cache en `.cache/embeddings.json`) y hace upsert en la tabla
  `catalog_products` ([app/postgres_store.py](app/postgres_store.py), `UPSERT_SQL`). **No hay
  tarea Celery ni cron que lo dispare** → si no lo corrés a mano, Postgres queda desactualizado.
- **Catálogo → Typesense: AUTOMÁTICO pero NO lee de Postgres.** La tarea beat
  `sync_typesense_catalog` corre cada `typesense_sync_minutes` (30) y llama `run_typesense_sync`
  ([app/typesense_sync.py](app/typesense_sync.py)). Pero `build_catalog_documents()` lee del
  **snapshot `productos.json` o de WooCommerce en vivo**, NO de Postgres
  ([app/typesense_sync.py:25](app/typesense_sync.py#L25)).

**Problema arquitectural:** Postgres (system of record para la búsqueda pgvector y el contexto
del agente) y Typesense (índice de búsqueda activo) se alimentan por caminos distintos. Postgres
puede quedar viejo (sync manual) mientras Typesense se refresca solo desde otra fuente. Pueden
**divergir**. Además se re-embebe en cada lado por separado (doble costo OpenAI).

### C1. Arquitectura objetivo (UNA sola fuente de verdad)

```
WooCommerce  ──(tarea programada)──▶  normalizar  ──▶  embeddings (con cache)
                                                          │
                                                          ▼
                                              UPSERT en Postgres (catalog_products)
                                                          │   ← SYSTEM OF RECORD
                                                          ▼
                                       Typesense lee DE Postgres y re-indexa
```

Un único pipeline programado: WooCommerce → Postgres, y Typesense se construye **desde Postgres**
(reutilizando los embeddings ya guardados, sin re-embeber). Así nunca divergen y se paga el
embedding una sola vez.

### C2. Tareas concretas (código)

**C2.a — Automatizar WooCommerce → Postgres.**
- Crear tarea Celery `sync_catalog_to_postgres` en [app/tasks/catalog_tasks.py](app/tasks/catalog_tasks.py)
  que haga lo mismo que [scripts/sync_catalog.py](scripts/sync_catalog.py) (reutilizar su lógica:
  extraer la parte reutilizable a una función en `app/` e invocarla tanto desde el script como
  desde la tarea — no duplicar).
- Agendarla en el `beat_schedule` de [app/celery_app.py](app/celery_app.py) con su propia setting
  `catalog_sync_minutes: int = 60` (configurable, default cada 60 min; la ingesta de WooCommerce
  + embeddings es más pesada que el refresh de Typesense, por eso menos frecuente).
- Mantener el **cache de embeddings por hash de contenido** (ya existe en el script) para no
  re-embeber productos que no cambiaron.
- Endpoint admin `/admin/sync-catalog` (protegido con el token de A2) para disparar el sync
  manualmente bajo demanda.

**C2.b — Typesense lee desde Postgres.**
- Cambiar `build_catalog_documents()` ([app/typesense_sync.py:25](app/typesense_sync.py#L25)) para
  leer los productos **y sus embeddings desde `catalog_products` (Postgres)** en vez del snapshot
  WooCommerce. Reutilizar los embeddings guardados (columna `embedding`) → `embeddings_for()` deja
  de llamar a OpenAI cuando ya hay vector en la fila.
- Dejar el snapshot/WooCommerce solo como **fallback** si Postgres no está configurado (para tests
  y dev local sin DB).
- **Efecto secundario clave**: al leer desde Postgres, el runtime deja de depender de
  `productos.json`. Eso es lo que habilita sacar ese JSON de la raíz/prod (ver D2). El agente nunca
  más lee un `.json` de catálogo en producción.

**C2.c — Orden de las tareas beat.** El sync a Postgres debe correr antes que el de Typesense
(o el de Typesense detecta que Postgres cambió). Simple: distintas frecuencias + Typesense siempre
lee el estado actual de Postgres, así eventualmente consistente sin coordinación compleja.

**Criterio de aceptación.**
- Existe tarea beat que actualiza Postgres desde WooCommerce sin intervención manual.
- `run_typesense_sync` toma los productos y embeddings de Postgres (verificable con un fake store).
- Un producto nuevo/modificado en WooCommerce aparece en Postgres y luego en Typesense sin correr
  nada a mano.
- No se re-embebe lo que no cambió (cache por hash respetado).
- Tests de la nueva tarea y del nuevo `build_catalog_documents`.

---

## PARTE D — Reestructurar el proyecto (carpetas + limpiar la raíz)

> Objetivo: raíz limpia y `app/` organizada por dominio. La suite de tests (104) es la red de
> seguridad: mover/renombrar y dejar `pytest tests/` verde. **Cada commit debe seguir importando
> y testeando bien** — hacer la mudanza por pasos, no todo de una.

### D1. Reorganizar `app/` por dominio

Hoy `app/` tiene ~25 módulos planos. Agrupar en subpaquetes (cada uno con `__init__.py`):

```
app/
  core/        config.py, models.py
  catalog/     normalization.py, postgres_store.py, woocommerce.py, typesense_sync.py,
               coverage.py, footwear.py, domain_synonyms.py, catalog_context.py
  search/      db_search.py, typesense_search.py, typesense_index.py, typesense_client.py,
               retrieval.py, query_parser.py, search_common.py, embeddings.py, rag_precontext.py
  chat/        chatwoot.py, chatwoot_service.py, chat_memory.py, slot_questions.py
  agents/      (ya existe) pydantic_agent.py, catalog_helpers.py
  tasks/       (ya existe) chatwoot_tasks.py, catalog_tasks.py
  main.py, celery_app.py        (quedan en la raíz de app/)
```

- Actualizar **todos** los imports (`from app.X import` → `from app.search.X import`, etc.) y los
  `include=[...]`/`task_routes` de [app/celery_app.py](app/celery_app.py) (usan rutas por string
  como `"app.tasks.catalog_tasks.sync_typesense_catalog"` — cuidado, esos strings deben seguir
  coincidiendo con la ubicación real).
- Hacerlo en pasos chicos (un subpaquete por commit) corriendo los tests entre medio.
- La agrupación exacta es orientativa; lo importante es separar por dominio y no romper nada.

### D2. Limpiar la raíz

**Trackeados en git que conviene MOVER a `docs/`:** `AGENT.md`, `ARQUITECTURA_MICROSERVICIO.md`,
`BUSCAR_PRODUCTOS_TOOL.md`, `CHATWOOT_WEBHOOK_SETUP.md`, `POSTGRES_VPS_SETUP.md`,
`RESUMEN_PROYECTO.md`, `ROADMAP_TECNICO.md`, `PREPARACION_PRODUCCION.md`. Crear `docs/` y moverlos.

**Principio: ningún JSON de datos en producción ni en el runtime del agente.** Los JSON sueltos
eran para pruebas/desarrollo. En producción el dato sale de WooCommerce → Postgres → Typesense
(Parte C). El agente/runtime NO debe leer ningún `.json` de catálogo. Los JSON quedan, a lo sumo,
como fixtures de dev **fuera de git** (`data/`, gitignored).

- `chatwoot_conversaciones.json` (3,5 MB) — **trackeado en git**, es un fixture de replay
  (dev only). `git rm --cached chatwoot_conversaciones.json`, moverlo a `data/fixtures/` y agregar
  `data/` al `.gitignore`. Que los scripts de replay ([scripts/replay_chatwoot_conversations.py](scripts/replay_chatwoot_conversations.py))
  lo lean desde ahí.
- `sheet.json` (untracked) — dato viejo, sin uso en el código. **Verificar que nadie lo importe
  y borrarlo.**
- `productos.json` (9,8 MB, untracked) — snapshot del catálogo. Hoy está **enchufado al runtime**:
  es el default de `catalog_file` ([app/config.py:11](app/config.py#L11)) y la fuente que usa
  `build_catalog_documents()` para Typesense. **No se puede sacar hasta completar C2.b** (que
  Typesense lea desde Postgres). Secuencia correcta:
  1. Hacer C2.b → Postgres pasa a ser la única fuente; `productos.json` deja de leerse en runtime.
  2. Recién ahí, mover `productos.json` a `data/` (gitignored) como fixture de dev, o eliminarlo
     (se regenera desde WooCommerce con `scripts/sync_catalog.py --from-file` o el sync directo).
  3. El default de `catalog_file` deja de apuntar a la raíz; el snapshot es opcional, solo dev.
  - Los **tests NO dependen** de `productos.json` (usan fixtures chicos inline), así que sacarlo
    de la raíz no rompe la suite.

**Verificar / decidir:**
- `supabase/` — está **vacío** (sin archivos). Borrar. La base es Postgres+pgvector con migraciones
  en [postgres/migrations/](postgres/migrations/); Supabase quedó de una etapa anterior.
- `buscar_productos_tool.schema.json` — schema de la tool. Si ya no se carga en runtime, mover a
  `docs/` o `app/agents/`. Verificar que nada lo importe antes de mover.
- Ya ignorados (dejar como están): `.cache/`, `reports/`, `.venv/`, `.pytest_cache/`, `.agents/`,
  `.codex/`, `.claude/`, `.env`.

⚠️ **`prompt_agente_odranid.md` NO moverlo a la ligera**: se carga en runtime como
`agent_prompt_file` ([app/config.py:12](app/config.py#L12)). Si se mueve a `docs/` o `app/`, hay
que actualizar ese default. Recomendado: dejarlo en la raíz o moverlo a `app/agents/prompts/` y
actualizar la setting.

**Criterio de aceptación.** Raíz con solo: `app/`, `tests/`, `scripts/`, `postgres/`, `docs/`,
`data/` (gitignored), archivos de build/deploy (`pyproject.toml`, `uv.lock`, `Dockerfile`,
`.dockerignore`, `docker-compose.yml`, `.gitignore`, `.env.example`) y el prompt si se deja ahí.
`pytest tests/` verde. `git status` sin archivos pesados nuevos trackeados.

---

## PARTE E — Documentar el `.env` por completo

El [.env.example](.env.example) actual no incluye todas las settings de
[app/config.py](app/config.py). Dejarlo **exhaustivo y comentado**: cada variable con (1) qué hace,
(2) si es obligatoria u opcional, (3) su default.

**Faltan documentar** (están en `config.py`, no en `.env.example`): `ODRANID_CONTEXT_CACHE_FILE`,
`ODRANID_VECTOR_TOP_K`, `ODRANID_EMBEDDING_MODEL`,
`ODRANID_CHATWOOT_WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS`, más las **nuevas** de este plan:
`ODRANID_ADMIN_API_TOKEN` (A2), `ODRANID_REQUIRE_WEBHOOK_SECRET` (A3),
`ODRANID_CATALOG_CONTEXT_TTL_SECONDS` (A1), `ODRANID_CATALOG_SYNC_MINUTES` (C2).

Agrupar por bloque con comentarios de sección, por ejemplo:

```dotenv
# ============================================================
# OpenAI (OBLIGATORIO) — embeddings + chat del agente
# ============================================================
OPENAI_API_KEY=sk-...                       # obligatorio (sin esto no hay búsqueda ni agente)
ODRANID_AGENT_MODEL=gpt-4.1-mini            # modelo de chat (default: gpt-4.1-mini)
ODRANID_EMBEDDING_MODEL=text-embedding-3-small  # default: text-embedding-3-small (1536 dims)

# ============================================================
# Postgres + pgvector (OBLIGATORIO en producción) — system of record
# ============================================================
ODRANID_DATABASE_URL=postgresql://user:pass@host:5432/odranid_catalog  # obligatorio en prod
# ...

# ============================================================
# Seguridad (PRODUCCIÓN)
# ============================================================
ODRANID_ADMIN_API_TOKEN=                    # token para /admin/* (sin él, admin deshabilitado)
ODRANID_REQUIRE_WEBHOOK_SECRET=true         # en prod: aborta el arranque si no hay webhook secret
```

**Criterio de aceptación.** Toda setting de `config.py` (incluidas las nuevas) aparece comentada en
`.env.example`, marcando obligatorias vs opcionales y su default. Un dev nuevo puede levantar el
proyecto solo leyendo ese archivo.

---

## PARTE F — Guía de despliegue (`docs/DESPLIEGUE.md`)

Crear `docs/DESPLIEGUE.md` con el paso a paso completo para levantar en un VPS desde cero.
Consolidar lo que hoy está disperso en [POSTGRES_VPS_SETUP.md](POSTGRES_VPS_SETUP.md) y
[CHATWOOT_WEBHOOK_SETUP.md](CHATWOOT_WEBHOOK_SETUP.md). Debe cubrir:

1. **Requisitos**: Docker + Docker Compose, dominio para el webhook, credenciales WooCommerce,
   token de Chatwoot, API key de OpenAI.
2. **Configuración**: copiar `.env.example` a `.env` y completar (referenciar Parte E).
3. **Base de datos**: aplicar las migraciones de [postgres/migrations/](postgres/migrations/)
   en orden (`001_catalog_products.sql` … `004_celery_chatwoot_outbox.sql`). Indicar cómo
   (psql / contenedor db).
4. **Primer sync del catálogo**: correr el sync inicial WooCommerce → Postgres (script o el
   nuevo endpoint admin), verificar conteo de filas en `catalog_products`, y el primer
   build de Typesense (`/admin/typesense-sync`).
5. **Levantar los servicios**: `docker compose up -d` (qué servicios son: api, worker_messages,
   worker_outbound, beat, flower, db, typesense, dragonfly). Verificar `GET /health`.
6. **Webhook de Chatwoot**: apuntar el webhook a `https://dominio/webhooks/chatwoot`, setear el
   secret, verificar con `GET /webhooks/chatwoot/health`.
7. **Reverse proxy + HTTPS** (Parte B2) y **cierre de puertos** (Parte B1).
8. **Operación**: cómo ver logs, Flower para monitorear Celery, cómo forzar un re-sync, cómo
   escalar workers (`--scale worker_messages=N`).
9. **Backups y restore** de Postgres (Parte B5).
10. **Checklist de verificación post-deploy** (reusar el de la sección final).

**Criterio de aceptación.** Siguiendo `docs/DESPLIEGUE.md` de cero, el servicio queda andando y
respondiendo un mensaje de prueba por Chatwoot.

---

## 3. Orden sugerido de ejecución

1. **A1** (cache contexto) + **A2** (auth admin) — mayor ROI: baja latencia y cierra el agujero
   de plata/DoS. Son código y testeables.
2. **A3** (exigir webhook secret) — código, rápido.
3. **C** (unificar sync WooCommerce → Postgres → Typesense) — el catálogo se mantiene solo, sin
   sync manual ni fuentes divergentes. Hacerlo después de A2 porque suma un endpoint admin.
4. **D** (reestructurar carpetas + limpiar raíz) — mejor hacerlo cuando el código de A/C ya
   esté estable, para no rebasar imports en medio de otras tareas.
5. **E** (documentar `.env`) — rápido; hacerlo al final para incluir todas las variables nuevas.
6. **B1** (cerrar puertos) + **B2** (reverse proxy + TLS) — deja el servicio seguro de cara afuera.
7. **B3** (workers/réplicas) + **B4** (límites) + **B5** (backups) — aguante de tráfico y resiliencia.
8. **F** (guía de despliegue) — escribir al final, reflejando todo lo anterior ya hecho.
9. **A4** (cache embeddings) — optimización opcional.

## 4. Checklist final de "listo para producción"

- [ ] `current_catalog_context()` cacheado; respuesta no incluye los ~3 s de facets por mensaje.
- [ ] `/admin/*` requieren token; sin token → 401/503.
- [ ] En prod: `ODRANID_CHATWOOT_WEBHOOK_SECRET` y `ODRANID_REQUIRE_WEBHOOK_SECRET=true` seteados.
- [ ] Sync WooCommerce → Postgres automatizado (tarea beat); Typesense lee desde Postgres; sin
      divergencia ni sync manual; embeddings no se recalculan si el producto no cambió.
- [ ] `app/` reorganizada por dominio; raíz limpia; datos pesados fuera de git; `pytest tests/` verde.
- [ ] Solo el 8000 expuesto, detrás de reverse proxy con HTTPS; Flower con auth o cerrado.
- [ ] API con varios workers; worker_messages escalable; límites de recursos puestos.
- [ ] Backups de Postgres andando y restore probado.
- [ ] `.env.example` documenta TODAS las variables (incluidas las nuevas) con default y si son obligatorias.
- [ ] `docs/DESPLIEGUE.md` permite levantar el proyecto de cero.
