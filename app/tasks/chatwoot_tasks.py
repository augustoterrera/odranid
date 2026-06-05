from __future__ import annotations

import logging
import socket
import time
from contextlib import contextmanager
from datetime import datetime
from typing import Iterator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import redis

from app.celery_app import celery_app
from app.chat.chat_memory import (
    ChatMemoryError,
    ChatMemoryStore,
    build_chat_memory_store_from_settings,
    build_retargeting_message,
)
from app.chat.chatwoot import ChatwootError, build_chatwoot_client
from app.chat.chatwoot_service import process_pending_conversation_messages
from app.core.config import settings

logger = logging.getLogger(__name__)


def memory_store() -> ChatMemoryStore:
    store = build_chat_memory_store_from_settings(settings)
    if store is None:
        raise ChatMemoryError("Chat memory store is not configured")
    return store


def dragonfly() -> redis.Redis:
    return redis.Redis.from_url(settings.celery_broker_url, decode_responses=True)


def debounce_key(conversation_id: int | str) -> str:
    return f"chatwoot:conversation:{conversation_id}:debounce"


def lock_key(conversation_id: int | str) -> str:
    return f"chatwoot:conversation:{conversation_id}:lock"


def requeue_key(conversation_id: int | str) -> str:
    return f"chatwoot:conversation:{conversation_id}:requeue"


def worker_id(task_id: str | None = None) -> str:
    host = socket.gethostname()
    return f"{host}:{task_id or 'unknown'}"


def set_conversation_debounce(conversation_id: int | str) -> None:
    try:
        dragonfly().set(
            debounce_key(conversation_id),
            str(time.time()),
            ex=max(1, settings.chatwoot_debounce_seconds),
        )
    except redis.RedisError as exc:
        logger.warning("chatwoot_debounce_set_failed", extra={"conversation_id": conversation_id, "error": str(exc)})


def debounce_active(conversation_id: int | str) -> bool:
    try:
        return bool(dragonfly().exists(debounce_key(conversation_id)))
    except redis.RedisError as exc:
        logger.warning("chatwoot_debounce_check_failed", extra={"conversation_id": conversation_id, "error": str(exc)})
        return False


def debounce_ttl(conversation_id: int | str) -> int:
    try:
        ttl = int(dragonfly().ttl(debounce_key(conversation_id)))
    except redis.RedisError as exc:
        logger.warning("chatwoot_debounce_ttl_failed", extra={"conversation_id": conversation_id, "error": str(exc)})
        return 0
    return max(0, ttl)


def requeue_conversation_once(conversation_id: int | str, countdown: int) -> bool:
    countdown = max(1, countdown)
    try:
        was_set = bool(dragonfly().set(requeue_key(conversation_id), str(time.time()), nx=True, ex=countdown))
    except redis.RedisError as exc:
        logger.warning("chatwoot_requeue_key_failed", extra={"conversation_id": conversation_id, "error": str(exc)})
        was_set = True
    if was_set:
        process_chatwoot_conversation.apply_async(
            (str(conversation_id),),
            queue="chatwoot_messages",
            countdown=countdown,
        )
    return was_set


@contextmanager
def conversation_lock(conversation_id: int | str, task_id: str | None) -> Iterator[bool]:
    client = dragonfly()
    key = lock_key(conversation_id)
    value = worker_id(task_id)
    acquired = bool(client.set(key, value, nx=True, ex=max(1, settings.chatwoot_lock_seconds)))
    try:
        yield acquired
    finally:
        if acquired:
            try:
                if client.get(key) == value:
                    client.delete(key)
            except redis.RedisError:
                logger.warning("chatwoot_lock_release_failed", extra={"conversation_id": conversation_id, "task_id": task_id})


