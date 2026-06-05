# AGENT - Continuidad Del Proyecto Odranid

Este archivo es la memoria operativa para seguir el proyecto sin perder el hilo. El objetivo no es parchar un caso puntual del cliente, sino construir un microservicio robusto, escalable y mantenible para busqueda y recomendacion de productos Odranid.

## Norte Del Proyecto

Construir un microservicio Python que reemplace el flujo fragil de n8n para catalogo/RAG.

Principio rector:

- La IA conversa.
- El microservicio gobierna la busqueda, facets, calculos, normalizacion y reglas duras.
- WooCommerce es la fuente de verdad del catalogo.
- Postgres directo + pgvector es la base principal de produccion.
- El prompt del agente no debe contener logica pesada ni catalogo completo.

## Arquitectura Actual

Componentes principales:

- `app/main.py`: FastAPI, endpoints publicos/admin.
- `app/woocommerce.py`: ingesta desde WooCommerce Store API.
- `app/normalization.py`: transforma productos WooCommerce en documentos normalizados.
- `app/query_parser.py`: SOLO `filters_from_intake()` — convierte el intake estructurado del LLM en `ProductFilters`. Ya no infiere nada por keywords.
- `app/slot_questions.py`: helpers deterministas de slot/cálculo sobre el estado YA estructurado por el LLM (`floor_next_question`, `hose_next_question`, `derived_roll_surface_m2`). No es extracción de intención.
- `app/agents/requirements_agent.py`: ÚNICO analizador de intención/estado. LLM con `output_schema=ProductIntakeResponse`. No hay fallback por keywords.
- `app/chat_memory.py`: memoria conversacional persistente, dedupe, jobs y locks contra Postgres.
- `app/db_search.py`: busqueda vectorial/facetada contra Postgres directo.
- `app/coverage.py`: calcula cobertura para pisos cuando el cliente pide m2.
- `app/chatwoot.py`: integracion Chatwoot webhook + envio de respuestas.
- `app/models.py`: contratos Pydantic.
- `app/postgres_store.py`: upsert directo a Postgres.
- `scripts/sync_catalog.py`: sincronizacion del catalogo desde WooCommerce a Postgres.
- `postgres/migrations/`: schema/RPC principal de produccion aplicado por el servicio `migrate` de Docker Compose.

Endpoints importantes:

- `GET /health`
- `GET /catalog/context`
- `POST /intake/analyze`
- `POST /search`
- `POST /agent/respond`
- `GET /webhooks/chatwoot/health`
- `POST /webhooks/chatwoot`
- `POST /admin/reload`
- `POST /admin/fetch-woocommerce`

## Pipeline LLM-Only (Sin Keywords) — DECISIÓN VIGENTE

**Las capas de detección por keywords fueron eliminadas por completo.** El sistema ya no
intercepta ni clasifica mensajes con listas de palabras. Todo mensaje pasa por el equipo Agno
(RequirementsAgent → CatalogAgent). El criterio del agente nunca se pisa con código.

Borrado definitivo (NO recrear):

- `app/conversation_policy.py` — reja de ~15 respuestas canned (`LOCATION_REPLY`, `SHIPPING_REPLY`,
  `PAYMENT_REPLY`, `ADVISOR_REPLY`, etc.) gateadas por listas de patrones. Era redundante con el
  prompt (que ya tiene dirección, envíos, derivaciones) e interceptaba antes del LLM. **ELIMINADO.**
- `app/conversation_router.py` — `route_conversation` / `state_after_route`. Solo envolvía la policy. **ELIMINADO.**
- `app/product_intake.py` — intake determinista por árboles de keywords (`analyze_product_intake`,
  `detect_*`, `is_*`, `extract_contextual_*`). **ELIMINADO.** Lo único rescatado (slot/cálculo) vive en
  `app/slot_questions.py`.
- `app/query_parser.py::infer_filters_from_query` y todo su árbol (`has_wood_design`, etc.). **ELIMINADO.**
  Solo queda `filters_from_intake` (estructurado).

Reglas de oro:

- **No agregar listas de keywords para detectar intención, disponibilidad, ubicación, envío, pago, etc.**
  Si un caso de lenguaje se clasifica mal, se corrige mejorando el prompt del RequirementsAgent
  (Regla 10: operativo/institucional → `intent=null`) o del CatalogAgent — nunca con un `if "palabra" in texto`.
- Lo institucional (dirección, horario, links, asesor) vive TEXTUAL en `prompt_agente_odranid.md`
  (sección "DATOS DE CONTACTO — COPIAR TEXTUAL"). El CatalogAgent lo responde desde ahí.
