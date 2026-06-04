# Resumen del proyecto Odranid (microservicio Python)

> Documento descriptivo, solo lectura. Describe el estado actual del código tal como está,
> sin proponer cambios. Pensado para que alguien decida cómo migrarlo.

Odranid es un agente de IA que atiende clientes de una empresa argentina de productos de
goma/caucho/PVC por WhatsApp (a través de Chatwoot). El cliente escribe, el sistema entiende
qué producto busca, lo busca en un catálogo y responde con opciones. Es una reescritura en
Python de un flujo que antes corría en n8n.

---

## 1. Estructura general

```
odranid/
├── app/                        # Código de la aplicación (FastAPI + Celery)
│   ├── main.py                 # App FastAPI: endpoints, arranque, orquestación
│   ├── config.py               # Settings (pydantic-settings, prefijo ODRANID_)
│   ├── models.py               # Contratos Pydantic (productos, filtros, intake, respuestas)
│   ├── celery_app.py           # Definición de Celery: colas, rutas, beat schedule
│   │
│   ├── agents/                 # Capa de IA (framework Agno)
│   │   ├── odranid_team.py     # Coordina los dos agentes en secuencia
│   │   ├── requirements_agent.py  # LLM que extrae intención + datos del pedido
│   │   └── catalog_agent.py    # LLM conversacional con tool-use que arma la respuesta
│   ├── tools/
│   │   └── buscar_productos.py # Tool Agno que el catalog_agent invoca para buscar
│   ├── agent.py                # Marcado DEPRECATED; aún provee helpers usados por la capa nueva
│   │
│   ├── tasks/
│   │   └── chatwoot_tasks.py   # Tareas Celery: procesar conversación, enviar respuesta, cron
│   ├── chatwoot.py             # Webhook: verificación de firma, parseo, cliente HTTP de Chatwoot
│   ├── chatwoot_service.py     # Lógica de persistir evento entrante y procesar pendientes
│   │
│   ├── chat_memory.py          # Memoria conversacional + jobs + locks + outbox (Postgres)
│   ├── slot_questions.py       # Genera la "próxima pregunta" sobre el estado ya estructurado
│   ├── query_parser.py         # Convierte el intake del LLM en ProductFilters
│   ├── rag_precontext.py       # Arma un bloque de pre-contexto JSON para el prompt
│   │
│   ├── db_search.py            # Búsqueda vectorial/facetada contra Postgres (pgvector)
│   ├── retrieval.py            # Búsqueda local léxica (fallback, sin base de datos)
│   ├── embeddings.py           # Cliente HTTP de embeddings de OpenAI
│   ├── coverage.py             # Cálculo de cobertura en m²/rollos para pisos
│   ├── catalog_context.py      # Cachea un resumen textual del catálogo para el prompt
│   │
│   ├── woocommerce.py          # Ingesta de productos desde la Store API de WooCommerce
│   ├── normalization.py        # Normaliza/clasifica productos WooCommerce → ProductDocument
│   └── postgres_store.py       # Upsert de productos normalizados a Postgres
│
├── scripts/
│   ├── sync_catalog.py         # Sincroniza catálogo WooCommerce → Postgres (con embeddings)
│   ├── fetch_woocommerce.py    # Descarga el catálogo a un JSON local
│   ├── analyze_catalog.py      # Análisis del catálogo
│   ├── replay_chatwoot_conversations.py  # Re-ejecuta conversaciones reales contra el agente
│   └── analyze_shadow_report.py          # Analiza reportes de "shadow" testing
│
├── postgres/
│   ├── schema.sql              # Esquema base Postgres + pgvector + funciones de búsqueda
│   └── migrations/             # 4 migraciones .sql incrementales
├── supabase/
│   └── migrations/             # 4 migraciones .sql (mismos nombres que postgres/, contenido distinto)
│
├── tests/                      # 9 archivos de tests (pytest)
├── reports/                    # Salidas de shadow-testing y análisis (jsonl + md + json)
│
├── prompt_agente_odranid.md    # System prompt del agente de catálogo (~413 líneas)
├── productos.json              # Snapshot local del catálogo (fixture)
├── requirements.txt            # Dependencias (sin lockfile)
├── Dockerfile                  # Imagen Python 3.12-slim
├── docker-compose.yml          # API + workers + beat + Dragonfly (+ Postgres/Flower opcionales)
├── .env / .env.example         # Configuración
└── *.md                        # Documentación: AGENT.md, ARQUITECTURA_*, ROADMAP_*, setups
```

