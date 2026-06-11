from __future__ import annotations

import unittest

from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import ToolDefinition

from app.agents.pydantic_agent import product_summary_line, run_pydantic_agent
from app.core.models import AgentRequest, CoverageCalculation, ProductDocument, ProductSpecs, SearchHit, SearchRequest, SearchResponse


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

    def test_product_summary_labels_hose_mm_as_diameter(self) -> None:
        line = product_summary_line(
            1,
            SearchHit(
                product=ProductDocument(
                    id=1,
                    title="Manguera aire roja 8mm",
                    rubro="mangueras",
                    specs=ProductSpecs(espesor_mm=8, largo_manguera_m=15),
                    content="Manguera aire roja",
                ),
                score=1.0,
            ),
        )

        self.assertIn("Diámetro 8mm", line)
        self.assertNotIn("Espesor 8mm", line)

    def test_pickup_today_answer_gets_address_and_advisor_without_promise(self) -> None:
        def fake_search(request: SearchRequest) -> SearchResponse:
            return SearchResponse(
                query=request.query,
                total_catalog_size=1,
                hits=[
                    SearchHit(
                        product=ProductDocument(
                            id=1,
                            title="Manguera aire roja 8mm",
                            link="https://odranid.com/producto/manguera-aire-roja-8mm/",
                            rubro="mangueras",
                            specs=ProductSpecs(espesor_mm=8, largo_manguera_m=15),
                            content="Manguera aire roja",
                        ),
                        score=1.0,
                    )
                ],
            )

        response = run_pydantic_agent(
            request=AgentRequest(
                message="buenas tardes, necesito 1 rollo de 15 metros de manguera de aire roja de 8mm, puedo retirar hoy?",
                limit=5,
            ),
            search=fake_search,
            api_key="sk-test",
            catalog_context="CATALOGO",
            pydantic_model=ControlledTestModel(
                tool_args={
                    "query_semantica": "manguera aire roja 8mm 15m",
                    "rubro": "mangueras",
                },
                output_args={
                    "intake": {"intent": "mangueras", "known": {"rubro": "mangueras"}, "should_search": True},
                    "answer": "\n".join(
                        [
                            "Manguera aire roja 8mm",
                            "🔗 https://odranid.com/producto/manguera-aire-roja-8mm/",
                            "Podés retirar hoy.",
                        ]
                    ),
                },
            ),
        )

        self.assertTrue(response.answer.startswith("Buenas tardes. Sí, tenemos esta opción:"))
        self.assertIn("Manguera aire roja 8mm", response.answer)
        self.assertIn("https://wa.me/5491125539459", response.answer)
        self.assertIn("Av. Suárez 2737", response.answer)
        self.assertIn("8 a 16 hs", response.answer)
        self.assertNotIn("Podés retirar hoy.", response.answer)
        self.assertNotIn("1. Manguera", response.answer)

    def test_agent_preloads_product_from_web_link(self) -> None:
        seen_requests: list[SearchRequest] = []

        def fake_search(request: SearchRequest) -> SearchResponse:
            seen_requests.append(request)
            return SearchResponse(
                query=request.query,
                total_catalog_size=1,
                hits=[
                    SearchHit(
                        product=ProductDocument(
                            id=10,
                            title="Piso Web Semilla Melon 3mm",
                            slug="piso-web-semilla-melon-3mm",
                            link="https://odranid.com/producto/piso-web-semilla-melon-3mm/",
                            rubro="pisos",
                            floor_kind="diseno",
                            floor_design="semilla_melon",
                            material="goma",
                            specs=ProductSpecs(espesor_mm=3, ancho_m=1.4, largo_m=10),
                            content="Piso web semilla melon",
                        ),
                        score=1.0,
                    )
                ],
            )

        model = InspectPromptModel(
            output_args={
                "intake": {"intent": None, "known": {}, "should_search": False},
                "answer": "Piso Web Semilla Melon 3mm\n🔗 https://odranid.com/producto/piso-web-semilla-melon-3mm/",
            }
        )

        response = run_pydantic_agent(
            request=AgentRequest(
                message=(
                    "Hola Odranid! Vengo de la tienda online Piso Web "
                    "https://odranid.com.ar/producto/piso-web-semilla-melon-3mm/ "
                    "y quisiera saber si hacen envío"
                ),
                limit=5,
            ),
            search=fake_search,
            api_key="sk-test",
            catalog_context="CATALOGO",
            pydantic_model=model,
        )

        self.assertEqual(len(seen_requests), 1)
        self.assertEqual(seen_requests[0].filters.product_slug, "piso-web-semilla-melon-3mm")
        self.assertIn("linked_products_from_web", model.messages_text)
        self.assertIn("Piso Web Semilla Melon 3mm", response.answer)
        self.assertIn("🔗 https://odranid.com.ar/producto/piso-web-semilla-melon-3mm/", response.answer)
        self.assertEqual(response.tool_calls, [])

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

    def test_agent_repairs_orphan_product_link_with_real_title(self) -> None:
        def fake_search(request: SearchRequest) -> SearchResponse:
            return SearchResponse(
                query=request.query,
                total_catalog_size=1,
                requested_m2=15,
                hits=[
                    SearchHit(
                        product=ProductDocument(
                            id=1,
                            title="Combo Piso Moneda Gris Simil Goma 15m2 + Adhesivo!!",
                            link="https://odranid.com/producto/combo-piso-moneda-gris-simil-goma-15m2-adhesivo/",
                            rubro="pisos",
                            floor_kind="diseno",
                            floor_design="moneda",
                            material="PVC",
                            specs=ProductSpecs(espesor_mm=1.2, ancho_m=1.5, largo_m=10, rendimiento_m2=15),
                            content="Piso moneda gris",
                        ),
                        score=0.9,
                        coverage=CoverageCalculation(
                            requested_m2=15,
                            sale_unit="rollo",
                            coverage_m2=15,
                            rolls_needed=1,
                            message="Para cubrir 15 m2, recomendar 1 rollo de este producto.",
                        ),
                    )
                ],
            )

        response = run_pydantic_agent(
            request=AgentRequest(message="Necesito piso moneda para cubrir 15m2", limit=5),
            search=fake_search,
            api_key="sk-test",
            catalog_context="CATALOGO",
            pydantic_model=ControlledTestModel(
                tool_args={
                    "query_semantica": "piso moneda cubrir 15m2",
                    "rubro": "pisos",
                    "tipo": "moneda",
                    "requested_m2": 15,
                },
                output_args={
                    "intake": {"intent": "pisos", "known": {"rubro": "pisos"}, "should_search": True},
                    "answer": "\n".join(
                        [
                            "Te muestro opciones disponibles:",
                            "",
                            "1. Combo Piso Moneda Gris",
                            "🔗 https://odranid.com/producto/combo-piso-moneda-gris-simil-goma-15m2-adhesivo/",
                        ]
                    ),
                },
            ),
        )

        self.assertIn("1. Combo Piso Moneda Gris Simil Goma 15m2 + Adhesivo!!", response.answer)
        self.assertIn("PVC • Con diseño • Moneda • Espesor 1.2mm", response.answer)
        self.assertIn("Rollo 10m x 1.5m (15 m²)", response.answer)
        self.assertIn("Necesitás 1 rollo", response.answer)
        self.assertIn("🔗 https://odranid.com.ar/producto/combo-piso-moneda-gris-simil-goma-15m2-adhesivo/", response.answer)

    def test_agent_reruns_forcing_search_when_should_search_but_no_tool_call(self) -> None:
        # Invariante: should_search=true exige una llamada a buscar_productos. Si el modelo
        # narra la búsqueda como texto (en primera persona) sin llamar la herramienta, el turno
        # es inválido y debe re-ejecutarse forzando la búsqueda.
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
                            title="Piso liso 2mm",
                            link="https://odranid.com/producto/piso-liso-2mm/",
                            rubro="pisos",
                            floor_kind="liso",
                            specs=ProductSpecs(espesor_mm=2, ancho_m=1, largo_m=15),
                            product_type="rollo",
                            content="Piso liso",
                        ),
                        score=0.9,
                    )
                ],
            )

        response = run_pydantic_agent(
            request=AgentRequest(message="2", limit=5),
            search=fake_search,
            api_key="sk-test",
            catalog_context="CATALOGO",
            pydantic_model=ForceSearchRetryModel(
                tool_args={
                    "query_semantica": "piso liso 2mm ancho 2m cubrir 12m2 gimnasio",
                    "rubro": "pisos",
                    "tipo": "liso",
                    "espesor_mm": 2,
                    "ancho_m": 2,
                    "requested_m2": 12,
                },
                narrated_answer="Busco pisos liso 2 mm de espesor, 2 m de ancho para cubrir 12 m2 en gimnasio.",
                searched_answer="Te muestro estas opciones reales para tu gimnasio.",
            ),
        )

        # No se filtró la query narrada: la respuesta final viene de la búsqueda forzada.
        self.assertEqual(response.answer, "Te muestro estas opciones reales para tu gimnasio.")
        self.assertNotIn("Busco pisos liso", response.answer)
        self.assertEqual(len(response.tool_calls), 1)
        self.assertEqual(response.tool_calls[0].name, "buscar_productos")
        self.assertEqual(len(seen_requests), 1)

    def test_agent_reruns_forcing_presentation_when_recommendation_hides_products(self) -> None:
        # En modo recomendación el agente buscó y trajo productos, pero respondió pidiendo el ancho
        # en vez de mostrarlos. El guard debe re-ejecutar forzando la presentación.
        def fake_search(request: SearchRequest) -> SearchResponse:
            return SearchResponse(
                query=request.query,
                total_catalog_size=1,
                hits=[
                    SearchHit(
                        product=ProductDocument(
                            id=1,
                            title="Piso Vinilico Madera",
                            link="https://odranid.com/producto/piso-vinilico-madera/",
                            rubro="pisos",
                            category="pisos_vinilicos",
                            floor_kind="diseno",
                            floor_design="simil_madera",
                            material="PVC",
                            specs=ProductSpecs(espesor_mm=1.2, ancho_m=2, largo_m=5),
                            content="Piso vinilico simil madera",
                        ),
                        score=0.9,
                    )
                ],
            )

        response = run_pydantic_agent(
            request=AgentRequest(message="Que me recomendas para mi oficina de 12m2", limit=5),
            search=fake_search,
            api_key="sk-test",
            catalog_context="CATALOGO",
            pydantic_model=ForcePresentRetryModel(
                tool_args={"query_semantica": "piso vinilico simil madera oficina cubrir 12m2", "rubro": "pisos"},
                hidden_answer="Para tu oficina te recomiendo simil madera 2mm. ¿Qué ancho preferís?",
                presented_answer="\n".join(
                    [
                        "Para tu oficina te recomiendo estas opciones:",
                        "",
                        "1. Piso Vinilico Madera • PVC • Con diseño • Símil madera • Espesor 1.2mm",
                        "🔗 https://odranid.com/producto/piso-vinilico-madera/",
                    ]
                ),
            ),
        )

        self.assertIn("Piso Vinilico Madera", response.answer)
        self.assertIn("🔗 https://odranid.com.ar/producto/piso-vinilico-madera/", response.answer)
        self.assertNotIn("¿Qué ancho preferís?", response.answer)

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


