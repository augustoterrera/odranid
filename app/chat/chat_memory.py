from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from ..core.models import AgentMessage, AgentResponse, ProductIntakeResponse
from ..catalog.normalization import norm_num, norm_text
from .slot_questions import derived_roll_surface_m2, floor_next_question, hose_next_question


_POOLS: dict[tuple[int, str], ConnectionPool] = {}


class ChatMemoryError(RuntimeError):
    pass


@dataclass(frozen=True)
class ChatConversation:
    id: int
    channel: str
    external_conversation_id: str
    state: dict[str, Any]
    account_id: str | None = None


@dataclass(frozen=True)
class ChatMessage:
    id: int
    conversation_id: int
    external_message_id: str | None
    role: str
    content: str
    raw_payload: dict[str, Any]
    created_at: str | None = None


@dataclass(frozen=True)
class ChatOutboxMessage:
    id: int
    conversation_id: int
    external_conversation_id: str
    channel: str
    content: str
    status: str
    idempotency_key: str
    attempts: int
    max_attempts: int
    error: str | None = None


@dataclass(frozen=True)
class ChatMemoryStore:
    database_url: str

    def get_or_create_conversation(
        self,
        channel: str,
        external_conversation_id: int | str,
        external_contact_id: int | str | None = None,
        account_id: int | str | None = None,
    ) -> ChatConversation:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into public.chat_conversations (
                      channel, external_conversation_id, external_contact_id, account_id, last_seen_at
                    )
                    values (%s, %s, %s, %s, now())
                    on conflict (channel, external_conversation_id) do update set
                      external_contact_id = coalesce(excluded.external_contact_id, public.chat_conversations.external_contact_id),
                      account_id = coalesce(excluded.account_id, public.chat_conversations.account_id),
                      last_seen_at = now()
                    returning *
                    """,
                    (
                        channel,
                        str(external_conversation_id),
                        str(external_contact_id) if external_contact_id is not None else None,
                        str(account_id) if account_id is not None else None,
                    ),
                )
                return conversation_from_row(first_row(cur.fetchone()))

    def mark_event_received(
        self,
        event_key: str,
        channel: str,
        external_conversation_id: int | str,
        external_message_id: int | str | None,
        raw_payload: dict[str, Any],
    ) -> bool:
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        insert into public.chat_processed_events (
                          event_key, channel, external_conversation_id, external_message_id, raw_payload, status
                        )
                        values (%s, %s, %s, %s, %s, 'received')
                        """,
                        (
                            event_key,
                            channel,
                            str(external_conversation_id),
                            str(external_message_id) if external_message_id is not None else None,
                            Jsonb(raw_payload),
                        ),
                    )
        except UniqueViolation:
            return False
        return True

    def update_event_status(self, event_key: str, status: str, error: str | None = None) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "update public.chat_processed_events set status = %s, error = %s where event_key = %s",
                    (status, error, event_key),
                )

    def enqueue_job(
        self,
        event_key: str,
        channel: str,
        external_conversation_id: int | str,
        external_message_id: int | str | None,
        raw_payload: dict[str, Any],
        run_at: str | None = None,
        max_attempts: int | None = None,
    ) -> int:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into public.chat_webhook_jobs (
                      event_key, channel, external_conversation_id, external_message_id,
                      raw_payload, status, run_at, max_attempts
                    )
                    values (%s, %s, %s, %s, %s, 'queued', coalesce(%s::timestamptz, now()), coalesce(%s, 5))
                    returning id
                    """,
                    (
                        event_key,
                        channel,
                        str(external_conversation_id),
                        str(external_message_id) if external_message_id is not None else None,
                        Jsonb(raw_payload),
                        run_at,
                        max_attempts,
                    ),
                )
                return int(first_row(cur.fetchone())["id"])

    def update_job_status(self, job_id: int, status: str, error: str | None = None) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    update public.chat_webhook_jobs
                    set status = %s,
                        error = %s,
                        attempts = case when %s = 'processing' then attempts + 1 else attempts end,
                        started_at = case when %s = 'processing' then now() else started_at end,
                        locked_at = case when %s = 'processing' then now() else locked_at end,
                        finished_at = case when %s in ('completed', 'failed', 'skipped') then now() else finished_at end,
                        completed_at = case when %s = 'completed' then now() else completed_at end
                    where id = %s
                    """,
                    (status, error, status, status, status, status, status, job_id),
                )

    def update_jobs_for_conversation(
        self,
        conversation_id: int,
        status: str,
        error: str | None = None,
        worker_id: str | None = None,
    ) -> None:
        conversation = self.get_conversation(conversation_id)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    update public.chat_webhook_jobs
                    set status = %s,
                        error = %s,
                        worker_id = case when %s = 'processing' then %s else worker_id end,
                        attempts = case when %s = 'processing' then attempts + 1 else attempts end,
                        started_at = case when %s = 'processing' then now() else started_at end,
                        locked_at = case when %s = 'processing' then now() else locked_at end,
                        finished_at = case when %s in ('completed', 'failed', 'skipped') then now() else finished_at end,
                        completed_at = case when %s = 'completed' then now() else completed_at end
                    where channel = %s
                      and external_conversation_id = %s
                      and status in ('queued', 'processing', 'retry')
                    """,
                    (
                        status,
                        error,
                        status,
                        worker_id,
                        status,
                        status,
                        status,
                        status,
                        status,
                        conversation.channel,
                        conversation.external_conversation_id,
                    ),
                )

    def update_events_for_conversation(self, conversation_id: int, status: str, error: str | None = None) -> None:
        conversation = self.get_conversation(conversation_id)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    update public.chat_processed_events
                    set status = %s, error = %s
                    where channel = %s
                      and external_conversation_id = %s
                      and status in ('received', 'processing', 'retry')
                    """,
                    (status, error, conversation.channel, conversation.external_conversation_id),
                )

    def get_conversation(self, conversation_id: int | str) -> ChatConversation:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("select * from public.chat_conversations where id = %s", (conversation_id,))
                return conversation_from_row(first_row(cur.fetchone()))

    def acquire_lock(self, channel: str, external_conversation_id: int | str, lock_seconds: int = 60) -> bool:
        with self._connect() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        select id
                        from public.chat_conversations
                        where channel = %s and external_conversation_id = %s
                        for update
                        """,
                        (channel, str(external_conversation_id)),
                    )
                    row = cur.fetchone()
                    if row is None:
                        return False
                    cur.execute(
                        """
                        update public.chat_conversations
                        set locked_until = now() + make_interval(secs => greatest(%s, 1))
                        where id = %s
                          and (locked_until is null or locked_until < now())
                        returning id
                        """,
                        (lock_seconds, row["id"]),
                    )
                    return cur.fetchone() is not None

    def release_lock(self, channel: str, external_conversation_id: int | str) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    update public.chat_conversations
                    set locked_until = null
                    where channel = %s and external_conversation_id = %s
                    """,
                    (channel, str(external_conversation_id)),
                )

    def add_message(
        self,
        conversation_id: int,
        role: str,
        content: str,
        external_message_id: int | str | None = None,
        raw_payload: dict[str, Any] | None = None,
        agent_response: AgentResponse | None = None,
        processing_status: str | None = None,
        created_at: Any = None,
    ) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into public.chat_messages (
                      conversation_id, external_message_id, role, content, raw_payload, tool_calls, processing_status, created_at
                    )
                    values (%s, %s, %s, %s, %s, %s, coalesce(%s, 'processed'), coalesce(%s::timestamptz, now()))
                    on conflict (conversation_id, external_message_id, role) do update set
                      content = excluded.content,
                      raw_payload = excluded.raw_payload,
                      tool_calls = excluded.tool_calls,
                      processing_status = excluded.processing_status
                    """,
                    (
                        conversation_id,
                        str(external_message_id) if external_message_id is not None else None,
                        role,
                        content,
                        Jsonb(raw_payload or {}),
                        Jsonb([trace.model_dump() for trace in agent_response.tool_calls] if agent_response else []),
                        processing_status,
                        created_at,
                    ),
                )

    def pending_messages(self, conversation_id: int, limit: int = 50) -> list[ChatMessage]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select id, conversation_id, external_message_id, role, content, raw_payload, created_at
                    from public.chat_messages
                    where conversation_id = %s
                      and role = 'user'
                      and processing_status = 'pending'
                    order by created_at asc
                    limit %s
                    """,
                    (conversation_id, limit),
                )
                return [message_from_row(dict(row)) for row in cur.fetchall()]

    def mark_messages_processing(self, message_ids: list[int]) -> None:
        self._mark_messages(message_ids, "processing")

    def mark_messages_processed(self, message_ids: list[int]) -> None:
        self._mark_messages(message_ids, "processed", processed=True)

    def mark_messages_pending(self, message_ids: list[int], error: str | None = None) -> None:
        self._mark_messages(message_ids, "pending", error=error)

    def _mark_messages(
        self,
        message_ids: list[int],
        status: str,
        processed: bool = False,
        error: str | None = None,
    ) -> None:
        if not message_ids:
            return
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    update public.chat_messages
                    set processing_status = %s,
                        processing_error = %s,
                        processed_at = case when %s then now() else processed_at end
                    where id = any(%s)
                    """,
                    (status, error, processed, message_ids),
                )

    def recent_history(self, conversation_id: int, limit: int) -> list[AgentMessage]:
        if limit <= 0:
            return []
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select role, content
                    from public.chat_messages
                    where conversation_id = %s
                    order by created_at desc
                    limit %s
                    """,
                    (conversation_id, limit),
                )
                rows = cur.fetchall()

        messages: list[AgentMessage] = []
        for row in reversed(rows):
            role = row.get("role")
            if role not in {"user", "assistant"}:
                continue
            content = str(row.get("content") or "").strip()
            if content:
                messages.append(AgentMessage(role=role, content=content))
        return messages

    def update_conversation_state(self, conversation_id: int, state: dict[str, Any]) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "update public.chat_conversations set state = %s where id = %s",
                    (Jsonb(state), conversation_id),
                )

    def retargeting_candidates(
        self,
        hours: int,
        window_hours: int = 22,
        max_age_hours: int = 0,
        require_intent: bool = True,
        limit: int = 100,
    ) -> list[ChatConversation]:
        """Conversaciones elegibles para un recordatorio único de retargeting.

        Condiciones:
        - El último mensaje de la conversación es del bot (assistant) y el cliente
          no respondió hace >= ``hours`` (abandono).
        - El último mensaje del CLIENTE cae dentro de ``window_hours`` (ventana de
          WhatsApp de 24h): fuera de eso un texto libre sería rechazado.
        - ``require_intent``: solo leads con intención de producto (state.intent),
          excluyendo cierres cordiales ("no, gracias") que dejan intent en null.
        - ``max_age_hours`` (si > 0): descarta backlog histórico muy viejo.
        - Todavía no recibieron retargeting (one-shot permanente).
        """
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select c.id, c.channel, c.external_conversation_id, c.state, c.account_id
                    from public.chat_conversations c
                    join lateral (
                      select role, created_at
                      from public.chat_messages m
                      where m.conversation_id = c.id
                      order by m.created_at desc
                      limit 1
                    ) last_msg on true
                    join lateral (
                      select max(created_at) as created_at
                      from public.chat_messages m
                      where m.conversation_id = c.id and m.role = 'user'
                    ) last_user on true
                    where last_msg.role = 'assistant'
                      and last_msg.created_at <= now() - make_interval(hours => %(hours)s)
                      and (%(max_age)s = 0 or last_msg.created_at >= now() - make_interval(hours => %(max_age)s))
                      and last_user.created_at is not null
                      and last_user.created_at >= now() - make_interval(hours => %(window)s)
                      and coalesce((c.state->>'retargeting_sent')::boolean, false) = false
                      and (%(require_intent)s = false
                           or (c.state->>'intent') is not null and (c.state->>'intent') <> '')
                    order by last_msg.created_at asc
                    limit %(limit)s
                    """,
                    {
                        "hours": hours,
                        "window": window_hours,
                        "max_age": max_age_hours,
                        "require_intent": require_intent,
                        "limit": limit,
                    },
                )
                return [conversation_from_row(row) for row in cur.fetchall()]

    def retargeting_stats(self) -> dict[str, int]:
        """Funnel de retargeting: enviados y cuántos reactivaron (el cliente
        escribió DESPUÉS de recibir el recordatorio)."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select
                      count(*) filter (
                        where coalesce((c.state->>'retargeting_sent')::boolean, false)
                      ) as sent,
                      count(*) filter (
                        where coalesce((c.state->>'retargeting_sent')::boolean, false)
                          and (c.state->>'retargeting_sent_at') is not null
                          and exists (
                            select 1 from public.chat_messages m
                            where m.conversation_id = c.id
                              and m.role = 'user'
                              and m.created_at > (c.state->>'retargeting_sent_at')::timestamptz
                          )
                      ) as reactivated
                    from public.chat_conversations c
                    """
                )
                row = first_row(cur.fetchone())
                return {"sent": int(row.get("sent") or 0), "reactivated": int(row.get("reactivated") or 0)}

    def mark_retargeting_sent(self, conversation_id: int) -> None:
        """Marca la conversación como retargeteada (one-shot permanente),
        sin pisar el resto del ``state`` (merge jsonb)."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    update public.chat_conversations
                    set state = coalesce(state, '{}'::jsonb)
                                || jsonb_build_object('retargeting_sent', true,
                                                      'retargeting_sent_at', now()::text)
                    where id = %s
                    """,
                    (conversation_id,),
                )

    def create_outbox_message(
        self,
        conversation_id: int,
        external_conversation_id: int | str,
        channel: str,
        content: str,
        idempotency_key: str,
        max_attempts: int = 5,
    ) -> int:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into public.chat_outbox_messages (
                      conversation_id, external_conversation_id, channel, content,
                      status, idempotency_key, max_attempts
                    )
                    values (%s, %s, %s, %s, 'pending', %s, %s)
                    on conflict (idempotency_key) do update set
                      idempotency_key = excluded.idempotency_key
                    returning id
                    """,
                    (conversation_id, str(external_conversation_id), channel, content, idempotency_key, max_attempts),
                )
                return int(first_row(cur.fetchone())["id"])

    def get_outbox_message(self, outbox_id: int | str) -> ChatOutboxMessage:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("select * from public.chat_outbox_messages where id = %s", (outbox_id,))
                return outbox_from_row(first_row(cur.fetchone()))

    def get_outbox_by_idempotency_key(self, idempotency_key: str) -> ChatOutboxMessage:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("select * from public.chat_outbox_messages where idempotency_key = %s", (idempotency_key,))
                return outbox_from_row(first_row(cur.fetchone()))

    def mark_outbox_processing(self, outbox_id: int | str) -> bool:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    update public.chat_outbox_messages
                    set status = 'processing',
                        attempts = attempts + 1
                    where id = %s
                      and status in ('pending', 'retry')
                    """,
                    (outbox_id,),
                )
                return cur.rowcount == 1

    def mark_outbox_sent(self, outbox_id: int | str, raw_payload: dict[str, Any] | None = None) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    update public.chat_outbox_messages
                    set status = 'sent',
                        sent_at = now(),
                        error = null,
                        raw_payload = %s
                    where id = %s
                    """,
                    (Jsonb(raw_payload or {}), outbox_id),
                )

    def mark_outbox_retry_or_failed(self, outbox_id: int | str, error: str) -> str:
        outbox = self.get_outbox_message(outbox_id)
        status = "failed" if outbox.attempts >= outbox.max_attempts else "retry"
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "update public.chat_outbox_messages set status = %s, error = %s where id = %s",
                    (status, error, outbox_id),
                )
        return status

    def pending_outbox_ids(self, limit: int = 100) -> list[int]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select id
                    from public.chat_outbox_messages
                    where status in ('pending', 'retry')
                    order by created_at asc
                    limit %s
                    """,
                    (limit,),
                )
                return [int(row["id"]) for row in cur.fetchall()]

    def requeue_stale_jobs(self, stale_minutes: int, limit: int = 100) -> list[int]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("select public.requeue_stale_chat_webhook_jobs(%s, %s) as ids", (stale_minutes, limit))
                ids = first_row(cur.fetchone()).get("ids") or []
                return [int(value) for value in ids]

    def due_job_conversation_ids(self, limit: int = 100) -> list[int]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("select public.due_chat_webhook_job_conversations(%s) as ids", (limit,))
                ids = first_row(cur.fetchone()).get("ids") or []
                return [int(value) for value in ids]

    def cleanup_expired_conversation_locks(self) -> int:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("select public.cleanup_expired_chat_conversation_locks() as cleaned")
                return int(first_row(cur.fetchone()).get("cleaned") or 0)

    def _connect(self) -> psycopg.Connection:
        try:
            return self._pool().connection()
        except psycopg.Error as exc:
            raise ChatMemoryError(f"Could not connect to Postgres chat memory: {exc}") from exc

    def _pool(self) -> ConnectionPool:
        key = (os.getpid(), self.database_url)
        pool = _POOLS.get(key)
        if pool is None or pool.closed:
            pool = ConnectionPool(
                self.database_url,
                min_size=1,
                max_size=5,
                kwargs={"row_factory": dict_row},
                open=True,
            )
            _POOLS[key] = pool
        return pool


def conversation_from_row(row: dict[str, Any]) -> ChatConversation:
    state = row.get("state") if isinstance(row.get("state"), dict) else {}
    return ChatConversation(
        id=int(row["id"]),
        channel=str(row["channel"]),
        external_conversation_id=str(row["external_conversation_id"]),
        state=state,
        account_id=str(row["account_id"]) if row.get("account_id") is not None else None,
    )


def message_from_row(row: dict[str, Any]) -> ChatMessage:
    raw_payload = row.get("raw_payload") if isinstance(row.get("raw_payload"), dict) else {}
    return ChatMessage(
        id=int(row["id"]),
        conversation_id=int(row["conversation_id"]),
        external_message_id=str(row["external_message_id"]) if row.get("external_message_id") is not None else None,
        role=str(row["role"]),
        content=str(row.get("content") or ""),
        raw_payload=raw_payload,
        created_at=str(row["created_at"]) if row.get("created_at") is not None else None,
    )


def outbox_from_row(row: dict[str, Any]) -> ChatOutboxMessage:
    return ChatOutboxMessage(
        id=int(row["id"]),
        conversation_id=int(row["conversation_id"]),
        external_conversation_id=str(row["external_conversation_id"]),
        channel=str(row["channel"]),
        content=str(row.get("content") or ""),
        status=str(row.get("status") or ""),
        idempotency_key=str(row.get("idempotency_key") or ""),
        attempts=int(row.get("attempts") or 0),
        max_attempts=int(row.get("max_attempts") or 1),
        error=str(row["error"]) if row.get("error") is not None else None,
    )


def build_memory_state(previous_state: dict[str, Any], intake: ProductIntakeResponse, pending_slot: str | None) -> dict[str, Any]:
    if should_keep_pending_question(previous_state, intake):
        return {
            **previous_state,
            "should_search": False,
            "last_question": previous_state.get("last_question") or fallback_pending_question(previous_state.get("pending_slot")),
        }

    known = authoritative_known_from_intake(intake)
    missing = recompute_missing_slots(intake.intent, known, intake.missing)
    should_search = not missing if intake.intent in {"pisos", "mangueras"} else intake.should_search
    next_question = None
    if not should_search:
        next_question = recompute_next_question(intake.intent, known, missing, intake.next_question)
    return {
        **previous_state,
        "intent": intake.intent,
        "known": known,
        "missing": missing,
        "pending_slot": None if should_search else pending_slot_from_missing(missing),
        "last_question": next_question,
        "should_search": should_search,
    }


# El `intent` es texto libre del LLM (ej. "buscar_piso", "pisos", "comprar_manguera"),
# así que matcheamos por substring sobre el intent normalizado en vez de igualdad exacta.
_RETARGETING_RUBROS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("piso", "vinil", "pvc", "goma"), "los pisos de goma"),
    (("mangu",), "las mangueras"),
    (("masc", "juguete", "perro", "gato"), "los juguetes para tu mascota"),
    (("calzado", "bota", "zapat"), "el calzado"),
)


def retargeting_rubro(intent: Any) -> str | None:
    text = str(intent or "").strip().lower()
    if not text:
        return None
    for needles, rubro in _RETARGETING_RUBROS:
        if any(needle in text for needle in needles):
            return rubro
    return None


def build_retargeting_message(state: dict[str, Any], default_message: str) -> str:
    """Personaliza el recordatorio según el rubro que venía buscando el cliente.
    Si no hay intención confiable, cae al mensaje genérico (no arriesga sonar robótico)."""
    rubro = retargeting_rubro((state or {}).get("intent"))
    if not rubro:
        return default_message
    return (
        "Hola, ¿cómo estás? 👋\n\n"
        f"Quedamos viendo {rubro} y no quería dejarte sin respuesta. "
        "¿Seguís buscando? Si me contás lo que te falta, te paso opciones 👌"
    )


def pending_slot_from_intake(intake: ProductIntakeResponse) -> str | None:
    if intake.should_search or not intake.missing:
        return None
    return pending_slot_from_missing(intake.missing)


def pending_slot_from_missing(missing: list[str]) -> str | None:
    if len(missing) == 1:
        return missing[0]
    return None


def should_keep_pending_question(previous_state: dict[str, Any], intake: ProductIntakeResponse) -> bool:
    if intake.intent is not None or intake.known:
        return False
    return previous_state.get("pending_slot") in {"tipo_producto", "tipo_calzado"}


def fallback_pending_question(pending_slot: Any) -> str | None:
    if pending_slot == "tipo_producto":
        return "¿Qué producto buscás o para qué uso?"
    if pending_slot == "tipo_calzado":
        return "¿Qué tipo de calzado necesitás y para qué uso?"
    return None


def recompute_missing_slots(intent: str | None, known: dict[str, Any], fallback_missing: list[str]) -> list[str]:
    if intent == "mangueras":
        return [slot for slot in ["use", "diameter", "length_m"] if known.get(slot) is None]
    if intent != "pisos":
        return fallback_missing
    is_availability_width_lookup = known.get("lookup_mode") == "availability_width"
    missing = []
    if not (known.get("floor_kind") or known.get("floor_design") or known.get("style_preference")):
        missing.append("floor_kind_or_design")
    if known.get("espesor_mm") is None:
        missing.append("espesor_mm")
    if not is_availability_width_lookup and known.get("ancho_m") is None:
        missing.append("ancho_m")
    if known.get("coverage_required") is not False and not is_availability_width_lookup and known.get("requested_m2") is None:
        missing.append("requested_m2_confirmation" if known.get("ambiguous_requested_m2") is not None else "requested_m2")
    return missing


def recompute_next_question(
    intent: str | None,
    known: dict[str, Any],
    missing: list[str],
    fallback_question: str | None,
) -> str | None:
    if not missing:
        return None
    if intent == "pisos":
        return floor_next_question(known, missing)
    if intent == "mangueras":
        return hose_next_question(known, missing)
    return fallback_question


def apply_pending_slot_to_message(message: str, state: dict[str, Any]) -> str:
    pending_slot = state.get("pending_slot")
    if pending_slot == "ancho_m":
        return f"{message} de ancho"
    if pending_slot == "espesor_mm":
        return f"{message} de espesor"
    if pending_slot == "requested_m2":
        value = extract_first_number(message)
        return f"{value:g} m2" if value is not None else f"{message} m2"
    if pending_slot == "length_m":
        return f"{message} metros"
    if pending_slot == "diameter":
        return f"{message} pulgadas"
    if pending_slot == "use":
        return f"uso {message}"
    if pending_slot == "requested_m2_confirmation" and is_affirmative_reply(message):
        ambiguous_requested_m2 = (state.get("known") or {}).get("ambiguous_requested_m2")
        if ambiguous_requested_m2 is not None:
            return f"{ambiguous_requested_m2} m2"
    return message


def should_reset_conversation_state(message: str, state: dict[str, Any]) -> bool:
    if not state:
        return False
    text = norm_text(message)
    if is_short_slot_reply(text):
        return False
    has_product_term = any(
        term in text
        for term in [
            "piso",
            "pisos",
            "manguera",
            "juguete",
            "mascota",
            "bota",
            "calzado",
            "alfombra",
            "revestimiento",
        ]
    )
    has_request_term = any(
        term in text
        for term in [
            "busco",
            "necesito",
            "nesesito",
            "nesecito",
            "preciso",
            "quiero",
            "tenes",
            "tenés",
            "tenian",
            "tienen",
            "me pasas",
            "me mostrás",
            "me mostras",
        ]
    )
    return has_product_term and has_request_term


def is_short_slot_reply(text: str) -> bool:
    return bool(re.fullmatch(r"(?:\d+\s*/\s*\d+|\d+(?:[.,]\d+)?)\s*(?:m|mt|mts|metro|metros|mm|m2|m²|pulgadas?)?", text))


def is_affirmative_reply(message: str) -> bool:
    text = norm_text(message)
    return bool(re.fullmatch(r"(si|sí|sisi|si si|correcto|exacto|dale|ok|okay|eso|tal cual)", text))


def extract_first_number(message: str) -> float | None:
    match = re.search(r"\d+(?:[.,]\d+)?", norm_text(message))
    if not match:
        return None
    value = norm_num(match.group(0))
    return value if value is not None and value > 0 else None


def history_from_state(state: dict[str, Any]) -> list[AgentMessage]:
    known = state.get("known")
    if not isinstance(known, dict) or not known:
        return []
    text = known_to_natural_text(known)
    if not text:
        return []
    return [AgentMessage(role="user", content=f"Datos ya recopilados: {text}")]


def authoritative_known_from_intake(intake: ProductIntakeResponse) -> dict[str, Any]:
    known = {key: value for key, value in intake.known.items() if value is not None}
    complete_derived_slots(known)
    return known


def complete_derived_slots(known: dict[str, Any]) -> None:
    if known.get("rubro") != "pisos" or known.get("requested_m2") is not None:
        return
    derived = derived_roll_surface_m2(
        safe_int(known.get("roll_count")),
        safe_float(known.get("roll_length_m")),
        safe_float(known.get("ancho_m")),
    )
    if derived is not None:
        known["requested_m2"] = derived


def safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def known_to_natural_text(known: dict[str, Any]) -> str:
    parts: list[str] = []
    if known.get("rubro") == "pisos":
        if known.get("category") == "pisos_vinilicos":
            parts.append("piso vinilico")
        elif known.get("floor_design") == "simil_madera":
            parts.append("piso")
        elif known.get("floor_kind") or known.get("floor_design"):
            parts.append("piso goma")
        else:
            parts.append("piso")
    elif known.get("rubro") == "mascotas":
        parts.append("juguete para mascota")
    elif known.get("rubro") == "mangueras":
        parts.append("manguera")
    elif known.get("rubro") is not None:
        parts.append(str(known["rubro"]))

    if known.get("category") == "pisos_vinilicos":
        parts.append("vinilico")
    elif known.get("category") is not None:
        parts.append(str(known["category"]).replace("_", " "))

    if known.get("lookup_mode") == "product_reference" or (
        known.get("coverage_required") is False and known.get("lookup_mode") != "availability_width"
    ):
        parts.append("vengo de la tienda online producto")
    if known.get("lookup_mode") == "availability_width":
        parts.append("consulta anchos disponibles")

    if known.get("animal") is not None:
        parts.append(str(known["animal"]))
    if known.get("size") is not None:
        parts.append(f"tamaño {known['size']}")
    if known.get("toy_type") is not None:
        parts.append(f"tipo {known['toy_type']}")
    if known.get("resistant") is True:
        parts.append("resistente")
    if known.get("diameter") is not None:
        parts.append(f"diametro {known['diameter']}")
    if known.get("length_m") is not None:
        parts.append(f"largo {known['length_m']}m")
    if known.get("requested_m2") is not None:
        parts.append(f"para cubrir {known['requested_m2']}m2")
    if known.get("roll_count") is not None:
        parts.append(f"{known['roll_count']} rollos")
    if known.get("roll_length_m") is not None:
        parts.append(f"largo rollo {known['roll_length_m']}m")
    if known.get("floor_design") is not None:
        parts.append(f"diseño {known['floor_design']}")
    elif known.get("style_preference") == "indiferente":
        parts.append("diseño indiferente")
    elif known.get("floor_kind") is not None:
        parts.append(str(known["floor_kind"]))
    if known.get("espesor_mm") is not None:
        parts.append(f"espesor {known['espesor_mm']}mm")
    if known.get("ancho_m") is not None:
        parts.append(f"ancho {known['ancho_m']}m")
    if known.get("use") is not None:
        parts.append(f"uso {known['use']}")
    if known.get("traffic") is not None:
        parts.append(f"transito {known['traffic']}")
    if known.get("budget_preference") is not None:
        parts.append(str(known["budget_preference"]))
    if known.get("tags"):
        parts.extend(str(tag).replace("_", " ") for tag in known["tags"])
    return " ".join(parts)


def first_row(payload: Any) -> dict[str, Any]:
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        return payload[0]
    if isinstance(payload, dict):
        return payload
    raise ChatMemoryError("Postgres returned an empty or invalid row payload")


def build_chat_memory_store_from_settings(settings: Any) -> ChatMemoryStore | None:
    if not settings.database_url:
        return None
    return ChatMemoryStore(settings.database_url)
