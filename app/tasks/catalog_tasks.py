from __future__ import annotations

import logging

from ..celery_app import celery_app
from ..config import settings
from ..typesense_sync import TypesenseSyncError, run_typesense_sync

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.catalog_tasks.sync_typesense_catalog")
def sync_typesense_catalog() -> dict[str, object]:
    """Periodic refresh of the Typesense index (upsert, no drop)."""
    if not settings.typesense_api_key:
        logger.info("typesense_sync_skipped: no API key configured")
        return {"ok": False, "skipped": "no_typesense_api_key"}
    try:
        result = run_typesense_sync(recreate=False)
        logger.info("typesense_sync_ok: %s", result)
        return result
    except TypesenseSyncError as exc:
        logger.warning("typesense_sync_failed: %s", exc)
        return {"ok": False, "error": str(exc)}