class InspectPromptModel(TestModel):
    def __init__(self, *, output_args: dict[str, object]) -> None:
        super().__init__(call_tools=[], custom_output_args=output_args)
        self.messages_text = ""

    async def request(self, messages, model_settings, model_request_parameters):  # type: ignore[override]
        self.messages_text = str(messages)
        return await super().request(messages, model_settings, model_request_parameters)


class ForceSearchRetryModel(TestModel):
    """First run claims should_search but calls no tool; the forced re-run calls the tool.

    The forced re-run is detected by the `force_search` marker that ``build_user_prompt``
    appends to the user prompt.
    """

    _FORCE_MARKER = "ya tenés datos suficientes para buscar"

    def __init__(self, *, tool_args: dict[str, object], narrated_answer: str, searched_answer: str) -> None:
        super().__init__(call_tools=[], custom_output_args=self._intake_output(narrated_answer))
        self.tool_args = tool_args
        self.narrated_answer = narrated_answer
        self.searched_answer = searched_answer

    @staticmethod
    def _intake_output(answer: str) -> dict[str, object]:
        return {
            "intake": {"intent": "pisos", "known": {"rubro": "pisos"}, "should_search": True},
            "answer": answer,
        }

    async def request(self, messages, model_settings, model_request_parameters):  # type: ignore[override]
        if self._FORCE_MARKER in str(messages):
            self.call_tools = ["buscar_productos"]
            self.custom_output_args = self._intake_output(self.searched_answer)
        return await super().request(messages, model_settings, model_request_parameters)

    def gen_tool_args(self, tool_def: ToolDefinition) -> object:
        if tool_def.name == "buscar_productos":
            return self.tool_args
        return super().gen_tool_args(tool_def)