---

## 2. Stack actual

Versiones tomadas de `requirements.txt` (usa rangos `>=`, no hay lockfile ni pyproject.toml):

| Componente | Tecnología | Versión declarada |
|---|---|---|
| Lenguaje | Python | 3.12 (del Dockerfile) |
| Framework web | FastAPI | `>=0.111` |
| Servidor ASGI | Uvicorn | `>=0.30` (extras `[standard]`) |
| Validación/config | Pydantic / pydantic-settings | `>=2.7` / `>=2.3` |
| Driver Postgres | psycopg (binario) + psycopg-pool | `>=3.2` |
| Cola / worker | Celery (extras `[redis]`) | `>=5.4` |
| Broker/cache cliente | redis-py | `>=5.0` |
| Monitoreo Celery | Flower | `>=2.0` |
| Framework de agentes | **Agno** | `>=2.6` (instalado: 2.6.11) |
| LLM / embeddings | openai | `>=1.60` |

- **Base de datos:** PostgreSQL con extensión **pgvector** (embeddings `vector(1536)`). En el
  compose local se usa la imagen `pgvector/pgvector:pg16`. El `.env.example` apunta como backend
  de producción a un Postgres directo (vía Tailscale/VPS); `AGENT.md` menciona Supabase como base
  principal de producción, pero **no hay código que use Supabase** (no existe `app/supabase_store.py`
  pese a estar citado en la doc, y `grep supabase` sobre `app/` no devuelve nada).
- **Broker / backend Celery:** Redis. En el compose se usa **Dragonfly** (compatible con protocolo
  Redis) en `redis://dragonfly:6379/0` (broker) y `/1` (backend de resultados).
- **Modelos:** `gpt-4.1-mini` para el agente (configurable), `text-embedding-3-small` para embeddings.

---

## 3. Cómo se ejecuta

**Puntos de entrada:**
- API: `uvicorn app.main:app --host 0.0.0.0 --port 8000` (CMD del Dockerfile).
- Worker de mensajes: `celery -A app.celery_app.celery_app worker -Q chatwoot_messages --concurrency=4`
- Worker de salida: `celery -A app.celery_app.celery_app worker -Q chatwoot_outbound --concurrency=2`
- Scheduler: `celery -A app.celery_app.celery_app beat`
- Monitoreo (opcional): `celery ... flower --port=5555`
- Sincronización de catálogo (manual/CLI): `python scripts/sync_catalog.py`

**docker-compose.yml** define los servicios: `api`, `worker_messages`, `worker_outbound`, `beat`,
`dragonfly`, y con perfiles opcionales `postgres`+`migrate` (perfil `local-db`) y `flower`
(perfil `observability`). El servicio `migrate` aplica los `.sql` de `postgres/migrations/` con `psql`.

**Endpoints HTTP (FastAPI):**
- `GET /health`, `GET /catalog/context`
- `POST /search`, `POST /intake/analyze`, `POST /agent/respond`
- `GET /webhooks/chatwoot/health`, `POST /webhooks/chatwoot`
- `POST /admin/reload`, `POST /admin/fetch-woocommerce`

**Variables de entorno** (prefijo `ODRANID_`, archivo `.env`). Las principales:
- `OPENAI_API_KEY`, `ODRANID_AGENT_MODEL`, `ODRANID_DATABASE_URL`
- WooCommerce: `ODRANID_WOOCOMMERCE_BASE_URL`, `_PER_PAGE`, `_MAX_PAGES`, `_STOCK_STATUS`
- Chatwoot: `ODRANID_CHATWOOT_BASE_URL`, `_ACCOUNT_ID`, `_API_ACCESS_TOKEN`, `_WEBHOOK_SECRET`, `_AUTO_REPLY`
- Memoria/colas: `ODRANID_CHAT_MEMORY_ENABLED`, `_HISTORY_LIMIT`, `_LOCK_SECONDS`, `_DEBOUNCE_SECONDS`,
  `_JOB_MAX_RETRIES`, `_OUTBOX_MAX_RETRIES`, `_STALE_PROCESSING_MINUTES`
