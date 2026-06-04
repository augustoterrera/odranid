from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.types.json import Jsonb


class PostgresStoreError(RuntimeError):
    pass


@dataclass(frozen=True)
class PostgresCatalogStore:
    database_url: str

    def upsert_products(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return

        try:
            with psycopg.connect(self.database_url) as conn:
                with conn.cursor() as cur:
                    for row in rows:
                        cur.execute(UPSERT_SQL, row_to_params(row))
                conn.commit()
        except psycopg.Error as exc:
            raise PostgresStoreError(str(exc)) from exc


def build_postgres_store_from_settings(settings: Any) -> PostgresCatalogStore:
    if not settings.database_url:
        raise PostgresStoreError("ODRANID_DATABASE_URL or DATABASE_URL is required")
    return PostgresCatalogStore(settings.database_url)


def row_to_params(row: dict[str, Any]) -> dict[str, Any]:
    params = dict(row)
    params["metadata"] = Jsonb(row.get("metadata") or {})
    params["raw_attributes"] = Jsonb(row.get("raw_attributes") or {})
    embedding = row.get("embedding")
    params["embedding"] = f"[{','.join(str(float(value)) for value in embedding)}]" if embedding is not None else None
    return params


UPSERT_SQL = """
insert into catalog_products (
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
  %(id)s,
  %(title)s,
  %(slug)s,
  %(link)s,
  %(image)s,
  %(price)s,
  %(currency)s,
  %(in_stock)s,
  %(stock_text)s,
  %(rubro)s,
  %(category)s,
  %(subcategory)s,
  %(product_type)s,
  %(floor_kind)s,
  %(floor_design)s,
  %(material)s,
  %(color)s,
  %(environments)s,
  %(brands)s,
  %(categories)s,
  %(woo_tags)s,
  %(technical_tags)s,
  %(espesor_mm)s,
  %(ancho_m)s,
  %(largo_m)s,
  %(rendimiento_m2)s,
  %(diametro_mm)s,
  %(largo_manguera_m)s,
  %(content)s,
  %(metadata)s,
  %(raw_attributes)s,
  %(embedding)s::vector,
  %(content_hash)s
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
  embedding = coalesce(excluded.embedding, catalog_products.embedding),
  content_hash = excluded.content_hash,
  indexed_at = now();
"""