class ForcePresentRetryModel(TestModel):
    """Recommendation turn: first run searches (hits) but answers a question without showing
    products; the forced re-run presents them. The force_present re-run is detected by the marker
    that ``build_user_prompt`` appends.
    """

    _FORCE_MARKER = "PRESENTÁ las opciones AHORA"

    def __init__(self, *, tool_args: dict[str, object], hidden_answer: str, presented_answer: str) -> None:
        super().__init__(call_tools=["buscar_productos"], custom_output_args=self._output(hidden_answer))
        self.tool_args = tool_args
        self.hidden_answer = hidden_answer
        self.presented_answer = presented_answer

    @staticmethod
    def _output(answer: str) -> dict[str, object]:
        return {
            "intake": {
                "intent": "pisos",
                "known": {"rubro": "pisos", "recommendation": True},
                "should_search": True,
            },
            "answer": answer,
        }

    async def request(self, messages, model_settings, model_request_parameters):  # type: ignore[override]
        if self._FORCE_MARKER in str(messages):
            self.custom_output_args = self._output(self.presented_answer)
        return await super().request(messages, model_settings, model_request_parameters)

    def gen_tool_args(self, tool_def: ToolDefinition) -> object:
        if tool_def.name == "buscar_productos":
            return self.tool_args
        return super().gen_tool_args(tool_def)


