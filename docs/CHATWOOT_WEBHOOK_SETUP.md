# Chatwoot Webhook

Endpoint para conectar Chatwoot con el agente Odranid:

```text
POST /webhooks/chatwoot
```

Health/config sin secretos:

```bash
curl -s http://127.0.0.1:8000/webhooks/chatwoot/health
```

## Variables

```env
ODRANID_CHATWOOT_BASE_URL=https://chatwoot.tu-dominio.com
ODRANID_CHATWOOT_ACCOUNT_ID=1
ODRANID_CHATWOOT_API_ACCESS_TOKEN=TU_API_ACCESS_TOKEN
ODRANID_CHATWOOT_WEBHOOK_SECRET=TU_WEBHOOK_SECRET
ODRANID_CHATWOOT_AUTO_REPLY=true
ODRANID_CHATWOOT_AGENT_LIMIT=5
ODRANID_CHATWOOT_HISTORY_LIMIT=8
ODRANID_CHAT_MEMORY_ENABLED=true
ODRANID_CHATWOOT_LOCK_SECONDS=60
ODRANID_CHATWOOT_LOCK_WAIT_SECONDS=20
```

`ODRANID_CHATWOOT_WEBHOOK_SECRET` es opcional, pero recomendado. Si está configurado, el microservicio valida `X-Chatwoot-Signature` y `X-Chatwoot-Timestamp`.

## Configuración En Chatwoot

1. Ir a `Settings -> Integrations -> Webhooks`.
2. Crear webhook con URL pública:

```text
https://TU-DOMINIO/webhooks/chatwoot
```

3. Seleccionar el evento:

```text
message_created
```

4. Copiar el webhook secret generado por Chatwoot a:

```env
ODRANID_CHATWOOT_WEBHOOK_SECRET=...
```

## Comportamiento

- Procesa solamente `message_created` con `message_type=incoming`.
- Ignora mensajes `outgoing`, privados, vacíos o no-texto para evitar loops.
- Guarda conversaciones, mensajes, eventos y jobs en Postgres si está configurada `ODRANID_DATABASE_URL`.
- Usa memoria persistente por `conversation_id`; no depende solo del historial incluido en el webhook.
- Ejecuta el agente igual que `POST /agent/respond`.
- Si `ODRANID_CHATWOOT_AUTO_REPLY=true`, publica la respuesta en Chatwoot como mensaje `outgoing`.
- Deduplica eventos de forma persistente por `X-Chatwoot-Delivery`; si no existe, usa `conversation_id + message_id`.
- En producción, responde rápido `queued` al webhook y procesa el agente en background.
- Usa lock por conversación para evitar procesar dos mensajes del mismo chat al mismo tiempo.

## Migración Postgres

Aplicar con el servicio `migrate` de Docker Compose o ejecutar:

```text
postgres/migrations/003_chat_memory.sql
```

Esta migración crea:

- `chat_conversations`
- `chat_messages`
- `chat_processed_events`
- `chat_webhook_jobs`
- RPCs de deduplicación, jobs y locks por conversación.

Si necesitás levantar el webhook antes de aplicar esta migración, usar temporalmente:

```env
ODRANID_CHAT_MEMORY_ENABLED=false
```

Para producción debe quedar:

```env
ODRANID_CHAT_MEMORY_ENABLED=true
```

## Prueba Local Sin Firma

Si no configuraste `ODRANID_CHATWOOT_WEBHOOK_SECRET`, podés probar así:

```bash
curl -s -X POST http://127.0.0.1:8000/webhooks/chatwoot \
  -H 'content-type: application/json' \
  -d '{
    "event": "message_created",
    "id": 123,
    "content": "tenes piso moneda 3mm de 1.20 de ancho para cubrir 20m2 gimnasio",
    "message_type": "incoming",
    "content_type": "text",
    "account": {"id": 1},
    "conversation": {"id": 456, "messages": []}
  }'
```

Para probar sin enviar respuesta real a Chatwoot:

```env
ODRANID_CHATWOOT_AUTO_REPLY=false
```