- Sin `OPENAI_API_KEY` el servicio responde 503: ya no degrada a keywords. El modo local/dev no es runtime productivo.
- `app/chat_memory.py` conserva heurísticas mínimas de slot/estado (`should_reset_conversation_state`,
  `recompute_missing_slots`) que operan SOBRE el estado del LLM. Son plumbing de memoria, no
  interceptan ni clasifican intención. Si crecen en complejidad de keywords, revisar.

## Decisiones Ya Tomadas

- No depender de `productos.json` en produccion. Queda como fixture/snapshot de desarrollo.
- No volver a n8n como lugar de logica principal.
- Mantener Postgres directo + pgvector como backend principal de runtime.
- No hacer que el agente arme SQL ni filtros estrictos manualmente.
- No interpretar `2m2` como `ancho_m = 2` ni como `espesor_mm = 2`.
- El agente debe mandar query natural a `buscar_productos`.
- El microservicio decide facets, relajacion de filtros y calculos.
- El RequirementsAgent (LLM) determina `should_search`. El CatalogAgent decide y ejecuta `buscar_productos`. No hay búsqueda directa por keywords ni intercepción previa.
- El prompt `prompt_agente_odranid.md` es comportamiento conversacional + institucional, no motor de busqueda.
- El contexto cacheado del catalogo debe venir desde `GET /catalog/context`.
- La recopilacion de datos minimos por rubro la hace el RequirementsAgent (LLM, `app/agents/requirements_agent.py`); las preguntas de slot deterministas viven en `app/slot_questions.py`.
- Si el cliente pide superficie, `/search` debe devolver `coverage` por hit cuando sea posible.
- La memoria conversacional de produccion debe vivir en Postgres, no depender solo del historial que envie Chatwoot en cada webhook.
- Redis puede agregarse despues para locks, deduplicacion y cache temporal, pero no como memoria principal.
- MongoDB no es necesario para esta etapa: Postgres `jsonb` cubre bien el estado flexible de conversacion.

## Contrato De Busqueda

Input recomendado para la herramienta del agente:

```json
{
  "query": "tenes piso moneda 3mm de 1.20 de ancho para cubrir 20m2",
  "limit": 5
}
```

El agente no debe separar filtros tecnicos salvo que en el futuro se disene explicitamente otro contrato.

Output relevante:

- `hits[].product`: producto real.
- `hits[].matched_filters`: filtros aplicados.
- `hits[].relaxed_filters`: filtros relajados.
- `hits[].coverage`: calculo de cantidad/cobertura si aplica.
- `used_relaxation`: indica si se devolvieron alternativas cercanas.
- `requested_m2`: superficie pedida detectada en la query.

## Contrato Del Agente

Endpoint conversacional:

```http
POST /agent/respond
```

Input:

```json
{
  "message": "tenes piso moneda para cubrir 20m2",
  "history": [],
  "limit": 5
}
```

El agente usa `prompt_agente_odranid.md`, inyecta el contexto actual de `GET /catalog/context`, decide si llama `buscar_productos` y redacta la respuesta final.

`/agent/respond` corre el equipo Agno (RequirementsAgent → CatalogAgent). No hay gate de keywords antes del modelo: si la consulta es amplia, el propio agente pide la precisión que falta.

El tool output que recibe el modelo esta sanitizado: no incluye precios.

## Contrato Chatwoot

Endpoint webhook:

```http
POST /webhooks/chatwoot
```

Health:

```http
GET /webhooks/chatwoot/health
```

Reglas:

- Escuchar en Chatwoot solo el evento `message_created`.
- Responder solo si `message_type=incoming`, `content_type=text`, no privado y con contenido.
- Ignorar `outgoing` para evitar loops.
- Verificar `X-Chatwoot-Signature` si `ODRANID_CHATWOOT_WEBHOOK_SECRET` esta configurado.
- Usar `ODRANID_CHATWOOT_AUTO_REPLY=false` para pruebas donde se quiere ver el output sin publicar en Chatwoot.
- Enviar respuestas a Chatwoot con `POST /api/v1/accounts/{account_id}/conversations/{conversation_id}/messages`.
- Deduplicar por `X-Chatwoot-Delivery`, o por `conversation_id + message_id` si no viene ese header.
- Con `ODRANID_DATABASE_URL` configurado, el webhook guarda evento, encola job, procesa en background y persiste memoria por `conversation_id`.

