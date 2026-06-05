# Infra de producción (Parte B)

Cómo dejar Odranid seguro y escalable en el VPS. Implementado en `docker-compose.yml` y
`scripts/backup_postgres.sh`. Las variables van en `.env` (ver `.env.example`, bloque
"Infra / despliegue").

> Este VPS usa **Docker + Traefik + Tailscale**. El reverse proxy/TLS lo provee el Traefik ya
> existente; el acceso a herramientas internas es por la **tailnet** (no se publican).

## Modelo de puertos / exposición (B1)
- **Público (vía Traefik)**: SOLO el webhook de Chatwoot (`/webhooks/...`). Es lo único que un
  servicio externo (Chatwoot) necesita alcanzar.
- **Loopback `127.0.0.1` del host**: Postgres 5432, Typesense 8108, Dragonfly 6379, Flower 5555 y
  la API 8000. No accesibles desde internet. Desde el VPS y por la **tailnet** sí.
- **Acceso a internas por Tailscale**: para usar Flower o pegarle a `/agent`, `/search`, `/admin/*`
  sin exponerlos, usar Tailscale. Lo más simple, `tailscale serve`:
  ```bash
  tailscale serve --bg 8000   # API por https://<nodo>.<tailnet>.ts.net
  tailscale serve --bg 5555   # Flower
  ```
  (o un túnel SSH a `127.0.0.1:<puerto>`). Flower igual lleva basic auth (`FLOWER_BASIC_AUTH`).

## Reverse proxy + HTTPS con Traefik (B2)
La API se integra al Traefik existente por **labels** (servicio `api` en el compose), conectada a
la red externa `proxy`. Traefik termina TLS (Let's Encrypt) y rutea por el entrypoint `websecure`
con el certresolver `letsencrypt`.

Setup del VPS (una vez):
```bash
# La red 'proxy' ya existe (la usa Traefik). Si no:
docker network create proxy

# en .env
ODRANID_PUBLIC_DOMAIN=odranid.tu-dominio.com   # DNS -> IP pública del VPS

docker compose up -d        # api se registra solo en Traefik por sus labels
```

Por defecto el router matchea `Host(...) && PathPrefix(/webhooks)`: **solo el webhook** sale a
internet (incluye `/webhooks/chatwoot/health`). Para publicar todo el dominio, cambiar la rule del
label a `Host(${ODRANID_PUBLIC_DOMAIN})` (ver comentario en el compose). URL del webhook para
Chatwoot: `https://<dominio>/webhooks/chatwoot`.

Rate-limiting por IP: configurarlo en Traefik (middleware `rateLimit`) o a nivel firewall del VPS.

## Escalado (B3)
- **API**: corre con `--workers ${API_WORKERS:-2}`. Subir `API_WORKERS` según CPU/RAM.
- **worker_messages** (responde a los clientes): para picos, escalar horizontalmente:
  ```bash
  docker compose up -d --scale worker_messages=3
  ```
- **worker_catalog**: cola `catalog` dedicada (concurrency 1), aislada de `chatwoot_messages`
  para que un sync pesado del catálogo no demore las respuestas. Mantener esa separación.

## Límites de recursos (B4)
Cada servicio tiene un tope de memoria (`deploy.resources.limits.memory`) configurable por env
(`POSTGRES_MEM_LIMIT`, `TYPESENSE_MEM_LIMIT`, `API_MEM_LIMIT`, `WORKER_MEM_LIMIT`, etc.). Son
**topes** (solo frenan un proceso desbocado), no reservas: podés sobre-suscribir sin problema.
Ajustar a la RAM real del VPS.

## Backups de Postgres (B5)
`scripts/backup_postgres.sh` hace `pg_dump` comprimido con rotación. Programar por cron:

```cron
0 3 * * * ODRANID_DATABASE_URL=postgresql://user:pass@127.0.0.1:5432/odranid \
  BACKUP_DIR=/var/backups/odranid RETENTION_DAYS=14 \
  /ruta/al/repo/scripts/backup_postgres.sh >> /var/log/odranid-backup.log 2>&1
```

Restore:
```bash
gunzip -c /var/backups/odranid/odranid-YYYYmmdd-HHMMSS.sql.gz | psql "$ODRANID_DATABASE_URL"
```
Probar el restore en una base de prueba al menos una vez (un backup sin restore probado no es
un backup).

## Checklist
- [ ] Red `proxy` existe (`docker network create proxy` si no) y Traefik la usa.
- [ ] `.env`: `ODRANID_PUBLIC_DOMAIN`, `FLOWER_BASIC_AUTH` (cambiado), `ODRANID_ADMIN_API_TOKEN`,
      `ODRANID_REQUIRE_WEBHOOK_SECRET=true` + `ODRANID_CHATWOOT_WEBHOOK_SECRET`.
- [ ] `docker compose up -d` y verificar `https://<dominio>/webhooks/chatwoot/health` (público) y
      el certificado emitido por Traefik.
- [ ] Confirmar que `/agent`, `/admin/*` y la raíz NO responden públicamente (404 de Traefik) y sí
      por la tailnet.
- [ ] Confirmar que 5432/8108/6379/5555/8000 NO responden desde una IP externa.
- [ ] Cron de backup andando y un restore probado.
- [ ] `API_WORKERS` y límites de memoria ajustados a la RAM del VPS.