- Celery: `ODRANID_CELERY_BROKER_URL`, `_RESULT_BACKEND`, `_TIMEZONE` (`America/Argentina/Tucuman`)

Nota: el archivo `.env` real está presente en el repo (no solo el `.example`).

---

## 4. Flujo de un mensaje entrante (de punta a punta)

1. **Entrada del webhook** — Chatwoot hace `POST /webhooks/chatwoot` ([app/main.py](app/main.py)).
   - Se verifica la firma HMAC-SHA256 (`x-chatwoot-signature` + timestamp con tolerancia configurable).
     Si no hay secreto configurado, la verificación pasa.
   - Se parsea el payload y se extrae el evento: solo se procesan `event=message_created`,
     `message_type=incoming`, no privados, `content_type=text` y con contenido no vacío. El resto se
     ignora devolviendo `handled=false` con una razón.
2. **Deduplicación + persistencia** ([app/chatwoot_service.py](app/chatwoot_service.py)).
   - Se calcula un `event_key` (por `x-chatwoot-delivery` si existe, si no `message:{conv}:{msg}`).
   - `mark_event_received` inserta en `chat_processed_events`; si choca por unicidad, es duplicado y se
     responde `status=duplicate` sin reprocesar.
   - Se hace `get_or_create_conversation`, se guarda el mensaje del usuario en `chat_messages` con
     `processing_status='pending'`, y se encola un job en `chat_webhook_jobs`.
3. **Debounce + encolado en Celery**.
   - Se setea una clave de debounce en Dragonfly y se despacha la tarea
     `process_chatwoot_conversation` a la cola `chatwoot_messages` con `countdown` =
     `ODRANID_CHATWOOT_DEBOUNCE_SECONDS` (junta mensajes seguidos del mismo cliente).
   - El webhook responde de inmediato `status=queued` (procesamiento asíncrono).
4. **Procesamiento en el worker** ([app/tasks/chatwoot_tasks.py](app/tasks/chatwoot_tasks.py)).
   - Si el debounce sigue activo, se re-encola una sola vez (clave `requeue` con `nx`).
   - Se toman **dos locks** en cascada: un lock en Dragonfly (`SET nx ex`) y un lock en Postgres
     (`locked_until` sobre la fila de la conversación). Si alguno está ocupado, se re-encola.
   - Con los locks, `process_pending_conversation_messages` ([chatwoot_service.py](app/chatwoot_service.py))
     junta todos los mensajes `pending` de la conversación, los concatena, y llama al agente.
5. **Generación de la respuesta**.
   - `build_agent_response_for_pending_messages` decide si resetea el estado, recupera el historial
     reciente (`ODRANID_CHATWOOT_HISTORY_LIMIT`, por defecto 8), corre el análisis con memoria
     (`analyze_with_memory`), recalcula el estado (`build_memory_state`) e invoca `run_agent`.
   - `run_agent` → `run_team` (equipo Agno, ver sección 5) produce un `AgentResponse(answer, tool_calls)`.
6. **Salida**.
   - La respuesta del asistente se guarda en `chat_messages`, se actualiza `state` de la conversación,
     se marcan los mensajes como `processed`, y se crea una fila en `chat_outbox_messages` (con
     `idempotency_key` derivada del contenido).
   - Se despacha `send_chatwoot_outbound_message` a la cola `chatwoot_outbound`, que llama a la API de
     Chatwoot (`POST .../conversations/{id}/messages`, `message_type=outgoing`) y marca el outbox como `sent`.
   - Tareas **beat** periódicas reintentan jobs colgados, despachan outbox pendientes, re-encolan
     conversaciones atascadas y limpian locks vencidos (cada 1–15 min).

---

## 5. El agente