## Memoria Conversacional

Objetivo:

- Manejar conversaciones por partes sin perder contexto.
- Saber que una respuesta corta como `2m` contesta el slot que el bot pidio antes, por ejemplo `ancho_m`.
- Evitar depender del historial incluido por Chatwoot, porque puede venir incompleto o variar segun canal/integracion.
- Auditar mensajes reales y respuestas del agente.

Backend recomendado:

- Postgres como memoria persistente principal.
- Redis solo si mas adelante hace falta para deduplicacion distribuida, locks o cache de pocos minutos.
- No sumar MongoDB salvo que aparezca una necesidad fuerte fuera de Postgres.

Implementado en:

- `app/chat_memory.py`
- `postgres/migrations/003_chat_memory.sql`

Tablas:

```sql
chat_conversations
- id
- channel
- external_conversation_id
- external_contact_id
- state jsonb
- last_seen_at
- created_at
- updated_at

chat_messages
- id
- conversation_id
- external_message_id
- role
- content
- raw_payload jsonb
- created_at

chat_processed_events
- event_key
- channel
- external_conversation_id
- external_message_id
- status
- error

chat_webhook_jobs
- id
- event_key
- channel
- external_conversation_id
- status
- attempts
- error
```

Estado sugerido en `chat_conversations.state`:

```json
{
  "intent": "pisos",
  "known": {
    "requested_m2": 50,
    "floor_design": "moneda",
    "espesor_mm": 3
  },
  "missing": ["ancho_m"],
  "pending_slot": "ancho_m",
  "last_question": "¿Qué ancho buscás?",
  "last_search_query": null
}
```

Flujo esperado:

```text
Chatwoot webhook
-> cargar conversacion por external_conversation_id
-> deduplicar evento y encolar job
-> tomar lock por conversacion
-> combinar mensaje actual + estado + ultimos mensajes persistidos
-> correr RequirementsAgent (LLM) via analyze_with_memory
-> actualizar estado/slots (build_memory_state) sobre el intake del LLM
-> correr el equipo Agno (CatalogAgent): responde institucional o llama buscar_productos
-> guardar mensaje entrante
-> guardar mensaje saliente
-> persistir state actualizado
-> liberar lock
```

Reglas importantes:

- `conversation_id` de Chatwoot debe ser la clave externa principal por conversacion.
- `pending_slot` manda sobre ambiguedades cortas: si `pending_slot=ancho_m` y el usuario dice `2m`, tomarlo como ancho.
- La memoria debe tener limite de historial para el modelo, por ejemplo ultimos 8-12 mensajes, pero conservar auditoria completa en `chat_messages`.
- El estado estructurado debe actualizarse deterministamente antes de llamar al modelo.
- El modelo redacta; el microservicio decide slots, filtros y busqueda.
- Para replay y produccion, cuando hay estado estructurado (`Datos ya recopilados`), la query de busqueda debe priorizar ese estado antes que el historial crudo. Esto evita arrastrar titulos viejos o URLs de `Vengo de la tienda online`.

## Contrato De Intake

Endpoint de diagnostico:

```http
POST /intake/analyze
```

Input:

```json
{
  "query": "piso para cubrir 20m2",
  "history": []
}
```

Output:

```json
{
  "intent": "pisos",
  "known": {"rubro": "pisos", "requested_m2": 20},
  "missing": ["floor_kind_or_design", "espesor_mm", "ancho_m"],
  "should_search": false,
  "next_question": "Para buscarlo mejor, decime si lo preferís liso o con diseño, el espesor y el ancho.",
  "confidence": 0.9
}
```

Regla:

- `should_search = true`: buscar productos.
- `should_search = false` con `next_question`: preguntar eso y no llamar al modelo.
- `intent = null`: no bloquear; dejar que el agente responda institucional/saludo/etc.

## Reglas Criticas De Dominio

Pisos:

- `m2`, `m²` y `metros cuadrados` son superficie a cubrir.
- `mm` es espesor.
- `ancho 1.20 m` es ancho.
- Para busqueda fina de pisos, recopilar antes de buscar:
  - liso o con diseno;
  - espesor;
  - ancho;
  - m2 a cubrir.
