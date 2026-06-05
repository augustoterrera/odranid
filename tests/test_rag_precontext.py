from __future__ import annotations

import unittest

from app.core.models import AgentMessage, AgentRequest, ProductIntakeResponse
from app.rag_precontext import build_rag_precontext


class RagPrecontextTests(unittest.TestCase):
    def test_build_rag_precontext_includes_intake_and_conversation(self) -> None:
        # The precontext is intake + conversation only — it no longer injects
        # pre-searched candidates (LLM-only pipeline: the CatalogAgent calls the
        # buscar_productos tool itself).
        context = build_rag_precontext(
            request=AgentRequest(
                message="2 y 2",
                history=[
                    AgentMessage(role="user", content="Estoy buscando pisos con diseño para cubrir 7m2"),
                    AgentMessage(role="assistant", content="¿Qué espesor y ancho buscás? Por ejemplo: 3 mm y 1,20 m."),
                ],
            ),
            search_query="Estoy buscando pisos con diseño para cubrir 7m2",
            intake=ProductIntakeResponse(
                intent="pisos",
                known={"rubro": "pisos", "floor_kind": "diseno", "requested_m2": 7},
                missing=["espesor_mm", "ancho_m"],
                should_search=True,
                next_question="¿Qué espesor y ancho buscás?",
            ),
        )

        self.assertIn("PRECONTEXTO RAG DE LA CONVERSACION", context)
        self.assertIn("recent_conversation", context)
        self.assertIn("¿Qué espesor y ancho buscás?", context)
        self.assertIn('"intent": "pisos"', context)
        self.assertIn('"requested_m2": 7', context)
        self.assertIn('"confidence":', context)
        self.assertIn("buscar_productos", context)


if __name__ == "__main__":
    unittest.main()
