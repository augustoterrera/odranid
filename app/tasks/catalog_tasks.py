from __future__ import annotations

import logging

from ..catalog_sync import CatalogSyncError, run_catalog_to_postgres_sync
from ..celery_app import celery_app
from ..core.config import settings
from ..postgres_store import PostgresStoreError
from ..typesense_sync import TypesenseSyncError, run_typesense_sync

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.catalog_tasks.sync_catalog_to_postgres")
def sync_catalog_to_postgres() -> dict[str, object]:
    """Periodic WooCommerce -> Postgres catalog sync."""
    if not settings.openai_api_key:
        logger.info("catalog_sync_skipped: no OpenAI API key configured")
        return {"ok": False, "skipped": "no_openai_api_key"}
    if not settings.database_url:
        logger.info("catalog_sync_skipped: no database URL configured")
        return {"ok": False, "skipped": "no_database_url"}

    try:
        result = run_catalog_to_postgres_sync()
        logger.info("catalog_sync_ok: %s", result)
        return result
    except (CatalogSyncError, PostgresStoreError) as exc:
        logger.warning("catalog_sync_failed: %s", exc)
        return {"ok": False, "error": str(exc)}


@celery_app.task(name="app.tasks.catalog_tasks.sync_typesense_catalog")
def sync_typesense_catalog(recreate: bool = False) -> dict[str, object]:
    """Refresh the Typesense index. ``recreate=False`` (beat) upserts in place;
    ``recreate=True`` (admin full rebuild) drops and rebuilds the collection."""
    if not settings.typesense_api_key:
        logger.info("typesense_sync_skipped: no API key configured")
        return {"ok": False, "skipped": "no_typesense_api_key"}
    try:
        result = run_typesense_sync(recreate=recreate)
        logger.info("typesense_sync_ok: %s", result)
        return result
    except TypesenseSyncError as exc:
        logger.warning("typesense_sync_failed: %s", exc)
        return {"ok": False, "error": str(exc)}
