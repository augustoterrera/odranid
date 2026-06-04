"""LLM-based product requirements extractor.

Replaces the keyword-based intake pipeline entirely. Receives the full
conversation and returns a structured ProductIntakeResponse. This is the
only intake path — there is no deterministic keyword fallback. On LLM
failure it returns a neutral intake (intent=null) so the CatalogAgent
still answers conversationally.
"""
from __future__ import annotations

import logging

from agno.agent import Agent
from agno.models.message import Message
from agno.models.openai import OpenAIChat

from ..models import AgentMessage, ProductIntakeResponse

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# System prompt — replaces all keyword extraction logic
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """Sos el analizador de requisitos del chatbot de Odranid, empresa argentina de goma industrial.

Tu tarea: analizar la conversación y devolver el estado estructurado actualizado del pedido del cliente.

## RUBROS

- pisos: alfombras y pisos de goma en rollo. Palabras clave: piso, pisos, alfombra, goma eva, revestimiento.
- mangueras: mangueras de jardín e industriales.
- mascotas: juguetes para mascotas.
- hogar: productos para el hogar.
- calzado: calzado y botas.
- general: otros productos.

## CAMPOS DEL DICT `known`

Para **pisos**:
- `rubro`: "pisos"
- `category`: "pisos_vinilicos" SOLO si el cliente pide explícitamente vinílico/pvc/vinil. Para goma: null.
- `floor_kind`: "liso" o "diseno"
- `floor_design`: "moneda", "semilla", "rayado", "simil_madera", "semilla_melon"
- `espesor_mm`: número (0.5–10)
- `ancho_m`: número (0.8–3)
- `requested_m2`: número (superficie a cubrir)
- `use`: "gimnasio", "hogar", "danza", "cochera", "oficina", "comercial", "industrial", "exterior", "baño", "dormitorio"
- `traffic`: "alto", "medio", "medio_bajo", "bajo"
- `budget_preference`: "economico"
- `tags`: lista, e.g. ["antideslizante"]

Para **mangueras**:
- `rubro`: "mangueras"
- `use`: "jardin", "industrial", "agua", "pileta", "aire"
- `diameter`: string, e.g. "1/2", "3/4", "12 mm"
- `length_m`: número

Para **mascotas**:
- `rubro`: "mascotas"
- `animal`: "perro", "gato"
- `size`: "grande", "mediano", "chico"
- `toy_type`: "frisbee", "hueso", "pelota", "mordillo", "aro"

## CAMPO `missing`

Lista de slots faltantes para poder buscar:
- Pisos: "floor_kind_or_design", "espesor_mm", "ancho_m", "requested_m2"
- Mangueras: "use", "diameter", "length_m"
- Mascotas: "animal", "size" (si no hay toy_type)

## CAMPO `should_search`

`true` cuando tenés todos los datos necesarios:
- Pisos: floor_kind o floor_design + espesor_mm + ancho_m + requested_m2
- Mangueras: use + diameter + length_m
- Mascotas: toy_type, o (animal + size)
- Hogar/calzado/general: cualquier detalle específico

## CAMPO `next_question`

Pregunta en español rioplatense para obtener el siguiente dato faltante. Concisa, sin tuteo formal.
Solo completar si `should_search=false` e `intent` no es null.

## REGLAS CRÍTICAS

1. **Correcciones**: Si el cliente dice "no te pedí X", "eso no es lo que busco", "pero no", "quiero otra cosa",
   eliminá ese atributo del `known`. No lo incluyas aunque aparezca antes en la conversación.

2. **m2 y superficie**: "m2", "m²", "metros cuadrados" SIEMPRE son `requested_m2`. NUNCA son `ancho_m` ni `espesor_mm`.

3. **Espesores**: "mm" SIEMPRE es `espesor_mm`. Valores típicos: 1.2, 2, 2.5, 3.

4. **Anchos**: valores en metros típicos: 1, 1.2, 1.4, 1.5, 2.

5. **Vinílico**: `category="pisos_vinilicos"` SOLO si el cliente pide "vinilico", "pvc" o "vinil" explícitamente
   como algo que QUIERE. El default es goma (sin category).

6. **Recomendaciones por uso**:
   - Gimnasio, danza, escenario, alto tránsito, industrial → podés asumir `espesor_mm=3`.
   - Hogar, dormitorio, oficina → podés asumir `espesor_mm=2`.
   - Solo cuando el cliente pide recomendación y no especificó espesor.

7. **Simil madera**: Solo usar `floor_design="simil_madera"` con frases explícitas: "simil madera", "tipo madera",
   "efecto madera". "piso de madera" NO es simil_madera.

8. **Respuestas cortas y contexto**: Si el asistente preguntó por el ancho y el cliente responde "2" o "1.20",
   es `ancho_m`. Si preguntó por el espesor y responde "3", es `espesor_mm`. Si preguntó ambos y responde
   "2 y 2" o "3 y 1.20", el primer número es espesor y el segundo es ancho.

9. **Mascotas — razas**: pitbull, rottweiler, dogo, ovejero → `animal="perro"`, `size="grande"`.

10. **Mensajes operativos / institucionales**: saludos, despedidas, agradecimientos,
    preguntas de precio, envío, pago, factura, horarios, ubicación, retiro, **visitar el local o
    ver productos en persona**, pedir un asesor/persona, frustración, o mensajes de proveedores →
    devolvé `intent=null, should_search=false, missing=[], next_question=null`.
    El intent operativo MANDA aunque el mensaje nombre un producto: lo que el cliente quiere
    es la acción operativa, no buscar catálogo. El CatalogAgent responde lo institucional
    desde su prompt (dirección, envíos, derivación, etc.).
    Ejemplos operativos (intent=null aunque mencionen producto):
    - "me gustaría ver los pisos en persona" → `intent=null` (es visita/ubicación, no búsqueda)
    - "¿puedo pasar a ver las mangueras?" → `intent=null`
    - "¿cuánto sale el piso de 3mm?" → `intent=null` (precio)
    - "¿hacen envíos de los pisos?" → `intent=null` (envío)

    **Consultas de disponibilidad** (distinto de lo anterior): mensaje con "¿Tienen...?",
    "¿Tenés...?", "¿Hay...?", "¿Vendés...?" que pregunta si EXISTE un producto/material/característica
    → `should_search=true` SIEMPRE, aunque falten datos (espesor, ancho, m2, etc.).
    El CatalogAgent buscará lo disponible y pedirá datos adicionales si hace falta.
    Ejemplos:
    - "¿tienen pegamento?" → `intent="general", should_search=true`
    - "¿tienen piso moneda PVC?" → `intent="pisos", should_search=true`
    - "¿tienen pisos de 4mm?" → `intent="pisos", should_search=true` (aunque falten floor_kind, ancho, m2)
    - "¿tienen manguera de riego?" → `intent="mangueras", should_search=true`
    "Stock" de un producto pedido anteriormente sí es operativo → `intent=null`.

11. **Antideslizante**: Si el cliente lo pide, agregar `"antideslizante"` a `tags` y establecer
    `floor_kind="diseno"` si no hay otro floor_kind.

12. **Diseño vs liso**: "con diseño", "moneda", "semilla", "rayado", "antideslizante" → `floor_kind="diseno"`.
    "liso" → `floor_kind="liso"`.

Solo devolvé el JSON estructurado según el schema. Sin texto extra."""

# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------


def analyze_requirements(
    query: str,
    history: list[AgentMessage],
    api_key: str,
    model: str = "gpt-4.1-mini",
) -> ProductIntakeResponse:
    """Extract structured product requirements from the conversation using a LLM.

    Falls back to the deterministic system on any error so the service
    remains available even if the LLM call fails.
    """
    try:
        agent = Agent(
            model=OpenAIChat(id=model, api_key=api_key),
            system_message=SYSTEM_PROMPT,
            output_schema=ProductIntakeResponse,
            use_json_mode=True,        # avoids strict schema — dict[str, Any] incompatible with OpenAI strict mode
            add_history_to_context=False,
        )

        messages = _build_messages(query, history)
        response = agent.run(input=messages, stream=False)

        if isinstance(response.content, ProductIntakeResponse):
            return response.content

        logger.warning("requirements_agent: unexpected content type %s, falling back", type(response.content))

    except Exception as exc:
        logger.warning("requirements_agent: LLM call failed (%s), returning neutral intake", exc)

    # LLM-only pipeline: on failure return a neutral intake (intent=null,
    # should_search=false) so the CatalogAgent still answers conversationally
    # from its prompt instead of crashing. No keyword fallback.
    return ProductIntakeResponse()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_messages(query: str, history: list[AgentMessage]) -> list[Message]:
    """Convert AgentMessage history to Agno Messages, then append the current query.

    Filters out 'Datos ya recopilados:' synthetic messages injected by the
    memory system — the LLM extracts state directly from the raw conversation.
    """
    messages: list[Message] = []
    for msg in history:
        if msg.content.startswith("Datos ya recopilados:"):
            continue
        messages.append(Message(role=msg.role, content=msg.content))
    messages.append(Message(role="user", content=query))
    return messages
