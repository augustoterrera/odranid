from __future__ import annotations

import typesense

from ..core.config import settings


class TypesenseConfigurationError(RuntimeError):
    pass


class TypesenseHealthcheckError(RuntimeError):
    pass


def build_typesense_client() -> typesense.Client:
    if not settings.typesense_api_key:
        raise TypesenseConfigurationError("ODRANID_TYPESENSE_API_KEY is required to connect to Typesense")

    return typesense.Client(
        {
            "api_key": settings.typesense_api_key,
            "nodes": [
                {
                    "host": settings.typesense_host,
                    "port": str(settings.typesense_port),
                    "protocol": settings.typesense_protocol,
                }
            ],
            "connection_timeout_seconds": 2,
        }
    )


def healthcheck(client: typesense.Client | None = None) -> bool:
    active_client = client or build_typesense_client()
    try:
        return bool(active_client.operations.is_healthy())
    except Exception as exc:
        raise TypesenseHealthcheckError(f"Typesense healthcheck failed: {exc}") from exc


def ping() -> bool:
    return healthcheck()
