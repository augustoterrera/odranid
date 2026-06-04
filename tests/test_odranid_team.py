from __future__ import annotations

import unittest
from unittest.mock import patch

from app.agents.odranid_team import run_team
from app.models import AgentRequest, AgentResponse, ProductIntakeResponse, SearchRequest, SearchResponse


class OdranidTeamTests(unittest.TestCase):
    def test_run_team_returns_next_question_without_catalog_agent(self) -> None:
        intake = ProductIntakeResponse(
            intent="pisos",
            should_search=False,
            next_question="¿Qué espesor y ancho buscás?",
        )

        with patch("app.agents.odranid_team.analyze_requirements", return_value=intake), patch(
            "app.agents.odranid_team.respond_with_catalog"
        ) as respond_with_catalog:
            response = run_team(
                request=AgentRequest(message="piso moneda"),
                search=self._fake_search,
                api_key="sk-test",
                context_builder=lambda req, intk: "CATALOGO",
            )

        self.assertEqual(response.answer, "¿Qué espesor y ancho buscás?")
        respond_with_catalog.assert_not_called()

    def test_run_team_calls_catalog_agent_for_searchable_intake(self) -> None:
        intake = ProductIntakeResponse(intent="pisos", should_search=True)
        catalog_response = AgentResponse(answer="respuesta catalogo")

        with patch("app.agents.odranid_team.analyze_requirements", return_value=intake), patch(
            "app.agents.odranid_team.respond_with_catalog", return_value=catalog_response
        ) as respond_with_catalog:
            response = run_team(
                request=AgentRequest(message="piso moneda 3mm ancho 1.20 para cubrir 30m2"),
                search=self._fake_search,
                api_key="sk-test",
                context_builder=lambda req, intk: "CATALOGO",
            )

        self.assertEqual(response.answer, "respuesta catalogo")
        respond_with_catalog.assert_called_once()

    @staticmethod
    def _fake_search(request: SearchRequest) -> SearchResponse:
        return SearchResponse(query=request.query, total_catalog_size=0, hits=[])


if __name__ == "__main__":
    unittest.main()