- `semilla` puede aceptar `semilla_melon` como alternativa compatible.
- Si no hay coincidencia exacta, relajar medidas antes que rubro/diseno.
- Si hay relajacion de filtros, el agente debe decir que son alternativas cercanas.
- Si el cliente pide recomendacion por uso exigente, por ejemplo danza, escenario, gimnasio o alto transito, el intake puede avanzar con espesor recomendado de 3 mm en vez de volver a preguntar espesor.
- Si el cliente pide recomendacion para dormitorio/hogar/oficina, el intake puede sugerir 2 mm cuando falte espesor, siempre que los demas datos minimos esten cubiertos.
- No tomar `piso de madera` como diseno `simil_madera`: eso suele describir el piso base/sustrato. Solo usar `simil_madera` con frases explicitas como `simil madera`, `tipo madera`, `efecto madera` o equivalentes.
- No agregar facet `antideslizante` cuando el cliente lo niega, por ejemplo `no tiene que ser antideslizante`.

Calculos:

- El calculo vive en `app/coverage.py`, no en el prompt.
- Si hay `rendimiento_m2`, usarlo como cobertura preferente.
- Si hay `ancho_m` y `largo_m`, usar `ancho_m * largo_m`.
- Si solo hay `ancho_m`, calcular metros lineales.
- Si el producto se vende como `metro lineal`, la respuesta debe hablar de metros lineales, no rollos. Esto puede inferirse desde titulo/contenido aunque `product_type` haya quedado viejo en la DB.
- Si no hay medidas suficientes, `coverage.needs_advisor = true`.

Comercial:

- No mostrar precios al cliente desde el agente.
- Mantener info institucional, direccion, retiro, envios, derivaciones y certificados en el prompt.
- Derivar al asesor para mayorista, efectivo, instalacion, envios complejos, factura especial, certificados o clientes frustrados.
- Mensajes de proveedores/vendedores externos, por ejemplo propuestas de Mercado Libre o marketing, no deben disparar menu de productos ni busqueda. Responder corto y derivar a asesor/institucional.
- Si el cliente pide vendedor/persona o muestra frustracion explicita (`no me des mas opciones`, `estas dando vueltas`, `ya te dije cual quiero`), derivar sin seguir buscando.

Mascotas:

- Priorizar el dato mas especifico del cliente sobre el titulo del producto anterior. Ejemplo: si el historial dice `perros chicos` pero el cliente dice `pitbull grande`, el estado debe quedar `animal=perro`, `size=grande`.
- Razas como pitbull, rottweiler, dogo u ovejero deben inferir `animal=perro` y normalmente `size=grande`.

## Comandos Utiles

Servidor local:

```bash
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Health:

```bash
curl -s http://127.0.0.1:8000/health | jq
```

Search:

```bash
curl -s -X POST http://127.0.0.1:8000/search \
  -H 'content-type: application/json' \
  -d '{"query":"tenes piso moneda para cubrir 20m2","limit":3}' \
  | jq
```

Ver calculos:

```bash
curl -s -X POST http://127.0.0.1:8000/search \
  -H 'content-type: application/json' \
  -d '{"query":"tenes piso moneda para cubrir 20m2","limit":3}' \
  | jq '.requested_m2 as $m | .hits[] | {requested_m2:$m, title:.product.title, specs:.product.specs, coverage:.coverage}'
```

Probar intake:

```bash
curl -s -X POST http://127.0.0.1:8000/intake/analyze \
  -H 'content-type: application/json' \
  -d '{"query":"piso para cubrir 20m2"}' \
  | jq
```

Probar agente:

```bash
curl -s -X POST http://127.0.0.1:8000/agent/respond \
  -H 'content-type: application/json' \
  -d '{"message":"tenes piso moneda para cubrir 20m2","limit":5}' \
  | jq
```

Sync WooCommerce a DB:

```bash
.venv/bin/python scripts/sync_catalog.py
```

Validacion rapida de Python:

```bash
.venv/bin/python -m compileall app
```

Replay con conversaciones reales exportadas:

```bash
.venv/bin/python scripts/replay_chatwoot_conversations.py \
  --from-file chatwoot_conversaciones.json \
  --use-file-messages \
  --mode all-incoming \
  --intake-only \
  --output reports/chatwoot_replay_727_intake_final.jsonl
```

Shadow mode por lote sin enviar a Chatwoot:

```bash
.venv/bin/python scripts/replay_chatwoot_conversations.py \
  --from-file chatwoot_conversaciones.json \
  --use-file-messages \
  --limit 50 \
  --offset 0 \
  --mode last-incoming \
  --output reports/chatwoot_shadow_50_final.jsonl
```

Analizar reportes shadow y detectar casos sospechosos:

```bash
.venv/bin/python scripts/analyze_shadow_report.py \
  reports/chatwoot_shadow_50_100_after_forced_search.jsonl \
  --limit 40
