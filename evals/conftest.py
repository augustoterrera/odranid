"""Fixtures de los evals conversacionales.

Corren el agente REAL (gasta tokens OpenAI) contra el catálogo CONGELADO de
evals/fixtures/catalog_snapshot.json, usando la búsqueda local (lexical + facetas).
Así una corrida depende solo de: prompt + modelo + guards. Ni Postgres, ni Typesense,
ni el catálogo vivo.

Requiere OPENAI_API_KEY (se toma del entorno o de .env). Sin la key, los evals se saltean.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.catalog.catalog_context import CatalogContextCache
from app.catalog.coverage import enrich_search_response
from app.core.models import ProductDocument, SearchRequest, SearchResponse
from app.search.retrieval import CatalogSearch

EVALS_DIR = Path(__file__).parent
SNAPSHOT_FILE = EVALS_DIR / "fixtures" / "catalog_snapshot.json"


class RecordingSearch:
    """Callable de búsqueda que registra las respuestas para las aserciones."""

    def __init__(self, products: list[ProductDocument]):
        self._engine = CatalogSearch(products)
        self.responses: list[SearchResponse] = []

    def __call__(self, request: SearchRequest) -> SearchResponse:
        response = enrich_search_response(self._engine.search(request))
        self.responses.append(response)
        return response


def _load_env_key() -> str | None:
    if os.environ.get("OPENAI_API_KEY"):
        return os.environ["OPENAI_API_KEY"]
    env_file = Path(".env")
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("OPENAI_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


@pytest.fixture(scope="session")
def openai_api_key() -> str:
    key = _load_env_key()
    if not key:
        pytest.skip("OPENAI_API_KEY no configurada: los evals llaman al LLM real")
    return key


@pytest.fixture(scope="session")
def catalog_products() -> list[ProductDocument]:
    payload = json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
    return [ProductDocument.model_validate(item) for item in payload["products"]]


@pytest.fixture(scope="session")
def catalog_context(catalog_products: list[ProductDocument], tmp_path_factory: pytest.TempPathFactory) -> str:
    cache_file = tmp_path_factory.mktemp("eval_context") / "context.txt"
    return CatalogContextCache(cache_file).build(catalog_products)


