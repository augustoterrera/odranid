#!/usr/bin/env python3
"""Backfill de conversaciones históricas de Chatwoot a la memoria de odranid.

Lee conversaciones y mensajes desde la API de Chatwoot (SOLO lectura) y los siembra
en chat_conversations / chat_messages usando los ids REALES de Chatwoot. Gracias a
eso el dedup con los webhooks futuros es automático: si un cliente sigue una charla
ya migrada, los mensajes nuevos no se duplican (unique key conversation+message+role).

Los mensajes se insertan con processing_status='processed' para que el worker NO los
tome como pendientes (no queremos responder mensajes históricos), y con su created_at
real para preservar el orden.

Uso (dentro del contenedor api, que ya tiene el .env cargado):
  docker compose run --rm api python scripts/backfill_chatwoot_history.py --days 3
  docker compose run --rm api python scripts/backfill_chatwoot_history.py            # todas
  docker compose run --rm api python scripts/backfill_chatwoot_history.py --days 3 --dry-run
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.chat.chat_memory import build_chat_memory_store_from_settings  # noqa: E402
from app.chat.chatwoot import chatwoot_message_role  # noqa: E402
from app.core.config import settings  # noqa: E402

# Todos los estados de conversación de Chatwoot (para traer "todas").
ALL_STATUSES = ["open", "pending", "snoozed", "resolved"]
PAGE_SLEEP_SECONDS = 0.2  # cortesía con la API de Chatwoot


class BackfillError(RuntimeError):
    pass


def _api_get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    base = (settings.chatwoot_base_url or "").rstrip("/")
    if not base or settings.chatwoot_account_id is None or not settings.chatwoot_api_access_token:
        raise BackfillError(
            "Faltan credenciales de Chatwoot en el .env "
            "(ODRANID_CHATWOOT_BASE_URL / ACCOUNT_ID / API_ACCESS_TOKEN)."
        )
    url = f"{base}/api/v1/accounts/{settings.chatwoot_account_id}{path}"
    if params:
        url = f"{url}?{urlencode({k: v for k, v in params.items() if v is not None})}"
    req = Request(url, headers={"api_access_token": settings.chatwoot_api_access_token})
    try:
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        raise BackfillError(f"Chatwoot HTTP {exc.code} en {path}: {exc.read()[:200]!r}") from exc
    except URLError as exc:
        raise BackfillError(f"No se pudo alcanzar Chatwoot en {path}: {exc}") from exc


def _payload(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Extrae la lista 'payload' tanto del formato {'data': {'payload': []}}
    (lista de conversaciones) como del formato plano {'payload': []} (mensajes)."""
    container = data.get("data") if isinstance(data.get("data"), dict) else data
    payload = container.get("payload") if isinstance(container, dict) else None
    return payload if isinstance(payload, list) else []


def iter_conversations(status: str) -> Iterator[dict[str, Any]]:
    page = 1
    while True:
        data = _api_get("/conversations", {"status": status, "page": page})
        items = _payload(data)
        if not items:
            return
        for conv in items:
            if isinstance(conv, dict):
                yield conv
        page += 1
        time.sleep(PAGE_SLEEP_SECONDS)


def fetch_all_messages(conversation_id: int) -> list[dict[str, Any]]:
    """Trae TODOS los mensajes de una conversación, paginando hacia atrás con `before`
    (el endpoint devuelve los últimos ~20 y se retrocede por el id mínimo)."""
    collected: list[dict[str, Any]] = []
    before: int | None = None
    while True:
        data = _api_get(f"/conversations/{conversation_id}/messages", {"before": before})
        items = _payload(data)
        if not items:
            break
        collected = items + collected
        ids = [m["id"] for m in items if isinstance(m, dict) and isinstance(m.get("id"), int)]
        if not ids:
            break
        min_id = min(ids)
        if before is not None and min_id >= before:
            break  # sin progreso, cortar
        before = min_id
        if len(items) < 20:
            break  # última página
        time.sleep(PAGE_SLEEP_SECONDS)
    return collected


