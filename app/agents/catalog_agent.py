from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from agno.agent import Agent
from agno.models.message import Message
from agno.models.openai import OpenAIChat

from ..agent import AgentError, build_system_prompt
from ..models import AgentRequest, AgentResponse, AgentToolTrace, SearchRequest, SearchResponse
from ..tools.buscar_productos import make_buscar_productos_tool

SearchCallable = Callable[[SearchRequest], SearchResponse]


def respond_with_catalog(
    request: AgentRequest,
    catalog_context: str,
    search: SearchCallable,
    api_key: str,
    model: str = "gpt-4.1-mini",
    prompt_file: Path | None = None,
) -> AgentResponse:
    """Run the catalog conversation agent with native Agno tool use."""
    if prompt_file is None:
        prompt_file = Path("prompt_agente_odranid.md")

    buscar_productos = make_buscar_productos_tool(search, default_limit=request.limit, max_limit=request.limit)
    agent = Agent(
        model=OpenAIChat(id=model, api_key=api_key),
        tools=[buscar_productos],
        system_message=build_system_prompt(prompt_file, catalog_context),
        add_history_to_context=False,
    )

    try:
        response = agent.run(input=build_messages(request), stream=False)
    except Exception as exc:
        raise AgentError(f"CatalogAgent LLM call failed: {exc}") from exc

    answer = response.content
    if not isinstance(answer, str) or not answer.strip():
        raise AgentError("CatalogAgent response did not include final text")

    return AgentResponse(
        answer=answer.strip(),
        tool_calls=extract_tool_traces(getattr(response, "tools", None)),
    )


def build_messages(request: AgentRequest) -> list[Message]:
    messages = [Message(role=message.role, content=message.content) for message in request.history]
    messages.append(Message(role="user", content=request.message))
    return messages


def extract_tool_traces(tools: Any) -> list[AgentToolTrace]:
    if not isinstance(tools, list):
        return []

    traces: list[AgentToolTrace] = []
    for execution in tools:
        name = getattr(execution, "tool_name", None) or getattr(execution, "name", None)
        if not name:
            continue
        arguments = getattr(execution, "tool_args", None) or getattr(execution, "arguments", None) or {}
        if not isinstance(arguments, dict):
            arguments = {}
        traces.append(
            AgentToolTrace(
                name=str(name),
                arguments=arguments,
                result_count=result_count_from_tool_execution(execution),
            )
        )
    return traces


def result_count_from_tool_execution(execution: Any) -> int:
    result = getattr(execution, "result", None)
    if isinstance(result, dict):
        hits = result.get("hits")
        return len(hits) if isinstance(hits, list) else 0
    if not isinstance(result, str) or not result.strip():
        return 0
    try:
        payload = json.loads(result)
    except json.JSONDecodeError:
        return 0
    hits = payload.get("hits") if isinstance(payload, dict) else None
    return len(hits) if isinstance(hits, list) else 0
