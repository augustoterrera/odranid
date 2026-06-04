from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Callable
from typing import Any

from .chat_memory import (
    ChatConversation,
    ChatMemoryError,
    ChatMemoryStore,
    analyze_with_memory,
    apply_pending_slot_to_message,
    build_memory_state,
    history_from_state,
    pending_slot_from_intake,
    should_reset_conversation_state,
)
from .chatwoot import ChatwootMessageEvent
from .config import settings
from .models import AgentRequest, AgentResponse

logger = logging.getLogger(__name__)


def chatwoot_contact_id(payload: dict[str, object]) -> str | None:
    sender = payload.get("sender") if isinstance(payload.get("sender"), dict) else {}
    contact = payload.get("contact") if isinstance(payload.get("contact"), dict) else {}
    value = sender.get("id") or contact.get("id")
    return str(value) if value is not None else None


def chatwoot_event_key(
    headers: dict[str, str | None],
    conversation_id: int | str,
    message_id: int | str | None,
) -> str:
    delivery_id = headers.get("x-chatwoot-delivery")
    if delivery_id:
        return f"delivery:{delivery_id}"
    return f"message:{conversation_id}:{message_id}"


def outbox_idempotency_key(conversation_id: int | str, message_ids: list[int], content: str) -> str:
    digest_payload = {
        "conversation_id": str(conversation_id),
        "message_ids": message_ids,
        "content": content,
    }
    digest = hashlib.sha256(json.dumps(digest_payload, sort_keys=True).encode("utf-8")).hexdigest()
    return f"chatwoot:{conversation_id}:{digest}"


def persist_incoming_chatwoot_event(
    store: ChatMemoryStore,
    event_key: str,
    event: ChatwootMessageEvent,
    payload: dict[str, Any],
) -> tuple[bool, ChatConversation, int | None]:
    is_new = store.mark_event_received(
        event_key=event_key,
        channel="chatwoot",
        external_conversation_id=event.conversation_id,
        external_message_id=event.message_id,
        raw_payload=payload,
    )
    conversation = store.get_or_create_conversation(
        channel="chatwoot",
        external_conversation_id=event.conversation_id,
        external_contact_id=chatwoot_contact_id(payload),
        account_id=event.account_id or settings.chatwoot_account_id,
    )
    if not is_new:
        return False, conversation, None

    store.add_message(
        conversation_id=conversation.id,
        role="user",
        content=event.content,
        external_message_id=event.message_id,
        raw_payload=payload,
        processing_status="pending",
    )
    job_id = store.enqueue_job(
        event_key=event_key,
        channel="chatwoot",
        external_conversation_id=event.conversation_id,
        external_message_id=event.message_id,
        raw_payload=payload,
        max_attempts=settings.chatwoot_job_max_retries,
    )
    logger.info(
        "chatwoot_webhook_queued",
        extra={
            "event_key": event_key,
            "conversation_id": conversation.id,
            "job_id": job_id,
            "status": "queued",
        },
    )
    return True, conversation, job_id


def build_agent_response_for_pending_messages(
    store: ChatMemoryStore,
    conversation: ChatConversation,
    user_content: str,
    run_agent: Callable[[AgentRequest], AgentResponse],
) -> tuple[AgentResponse, dict[str, Any]]:
    reset_state = should_reset_conversation_state(user_content, conversation.state)
    active_state = {} if reset_state else conversation.state
    history = [] if reset_state else store.recent_history(conversation.id, settings.chatwoot_history_limit)

    intake = analyze_with_memory(
        user_content, history, active_state,
        api_key=str(settings.openai_api_key) if settings.openai_api_key else None,
        model=settings.agent_model,
    )
    state = build_memory_state(active_state, intake, pending_slot_from_intake(intake))
    agent_message = apply_pending_slot_to_message(user_content, active_state)
    agent_history = [*history, *history_from_state(state)]

    # LLM-only pipeline: no deterministic keyword interception. Every message
    # is handled by the Agno team (RequirementsAgent -> CatalogAgent), which
    # answers institutional/conversational from its prompt or runs a search.
    agent_response = run_agent(
        AgentRequest(
            message=agent_message,
            history=agent_history,
            limit=settings.chatwoot_agent_limit,
        )
    )

    state["last_tool_calls"] = [trace.model_dump() for trace in agent_response.tool_calls]
    return agent_response, state


