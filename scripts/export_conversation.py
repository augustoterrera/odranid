"""Exporta una conversación real de chat_messages como caso de eval.

Flujo de incidentes (docs/ROADMAP_CALIDAD_AGENTE.md, Fase 3):
    1. exportar:   PYTHONPATH=. uv run python scripts/export_conversation.py --conversation 1234
    2. declarar:   editar los `asserts` del YAML generado (qué DEBERÍA haber hecho el bot)
    3. rojo:       make eval-case CASE=incident_1234  (debe fallar reproduciendo el problema)
    4. fix:        según el árbol de decisión del roadmap (guard / extractor / prompt / estado)
    5. verde:      make eval  (el caso nuevo pasa y el set entero sigue verde)

Por defecto el último turno del cliente queda como `message` (los mensajes user consecutivos
del final se unen con \\n, igual que hace el debounce) y todo lo anterior queda como history.
Si el incidente fue en un turno anterior, recortá el YAML a mano.

La URL de la base sale de ODRANID_DATABASE_URL (entorno o .env). Contra producción,
correr por la tailnet apuntando al Postgres del VPS.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import psycopg
import yaml
from psycopg.rows import dict_row

CASES_DIR = Path("evals/cases")


def database_url() -> str:
    url = os.environ.get("ODRANID_DATABASE_URL")
    if not url:
        env_file = Path(".env")
        if env_file.exists():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("ODRANID_DATABASE_URL="):
                    url = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    if not url:
        sys.exit("Falta ODRANID_DATABASE_URL (entorno o .env)")
    # El host "postgres" solo existe dentro de la red del compose; desde el host el
    # contenedor está mapeado a 127.0.0.1:5432.
    if "@postgres:" in url and not Path("/.dockerenv").exists():
        url = url.replace("@postgres:", "@localhost:")
    return url


def fetch_messages(conversation: str, channel: str) -> list[dict]:
    with psycopg.connect(database_url(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                select m.role, m.content
                from public.chat_messages m
                join public.chat_conversations c on c.id = m.conversation_id
                where (c.external_conversation_id = %(conv)s or c.id::text = %(conv)s)
                  and c.channel = %(channel)s
                  and m.role in ('user', 'assistant')
                order by m.created_at
                """,
                {"conv": conversation, "channel": channel},
            )
            return [row for row in cur.fetchall() if str(row["content"] or "").strip()]


def split_case(messages: list[dict]) -> tuple[list[dict], str]:
    """Separa history y message: el bloque final de mensajes user consecutivos es el
    `message` (unidos con \\n como en el debounce); lo anterior, history. Los mensajes
    assistant posteriores al último turno user (la respuesta mala) se descartan."""
    last_user = max((i for i, m in enumerate(messages) if m["role"] == "user"), default=None)
    if last_user is None:
        sys.exit("La conversación no tiene mensajes del cliente")
    start = last_user
    while start > 0 and messages[start - 1]["role"] == "user":
        start -= 1
    history = [{"role": m["role"], "content": m["content"]} for m in messages[:start]]
    message = "\n".join(m["content"] for m in messages[start : last_user + 1])
    return history, message


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--conversation", required=True, help="ID de conversación de Chatwoot (o id interno)")
    parser.add_argument("--channel", default="chatwoot")
    parser.add_argument("--name", default=None, help="Nombre del caso (default: incident_<id>)")
    args = parser.parse_args()

    messages = fetch_messages(args.conversation, args.channel)
    if not messages:
        sys.exit(f"No hay mensajes para la conversación {args.conversation!r}")
    history, message = split_case(messages)

    name = args.name or f"incident_{args.conversation}"
    case_file = CASES_DIR / f"{name}.yaml"
    if case_file.exists():
        sys.exit(f"{case_file} ya existe; usá --name para otro nombre")

    body = yaml.safe_dump(
        {"history": history, "message": message, "asserts": []},
        allow_unicode=True,
        sort_keys=False,
        width=100,
    )
    header = (
        f"# Incidente exportado de la conversación {args.conversation} ({args.channel}).\n"
        "# TODO: describir acá qué hizo mal el bot y completar `asserts` con el comportamiento\n"
        "# esperado (ver evals/README.md). El caso debe fallar ANTES del fix.\n"
    )
    case_file.write_text(header + body, encoding="utf-8")
    print(f"caso generado: {case_file}")
    print(f"  turnos en history: {len(history)} | message: {message[:80]!r}")
    print(f"siguiente paso: editar asserts y correr  make eval-case CASE={name}")


if __name__ == "__main__":
    main()
