-- Extiende catalog_facets con `categories_by_rubro`: por cada rubro, las
-- categorías que hoy tienen stock. Sirve para que el agente conozca el mapa de
-- líneas de producto (Calzado -> para_lluvia, etc.) sin hardcodearlo en el prompt.
-- create or replace: reemplaza la versión de 001 de forma idempotente.
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
    -- Mapa de categorías de TODOS los rubros (no filtra por p_rubro, igual que `rubros`):
    -- es la orientación de catálogo que el agente necesita completa.
    'categories_by_rubro', (
      select coalesce(jsonb_object_agg(rubro, cats), '{}'::jsonb)
      from (
        select rubro, jsonb_agg(category order by category) as cats
        from (
          select distinct rubro, category
          from public.catalog_products
          where category is not null
            and (not p_in_stock_only or in_stock = true)
        ) d
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
