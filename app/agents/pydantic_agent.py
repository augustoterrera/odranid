from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from pydantic_ai import Agent, RunContext
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from .catalog_helpers import AgentError, build_system_prompt, canonical_product_link, clamp_int, compact_search_response
from ..catalog.coverage import calculate_coverage, extract_requested_m2
from ..catalog.footwear import extract_requested_talle
from ..core.models import (
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
logger = logging.getLogger(__name__)
_LOGFIRE_CONFIGURED = False


class OdranidAgentOutput(BaseModel):
    intake: ProductIntakeResponse = Field(default_factory=ProductIntakeResponse)
    answer: str


@dataclass
class OdranidAgentDeps:
    search: SearchCallable
    default_limit: int
    max_limit: int
    latest_message: str = ""
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

INTAKE_EXTRACTION_RULES = """\
## Reglas de extracción de intake

Tu `intake` es el estado estructurado actualizado del pedido del cliente. Usalo para reflejar lo que
el cliente quiere ahora, no para repetir datos viejos que fueron corregidos o descartados. Devolvé el
`known` completo re-derivado de toda la conversación real en cada turno: incluí todos los atributos
que el cliente sigue queriendo y omití los que corrigió o descartó. El `known` es la verdad del
estado: lo que no incluyas se considera que ya no aplica.

### Campos de `known`

Para pisos:
- `rubro`: "pisos"
- `category`: "pisos_vinilicos" solo si el cliente pide explícitamente vinílico, PVC o vinil como algo que quiere.
- `floor_kind`: "liso" o "diseno"
- `floor_design`: "moneda", "semilla", "rayado", "simil_madera", "semilla_melon"
- `espesor_mm`: número
- `ancho_m`: número
- `requested_m2`: número
- `use`: "gimnasio", "hogar", "danza", "cochera", "oficina", "comercial", "industrial", "exterior", "baño", "dormitorio"
- `traffic`: "alto", "medio", "medio_bajo", "bajo"
- `budget_preference`: "economico"
- `tags`: lista, por ejemplo ["antideslizante"]

Para mangueras:
- `rubro`: "mangueras"
- `use`: "jardin", "industrial", "agua", "pileta", "aire"
- `diameter`: string, por ejemplo "1/2", "3/4", "12 mm"
- `length_m`: número

Para mascotas:
- `rubro`: "mascotas"
- `animal`: "perro", "gato"
- `size`: "grande", "mediano", "chico"
- `toy_type`: "frisbee", "hueso", "pelota", "mordillo", "aro"

### `missing`, `should_search` y `next_question`

Usá `missing` solo para slots que todavía faltan para buscar:
- Pisos: "floor_kind_or_design", "espesor_mm", "ancho_m", "requested_m2"
- Mangueras: "use", "diameter", "length_m"
- Mascotas: "animal", "size" si no hay `toy_type`

`should_search=true` solo cuando el agente efectivamente va a llamar `buscar_productos` y tiene datos
suficientes para hacerlo:
- Pisos: `floor_kind` o `floor_design` + `espesor_mm` + `ancho_m` + `requested_m2`
- Mangueras: `use` + `diameter` + `length_m`
- Mascotas: `toy_type`, o `animal` + `size`
- Hogar/calzado/general: algún producto o detalle específico buscable

Si `should_search=false`, no llames `buscar_productos`. Si llamás `buscar_productos`, el `intake.should_search`
debe ser `true`. Cuando falte información, `should_search=false`, completá `missing` y poné una
`next_question` concisa en español rioplatense. En mensajes institucionales, `intent=null`,
`known={}`, `missing=[]`, `should_search=false`, `next_question=null`.

### Reglas críticas de intake

1. Correcciones: si el cliente dice "no te pedí X", "no es lo que busco", "pero no", "quiero otra cosa",
   "eso no", "no era eso" o corrige una característica, eliminá ese atributo de `known`. No lo incluyas
   aunque aparezca antes en la conversación, y no lo uses en `buscar_productos`. Si es una corrección
   pura sin nuevo producto, devolvé `intent=null`, `known={}`, `missing=[]`, `should_search=false`,
   `next_question=null` y no llames herramientas. Esa corrección sigue vigente en turnos posteriores:
   no vuelvas a inferir atributos descartados desde mensajes anteriores ni desde respuestas previas del
   asistente, salvo que el cliente los vuelva a pedir explícitamente. Referencias ambiguas como
   "similar a este", "la segunda" o "eso" no alcanzan para resucitar slots descartados.

2. Espesores: "mm" siempre es `espesor_mm`. Valores típicos: 1.2, 2, 2.5, 3.

3. Anchos: valores en metros típicos: 1, 1.2, 1.4, 1.5, 2.

4. Vinílico: `category="pisos_vinilicos"` solo si el cliente pide "vinilico", "PVC" o "vinil"
   explícitamente como algo que quiere. El default es goma, sin `category`.

5. Recomendaciones por uso: si el cliente pide recomendación y no especificó espesor, para gimnasio,
   danza, escenario, alto tránsito o industrial podés asumir `espesor_mm=3`; para hogar, dormitorio
   u oficina podés asumir `espesor_mm=2`.

6. Símil madera: usá `floor_design="simil_madera"` solo con frases explícitas como "simil madera",
   "tipo madera" o "efecto madera". "piso de madera" no es simil madera.

7. Respuestas cortas y contexto: si el asistente preguntó por ancho y el cliente responde "2" o
   "1.20", eso es `ancho_m`. Si preguntó por espesor y responde "3", eso es `espesor_mm`. Si preguntó
   ambos y el cliente responde "2 y 2" o "3 y 1.20", el primer número es espesor y el segundo es ancho.

8. Mascotas y razas: pitbull, rottweiler, dogo u ovejero implican `animal="perro"` y `size="grande"`.

9. Mensajes operativos o institucionales: saludos, despedidas, agradecimientos, preguntas de precio,
   envío, pago, factura, horarios, ubicación, retiro, visitar el local, ver productos en persona,
   pedir un asesor/persona, frustración o mensajes de proveedores deben devolver `intent=null`,
   `known={}`, `missing=[]`, `should_search=false`, `next_question=null`. Esto manda aunque el mensaje
   nombre un producto, porque la intención actual es operativa y no buscar catálogo.

10. Disponibilidad: mensajes con "¿tienen...?", "¿tenés...?", "¿hay...?", "¿vendés...?" que preguntan
    si existe un producto, material o característica deben tener `should_search=true` y una intención
    buscable, aunque falten datos finos. Distinto: "stock" de un producto ya elegido o pedido antes
    es operativo y debe ir como `intent=null`, sin búsqueda.

11. Antideslizante: si el cliente lo pide, agregá "antideslizante" a `tags` y establecé
    `floor_kind="diseno"` si no hay otro `floor_kind`.

12. Diseño vs liso: "con diseño", "moneda", "semilla", "rayado" y "antideslizante" implican
    `floor_kind="diseno"`. "liso" implica `floor_kind="liso"`.
"""

FIXED_SAFE_LINKS = {
    "https://wa.me/5491125539459",
    "https://maps.app.goo.gl/zMfBWeQwwPKFGBa89",
}


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
        prompt_file = Path("app/agents/prompts/prompt_agente_odranid.md")

    configure_logfire()
    deps = OdranidAgentDeps(
        search=search, default_limit=request.limit, max_limit=request.limit, latest_message=request.message
    )
    agent = build_agent(
        model=pydantic_model or build_openai_model(model, api_key),
        system_prompt=build_pydantic_system_prompt(prompt_file, catalog_context),
    )

    try:
        result = agent.run_sync(build_user_prompt(request), deps=deps)
    except Exception as exc:
        raise AgentError(f"PydanticAI agent run failed: {exc}") from exc

    output = result.output
    safe_answer = guard_agent_answer(output.answer, deps.search_responses)
    if not safe_answer.strip():
        raise AgentError("PydanticAI agent response did not include final text")

    return AgentResponse(
        answer=safe_answer,
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
        # Calzado: extraer el talle pedido del mensaje (determinístico) para descartar
        # productos cuyo rango de talles no lo incluya, sin depender del LLM.
        filters.talle = extract_requested_talle(ctx.deps.latest_message)
        # No confiar solo en que el LLM emita requested_m2: si el cliente mencionó m² a cubrir
        # (en este mensaje o en la query semántica), extraerlo de forma determinística para que
        # la cobertura SIEMPRE se calcule. Así no quedan respuestas sin "Necesitás X rollos".
        effective_m2 = requested_m2
        if effective_m2 is None:
            effective_m2 = extract_requested_m2(" ".join(filter(None, [ctx.deps.latest_message, query_semantica])))

        query = semantic_query_with_requested_m2(query_semantica, effective_m2)
        search_request = SearchRequest(query=query, filters=filters, limit=safe_limit, relax_filters=True)
        response = ctx.deps.search(search_request)
        if effective_m2 is not None:
            apply_requested_coverage(response, effective_m2)

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
            "requested_m2": effective_m2,
            "limit": safe_limit,
        }
        ctx.deps.tool_calls.append(
            AgentToolTrace(name="buscar_productos", arguments=arguments, result_count=len(response.hits))
        )
        ctx.deps.search_responses.append(response)
        return compact_search_response(response)

    return agent


def configure_logfire() -> None:
    global _LOGFIRE_CONFIGURED
    if _LOGFIRE_CONFIGURED:
        return
    try:
        import logfire

        logfire.configure(
            send_to_logfire="if-token-present",
            service_name="odranid-catalog-service",
            console=False,
            config_dir=Path("/tmp/odranid-logfire"),
            data_dir=Path("/tmp/odranid-logfire"),
        )
        logfire.instrument_pydantic_ai(include_content=True)
        _LOGFIRE_CONFIGURED = True
    except Exception as exc:  # pragma: no cover - instrumentation must never break chat.
        logger.warning("logfire_setup_failed", extra={"error": str(exc)})


def build_openai_model(model_name: str, api_key: str) -> OpenAIChatModel:
    return OpenAIChatModel(model_name, provider=OpenAIProvider(api_key=api_key))


def build_pydantic_system_prompt(prompt_file: Path, catalog_context: str) -> str:
    return "\n\n".join([build_system_prompt(prompt_file, catalog_context), PYDANTIC_AGENT_INSTRUCTIONS, INTAKE_EXTRACTION_RULES])


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
    return history


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


def guard_agent_answer(answer: str, search_responses: list[SearchResponse]) -> str:
    if not search_responses:
        return answer.strip()

    allowed = allowed_catalog_items(search_responses)
    lines: list[str] = []
    for raw_line in answer.splitlines():
        line, had_disallowed_link = format_allowed_links_for_whatsapp(raw_line, allowed["links"])
        if had_disallowed_link:
            continue
        if looks_like_product_line(line) and not mentions_allowed_product(line, allowed["titles"]):
            continue
        lines.append(line.rstrip())

    return compact_answer_lines(lines)


def allowed_catalog_items(search_responses: list[SearchResponse]) -> dict[str, set[str]]:
    links: set[str] = set(FIXED_SAFE_LINKS)
    titles: set[str] = set()
    for response in search_responses:
        for hit in response.hits:
            title = hit.product.title.strip()
            if title:
                titles.add(normalize_answer_text(title))
            link = canonical_product_link(hit.product.link)
            if link:
                links.add(link)
    return {"links": links, "titles": titles}


def format_allowed_links_for_whatsapp(line: str, allowed_links: set[str]) -> tuple[str, bool]:
    had_disallowed_link = False

    def replace_markdown_link(match: re.Match[str]) -> str:
        nonlocal had_disallowed_link
        label = match.group("label").strip()
        link = canonical_product_link(match.group("url").strip())
        if link not in allowed_links:
            had_disallowed_link = True
            return ""
        return f"{label}\n🔗 {link}"

    line = re.sub(r"\[(?P<label>[^\]]+)\]\((?P<url>https?://[^)]+)\)", replace_markdown_link, line)

    def replace_bare_link(match: re.Match[str]) -> str:
        nonlocal had_disallowed_link
        link = canonical_product_link(match.group(0).rstrip(".,)"))
        if link not in allowed_links:
            had_disallowed_link = True
            return ""
        return f"🔗 {link}"

    line = re.sub(r"https?://\S+", replace_bare_link, line)
    line = line.replace("🔗 🔗", "🔗")
    return line.strip(), had_disallowed_link


def looks_like_product_line(line: str) -> bool:
    return bool(re.match(r"^\s*(?:\d+[\).\s]|[-*]\s+)", line))


def mentions_allowed_product(line: str, allowed_titles: set[str]) -> bool:
    normalized = normalize_answer_text(line)
    return any(title and title in normalized for title in allowed_titles)


def normalize_answer_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.casefold()).strip()


def compact_answer_lines(lines: list[str]) -> str:
    compacted: list[str] = []
    previous_blank = False
    for line in lines:
        blank = not line.strip()
        if blank and previous_blank:
            continue
        compacted.append(line)
        previous_blank = blank
    return "\n".join(compacted).strip()