- **Framework:** **Agno** (`agno>=2.6`), con modelos OpenAI (`OpenAIChat`).
- **Topología:** un "equipo" de dos agentes coordinados en secuencia por
  [app/agents/odranid_team.py](app/agents/odranid_team.py) (`run_team`):

  1. **RequirementsAgent** ([requirements_agent.py](app/agents/requirements_agent.py)):
     recibe la conversación y devuelve un objeto estructurado `ProductIntakeResponse`
     (`intent`, `known` dict, `missing`, `should_search`, `next_question`, `confidence`). Usa
     `output_schema=ProductIntakeResponse` + `use_json_mode=True`. Su system prompt (extenso, embebido
     en el `.py`) contiene toda la lógica de clasificación por rubro (pisos, mangueras, mascotas,
     hogar, calzado, general), reglas de slots, correcciones, y qué cuenta como mensaje "operativo"
     (visita, precio, envío → `intent=null`). Si el LLM falla, devuelve un intake neutro (no hay
     fallback por keywords).
  2. **CatalogAgent** ([catalog_agent.py](app/agents/catalog_agent.py)): agente conversacional con
     **tool-use nativo de Agno**. Su única herramienta es `buscar_productos`. Genera el texto final
     que se le manda al cliente.

  Si el RequirementsAgent determina que faltan datos (`should_search=false` y hay `next_question`),
  `run_team` devuelve directamente esa pregunta **sin** llamar al CatalogAgent.

- **Construcción del prompt** ([app/agent.py](app/agent.py) `build_system_prompt`): toma el archivo
  [prompt_agente_odranid.md](prompt_agente_odranid.md) y le concatena `## CONTEXTO DINAMICO ACTUAL`
  (resumen del catálogo desde la base) más un bloque `PRECONTEXTO RAG DE LA CONVERSACION`
  ([rag_precontext.py](app/rag_precontext.py)) con el intake del LLM en JSON. El prompt define tono
  (español rioplatense, WhatsApp), reglas duras (no inventar productos/medidas/links, no mostrar
  precios, renombrar "Simil goma"→"PVC", "ranurado"→"con diseño"), flujos por rubro, datos de
  contacto fijos y derivación a asesor.

- **La tool `buscar_productos`** ([app/tools/buscar_productos.py](app/tools/buscar_productos.py)):
  decorada con `@agno.tools.tool`. Firma expuesta al LLM: `buscar_productos(query: str, limit: int)`.
  Internamente la función de búsqueda real se **inyecta** desde `main.py`, ya pre-cargada con los
  filtros derivados del intake del LLM (`_search_with_intake_filters`), de modo que el LLM solo aporta
  la query en lenguaje natural. Devuelve un JSON compacto de resultados (`compact_search_response`).

---

## 6. La búsqueda de productos

Hay **dos motores** detrás de la misma interfaz `SearchRequest → SearchResponse`:

- **Producción: `DatabaseCatalogSearch`** ([app/db_search.py](app/db_search.py)). Se activa cuando hay
  `OPENAI_API_KEY` + `DATABASE_URL`.
  1. Embebe la query con OpenAI (`text-embedding-3-small`).
  2. Llama a la función SQL `search_catalog_products(...)` en Postgres, que hace **ANN con pgvector**
     (`embedding <=> query_embedding`, índice HNSW coseno) y aplica filtros por columnas: `rubro`,
     `category`, `floor_kind`, `floor_design`, `espesor_mm`, `ancho_m`, `material`, `color`, `tags`,
     `in_stock_only`.
  3. Los filtros vienen del intake del LLM (`filters_from_intake` en [query_parser.py](app/query_parser.py)),
     que además aplica reglas como excluir `pisos_vinilicos` salvo que el cliente lo pida explícitamente.
  4. **Relajación progresiva**: si la búsqueda estricta no devuelve nada, reintenta soltando filtros en
     pasos predefinidos (`ancho_m` → `espesor_mm` → ambos → `color` → `material` → `floor_design` → todos).
  5. **Post-filtro por términos específicos**: para ciertas palabras (tejo, frisbee, medidas de manguera
     como `1/2`, largos en metros) exige que el texto del producto las contenga.
  6. `coverage.enrich_search_response` agrega cálculo de cobertura (m²/rollos/metros lineales) cuando la
     query menciona metros cuadrados.
