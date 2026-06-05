#!/usr/bin/env bash
# Backup de Postgres (system of record de Odranid) con rotación.
#
# Uso:
#   ODRANID_DATABASE_URL=postgresql://user:pass@host:5432/db \
#   BACKUP_DIR=/var/backups/odranid RETENTION_DAYS=14 \
#   scripts/backup_postgres.sh
#
# Programar por cron en el VPS (ej. diario 3am):
#   0 3 * * * ODRANID_DATABASE_URL=... BACKUP_DIR=/var/backups/odranid \
#     /ruta/al/repo/scripts/backup_postgres.sh >> /var/log/odranid-backup.log 2>&1
#
# Restore:
#   gunzip -c /var/backups/odranid/odranid-YYYYmmdd-HHMMSS.sql.gz | psql "$ODRANID_DATABASE_URL"
set -euo pipefail

DB_URL="${ODRANID_DATABASE_URL:-${DATABASE_URL:-}}"
if [[ -z "${DB_URL}" ]]; then
  echo "ERROR: definí ODRANID_DATABASE_URL (o DATABASE_URL) con la conexión a Postgres." >&2
  exit 1
fi

BACKUP_DIR="${BACKUP_DIR:-/var/backups/odranid}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"

mkdir -p "${BACKUP_DIR}"
timestamp="$(date +%Y%m%d-%H%M%S)"
outfile="${BACKUP_DIR}/odranid-${timestamp}.sql.gz"

echo "[$(date -Is)] dump -> ${outfile}"
# --no-owner / --no-privileges para que el restore sea portable entre instancias.
pg_dump --no-owner --no-privileges "${DB_URL}" | gzip > "${outfile}"

# Verificación mínima: el archivo no debe quedar vacío.
if [[ ! -s "${outfile}" ]]; then
  echo "ERROR: el backup quedó vacío, lo elimino." >&2
  rm -f "${outfile}"
  exit 1
fi

echo "[$(date -Is)] ok ($(du -h "${outfile}" | cut -f1))"

# Rotación: borrar backups más viejos que RETENTION_DAYS.
deleted="$(find "${BACKUP_DIR}" -name 'odranid-*.sql.gz' -type f -mtime "+${RETENTION_DAYS}" -print -delete | wc -l)"
echo "[$(date -Is)] rotación: ${deleted} backup(s) vencidos eliminados (retención ${RETENTION_DAYS}d)"
