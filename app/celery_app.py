from __future__ import annotations

from celery import Celery
from celery.schedules import crontab

from .core.config import settings


celery_app = Celery(
    "odranid",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["app.tasks.chatwoot_tasks", "app.tasks.catalog_tasks"],
)

celery_app.conf.update(
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_reject_on_worker_lost=True,
    task_track_started=True,
    timezone=settings.celery_timezone,
    task_default_queue="default",
    task_routes={
        "app.tasks.chatwoot_tasks.process_chatwoot_conversation": {"queue": "chatwoot_messages"},
        "app.tasks.chatwoot_tasks.send_chatwoot_outbound_message": {"queue": "chatwoot_outbound"},
        "app.tasks.chatwoot_tasks.retry_stale_processing_jobs": {"queue": "chatwoot_messages"},
        "app.tasks.chatwoot_tasks.requeue_stuck_conversation_jobs": {"queue": "chatwoot_messages"},
        "app.tasks.chatwoot_tasks.dispatch_pending_outbox_messages": {"queue": "chatwoot_outbound"},
        "app.tasks.chatwoot_tasks.cleanup_expired_locks": {"queue": "chatwoot_messages"},
        "app.tasks.catalog_tasks.sync_catalog_to_postgres": {"queue": "catalog"},
        "app.tasks.catalog_tasks.sync_typesense_catalog": {"queue": "catalog"},
    },
    beat_schedule={
        "retry-stale-processing-jobs": {
            "task": "app.tasks.chatwoot_tasks.retry_stale_processing_jobs",
            "schedule": crontab(minute="*/5"),
        },
        "dispatch-pending-outbox-messages": {
            "task": "app.tasks.chatwoot_tasks.dispatch_pending_outbox_messages",
            "schedule": crontab(minute="*/1"),
        },
        "requeue-stuck-conversation-jobs": {
            "task": "app.tasks.chatwoot_tasks.requeue_stuck_conversation_jobs",
            "schedule": crontab(minute="*/5"),
        },
        "cleanup-expired-locks": {
            "task": "app.tasks.chatwoot_tasks.cleanup_expired_locks",
            "schedule": crontab(minute="*/15"),
        },
        "sync-catalog-to-postgres": {
            "task": "app.tasks.catalog_tasks.sync_catalog_to_postgres",
            "schedule": crontab(minute=f"*/{settings.catalog_sync_minutes}"),
        },
        "sync-typesense-catalog": {
            "task": "app.tasks.catalog_tasks.sync_typesense_catalog",
            "schedule": crontab(minute=f"*/{settings.typesense_sync_minutes}"),
        },
    },
)