- **Fallback local: `CatalogSearch`** ([app/retrieval.py](app/retrieval.py)). Sin base de datos: scoring
  léxico (tokenización + coincidencia de términos) + boost por facetas y stock, con la misma lógica de
  relajación y post-filtro. Se carga desde `productos.json` o desde WooCommerce.

El **contexto del catálogo** que ve el prompt (`catalog_context`) se genera con facetas reales de la
base (`catalog_facets` RPC): rubros, espesores y anchos disponibles, diseños, etc., para que el agente
no ofrezca medidas inexistentes.

---

## 7. Estado y memoria de la conversación

Todo vive en **Postgres** ([app/chat_memory.py](app/chat_memory.py), `ChatMemoryStore`, con
connection pool por proceso). Tablas (creadas por las migraciones `003_chat_memory` y `004_...outbox`):

- `chat_conversations` — una fila por conversación (channel + external_conversation_id). Guarda
  `state` (JSONB) y `locked_until` (lock pesimista).
- `chat_messages` — historial completo (user/assistant), con `processing_status`
  (`pending`/`processing`/`processed`), `tool_calls`, etc.
- `chat_processed_events` — deduplicación de webhooks por `event_key`.
- `chat_webhook_jobs` — cola persistente de trabajos (estados `queued`/`processing`/`retry`/`completed`/`failed`).
- `chat_outbox_messages` — patrón outbox para el envío saliente, con `idempotency_key`.

**Memoria de trabajo (`state`):** es un dict con `intent`, `known` (datos ya recopilados: rubro,
espesor, ancho, m², diseño, uso, etc.), `missing`, `pending_slot`, `last_question`, `should_search`.
En cada turno:
- `merge_known_state` fusiona lo previo con lo nuevo extraído por el LLM (con reglas de qué slots
  conservar según el rubro y limpieza de correcciones).
- `apply_pending_slot_to_message` interpreta respuestas cortas según la última pregunta (ej.: si se
  preguntó el ancho y el cliente responde "2", se transforma en "2 de ancho").
- `history_from_state` re-inyecta los datos conocidos como un mensaje sintético
  `"Datos ya recopilados: ..."` para que el agente no vuelva a preguntar.
- El historial reciente para el LLM se limita a `ODRANID_CHATWOOT_HISTORY_LIMIT` (8) mensajes.

---

## 8. Integraciones

- **Chatwoot / WhatsApp** ([app/chatwoot.py](app/chatwoot.py), [chatwoot_service.py](app/chatwoot_service.py)):
  entrada por webhook firmado, salida por la API REST de Chatwoot
  (`/api/v1/accounts/{id}/conversations/{id}/messages`). WhatsApp es el canal subyacente de Chatwoot
  (el código habla en términos genéricos de Chatwoot, channel = `"chatwoot"`).
- **WooCommerce (catálogo)** ([app/woocommerce.py](app/woocommerce.py)): ingesta paginada desde la
  **Store API** pública (`/wp-json/wc/store/v1/products`), filtrando por `stock_status`. Los productos
  se normalizan/clasifican ([normalization.py](app/normalization.py): `classify_product`,
  `extract_specs`, etc.) y se suben a Postgres con embeddings vía `scripts/sync_catalog.py`
  (con caché de embeddings en `.cache/embeddings.json`). También hay endpoints admin
  (`/admin/reload`, `/admin/fetch-woocommerce`) para recarga manual.
- **Audios / transcripción:** **no existe**. El webhook ignora explícitamente todo lo que no sea
  `content_type=text`; no hay integración de voz/Whisper.
- **Retargeting / seguimientos automáticos:** **no existe** como feature de negocio. Las únicas tareas
  programadas (Celery beat) son de robustez de la cola (reintentos, despacho de outbox, limpieza de
  locks), no campañas ni follow-ups al cliente.
