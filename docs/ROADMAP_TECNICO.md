# Roadmap de migración — Agente Odranid

> Documento maestro para ejecutar con agentes (Claude Code) dentro del repo.
> Es auto-contenido: incluye el contexto necesario para trabajar sin haber visto
> las conversaciones previas. Se ejecuta **por fases, en orden**. Cada fase queda
> probada y funcionando antes de pasar a la siguiente.

---

## 0. Contexto del proyecto

Odranid es un agente de IA que atiende clientes por **WhatsApp** (vía **Chatwoot**) para una
empresa argentina de productos de goma/caucho/PVC, mangueras y juguetes para mascotas. El
cliente escribe, el agente entiende qué busca, lo busca en el catálogo y responde con opciones
y links de compra. Es una reescritura en Python de un flujo que antes corría en n8n.

### Qué ya existe y es sólido (NO rehacer)
- API en **FastAPI**.
- **PostgreSQL + pgvector** con índice HNSW coseno y función SQL de búsqueda.
- Sistema robusto de mensajería en Postgres: cola persistente de jobs, deduplicación por
  `event_key`, patrón **outbox** con `idempotency_key`, locks en cascada (Dragonfly + Postgres).
- Estado de conversación con slots (`known` / `missing` / `pending_slot`) en Postgres.
- Ingesta de catálogo desde la **Store API de WooCommerce** + normalización + embeddings.
- Integración con Chatwoot (webhook firmado HMAC de entrada, API REST de salida).
- Worker con **Celery** + **Dragonfly** (compatible con Redis).
- Suite de tests (pytest) y herramientas de shadow-testing / replay de conversaciones reales.

### Lo que está mal o sobra
- Agente partido en **dos LLM** (Agno: RequirementsAgent + CatalogAgent), con filtros inyectados
  por detrás de la tool.
- Sin validación en código de la salida del agente (puede inventar links/productos).
- Búsqueda por **filtros duros con relajación** en vez de scoring; no garantiza traer alternativas.
- Cruft: código muerto, migraciones duplicadas (`postgres/` vs `supabase/`), docs desalineadas,
  dos caminos de intake, dos motores de búsqueda acoplados, conexiones por operación, sin
  lockfile, **sin historial git y con `.env` en el árbol del repo**.
- Sin observabilidad del agente.

---

## 1. Stack objetivo (revisado sobre lo que ya hay)

| Pieza | Decisión | Por qué |
|---|---|---|
| FastAPI | **Conservar** | Ya está y funciona. |
| PostgreSQL + pgvector | **Conservar** (arreglar la búsqueda dentro) | Migrar a otro motor ahora sería tirar trabajo. |
| Celery | **Conservar** (no migrar a Arq) | El outbox/dedup/locks están construidos alrededor; migrar es churn sin ganancia a esta escala. |
| Dragonfly | **Conservar** | Compatible con Redis, ya integrado. |
| Agno | **Reemplazar por PydanticAI** | Un solo agente con tools tipadas; ataca la causa raíz. |
| Logfire | **Agregar** | Observabilidad nativa con PydanticAI. |
| Typesense | **Evaluar en Fase 3** | Solo si pgvector arreglado no alcanza en relevancia. |
| Whisper / Retargeting | **Decisión de negocio (Fase 4)** | Estaban en n8n, se cayeron en la reescritura. |

> Principio: el objetivo no es "matchear una lista de tecnologías", es **arreglar los 4 problemas
> y sacar el cruft**. Si una pieza del stack no se gana el lugar a esta escala, se discute.

---

## 2. Los 4 problemas a resolver

1. **Alucina** e inventa links/productos.
2. **Pregunta dos veces** lo mismo.
3. **No toma bien** lo que el cliente pide.
4. **Búsqueda ineficiente:** no siempre trae lo correcto y no ofrece alternativas similares.

## 3. Principios de arquitectura (qué arregla cada cosa)

1. **Tools tipadas** (PydanticAI) → mata la doble extracción y el "no toma lo que pide".
2. **Búsqueda por puntajes con fallback a similares** → mata el "no trae nada / no trae parecidos".
3. **Estado explícito de conversación** (ya existe, conectarlo bien) → mata el "pregunta dos veces".
4. **Validación en código de la salida** (rechazar lo que no vino de la tool) → mata las alucinaciones.

---

## Reglas transversales (válidas en TODAS las fases)

