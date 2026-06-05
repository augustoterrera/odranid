from __future__ import annotations

import unittest

from app.search.db_search import DatabaseCatalogSearch
from app.core.models import ProductDocument, ProductSpecs, SearchHit
from app.search.search_common import post_filter_specific_terms


class DbSearchTests(unittest.TestCase):
    def test_tejo_query_keeps_only_tejo_hits_when_available(self) -> None:
        tejo = product(1, "Tejo De Goma Chico + Frisbee")
        regaton = product(2, "Regatones De Goma De 16mm Pack X 100")
        hits = [
            SearchHit(product=regaton, score=0.9),
            SearchHit(product=tejo, score=0.8),
        ]

        filtered = post_filter_specific_terms("tejos de goma", hits, 5)

        self.assertEqual([hit.product.id for hit in filtered], [1])

    def test_specific_filter_returns_empty_when_no_matching_hit_exists(self) -> None:
        regaton = product(2, "Regatones De Goma De 16mm Pack X 100")

        filtered = post_filter_specific_terms("tejos de goma", [SearchHit(product=regaton, score=0.9)], 5)

        self.assertEqual(filtered, [])

    def test_hose_query_keeps_exact_diameter_and_length_when_available(self) -> None:
        half = product(1, "Manguera Riego 1/2 X 20mts Anticolapso")
        three_quarter_50 = product(2, "Manguera Reforzada 3/4 Jardín 50m")
        exact = product(3, "Manguera De Riego Trenzada 3/4 Presión Premium X 20 Mts")
        hits = [
            SearchHit(product=half, score=0.95),
            SearchHit(product=three_quarter_50, score=0.9),
            SearchHit(product=exact, score=0.7),
        ]

        filtered = post_filter_specific_terms("manguera riego 3/4 de 20 mts", hits, 5)

        self.assertEqual([hit.product.id for hit in filtered], [3])

    def test_postgres_search_casts_function_arguments(self) -> None:
        constants = "\n".join(str(value) for value in DatabaseCatalogSearch._search_postgres.__code__.co_consts)

        self.assertIn("%(espesor_mm)s::numeric", constants)
        self.assertIn("%(ancho_m)s::numeric", constants)
        self.assertIn("%(limit)s::integer", constants)
        self.assertIn("%(tags)s::text[]", constants)


def product(product_id: int, title: str) -> ProductDocument:
    return ProductDocument(
        id=product_id,
        title=title,
        link=f"https://example.test/{product_id}",
        rubro="mascotas",
        category="juguetes",
        specs=ProductSpecs(),
        content=title,
    )


if __name__ == "__main__":
    unittest.main()