class IntakeInvariantTests(unittest.TestCase):
    def test_should_search_is_true_when_tool_was_called(self) -> None:
        """El LLM a veces busca y devuelve should_search=false; el código lo corrige."""

        def fake_search(request: SearchRequest) -> SearchResponse:
            return SearchResponse(
                query=request.query,
                total_catalog_size=1,
                hits=[
                    SearchHit(
                        product=ProductDocument(
                            id=1,
                            title="Bota De Goma Calfor",
                            link="https://odranid.com.ar/producto/bota-de-goma-calfor/",
                            rubro="calzado",
                            content="Bota de goma",
                        ),
                        score=0.9,
                    )
                ],
            )

        response = run_pydantic_agent(
            request=AgentRequest(message="venden botas de goma?", limit=5),
            search=fake_search,
            api_key="sk-test",
            catalog_context="CATALOGO",
            pydantic_model=ControlledTestModel(
                tool_args={"query_semantica": "botas de goma", "rubro": "calzado"},
                output_args={
                    "intake": {
                        "intent": "calzado",
                        "known": {"rubro": "calzado"},
                        "missing": [],
                        # Inconsistencia del LLM: buscó pero dice que no.
                        "should_search": False,
                        "next_question": None,
                        "confidence": 0.9,
                    },
                    "answer": "Te muestro estas opciones: Bota De Goma Calfor",
                },
            ),
        )

        self.assertEqual(response.tool_calls[0].name, "buscar_productos")
        self.assertTrue(response.intake.should_search)


class InferFloorKindTests(unittest.TestCase):
    def test_liso_infers_liso(self) -> None:
        from app.search.search_common import infer_floor_kind

        self.assertEqual(infer_floor_kind("busco piso liso de goma 2mm"), "liso")
        self.assertEqual(infer_floor_kind("pisos lisos para oficina"), "liso")

    def test_design_terms_infer_diseno(self) -> None:
        from app.search.search_common import infer_floor_kind

        self.assertEqual(infer_floor_kind("piso moneda para rampa"), "diseno")
        self.assertEqual(infer_floor_kind("algo antideslizante para la entrada"), "diseno")

    def test_ambiguous_or_indifferent_infers_nothing(self) -> None:
        from app.search.search_common import infer_floor_kind

        self.assertIsNone(infer_floor_kind("liso o con diseño, lo que tengas"))
        self.assertIsNone(infer_floor_kind("el diseño no importa"))
        self.assertIsNone(infer_floor_kind("cualquier diseño me sirve"))
        self.assertIsNone(infer_floor_kind("piso de goma 2mm"))


