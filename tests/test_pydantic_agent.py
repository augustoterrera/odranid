from __future__ import annotations

import unittest

from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import ToolDefinition

from app.agents.pydantic_agent import run_pydantic_agent
from app.models import AgentRequest, ProductDocument, ProductSpecs, SearchHit, SearchRequest, SearchResponse


class PydanticAgentTests(unittest.TestCase):
    def test_agent_calls_typed_search_tool_and_returns_intake(self) -> None:
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
                            floor_kind="diseno",
                            floor_design="moneda",
                            specs=ProductSpecs(espesor_mm=3, ancho_m=1, largo_m=10),
                            product_type="rollo",
                            content="Piso moneda de goma",
                        ),
                        score=0.9,
                    )
                ],
            )

        response = run_pydantic_agent(
            request=AgentRequest(message="Necesito piso moneda 3mm ancho 1 para 20m2", limit=5),
            search=fake_search,
            api_key="sk-test",
            catalog_context="CATALOGO",
            pydantic_model=ControlledTestModel(
                tool_args={
                    "query_semantica": "piso moneda goma 3mm ancho 1m cubrir 20m2",
                    "rubro": "pisos",
                    "tipo": "moneda",
                    "espesor_mm": 3,
                    "ancho_m": 1,
                    "tags": ["antideslizante"],
                    "requested_m2": 20,
                    "limit": 5,
                },
                output_args={
                    "intake": {
                        "intent": "pisos",
                        "known": {
                            "rubro": "pisos",
                            "floor_kind": "diseno",
                            "floor_design": "moneda",
                            "espesor_mm": 3,
                            "ancho_m": 1,
                            "requested_m2": 20,
                        },
                        "missing": [],
                        "should_search": True,
                        "next_question": None,
                        "confidence": 0.9,
                    },
                    "answer": "Te muestro opciones reales.",
                },
            ),
        )

        self.assertEqual(response.answer, "Te muestro opciones reales.")
        self.assertIsNotNone(response.intake)
        self.assertEqual(response.intake.known["floor_design"], "moneda")
        self.assertEqual(response.tool_calls[0].name, "buscar_productos")
        self.assertEqual(response.tool_calls[0].result_count, 1)

        request = seen_requests[0]
        self.assertEqual(request.filters.rubro, "pisos")
        self.assertEqual(request.filters.floor_kind, "diseno")
        self.assertEqual(request.filters.floor_design, "moneda")
        self.assertEqual(request.filters.espesor_mm, 3)
        self.assertEqual(request.filters.ancho_m, 1)
        self.assertEqual(request.filters.tags, ["antideslizante"])
        self.assertIn("20", request.query)

    def test_agent_can_answer_next_question_without_search(self) -> None:
        response = run_pydantic_agent(
            request=AgentRequest(message="Quiero pisos lisos", limit=5),
            search=lambda request: SearchResponse(query=request.query, total_catalog_size=0, hits=[]),
            api_key="sk-test",
            catalog_context="CATALOGO",
            pydantic_model=TestModel(
                call_tools=[],
                custom_output_args={
                    "intake": {
                        "intent": "pisos",
                        "known": {"rubro": "pisos", "floor_kind": "liso"},
                        "missing": ["espesor_mm", "ancho_m", "requested_m2"],
                        "should_search": False,
                        "next_question": "¿Qué espesor, ancho y cuántos m2 querés cubrir?",
                        "confidence": 0.8,
                    },
                    "answer": "¿Qué espesor, ancho y cuántos m2 querés cubrir?",
                },
            ),
        )

        self.assertEqual(response.answer, "¿Qué espesor, ancho y cuántos m2 querés cubrir?")
        self.assertEqual(response.tool_calls, [])
        self.assertIsNotNone(response.intake)
        self.assertFalse(response.intake.should_search)
        self.assertEqual(response.intake.missing, ["espesor_mm", "ancho_m", "requested_m2"])


class ControlledTestModel(TestModel):
    def __init__(self, *, tool_args: dict[str, object], output_args: dict[str, object]) -> None:
        super().__init__(
            call_tools=["buscar_productos"],
            custom_output_args=output_args,
        )
        self.tool_args = tool_args

    def gen_tool_args(self, tool_def: ToolDefinition) -> object:
        if tool_def.name == "buscar_productos":
            return self.tool_args
        return super().gen_tool_args(tool_def)


if __name__ == "__main__":
    unittest.main()
