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

from .catalog_helpers import AgentError, build_system_prompt, canonical_product_link, clamp_int, compact_search_response, format_number
from ..catalog.coverage import calculate_coverage, extract_requested_m2
from ..catalog.footwear import extract_requested_talle
from ..catalog.product_links import extract_product_slugs
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
    linked_product_responses: list[SearchResponse] = field(default_factory=list)


PYDANTIC_AGENT_INSTRUCTIONS = """\
Además de responder al cliente, devolvé siempre un `intake` estructurado compatible con ProductIntakeResponse:
- `intent`, `known`, `missing`, `should_search`, `next_question`, `confidence`.
- `known` conserva los mismos nombres de slots ya usados por Odranid.

`latest_user_message` puede traer varios mensajes pendientes unidos con saltos de línea, en orden cronológico.
Leelos de arriba hacia abajo: las líneas de abajo son más nuevas y pueden corregir o precisar las de arriba.
Cuando el primer texto viene de "Vengo de la tienda online..." y después el cliente escribe "estoy interesada en...",
la descripción escrita por el cliente después tiene prioridad sobre el título/link inicial de la tienda.
En pisos, una medida escrita como "1.00x10", "1 x 10", "1,00 x 10" o similar significa ancho 1.00 m
por largo 10 m. Si aparece después de un título/link con otro ancho, reemplaza el ancho anterior.

Cuando el user prompt trae `linked_products_from_web`, esos productos fueron resueltos desde la DB a partir
de links de Odranid enviados por la web. Tratá al cliente como interesado en ese producto real: usá su título,
link y specs como contexto confiable. Si el cliente pregunta algo operativo (envío/costo/pago), respondé sobre
ese producto sin abrir un cuestionario ni mostrar alternativas. Si los datos escritos por el cliente después del
link corrigen alguna medida del producto detectado, no mezcles ambas medidas: aclaralo o priorizá lo último que
escribió el cliente.
Ejemplo: si el link resuelto es "ancho 1.40m" pero después el cliente escribe "1.00x10 no PVC", respondé
"veo que venís del producto de 1.40m y también mencionás 1m x 10m no PVC" antes de derivar/cotizar.

Cuando busques productos, llamá `buscar_productos` con argumentos estructurados. No escondas filtros dentro
de una query libre: emití rubro, tipo/floor_kind/floor_design, espesor_mm, ancho_m, material, color, tags,
requested_m2 y query_semantica cuando correspondan.

Si no hace falta buscar, `answer` debe ser la respuesta final breve. Si falta información, `answer` puede ser
la `next_question`.

Prioridad de respuesta final para consultas operativas:
- Si el cliente pregunta por un producto concreto y además quiere retirar hoy, buscá el producto si tenés datos
  suficientes. Si existe, mencioná el producto encontrado y su link, pero NO prometas "podés retirar hoy".
  Terminá siempre con asesor + dirección/horario:
  "Para confirmar disponibilidad y coordinar el retiro hoy, comunicate con un asesor: https://wa.me/5491125539459
  Estamos en Av. Suárez 2737, Barracas (CABA), de lunes a viernes de 8 a 16 hs."
- Si el último mensaje pregunta por envío, costo de envío, flete, correo, transporte o destino/localidad,
  respondé sobre envío y no incluyas dirección, horario ni link de cómo llegar.
- Si ese mensaje también menciona un producto o viene desde una página de producto, reconocelo brevemente
  en la respuesta para dar continuidad (ej. "Por el piso semilla melón no PVC..."), pero no presentes
  catálogo.
- Si hay varias medidas o descripciones del producto, priorizá la última que escribió el cliente por sobre
  títulos/links anteriores de la tienda. No cambies 1.00 x 10 por 1.40 si el cliente corrigió después.
- Al reconocer el producto en la respuesta de envío, conservá los datos clave escritos por el cliente al final:
  diseño, material/no PVC y medida si la dio.
- No confirmes stock ni disponibilidad con "sí, tenemos..." salvo que hayas llamado `buscar_productos` o el
  cliente pregunte solo por envío sin pedir costo. Para continuidad usá "Por el piso..." o "Sobre el piso...".
- Si es interior o menciona una localidad/provincia, decí que hacemos envíos al interior por correo y derivá
  al asesor para confirmar/cotizar costo: https://wa.me/5491125539459
- No busques productos ni presentes catálogo en esa respuesta salvo que además pregunte disponibilidad/stock o
  pida ver alternativas.
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

Para calzado:
- `rubro`: "calzado"
- `use`: "lluvia", "seguridad", "trabajo", "industrial"
- El talle lo extrae el microservicio del mensaje; no hace falta que lo pongas en `known`.

### `missing`, `should_search` y `next_question`

Usá `missing` solo para slots que todavía faltan para buscar:
- Pisos: "floor_kind_or_design", "espesor_mm", "ancho_m", "requested_m2". Excepción — modo recomendación:
  cuando el cliente pide recomendación o dice que no conoce las medidas, NO pongas `espesor_mm` ni
  `ancho_m` en `missing` (los definís vos por uso); completá `espesor_mm` en `known` y avanzá.
- Mangueras: "use", "diameter", "length_m"
  Excepción — disponibilidad con diámetro/tipo/foto: si el cliente pregunta si tenemos una manguera concreta
  ("tenés/tienen/hay/venden", "estas mangueras", "como la foto") y da diámetro, tipo o referencia, no exijas
  use ni length_m antes de buscar. Poné `should_search=true` y buscá con lo disponible.
  Si no hay exacto para los diámetros que ya dio, no vuelvas a pedir `diameter`; ofrecé buscar alternativas y,
  si hace falta, pedí solo uso y metros.
- Mascotas: "animal", "size" si no hay `toy_type`

`should_search=true` solo cuando el agente efectivamente va a llamar `buscar_productos` y tiene datos
suficientes para hacerlo:
- Pisos: `floor_kind` o `floor_design` + `espesor_mm` + `ancho_m` + `requested_m2`. Excepción — modo
  recomendación: si el cliente pidió recomendación o dijo que no sabe las medidas, alcanza con
  `floor_kind`/`floor_design` + `espesor_mm` (recomendado por uso) + `requested_m2`, sin `ancho_m`.
  Excepción — cálculo sobre productos ya mostrados: si en el historial ya hubo una búsqueda de pisos y el
  cliente agrega los m² a cubrir o pregunta "¿cuántos rollos?" / "¿cuánto necesito?", poné
  `should_search=true` para recalcular la cobertura sobre esos mismos productos, AUNQUE falten
  `espesor_mm`/`ancho_m`: las medidas ya las define el producto mostrado, no hay que volver a pedirlas.
- Mangueras: `use` + `diameter` + `length_m`
  Excepción — disponibilidad con diámetro/tipo/foto: alcanza con `diameter` o una referencia concreta para
  buscar disponibilidad. Si hay varios diámetros (ej. 80mm y 110mm), buscá cada diámetro o incluí ambos en
  la búsqueda y respondé por cada uno.
- Mascotas: `toy_type`, o `animal` + `size`
- Calzado/hogar/general: con el rubro + cualquier detalle (uso, talle, tipo) ya alcanza para buscar. NO
  exijas un cuestionario completo: una pregunta de disponibilidad ("¿tenés botas?") debe tener
  `should_search=true` aunque falten detalles finos. Mejor buscar y mostrar el surtido que interrogar.

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
   En pisos, formatos como "1.00x10", "1 x 10" o "1,00 x 10" se interpretan como ancho x largo:
   `ancho_m=1.0` y largo 10 m. Ese ancho reemplaza cualquier ancho anterior del título/link.

4. Vinílico: `category="pisos_vinilicos"` solo si el cliente pide "vinilico", "PVC" o "vinil"
   explícitamente como algo que quiere. El default es goma, sin `category`.

5. Recomendaciones por uso: cuando el cliente pide recomendación explícita ("¿qué me recomendás?",
   "no sé qué elegir", "no sé las medidas", "ayudame a elegir", "¿cuál me conviene?") o dice que no
   conoce las especificaciones, te está DELEGANDO la decisión: poné `known["recommendation"]=true`,
   completá `known` con los valores recomendados por uso y poné `should_search=true` (no sigas
   preguntando esas medidas). Defaults:
   - gimnasio, danza, escenario, alto tránsito o industrial: `espesor_mm=3`.
   - hogar, dormitorio, oficina o comercio: `espesor_mm=2`.
   - material: goma/caucho para uso intenso (gimnasio, industrial); PVC para tránsito medio (oficina,
     comercio, hogar). No pises `floor_kind`/`floor_design` si el cliente ya lo eligió (ej. simil madera).
   IMPORTANTE — no impongas espesor si el cliente ya eligió un diseño/producto concreto (ej. simil
   madera): NO setees `espesor_mm` por defecto en ese caso; dejá que la búsqueda traiga los espesores
   que ese diseño realmente tiene. El espesor por uso solo aplica cuando el cliente no fijó un producto.
   El `ancho_m` NO hace falta para recomendar: es un atributo del producto. No bloquees la búsqueda
   esperando el ancho cuando el cliente pidió que recomendaras o no conoce la medida.

6. Símil madera: usá `floor_design="simil_madera"` solo con frases explícitas como "simil madera",
   "tipo madera" o "efecto madera". "piso de madera" no es simil madera.

7. Respuestas cortas y contexto: si el asistente preguntó por ancho y el cliente responde "2" o
   "1.20", eso es `ancho_m`. Si preguntó por espesor y responde "3", eso es `espesor_mm`. Si preguntó
   ambos y el cliente responde "2 y 2" o "3 y 1.20", el primer número es espesor y el segundo es ancho.

8. Mascotas y razas: pitbull, rottweiler, dogo u ovejero implican `animal="perro"` y `size="grande"`.

9. Mensajes operativos o institucionales: saludos, despedidas, agradecimientos, preguntas de precio,
   envío, costo de envío, tiempos de envío, pago, factura, horarios, ubicación, retiro, visitar el local,
   ver productos en persona, pedir un asesor/persona, frustración o mensajes de proveedores deben devolver
   `intent=null`, `known={}`, `missing=[]`, `should_search=false`, `next_question=null`. Esto manda aunque
   el mensaje nombre un producto o traiga link de producto, porque la intención actual es operativa y no
   buscar catálogo. No uses intents como `consulta_envio`, `precio`, `pago` ni copies especificaciones del
   producto a `known` en estos casos.

10. Disponibilidad: mensajes con "¿tienen...?", "¿tenés...?", "¿hay...?", "¿vendés...?" que preguntan
    si existe un producto, material o característica deben tener `should_search=true` y una intención
    buscable, aunque falten datos finos. Distinto: "stock" de un producto ya elegido o pedido antes
    es operativo y debe ir como `intent=null`, sin búsqueda.

11. Antideslizante: si el cliente lo pide, agregá "antideslizante" a `tags` y establecé
    `floor_kind="diseno"` si no hay otro `floor_kind`.

12. Diseño vs liso: "con diseño", "moneda", "semilla", "rayado" y "antideslizante" implican
    `floor_kind="diseno"`. "liso" implica `floor_kind="liso"`.

13. Mangueras: "inter" o "int." significa "interno" / diámetro interno. Si el cliente dice "estas mangueras"
    y adjunta o menciona una foto, tratá eso como referencia concreta; buscá antes de pedir uso/largo.
    En búsquedas de mangueras, conservá siempre la unidad del diámetro en `query_semantica`: escribí "80mm",
    "110mm", etc., no solo "80" o "110".
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
    deps = build_agent_deps(request, search)
    agent = build_agent(
        model=pydantic_model or build_openai_model(model, api_key),
        system_prompt=build_pydantic_system_prompt(prompt_file, catalog_context),
    )

    output, deps = run_agent_enforcing_search(agent, request, search, deps)

    safe_answer = guard_agent_answer(output.answer, deps.search_responses)
    safe_answer = ensure_pickup_today_details(safe_answer, request, deps.search_responses)
    if not safe_answer.strip():
        raise AgentError("PydanticAI agent response did not include final text")

    return AgentResponse(
        answer=safe_answer,
        tool_calls=deps.tool_calls,
        intake=output.intake,
    )


def build_agent_deps(request: AgentRequest, search: SearchCallable) -> OdranidAgentDeps:
    deps = OdranidAgentDeps(
        search=search,
        default_limit=request.limit,
        max_limit=request.limit,
        latest_message=request.message,
    )
    preload_linked_products(deps, request)
    return deps


def preload_linked_products(deps: OdranidAgentDeps, request: AgentRequest) -> None:
    for slug in extract_product_slugs(request.message):
        try:
            response = deps.search(
                SearchRequest(
                    query=slug.replace("-", " "),
                    filters=ProductFilters(product_slug=slug, in_stock_only=False),
                    limit=1,
                    relax_filters=False,
                )
            )
        except Exception as exc:
            logger.warning("linked_product_lookup_failed", extra={"product_slug": slug, "error": str(exc)})
            continue
        if response.hits:
            deps.linked_product_responses.append(response)
            deps.search_responses.append(response)


def run_agent_enforcing_search(
    agent: Agent[OdranidAgentDeps, OdranidAgentOutput],
    request: AgentRequest,
    search: SearchCallable,
    deps: OdranidAgentDeps,
) -> tuple[OdranidAgentOutput, OdranidAgentDeps]:
    """Run the agent enforcing the should_search invariant.

    Per the prompt, ``should_search=true`` means the agent WILL call ``buscar_productos``.
    If it claims should_search but made no tool call, it narrated the search instead of
    doing it (e.g. replying with the query in first person: "Busco pisos liso 2mm..."),
    and that text would leak to the client. That turn is invalid: re-run once forcing the
    tool so the answer is built from real catalog data.
    """
    output = _run_agent_once(agent, request, deps)
    if output.intake and output.intake.should_search and not deps.tool_calls:
        logger.warning(
            "agent_should_search_without_tool_call",
            extra={"latest_message": request.message},
        )
        deps = build_agent_deps(request, search)
        output = _run_agent_once(agent, request, deps, force_search=True)

    # En modo recomendación, si buscó y trajo productos pero NO los presentó (los retuvo para
    # pedir el ancho u otra medida), el turno incumple la regla: re-ejecutar forzando la
    # presentación. El cliente que delega la decisión quiere ver las opciones, no más preguntas.
    if (
        is_recommendation_request(request, output)
        and search_has_hits(deps.search_responses)
        and not answer_presents_any_product(output.answer, deps.search_responses)
    ):
        logger.warning(
            "agent_recommendation_hid_products",
            extra={"latest_message": request.message},
        )
        retry_deps = build_agent_deps(request, search)
        retry_output = _run_agent_once(agent, request, retry_deps, force_present=True)
        if search_has_hits(retry_deps.search_responses) and answer_presents_any_product(
            retry_output.answer, retry_deps.search_responses
        ):
            output, deps = retry_output, retry_deps
    return output, deps


_RECOMMENDATION_RE = re.compile(
    r"recomend|no\s+s[eé]\b|cu[aá]l\s+me\s+conviene|ayud[aá](?:me)?\s+a\s+elegir|qu[eé]\s+me\s+conviene|no\s+entiendo",
    re.IGNORECASE,
)


def is_recommendation_request(request: AgentRequest, output: OdranidAgentOutput) -> bool:
    """El cliente delegó la decisión (pidió recomendación o dijo que no sabe).

    Señal primaria: el flag `recommendation` que el intake marca al delegar. Backup
    determinístico sobre el mensaje, para que el guard sea confiable aunque el LLM no lo marque.
    """
    known = (output.intake.known if output.intake else None) or {}
    if known.get("recommendation"):
        return True
    return bool(_RECOMMENDATION_RE.search(request.message or ""))


def search_has_hits(search_responses: list[SearchResponse]) -> bool:
    return any(response.hits for response in search_responses)


def answer_presents_any_product(answer: str, search_responses: list[SearchResponse]) -> bool:
    text = (answer or "").lower()
    for response in search_responses:
        for hit in response.hits:
            slug = product_link_slug(hit.product.link)
            if slug and slug in text:
                return True
    return False


def product_link_slug(link: str | None) -> str:
    if not link:
        return ""
    return link.rstrip("/").rsplit("/", 1)[-1].lower()


def _run_agent_once(
    agent: Agent[OdranidAgentDeps, OdranidAgentOutput],
    request: AgentRequest,
    deps: OdranidAgentDeps,
    *,
    force_search: bool = False,
    force_present: bool = False,
) -> OdranidAgentOutput:
    try:
        result = agent.run_sync(
            build_user_prompt(
                request,
                linked_product_responses=deps.linked_product_responses,
                force_search=force_search,
                force_present=force_present,
            ),
            deps=deps,
        )
    except Exception as exc:
        raise AgentError(f"PydanticAI agent run failed: {exc}") from exc
    return result.output


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


def build_user_prompt(
    request: AgentRequest,
    *,
    linked_product_responses: list[SearchResponse] | None = None,
    force_search: bool = False,
    force_present: bool = False,
) -> str:
    payload = {
        "latest_user_message": request.message,
        "history": [message.model_dump() for message in visible_history(request.history)],
    }
    linked_products = linked_products_payload(linked_product_responses or [])
    if linked_products:
        payload["linked_products_from_web"] = linked_products
    lines = [
        "Respondé el último mensaje del cliente usando este contexto de conversación.",
        "```json",
        json.dumps(payload, ensure_ascii=False, indent=2),
        "```",
    ]
    if force_search:
        lines.append(
            "IMPORTANTE: ya tenés datos suficientes para buscar. Llamá `buscar_productos` AHORA y "
            "construí la respuesta con los resultados. NO respondas con un texto que describa o "
            "confirme lo que vas a buscar, ni repitas el pedido en primera persona "
            "(ej. \"Busco pisos liso 2mm...\"): eso es una query interna, no un mensaje al cliente. "
            "Ejecutá la herramienta."
        )
    if force_present:
        lines.append(
            "IMPORTANTE: el cliente te pidió una recomendación y hay productos para mostrarle. "
            "Llamá `buscar_productos` y PRESENTÁ las opciones AHORA con el formato de lista "
            "(nombre, specs, link). PROHIBIDO pedir el ancho, pedir más medidas o decir "
            "\"para mostrarte productos exactos\": el cliente quiere ver las opciones ya y elige sobre ellas."
        )
    return "\n".join(lines)


def linked_products_payload(responses: list[SearchResponse]) -> list[dict[str, Any]]:
    products: list[dict[str, Any]] = []
    for response in responses:
        for hit in response.hits:
            product = hit.product
            products.append(
                {
                    "title": product.title,
                    "link": canonical_product_link(product.link),
                    "in_stock": product.in_stock,
                    "stock_text": product.stock_text,
                    "rubro": product.rubro,
                    "category": product.category,
                    "product_type": product.product_type,
                    "floor_kind": product.floor_kind,
                    "floor_design": product.floor_design,
                    "material": product.material,
                    "color": product.color,
                    "specs": product.specs.model_dump(),
                }
            )
    return products


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
    discarded_links = 0
    discarded_products = 0
    for raw_line in answer.splitlines():
        line, had_disallowed_link = format_allowed_links_for_whatsapp(raw_line, allowed["links"])
        if had_disallowed_link:
            discarded_links += 1
            continue
        if looks_like_product_line(line) and not mentions_allowed_product(line, allowed["titles"]):
            discarded_products += 1
            continue
        lines.append(line.rstrip())

    if discarded_links or discarded_products:
        logger.warning(
            "guard_discarded_answer_lines",
            extra={
                "discarded_link_lines": discarded_links,
                "discarded_product_lines": discarded_products,
                "total_lines": len(answer.splitlines()),
            },
        )
    lines = repair_orphan_product_links(lines, allowed["link_hits"], allowed["titles"])
    return compact_answer_lines(lines)


def ensure_pickup_today_details(answer: str, request: AgentRequest, search_responses: list[SearchResponse]) -> str:
    if not is_pickup_today_request(request.message) or not search_has_hits(search_responses):
        return answer
    exact_hit = first_exact_search_hit(search_responses)
    if exact_hit is not None:
        return pickup_today_answer_for_hit(exact_hit, request)
    safe_answer = remove_pickup_today_promise(answer).strip()
    missing_parts: list[str] = []
    if "wa.me/5491125539459" not in safe_answer:
        missing_parts.append(
            "Para confirmar disponibilidad y coordinar el retiro hoy, comunicate con un asesor: https://wa.me/5491125539459"
        )
    has_address = "Av. Suárez 2737" in safe_answer or "Av. Suarez 2737" in safe_answer
    has_hours = "8 a 16" in safe_answer
    if not (has_address and has_hours):
        missing_parts.append("Estamos en Av. Suárez 2737, Barracas (CABA), de lunes a viernes de 8 a 16 hs.")
    if not missing_parts:
        return safe_answer
    return compact_answer_lines([safe_answer, "", *missing_parts])


def first_exact_search_hit(search_responses: list[SearchResponse]) -> SearchHit | None:
    fallback: SearchHit | None = None
    for response in search_responses:
        for hit in response.hits:
            fallback = fallback or hit
            if not hit.is_alternative:
                return hit
    return fallback


def pickup_today_answer_for_hit(hit: SearchHit, request: AgentRequest) -> str:
    link = canonical_product_link(hit.product.link)
    intro = pickup_today_intro(request.message)
    lines = [intro, "", single_product_summary_line(hit)]
    if link:
        lines.append(f"🔗 {link}")
    lines.extend(
        [
            "",
            "Para retirar hoy, confirmá stock y preparación con un asesor antes de venir: https://wa.me/5491125539459",
            "Estamos en Av. Suárez 2737, Barracas (CABA), de lunes a viernes de 8 a 16 hs.",
        ]
    )
    return compact_answer_lines(lines)


def pickup_today_intro(message: str) -> str:
    greeting = greeting_from_message(message)
    prefix = f"{greeting} " if greeting else ""
    return f"{prefix}Sí, tenemos esta opción:"


def greeting_from_message(message: str) -> str | None:
    text = normalize_answer_text(message)
    if re.match(r"^(buenas tardes|buena tarde)\b", text):
        return "Buenas tardes."
    if re.match(r"^(buenos dias|buen dia|buenas)\b", text):
        return "Buenos días."
    if re.match(r"^(buenas noches)\b", text):
        return "Buenas noches."
    if re.match(r"^(hola|holaa|hello)\b", text):
        return "Hola."
    return None


def single_product_summary_line(hit: SearchHit) -> str:
    product = hit.product
    parts = [product.title]
    parts.extend(product_descriptors(hit))
    roll = roll_description(hit)
    if roll:
        parts.append(roll)
    quantity = coverage_quantity(hit)
    if quantity:
        parts.append(quantity)
    return " • ".join(part for part in parts if part)


def is_pickup_today_request(message: str) -> bool:
    text = normalize_answer_text(message)
    asks_today = "hoy" in text or "en el dia" in text
    asks_pickup = any(term in text for term in ["retir", "pasar", "paso", "buscar", "retiro"])
    return asks_today and asks_pickup


def remove_pickup_today_promise(answer: str) -> str:
    text = re.sub(r"(?im)^\s*(sí,\s*)?pod[eé]s retirar hoy\.?\s*$", "", answer)
    text = re.sub(r"(?i)\bpod[eé]s retirar hoy\b", "podés coordinar el retiro hoy", text)
    return text


def allowed_catalog_items(search_responses: list[SearchResponse]) -> dict[str, Any]:
    links: set[str] = set(FIXED_SAFE_LINKS)
    titles: set[str] = set()
    link_hits: dict[str, tuple[int, SearchHit]] = {}
    for response in search_responses:
        for hit in response.hits:
            title = hit.product.title.strip()
            if title:
                titles.add(normalize_answer_text(title))
            link = canonical_product_link(hit.product.link)
            if link:
                links.add(link)
                link_hits.setdefault(link, (len(link_hits) + 1, hit))
    return {"links": links, "titles": titles, "link_hits": link_hits}


def repair_orphan_product_links(
    lines: list[str],
    link_hits: dict[str, tuple[int, SearchHit]],
    allowed_titles: set[str],
) -> list[str]:
    repaired: list[str] = []
    for line in lines:
        link = standalone_whatsapp_link(line)
        if link and link in link_hits and not previous_line_mentions_allowed_product(repaired, allowed_titles):
            index, hit = link_hits[link]
            repaired.append(product_summary_line(index, hit))
        repaired.append(line)
    return repaired


def standalone_whatsapp_link(line: str) -> str | None:
    match = re.match(r"^\s*🔗\s+(?P<link>https?://\S+)\s*$", line)
    if not match:
        return None
    return canonical_product_link(match.group("link").rstrip(".,)"))


def previous_line_mentions_allowed_product(lines: list[str], allowed_titles: set[str]) -> bool:
    for line in reversed(lines):
        if not line.strip():
            continue
        return mentions_allowed_product(line, allowed_titles)
    return False


def product_summary_line(index: int, hit: SearchHit) -> str:
    product = hit.product
    parts = [f"{index}. {product.title}"]

    parts.extend(product_descriptors(hit))

    roll = roll_description(hit)
    if roll:
        parts.append(roll)

    quantity = coverage_quantity(hit)
    if quantity:
        parts.append(quantity)

    return " • ".join(parts)


def product_descriptors(hit: SearchHit) -> list[str]:
    product = hit.product
    descriptors = []
    if product.material:
        descriptors.append(product.material)
    if product.category == "pisos_vinilicos":
        descriptors.append("Vinílico")
    if product.floor_kind == "diseno":
        descriptors.append("Con diseño")
    elif product.floor_kind == "liso":
        descriptors.append("Liso")
    elif product.floor_kind:
        descriptors.append(product.floor_kind)
    if product.floor_design:
        descriptors.append(product.floor_design.replace("_", " ").capitalize())
    if product.rubro == "mangueras":
        if product.specs.diametro_mm is not None:
            descriptors.append(f"Diámetro {format_number(product.specs.diametro_mm)}mm")
        elif product.specs.espesor_mm is not None:
            descriptors.append(f"Diámetro {format_number(product.specs.espesor_mm)}mm")
    elif product.specs.espesor_mm is not None:
        descriptors.append(f"Espesor {format_number(product.specs.espesor_mm)}mm")
    return descriptors


def roll_description(hit: SearchHit) -> str | None:
    specs = hit.product.specs
    if specs.ancho_m is None or specs.largo_m is None:
        return None
    roll = f"Rollo {format_number(specs.largo_m)}m x {format_number(specs.ancho_m)}m"
    rendimiento = specs.rendimiento_m2 or hit.coverage.coverage_m2 if hit.coverage else specs.rendimiento_m2
    if rendimiento is not None:
        roll += f" ({format_number(rendimiento)} m²)"
    return roll


def coverage_quantity(hit: SearchHit) -> str | None:
    coverage = hit.coverage
    if coverage is None:
        return None
    if coverage.rolls_needed is not None:
        unit = "rollo" if coverage.rolls_needed == 1 else "rollos"
        return f"Necesitás {coverage.rolls_needed} {unit}"
    if coverage.quantity_m2 is not None:
        return f"Necesitás {format_number(coverage.quantity_m2)} m2"
    return coverage.message


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
