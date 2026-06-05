# Infra de producción (Parte B)

Cómo dejar Odranid seguro y escalable en el VPS. Implementado en `docker-compose.yml`,
`deploy/Caddyfile` y `scripts/backup_postgres.sh`. Las variables van en `.env`
(ver `.env.example`, bloque "Infra / despliegue").

## Modelo de puertos (B1)
Solo el reverse proxy (80/443) queda expuesto a internet. Todo lo demás está bindeado a
`127.0.0.1` del host (loopback): Postgres 5432, Typesense 8108, Dragonfly 6379, Flower 5555 y
la API 8000. Desde el VPS se alcanzan; desde internet, no. En dev local `localhost:<puerto>`
sigue funcionando igual.

- **Flower** además lleva basic auth: setear `FLOWER_BASIC_AUTH=usuario:password` (NO dejar el
  default). Está bajo el perfil `observability`.

## Reverse proxy + HTTPS (B2)
Servicio `caddy` bajo el perfil `proxy` (opt-in). Caddy termina TLS con Let's Encrypt
automáticamente y proxea a `api:8000` por la red interna.

Requisitos: un dominio apuntando al VPS y los puertos 80/443 libres.

```bash
# en .env
ODRANID_PUBLIC_DOMAIN=odranid.tu-dominio.com

docker compose --profile proxy up -d caddy
```

Rate-limiting por IP: requiere construir Caddy con el plugin `caddy-ratelimit` (xcaddy). Está
documentado y comentado en `deploy/Caddyfile`. Como alternativa inmediata, limitar a nivel
firewall del VPS (nftables/ufw).

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
- [ ] `.env`: `ODRANID_PUBLIC_DOMAIN`, `FLOWER_BASIC_AUTH` (cambiado), `ODRANID_ADMIN_API_TOKEN`,
      `ODRANID_REQUIRE_WEBHOOK_SECRET=true` + `ODRANID_CHATWOOT_WEBHOOK_SECRET`.
- [ ] `docker compose --profile proxy up -d` y verificar `https://<dominio>/health`.
- [ ] Confirmar que 5432/8108/6379/5555/8000 NO responden desde una IP externa.
- [ ] Cron de backup andando y un restore probado.
- [ ] `API_WORKERS` y límites de memoria ajustados a la RAM del VPS.
