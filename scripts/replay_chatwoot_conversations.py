from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.chat_memory import (  # noqa: E402
    apply_pending_slot_to_message,
    build_memory_state,
    pending_slot_from_intake,
    should_reset_conversation_state,
)
from app.chatwoot import chatwoot_message_role  # noqa: E402
from app.config import settings  # noqa: E402
from app.main import configure_search, run_agent  # noqa: E402
from app.models import AgentMessage, AgentRequest, ProductIntakeResponse  # noqa: E402


class ReplayError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReplayTurn:
    conversation_id: int | str
    message_id: int | str | None
    user_message: str
    history: list[AgentMessage]
    state_before: dict[str, Any]
    expected_assistant: str | None = None


class ChatwootApi:
    def __init__(self, base_url: str, account_id: int | str, token: str, timeout_seconds: int = 30) -> None:
        self.base_url = base_url.rstrip("/")
        self.account_id = account_id
        self.token = token
        self.timeout_seconds = timeout_seconds

    def list_conversations(self, limit: int, status: str | None = None) -> list[dict[str, Any]]:
        params = {"page": 1}
        if status:
            params["status"] = status
        endpoint = f"{self.base_url}/api/v1/accounts/{self.account_id}/conversations?{urlencode(params)}"
        payload = self.get_json(endpoint)
        conversations = extract_collection(payload)
        return conversations[:limit]

    def get_conversation(self, conversation_id: int | str) -> dict[str, Any]:
        endpoint = f"{self.base_url}/api/v1/accounts/{self.account_id}/conversations/{conversation_id}"
        payload = self.get_json(endpoint)
        if isinstance(payload, dict) and isinstance(payload.get("payload"), dict):
            return payload["payload"]
        if isinstance(payload, dict):
            return payload
        raise ReplayError(f"Conversation {conversation_id} returned unexpected payload")

    def get_messages(self, conversation_id: int | str) -> list[dict[str, Any]]:
        endpoint = f"{self.base_url}/api/v1/accounts/{self.account_id}/conversations/{conversation_id}/messages"
        payload = self.get_json(endpoint)
        return extract_collection(payload)

    def get_json(self, endpoint: str) -> Any:
        request = Request(
            endpoint,
            headers={
                "api_access_token": self.token,
                "content-type": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ReplayError(f"Chatwoot HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise ReplayError(f"Could not connect to Chatwoot: {exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise ReplayError("Chatwoot returned invalid JSON") from exc


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay real Chatwoot conversations against the Odranid agent without sending replies.")
    parser.add_argument("--conversation-id", action="append", default=[], help="Conversation ID to replay. Can be used multiple times.")
    parser.add_argument("--from-file", type=Path, default=None, help="Read conversation IDs from a Chatwoot conversations JSON export.")
    parser.add_argument(
        "--use-file-messages",
        action="store_true",
        help="Replay the messages embedded in --from-file instead of fetching full messages from Chatwoot.",
    )
    parser.add_argument("--recent", type=int, default=0, help="Fetch this many recent conversations when no IDs are provided.")
    parser.add_argument("--status", default=None, help="Optional Chatwoot status filter for --recent, e.g. open/resolved/pending.")
    parser.add_argument("--limit", type=int, default=0, help="Limit selected conversations after filtering.")
    parser.add_argument("--offset", type=int, default=0, help="Skip this many selected conversations after filtering.")
    parser.add_argument("--mode", choices=["last-incoming", "all-incoming"], default="last-incoming")
    parser.add_argument("--intake-only", action="store_true", help="Do not call OpenAI; only test deterministic intake and memory.")
    parser.add_argument("--max-history", type=int, default=8)
    parser.add_argument("--output", type=Path, default=ROOT / "reports" / "chatwoot_replay.jsonl")
    parser.add_argument("--quiet", action="store_true", help="Do not print one line per conversation/turn.")
    args = parser.parse_args()

    needs_chatwoot_api = not (args.from_file and args.use_file_messages)
    if needs_chatwoot_api and (
        not settings.chatwoot_base_url
        or not settings.chatwoot_account_id
        or not settings.chatwoot_api_access_token
    ):
        raise ReplayError("ODRANID_CHATWOOT_BASE_URL, ODRANID_CHATWOOT_ACCOUNT_ID and ODRANID_CHATWOOT_API_ACCESS_TOKEN are required")

    if not args.intake_only:
        configure_search()

    api = None
    if needs_chatwoot_api:
        api = ChatwootApi(
            base_url=settings.chatwoot_base_url,
            account_id=settings.chatwoot_account_id,
            token=settings.chatwoot_api_access_token,
        )

    selected_rows = select_conversation_rows(args, api)
    if not selected_rows:
        raise ReplayError("No conversations selected")
    selected_rows = selected_rows[max(0, args.offset) :]
    if args.limit > 0:
        selected_rows = selected_rows[: args.limit]
    if not selected_rows:
        raise ReplayError("No conversations selected after applying --offset/--limit")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    total_turns = 0
    with args.output.open("w", encoding="utf-8") as output_file:
        for row in selected_rows:
            conversation_id = conversation_id_from_row(row)
            if args.from_file and args.use_file_messages:
                messages = conversation_messages_from_row(row)
            else:
                if api is None:
                    raise ReplayError("Chatwoot API client is required to fetch full conversation messages")
                messages = api.get_messages(conversation_id)
            records = replay_conversation(conversation_id, messages, args.mode, args.max_history, args.intake_only)
            if not args.quiet:
                print(f"conversation={conversation_id} messages={len(messages)} turns={len(records)}")
            for record in records:
                output_file.write(json.dumps(record, ensure_ascii=False) + "\n")
                total_turns += 1
                if not args.quiet:
                    print(
                        "  "
                        f"message={record.get('message_id')} "
                        f"intent={record.get('intake', {}).get('intent')} "
                        f"should_search={record.get('intake', {}).get('should_search')} "
                        f"tool_calls={len(record.get('agent', {}).get('tool_calls', []))} "
                        f"error={record.get('error')}"
                    )

    print(f"replay_ok=true conversations={len(selected_rows)} turns={total_turns} output={args.output}")


def select_conversation_rows(args: argparse.Namespace, api: ChatwootApi | None) -> list[dict[str, Any]]:
    if args.from_file:
        rows = load_conversations_from_file(args.from_file)
        if args.conversation_id:
            allowed_ids = {str(value) for value in args.conversation_id}
            rows = [row for row in rows if str(conversation_id_from_row(row)) in allowed_ids]
        return rows

    if args.conversation_id:
        return [{"id": value} for value in args.conversation_id]

    if api is None:
        raise ReplayError("Chatwoot API client is required to list recent conversations")
    return api.list_conversations(args.recent or 10, args.status)


def replay_conversation(
    conversation_id: int | str,
    raw_messages: list[dict[str, Any]],
    mode: str,
    max_history: int,
    intake_only: bool,
) -> list[dict[str, Any]]:
    messages = sorted(raw_messages, key=message_sort_key)
    state: dict[str, Any] = {}
    history: list[AgentMessage] = []
    records: list[dict[str, Any]] = []
    last_record: dict[str, Any] | None = None

    for index, message in enumerate(messages):
        role = replay_message_role(message)
        content = message_content(message)
        if role is None or not content or is_private_or_non_text(message):
            continue

        if role == "user":
            if should_reset_conversation_state(content, state):
                state = {}
                history = []

            turn = ReplayTurn(
                conversation_id=conversation_id,
                message_id=message.get("id"),
                user_message=content,
                history=history[-max_history:],
                state_before=state,
                expected_assistant=next_assistant_reply(messages, index),
            )
            record = replay_turn(turn, intake_only=intake_only)
            state_after = record.get("state_after")
            if isinstance(state_after, dict):
                state = state_after
            if mode == "all-incoming":
                records.append(record)
            elif mode == "last-incoming":
                last_record = record

        history.append(AgentMessage(role=role, content=content))

    if mode == "last-incoming" and last_record is not None:
        return [last_record]
    return records


def replay_turn(turn: ReplayTurn, intake_only: bool) -> dict[str, Any]:
    if intake_only:
        intake = ProductIntakeResponse()
        state_after = turn.state_before
    else:
        intake = ProductIntakeResponse()
        state_after = turn.state_before
    record: dict[str, Any] = {
        "conversation_id": turn.conversation_id,
        "message_id": turn.message_id,
        "user_message": turn.user_message,
        "expected_assistant": turn.expected_assistant,
        "history": [message.model_dump() for message in turn.history],
        "state_before": turn.state_before,
        "intake": intake.model_dump(),
        "state_after": state_after,
    }
    if intake_only:
        return record

    try:
        # Cutover pipeline: the single PydanticAI agent returns both answer and intake.
        response = run_agent(
            AgentRequest(
                message=apply_pending_slot_to_message(turn.user_message, turn.state_before),
                history=turn.history,
                limit=settings.chatwoot_agent_limit,
            )
        )
        intake = response.intake or ProductIntakeResponse()
        state_after = build_memory_state(turn.state_before, intake, pending_slot_from_intake(intake))
        record["intake"] = intake.model_dump()
        record["state_after"] = state_after
        record["agent"] = {
            "answer": response.answer,
            "tool_calls": [trace.model_dump() for trace in response.tool_calls],
        }
    except Exception as exc:
        record["error"] = str(exc)
    return record


def extract_collection(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        rows = [row for row in payload if isinstance(row, dict)]
        if len(rows) == 1:
            normalized_row = normalize_n8n_row(rows[0])
            if is_conversation_row(normalized_row):
                return [normalized_row]
            nested_rows = extract_collection_from_dict(normalized_row)
            if nested_rows:
                return nested_rows
        return rows
    if isinstance(payload, dict):
        return extract_collection_from_dict(payload)
    return []


def extract_collection_from_dict(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if is_conversation_row(payload):
        return [payload]
    for key in ["json", "payload", "data", "messages", "conversations"]:
        value = payload.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
        if isinstance(value, dict):
            nested_rows = extract_collection_from_dict(value)
            if nested_rows:
                return nested_rows
    return []


def is_conversation_row(row: dict[str, Any]) -> bool:
    has_id = row.get("id") is not None or row.get("conversation_id") is not None
    return has_id and isinstance(row.get("messages"), list)


def load_conversations_from_file(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ReplayError(f"Could not read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ReplayError(f"{path} is not valid JSON: {exc}") from exc

    rows = [normalize_n8n_row(row) for row in extract_collection(payload)]
    conversations = [row for row in rows if row.get("id") is not None or row.get("conversation_id") is not None]
    if not conversations:
        raise ReplayError(f"{path} does not contain Chatwoot conversation rows")
    return conversations


def normalize_n8n_row(row: dict[str, Any]) -> dict[str, Any]:
    value = row.get("json")
    if isinstance(value, dict):
        return value
    return row


def conversation_messages_from_row(row: dict[str, Any]) -> list[dict[str, Any]]:
    messages = row.get("messages")
    if not isinstance(messages, list):
        return []
    return [message for message in messages if isinstance(message, dict)]


def conversation_id_from_row(row: dict[str, Any]) -> int | str:
    value = row.get("id") or row.get("conversation_id")
    if value is None:
        raise ReplayError(f"Conversation row without id: {row}")
    return value


def message_sort_key(message: dict[str, Any]) -> tuple[int, int]:
    created_at = message.get("created_at")
    try:
        created = int(float(created_at))
    except (TypeError, ValueError):
        created = 0
    try:
        message_id = int(message.get("id") or 0)
    except (TypeError, ValueError):
        message_id = 0
    return created, message_id


def is_private_or_non_text(message: dict[str, Any]) -> bool:
    if bool(message.get("private")):
        return True
    return str(message.get("content_type") or "text") != "text"


def replay_message_role(message: dict[str, Any]) -> str | None:
    role = str(message.get("role") or "").strip().lower()
    if role in {"user", "assistant"}:
        return role
    return chatwoot_message_role(message)


def message_content(message: dict[str, Any]) -> str:
    return str(message.get("content") or message.get("processed_message_content") or "").strip()


def next_assistant_reply(messages: list[dict[str, Any]], user_index: int) -> str | None:
    for message in messages[user_index + 1 :]:
        role = replay_message_role(message)
        content = message_content(message)
        if role is None or not content or is_private_or_non_text(message):
            continue
        if role == "assistant":
            return content
        if role == "user":
            return None
    return None


if __name__ == "__main__":
    main()
