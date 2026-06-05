from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.rows import dict_row
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

    def list_products(self) -> list[dict[str, Any]]:
        try:
            with psycopg.connect(self.database_url, row_factory=dict_row) as conn:
                with conn.cursor() as cur:
                    cur.execute(LIST_PRODUCTS_SQL)
                    return [row_with_parsed_embedding(dict(row)) for row in cur.fetchall()]
        except psycopg.Error as exc:
            raise PostgresStoreError(str(exc)) from exc

    def existing_embeddings_by_content_hashes(self, content_hash_by_id: Mapping[int, str]) -> dict[int, list[float]]:
        if not content_hash_by_id:
            return {}

        try:
            with psycopg.connect(self.database_url, row_factory=dict_row) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        select id, content_hash, embedding::text as embedding
                        from catalog_products
                        where id = any(%s::bigint[])
                        """,
                        (list(content_hash_by_id.keys()),),
                    )
                    rows = cur.fetchall()
        except psycopg.Error as exc:
            raise PostgresStoreError(str(exc)) from exc

        embeddings: dict[int, list[float]] = {}
        for row in rows:
            product_id = int(row["id"])
            if row.get("content_hash") != content_hash_by_id.get(product_id):
                continue
            embedding = parse_vector(row.get("embedding"))
            if embedding is not None:
                embeddings[product_id] = embedding
        return embeddings


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


def row_with_parsed_embedding(row: dict[str, Any]) -> dict[str, Any]:
    row["embedding"] = parse_vector(row.get("embedding"))
    return row


def parse_vector(value: Any) -> list[float] | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return [float(item) for item in value]

    text = str(value).strip()
    if not text:
        return None
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    if not text.strip():
        return []
    return [float(part.strip()) for part in text.split(",") if part.strip()]


LIST_PRODUCTS_SQL = """
select
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
  embedding::text as embedding,
  content_hash
from catalog_products
order by id
"""


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
