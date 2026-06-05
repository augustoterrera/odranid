from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

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

    def test_run_agent_uses_pydantic_agent_when_configured(self) -> None:
        seen: dict[str, object] = {}

        def fake_run_pydantic_agent(request, search, api_key, catalog_context, model, prompt_file):
            seen["request"] = request
            seen["catalog_context"] = catalog_context
            seen["search"] = search
            seen["api_key"] = api_key
            seen["model"] = model
            seen["prompt_file"] = prompt_file
            return AgentResponse(answer="respuesta del agente")

        with patch.object(main.settings, "openai_api_key", "sk-test"), patch.object(
            main, "current_catalog_context", return_value="CATALOGO"
        ), patch(
            "app.agents.pydantic_agent.run_pydantic_agent", side_effect=fake_run_pydantic_agent
        ):
            response = main.run_agent(AgentRequest(message="Estoy buscando pisos con diseño para cubrir 7m2"))

        self.assertEqual(response.answer, "respuesta del agente")
        self.assertEqual(seen["api_key"], "sk-test")
        self.assertEqual(seen["catalog_context"], "CATALOGO")
        self.assertIs(seen["search"], main.perform_search)

    def test_run_agent_requires_openai_key(self) -> None:
        # LLM-only pipeline: no deterministic keyword fallback. Without a key the
        # agent endpoint returns 503 instead of degrading to keyword matching.
        from fastapi import HTTPException

        with patch.object(main.settings, "openai_api_key", None):
            with self.assertRaises(HTTPException) as ctx:
                main.run_agent(AgentRequest(message="Estoy buscando pisos con diseño para cubrir 7m2"))

        self.assertEqual(ctx.exception.status_code, 503)


class AdminAuthTests(unittest.TestCase):
    def test_admin_disabled_when_no_token_configured(self) -> None:
        from fastapi import HTTPException

        with patch.object(main.settings, "admin_api_token", None):
            with self.assertRaises(HTTPException) as ctx:
                main.require_admin_token(x_admin_token="lo-que-sea")
        self.assertEqual(ctx.exception.status_code, 503)

    def test_admin_rejects_wrong_or_missing_token(self) -> None:
        from fastapi import HTTPException

        with patch.object(main.settings, "admin_api_token", "secreto"):
            with self.assertRaises(HTTPException) as ctx:
                main.require_admin_token(x_admin_token="incorrecto")
            self.assertEqual(ctx.exception.status_code, 401)
            with self.assertRaises(HTTPException) as ctx2:
                main.require_admin_token(x_admin_token=None)
            self.assertEqual(ctx2.exception.status_code, 401)

    def test_admin_accepts_correct_token(self) -> None:
        with patch.object(main.settings, "admin_api_token", "secreto"):
            self.assertIsNone(main.require_admin_token(x_admin_token="secreto"))

    def test_sync_catalog_endpoint_requires_admin_token(self) -> None:
        client = TestClient(main.app)

        with patch.object(main.settings, "admin_api_token", None):
            response = client.post("/admin/sync-catalog")
        self.assertEqual(response.status_code, 503)

        with patch.object(main.settings, "admin_api_token", "secreto"):
            response = client.post("/admin/sync-catalog")
        self.assertEqual(response.status_code, 401)

    def test_sync_catalog_endpoint_queues_celery_task(self) -> None:
        from app.tasks import catalog_tasks

        client = TestClient(main.app)

        class FakeAsyncResult:
            id = "task-123"

        with patch.object(main.settings, "admin_api_token", "secreto"), patch.object(
            catalog_tasks.sync_catalog_to_postgres, "delay", return_value=FakeAsyncResult()
        ) as delay:
            response = client.post("/admin/sync-catalog", headers={"x-admin-token": "secreto"})

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "queued")
        self.assertEqual(body["task_id"], "task-123")
        delay.assert_called_once_with()

    def test_typesense_sync_endpoint_queues_full_rebuild(self) -> None:
        from app.tasks import catalog_tasks

        client = TestClient(main.app)

        class FakeAsyncResult:
            id = "task-456"

        with patch.object(main.settings, "admin_api_token", "secreto"), patch.object(
            catalog_tasks.sync_typesense_catalog, "delay", return_value=FakeAsyncResult()
        ) as delay:
            response = client.post("/admin/typesense-sync", headers={"x-admin-token": "secreto"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "queued")
        delay.assert_called_once_with(recreate=True)


class WebhookSecretPolicyTests(unittest.TestCase):
    def test_aborts_startup_when_required_but_missing(self) -> None:
        with patch.object(main.settings, "chatwoot_webhook_secret", None), patch.object(
            main.settings, "require_webhook_secret", True
        ):
            with self.assertRaises(RuntimeError):
                main.enforce_webhook_secret_policy()

    def test_only_warns_when_not_required(self) -> None:
        with patch.object(main.settings, "chatwoot_webhook_secret", None), patch.object(
            main.settings, "require_webhook_secret", False
        ):
            # No debe lanzar; solo loguea un warning.
            self.assertIsNone(main.enforce_webhook_secret_policy())

    def test_ok_when_secret_present(self) -> None:
        with patch.object(main.settings, "chatwoot_webhook_secret", "shh"), patch.object(
            main.settings, "require_webhook_secret", True
        ):
            self.assertIsNone(main.enforce_webhook_secret_policy())


if __name__ == "__main__":
    unittest.main()
