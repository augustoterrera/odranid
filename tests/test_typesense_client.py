from __future__ import annotations

import pytest

from app.core.config import settings
from app.search.typesense_client import TypesenseHealthcheckError, healthcheck


def test_typesense_healthcheck_smoke() -> None:
    if not settings.typesense_api_key:
        pytest.skip("ODRANID_TYPESENSE_API_KEY is not configured")

    try:
        assert healthcheck() is True
    except TypesenseHealthcheckError as exc:
        pytest.skip(f"Typesense is not accessible: {exc}")
