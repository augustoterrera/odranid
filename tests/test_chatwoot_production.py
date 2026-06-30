from __future__ import annotations

import unittest
import asyncio
import json
from unittest.mock import Mock, patch

from app import main
from app.chat.chat_memory import ChatConversation, ChatMessage, ChatOutboxMessage
from app.chat.chatwoot_service import process_pending_conversation_messages
from app.core.models import AgentMessage, AgentRequest, AgentResponse
from app.tasks import chatwoot_tasks


def chatwoot_payload(message_id: int = 10, content: str = "hola") -> dict[str, object]:
    return {
        "event": "message_created",
        "id": message_id,
        "content": content,
        "message_type": "incoming",
        "content_type": "text",
        "account": {"id": 7},
        "conversation": {"id": 99},
    }


class FakeQueryParams(dict[str, str]):
    def get(self, key: str, default: str | None = None) -> str | None:
        return super().get(key, default)


class FakeRequest:
    def __init__(
        self,
        payload: dict[str, object],
        headers: dict[str, str] | None = None,
        query_params: dict[str, str] | None = None,
    ) -> None:
        self._body = json.dumps(payload).encode("utf-8")
        self.headers = headers or {}
        self.query_params = FakeQueryParams(query_params or {})

    async def body(self) -> bytes:
        return self._body


class WebhookStore:
    def __init__(self, duplicate: bool = False) -> None:
        self.duplicate = duplicate
        self.messages: list[dict[str, object]] = []
        self.jobs = 0

    def mark_event_received(self, **kwargs: object) -> bool:
        return not self.duplicate

    def get_or_create_conversation(self, **kwargs: object) -> ChatConversation:
        return ChatConversation(id=123, channel="chatwoot", external_conversation_id="99", state={}, account_id="7")

    def add_message(self, **kwargs: object) -> None:
        self.messages.append(kwargs)

    def enqueue_job(self, **kwargs: object) -> int:
        self.jobs += 1
        return 456


class ProcessingStore:
    def __init__(self) -> None:
        self.pending = [
            ChatMessage(1, 123, "10", "user", "consulta uno", {}),
            ChatMessage(2, 123, "11", "user", "consulta dos", {}),
        ]
        self.outbox_ids: dict[str, int] = {}
        self.assistant_messages: list[str] = []
        self.processed_ids: list[int] = []
        self.job_statuses: list[str] = []

    def pending_messages(self, conversation_id: int) -> list[ChatMessage]:
        return self.pending

    def mark_messages_processing(self, message_ids: list[int]) -> None:
        self.processing_ids = message_ids

    def get_conversation(self, conversation_id: int) -> ChatConversation:
        return ChatConversation(id=conversation_id, channel="chatwoot", external_conversation_id="99", state={}, account_id="7")

    def recent_history(self, conversation_id: int, limit: int) -> list[object]:
        return []

    def add_message(self, **kwargs: object) -> None:
        if kwargs["role"] == "assistant":
            self.assistant_messages.append(str(kwargs["content"]))

    def update_conversation_state(self, conversation_id: int, state: dict[str, object]) -> None:
        self.state = state

    def mark_messages_processed(self, message_ids: list[int]) -> None:
        self.processed_ids = message_ids

    def mark_messages_pending(self, message_ids: list[int], error: str | None = None) -> None:
        self.pending_error = error

    def create_outbox_message(self, **kwargs: object) -> int:
        key = str(kwargs["idempotency_key"])
        self.outbox_ids.setdefault(key, len(self.outbox_ids) + 1)
        return self.outbox_ids[key]

    def update_jobs_for_conversation(self, conversation_id: int, status: str, error: str | None = None, worker_id: str | None = None) -> None:
        self.job_statuses.append(status)

    def update_events_for_conversation(self, conversation_id: int, status: str, error: str | None = None) -> None:
        self.event_status = status


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def set(self, key: str, value: str, nx: bool = False, ex: int | None = None) -> bool:
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def delete(self, key: str) -> None:
        self.values.pop(key, None)

    def exists(self, key: str) -> bool:
        return key in self.values


class ChatwootProductionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_store = main.chat_memory_store
        self.previous_secret = main.settings.chatwoot_webhook_secret
        main.settings.chatwoot_webhook_secret = None

    def tearDown(self) -> None:
        main.chat_memory_store = self.previous_store
        main.settings.chatwoot_webhook_secret = self.previous_secret

    def test_webhook_queues_but_does_not_process(self) -> None:
        store = WebhookStore()
        main.chat_memory_store = store  # type: ignore[assignment]
        with patch.object(chatwoot_tasks, "set_conversation_debounce") as debounce, patch.object(
            chatwoot_tasks.process_chatwoot_conversation, "apply_async"
        ) as apply_async, patch.object(main, "run_agent") as run_agent:
            response = asyncio.run(main.chatwoot_webhook(FakeRequest(chatwoot_payload())))

        self.assertEqual(response.status, "queued")
        self.assertEqual(len(store.messages), 1)
        self.assertEqual(store.jobs, 1)
        debounce.assert_called_once_with(123)
        apply_async.assert_called_once()
        run_agent.assert_not_called()

    def test_webhook_accepts_query_token_when_signature_is_missing(self) -> None:
        store = WebhookStore()
        main.chat_memory_store = store  # type: ignore[assignment]
        main.settings.chatwoot_webhook_secret = "secret"
        with patch.object(chatwoot_tasks, "set_conversation_debounce"), patch.object(
            chatwoot_tasks.process_chatwoot_conversation, "apply_async"
        ):
            response = asyncio.run(main.chatwoot_webhook(FakeRequest(chatwoot_payload(), query_params={"token": "secret"})))

        self.assertEqual(response.status, "queued")

    def test_duplicate_event_does_not_create_message_or_task(self) -> None:
        store = WebhookStore(duplicate=True)
        main.chat_memory_store = store  # type: ignore[assignment]
        with patch.object(chatwoot_tasks.process_chatwoot_conversation, "apply_async") as apply_async:
            response = asyncio.run(main.chatwoot_webhook(FakeRequest(chatwoot_payload())))

        self.assertEqual(response.status, "duplicate")
        self.assertEqual(store.messages, [])
        self.assertEqual(store.jobs, 0)
        apply_async.assert_not_called()

    def test_debounce_processing_groups_pending_messages(self) -> None:
        store = ProcessingStore()
        seen_requests: list[AgentRequest] = []

        def run_agent(request: AgentRequest) -> AgentResponse:
            seen_requests.append(request)
            return AgentResponse(answer="respuesta")

        outbox_id = process_pending_conversation_messages(store, 123, run_agent)  # type: ignore[arg-type]

        self.assertEqual(outbox_id, 1)
        self.assertEqual(store.processed_ids, [1, 2])
        self.assertEqual(store.assistant_messages, ["respuesta"])
        self.assertIn("consulta uno\nconsulta dos", seen_requests[0].message)
        self.assertEqual(store.job_statuses[-1], "completed")

    def test_openai_configured_passes_incomplete_product_intake_to_agent(self) -> None:
        store = ProcessingStore()
        store.pending = [ChatMessage(1, 123, "10", "user", "Estoy buscando pisos con diseño para cubrir 7m2", {})]
        seen_requests: list[AgentRequest] = []

        def run_agent(request: AgentRequest) -> AgentResponse:
            seen_requests.append(request)
            return AgentResponse(answer="respuesta conversacional")

        with patch.object(main.settings, "openai_api_key", "sk-test"):
            outbox_id = process_pending_conversation_messages(store, 123, run_agent)  # type: ignore[arg-type]

        self.assertEqual(outbox_id, 1)
        self.assertEqual(store.assistant_messages, ["respuesta conversacional"])
        self.assertEqual(len(seen_requests), 1)
        self.assertIn("Estoy buscando pisos", seen_requests[0].message)
        self.assertIn("last_question", store.state)
        self.assertEqual(store.job_statuses[-1], "completed")

    def test_openai_configured_preserves_real_short_reply_context_for_agent(self) -> None:
        store = ProcessingStore()
        store.pending = [ChatMessage(1, 123, "10", "user", "2 y 2", {})]
        previous_question = "¿Qué espesor y ancho buscás? Por ejemplo: 3 mm y 1,20 m."
        store.recent_history = lambda conversation_id, limit: [  # type: ignore[method-assign]
            AgentMessage(role="user", content="Estoy buscando pisos con diseño para cubrir 7m2"),
            AgentMessage(role="assistant", content=previous_question),
        ]
        store.get_conversation = lambda conversation_id: ChatConversation(  # type: ignore[method-assign]
            id=conversation_id,
            channel="chatwoot",
            external_conversation_id="99",
            state={
                "intent": "pisos",
                "known": {"rubro": "pisos", "floor_kind": "diseno", "requested_m2": 7},
                "missing": ["espesor_mm", "ancho_m"],
                "last_question": previous_question,
                "should_search": False,
            },
            account_id="7",
        )
        seen_requests: list[AgentRequest] = []

        def run_agent(request: AgentRequest) -> AgentResponse:
            seen_requests.append(request)
            return AgentResponse(answer="busco con contexto")

        with patch.object(main.settings, "openai_api_key", "sk-test"):
            process_pending_conversation_messages(store, 123, run_agent)  # type: ignore[arg-type]

        self.assertEqual(seen_requests[0].message, "2 y 2")
        self.assertIn(previous_question, [message.content for message in seen_requests[0].history])
        self.assertFalse(any(message.content.startswith("Datos ya recopilados:") for message in seen_requests[0].history))

    def test_new_message_during_generation_supersedes_the_turn(self) -> None:
        store = ProcessingStore()
        marked_pending: list[list[int]] = []
        store.mark_messages_pending = lambda ids, error=None: marked_pending.append(ids)  # type: ignore[assignment]

        def run_agent(request: AgentRequest) -> AgentResponse:
            # El cliente escribe MIENTRAS se genera la respuesta.
            store.pending.append(ChatMessage(3, 123, "12", "user", "ah y también necesito pegamento", {}))
            return AgentResponse(answer="respuesta ya vieja")

        outbox_id = process_pending_conversation_messages(store, 123, run_agent)  # type: ignore[arg-type]

        self.assertIsNone(outbox_id)
        self.assertEqual(store.assistant_messages, [])
        self.assertEqual(store.outbox_ids, {})
        self.assertEqual(marked_pending, [[1, 2]])
        self.assertEqual(store.job_statuses[-1], "completed")

    def test_outbox_idempotency_key_prevents_duplicate_outbox_rows(self) -> None:
        store = ProcessingStore()

        def run_agent(request: AgentRequest) -> AgentResponse:
            return AgentResponse(answer="misma respuesta")

        first = process_pending_conversation_messages(store, 123, run_agent)  # type: ignore[arg-type]
        second = process_pending_conversation_messages(store, 123, run_agent)  # type: ignore[arg-type]

        self.assertEqual(first, second)
        self.assertEqual(len(store.outbox_ids), 1)

    def test_dragonfly_lock_rejects_parallel_processing(self) -> None:
        fake_redis = FakeRedis()
        with patch.object(chatwoot_tasks, "dragonfly", return_value=fake_redis):
            with chatwoot_tasks.conversation_lock("123", "task-1") as first:
                with chatwoot_tasks.conversation_lock("123", "task-2") as second:
                    self.assertTrue(first)
                    self.assertFalse(second)

    def test_scheduler_requeues_stale_processing_jobs(self) -> None:
        store = Mock()
        store.requeue_stale_jobs.return_value = [123, 456]
        with patch.object(chatwoot_tasks, "memory_store", return_value=store), patch.object(
            chatwoot_tasks.process_chatwoot_conversation, "apply_async"
        ) as apply_async:
            result = chatwoot_tasks.retry_stale_processing_jobs.run()

        self.assertEqual(result["requeued"], 2)
        self.assertEqual(apply_async.call_count, 2)

    def test_sweep_rescues_stranded_pending_and_dedups(self) -> None:
        store = Mock()
        store.due_job_conversation_ids.return_value = [123]
        store.requeue_stale_jobs.return_value = []
        # 123 ya está en due_job (job failed re-marcado) y 456 solo aparece por mensajes pending varados.
        store.due_stranded_pending_conversation_ids.return_value = [123, 456]
        with patch.object(chatwoot_tasks, "memory_store", return_value=store), patch.object(
            chatwoot_tasks, "set_conversation_debounce"
        ), patch.object(chatwoot_tasks.process_chatwoot_conversation, "apply_async") as apply_async:
            result = chatwoot_tasks.requeue_stuck_conversation_jobs.run()

        self.assertEqual(result["requeued"], 2)  # 123 deduplicado, no encolado dos veces
        self.assertEqual(apply_async.call_count, 2)

    def test_outbox_sender_skips_already_sent_messages(self) -> None:
        store = Mock()
        store.get_outbox_message.return_value = ChatOutboxMessage(
            id=1,
            conversation_id=123,
            external_conversation_id="99",
            channel="chatwoot",
            content="hola",
            status="sent",
            idempotency_key="k",
            attempts=1,
            max_attempts=5,
        )
        with patch.object(chatwoot_tasks, "memory_store", return_value=store):
            result = chatwoot_tasks.send_chatwoot_outbound_message.run("1")

        self.assertEqual(result["status"], "already_sent")
        store.mark_outbox_processing.assert_not_called()


if __name__ == "__main__":
    unittest.main()
