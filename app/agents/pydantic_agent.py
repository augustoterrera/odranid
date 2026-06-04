from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from pydantic_ai import Agent, RunContext
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from ..agent import AgentError, build_system_prompt, clamp_int, compact_search_response
from ..coverage import calculate_coverage
from ..models import (
    AgentMessage,
    AgentRequest,
    AgentResponse,
    AgentToolTrace,
    ProductFilters,
    ProductIntakeResponse,
    SearchRequest,
    SearchResponse,
)

SearchCallable = Callable[[SearchRequest], SearchResponse]


class OdranidAgentOutput(BaseModel):
    intake: ProductIntakeResponse = Field(default_factory=ProductIntakeResponse)
    answer: str


@dataclass
class OdranidAgentDeps:
    search: SearchCallable
    default_limit: int
    max_limit: int
    tool_calls: list[AgentToolTrace] = field(default_factory=list)
    search_responses: list[SearchResponse] = field(default_factory=list)


PYDANTIC_AGENT_INSTRUCTIONS = """\
Además de responder al cliente, devolvé siempre un `intake` estructurado compatible con ProductIntakeResponse:
- `intent`, `known`, `missing`, `should_search`, `next_question`, `confidence`.
- `known` conserva los mismos nombres de slots ya usados por Odranid.

Cuando busques productos, llamá `buscar_productos` con argumentos estructurados. No escondas filtros dentro
de una query libre: emití rubro, tipo/floor_kind/floor_design, espesor_mm, ancho_m, material, color, tags,
requested_m2 y query_semantica cuando correspondan.

Si no hace falta buscar, `answer` debe ser la respuesta final breve. Si falta información, `answer` puede ser
la `next_question`.
"""


def run_pydantic_agent(
    request: AgentRequest,
    search: SearchCallable,
    api_key: str,
    catalog_context: str,
    model: str = "gpt-4.1-mini",
    prompt_file: Path | None = None,
    pydantic_model: Model | None = None,
) -> AgentResponse:
    """Run the single PydanticAI agent without the Agno team."""
    if prompt_file is None:
        prompt_file = Path("prompt_agente_odranid.md")

    deps = OdranidAgentDeps(search=search, default_limit=request.limit, max_limit=request.limit)
    agent = build_agent(
        model=pydantic_model or build_openai_model(model, api_key),
        system_prompt=build_pydantic_system_prompt(prompt_file, catalog_context),
    )

    try:
        result = agent.run_sync(build_user_prompt(request), deps=deps)
    except Exception as exc:
        raise AgentError(f"PydanticAI agent run failed: {exc}") from exc

    output = result.output
    if not output.answer.strip():
        raise AgentError("PydanticAI agent response did not include final text")

    return AgentResponse(
        answer=output.answer.strip(),
        tool_calls=deps.tool_calls,
        intake=output.intake,
    )