def process_pending_conversation_messages(
    store: ChatMemoryStore,
    conversation_id: int,
    run_agent: Callable[[AgentRequest], AgentResponse],
) -> int | None:
    started = time.monotonic()
    phase_started = started
    pending = store.pending_messages(conversation_id)
    logger.info(
        "chatwoot_worker_phase",
        extra={
            "conversation_id": conversation_id,
            "phase": "load_pending_messages",
            "duration_ms": int((time.monotonic() - phase_started) * 1000),
            "message_count": len(pending),
        },
    )
    if not pending:
        store.update_jobs_for_conversation(conversation_id, "completed")
        store.update_events_for_conversation(conversation_id, "completed")
        return None

    message_ids = [message.id for message in pending]
    phase_started = time.monotonic()
    store.mark_messages_processing(message_ids)
    logger.info(
        "chatwoot_worker_phase",
        extra={
            "conversation_id": conversation_id,
            "phase": "mark_messages_processing",
            "duration_ms": int((time.monotonic() - phase_started) * 1000),
            "message_count": len(pending),
        },
    )
    try:
        phase_started = time.monotonic()
        conversation = store.get_conversation(conversation_id)
        logger.info(
            "chatwoot_worker_phase",
            extra={
                "conversation_id": conversation_id,
                "phase": "load_conversation",
                "duration_ms": int((time.monotonic() - phase_started) * 1000),
            },
        )
        user_content = "\n".join(message.content for message in pending if message.content.strip()).strip()
        if not user_content:
            store.mark_messages_processed(message_ids)
            store.update_jobs_for_conversation(conversation_id, "skipped")
            store.update_events_for_conversation(conversation_id, "completed")
            return None

        phase_started = time.monotonic()
        agent_response, state = build_agent_response_for_pending_messages(store, conversation, user_content, run_agent)
        logger.info(
            "chatwoot_worker_phase",
            extra={
                "conversation_id": conversation_id,
                "phase": "agent_response",
                "duration_ms": int((time.monotonic() - phase_started) * 1000),
                "message_count": len(pending),
                "tool_call_count": len(agent_response.tool_calls),
            },
        )
        phase_started = time.monotonic()
        store.add_message(
            conversation_id=conversation_id,
            role="assistant",
            content=agent_response.answer,
            raw_payload={"source": "odranid-agent", "pending_message_ids": message_ids},
            agent_response=agent_response,
            processing_status="processed",
        )
        store.update_conversation_state(conversation_id, state)
        store.mark_messages_processed(message_ids)
        logger.info(
            "chatwoot_worker_phase",
            extra={
                "conversation_id": conversation_id,
                "phase": "persist_agent_result",
                "duration_ms": int((time.monotonic() - phase_started) * 1000),
                "message_count": len(pending),
            },
        )
        phase_started = time.monotonic()
        outbox_id = store.create_outbox_message(
            conversation_id=conversation_id,
            external_conversation_id=conversation.external_conversation_id,
            channel=conversation.channel,
            content=agent_response.answer,
            idempotency_key=outbox_idempotency_key(conversation.external_conversation_id, message_ids, agent_response.answer),
            max_attempts=settings.chatwoot_outbox_max_retries,
        )
        store.update_jobs_for_conversation(conversation_id, "completed")
        store.update_events_for_conversation(conversation_id, "completed")
        logger.info(
            "chatwoot_worker_phase",
            extra={
                "conversation_id": conversation_id,
                "outbox_id": outbox_id,
                "phase": "create_outbox_complete_job",
                "duration_ms": int((time.monotonic() - phase_started) * 1000),
            },
        )
        logger.info(
            "chatwoot_conversation_processed",
            extra={
                "conversation_id": conversation_id,
                "outbox_id": outbox_id,
                "duration_ms": int((time.monotonic() - started) * 1000),
                "status": "completed",
            },
        )
        return outbox_id
    except Exception as exc:
        store.mark_messages_pending(message_ids, str(exc))
        store.update_jobs_for_conversation(conversation_id, "failed", str(exc))
        store.update_events_for_conversation(conversation_id, "failed", str(exc))
        raise ChatMemoryError(str(exc)) from exc
