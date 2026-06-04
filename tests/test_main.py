from __future__ import annotations

import unittest
from unittest.mock import patch

from app.models import AgentMessage, AgentRequest, AgentResponse, ProductIntakeResponse
from app import main
from app.main import clean_agent_search_query
from app.main import search_query_from_agent_request


class MainFlowTests(unittest.TestCase):
    def test_clean_agent_search_query_removes_store_boilerplate_and_urls(self) -> None:
        query = clean_agent_search_query(
            "Hola *Odranid*! Vengo de la tienda online Kit Combo "
            "https://odranid.com/producto/kit-combo/ y quisiera saber Pitbull grande mordedor pelota"
        )

        self.assertEqual(query, "Kit Combo Pitbull grande mordedor pelota")

    def test_search_query_prefers_structured_memory_over_old_history(self) -> None:
        query = search_query_from_agent_request(
            AgentRequest(
                message="Mas resistente",
                history=[
                    AgentMessage(
                        role="user",
                        content="Vengo de la tienda online Kit Combo Juguetes De Goma Perros Chicos Mascotas",
                    ),
                    AgentMessage(
                        role="user",
                        content="Datos ya recopilados: juguete para mascota perro tamaño grande tipo pelota resistente",
                    ),
                ],
            )
        )

        self.assertEqual(query, "Datos ya recopilados: juguete para mascota perro tamaño grande tipo pelota resistente Mas resistente")
        self.assertNotIn("Chicos", query)

    def test_current_catalog_context_for_request_includes_rag_precontext_and_recent_history(self) -> None:
        request = AgentRequest(
            message="2 y 2",
            history=[
                AgentMessage(role="user", content="Estoy buscando pisos con diseño para cubrir 7m2"),
                AgentMessage(role="assistant", content="¿Qué espesor y ancho buscás? Por ejemplo: 3 mm y 1,20 m."),
            ],
        )

        # LLM-only pipeline: the intake comes from the RequirementsAgent. We mock it
        # with a fixed ProductIntakeResponse so the test stays deterministic and offline.
        fake_intake = ProductIntakeResponse(
            intent="pisos",
            known={"rubro": "pisos", "floor_kind": "diseno", "espesor_mm": 2, "ancho_m": 2, "requested_m2": 7},
            missing=[],
            should_search=True,
        )

        with patch.object(main, "current_catalog_context", return_value="CATALOGO"), patch.object(
            main, "get_product_intake", return_value=fake_intake
        ):
            context = main.current_catalog_context_for_request(request)

        self.assertIn("CATALOGO", context)
        self.assertIn("PRECONTEXTO RAG DE LA CONVERSACION", context)
        self.assertIn("¿Qué espesor y ancho buscás?", context)
        self.assertIn('"espesor_mm": 2', context)
        self.assertIn('"ancho_m": 2', context)
        self.assertIn("buscar_productos", context)

    def test_run_agent_uses_odranid_team_with_precontext_when_configured(self) -> None:
        seen: dict[str, object] = {}

        def fake_run_team(request, search, api_key, context_builder, model, prompt_file):
            seen["request"] = request
            seen["context_builder"] = context_builder
            seen["search"] = search
            seen["api_key"] = api_key
            seen["model"] = model
            seen["prompt_file"] = prompt_file
            return AgentResponse(answer="respuesta del agente")

        with patch.object(main.settings, "openai_api_key", "sk-test"), patch(
            "app.agents.odranid_team.run_team", side_effect=fake_run_team
        ):
            response = main.run_agent(AgentRequest(message="Estoy buscando pisos con diseño para cubrir 7m2"))

        self.assertEqual(response.answer, "respuesta del agente")
        self.assertEqual(seen["api_key"], "sk-test")
        self.assertTrue(callable(seen["context_builder"]))
        self.assertIs(seen["search"], main.perform_search)

    def test_run_agent_requires_openai_key(self) -> None:
        # LLM-only pipeline: no deterministic keyword fallback. Without a key the
        # agent endpoint returns 503 instead of degrading to keyword matching.
        from fastapi import HTTPException

        with patch.object(main.settings, "openai_api_key", None):
            with self.assertRaises(HTTPException) as ctx:
                main.run_agent(AgentRequest(message="Estoy buscando pisos con diseño para cubrir 7m2"))

        self.assertEqual(ctx.exception.status_code, 503)


if __name__ == "__main__":
    unittest.main()
