"""Hybrid catalog search backed by Typesense.

Core of Fase 3: replaces the "hard filter + progressive relaxation" model with
scoring. ``rubro`` and stock are the ONLY hard filters; every other attribute
(espesor, ancho, tipo, diseño, material, color, tags) feeds the ranking via
Typesense ``_eval`` boosts but never excludes a product. The query also runs as
hybrid (keyword + vector) so semantics still help. Results are always the top-N,
each tagged exact vs. alternative.

The query builders and result mapping are pure functions (unit-tested without a
live server); ``TypesenseCatalogSearch.search`` wires them to the SDK client.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..catalog.domain_synonyms import compatible_designs
from .embeddings import OpenAIEmbeddingClient
from ..catalog.footwear import talle_excluded
from ..core.models import ProductDocument, ProductFilters, ProductSpecs, SearchHit, SearchRequest, SearchResponse
from .typesense_index import QUERY_BY_FIELDS

# Attributes that influence ranking but never exclude. Weight = how much a match
# matters relative to the others.
SOFT_ATTRIBUTE_WEIGHTS = {
    "floor_design": 3,
    "espesor_mm": 2,
    "ancho_m": 2,
    "floor_kind": 1,
    "material": 1,
    "color": 1,
}

MAX_CANDIDATES = 50


class TypesenseSearchError(RuntimeError):
    pass


def _fmt_number(value: float) -> str:
    return f"{float(value):g}"


def build_filter_by(filters: ProductFilters) -> str:
    """The ONLY hard filters: rubro and stock."""
    clauses: list[str] = []
    if filters.rubro:
        clauses.append(f"rubro:={filters.rubro}")
    if filters.in_stock_only:
        clauses.append("in_stock:=true")
    return " && ".join(clauses)


def build_sort_by(filters: ProductFilters) -> str:
    """Attribute matches add to the ranking via _eval; nothing is excluded."""
    evals: list[str] = []
    if filters.floor_design:
        designs = sorted(compatible_designs(filters.floor_design))
        target = f"[{','.join(designs)}]" if len(designs) > 1 else designs[0]
        evals.append(f"(floor_design:={target}):{SOFT_ATTRIBUTE_WEIGHTS['floor_design']}")
    if filters.espesor_mm is not None:
        evals.append(f"(espesor_mm:={_fmt_number(filters.espesor_mm)}):{SOFT_ATTRIBUTE_WEIGHTS['espesor_mm']}")
    if filters.ancho_m is not None:
        evals.append(f"(ancho_m:={_fmt_number(filters.ancho_m)}):{SOFT_ATTRIBUTE_WEIGHTS['ancho_m']}")
    if filters.floor_kind:
        evals.append(f"(floor_kind:={filters.floor_kind}):{SOFT_ATTRIBUTE_WEIGHTS['floor_kind']}")
    if filters.material:
        evals.append(f"(material:={filters.material}):{SOFT_ATTRIBUTE_WEIGHTS['material']}")
    if filters.color:
        evals.append(f"(color:={filters.color}):{SOFT_ATTRIBUTE_WEIGHTS['color']}")
    for tag in filters.tags:
        evals.append(f"(technical_tags:={tag}):1")

    parts: list[str] = []
    if evals:
        parts.append(f"_eval([{', '.join(evals)}]):desc")
    parts.append("_text_match:desc")
    return ", ".join(parts)


def candidate_pool_size(limit: int) -> int:
    # Traemos un pool amplio para re-rankear en Python (exactos primero), porque el
    # _eval de Typesense no garantiza que los matches exactos queden arriba.
    return min(MAX_CANDIDATES, max(limit * 5, 25))


def build_search_params(request: SearchRequest, query_embedding: list[float] | None) -> dict[str, Any]:
    candidates = candidate_pool_size(request.limit)
    params: dict[str, Any] = {
        "q": request.query.strip() or "*",
        "query_by": ",".join(QUERY_BY_FIELDS),
        "per_page": candidates,
        "sort_by": build_sort_by(request.filters),
        "exhaustive_search": True,
    }
    filter_by = build_filter_by(request.filters)
    if filter_by:
        params["filter_by"] = filter_by
    if query_embedding:
        vector = ",".join(f"{value:.6f}" for value in query_embedding)
        params["vector_query"] = f"embedding:([{vector}], k:{candidates})"
    return params


def product_from_typesense_doc(doc: dict[str, Any]) -> ProductDocument:
    return ProductDocument(
        id=int(doc["id"]),
        title=doc.get("title") or "",
        link=doc.get("link"),
        price=doc.get("price"),
        in_stock=bool(doc.get("in_stock", True)),
        rubro=doc.get("rubro") or "general",
        category=doc.get("category") or "general",
        product_type=doc.get("product_type") or "unidad",
        floor_kind=doc.get("floor_kind"),
        floor_design=doc.get("floor_design"),
        material=doc.get("material"),
        color=doc.get("color"),
        technical_tags=doc.get("technical_tags") or [],
        specs=ProductSpecs(
            espesor_mm=doc.get("espesor_mm"),
            ancho_m=doc.get("ancho_m"),
            largo_m=doc.get("largo_m"),
            rendimiento_m2=doc.get("rendimiento_m2"),
            diametro_mm=doc.get("diametro_mm"),
            largo_manguera_m=doc.get("largo_manguera_m"),
        ),
        content=doc.get("content") or "",
    )


def _attribute_matches(product: ProductDocument, name: str, value: Any) -> bool:
    if name == "floor_design":
        return product.floor_design in compatible_designs(value)
    if name == "floor_kind":
        return product.floor_kind == value
    if name == "material":
        return bool(product.material) and value.lower() in product.material.lower()
    if name == "color":
        return bool(product.color) and value.lower() in product.color.lower()
    if name == "espesor_mm":
        return product.specs.espesor_mm == value
    if name == "ancho_m":
        return product.specs.ancho_m == value
    return False


def requested_soft_attributes(filters: ProductFilters) -> dict[str, Any]:
    requested: dict[str, Any] = {}
    for name in SOFT_ATTRIBUTE_WEIGHTS:
        value = getattr(filters, name, None)
        if value is not None:
            requested[name] = value
    return requested


def classify_hit(product: ProductDocument, filters: ProductFilters) -> tuple[list[str], bool]:
    """Return (matched attribute names, is_alternative)."""
    requested = requested_soft_attributes(filters)
    matched = [name for name, value in requested.items() if _attribute_matches(product, name, value)]
    matched_tags = [tag for tag in filters.tags if tag in product.technical_tags]
    total_requested = len(requested) + len(filters.tags)
    total_matched = len(matched) + len(matched_tags)
    is_alternative = total_requested > 0 and total_matched < total_requested
    return matched + matched_tags, is_alternative


def hits_from_response(payload: dict[str, Any], filters: ProductFilters) -> list[SearchHit]:
    hits: list[SearchHit] = []
    for raw in payload.get("hits") or []:
        document = raw.get("document") or {}
        product = product_from_typesense_doc(document)
        matched, is_alternative = classify_hit(product, filters)
        # Calzado: si el cliente pidió un talle y el rango declarado del producto
        # no lo incluye, no le entra — marcar como alternativa para mandarlo al fondo.
        if filters.talle is not None and talle_excluded(product.title, filters.talle):
            is_alternative = True
        hits.append(
            SearchHit(
                product=product,
                score=_hit_score(raw),
                matched_filters=matched,
                is_alternative=is_alternative,
            )
        )
    return hits


def _hit_score(raw: dict[str, Any]) -> float:
    distance = raw.get("vector_distance")
    if distance is not None:
        return float(1 - distance)
    text_match = raw.get("text_match")
    return float(text_match) if text_match is not None else 0.0


@dataclass(frozen=True)
class TypesenseCatalogSearch:
    client: Any
    embedder: OpenAIEmbeddingClient | None
    collection: str

    def search(self, request: SearchRequest) -> SearchResponse:
        query_embedding = None
        if self.embedder is not None and request.query.strip():
            query_embedding = self.embedder.embed_many([request.query])[0]

        params = build_search_params(request, query_embedding)
        try:
            if query_embedding is not None:
                # Vector queries with 1536 dimensions exceed Typesense's GET
                # query-string limit through documents.search; multi_search
                # sends the same search params as a POST body.
                payload = self.client.multi_search.perform(
                    {"searches": [{"collection": self.collection, **params}]},
                    {},
                )
                payload = (payload.get("results") or [{}])[0]
            else:
                payload = self.client.collections[self.collection].documents.search(params)
        except Exception as exc:
            raise TypesenseSearchError(f"Typesense search failed: {exc}") from exc

        hits = rerank_exact_first(hits_from_response(payload, request.filters))
        total = int(payload.get("out_of") or payload.get("found") or 0)
        # Scoring model has no relaxation step: everything that survives the
        # rubro/stock filter is returned, ranked exact-first, tagged exact/alternative.
        return SearchResponse(
            query=request.query,
            hits=hits[: request.limit],
            used_relaxation=False,
            total_catalog_size=total,
        )


def rerank_exact_first(hits: list[SearchHit]) -> list[SearchHit]:
    """Exact matches (all requested attributes) first, then by number of matched
    attributes, then by the engine's relevance score. Keeps the original engine
    order as the final tie-breaker (stable sort)."""
    return sorted(
        hits,
        key=lambda hit: (hit.is_alternative, -len(hit.matched_filters), -hit.score),
    )
