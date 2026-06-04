from __future__ import annotations

import unittest

from app.models import ProductFilters, SearchRequest
from app.typesense_search import (
    TypesenseCatalogSearch,
    build_filter_by,
    build_search_params,
    build_sort_by,
    classify_hit,
    hits_from_response,
    product_from_typesense_doc,
)


class FilterTests(unittest.TestCase):
    def test_only_rubro_and_stock_are_hard_filters(self) -> None:
        filters = ProductFilters(rubro="pisos", espesor_mm=3, ancho_m=1, floor_design="moneda", in_stock_only=True)
        clause = build_filter_by(filters)
        self.assertEqual(clause, "rubro:=pisos && in_stock:=true")
        # Attributes must NOT appear in the hard filter.
        self.assertNotIn("espesor_mm", clause)
        self.assertNotIn("ancho_m", clause)
        self.assertNotIn("floor_design", clause)

    def test_sort_by_boosts_attributes_without_excluding(self) -> None:
        filters = ProductFilters(rubro="pisos", espesor_mm=3, ancho_m=1, floor_design="semilla")
        sort_by = build_sort_by(filters)
        self.assertIn("_eval(", sort_by)
        self.assertIn("espesor_mm:=3", sort_by)
        self.assertIn("ancho_m:=1", sort_by)
        # semilla expands to its compatible designs.
        self.assertIn("semilla_melon", sort_by)
        self.assertTrue(sort_by.endswith("_text_match:desc"))

    def test_search_params_include_vector_query_when_embedding_present(self) -> None:
        request = SearchRequest(query="piso moneda", filters=ProductFilters(rubro="pisos"), limit=5)
        params = build_search_params(request, [0.1, 0.2, 0.3])
        self.assertIn("vector_query", params)
        self.assertIn("embedding:(", params["vector_query"])
        self.assertEqual(params["filter_by"], "rubro:=pisos && in_stock:=true")

    def test_search_params_omit_vector_query_without_embedding(self) -> None:
        request = SearchRequest(query="piso", filters=ProductFilters(rubro="pisos"), limit=5)
        params = build_search_params(request, None)
        self.assertNotIn("vector_query", params)


class ClassifyTests(unittest.TestCase):
    def _doc(self, **over) -> dict:
        base = dict(id="1", title="Piso moneda", rubro="pisos", in_stock=True,
                    floor_design="moneda", espesor_mm=3, ancho_m=1.0, content="x")
        base.update(over)
        return base

    def test_exact_match_when_all_attributes_match(self) -> None:
        product = product_from_typesense_doc(self._doc())
        filters = ProductFilters(rubro="pisos", floor_design="moneda", espesor_mm=3, ancho_m=1.0)
        matched, is_alternative = classify_hit(product, filters)
        self.assertFalse(is_alternative)
        self.assertIn("floor_design", matched)
        self.assertIn("espesor_mm", matched)

    def test_alternative_when_an_attribute_differs(self) -> None:
        product = product_from_typesense_doc(self._doc(espesor_mm=2))
        filters = ProductFilters(rubro="pisos", floor_design="moneda", espesor_mm=3)
        matched, is_alternative = classify_hit(product, filters)
        self.assertTrue(is_alternative)
        self.assertIn("floor_design", matched)
        self.assertNotIn("espesor_mm", matched)

    def test_semilla_request_matches_semilla_melon_product(self) -> None:
        product = product_from_typesense_doc(self._doc(floor_design="semilla_melon"))
        filters = ProductFilters(rubro="pisos", floor_design="semilla")
        matched, is_alternative = classify_hit(product, filters)
        self.assertIn("floor_design", matched)
        self.assertFalse(is_alternative)


class FakeDocuments:
    def __init__(self, payload):
        self.payload = payload
        self.last_params = None

    def search(self, params):
        self.last_params = params
        return self.payload


class FakeCollection:
    def __init__(self, payload):
        self.documents = FakeDocuments(payload)


class FakeClient:
    def __init__(self, payload):
        self._col = FakeCollection(payload)

    @property
    def collections(self):
        return {"catalog_products": self._col}


class FakeMultiSearch:
    def __init__(self, payload):
        self.payload = payload
        self.last_search_queries = None
        self.last_common_params = None

    def perform(self, search_queries, common_params):
        self.last_search_queries = search_queries
        self.last_common_params = common_params
        return {"results": [self.payload]}


class FakeHybridClient(FakeClient):
    def __init__(self, payload):
        super().__init__(payload)
        self.multi_search = FakeMultiSearch(payload)


class FakeEmbedder:
    def embed_many(self, texts):
        return [[0.1, 0.2, 0.3] for _ in texts]


class SearchTests(unittest.TestCase):
    def test_search_returns_topn_and_tags_alternatives(self) -> None:
        payload = {
            "found": 2,
            "hits": [
                {"document": {"id": "1", "title": "Piso moneda 3mm", "rubro": "pisos", "in_stock": True,
                              "floor_design": "moneda", "espesor_mm": 3, "ancho_m": 1.0, "content": "goma"},
                 "vector_distance": 0.1},
                {"document": {"id": "2", "title": "Piso moneda 2mm", "rubro": "pisos", "in_stock": True,
                              "floor_design": "moneda", "espesor_mm": 2, "ancho_m": 1.0, "content": "goma"},
                 "vector_distance": 0.3},
            ],
        }
        engine = TypesenseCatalogSearch(client=FakeClient(payload), embedder=None, collection="catalog_products")
        request = SearchRequest(query="piso moneda 3mm", filters=ProductFilters(rubro="pisos", floor_design="moneda", espesor_mm=3), limit=5)
        response = engine.search(request)

        self.assertEqual(len(response.hits), 2)
        self.assertFalse(response.used_relaxation)
        self.assertFalse(response.hits[0].is_alternative)  # exact espesor 3
        self.assertTrue(response.hits[1].is_alternative)   # espesor 2 -> alternative

    def test_hybrid_search_uses_multi_search_post_body(self) -> None:
        payload = {
            "found": 1,
            "hits": [
                {"document": {"id": "1", "title": "Piso moneda 3mm", "rubro": "pisos", "in_stock": True,
                              "floor_design": "moneda", "espesor_mm": 3, "ancho_m": 1.0, "content": "goma"},
                 "vector_distance": 0.1},
            ],
        }
        client = FakeHybridClient(payload)
        engine = TypesenseCatalogSearch(client=client, embedder=FakeEmbedder(), collection="catalog_products")
        request = SearchRequest(query="piso moneda 3mm", filters=ProductFilters(rubro="pisos"), limit=5)

        response = engine.search(request)

        self.assertEqual(len(response.hits), 1)
        search = client.multi_search.last_search_queries["searches"][0]
        self.assertEqual(search["collection"], "catalog_products")
        self.assertIn("vector_query", search)
        self.assertEqual(client.multi_search.last_common_params, {})


if __name__ == "__main__":
    unittest.main()
