from __future__ import annotations

import unittest

from app.catalog.product_links import extract_product_slugs, product_link_matches_slug
from app.core.models import ProductDocument, ProductFilters, ProductSpecs, SearchRequest
from app.search.retrieval import CatalogSearch


class ProductLinkTests(unittest.TestCase):
    def test_extract_product_slugs_from_odranid_urls(self) -> None:
        self.assertEqual(
            extract_product_slugs(
                "Vengo de https://odranid.com.ar/producto/piso-web-3mm/ y quisiera saber"
            ),
            ["piso-web-3mm"],
        )

    def test_product_link_matches_slug_ignores_domain_variant(self) -> None:
        self.assertTrue(product_link_matches_slug("https://odranid.com/producto/piso-web-3mm/", "piso-web-3mm"))

    def test_local_search_resolves_product_slug_exactly(self) -> None:
        product = ProductDocument(
            id=1,
            title="Piso Web 3mm",
            slug="piso-web-3mm",
            link="https://odranid.com.ar/producto/piso-web-3mm/",
            rubro="pisos",
            specs=ProductSpecs(espesor_mm=3),
            content="Piso web",
        )
        engine = CatalogSearch([product])

        response = engine.search(
            SearchRequest(query="piso web", filters=ProductFilters(product_slug="piso-web-3mm"), limit=5)
        )

        self.assertEqual([hit.product.id for hit in response.hits], [1])


if __name__ == "__main__":
    unittest.main()