Con memoria persistente habilitada, la respuesta del webhook se verá así:

```json
{
  "ok": true,
  "handled": true,
  "status": "queued",
  "reason": "queued_for_background_processing"
}
```

La respuesta real al cliente se envía después por la API de Chatwoot.

## Replay Offline De Conversaciones Reales

Importante:

- Si el archivo viene del flujo viejo de n8n, no usar las respuestas anteriores como resultado esperado.
- El objetivo del replay es estudiar comportamiento real del cliente: consultas, datos incompletos, respuestas cortas, pedidos operativos y frustraciones.
- Los mensajes `assistant` del export sirven solo como contexto para interpretar respuestas del usuario; no son criterio de calidad para el nuevo microservicio.
- El nuevo agente debe evaluarse contra reglas propias: intake, memoria, facets, busqueda, calculos de cobertura, derivaciones y prompt actual.

Para probar con conversaciones reales sin enviar mensajes a clientes:

```bash
.venv/bin/python scripts/replay_chatwoot_conversations.py \
  --conversation-id 167 \
  --intake-only
```

Esto descarga mensajes de Chatwoot y prueba memoria/intake sin llamar OpenAI.

Para probar el agente completo en shadow mode, sin enviar a Chatwoot:

```bash
.venv/bin/python scripts/replay_chatwoot_conversations.py \
  --conversation-id 167
```

Para tomar conversaciones recientes:

```bash
.venv/bin/python scripts/replay_chatwoot_conversations.py \
  --recent 10 \
  --mode last-incoming \
  --intake-only
```

Si ya exportaste el listado de conversaciones desde Chatwoot, usalo como indice de IDs y el script va a descargar el hilo completo de cada conversacion:

```bash
.venv/bin/python scripts/replay_chatwoot_conversations.py \
  --from-file chatwoot_conversaciones.json \
  --mode all-incoming \
  --intake-only
```

El endpoint `/conversations` de Chatwoot suele traer solo el ultimo mensaje de cada conversacion. Para analizar memoria real, no uses `--use-file-messages` salvo que el archivo ya incluya el historial completo. Sin esa opcion, el replay llama a `/conversations/{id}/messages` por cada conversacion y mantiene cada hilo separado.

Si n8n ya descarga los mensajes completos de cada conversacion, guardalos en este formato:

```json
[
  {
    "conversation_id": 118,
    "account_id": 4,
    "status": "open",
    "contact": {
      "name": "Cliente",
      "phone_number": "+549..."
    },
    "messages": [
      {
        "id": 7981,
        "role": "user",
        "content": "Hola, queria consultar...",
        "created_at": 1769449863
      },
      {
        "id": 7982,
        "role": "assistant",
        "content": "Hola, te ayudo...",
        "created_at": 1769449870
      }
    ]
  }
]
```

Y probalo sin tocar Chatwoot:

```bash
.venv/bin/python scripts/replay_chatwoot_conversations.py \
  --from-file chatwoot_conversaciones_completas.json \
  --use-file-messages \
  --mode all-incoming \
  --intake-only
```

Salida:

```text
reports/chatwoot_replay.jsonl
```

Cada línea incluye:

- conversación;
- mensaje de usuario;
- historial usado;
- estado antes/después;
- intake;
- respuesta que habría dado el agente;
- tool calls;
- error si ocurrió.

El replay nunca llama a `create_outgoing_message`, por lo que no responde en Chatwoot.

## Analizar Reportes Shadow

Despues de generar un shadow report, usar:

```bash
.venv/bin/python scripts/analyze_shadow_report.py \
  reports/chatwoot_shadow_50_100_after_forced_search.jsonl \
  --limit 40
```

El analizador ayuda a priorizar:

- errores de runtime;
- casos listos para buscar donde no hubo tool call;
- mensajes operativos que dispararon busqueda;
- preguntas repetidas sobre datos ya conocidos;
- respuestas genericas con contexto;
- links de producto sin tool call;
- posibles problemas de unidades/cobertura.

Las conversaciones exportadas desde n8n siguen siendo corpus de comportamiento del cliente, no respuestas esperadas.
