# Microservicio Odranid

## Objetivo

Reemplazar la logica fragil de n8n por un servicio Python estable que:

- ingiera productos desde WooCommerce;
- normalice datos ruidosos en una taxonomia propia;
- genere documentos para busqueda vectorial;
- permita buscar con facetas profesionales;
- entregue a la IA un contexto de catalogo cacheado y compacto;
- funcione de manera continua con endpoints claros.

## Problema Actual

La funcion SQL actual filtra con igualdad exacta sobre metadata:

- `rubro`
- `categoria_principal`
- `tipo_piso_categoria`
- `tipo_piso_diseno`
- `espesor_mm`
- `ancho_m`
- `tags`
- `en_stock`

Eso rompe busquedas cuando la IA interpreta mal una medida. Ejemplo: confundir `2 m2` de cobertura con `ancho_m = 2`. En ese caso, el filtro exacto elimina todos los candidatos antes de que el embedding pueda ayudar.

## Arquitectura Propuesta

### 1. Ingesta

Fuente:

- WooCommerce Store API (`productos.json` muestra el formato real).
- Opcional: planilla tecnica `sheet.json` para complementar pesos, rollos y diametros.

Responsabilidad:

- traer paginas de productos;
- detectar cambios por `id`, `slug`, fecha o hash del contenido;
- normalizar;
- guardar en base relacional;
- generar embeddings solo cuando cambie el texto indexable.

### 2. Normalizacion

Modulo creado:

- `app/normalization.py`

Convierte un producto WooCommerce en `ProductDocument` con:

- identidad: `id`, `title`, `slug`, `link`, `image`;
- comercial: `price`, `currency`, `in_stock`, `stock_text`;
- taxonomia: `rubro`, `category`, `subcategory`, `product_type`;
- facetas: `floor_kind`, `floor_design`, `material`, `color`;
- medidas: `espesor_mm`, `ancho_m`, `largo_m`, `rendimiento_m2`, `diametro_mm`;
- busqueda: `content` y `metadata`.

### 3. Taxonomia Inicial

Rubros:

- `pisos`
- `mangueras`
- `hogar`
- `calzado`
- `mascotas`
- `general`

Facetas criticas para pisos:

- `floor_kind`: `liso`, `diseno`
- `floor_design`: `moneda`, `semilla`, `semilla_melon`, `rayado`, `simil_madera`, `vinilico`
- `espesor_mm`
- `ancho_m`
- `largo_m`
- `rendimiento_m2`
- `material`
- `color`
- `technical_tags`: `alto_transito`, `antideslizante`, `ignifugo`, etc.

Regla importante:

- `m2` pedido por el cliente es cobertura o rendimiento, no ancho.
- La busqueda no debe convertir `20m2` en `espesor=20` ni `ancho=20`.

### 4. Busqueda

Modulo creado:

- `app/retrieval.py`

Flujo recomendado:

1. La IA o un parser extrae una intencion estructurada.
2. Se ejecuta busqueda vectorial amplia: por ejemplo top 50.
3. Se aplican facetas duras: rubro, stock, tipo.
4. Se aplican facetas blandas: ancho, espesor, color, material.
5. Si no hay resultados, el servicio relaja filtros en orden:
   - ancho;
   - espesor;
   - ancho + espesor;
   - color;
   - material;
   - diseno;
   - combinacion de filtros blandos.

Esto evita el caso actual de cero resultados por una sola faceta mal interpretada.

### 5. Base de Datos Principal

Supabase + pgvector.

Postgres directo queda como fallback/local/dev porque comparte el mismo modelo relacional y las mismas funciones SQL base.

Tabla sugerida:

```sql
create table catalog_products (
  id bigint primary key,
  title text not null,
  slug text,
  link text,
  image text,
  price numeric,
  currency text default 'ARS',
  in_stock boolean not null default true,
  stock_text text,

  rubro text not null,
  category text not null,
  subcategory text,
  product_type text,
  floor_kind text,
  floor_design text,
  material text,
  color text,
  espesor_mm numeric,
  ancho_m numeric,
  largo_m numeric,
  rendimiento_m2 numeric,
  diametro_mm numeric,

  content text not null,
  metadata jsonb not null default '{}',
  embedding vector(1536),
  content_hash text not null,
  updated_at timestamptz not null default now()
);
```

Indices:

```sql
create index catalog_products_embedding_idx
  on catalog_products using hnsw (embedding vector_cosine_ops);

create index catalog_products_facets_idx
  on catalog_products (rubro, category, floor_kind, floor_design, in_stock);

create index catalog_products_specs_idx
  on catalog_products (espesor_mm, ancho_m);

create index catalog_products_metadata_idx
  on catalog_products using gin (metadata);
```

### 6. Contexto Cacheado Para IA

Modulo creado:

- `app/catalog_context.py`

Endpoint:

- `GET /catalog/context`

Devuelve un resumen compacto con:

- cantidad de productos;
- rubros disponibles;
- categorias frecuentes;
- espesores/anchos detectados;
- disenos de pisos;
- reglas operativas.

Ese texto va en el system prompt o en una seccion fija cacheada. No reemplaza la busqueda; solo orienta a la IA para que no invente facetas imposibles.

### 7. Endpoints Iniciales

Implementados:

- `GET /health`
- `GET /catalog/context`
- `POST /search`
- `POST /admin/reload`

Ejemplo de busqueda:

```json
{
  "query": "piso con diseno semilla alto transito para cubrir 20m2",
  "filters": {
    "rubro": "pisos",
    "floor_kind": "diseno",
    "floor_design": "semilla",
    "in_stock_only": true
  },
  "limit": 10,
  "relax_filters": true
}
```

### 8. Siguientes Bloques

Prioridad tecnica:

1. Conectar ingesta real WooCommerce.
2. Persistir `catalog_products` en Supabase.
3. Agregar proveedor de embeddings.
4. Implementar busqueda pgvector + reranking por facetas.
5. Agregar parser de consulta a filtros estructurados.
6. Agregar tests con casos reales: `semilla 2m2`, `liso 1.2`, `ancho 1.40`, `ignifugo`, etc.
7. Agregar job periodico de sincronizacion.

## Decision Clave

La IA no deberia construir directamente filtros SQL estrictos. El microservicio debe recibir intencion + facetas, validar contra el indice real del catalogo y decidir que filtros son duros o blandos.

Ese es el salto de calidad: la IA conversa, pero el servicio gobierna la busqueda.

## Fuente Principal

WooCommerce es la fuente principal del catalogo. `productos.json` queda solo como fixture local para analizar estructura, probar normalizacion y depurar sin depender de red.
