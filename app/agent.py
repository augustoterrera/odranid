"""Shared agent helpers used by the Agno catalog agent and search tool."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .models import AgentResponse, AgentToolTrace, SearchHit, SearchResponse


class AgentError(RuntimeError):
    pass


def build_system_prompt(prompt_file: Path, catalog_context: str) -> str:
    prompt = prompt_file.read_text(encoding="utf-8")
    return "\n\n".join([prompt, "## CONTEXTO DINAMICO ACTUAL", catalog_context])


def clamp_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def compact_search_response(response: SearchResponse) -> dict[str, Any]:
    return {
        "query": response.query,
        "used_relaxation": response.used_relaxation,
        "requested_m2": response.requested_m2,
        "total_catalog_size": response.total_catalog_size,
        "hits": [
            {
                "product": {
                    "id": hit.product.id,
                    "title": hit.product.title,
                    "link": canonical_product_link(hit.product.link),
                    "in_stock": hit.product.in_stock,
                    "stock_text": hit.product.stock_text,
                    "rubro": hit.product.rubro,
                    "category": hit.product.category,
                    "product_type": hit.product.product_type,
                    "floor_kind": hit.product.floor_kind,
                    "floor_design": hit.product.floor_design,
                    "material": hit.product.material,
                    "color": hit.product.color,
                    "technical_tags": hit.product.technical_tags,
                    "specs": hit.product.specs.model_dump(),
                },
                "score": hit.score,
                "matched_filters": hit.matched_filters,
                "relaxed_filters": hit.relaxed_filters,
                "coverage": hit.coverage.model_dump() if hit.coverage else None,
            }
            for hit in response.hits
        ],
    }


def response_from_search_response(response: SearchResponse, limit: int) -> AgentResponse:
    trace = AgentToolTrace(
        name="buscar_productos",
        arguments={"query": response.query, "limit": limit},
        result_count=len(response.hits),
    )
    if not response.hits:
        return AgentResponse(
            answer=(
                "No encontré una opción exacta en el catálogo con esos datos. "
                "Te recomiendo hablar con un asesor para revisar alternativas: https://wa.me/5491125539459"
            ),
            tool_calls=[trace],
        )

    visible_hits = visible_search_hits(response, limit)
    lines = [search_intro(response)]
    for index, hit in enumerate(visible_hits, start=1):
        lines.extend(format_hit(index, hit))

    if len(response.hits) > len(visible_hits):
        lines.append("Tengo más opciones si querés seguir comparando.")
    lines.append("📦 Envío: CABA flete propio / Interior correo | 💰 5% OFF efectivo/transferencia")
    lines.append("¿Cuál te interesa?")
    return AgentResponse(answer="\n\n".join(lines), tool_calls=[trace])


def search_intro(response: SearchResponse) -> str:
    if response.used_relaxation:
        return "No encontré coincidencia exacta, pero te muestro opciones cercanas:"
    return "Te muestro estas opciones:"


def visible_search_hits(response: SearchResponse, limit: int) -> list[SearchHit]:
    visible_limit = min(limit, 3)
    if not response.hits:
        return []

    first_unit = response.hits[0].coverage.sale_unit if response.hits[0].coverage else None
    if first_unit and response.requested_m2 is not None:
        same_unit_hits = [hit for hit in response.hits if hit.coverage and hit.coverage.sale_unit == first_unit]
        if same_unit_hits:
            return same_unit_hits[:visible_limit]

    return response.hits[:visible_limit]


def format_hit(index: int, hit: SearchHit) -> list[str]:
    product = hit.product
    details = [f"{index}. {product.title}"]
    if product.link:
        details.append(f"Link: {canonical_product_link(product.link)}")
    specs = product.specs
    spec_parts = []
    if specs.espesor_mm is not None:
        spec_parts.append(f"espesor {format_number(specs.espesor_mm)} mm")
    if specs.ancho_m is not None:
        spec_parts.append(f"ancho {format_number(specs.ancho_m)} m")
    if specs.largo_m is not None:
        spec_parts.append(f"largo {format_number(specs.largo_m)} m")
    if spec_parts:
        details.append("Datos: " + ", ".join(spec_parts))
    if hit.coverage:
        details.append(hit.coverage.message)
    if hit.relaxed_filters:
        details.append("Alternativa cercana: se relajó " + ", ".join(hit.relaxed_filters) + ".")
    return ["\n".join(details)]


def format_number(value: float) -> str:
    number = round(float(value), 2)
    if number.is_integer():
        return str(int(number))
    return f"{number:g}"


def canonical_product_link(link: str | None) -> str | None:
    if not link:
        return link
    return link.replace("https://odranid.com/producto/", "https://odranid.com.ar/producto/")
