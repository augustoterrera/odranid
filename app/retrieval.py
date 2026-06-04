from __future__ import annotations

import math
import re
from collections import Counter

from .db_search import post_filter_specific_terms
from .models import ProductDocument, ProductFilters, SearchHit, SearchRequest, SearchResponse
from .normalization import norm_text


FILTER_ORDER = [
    "rubro",
    "category",
    "subcategory",
    "floor_kind",
    "floor_design",
    "material",
    "color",
    "espesor_mm",
    "ancho_m",
    "tags",
]


class CatalogSearch:
    """Local hybrid search.

    This class is intentionally provider-free. In production the first candidate
    set should come from pgvector, then this same facet/rerank logic can
    run over those candidates.
    """

    def __init__(self, products: list[ProductDocument]):
        self.products = products

    def search(self, request: SearchRequest) -> SearchResponse:
        strict_hits = self._search_with_filters(request.query, request.filters, request.limit, relaxed=[])
        if strict_hits or not request.relax_filters:
            return SearchResponse(
                query=request.query,
                hits=strict_hits,
                used_relaxation=False,
                total_catalog_size=len(self.products),
            )

        for relaxed in self._relaxation_steps(request.filters):
            hits = self._search_with_filters(request.query, request.filters, request.limit, relaxed=relaxed)
            if hits:
                return SearchResponse(
                    query=request.query,
                    hits=hits,
                    used_relaxation=True,
                    total_catalog_size=len(self.products),
                )

        return SearchResponse(query=request.query, hits=[], used_relaxation=False, total_catalog_size=len(self.products))

    def _search_with_filters(
        self,
        query: str,
        filters: ProductFilters,
        limit: int,
        relaxed: list[str],
    ) -> list[SearchHit]:
        candidates: list[SearchHit] = []
        for product in self.products:
            matched, failed = self._match_filters(product, filters, relaxed)
            if failed:
                continue
            lexical = self._lexical_score(query, product)
            facet_boost = len(matched) * 0.05
            stock_boost = 0.08 if product.in_stock else 0
            score = lexical + facet_boost + stock_boost
            candidates.append(SearchHit(product=product, score=score, matched_filters=matched, relaxed_filters=relaxed))

        candidates.sort(key=lambda hit: hit.score, reverse=True)
        return post_filter_specific_terms(query, candidates, limit)

    def _match_filters(self, product: ProductDocument, filters: ProductFilters, relaxed: list[str]) -> tuple[list[str], list[str]]:
        matched: list[str] = []
        failed: list[str] = []

        def check(name: str, condition: bool) -> None:
            if name in relaxed:
                return
            if condition:
                matched.append(name)
            else:
                failed.append(name)

        if filters.in_stock_only and "in_stock_only" not in relaxed and not product.in_stock:
            failed.append("in_stock_only")

        if filters.rubro:
            check("rubro", product.rubro == filters.rubro)
        if filters.category:
            check("category", product.category == filters.category)
        if filters.subcategory:
            check("subcategory", product.subcategory == filters.subcategory)
        if filters.floor_kind:
            check("floor_kind", product.floor_kind == filters.floor_kind)
        if filters.floor_design:
            check("floor_design", self._same_design(product.floor_design, filters.floor_design))
        if filters.material:
            material = norm_text(product.material or "")
            check("material", norm_text(filters.material) in material or material in norm_text(filters.material))
        if filters.color:
            check("color", norm_text(filters.color) in norm_text(product.color or ""))
        if filters.espesor_mm is not None:
            check("espesor_mm", close_number(product.specs.espesor_mm, filters.espesor_mm, tolerance=0.01))
        if filters.ancho_m is not None:
            check("ancho_m", close_number(product.specs.ancho_m, filters.ancho_m, tolerance=0.01))
        if filters.tags:
            product_tags = set(product.technical_tags)
            check("tags", all(tag in product_tags for tag in filters.tags))

        return matched, failed

    def _same_design(self, product_design: str | None, requested_design: str) -> bool:
        product_norm = norm_text(product_design or "")
        requested_norm = norm_text(requested_design)
        if product_norm == requested_norm:
            return True
        if requested_norm == "semilla" and product_norm == "semilla_melon":
            return True
        return False

    def _relaxation_steps(self, filters: ProductFilters) -> list[list[str]]:
        steps: list[list[str]] = []
        if filters.ancho_m is not None:
            steps.append(["ancho_m"])
        if filters.espesor_mm is not None:
            steps.append(["espesor_mm"])
        if filters.ancho_m is not None or filters.espesor_mm is not None:
            steps.append(["ancho_m", "espesor_mm"])
        if filters.color:
            steps.append(["color"])
        if filters.material:
            steps.append(["material"])
        if filters.floor_design:
            steps.append(["floor_design"])
        steps.append(["ancho_m", "espesor_mm", "color", "material"])
        return steps

    def _lexical_score(self, query: str, product: ProductDocument) -> float:
        query_terms = tokenize(query)
        if not query_terms:
            return 0
        haystack_terms = tokenize(" ".join([product.title, product.content, " ".join(product.technical_tags)]))
        counts = Counter(haystack_terms)
        matched = sum(min(counts[term], 2) for term in query_terms)
        coverage = matched / max(len(query_terms), 1)
        title_bonus = sum(1 for term in query_terms if term in tokenize(product.title)) / max(len(query_terms), 1)
        return coverage + title_bonus * 0.25


def tokenize(text: str) -> list[str]:
    return [term for term in re.split(r"[^a-z0-9_]+", norm_text(text)) if len(term) > 1]


def close_number(value: float | None, expected: float, tolerance: float) -> bool:
    if value is None:
        return False
    return math.isclose(float(value), float(expected), abs_tol=tolerance)
