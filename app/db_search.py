from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .embeddings import OpenAIEmbeddingClient
from .core.models import ProductDocument, ProductFilters, ProductSpecs, SearchHit, SearchRequest, SearchResponse
from .search_common import post_filter_specific_terms, specific_required_terms


class DatabaseSearchError(RuntimeError):
    pass


_POOLS: dict[tuple[int, str], ConnectionPool] = {}


RELAXATION_STEPS = [
    ["ancho_m"],
    ["espesor_mm"],
    ["ancho_m", "espesor_mm"],
    ["color"],
    ["material"],
    ["floor_design"],
    ["ancho_m", "espesor_mm", "color", "material"],
]


@dataclass(frozen=True)
class DatabaseCatalogSearch:
    embedder: OpenAIEmbeddingClient
    postgres_url: str | None = None

    def search(self, request: SearchRequest) -> SearchResponse:
        query_embedding = self.embedder.embed_many([request.query])[0]
        strict_hits = self._search_once(request, query_embedding, relaxed=[])
        total = self.count_products()
        if strict_hits or not request.relax_filters:
            return SearchResponse(query=request.query, hits=strict_hits, used_relaxation=False, total_catalog_size=total)

        for relaxed in RELAXATION_STEPS:
            hits = self._search_once(request, query_embedding, relaxed=relaxed)
            if hits:
                return SearchResponse(query=request.query, hits=hits, used_relaxation=True, total_catalog_size=total)

        return SearchResponse(query=request.query, hits=[], used_relaxation=False, total_catalog_size=total)

    def count_products(self) -> int:
        if not self.postgres_url:
            return 0

        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("select count(*) from catalog_products")
                return int(cur.fetchone()["count"])

    def catalog_context(self) -> str:
        facets = self.catalog_facets("pisos", True)
        floor_kinds = facets.get("floor_kinds") or {}
        return "\n".join(
            [
                "CATALOGO ODRANID — ESTADO ACTUAL (generado desde la base de datos)",
                f"Productos indexados: {self.count_products()}",
                "",
                "Rubros disponibles:",
                *[f"- {name}: {total} productos" for name, total in (facets.get("rubros") or {}).items()],
                "",
                "Pisos — valores reales en stock:",
                f"- Espesores en mm: {format_values(facets.get('espesores_mm') or [])}",
                f"- Anchos en m: {format_values(facets.get('anchos_m') or [])}",
                *(
                    [f"- Tipos: {format_dict(floor_kinds)}"]
                    if floor_kinds
                    else ["- Tipos: liso, diseno"]
                ),
                f"- Disenos: {format_dict(facets.get('floor_designs') or {})}",
                "",
                "Reglas de uso de estos datos:",
                "- Usar SOLO espesores y anchos que aparezcan en esta lista. Si el cliente pide uno que no existe, informarlo y ofrecer el más cercano.",
                "- Los m2 del cliente son superficie a cubrir, NUNCA ancho ni espesor.",
                "- Para semilla, aceptar semilla_melon como alternativa compatible.",
                "- Si no hay resultados exactos, relajar ancho/espesor/color/material antes que rubro.",
            ]
        )

    def catalog_facets(self, rubro: str | None = "pisos", in_stock_only: bool = True) -> dict[str, Any]:
        if not self.postgres_url:
            return {}

        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("select catalog_facets(%s, %s)", (rubro, in_stock_only))
                value = cur.fetchone()["catalog_facets"]
                return dict(value)

    def _search_once(self, request: SearchRequest, query_embedding: list[float], relaxed: list[str]) -> list[SearchHit]:
        filters = relaxed_filters(request.filters, relaxed)
        candidate_limit = candidate_search_limit(request.query, request.limit)
        if self.postgres_url:
            rows = self._search_postgres(query_embedding, filters, candidate_limit)
        else:
            raise DatabaseSearchError("No database search backend configured")

        hits = [
            SearchHit(
                product=product_from_row(row),
                score=float(row.get("similarity") or 0),
                matched_filters=matched_filter_names(filters),
                relaxed_filters=relaxed,
            )
            for row in rows
        ]
        if filters.exclude_vinilico:
            hits = [h for h in hits if h.product.category != "pisos_vinilicos"]
        return post_filter_specific_terms(request.query, hits, request.limit)

    def _search_postgres(self, query_embedding: list[float], filters: ProductFilters, limit: int) -> list[dict[str, Any]]:
        embedding = vector_literal(query_embedding)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select *
                    from search_catalog_products(
                      %(query_embedding)s::vector,
                      %(rubro)s::text,
                      %(category)s::text,
                      %(floor_kind)s::text,
                      %(floor_design)s::text,
                      %(espesor_mm)s::numeric,
                      %(ancho_m)s::numeric,
                      %(material)s::text,
                      %(color)s::text,
                      %(tags)s::text[],
                      %(in_stock_only)s::boolean,
                      %(limit)s::integer
                    )
                    """,
                    {
                        "query_embedding": embedding,
                        **filters_to_rpc_params(filters),
                        "limit": limit,
                    },
                )
                return [dict(row) for row in cur.fetchall()]

    def _connect(self) -> psycopg.Connection:
        try:
            return self._pool().connection()
        except psycopg.Error as exc:
            raise DatabaseSearchError(f"Could not connect to Postgres catalog search: {exc}") from exc

    def _pool(self) -> ConnectionPool:
        if not self.postgres_url:
            raise DatabaseSearchError("No database search backend configured")
        key = (os.getpid(), self.postgres_url)
        pool = _POOLS.get(key)
        if pool is None or pool.closed:
            pool = ConnectionPool(
                self.postgres_url,
                min_size=1,
                max_size=5,
                kwargs={"row_factory": dict_row},
                open=True,
            )
            _POOLS[key] = pool
        return pool


def relaxed_filters(filters: ProductFilters, relaxed: list[str]) -> ProductFilters:
    data = filters.model_dump()
    for name in relaxed:
        if name == "tags":
            data[name] = []
        elif name in data:
            data[name] = None
    return ProductFilters(**data)


def filters_to_rpc_params(filters: ProductFilters) -> dict[str, Any]:
    return {
        "rubro": filters.rubro,
        "category": filters.category,
        "floor_kind": filters.floor_kind,
        "floor_design": filters.floor_design,
        "espesor_mm": filters.espesor_mm,
        "ancho_m": filters.ancho_m,
        "material": filters.material,
        "color": filters.color,
        "tags": filters.tags,
        "in_stock_only": filters.in_stock_only,
    }


_INTERNAL_FILTER_FIELDS = {"in_stock_only", "exclude_vinilico"}


def matched_filter_names(filters: ProductFilters) -> list[str]:
    return [
        name
        for name, value in filters.model_dump().items()
        if value is not None and value != [] and name not in _INTERNAL_FILTER_FIELDS
    ]


def candidate_search_limit(query: str, limit: int) -> int:
    return max(limit, limit * 5) if specific_required_terms(query) else limit


def vector_literal(values: list[float]) -> str:
    return "[" + ",".join(str(float(value)) for value in values) + "]"


def product_from_row(row: dict[str, Any]) -> ProductDocument:
    metadata = row.get("metadata") or {}
    specs = ProductSpecs(
        espesor_mm=float_or_none(row.get("espesor_mm", metadata.get("espesor_mm"))),
        ancho_m=float_or_none(row.get("ancho_m", metadata.get("ancho_m"))),
        largo_m=float_or_none(row.get("largo_m", metadata.get("largo_m"))),
        rendimiento_m2=float_or_none(row.get("rendimiento_m2", metadata.get("rendimiento_m2"))),
        diametro_mm=float_or_none(row.get("diametro_mm", metadata.get("diametro_mm"))),
        largo_manguera_m=float_or_none(row.get("largo_manguera_m", metadata.get("largo_manguera_m"))),
    )
    return ProductDocument(
        id=int(row["id"]),
        title=row.get("title") or metadata.get("titulo") or "",
        slug=row.get("slug") or metadata.get("slug"),
        link=row.get("link") or metadata.get("link"),
        image=row.get("image") or metadata.get("image"),
        price=row.get("price") if row.get("price") is None else float(row["price"]),
        currency=row.get("currency") or metadata.get("moneda") or "ARS",
        in_stock=bool(row.get("in_stock", metadata.get("en_stock", True))),
        stock_text=row.get("stock_text") or metadata.get("stock_text"),
        rubro=row.get("rubro") or metadata.get("rubro") or "general",
        category=row.get("category") or metadata.get("categoria_principal") or metadata.get("category") or "general",
        subcategory=row.get("subcategory") or metadata.get("subcategoria"),
        product_type=row.get("product_type") or metadata.get("tipo_producto") or metadata.get("product_type") or "unidad",
        floor_kind=row.get("floor_kind") or metadata.get("tipo_piso_categoria") or metadata.get("floor_kind"),
        floor_design=row.get("floor_design") or metadata.get("tipo_piso_diseno") or metadata.get("floor_design"),
        material=row.get("material") or metadata.get("material"),
        color=row.get("color") or metadata.get("color"),
        environments=row.get("environments") or metadata.get("environments"),
        brands=list_field(row.get("brands") or metadata.get("brands")),
        categories=list_field(row.get("categories") or metadata.get("categories")),
        woo_tags=list_field(row.get("woo_tags") or metadata.get("woo_tags")),
        technical_tags=list_field(row.get("technical_tags") or metadata.get("tags")),
        specs=specs,
        raw_attributes=row.get("raw_attributes") or {},
        content=row.get("content") or "",
        metadata=metadata,
    )


def float_or_none(value: Any) -> float | None:
    return None if value is None else float(value)


def list_field(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, tuple):
        return [str(item) for item in value]
    return [str(value)]


def format_values(values: list[Any]) -> str:
    return ", ".join(str(value) for value in values) if values else "N/D"


def format_dict(values: dict[str, Any]) -> str:
    return ", ".join(f"{name} ({total})" for name, total in values.items()) if values else "N/D"
