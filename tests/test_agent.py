from __future__ import annotations

import json
import unittest

from agno.models.response import ToolExecution

from app.agent import compact_search_response, response_from_search_response
from app.agents.catalog_agent import extract_tool_traces
from app.models import AgentRequest, CoverageCalculation, ProductDocument, ProductSpecs, SearchHit, SearchRequest, SearchResponse
from app.tools.buscar_productos import make_buscar_productos_tool


class AgentTests(unittest.TestCase):
    def test_compact_search_response_excludes_price(self) -> None:
        response = SearchResponse(
            query="piso moneda",
            total_catalog_size=1,
            hits=[
                SearchHit(
                    product=ProductDocument(
                        id=1,
                        title="Piso moneda",
                        link="https://odranid.com.ar/producto/piso",
                        price=12345,
                        currency="ARS",
                        rubro="pisos",
                        specs=ProductSpecs(espesor_mm=3, ancho_m=1.2),
                        content="texto interno",
                    ),
                    score=0.9,
                )
            ],
        )

        compact = compact_search_response(response)
        product = compact["hits"][0]["product"]

        self.assertNotIn("price", product)
        self.assertNotIn("currency", product)
        self.assertEqual(product["title"], "Piso moneda")

    def test_compact_search_response_normalizes_legacy_product_domain(self) -> None:
        response = SearchResponse(
            query="piso moneda",
            total_catalog_size=1,
            hits=[
                SearchHit(
                    product=ProductDocument(
                        id=1,
                        title="Piso moneda",
                        link="https://odranid.com/producto/piso-moneda/",
                        rubro="pisos",
                        specs=ProductSpecs(),
                        content="",
                    ),
                    score=0.9,
                )
            ],
        )

        compact = compact_search_response(response)

        self.assertEqual(compact["hits"][0]["product"]["link"], "https://odranid.com.ar/producto/piso-moneda/")

    def test_buscar_productos_tool_uses_injected_search(self) -> None:
        def fake_search(request: SearchRequest) -> SearchResponse:
            self.assertEqual(request.query, "piso moneda")
            self.assertEqual(request.limit, 3)
            return SearchResponse(query=request.query, total_catalog_size=0, hits=[])

        buscar_productos = make_buscar_productos_tool(fake_search, default_limit=5, max_limit=5)
        payload = json.loads(buscar_productos.entrypoint(query="piso moneda", limit=3))

        self.assertEqual(payload["query"], "piso moneda")

    def test_buscar_productos_tool_caps_model_limit_to_request_limit(self) -> None:
        def fake_search(request: SearchRequest) -> SearchResponse:
            self.assertEqual(request.limit, 3)
            return SearchResponse(query=request.query, total_catalog_size=0, hits=[])

        buscar_productos = make_buscar_productos_tool(fake_search, default_limit=3, max_limit=3)
        buscar_productos.entrypoint(query="piso moneda", limit=5)

    def test_extract_tool_traces_counts_json_string_result_hits(self) -> None:
        traces = extract_tool_traces(
            [
                ToolExecution(
                    tool_name="buscar_productos",
                    tool_args={"query": "piso moneda", "limit": 3},
                    result=json.dumps({"hits": [{"product": {"title": "Piso"}}]}),
                )
            ]
        )

        self.assertEqual(traces[0].name, "buscar_productos")
        self.assertEqual(traces[0].arguments["limit"], 3)
        self.assertEqual(traces[0].result_count, 1)

    def test_response_from_search_response_formats_products_without_prices(self) -> None:
        response = SearchResponse(
            query="piso moneda 3mm para cubrir 20m2",
            total_catalog_size=1,
            requested_m2=20,
            hits=[
                SearchHit(
                    product=ProductDocument(
                        id=1,
                        title="Piso moneda 3mm",
                        link="https://odranid.com.ar/producto/piso-moneda-3mm/",
                        price=9999,
                        currency="ARS",
                        rubro="pisos",
                        specs=ProductSpecs(espesor_mm=3, ancho_m=1.2),
                        content="",
                    ),
                    score=0.9,
                )
            ],
        )

        agent_response = response_from_search_response(response, limit=5)

        self.assertIn("Piso moneda 3mm", agent_response.answer)
        self.assertIn("https://odranid.com.ar/producto/piso-moneda-3mm/", agent_response.answer)
        self.assertIn("espesor 3 mm", agent_response.answer)
        self.assertNotIn("9999", agent_response.answer)
        self.assertEqual(agent_response.tool_calls[0].name, "buscar_productos")
        self.assertEqual(agent_response.tool_calls[0].result_count, 1)

    def test_response_from_search_response_limits_visible_products(self) -> None:
        response = SearchResponse(
            query="piso moneda",
            total_catalog_size=4,
            hits=[
                SearchHit(
                    product=ProductDocument(
                        id=index,
                        title=f"Piso opcion {index}",
                        link=f"https://odranid.com/producto/piso-{index}/",
                        rubro="pisos",
                        specs=ProductSpecs(),
                        content="",
                    ),
                    score=0.9,
                )
                for index in range(1, 5)
            ],
        )

        agent_response = response_from_search_response(response, limit=5)

        self.assertIn("Piso opcion 1", agent_response.answer)
        self.assertIn("Piso opcion 3", agent_response.answer)
        self.assertNotIn("Piso opcion 4", agent_response.answer)
        self.assertIn("https://odranid.com.ar/producto/piso-1/", agent_response.answer)
        self.assertIn("Tengo más opciones", agent_response.answer)

    def test_response_from_search_response_keeps_visible_coverage_units_together(self) -> None:
        response = SearchResponse(
            query="piso para cubrir 35m2",
            total_catalog_size=2,
            requested_m2=35,
            hits=[
                SearchHit(
                    product=ProductDocument(
                        id=1,
                        title="Piso x metro lineal",
                        link="https://odranid.com/producto/piso-lineal/",
                        rubro="pisos",
                        specs=ProductSpecs(ancho_m=1.4, largo_m=1),
                        content="",
                    ),
                    score=0.9,
                    coverage=CoverageCalculation(
                        requested_m2=35,
                        sale_unit="metro_lineal",
                        linear_meters_needed=25,
                        message="Con ancho de 1.4 m, para cubrir 35 m2 se necesitan aproximadamente 25 metros lineales.",
                    ),
                ),
                SearchHit(
                    product=ProductDocument(
                        id=2,
                        title="Piso rollo completo",
                        link="https://odranid.com/producto/piso-rollo/",
                        rubro="pisos",
                        specs=ProductSpecs(ancho_m=1, largo_m=20),
                        content="",
                    ),
                    score=0.8,
                    coverage=CoverageCalculation(
                        requested_m2=35,
                        sale_unit="rollo",
                        coverage_m2=20,
                        rolls_needed=2,
                        message="Para cubrir 35 m2, cada rollo cubre 20 m2. Se necesitan 2 rollos.",
                    ),
                ),
            ],
        )

        agent_response = response_from_search_response(response, limit=5)

        self.assertIn("Piso x metro lineal", agent_response.answer)
        self.assertNotIn("Piso rollo completo", agent_response.answer)
        self.assertNotIn("rollos", agent_response.answer)


if __name__ == "__main__":
    unittest.main()