- Trabajar siempre en una **rama por fase** (`limpieza-fase-1`, `agente-fase-2`, etc.).
- Un cambio lógico por commit, con mensaje claro.
- Correr **pytest antes y después de cada paso**.
- Usar la **suite de shadow-testing / replay** como red de seguridad: correr conversaciones
  reales antes y después de cada fase para verificar que se mejora sin romper.
- **Nada de cambios a ciegas.** Cada fase queda probada y funcionando antes de la siguiente.
- Si un cambio se sale del alcance de la fase actual, no resolverlo ahí: marcarlo
  `// pendiente Fase X` y seguir.

---

# FASE 1 — Limpieza del proyecto

> Higiene y borrado de cruft, **sin cambiar el comportamiento** del agente, el intake ni la
> búsqueda. Lo que toque esa lógica es Fase 2.

### Paso 0 — Punto de partida seguro
- Verificá git. Si la rama actual no tiene commits, `git init` (si hace falta) + commit inicial
  "snapshot inicial antes de limpieza" con todo el árbol.
- Creá la rama `limpieza-fase-1`. Corré `pytest` y guardá el resultado de referencia.

### Paso 1 — Secretos y `.gitignore` (urgente)
- `.gitignore` con al menos: `.env`, `.env.*` (excepto `.env.example`), `.cache/`, `reports/`,
  `__pycache__/`, `*.pyc`, `.venv/`, `*.egg-info/`.
- `git rm --cached .env` (sin borrarlo del disco). Conservá `.env.example`.
- En el resumen final listá los **nombres** de los secretos a rotar (sin valores): API key de
  OpenAI, token de Chatwoot, webhook secret de Chatwoot, credenciales de WooCommerce, URL/clave
  de Postgres. La rotación la hace el dueño en cada servicio.

### Paso 2 — Una sola fuente de verdad para migraciones
- La base se construye con `postgres/migrations/*.sql` (servicio `migrate` del compose). Esa es la verdad.
- Hacé `diff` entre cada `supabase/migrations/00X` y su par `postgres/migrations/00X`; dejá el
  resumen en el commit.
- Borrá `supabase/migrations/` completo.
- `postgres/schema.sql`: grepeá referencias. Si solo duplica las migraciones y nadie lo usa,
  borralo o rotulalo como snapshot generado. No lo borres si algo lo referencia.

### Paso 3 — Documentación desalineada
- Sacá de `AGENT.md` y demás `.md` toda referencia a Supabase como base de producción y a
  `app/supabase_store.py` (no existe). Es Postgres directo + pgvector.
- Verificá que comandos y variables documentados coincidan con el código real.

### Paso 4 — Código muerto en `app/agent.py`
- **Solo borrar lo provablemente sin uso. No mover ni renombrar lo vivo** (eso es Fase 2).
- Grepeá cada símbolo. Sin referencias (esperados: `response_from_search_response`, `format_hit`,
  `search_intro`) → borrar. En uso (`build_system_prompt`, `clamp_int`, `compact_search_response`)
  → dejar donde están.
- Quitá el docstring "DEPRECATED / se elimina en Fase 3". Corré tests.

### Paso 5 — Romper el acoplamiento entre motores de búsqueda
- `retrieval.py` importa `post_filter_specific_terms` de `db_search.py`. Mové los helpers
  compartidos a `app/search_common.py` y que ambos importen de ahí. **Sin cambiar lógica.**
- La decisión de eliminar `retrieval.py` queda para Fase 3. Corré tests.

### Paso 6 — Sacar el tooling de experimentación del runtime
- `reports/` en `.gitignore` y fuera de la imagen de deploy.
- `scripts/replay_chatwoot_conversations.py` y `analyze_shadow_report.py`: dejarlos en `scripts/`
  pero revisar que el `Dockerfile` no los meta al runtime productivo si no se usan ahí.

### Paso 7 — Conexiones a Postgres por operación
- En `db_search.py`, `DatabaseCatalogSearch` abre conexión nueva por método. Que use el connection
  pool, mismo patrón que `ChatMemoryStore`. **No cambia resultados.**
- Si te obliga a cambiar la firma que usa el agente, frená y marcá `pendiente Fase 2`. Corré tests.

