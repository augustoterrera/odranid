create or replace function public.upsert_catalog_products(p_products jsonb)
returns integer
language plpgsql
security definer
set search_path = public
as $$
declare
  p_product jsonb;
  v_count integer := 0;
begin
  for p_product in select * from jsonb_array_elements(p_products)
  loop
    insert into public.catalog_products (
      id,
      title,
      slug,
      link,
      image,
      price,
      currency,
      in_stock,
      stock_text,
      rubro,
      category,
      subcategory,
      product_type,
      floor_kind,
      floor_design,
      material,
      color,
      environments,
      brands,
      categories,
      woo_tags,
      technical_tags,
      espesor_mm,
      ancho_m,
      largo_m,
      rendimiento_m2,
      diametro_mm,
      largo_manguera_m,
      content,
      metadata,
      raw_attributes,
      embedding,
      content_hash
    )
    values (
      (p_product->>'id')::bigint,
      p_product->>'title',
      p_product->>'slug',
      p_product->>'link',
      p_product->>'image',
      nullif(p_product->>'price', '')::numeric,
      coalesce(p_product->>'currency', 'ARS'),
      coalesce((p_product->>'in_stock')::boolean, true),
      p_product->>'stock_text',
      p_product->>'rubro',
      p_product->>'category',
      p_product->>'subcategory',
      coalesce(p_product->>'product_type', 'unidad'),
      p_product->>'floor_kind',
      p_product->>'floor_design',
      p_product->>'material',
      p_product->>'color',
      p_product->>'environments',
      coalesce(array(select jsonb_array_elements_text(coalesce(p_product->'brands', '[]'::jsonb))), '{}'),
      coalesce(array(select jsonb_array_elements_text(coalesce(p_product->'categories', '[]'::jsonb))), '{}'),
      coalesce(array(select jsonb_array_elements_text(coalesce(p_product->'woo_tags', '[]'::jsonb))), '{}'),
      coalesce(array(select jsonb_array_elements_text(coalesce(p_product->'technical_tags', '[]'::jsonb))), '{}'),
      nullif(p_product->>'espesor_mm', '')::numeric,
      nullif(p_product->>'ancho_m', '')::numeric,
      nullif(p_product->>'largo_m', '')::numeric,
      nullif(p_product->>'rendimiento_m2', '')::numeric,
      nullif(p_product->>'diametro_mm', '')::numeric,
      nullif(p_product->>'largo_manguera_m', '')::numeric,
      p_product->>'content',
      coalesce(p_product->'metadata', '{}'::jsonb),
      coalesce(p_product->'raw_attributes', '{}'::jsonb),
      case
        when p_product ? 'embedding' then
          ('[' || array_to_string(array(select jsonb_array_elements_text(p_product->'embedding')), ',') || ']')::vector
        else null
      end,
      p_product->>'content_hash'
    )
    on conflict (id) do update set
      title = excluded.title,
      slug = excluded.slug,
      link = excluded.link,
      image = excluded.image,
      price = excluded.price,
      currency = excluded.currency,
      in_stock = excluded.in_stock,
      stock_text = excluded.stock_text,
      rubro = excluded.rubro,
      category = excluded.category,
      subcategory = excluded.subcategory,
      product_type = excluded.product_type,
      floor_kind = excluded.floor_kind,
      floor_design = excluded.floor_design,
      material = excluded.material,
      color = excluded.color,
      environments = excluded.environments,
      brands = excluded.brands,
      categories = excluded.categories,
      woo_tags = excluded.woo_tags,
      technical_tags = excluded.technical_tags,
      espesor_mm = excluded.espesor_mm,
      ancho_m = excluded.ancho_m,
      largo_m = excluded.largo_m,
      rendimiento_m2 = excluded.rendimiento_m2,
      diametro_mm = excluded.diametro_mm,
      largo_manguera_m = excluded.largo_manguera_m,
      content = excluded.content,
      metadata = excluded.metadata,
      raw_attributes = excluded.raw_attributes,
      embedding = coalesce(excluded.embedding, public.catalog_products.embedding),
      content_hash = excluded.content_hash,
      indexed_at = now();

    v_count := v_count + 1;
  end loop;

  return v_count;
end;
$$;

grant execute on function public.upsert_catalog_products(jsonb) to service_role;

notify pgrst, 'reload schema';
