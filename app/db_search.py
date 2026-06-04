from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import psycopg
import re

from .embeddings import OpenAIEmbeddingClient
from .models import ProductDocument, ProductFilters, ProductSpecs, SearchHit, SearchRequest, SearchResponse
from .normalization import norm_num, norm_text


class DatabaseSearchError(RuntimeError):
    pass


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

        with psycopg.connect(self.postgres_url) as conn:
            with conn.cursor() as cur:
                cur.execute("select count(*) from catalog_products")
                return int(cur.fetchone()[0])

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

        with psycopg.connect(self.postgres_url) as conn:
            with conn.cursor() as cur:
                cur.execute("select catalog_facets(%s, %s)", (rubro, in_stock_only))
                value = cur.fetchone()[0]
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
        with psycopg.connect(self.postgres_url) as conn:
            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
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


def post_filter_specific_terms(query: str, hits: list[SearchHit], limit: int) -> list[SearchHit]:
    terms = specific_required_terms(query)
    if not terms:
        return hits[:limit]

    filtered = [hit for hit in hits if all(term in searchable_product_text(hit.product) for term in terms)]
    return filtered[:limit]


def candidate_search_limit(query: str, limit: int) -> int:
    return max(limit, limit * 5) if specific_required_terms(query) else limit


def specific_required_terms(query: str) -> list[str]:
    text = norm_text(query)
    terms = []
    if has_word(text, "tejo") or has_word(text, "tejos"):
        terms.append("tejo")
    if has_word(text, "frisbee"):
        terms.append("frisbee")
    hose_terms = required_hose_terms(text)
    terms.extend(term for term in hose_terms if term not in terms)
    return terms


def required_hose_terms(text: str) -> list[str]:
    if not any(term in text for term in ["manguera", "riego", "jardin", "jardin", "diametro"]):
        return []

    terms: list[str] = []
    for match in re.finditer(r"\b(\d+\s*/\s*\d+)\b", text):
        terms.append(match.group(1).replace(" ", ""))

    length_match = re.search(r"\b(\d+(?:[.,]\d+)?)\s*(?:m|mt|mts|metro|metros)\b", text)
    if length_match:
        value = norm_num(length_match.group(1))
        if value is not None and value >= 5:
            terms.append(f"{value:g}")

    return terms


def searchable_product_text(product: ProductDocument) -> str:
    return norm_text(
        " ".join(
            [
                product.title,
                product.content,
                product.category,
                product.subcategory or "",
                " ".join(product.technical_tags),
            ]
        )
    )


def has_word(text: str, word: str) -> bool:
    return any(part == word for part in text.split())


def vector_literal(values: list[float]) -> str:
    return "[" + ",".join(str(float(value)) for value in values) + "]"


def product_from_row(row: dict[str, Any]) -> ProductDocument:
    metadata = row.get("metadata") or {}
    specs = ProductSpecs(
        espesor_mm=metadata.get("espesor_mm"),
        ancho_m=metadata.get("ancho_m"),
        largo_m=metadata.get("largo_m"),
        rendimiento_m2=metadata.get("rendimiento_m2"),
    )
    return ProductDocument(
        id=int(row["id"]),
        title=row.get("title") or metadata.get("titulo") or "",
        link=row.get("link") or metadata.get("link"),
        price=row.get("price") if row.get("price") is None else float(row["price"]),
        currency=row.get("currency") or metadata.get("moneda") or "ARS",
        in_stock=bool(row.get("in_stock", metadata.get("en_stock", True))),
        rubro=metadata.get("rubro") or "general",
        category=metadata.get("categoria_principal") or metadata.get("category") or "general",
        subcategory=metadata.get("subcategoria"),
        product_type=row.get("product_type") or metadata.get("tipo_producto") or metadata.get("product_type") or "unidad",
        floor_kind=metadata.get("tipo_piso_categoria") or metadata.get("floor_kind"),
        floor_design=metadata.get("tipo_piso_diseno") or metadata.get("floor_design"),
        color=metadata.get("color"),
        material=metadata.get("material"),
        technical_tags=metadata.get("tags") or [],
        specs=specs,
        content=row.get("content") or "",
        metadata=metadata,
    )


def format_values(values: list[Any]) -> str:
    return ", ".join(str(value) for value in values) if values else "N/D"


def format_dict(values: dict[str, Any]) -> str:
    return ", ".join(f"{name} ({total})" for name, total in values.items()) if values else "N/D"