- **OpenAI** ([app/embeddings.py](app/embeddings.py) y Agno): embeddings vía HTTP directo (`urllib`),
  y los LLMs vía el cliente OpenAI que usa Agno por dentro.

---

## 9. Cosas incompletas, duplicadas o a medio hacer (descripción objetiva)

- **`app/agent.py` marcado como DEPRECATED pero todavía en uso.** Su docstring dice "reemplazado por
  catalog_agent / se elimina en Fase 3", pero `build_system_prompt`, `clamp_int` y
  `compact_search_response` siguen siendo importados por la capa nueva (catalog_agent y la tool).
  Conviven funciones realmente sin uso en ese archivo (p. ej. `response_from_search_response`,
  `format_hit`, `search_intro`), que arman respuestas formateadas que el pipeline LLM ya no llama.
- **Migraciones duplicadas y divergentes.** Existen `postgres/migrations/` y `supabase/migrations/`
  con los mismos 4 nombres de archivo (`001`–`004`) pero **contenido distinto** (difieren en tamaño y
  en bytes). Además hay un `postgres/schema.sql` que repite el esquema base. No queda claro cuál es la
  fuente de verdad.
- **Documentación desalineada con el código.** `AGENT.md` describe Supabase como "base principal de
  producción" y cita `app/supabase_store.py`, **que no existe**. No hay ninguna referencia a Supabase
  en `app/`. La realidad del código es Postgres directo + pgvector.
- **Dos caminos de intake en paralelo.** El endpoint `POST /agent/respond` (`run_team`) llama a
  `analyze_requirements` directamente, mientras que el flujo de Chatwoot pasa por
  `analyze_with_memory` + `build_memory_state` (con normalización de slot pendiente y fusión de estado).
  Hay funciones de recálculo de estado/slots (`recompute_missing_slots`, `recompute_next_question`,
  `build_memory_state`) que solo se ejercitan en el camino de Chatwoot; la lógica de "próxima pregunta"
  está parcialmente repetida entre el prompt del RequirementsAgent y `slot_questions.py`.
- **Acoplamiento cruzado entre los dos motores de búsqueda.** `retrieval.py` (búsqueda local) importa
  `post_filter_specific_terms` desde `db_search.py` (búsqueda de base), de modo que el "fallback local"
  depende del módulo de base de datos.
- **Conexiones por operación.** `DatabaseCatalogSearch` abre una conexión psycopg nueva en cada método
  (`search`, `count_products`, `catalog_facets`), por lo que una sola búsqueda puede abrir varias
  conexiones; no usa el pool que sí emplea `ChatMemoryStore`.
- **Pre-contexto RAG anunciado pero no provisto.** El prompt y `rag_precontext.py` mencionan "candidatos
  iniciales del catálogo cuando existen", pero `build_rag_precontext` no ejecuta ninguna búsqueda ni
  incluye candidatos: solo vuelca el intake y el historial.
- **`confidence` siempre 0.** `ProductIntakeResponse.confidence` tiene default 0 y no se observa que el
  pipeline lo complete o lo use para decisiones.
- **Repo sin historial git.** La rama actual (`master`) no tiene ningún commit; todo el proyecto figura
  como archivos sin trackear. El `.env` real está en el árbol de trabajo.
- **Artefactos de experimentación versionados.** El directorio `reports/` contiene numerosas salidas de
  "shadow testing"/replays (`.jsonl`, `.md`, `.json`) y hay scripts asociados
  (`replay_chatwoot_conversations.py`, `analyze_shadow_report.py`); son herramientas de evaluación, no
  parte del runtime.
- **Lógica de clasificación en dos lugares.** El rubro/categoría de un producto se decide por keywords
  en `normalization.classify_product` (sobre datos de WooCommerce), mientras que la intención del
  cliente se decide por LLM en `requirements_agent`. Son sistemas independientes con vocabularios que
  deben mantenerse coherentes a mano.
- **Sin gestión de dependencias reproducible.** Solo hay `requirements.txt` con rangos `>=` (sin
  lockfile, sin `pyproject.toml`), por lo que las versiones exactas instaladas dependen del momento del
  build (la única versión "fijada" observable es la instalada de Agno: 2.6.11).