def build_agent(model: Model, system_prompt: str) -> Agent[OdranidAgentDeps, OdranidAgentOutput]:
    agent = Agent(
        model=model,
        output_type=OdranidAgentOutput,
        deps_type=OdranidAgentDeps,
        system_prompt=system_prompt,
    )

    @agent.tool
    async def buscar_productos(
        ctx: RunContext[OdranidAgentDeps],
        query_semantica: str,
        rubro: str | None = None,
        tipo: str | None = None,
        floor_kind: str | None = None,
        floor_design: str | None = None,
        espesor_mm: float | None = None,
        ancho_m: float | None = None,
        material: str | None = None,
        color: str | None = None,
        tags: list[str] | None = None,
        requested_m2: float | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Busca productos reales usando filtros estructurados emitidos por el agente."""
        safe_limit = clamp_int(limit, default=ctx.deps.default_limit, minimum=1, maximum=ctx.deps.max_limit)
        filters = product_filters_from_tool_args(
            rubro=rubro,
            tipo=tipo,
            floor_kind=floor_kind,
            floor_design=floor_design,
            espesor_mm=espesor_mm,
            ancho_m=ancho_m,
            material=material,
            color=color,
            tags=tags or [],
        )
        query = semantic_query_with_requested_m2(query_semantica, requested_m2)
        search_request = SearchRequest(query=query, filters=filters, limit=safe_limit, relax_filters=True)
        response = ctx.deps.search(search_request)
        if requested_m2 is not None:
            apply_requested_coverage(response, requested_m2)

        arguments = {
            "query_semantica": query_semantica,
            "rubro": rubro,
            "tipo": tipo,
            "floor_kind": floor_kind,
            "floor_design": floor_design,
            "espesor_mm": espesor_mm,
            "ancho_m": ancho_m,
            "material": material,
            "color": color,
            "tags": tags or [],
            "requested_m2": requested_m2,
            "limit": safe_limit,
        }
        ctx.deps.tool_calls.append(
            AgentToolTrace(name="buscar_productos", arguments=arguments, result_count=len(response.hits))
        )
        ctx.deps.search_responses.append(response)
        return compact_search_response(response)

    return agent


def build_openai_model(model_name: str, api_key: str) -> OpenAIChatModel:
    return OpenAIChatModel(model_name, provider=OpenAIProvider(api_key=api_key))


def build_pydantic_system_prompt(prompt_file: Path, catalog_context: str) -> str:
    return "\n\n".join([build_system_prompt(prompt_file, catalog_context), PYDANTIC_AGENT_INSTRUCTIONS])


def build_user_prompt(request: AgentRequest) -> str:
    payload = {
        "latest_user_message": request.message,
        "history": [message.model_dump() for message in visible_history(request.history)],
    }
    return "\n".join(
        [
            "Respondé el último mensaje del cliente usando este contexto de conversación.",
            "```json",
            json.dumps(payload, ensure_ascii=False, indent=2),
            "```",
        ]
    )


def visible_history(history: list[AgentMessage]) -> list[AgentMessage]:
    return [message for message in history if not message.content.startswith("Datos ya recopilados:")]


def product_filters_from_tool_args(
    *,
    rubro: str | None,
    tipo: str | None,
    floor_kind: str | None,
    floor_design: str | None,
    espesor_mm: float | None,
    ancho_m: float | None,
    material: str | None,
    color: str | None,
    tags: list[str],
) -> ProductFilters:
    normalized_tipo = normalize_text_value(tipo)
    normalized_floor_kind = normalize_text_value(floor_kind)
    normalized_floor_design = normalize_text_value(floor_design)

    if normalized_floor_kind is None and normalized_tipo in {"liso", "diseno"}:
        normalized_floor_kind = normalized_tipo
    if normalized_floor_design is None and normalized_tipo in {
        "moneda",
        "semilla",
        "rayado",
        "simil_madera",
        "semilla_melon",
    }:
        normalized_floor_design = normalized_tipo
        normalized_floor_kind = normalized_floor_kind or "diseno"

    return ProductFilters(
        rubro=normalize_text_value(rubro),
        floor_kind=normalized_floor_kind,
        floor_design=normalized_floor_design,
        espesor_mm=espesor_mm,
        ancho_m=ancho_m,
        material=normalize_text_value(material),
        color=normalize_text_value(color),
        tags=[tag for tag in (normalize_text_value(value) for value in tags) if tag],
    )


def normalize_text_value(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower().replace("ñ", "n").replace(" ", "_")
    return normalized or None


def semantic_query_with_requested_m2(query: str, requested_m2: float | None) -> str:
    clean_query = query.strip()
    if requested_m2 is None:
        return clean_query
    return f"{clean_query} cubrir {requested_m2:g} m2".strip()


def apply_requested_coverage(response: SearchResponse, requested_m2: float) -> None:
    if requested_m2 <= 0:
        return
    for hit in response.hits:
        hit.coverage = calculate_coverage(hit.product, requested_m2)
    response.requested_m2 = requested_m2
