from __future__ import annotations

from pathlib import Path
from typing import Callable

from ..models import AgentRequest, AgentResponse, ProductIntakeResponse, SearchRequest, SearchResponse
from ..query_parser import filters_from_intake
from .catalog_agent import respond_with_catalog
from .requirements_agent import analyze_requirements

SearchCallable = Callable[[SearchRequest], SearchResponse]
ContextBuilder = Callable[[AgentRequest, ProductIntakeResponse], str]


def run_team(
    request: AgentRequest,
    search: SearchCallable,
    api_key: str,
    context_builder: ContextBuilder,
    model: str = "gpt-4.1-mini",
    prompt_file: Path | None = None,
) -> AgentResponse:
    """Coordinate RequirementsAgent and CatalogAgent in sequence.

    context_builder is called AFTER RequirementsAgent so the pre-context RAG
    reflects the LLM-extracted intake, not the stale deterministic one.
    The search callable is wrapped with intake-derived filters so the CatalogAgent
    never touches the keyword parser — all structured data comes from the LLM.
    """
    intake = analyze_requirements(request.message, request.history, api_key, model)

    if intake.intent is not None and not intake.should_search and intake.next_question:
        return AgentResponse(answer=intake.next_question)

    catalog_context = context_builder(request, intake)

    return respond_with_catalog(
        request=request,
        catalog_context=catalog_context,
        search=_search_with_intake_filters(search, intake),
        api_key=api_key,
        model=model,
        prompt_file=prompt_file,
    )


def _search_with_intake_filters(search: SearchCallable, intake: ProductIntakeResponse) -> SearchCallable:
    """Wrap search so buscar_productos uses LLM-extracted filters, not the keyword parser."""
    base_filters = filters_from_intake(intake)

    def search_fn(req: SearchRequest) -> SearchResponse:
        return search(SearchRequest(
            query=req.query,
            filters=base_filters,
            limit=req.limit,
            relax_filters=req.relax_filters,
        ))

    return search_fn
