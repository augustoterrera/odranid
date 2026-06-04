from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .models import AgentMessage, AgentResponse


class ChatwootError(RuntimeError):
    pass


@dataclass(frozen=True)
class ChatwootMessageEvent:
    event: str
    message_id: int | str | None
    conversation_id: int | str
    account_id: int | str | None
    content: str
    history: list[AgentMessage]


@dataclass(frozen=True)
class ChatwootClient:
    base_url: str
    api_access_token: str
    timeout_seconds: int = 30

    def create_outgoing_message(
        self,
        account_id: int | str,
        conversation_id: int | str,
        content: str,
        agent_response: AgentResponse | None = None,
    ) -> dict[str, Any]:
        endpoint = (
            f"{self.base_url.rstrip('/')}/api/v1/accounts/{account_id}"
            f"/conversations/{conversation_id}/messages"
        )
        body = {
            "content": content,
            "message_type": "outgoing",
            "private": False,
            "content_type": "text",
            "content_attributes": {
                "source": "odranid-agent",
                "tool_calls": [trace.model_dump() for trace in agent_response.tool_calls] if agent_response else [],
            },
        }
        request = Request(
            endpoint,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "content-type": "application/json",
                "api_access_token": self.api_access_token,
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ChatwootError(f"Chatwoot create message HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise ChatwootError(f"Could not connect to Chatwoot API: {exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise ChatwootError("Chatwoot API returned invalid JSON") from exc


def verify_chatwoot_signature(
    raw_body: bytes,
    secret: str | None,
    signature: str | None,
    timestamp: str | None,
    tolerance_seconds: int,
    now: float | None = None,
) -> bool:
    if not secret:
        return True
    if not signature or not timestamp:
        return False

    try:
        signed_at = int(timestamp)
    except ValueError:
        return False

    current_time = time.time() if now is None else now
    if tolerance_seconds > 0 and abs(current_time - signed_at) > tolerance_seconds:
        return False

    message = timestamp.encode("utf-8") + b"." + raw_body
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def parse_chatwoot_payload(raw_body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ChatwootError("Invalid Chatwoot webhook JSON") from exc
    if not isinstance(payload, dict):
        raise ChatwootError("Chatwoot webhook payload must be a JSON object")
    return payload


def extract_message_event(payload: dict[str, Any], history_limit: int = 8) -> tuple[ChatwootMessageEvent | None, str | None]:
    event = str(payload.get("event") or "")
    if event != "message_created":
        return None, "ignored_event"

    if str(payload.get("message_type") or "") != "incoming":
        return None, "ignored_non_incoming_message"

    if bool(payload.get("private")):
        return None, "ignored_private_message"

    if str(payload.get("content_type") or "text") != "text":
        return None, "ignored_non_text_message"

    content = str(payload.get("content") or "").strip()
    if not content:
        return None, "ignored_empty_message"

    conversation = payload.get("conversation") if isinstance(payload.get("conversation"), dict) else {}
    account = payload.get("account") if isinstance(payload.get("account"), dict) else {}
    conversation_id = conversation.get("id") or payload.get("conversation_id")
    account_id = account.get("id") or payload.get("account_id")
    if conversation_id is None:
        return None, "missing_conversation_id"

    return (
        ChatwootMessageEvent(
            event=event,
            message_id=payload.get("id"),
            conversation_id=conversation_id,
            account_id=account_id,
            content=content,
            history=extract_history(payload, history_limit),
        ),
        None,
    )


def extract_history(payload: dict[str, Any], limit: int) -> list[AgentMessage]:
    conversation = payload.get("conversation") if isinstance(payload.get("conversation"), dict) else {}
    messages = conversation.get("messages") if isinstance(conversation.get("messages"), list) else []
    current_id = payload.get("id")
    history: list[AgentMessage] = []

    for message in messages:
        if not isinstance(message, dict):
            continue
        if current_id is not None and message.get("id") == current_id:
            continue
        if bool(message.get("private")):
            continue
        if str(message.get("content_type") or "text") != "text":
            continue
        content = str(message.get("content") or "").strip()
        if not content:
            continue
        role = chatwoot_message_role(message)
        if role is None:
            continue
        history.append(AgentMessage(role=role, content=content))

    return history[-max(0, limit) :]


def chatwoot_message_role(message: dict[str, Any]) -> str | None:
    message_type = message.get("message_type")
    if message_type == "incoming" or message_type == 0:
        return "user"
    if message_type == "outgoing" or message_type == 1:
        return "assistant"
    return None


def build_chatwoot_client(base_url: str | None, api_access_token: str | None) -> ChatwootClient | None:
    if not base_url or not api_access_token:
        return None
    return ChatwootClient(base_url=base_url, api_access_token=api_access_token)
