from __future__ import annotations

import json
from typing import Callable

from agno.tools import tool

from ..agent import clamp_int, compact_search_response
from ..models import SearchRequest, SearchResponse

SearchCallable = Callable[[SearchRequest], SearchResponse]


def make_buscar_productos_tool(
    search: SearchCallable,
    *,
    default_limit: int = 5,
    max_limit: int = 10,
):
    """Build the Agno tool with the search function injected by the caller."""

    @tool
    def buscar_productos(query: str, limit: int = default_limit) -> str:
        """Busca productos reales del catálogo Odranid usando una consulta natural."""
        safe_limit = clamp_int(limit, default=default_limit, minimum=1, maximum=max_limit)
        result = search(SearchRequest(query=query, limit=safe_limit, relax_filters=True))
        # Return JSON string so Agno stores it in ToolExecution.result (Optional[str]),
        # enabling result_count extraction in catalog_agent.py.
        return json.dumps(compact_search_response(result), ensure_ascii=False)

    return buscar_productos