@celery_app.task(
    bind=True,
    name="app.tasks.chatwoot_tasks.process_chatwoot_conversation",
    queue="chatwoot_messages",
    autoretry_for=(ChatMemoryError,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=settings.chatwoot_job_max_retries,
)
def process_chatwoot_conversation(self, conversation_id: str) -> dict[str, object]:
    started = time.monotonic()
    task_id = self.request.id
    attempt = int(self.request.retries) + 1
    store = memory_store()

    if debounce_active(conversation_id):
        countdown = debounce_ttl(conversation_id) + 1
        scheduled = requeue_conversation_once(conversation_id, countdown)
        return {"ok": True, "conversation_id": conversation_id, "status": "debounced", "requeued": scheduled}

    phase_started = time.monotonic()
    with conversation_lock(conversation_id, task_id) as acquired:
        logger.info(
            "chatwoot_task_phase",
            extra={
                "conversation_id": conversation_id,
                "task_id": task_id,
                "phase": "dragonfly_lock",
                "duration_ms": int((time.monotonic() - phase_started) * 1000),
                "status": "acquired" if acquired else "busy",
            },
        )
        if not acquired:
            scheduled = requeue_conversation_once(conversation_id, settings.chatwoot_debounce_retry_seconds)
            return {"ok": True, "conversation_id": conversation_id, "status": "dragonfly_lock_busy", "requeued": scheduled}

        phase_started = time.monotonic()
        conversation = store.get_conversation(conversation_id)
        pg_lock_acquired = store.acquire_lock(conversation.channel, conversation.external_conversation_id, settings.chatwoot_lock_seconds)
        logger.info(
            "chatwoot_task_phase",
            extra={
                "conversation_id": conversation_id,
                "task_id": task_id,
                "phase": "postgres_lock",
                "duration_ms": int((time.monotonic() - phase_started) * 1000),
                "status": "acquired" if pg_lock_acquired else "busy",
            },
        )
        if not pg_lock_acquired:
            scheduled = requeue_conversation_once(conversation_id, settings.chatwoot_debounce_retry_seconds)
            return {"ok": True, "conversation_id": conversation_id, "status": "postgres_lock_busy", "requeued": scheduled}

        try:
            store.update_jobs_for_conversation(int(conversation_id), "processing", worker_id=worker_id(task_id))
            from app.main import configure_search, run_agent

            configure_search()

            outbox_id = process_pending_conversation_messages(store, int(conversation_id), run_agent)
            if outbox_id is not None:
                send_chatwoot_outbound_message.apply_async((str(outbox_id),), queue="chatwoot_outbound")
            duration_ms = int((time.monotonic() - started) * 1000)
            logger.info(
                "chatwoot_task_completed",
                extra={
                    "conversation_id": conversation_id,
                    "outbox_id": outbox_id,
                    "task_id": task_id,
                    "attempt": attempt,
                    "duration_ms": duration_ms,
                    "status": "completed",
                },
            )
            return {"ok": True, "conversation_id": conversation_id, "outbox_id": outbox_id}
        except Exception as exc:
            status = "failed" if self.request.retries >= settings.chatwoot_job_max_retries else "retry"
            store.update_jobs_for_conversation(int(conversation_id), status, str(exc))
            store.update_events_for_conversation(int(conversation_id), status, str(exc))
            raise
        finally:
            store.release_lock(conversation.channel, conversation.external_conversation_id)


@celery_app.task(
    bind=True,
    name="app.tasks.chatwoot_tasks.send_chatwoot_outbound_message",
    queue="chatwoot_outbound",
    autoretry_for=(ChatMemoryError, ChatwootError),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=settings.chatwoot_outbox_max_retries,
)
def send_chatwoot_outbound_message(self, outbox_id: str) -> dict[str, object]:
    started = time.monotonic()
    task_id = self.request.id
    attempt = int(self.request.retries) + 1
    store = memory_store()
    outbox = store.get_outbox_message(outbox_id)

    if outbox.status == "sent":
        return {"ok": True, "outbox_id": outbox_id, "status": "already_sent"}
    if outbox.status == "failed":
        return {"ok": False, "outbox_id": outbox_id, "status": "failed"}

    phase_started = time.monotonic()
    claimed = store.mark_outbox_processing(outbox_id)
    if not claimed:
        current = store.get_outbox_message(outbox_id)
        return {"ok": True, "outbox_id": outbox_id, "status": f"already_{current.status}"}
    conversation = store.get_conversation(outbox.conversation_id)
    logger.info(
        "chatwoot_outbox_phase",
        extra={
            "conversation_id": outbox.conversation_id,
            "outbox_id": outbox_id,
            "task_id": task_id,
            "phase": "load_outbox_conversation",
            "duration_ms": int((time.monotonic() - phase_started) * 1000),
        },
    )
    account_id = conversation.account_id or settings.chatwoot_account_id
    client = build_chatwoot_client(settings.chatwoot_base_url, settings.chatwoot_api_access_token)
    if client is None or account_id is None:
        error = "Chatwoot API is not configured"
        exc = ChatMemoryError(error)
        status = store.mark_outbox_retry_or_failed(outbox_id, "Chatwoot API is not configured")
        if status == "failed":
            raise exc
        raise self.retry(exc=exc, countdown=settings.chatwoot_debounce_retry_seconds)

    try:
        phase_started = time.monotonic()
        response = client.create_outgoing_message(account_id, outbox.external_conversation_id, outbox.content)
        logger.info(
            "chatwoot_outbox_phase",
            extra={
                "conversation_id": outbox.conversation_id,
                "outbox_id": outbox_id,
                "task_id": task_id,
                "phase": "chatwoot_api_send",
                "duration_ms": int((time.monotonic() - phase_started) * 1000),
            },
        )
        phase_started = time.monotonic()
        store.mark_outbox_sent(outbox_id, response)
        logger.info(
            "chatwoot_outbox_phase",
            extra={
                "conversation_id": outbox.conversation_id,
                "outbox_id": outbox_id,
                "task_id": task_id,
                "phase": "mark_outbox_sent",
                "duration_ms": int((time.monotonic() - phase_started) * 1000),
            },
        )
        duration_ms = int((time.monotonic() - started) * 1000)
        logger.info(
            "chatwoot_outbox_sent",
            extra={
                "conversation_id": outbox.conversation_id,
                "outbox_id": outbox_id,
                "task_id": task_id,
                "attempt": attempt,
                "duration_ms": duration_ms,
                "status": "sent",
            },
        )
        return {"ok": True, "outbox_id": outbox_id, "status": "sent"}
    except Exception as exc:
        status = store.mark_outbox_retry_or_failed(outbox_id, str(exc))
        logger.warning(
            "chatwoot_outbox_send_failed",
            extra={"outbox_id": outbox_id, "task_id": task_id, "attempt": attempt, "status": status},
        )
        if status == "failed":
            raise
        raise self.retry(exc=exc)


@celery_app.task(name="app.tasks.chatwoot_tasks.retry_stale_processing_jobs", queue="chatwoot_messages")
def retry_stale_processing_jobs() -> dict[str, object]:
    ids = memory_store().requeue_stale_jobs(settings.chatwoot_stale_processing_minutes)
    for conversation_id in ids:
        process_chatwoot_conversation.apply_async((str(conversation_id),), queue="chatwoot_messages")
    return {"ok": True, "requeued": len(ids)}


@celery_app.task(name="app.tasks.chatwoot_tasks.dispatch_pending_outbox_messages", queue="chatwoot_outbound")
def dispatch_pending_outbox_messages() -> dict[str, object]:
    ids = memory_store().pending_outbox_ids()
    for outbox_id in ids:
        send_chatwoot_outbound_message.apply_async((str(outbox_id),), queue="chatwoot_outbound")
    return {"ok": True, "dispatched": len(ids)}


@celery_app.task(name="app.tasks.chatwoot_tasks.requeue_stuck_conversation_jobs", queue="chatwoot_messages")
def requeue_stuck_conversation_jobs() -> dict[str, object]:
    store = memory_store()
    ids = [*store.due_job_conversation_ids(), *store.requeue_stale_jobs(settings.chatwoot_stale_processing_minutes)]
    for conversation_id in ids:
        set_conversation_debounce(conversation_id)
        process_chatwoot_conversation.apply_async(
            (str(conversation_id),),
            queue="chatwoot_messages",
            countdown=settings.chatwoot_debounce_seconds,
        )
    return {"ok": True, "requeued": len(ids)}


@celery_app.task(name="app.tasks.chatwoot_tasks.cleanup_expired_locks", queue="chatwoot_messages")
def cleanup_expired_locks() -> dict[str, object]:
    cleaned = memory_store().cleanup_expired_conversation_locks()
    return {"ok": True, "cleaned": cleaned}


def within_business_hours() -> bool:
    """True si la hora local (timezone de Celery) está dentro de la franja
    comercial configurada para enviar retargeting."""
    start = settings.retargeting_send_hour_start
    end = settings.retargeting_send_hour_end
    try:
        now = datetime.now(ZoneInfo(settings.celery_timezone))
    except ZoneInfoNotFoundError:
        now = datetime.now()
    return start <= now.hour < end


@celery_app.task(name="app.tasks.chatwoot_tasks.send_retargeting_messages", queue="chatwoot_outbound")
def send_retargeting_messages() -> dict[str, object]:
    """Recordatorio único a leads tibios que no contestaron el último mensaje del bot.

    One-shot permanente: una conversación retargeteada nunca se vuelve a tocar.
    Reusa el outbox (idempotencia + reintentos + envío real)."""
    if not settings.retargeting_enabled:
        return {"ok": True, "status": "disabled"}
    if not within_business_hours():
        return {"ok": True, "status": "outside_business_hours"}

    store = memory_store()
    candidates = store.retargeting_candidates(
        hours=settings.retargeting_hours,
        window_hours=settings.retargeting_window_hours,
        max_age_hours=settings.retargeting_max_age_hours,
        require_intent=settings.retargeting_require_intent,
        limit=settings.retargeting_batch_limit,
    )
    sent = 0
    for conversation in candidates:
        # Marca primero (one-shot): si algo falla después, no reintentamos el envío
        # automáticamente para no arriesgar mensajear dos veces a quien dijo "no, gracias".
        store.mark_retargeting_sent(conversation.id)
        content = build_retargeting_message(conversation.state, settings.retargeting_message)
        outbox_id = store.create_outbox_message(
            conversation_id=conversation.id,
            external_conversation_id=conversation.external_conversation_id,
            channel=conversation.channel,
            content=content,
            idempotency_key=f"retargeting:{conversation.id}",
            max_attempts=settings.chatwoot_outbox_max_retries,
        )
        send_chatwoot_outbound_message.apply_async((str(outbox_id),), queue="chatwoot_outbound")
        sent += 1
    logger.info("chatwoot_retargeting_sweep", extra={"candidates": len(candidates), "sent": sent})
    return {"ok": True, "sent": sent}
