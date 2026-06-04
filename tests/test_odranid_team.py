from __future__ import annotations

import unittest

from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import ToolDefinition

from app.agents.pydantic_agent import run_pydantic_agent
from app.models import AgentRequest, ProductDocument, ProductSpecs, SearchHit, SearchRequest, SearchResponse


class PydanticAgentCutoverTests(unittest.TestCase):
    def test_agent_returns_next_question_without_search(self) -> None:
        response = run_pydantic_agent(
            request=AgentRequest(message="piso moneda"),
            search=self._fake_search,
            api_key="sk-test",
            catalog_context="CATALOGO",
            pydantic_model=TestModel(
                call_tools=[],
                custom_output_args={
                    "intake": {
                        "intent": "pisos",
                        "known": {"rubro": "pisos", "floor_design": "moneda"},
                        "missing": ["espesor_mm", "ancho_m"],
                        "should_search": False,
                        "next_question": "¿Qué espesor y ancho buscás?",
                    },
                    "answer": "¿Qué espesor y ancho buscás?",
                },
            ),
        )

        self.assertEqual(response.answer, "¿Qué espesor y ancho buscás?")
        self.assertEqual(response.tool_calls, [])
        self.assertIsNotNone(response.intake)
        self.assertFalse(response.intake.should_search)

    def test_agent_calls_search_for_searchable_intake(self) -> None:
        seen_requests: list[SearchRequest] = []

        def fake_search(request: SearchRequest) -> SearchResponse:
            seen_requests.append(request)
            return SearchResponse(
                query=request.query,
                total_catalog_size=1,
                hits=[
                    SearchHit(
                        product=ProductDocument(
                            id=1,
                            title="Piso moneda 3mm",
                            link="https://odranid.com/producto/piso-moneda-3mm/",
                            rubro="pisos",
                            specs=ProductSpecs(espesor_mm=3, ancho_m=1.2),
                            content="",
                        ),
                        score=0.9,
                    )
                ],
            )

        response = run_pydantic_agent(
            request=AgentRequest(message="piso moneda 3mm ancho 1.20 para cubrir 30m2"),
            search=fake_search,
            api_key="sk-test",
            catalog_context="CATALOGO",
            pydantic_model=SearchableIntakeModel(),
        )

        self.assertEqual(response.answer, "Te muestro opciones.")
        self.assertEqual(response.tool_calls[0].result_count, 1)
        self.assertEqual(seen_requests[0].filters.floor_design, "moneda")
        self.assertEqual(seen_requests[0].filters.espesor_mm, 3)
        self.assertEqual(seen_requests[0].filters.ancho_m, 1.2)

    @staticmethod
    def _fake_search(request: SearchRequest) -> SearchResponse:
        return SearchResponse(query=request.query, total_catalog_size=0, hits=[])


class SearchableIntakeModel(TestModel):
    def __init__(self) -> None:
        super().__init__(
            call_tools=["buscar_productos"],
            custom_output_args={
                "intake": {
                    "intent": "pisos",
                    "known": {
                        "rubro": "pisos",
                        "floor_kind": "diseno",
                        "floor_design": "moneda",
                        "espesor_mm": 3,
                        "ancho_m": 1.2,
                        "requested_m2": 30,
                    },
                    "missing": [],
                    "should_search": True,
                },
                "answer": "Te muestro opciones.",
            },
        )

    def gen_tool_args(self, tool_def: ToolDefinition) -> object:
        if tool_def.name == "buscar_productos":
            return {
                "query_semantica": "piso moneda goma 3mm ancho 1.20 cubrir 30m2",
                "rubro": "pisos",
                "tipo": "moneda",
                "espesor_mm": 3,
                "ancho_m": 1.2,
                "requested_m2": 30,
                "limit": 5,
            }
        return super().gen_tool_args(tool_def)


if __name__ == "__main__":
    unittest.main()
