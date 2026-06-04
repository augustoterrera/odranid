# Supabase Principal Para Odranid

Supabase es el backend principal de produccion para catalogo, facets y busqueda vectorial.

Postgres directo queda como fallback/local/dev.

## 1. Variables

En `.env`:

```env
OPENAI_API_KEY=...
ODRANID_SUPABASE_URL=https://TU_PROYECTO.supabase.co
ODRANID_SUPABASE_SERVICE_ROLE_KEY=TU_SERVICE_ROLE_KEY
ODRANID_AGENT_MODEL=gpt-4.1-mini
```

Opcional fallback Postgres:

```env
ODRANID_DATABASE_URL=postgresql://...
```

Prioridad:

1. Supabase si estan `ODRANID_SUPABASE_URL` y `ODRANID_SUPABASE_SERVICE_ROLE_KEY`.
2. Postgres directo si no hay Supabase.
3. Fixture local solo para desarrollo.

## 2. Aplicar Migraciones

En Supabase SQL Editor, aplicar en orden:

```txt
supabase/migrations/001_catalog_products.sql
supabase/migrations/002_upsert_catalog_products_rpc.sql
```

Ambas migraciones incluyen:

```sql
notify pgrst, 'reload schema';
```

Eso fuerza a PostgREST a recargar el schema y evita el error donde una funcion existe en SQL pero Supabase REST/RPC todavia no la ve.

Si aun aparece cache viejo, correr manualmente:

```sql
notify pgrst, 'reload schema';
```

## 3. Sincronizar Catalogo

Usando WooCommerce directo:

```bash
.venv/bin/python scripts/sync_catalog.py
```

Sin gastar embeddings:

```bash
.venv/bin/python scripts/sync_catalog.py --no-embeddings
```

Forzar Supabase:

```bash
.venv/bin/python scripts/sync_catalog.py --store supabase
```

Usar RPC de upsert en vez de REST:

```bash
.venv/bin/python scripts/sync_catalog.py --store supabase --write-mode rpc
```

Usar snapshot local solo para desarrollo:

```bash
.venv/bin/python scripts/sync_catalog.py --from-file productos.json --no-embeddings
```

## 4. Verificar

Health:

```bash
curl -s http://127.0.0.1:8000/health | jq
```

Contexto cacheado:

```bash
curl -s http://127.0.0.1:8000/catalog/context | jq
```

Busqueda:

```bash
curl -s -X POST http://127.0.0.1:8000/search \
  -H 'content-type: application/json' \
  -d '{"query":"piso moneda 3mm ancho 1.20 metros para cubrir 20m2","limit":5}' \
  | jq
```

Agente:

```bash
curl -s -X POST http://127.0.0.1:8000/agent/respond \
  -H 'content-type: application/json' \
  -d '{"message":"piso moneda 3mm ancho 1.20 metros para cubrir 20m2","limit":5}' \
  | jq
```

## 5. Errores Tipicos

### `PGRST202 function not found in schema cache`

La funcion existe en Postgres pero PostgREST no actualizo schema.

Solucion:

```sql
notify pgrst, 'reload schema';
```

### `type "extensions.vector" does not exist`

No usar `extensions.vector(1536)` en columnas. Usar:

```sql
embedding vector(1536)
```

Y asegurarse de aplicar:

```sql
create extension if not exists vector;
```

### REST upsert falla pero RPC funciona

Usar:

```bash
.venv/bin/python scripts/sync_catalog.py --store supabase --write-mode rpc
```

## 6. Regla De Arquitectura

El agente no consulta Supabase directo.

Flujo:

```txt
Agente -> Microservicio -> Supabase RPC/REST -> Microservicio -> Agente
```

El microservicio gobierna:

- intake;
- facets;
- busqueda vectorial;
- relajacion de filtros;
- coverage/calculos;
- sanitizacion de datos para el agente.