### Paso 8 — Dependencias reproducibles
- Migrar de `requirements.txt` (rangos `>=`) a `pyproject.toml` + lockfile (preferido: **uv**).
- **Fijar las versiones exactas instaladas hoy** (no actualizar nada).
- Actualizar el `Dockerfile` para instalar desde el lock. Levantar con compose y verificar arranque.

### Cierre Fase 1
- Mergear (o dejar PR). Entregar resumen: qué se borró/movió, diff supabase vs postgres,
  nombres de secretos a rotar, si algún test/arranque necesitó atención.
- **No avanzar al agente.**

---

# FASE 2 — El agente (PydanticAI)

> Ataca 3 de los 4 problemas: alucina, pregunta dos veces, no toma lo que pide.
> Rama: `agente-fase-2`.

### Alcance
- Reemplazar el **equipo de dos agentes Agno** (RequirementsAgent + CatalogAgent) por **un solo
  agente PydanticAI**.
- Tool `buscar_productos` **tipada**: el agente emite campos estructurados y validados
  (`rubro`, `tipo`, `espesor_mm`, `ancho_m`, `material`, `query_semantica`, etc.), no un string
  libre con filtros inyectados por detrás. Una sola pasada de LLM.
- Tool `calculator` (o cálculo en código) para cobertura: reutilizar la lógica de `coverage.py`.
- **Validación de salida en código** (post-procesamiento): rechazar o limpiar todo link o
  producto que no provenga del resultado de la tool. Esta es la barrera anti-alucinación.
- **Formateo de links para WhatsApp en código**, no con un segundo LLM.
- Conectar el **estado de conversación existente** (slots `known`/`missing`/`pending_slot`) al
  agente nuevo. Consolidar el camino de intake duplicado (`/agent/respond` vs flujo Chatwoot) en uno.
- Agregar **Logfire** con la integración nativa de PydanticAI (trazas de cada corrida del agente).
- Mover/renombrar los helpers vivos que quedaron en `app/agent.py` (pendiente de Fase 1) a su lugar
  definitivo, ahora que el agente se reescribe.

### Validación
- Correr la suite de **shadow/replay** sobre conversaciones reales antes y después; comparar que
  no se rompe y que mejora. Revisar trazas en Logfire.

> La brief detallada paso a paso de esta fase se escribe al cerrar la Fase 1, con el repo ya limpio.

---

# FASE 3 — La búsqueda (dolor #1)

> Rama: `busqueda-fase-3`.

### Alcance
- Pasar de "filtro duro + relajación progresiva" a **scoring real**: el `rubro` como único filtro
  duro; los atributos (espesor, ancho, tipo, material) **suman en el ranking**, no excluyen.
- Garantizar **siempre** el fallback a similares: devolver top-N marcando cuáles coinciden exacto
  y cuáles son alternativas.
- **Una sola fuente** para los sinónimos del dominio (goma=caucho, simil goma=PVC,
  ranurado=con diseño, variantes de "ignífugo") y para la clasificación de rubro, en vez de
  repartida entre el prompt y `normalization`.
- Con datos de relevancia reales, **decidir si pgvector alcanza o si conviene Typesense**
  (híbrida + sinónimos + tolerancia a typos). Si se va a Typesense, sumar el paso de
  sincronización del catálogo al índice.
- Si el segundo motor (`retrieval.py`) ya no aporta, **eliminarlo**.

### Validación
- Medir relevancia con un set de queries reales (usar el replay): % de búsquedas que traen el
  producto correcto y % que ofrecen alternativa útil cuando no hay match exacto.

> La brief detallada se escribe al cerrar la Fase 2.

---

# FASE 4 — Features que se cayeron en la reescritura

> Decisión de negocio. Entran solo si se las quiere. Rama: `features-fase-4`.

- **Audio (Whisper):** transcribir las notas de voz entrantes antes de procesarlas (estaba en n8n).
- **Retargeting:** seguimiento automático a las ~8 horas sin actividad, una sola vez por
  conversación (estaba en n8n; la infra de jobs/beat para esto ya existe).

---

## Cómo usar este documento

1. Ejecutar las fases **en orden**. No saltear.
2. Respetar las **reglas transversales** en todas.
3. Al cerrar cada fase, entregar el resumen de cierre y recién entonces escribir la brief
   detallada de la siguiente (con los resultados reales en la mano).
4. La suite de shadow-testing es la red de seguridad: si una fase no mejora o rompe algo, se
   frena y se corrige antes de avanzar.