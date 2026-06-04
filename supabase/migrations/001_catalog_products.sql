create extension if not exists vector;

create table if not exists public.catalog_products (
  id bigint primary key,
  title text not null,
  slug text,
  link text,
  image text,
  price numeric,
  currency text not null default 'ARS',
  in_stock boolean not null default true,
  stock_text text,

  rubro text not null,
  category text not null,
  subcategory text,
  product_type text not null default 'unidad',

  floor_kind text,
  floor_design text,
  material text,
  color text,
  environments text,
  brands text[] not null default '{}',
  categories text[] not null default '{}',
  woo_tags text[] not null default '{}',
  technical_tags text[] not null default '{}',

  espesor_mm numeric,
  ancho_m numeric,
  largo_m numeric,
  rendimiento_m2 numeric,
  diametro_mm numeric,
  largo_manguera_m numeric,

  content text not null,
  metadata jsonb not null default '{}',
  raw_attributes jsonb not null default '{}',

  embedding vector(1536),
  content_hash text not null,
  source_updated_at timestamptz,
  indexed_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists catalog_products_embedding_hnsw_idx
  on public.catalog_products using hnsw (embedding vector_cosine_ops);

create index if not exists catalog_products_core_facets_idx
  on public.catalog_products (rubro, category, floor_kind, floor_design, in_stock);

create index if not exists catalog_products_specs_idx
  on public.catalog_products (espesor_mm, ancho_m);

create index if not exists catalog_products_stock_idx
  on public.catalog_products (in_stock);

create index if not exists catalog_products_technical_tags_idx
  on public.catalog_products using gin (technical_tags);

create index if not exists catalog_products_metadata_idx
  on public.catalog_products using gin (metadata);

create or replace function public.touch_catalog_products_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists catalog_products_touch_updated_at on public.catalog_products;

create trigger catalog_products_touch_updated_at
before update on public.catalog_products
for each row execute function public.touch_catalog_products_updated_at();

create or replace function public.search_catalog_products(
  query_embedding vector(1536),
  p_rubro text default null,
  p_category text default null,
  p_floor_kind text default null,
  p_floor_design text default null,
  p_espesor_mm numeric default null,
  p_ancho_m numeric default null,
  p_material text default null,
  p_color text default null,
  p_tags text[] default '{}',
  p_in_stock_only boolean default true,
  match_count integer default 20
)
returns table (
  id bigint,
  title text,
  link text,
  price numeric,
  currency text,
  in_stock boolean,
  product_type text,
  content text,
  metadata jsonb,
  similarity double precision
)
language sql
stable
as $$
  select
    p.id,
    p.title,
    p.link,
    p.price,
    p.currency,
    p.in_stock,
    p.product_type,
    p.content,
    p.metadata,
    1 - (p.embedding <=> query_embedding) as similarity
  from public.catalog_products p
  where p.embedding is not null
    and (not p_in_stock_only or p.in_stock = true)
    and (p_rubro is null or p.rubro = p_rubro)
    and (p_category is null or p.category = p_category)
    and (p_floor_kind is null or p.floor_kind = p_floor_kind)
    and (
      p_floor_design is null
      or p.floor_design = p_floor_design
      or (p_floor_design = 'semilla' and p.floor_design = 'semilla_melon')
    )
    and (p_espesor_mm is null or p.espesor_mm = p_espesor_mm)
    and (p_ancho_m is null or p.ancho_m = p_ancho_m)
    and (p_material is null or lower(coalesce(p.material, '')) like '%' || lower(p_material) || '%')
    and (p_color is null or lower(coalesce(p.color, '')) like '%' || lower(p_color) || '%')
    and (cardinality(p_tags) = 0 or p.technical_tags @> p_tags)
  order by p.embedding <=> query_embedding
  limit least(match_count, 50);
$$;

create or replace function public.catalog_facets(
  p_rubro text default null,
  p_in_stock_only boolean default true
)
returns jsonb
language sql
stable
as $$
  select jsonb_build_object(
    'rubros', (
      select coalesce(jsonb_object_agg(rubro, total order by total desc), '{}'::jsonb)
      from (
        select rubro, count(*) as total
        from public.catalog_products
        where (not p_in_stock_only or in_stock = true)
        group by rubro
      ) s
    ),
    'floor_designs', (
      select coalesce(jsonb_object_agg(floor_design, total order by total desc), '{}'::jsonb)
      from (
        select floor_design, count(*) as total
        from public.catalog_products
        where floor_design is not null
          and (p_rubro is null or rubro = p_rubro)
          and (not p_in_stock_only or in_stock = true)
        group by floor_design
      ) s
    ),
    'espesores_mm', (
      select coalesce(jsonb_agg(espesor_mm order by espesor_mm), '[]'::jsonb)
      from (
        select distinct espesor_mm
        from public.catalog_products
        where espesor_mm is not null
          and (p_rubro is null or rubro = p_rubro)
          and (not p_in_stock_only or in_stock = true)
      ) s
    ),
    'anchos_m', (
      select coalesce(jsonb_agg(ancho_m order by ancho_m), '[]'::jsonb)
      from (
        select distinct ancho_m
        from public.catalog_products
        where ancho_m is not null
          and (p_rubro is null or rubro = p_rubro)
          and (not p_in_stock_only or in_stock = true)
      ) s
    )
  );
$$;

grant usage on schema public to service_role;
grant all on public.catalog_products to service_role;
grant execute on function public.search_catalog_products(
  vector,
  text,
  text,
  text,
  text,
  numeric,
  numeric,
  text,
  text,
  text[],
  boolean,
  integer
) to service_role;
grant execute on function public.catalog_facets(text, boolean) to service_role;

notify pgrst, 'reload schema';