```

El analizador marca errores, busquedas omitidas, mensajes operativos que disparan tool calls, preguntas repetidas, links sin tool call y respuestas demasiado genericas. No reemplaza la revision humana: sirve para priorizar casos.

## Variables De Entorno

No imprimir secretos de `.env`.

Variables clave:

- `OPENAI_API_KEY` o `ODRANID_OPENAI_API_KEY`
- `ODRANID_DATABASE_URL`
- `ODRANID_AGENT_MODEL`
- `ODRANID_WOOCOMMERCE_BASE_URL`
- `ODRANID_WOOCOMMERCE_PER_PAGE`
- `ODRANID_WOOCOMMERCE_MAX_PAGES`
- `ODRANID_WOOCOMMERCE_STOCK_STATUS`

Prioridad de backend:

- Usar `ODRANID_DATABASE_URL` para Postgres directo + pgvector.
- Si no existe, el servicio puede usar modo local/desarrollo para algunos endpoints, pero no es el runtime productivo.

## Roadmap: Migración a Agno Multi-Agente

### Diagnóstico

El sistema actual tiene dos capas de inteligencia en tensión:

1. **Sistema determinista** (`product_intake.py`, `query_parser.py`, `infer_filters_from_query`): árboles de keywords que detectan intención, filtros y estado del pedido. Cada nuevo caso que no encaja agrega más `if/elif`. No puede manejar correcciones, negaciones, matices ni rubros nuevos sin código nuevo.

2. **Sistema LLM** (`agent.py`, OpenAI GPT-4.1-mini): entiende contexto, correcciones y conversación natural. Pero recibe el estado ya procesado por el sistema determinista, así que hereda sus errores.

El problema raíz: el estado del pedido lo construye código determinista y el agente lo consume. Cuando el código determinista falla (extrae "vinilico" de una corrección, asume pisos vinílicos por keywords), el agente no puede corregirlo porque ya recibió el estado contaminado.

La solución correcta: **el LLM debe ser el responsable de construir y mantener el estado estructurado del pedido**, no el código determinista.

### Solución: Agno Multi-Agente

Agno es un framework Python para sistemas multi-agente. Proporciona:

- `Agent`: agente individual con tools, structured outputs, prompts, storage.
- `Team`: coordinación entre agentes.
- Salidas estructuradas vía Pydantic (devuelve directamente `ProductIntakeResponse`).
- Integración nativa con PostgreSQL, Redis y OpenAI.

La integración no reemplaza FastAPI, Celery ni Chatwoot. Solo reemplaza la capa de inteligencia interna: extracción de estado + respuesta del agente.

### Arquitectura Target

```
Chatwoot webhook → FastAPI → Celery task
                              ↓
                   chat_memory.py (sin cambios)
                   construye historial + estado persistido
                              ↓
                   [OdranidAgnoTeam]
                       │
                       ├── RequirementsAgent (gpt-4.1-mini)
                       │   ├── Input: historial + mensaje actual
                       │   ├── Output: ProductIntakeResponse (Pydantic structured)
                       │   ├── Maneja: intención, filtros, correcciones, faltantes
                       │   └── Reemplaza: analyze_product_intake + infer_filters_from_query
                       │
                       └── CatalogAgent (gpt-4.1-mini)
                           ├── Input: ProductIntakeResponse + historial
                           ├── Tool: buscar_productos → db_search.py (sin cambios)
                           ├── Output: texto final para WhatsApp
                           └── Reemplaza: OpenAIAgentClient en agent.py
                              ↓
                   chat_memory.py persiste respuesta
                              ↓
                   Chatwoot API envía mensaje
```

### RequirementsAgent

**Archivo:** `app/agents/requirements_agent.py`

**Responsabilidad:** Analizar la conversación completa y devolver el estado estructurado del pedido como `ProductIntakeResponse`. Entiende intención, extrae filtros y detecta correcciones sin keywords hardcodeados.

**Structured output:** `ProductIntakeResponse` (Pydantic, mismo modelo actual, mismo contrato).

**Prompt del agente (sistema):**

```
Sos el analizador de requisitos del chatbot de Odranid (goma industrial y accesorios).
Tu tarea: analizar la conversación y devolver un JSON con el estado actualizado del pedido.

RUBROS DISPONIBLES:
- pisos: goma en rollo o planchas. Datos requeridos: floor_kind o floor_design, espesor_mm, ancho_m, requested_m2.
- mangueras: hoses. Datos requeridos: use, diameter, length_m.
- mascotas: juguetes. Datos requeridos: animal, size (salvo toy_type conocido).
- hogar, calzado, general: buscar con cualquier detalle.

