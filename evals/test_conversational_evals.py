"""Runner de los evals conversacionales: un test por caso YAML en evals/cases/.

Formato de caso:

    name: descripcion_corta            # opcional, informativo
    history:                           # opcional, turnos previos
      - {role: user, content: "..."}
      - {role: assistant, content: "..."}
    message: "último mensaje del cliente"   # los \n simulan ráfaga de mensajes unidos
    limit: 5                           # opcional
    asserts:
      - tool_called: buscar_productos
      - presents_product
      - not_asks: ["ancho"]

Los invariantes globales (no_prices, brand_rules, only_allowed_links) corren siempre.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from app.agents.pydantic_agent import run_pydantic_agent
from app.core.models import AgentMessage, AgentRequest

from evals.assertions import EvalContext, run_asserts
from evals.conftest import RecordingSearch

pytestmark = pytest.mark.eval

CASES_DIR = Path(__file__).parent / "cases"
CASE_FILES = sorted(CASES_DIR.glob("*.yaml"))
EVAL_MODEL = os.environ.get("ODRANID_EVAL_MODEL", "gpt-4.1-mini")


# El LLM no es determinístico: un caso puede fallar por ruido en una corrida y pasar en
# la siguiente. Cada caso se intenta hasta MAX_ATTEMPTS veces y falla solo si falla TODAS:
# una regresión sistemática falla siempre; el ruido ocasional no frena el CI.
MAX_ATTEMPTS = 2


@pytest.mark.parametrize("case_file", CASE_FILES, ids=lambda path: path.stem)
def test_conversational_case(
    case_file: Path,
    openai_api_key: str,
    catalog_context: str,
    catalog_products,
) -> None:
    case = yaml.safe_load(case_file.read_text(encoding="utf-8"))
    request = AgentRequest(
        message=case["message"],
        history=[AgentMessage.model_validate(turn) for turn in case.get("history", [])],
        limit=case.get("limit", 5),
    )

    attempts: list[tuple[list[str], str]] = []
    for _ in range(MAX_ATTEMPTS):
        recording_search = RecordingSearch(catalog_products)
        response = run_pydantic_agent(
            request=request,
            search=recording_search,
            api_key=openai_api_key,
            catalog_context=catalog_context,
            model=EVAL_MODEL,
        )
        ctx = EvalContext(response=response, search_responses=recording_search.responses)
        failures = run_asserts(case.get("asserts", []), ctx)
        if not failures:
            return
        attempts.append((failures, response.answer))

    detail = "\n\n".join(
        f"intento {i}:\n" + "\n".join(f"  - {f}" for f in failures) + f"\n  respuesta: {answer!r}"
        for i, (failures, answer) in enumerate(attempts, start=1)
    )
    pytest.fail(
        f"caso {case_file.stem} falló en {MAX_ATTEMPTS} intentos:\n{detail}",
        pytrace=False,
    )
