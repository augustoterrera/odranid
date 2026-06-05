from __future__ import annotations

import unittest

from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import ToolDefinition

from app.agents.pydantic_agent import run_pydantic_agent
from app.core.models import AgentRequest, ProductDocument, ProductSpecs, SearchHit, SearchRequest, SearchResponse


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

    def test_requested_m2_is_extracted_from_message_when_llm_omits_it(self) -> None:
        # El LLM no emite requested_m2 en los tool_args, pero el cliente sí dijo "cubrir 15m2".
        # El fallback determinístico debe calcular cobertura igual (no quedar sin "Necesitás X rollos").
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
                            title="Piso semilla 3mm",
                            link="https://odranid.com/producto/piso-semilla-3mm/",
                            rubro="pisos",
                            floor_kind="diseno",
                            floor_design="semilla",
                            specs=ProductSpecs(espesor_mm=3, ancho_m=1.2, largo_m=10, rendimiento_m2=12),
                            product_type="rollo",
                            content="Piso semilla de goma",
                        ),
                        score=0.9,
                    )
                ],
            )

        response = run_pydantic_agent(
            request=AgentRequest(message="Busco piso de goma semilla 3mm y 1.20m, quiero cubrir 15m2", limit=5),
            search=fake_search,
            api_key="sk-test",
            catalog_context="CATALOGO",
            pydantic_model=ControlledTestModel(
                tool_args={
                    "query_semantica": "piso goma semilla 3mm ancho 1.20m",
                    "rubro": "pisos",
                    "tipo": "semilla",
                    "espesor_mm": 3,
                    "ancho_m": 1.2,
                    # NOTA: el LLM NO incluye requested_m2 a propósito.
                },
                output_args={
                    "intake": {"intent": "pisos", "known": {"rubro": "pisos"}, "should_search": True},
                    "answer": "Te muestro opciones.",
                },
            ),
        )

        # El trace refleja el m2 efectivo extraído del mensaje.
        self.assertEqual(response.tool_calls[0].arguments["requested_m2"], 15)
        # La query enviada al buscador lleva el "cubrir 15 m2".
        self.assertIn("15", seen_requests[0].query)

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

    def test_agent_removes_hallucinated_product_and_link_from_tool_backed_answer(self) -> None:
        def fake_search(request: SearchRequest) -> SearchResponse:
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
                            specs=ProductSpecs(espesor_mm=3, ancho_m=1),
                            content="Piso moneda de goma",
                        ),
                        score=0.9,
                    )
                ],
            )

        response = run_pydantic_agent(
            request=AgentRequest(message="Necesito piso moneda", limit=5),
            search=fake_search,
            api_key="sk-test",
            catalog_context="CATALOGO",
            pydantic_model=ControlledTestModel(
                tool_args={
                    "query_semantica": "piso moneda goma",
                    "rubro": "pisos",
                    "tipo": "moneda",
                },
                output_args={
                    "intake": {"intent": "pisos", "known": {"rubro": "pisos"}, "should_search": True},
                    "answer": "\n".join(
                        [
                            "Te muestro estas opciones:",
                            "",
                            "1. Piso moneda 3mm",
                            "🔗 [Ver producto](https://odranid.com/producto/piso-moneda-3mm/)",
                            "",
                            "2. Piso inventado premium",
                            "https://odranid.com.ar/producto/no-existe/",
                        ]
                    ),
                },
            ),
        )

        self.assertIn("Piso moneda 3mm", response.answer)
        self.assertIn("🔗 https://odranid.com.ar/producto/piso-moneda-3mm/", response.answer)
        self.assertNotIn("🔗 🔗", response.answer)
        self.assertNotIn("Piso inventado", response.answer)
        self.assertNotIn("no-existe", response.answer)

    def test_agent_keeps_fixed_advisor_link_in_tool_backed_answer(self) -> None:
        def fake_search(request: SearchRequest) -> SearchResponse:
            return SearchResponse(query=request.query, total_catalog_size=0, hits=[])

        response = run_pydantic_agent(
            request=AgentRequest(message="pago efectivo tiene descuento?", limit=5),
            search=fake_search,
            api_key="sk-test",
            catalog_context="CATALOGO",
            pydantic_model=ControlledTestModel(
                tool_args={"query_semantica": "consulta pago efectivo", "rubro": "general"},
                output_args={
                    "intake": {"intent": None, "known": {}, "should_search": False},
                    "answer": "Para compras en efectivo contactá al asesor: 🔗 https://wa.me/5491125539459",
                },
            ),
        )

        self.assertIn("🔗 https://wa.me/5491125539459", response.answer)
        self.assertNotIn("🔗 🔗", response.answer)


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