REGLAS CRÍTICAS:
- Si el cliente corrige ("no te pedí vinilico", "eso no era", "pero no"), eliminá ese atributo del known.
- category=pisos_vinilicos SOLO si el cliente pide "vinilico", "pvc" o "vinil" explícitamente. Por defecto, pisos son de goma.
- "m2" y "metros cuadrados" son requested_m2 (superficie a cubrir), nunca ancho ni espesor.
- "mm" es siempre espesor_mm.
- should_search=true solo cuando tenés floor_kind/design + espesor_mm + ancho_m + requested_m2 (o modo product_lookup).
- Si el cliente pide recomendación por uso exigente (danza, gimnasio, industrial, alto tránsito), podés asumir espesor_mm=3.
- Si el cliente pide recomendación para hogar/dormitorio/oficina sin especificar espesor, podés asumir espesor_mm=2.
- No asumas simil_madera a menos que el cliente lo diga explícitamente.
- Razas como pitbull, rottweiler, dogo inferir animal=perro y size=grande.

SALIDA: JSON según ProductIntakeResponse. Sin texto extra.
```

**Lo que reemplaza:**
- `analyze_product_intake()` y todas sus sub-funciones de extracción
- `infer_filters_from_query()` de `query_parser.py`
- `filter_source()` e `is_result_correction()` de `product_intake.py`
- `known_to_natural_text()` para construcción de query de pre-search

**Lo que NO toca:**
- `floor_next_question()`, `hose_next_question()`: siguen siendo deterministas (están basadas en el estado que ahora devuelve el LLM)
- `extract_requested_m2()`, `coverage.py`: cálculos numéricos deterministas que siguen siendo válidos
- La lógica de `is_operational_message()`, `selected_menu_intent()`: pueden simplificarse pero no son urgentes

### CatalogAgent

**Archivo:** `app/agents/catalog_agent.py`

**Responsabilidad:** Recibir el `ProductIntakeResponse` del RequirementsAgent, decidir si buscar, llamar `buscar_productos` con query natural y generar la respuesta final en tono WhatsApp.

**Tool:** `buscar_productos` — llama a `perform_search()` en `main.py` (sin cambios).

**Prompt del agente:** `prompt_agente_odranid.md` existente (sin cambios de estructura).

**Input al agente:**
- Historial de conversación (últimos N mensajes, igual que hoy)
- `ProductIntakeResponse` del RequirementsAgent como contexto estructurado
- Candidatos RAG pre-buscados si `intake.should_search=True` (igual que hoy via `rag_precontext.py`)

**Lo que reemplaza:**
- `OpenAIAgentClient` en `app/agent.py`
- `build_input_items()`, `execute_tool_call()`, `build_system_prompt()`

**Lo que NO toca:**
- `compact_search_response()` en `agent.py`: se migra como utilidad
- `response_from_search_response()`: se migra o reusa
- `format_hit()`, `format_number()`: se migran como utilidades

### OdranidAgnoTeam

**Archivo:** `app/agents/odranid_team.py`

**Responsabilidad:** Coordinar RequirementsAgent → CatalogAgent de forma secuencial.

Flujo:
1. Llamar RequirementsAgent → obtener `ProductIntakeResponse`
2. Si `intake.intent is None`: no es consulta de producto, CatalogAgent responde directo (saludo, institucional)
3. Si `intake.should_search is False` y `intake.next_question`: devolver `next_question` sin llamar CatalogAgent (ahorro de LLM)
4. Si `intake.should_search is True`: llamar CatalogAgent con contexto completo

Este flujo reemplaza `run_openai_agent()` y `current_agent_context()` en `main.py`.

### Lo Que Cambia

| Archivo | Estado |
|---|---|
| `app/agents/__init__.py` | Nuevo |
| `app/agents/requirements_agent.py` | Nuevo |
| `app/agents/catalog_agent.py` | Nuevo |
| `app/agents/odranid_team.py` | Nuevo |
| `app/main.py` | Simplificado: `run_openai_agent` → llama al team |
| `app/product_intake.py` | Reducido: eliminar extracción de keywords, conservar utilidades numéricas y preguntas |
| `app/query_parser.py` | Reducido o eliminado: `infer_filters_from_query` ya no es la fuente de verdad |
| `app/agent.py` | Deprecado: `OpenAIAgentClient` reemplazado por `CatalogAgent` |
| `app/rag_precontext.py` | Simplificado: ya no necesita `suggested_search_query` workarounds |
| `requirements.txt` | Agregar `agno>=1.7` |

### Lo Que NO Cambia

| Archivo | Razón |
|---|---|
| `app/main.py` (endpoints) | FastAPI endpoints intactos |
| `app/chat_memory.py` | Persistencia, deduplicación, jobs: sin cambios |
| `app/db_search.py` | Búsqueda vectorial/facetada: sin cambios |
| `app/coverage.py` | Cálculos de cobertura: sin cambios |
| `app/models.py` | Contratos Pydantic: sin cambios (incluyendo `ProductIntakeResponse`) |
| `app/chatwoot*.py` | Integración Chatwoot: sin cambios |
| `app/normalization.py` | Normalización de productos: sin cambios |
| `app/tasks/` | Celery tasks: sin cambios |
| `postgres/` | Schema de DB: sin cambios |
| `prompt_agente_odranid.md` | Prompt institucional: sin cambios |

### Fases De Migración

#### Fase 1 — RequirementsAgent (prioritaria, desbloquea el bug en producción)

Objetivo: reemplazar el sistema keyword de extracción de estado por un LLM call con structured output.

Tareas:
1. Agregar `agno>=1.7` a `requirements.txt`
2. Crear `app/agents/__init__.py`
3. Crear `app/agents/requirements_agent.py`:
   - Agno `Agent` con `response_model=ProductIntakeResponse`
   - Prompt de dominio completo (ver sección anterior)
   - Función `analyze_requirements(query, history, api_key) -> ProductIntakeResponse`
4. Actualizar `app/main.py`:
   - En `current_agent_context()`: llamar `analyze_requirements()` en lugar de `analyze_product_intake()`
   - En `run_deterministic_product_fallback()`: idem
   - Simplificar `search_query_from_agent_request()`: ya no necesita `is_result_correction` ni `filter_source`
5. Actualizar tests: `tests/test_product_intake.py` → mockear el LLM call o usar fixtures de respuesta
6. Deprecar `filter_source()`, `is_result_correction()` de `product_intake.py`

Criterio de éxito: mismos tests de negocio pasando (160), sin árboles de keywords para extracción de estado.

#### Fase 2 — CatalogAgent

Objetivo: reemplazar `OpenAIAgentClient` por un Agno Agent con tool use nativo.

Tareas:
1. Crear `app/agents/catalog_agent.py`:
   - Agno `Agent` con `tools=[buscar_productos_function]`
   - `buscar_productos_function` llama a `perform_search()` via inyección
   - Reutilizar `compact_search_response()`, `format_hit()` de `agent.py`
2. Actualizar `app/main.py`:
   - `run_openai_agent()` → llama a `CatalogAgent`
3. Deprecar `app/agent.py` (`OpenAIAgentClient`, `build_input_items`, `execute_tool_call`)

Criterio de éxito: respuestas equivalentes, tool calls correctos, tests de integración pasando.

#### Fase 3 — OdranidAgnoTeam + Cleanup

Objetivo: unificar el flujo en un Team y limpiar el código legacy.

Tareas:
1. Crear `app/agents/odranid_team.py`:
   - Flujo secuencial RequirementsAgent → CatalogAgent
   - Manejo de `intent=None` (saludo/institucional) sin gastar Fase 2
   - Manejo de `should_search=False` devolviendo `next_question` directo
2. Simplificar `app/main.py`:
   - `run_agent()` → llama al Team, elimina toda la lógica de routing manual
3. Eliminar o reducir drásticamente `app/product_intake.py` (conservar solo utilidades numéricas)
4. Eliminar o reducir `app/query_parser.py` (conservar solo lo que usen tests externos)
5. Eliminar `app/agent.py`
6. Actualizar `app/rag_precontext.py`: simplificar contexto

Criterio de éxito: codebase limpio, sin dead code, tests verdes, replay de conversaciones reales sin regresiones.

### Criterio Para Nuevas Decisiones (actualizado)

Antes de escribir código determinista para manejar un caso de lenguaje natural:

1. ¿Es una regla de cálculo numérico? → determinista, con tests.
2. ¿Es una regla de negocio rígida (precio no se muestra, derivar si frustrado)? → prompt del CatalogAgent.
3. ¿Es interpretación de lenguaje, intención o estado? → RequirementsAgent (LLM), no keywords.

**No agregar más keywords a `infer_filters_from_query` ni a `product_intake.py`.** Cualquier caso nuevo de lenguaje natural se maneja mejorando el prompt del RequirementsAgent.

---

## Estado Actual

Catalogo reportado por el usuario:

- 533 productos subidos.
- Rubros detectados:
  - mangueras: 355
  - pisos: 80
  - mascotas: 42
  - hogar: 28
  - general: 8
  - calzado: 6
- Facets de pisos:
  - anchos: 1, 1.2, 1.4, 1.5, 2
  - espesores: 1.2, 2, 2.5, 3
  - disenos: moneda, rayado, semilla, simil_madera, semilla_melon

Validacion con conversaciones reales:

- `chatwoot_conversaciones.json` contiene 727 conversaciones exportadas.
- Estas conversaciones vienen del flujo anterior de n8n. No son fuente de verdad sobre como debe responder el nuevo agente.
- Usarlas como corpus de comportamiento real del cliente: como preguntan, que datos dan por partes, donde se frustran, que palabras usan, que consultas operativas aparecen y que productos piden.
- Las respuestas `assistant` del flujo n8n pueden usarse solo como contexto para entender a que estaba respondiendo el cliente, por ejemplo cuando dice `2m`, `si`, `3mm` o `quiero ese`.
- No copiar estilo, estructura ni decisiones comerciales del n8n si chocan con el nuevo microservicio. La calidad objetivo la definen las reglas de dominio, el catalogo actual, coverage, facets, memoria y el prompt nuevo.
- `reports/chatwoot_replay_727_intake_final.jsonl` cubre 4.760 turnos de usuario.
- Resultado intake-only final: 0 errores, 414 turnos listos para busqueda, 17 turnos en modo recomendacion.
- `reports/chatwoot_shadow_dance_after_coverage.jsonl` valida que danza/escenario recomiende 3 mm y use metros lineales cuando corresponde.
- `reports/chatwoot_shadow_targeted_after_policy.jsonl` valida tejos/regatones/proveedores externos sin enviar mensajes a Chatwoot.
- `reports/chatwoot_shadow_50_100_after_forced_search.jsonl` valida el lote 50-100 con busqueda forzada cuando `should_search=true`.
- `reports/chatwoot_shadow_pet_size_after_breed.jsonl` valida mascotas: pitbull/grande no queda contaminado por producto anterior de perros chicos.
- Suite actual: `78 tests OK`.

## Proximos Pasos Recomendados

Prioridad 1 — Migración Agno Fase 1: RequirementsAgent.

Ver sección "Roadmap: Migración a Agno Multi-Agente" para detalle completo.

- Agregar `agno>=1.7` a `requirements.txt`.
- Crear `app/agents/requirements_agent.py` con `Agent(response_model=ProductIntakeResponse)`.
- Conectar en `main.py` reemplazando `analyze_product_intake`.
- Actualizar tests para el nuevo contrato (mockear LLM o usar respuestas fijas).
- NO agregar más keywords a `product_intake.py` ni `query_parser.py` mientras dure la migración.

Prioridad 2 — Migración Agno Fase 2: CatalogAgent.

- Crear `app/agents/catalog_agent.py` con Agno Agent + tool `buscar_productos`.
- Reemplazar `OpenAIAgentClient` en `main.py`.
- Deprecar `app/agent.py`.

Prioridad 3 — Migración Agno Fase 3: Team + Cleanup.

- Crear `app/agents/odranid_team.py`.
- Eliminar dead code: `product_intake.py` keyword extraction, `query_parser.py`, `agent.py`.
- Simplificar `main.py` y `rag_precontext.py`.

Prioridad 4 — Estabilizar despliegue.

- Crear unidad systemd o docker compose para FastAPI.
- Configurar reverse proxy privado/publico segun canal.
- Definir logs y healthcheck.
- Definir job periodico de sync WooCommerce.

Prioridad 5 — Mejoras de recuperacion y datos.

- Agregar reranking post-vector por score de facets.
- Registrar queries sin resultados.
- Agregar `last_synced_at`, hashes y sync incremental real.
- Manejar productos borrados/despublicados en WooCommerce.

## Criterio Para Futuras Decisiones

Antes de resolver algo en prompt, preguntar:

1. Es una regla conversacional o institucional?
2. Es una regla de busqueda, datos, calculo o precision?

Si es conversacional/institucional, puede ir al prompt.
Si es busqueda/datos/calculo/precision, debe ir al microservicio con tests.

Ese criterio mantiene el sistema escalable.
