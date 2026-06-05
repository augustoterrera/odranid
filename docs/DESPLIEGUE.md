# Despliegue de Odranid (de cero)

Guía paso a paso para levantar Odranid en el VPS. El stack del VPS es **Docker + Traefik
+ Tailscale**: Traefik provee reverse proxy y TLS; **solo el webhook de Chatwoot queda
público**, el resto se accede por la tailnet. El detalle de la infra (puertos, límites,
backups) está en [INFRA_PRODUCCION.md](INFRA_PRODUCCION.md).

---

## 0. Prerrequisitos

- **DNS**: registro **A** `odranid.tudominio.com` → IP pública del VPS. Es lo que hace
  alcanzable el webhook y permite a Traefik emitir el certificado (HTTP challenge).
- **Traefik** corriendo con: red externa `proxy`, entrypoint `websecure`, certresolver
  `letsencrypt` (ya configurado en el VPS).
- **Postgres con pgvector** accesible (en este setup, por la tailnet — la
  `ODRANID_DATABASE_URL` apunta a una IP `100.x`).
- Credenciales: OpenAI, WooCommerce (la tienda), Chatwoot (base URL, account id, token,
  webhook secret).

---

## 1. Código

```bash
cd /ruta/donde/va/odranid
git clone https://github.com/augustoterrera/odranid.git .
# actualizaciones futuras: git pull origin main
```

## 2. Configurar `.env`

```bash
cp .env.example .env
```

Completar lo **obligatorio** en producción (el resto tiene defaults; ver comentarios del
`.env.example`):

```dotenv
# OpenAI
OPENAI_API_KEY=sk-...

# Postgres (system of record)
ODRANID_DATABASE_URL=postgresql://user:pass@100.x.y.z:5432/odranid_catalog

# Dominio público = URL del webhook
ODRANID_PUBLIC_DOMAIN=odranid.tudominio.com

# Typesense
ODRANID_TYPESENSE_API_KEY=<clave-fuerte>

# Chatwoot
ODRANID_CHATWOOT_BASE_URL=https://chatwoot.tu-dominio.com
ODRANID_CHATWOOT_ACCOUNT_ID=1
ODRANID_CHATWOOT_API_ACCESS_TOKEN=<token>
ODRANID_CHATWOOT_WEBHOOK_SECRET=<secret-fuerte>
ODRANID_REQUIRE_WEBHOOK_SECRET=true     # no arranca si falta el secret

# Seguridad / observabilidad
ODRANID_ADMIN_API_TOKEN=<token-admin-fuerte>
FLOWER_BASIC_AUTH=admin:<password>
```

## 3. Red de Traefik

```bash
docker network inspect proxy >/dev/null 2>&1 || docker network create proxy
```

## 4. Migraciones de la base

Contra el Postgres externo (recomendado en este setup):

```bash
for f in postgres/migrations/*.sql; do
  psql "$ODRANID_DATABASE_URL" -v ON_ERROR_STOP=1 -f "$f"
done
```

Alternativa todo-en-uno con el Postgres del compose:

```bash
docker compose --profile local-db up -d postgres
docker compose --profile local-db run --rm migrate
```

## 5. Build + primer sync del catálogo

```bash
docker compose build

# 5a. WooCommerce -> Postgres (normaliza + genera embeddings; tarda unos minutos)
docker compose run --rm api python scripts/sync_catalog.py

# 5b. Postgres -> Typesense (build inicial completo del índice)
docker compose run --rm api python -c \
  "from app.typesense_sync import run_typesense_sync; print(run_typesense_sync(recreate=True))"
```

A partir de acá, `beat` mantiene ambos syncs solos: WooCommerce→Postgres cada
`ODRANID_CATALOG_SYNC_MINUTES` (60) y Typesense cada `ODRANID_TYPESENSE_SYNC_MINUTES` (30).

## 6. Levantar el stack

```bash
docker compose up -d
```

Servicios: `api`, `worker_messages`, `worker_outbound`, `worker_catalog`, `beat`
(+ `flower` con perfil `observability`). La API se registra sola en Traefik por sus labels.

## 7. Verificar que el webhook está expuesto

```bash
# Público, con TLS válido emitido por Traefik:
curl https://odranid.tudominio.com/webhooks/chatwoot/health

# Lo privado NO debe salir a internet (404 de Traefik):
curl -i https://odranid.tudominio.com/agent/respond
```

`/webhooks/...` responde; `/agent`, `/search`, `/admin/*` y la raíz dan 404 público.

## 8. Configurar el webhook en Chatwoot

En Chatwoot → **Settings → Integrations → Webhooks** (o el inbox/bot):

- **URL**: `https://odranid.tudominio.com/webhooks/chatwoot`
- **Secret / HMAC**: el mismo `ODRANID_CHATWOOT_WEBHOOK_SECRET` del `.env`.
- **Eventos**: `message_created`.

Probar con un mensaje real y mirar los logs:

```bash
docker compose logs -f worker_messages
```

---

## Acceso a herramientas internas (por Tailscale)

No se exponen a internet; se llegan por la tailnet:

```bash
tailscale serve --bg 8000     # API (para /admin/*, pruebas)
tailscale serve --bg 5555     # Flower (panel de Celery)
```

`/admin/*` requiere el header `X-Admin-Token: <ODRANID_ADMIN_API_TOKEN>`. Ejemplos:

```bash
# Forzar un re-sync del catálogo (encola la task; devuelve task_id):
curl -X POST -H "X-Admin-Token: $TOKEN" http://127.0.0.1:8000/admin/sync-catalog
# Rebuild completo de Typesense:
curl -X POST -H "X-Admin-Token: $TOKEN" http://127.0.0.1:8000/admin/typesense-sync
```

## Operación

- **Logs**: `docker compose logs -f api worker_messages`.
- **Escalar respuestas** (picos de tráfico): `docker compose up -d --scale worker_messages=3`.
- **Más workers de API**: subir `API_WORKERS` en `.env` y `docker compose up -d api`.
- **Backups de Postgres**: ver [INFRA_PRODUCCION.md](INFRA_PRODUCCION.md) (`scripts/backup_postgres.sh` + cron).
- **Actualizar versión**: `git pull origin main && docker compose build && docker compose up -d`.

## Checklist post-deploy

- [ ] `https://<dominio>/webhooks/chatwoot/health` responde con cert válido.
- [ ] `/agent` y `/admin/*` NO responden públicamente (404), sí por la tailnet.
- [ ] Webhook configurado en Chatwoot con el secret correcto; un mensaje de prueba se responde.
- [ ] `docker compose ps` con todos los servicios `up`/healthy.
- [ ] Cron de backup de Postgres andando y un restore probado.