def message_created_at(message: dict[str, Any]) -> datetime | None:
    ts = message.get("created_at")
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    return None


def usable_text(message: dict[str, Any]) -> str | None:
    """Mismo filtrado que extract_history: solo texto real, sin notas privadas."""
    if bool(message.get("private")):
        return None
    if str(message.get("content_type") or "text") != "text":
        return None
    content = str(message.get("content") or "").strip()
    return content or None


def conversation_last_activity(conv: dict[str, Any]) -> float | None:
    """Timestamp (unix) de la última actividad de la conversación, para filtrar por fecha.
    Usa last_activity_at/timestamp del objeto; si no están, cae al created_at más nuevo
    de los mensajes que vienen embebidos en el listado."""
    for key in ("last_activity_at", "timestamp"):
        value = conv.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return float(value)
    inline = conv.get("messages") if isinstance(conv.get("messages"), list) else []
    stamps = [
        m["created_at"]
        for m in inline
        if isinstance(m, dict) and isinstance(m.get("created_at"), (int, float))
    ]
    return float(max(stamps)) if stamps else None


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill de conversaciones de Chatwoot a la memoria de odranid.")
    parser.add_argument(
        "--status",
        default="all",
        help="Estado a migrar: open|pending|snoozed|resolved|all (default: all).",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=0,
        help="Solo conversaciones con actividad en los últimos N días (0 = sin filtro de fecha).",
    )
    parser.add_argument("--limit", type=int, default=0, help="Máximo de conversaciones (0 = sin tope). Útil para probar.")
    parser.add_argument("--dry-run", action="store_true", help="No escribe en la DB; solo cuenta qué migraría.")
    args = parser.parse_args()

    store = build_chat_memory_store_from_settings(settings)
    if store is None:
        raise BackfillError("No hay ODRANID_DATABASE_URL configurada; no se puede escribir la memoria.")

    statuses = ALL_STATUSES if args.status == "all" else [args.status]
    account_id = settings.chatwoot_account_id
    cutoff = (time.time() - args.days * 86400) if args.days > 0 else None

    seen_conversations: set[int] = set()
    convs_done = 0
    msgs_done = 0

    for status in statuses:
        for conv in iter_conversations(status):
            conv_id = conv.get("id")
            if not isinstance(conv_id, int) or conv_id in seen_conversations:
                continue
            seen_conversations.add(conv_id)
            if cutoff is not None:
                activity = conversation_last_activity(conv)
                if activity is None or activity < cutoff:
                    continue
            if args.limit and convs_done >= args.limit:
                break

            sender = ((conv.get("meta") or {}).get("sender")) or {}
            contact_id = sender.get("id")
            phone = sender.get("phone_number")

            messages = fetch_all_messages(conv_id)
            inserted = 0
            for message in messages:
                role = chatwoot_message_role(message)
                if role is None:
                    continue
                content = usable_text(message)
                if content is None:
                    continue
                if not args.dry_run:
                    if inserted == 0:
                        conversation = store.get_or_create_conversation(
                            channel="chatwoot",
                            external_conversation_id=conv_id,
                            external_contact_id=contact_id,
                            account_id=account_id,
                        )
                    store.add_message(
                        conversation.id,
                        role=role,
                        content=content,
                        external_message_id=message.get("id"),
                        processing_status="processed",
                        created_at=message_created_at(message),
                    )
                inserted += 1

            if inserted:
                convs_done += 1
                msgs_done += inserted
                print(f"[{status}] conv {conv_id} ({phone or 's/tel'}): {inserted} mensajes")
            if args.limit and convs_done >= args.limit:
                break

    prefix = "DRY-RUN: " if args.dry_run else ""
    print(f"\n{prefix}listo. Conversaciones: {convs_done} | mensajes: {msgs_done}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BackfillError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