class CoverageSortTests(unittest.TestCase):
    def _hit(self, title: str, *, rolls: int | None, source: str = "rollo", is_alternative: bool = False) -> SearchHit:
        return SearchHit(
            product=ProductDocument(id=hash(title) % 1000, title=title, rubro="pisos", content=title),
            score=0.9,
            is_alternative=is_alternative,
            coverage=CoverageCalculation(
                requested_m2=60,
                sale_unit="rollo",
                coverage_source=source,
                rolls_needed=rolls,
                message="x",
            ),
        )

    def test_fewer_rolls_first_for_large_surfaces(self) -> None:
        from app.agents.pydantic_agent import sort_hits_by_coverage

        response = SearchResponse(
            query="cubrir 60 m2",
            total_catalog_size=3,
            requested_m2=60,
            hits=[
                self._hit("Rollo chico 6m2", rolls=10),
                self._hit("Rollo grande 15m2", rolls=4),
                self._hit("Rollo mediano 12m2", rolls=5),
            ],
        )

        sort_hits_by_coverage(response)

        self.assertEqual(
            [hit.product.title for hit in response.hits],
            ["Rollo grande 15m2", "Rollo mediano 12m2", "Rollo chico 6m2"],
        )

    def test_corte_a_medida_counts_as_single_piece_and_exact_beats_alternative(self) -> None:
        from app.agents.pydantic_agent import sort_hits_by_coverage

        response = SearchResponse(
            query="cubrir 60 m2",
            total_catalog_size=3,
            requested_m2=60,
            hits=[
                self._hit("Alternativa 1 rollo", rolls=1, is_alternative=True),
                self._hit("Rollo grande", rolls=4),
                self._hit("Corte a medida", rolls=None, source="corte_a_medida"),
            ],
        )

        sort_hits_by_coverage(response)

        self.assertEqual(
            [hit.product.title for hit in response.hits],
            ["Corte a medida", "Rollo grande", "Alternativa 1 rollo"],
        )


class GuardRenumberTests(unittest.TestCase):
    def _response_with_products(self) -> SearchResponse:
        def product(idx: int, title: str, slug: str) -> SearchHit:
            return SearchHit(
                product=ProductDocument(
                    id=idx,
                    title=title,
                    link=f"https://odranid.com.ar/producto/{slug}/",
                    rubro="pisos",
                    content=title,
                ),
                score=0.9,
            )

        return SearchResponse(
            query="pisos",
            total_catalog_size=3,
            hits=[product(1, "Piso Moneda 3mm", "piso-moneda-3mm"), product(2, "Piso Liso 2mm", "piso-liso-2mm")],
        )

    def test_guard_renumbers_after_discarding_hallucinated_product(self) -> None:
        from app.agents.pydantic_agent import guard_agent_answer

        answer = "\n".join(
            [
                "Te muestro estas opciones:",
                "1. Piso Moneda 3mm • Espesor 3mm",
                "🔗 https://odranid.com.ar/producto/piso-moneda-3mm/",
                "2. Piso Inventado Premium • Espesor 9mm",
                "3. Piso Liso 2mm • Espesor 2mm",
                "🔗 https://odranid.com.ar/producto/piso-liso-2mm/",
                "¿Cuál te interesa?",
            ]
        )

        guarded = guard_agent_answer(answer, [self._response_with_products()])

        self.assertNotIn("Inventado", guarded)
        self.assertIn("1. Piso Moneda 3mm", guarded)
        self.assertIn("2. Piso Liso 2mm", guarded)
        self.assertNotIn("3.", guarded)


class LinkedProductSlugTests(unittest.TestCase):
    def test_slugs_from_current_message_have_priority_over_history(self) -> None:
        from app.agents.pydantic_agent import linked_product_slugs
        from app.core.models import AgentMessage

        request = AgentRequest(
            message="me interesa https://odranid.com.ar/producto/piso-liso-2mm/",
            history=[
                AgentMessage(role="user", content="mira https://odranid.com.ar/producto/piso-moneda-3mm/"),
                AgentMessage(role="assistant", content="¡Buenísimo!"),
            ],
        )

        self.assertEqual(linked_product_slugs(request), ["piso-liso-2mm", "piso-moneda-3mm"])

    def test_follow_up_without_link_recovers_slug_from_history(self) -> None:
        from app.agents.pydantic_agent import linked_product_slugs
        from app.core.models import AgentMessage

        request = AgentRequest(
            message="y ese cuánto rinde?",
            history=[
                AgentMessage(role="user", content="vengo de https://odranid.com.ar/producto/piso-moneda-3mm/"),
                AgentMessage(role="assistant", content="Por el piso moneda..."),
            ],
        )

        self.assertEqual(linked_product_slugs(request), ["piso-moneda-3mm"])


if __name__ == "__main__":
    unittest.main()
