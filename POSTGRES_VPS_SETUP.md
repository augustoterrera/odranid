# Postgres + pgvector En VPS

## 1. Instalar pgvector

En Ubuntu/Debian, el paquete depende de la version de Postgres. Ejemplo para Postgres 16:

```bash
sudo apt update
sudo apt install postgresql-16-pgvector
```

Si tu version es otra:

```bash
psql --version
apt search pgvector
```

## 2. Crear DB Y Usuario

En la VPS:

```bash
sudo -u postgres psql
```

```sql
create database odranid_catalog;
create user odranid with encrypted password 'CAMBIAR_PASSWORD';
grant all privileges on database odranid_catalog to odranid;
```

Salir y entrar a la DB:

```bash
sudo -u postgres psql -d odranid_catalog
```

```sql
grant all on schema public to odranid;
```

## 3. Aplicar Schema

Desde tu maquina local, usando la IP de Tailscale de la VPS:

```bash
psql "postgresql://odranid:CAMBIAR_PASSWORD@100.x.y.z:5432/odranid_catalog" -f postgres/schema.sql
```

O desde la VPS:

```bash
psql -d odranid_catalog -f /ruta/al/proyecto/postgres/schema.sql
```

## 4. Configurar .env

Agregar:

```env
ODRANID_DATABASE_URL=postgresql://odranid:CAMBIAR_PASSWORD@100.x.y.z:5432/odranid_catalog
```

Postgres directo queda como fallback/local/dev. Si tambien tenes variables Supabase en `.env`, el sync usa Supabase por defecto. Para forzar Postgres:

```bash
.venv/bin/python scripts/sync_catalog.py --store postgres
```

## 5. Sincronizar Catalogo

Sin gastar OpenAI:

```bash
.venv/bin/python scripts/sync_catalog.py --no-embeddings
```

Con embeddings:

```bash
.venv/bin/python scripts/sync_catalog.py
```

## 6. Verificar

```bash
psql "$ODRANID_DATABASE_URL" -c "select count(*) from catalog_products;"
psql "$ODRANID_DATABASE_URL" -c "select catalog_facets('pisos', true);"
```
