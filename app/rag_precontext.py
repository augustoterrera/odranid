from __future__ import annotations

import json
from typing import Any

from .core.models import AgentRequest, ProductIntakeResponse


def build_rag_precontext(
    request: AgentRequest,
    search_query: str,
    intake: ProductIntakeResponse | None = None,
    search_error: str | None = None,
) -> str:
    payload: dict[str, Any] = {
        "latest_user_message": request.message,
        "suggested_search_query": search_query,
    }
    if request.history:
        payload["recent_conversation"] = [
            {"role": message.role, "content": message.content}
            for message in request.history[-8:]
            if message.content.strip()
        ]
    if intake is not None:
        payload["microservice_intake"] = {
            "intent": intake.intent,
            "known": intake.known,
            "missing": intake.missing,
            "should_search": intake.should_search,
            "next_question": intake.next_question,
            "confidence": intake.confidence,
        }
    if search_error:
        payload["initial_rag_error"] = search_error

    should_search = (intake is not None) and bool(intake.should_search)

    lines = ["## PRECONTEXTO RAG DE LA CONVERSACION", ""]

    if should_search:
        lines += [
            "El microservicio detectó que esto es una consulta de producto/disponibilidad.",
            f"Si vas a hablar de productos, stock o disponibilidad, primero llamá `buscar_productos` (query sugerida: \"{search_query}\").",
            "Nunca afirmes qué hay o no hay en stock desde tu conocimiento: usá la herramienta.",
            "Si en realidad es un mensaje operativo o institucional (visita, precio, envío, asesor), respondé eso y no busques.",
            "",
        ]

    lines += [
        "Contexto de la conversación (usar para interpretar datos ya dados por el cliente):",
        "",
        "Reglas:",
        "- Tomá `recent_conversation` como memoria de trabajo para interpretar respuestas cortas.",
        "- Si el cliente ya dijo rubro/producto, no preguntes de nuevo qué producto busca.",
        "- `next_question` es sugerencia del microservicio, no obligación literal.",
        "- Si faltan datos, preguntá solo los más útiles.",
        "",
        "```json",
        json.dumps(payload, ensure_ascii=False, indent=2),
        "```",
    ]

    return "\n".join(lines)
