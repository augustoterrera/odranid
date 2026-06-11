"""Aserciones determinísticas para los evals conversacionales.

Cada asercion es una función chica registrada por nombre; los casos YAML las referencian
así (string pelado = sin argumento, dict de una clave = con argumento):

    asserts:
      - tool_called: buscar_productos
      - presents_product
      - not_asks: ["ancho"]

Para agregar una asercion nueva: escribir la función con la firma
``(ctx: EvalContext, arg) -> str | None`` (None = pasa, string = motivo del fallo)
y decorarla con ``@register("nombre")``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from app.agents.pydantic_agent import answer_presents_any_product
from app.catalog.normalization import norm_text
from app.core.models import AgentResponse, SearchResponse

Assertion = Callable[["EvalContext", Any], "str | None"]
REGISTRY: dict[str, Assertion] = {}

# Invariantes que se verifican en TODOS los casos, sin declararlos.
GLOBAL_ASSERTS: list[str] = ["no_prices", "brand_rules", "only_allowed_links"]

FIXED_SAFE_LINKS = {
    "https://wa.me/5491125539459",
    "https://maps.app.goo.gl/zMfBWeQwwPKFGBa89",
}


@dataclass
class EvalContext:
    response: AgentResponse
    search_responses: list[SearchResponse] = field(default_factory=list)

    @property
    def answer(self) -> str:
        return self.response.answer

    @property
    def normalized_answer(self) -> str:
        return norm_text(self.response.answer)

    @property
    def question_segments(self) -> list[str]:
        """Preguntas de la respuesta: spans ¿...? si existen; si no, líneas con '?'.

        El span evita falsos positivos cuando una afirmación y una pregunta comparten
        línea ("No tengo de 2mm... ¿Querés ver opciones?" no pregunta por el espesor).
        """
        spans = re.findall(r"¿[^?¿]*\?", self.response.answer)
        if spans:
            return [norm_text(span) for span in spans]
        return [norm_text(line) for line in self.response.answer.splitlines() if "?" in line]


def register(name: str) -> Callable[[Assertion], Assertion]:
    def decorator(func: Assertion) -> Assertion:
        REGISTRY[name] = func
        return func

    return decorator


def run_asserts(case_asserts: list[Any], ctx: EvalContext) -> list[str]:
    failures: list[str] = []
    seen: set[str] = set()
    for item in [*GLOBAL_ASSERTS, *case_asserts]:
        if isinstance(item, str):
            name, arg = item, None
        elif isinstance(item, dict) and len(item) == 1:
            name, arg = next(iter(item.items()))
        else:
            failures.append(f"asercion malformada: {item!r}")
            continue
        if name in seen and name in GLOBAL_ASSERTS:
            continue
        seen.add(name)
        func = REGISTRY.get(name)
        if func is None:
            failures.append(f"asercion desconocida: {name!r} (disponibles: {sorted(REGISTRY)})")
            continue
        failure = func(ctx, arg)
        if failure:
            failures.append(f"[{name}] {failure}")
    return failures


def _terms(arg: Any) -> list[str]:
    if arg is None:
        return []
    if isinstance(arg, str):
        return [norm_text(arg)]
    return [norm_text(term) for term in arg]


# --- Invariantes globales ---------------------------------------------------


@register("no_prices")
def no_prices(ctx: EvalContext, _: Any) -> str | None:
    if re.search(r"\$\s*\d", ctx.answer) or re.search(r"\b\d[\d.,]*\s*(?:pesos|ars)\b", ctx.normalized_answer):
        return f"la respuesta muestra precios: {ctx.answer!r}"
    return None


@register("brand_rules")
def brand_rules(ctx: EvalContext, _: Any) -> str | None:
    """El bot no debe usar estos términos por su cuenta. Citar el título textual de un
    producto devuelto por la búsqueda sí vale (mostrar el nombre exacto es obligatorio),
    así que se excluyen los términos que vienen dentro de títulos de los hits."""
    forbidden = ["afa", "ibira", "simil goma", "simil caucho", "ranurado", "metros lineales", "redondeo"]
    hit_titles = norm_text(
        " | ".join(hit.product.title for response in ctx.search_responses for hit in response.hits)
    )
    found = [
        term
        for term in forbidden
        if re.search(rf"\b{re.escape(term)}\b", ctx.normalized_answer)
        and not re.search(rf"\b{re.escape(term)}\b", hit_titles)
    ]
    if found:
        return f"términos prohibidos al cliente: {found}"
    return None


@register("only_allowed_links")
def only_allowed_links(ctx: EvalContext, _: Any) -> str | None:
    if not ctx.search_responses:
        return None  # sin búsqueda este turno no hay set permitido (igual que el guard)
    allowed = set(FIXED_SAFE_LINKS)
    for response in ctx.search_responses:
        for hit in response.hits:
            if hit.product.link:
                allowed.add(_canonical(hit.product.link))
    bad = [
        url
        for url in re.findall(r"https?://\S+", ctx.answer)
        if _canonical(url.rstrip(".,)")) not in allowed
    ]
    if bad:
        return f"links fuera de los resultados de búsqueda: {bad}"
    return None


def _canonical(link: str) -> str:
    return link.replace("https://odranid.com/producto/", "https://odranid.com.ar/producto/").rstrip("/")


# --- Aserciones de caso -----------------------------------------------------


@register("tool_called")
def tool_called(ctx: EvalContext, arg: Any) -> str | None:
    name = str(arg or "buscar_productos")
    if not any(trace.name == name for trace in ctx.response.tool_calls):
        return f"esperaba llamada a {name}; tool_calls={[t.name for t in ctx.response.tool_calls]}"
    return None


@register("no_tool_calls")
def no_tool_calls(ctx: EvalContext, _: Any) -> str | None:
    if ctx.response.tool_calls:
        return f"no esperaba herramientas; llamó {[t.name for t in ctx.response.tool_calls]}"
    return None


@register("presents_product")
def presents_product(ctx: EvalContext, _: Any) -> str | None:
    if not answer_presents_any_product(ctx.answer, ctx.search_responses):
        return "la respuesta no presenta ningún producto devuelto por la búsqueda"
    return None


@register("not_presents_product")
def not_presents_product(ctx: EvalContext, _: Any) -> str | None:
    if answer_presents_any_product(ctx.answer, ctx.search_responses):
        return "la respuesta presenta productos y no debía"
    return None


def _presented_hits(ctx: EvalContext) -> list[Any]:
    """Hits cuyo slug aparece citado en la respuesta (los que el bot realmente mostró)."""
    text = ctx.normalized_answer
    presented = []
    for response in ctx.search_responses:
        for hit in response.hits:
            link = hit.product.link or ""
            slug = link.rstrip("/").rsplit("/", 1)[-1].lower()
            if slug and slug in text:
                presented.append(hit)
    return presented


@register("presented_only")
def presented_only(ctx: EvalContext, arg: Any) -> str | None:
    """Todos los productos presentados cumplen los atributos dados, ej. {floor_kind: liso}."""
    expected = dict(arg or {})
    bad = []
    for hit in _presented_hits(ctx):
        for key, value in expected.items():
            if getattr(hit.product, key, None) != value:
                bad.append(f"{hit.product.title!r} ({key}={getattr(hit.product, key, None)!r})")
    if bad:
        return f"presentó productos que no cumplen {expected}: {bad}"
    return None


@register("presented_max_rolls")
def presented_max_rolls(ctx: EvalContext, arg: Any) -> str | None:
    """Ningún producto presentado (vendido por rollo) requiere más rollos que el límite.
    Evita recomendar rollos chicos para superficies grandes (60 m² con rollos de 6 m²)."""
    limit = int(arg if arg is not None else 6)
    bad = [
        f"{hit.product.title!r} ({hit.coverage.rolls_needed} rollos)"
        for hit in _presented_hits(ctx)
        if hit.coverage and hit.coverage.rolls_needed is not None and hit.coverage.rolls_needed > limit
    ]
    if bad:
        return f"presentó rollos demasiado chicos para la superficie (límite {limit} rollos): {bad}"
    return None


@register("requested_m2_between")
def requested_m2_between(ctx: EvalContext, arg: Any) -> str | None:
    """La búsqueda usó una superficie dentro del rango [min, max] (suma de ambientes bien
    calculada, no el primer número suelto del mensaje)."""
    low, high = float(arg[0]), float(arg[1])
    values = [r.requested_m2 for r in ctx.search_responses if r.requested_m2 is not None]
    if not values:
        return f"ninguna búsqueda llevó requested_m2 (esperaba entre {low:g} y {high:g})"
    if not any(low <= value <= high for value in values):
        return f"requested_m2={values} fuera del rango [{low:g}, {high:g}]"
    return None


@register("intent_null")
def intent_null(ctx: EvalContext, _: Any) -> str | None:
    intake = ctx.response.intake
    if intake is None:
        return None
    problems = []
    if intake.intent is not None:
        problems.append(f"intent={intake.intent!r}")
    if intake.known:
        problems.append(f"known={intake.known!r}")
    if intake.should_search:
        problems.append("should_search=true")
    if problems:
        return "esperaba intake operativo/institucional vacío: " + ", ".join(problems)
    return None


@register("intake_not_known")
def intake_not_known(ctx: EvalContext, arg: Any) -> str | None:
    """El intake no debe contener slots que el cliente nunca dio (no inventar atributos)."""
    known = (ctx.response.intake.known if ctx.response.intake else None) or {}
    found = {key: known[key] for key in (arg or []) if known.get(key) is not None}
    if found:
        return f"el intake inventó slots que el cliente no dio: {found}"
    return None


@register("should_search")
def should_search(ctx: EvalContext, arg: Any) -> str | None:
    expected = True if arg is None else bool(arg)
    actual = bool(ctx.response.intake and ctx.response.intake.should_search)
    if actual != expected:
        return f"should_search={actual}, esperaba {expected}"
    return None


@register("mentions")
def mentions(ctx: EvalContext, arg: Any) -> str | None:
    missing = [term for term in _terms(arg) if term not in ctx.normalized_answer]
    if missing:
        return f"la respuesta no menciona {missing}: {ctx.answer!r}"
    return None


@register("mentions_any")
def mentions_any(ctx: EvalContext, arg: Any) -> str | None:
    terms = _terms(arg)
    if not any(term in ctx.normalized_answer for term in terms):
        return f"la respuesta no menciona ninguno de {terms}: {ctx.answer!r}"
    return None


@register("not_mentions")
def not_mentions(ctx: EvalContext, arg: Any) -> str | None:
    found = [term for term in _terms(arg) if term in ctx.normalized_answer]
    if found:
        return f"la respuesta menciona {found} y no debía: {ctx.answer!r}"
    return None


@register("asks")
def asks(ctx: EvalContext, arg: Any) -> str | None:
    """Al menos una pregunta contiene cada término."""
    segments = ctx.question_segments
    if not segments:
        return f"esperaba una pregunta y no hay ninguna: {ctx.answer!r}"
    missing = [term for term in _terms(arg) if not any(term in segment for segment in segments)]
    if missing:
        return f"ninguna pregunta menciona {missing}: {ctx.answer!r}"
    return None


@register("not_asks")
def not_asks(ctx: EvalContext, arg: Any) -> str | None:
    """Ninguna pregunta contiene los términos (mencionarlos fuera de preguntas vale)."""
    found = [term for term in _terms(arg) if any(term in segment for segment in ctx.question_segments)]
    if found:
        return f"la respuesta pregunta por {found} y no debía: {ctx.answer!r}"
    return None


@register("max_questions")
def max_questions(ctx: EvalContext, arg: Any) -> str | None:
    limit = int(arg if arg is not None else 1)
    count = ctx.answer.count("?")
    if count > limit:
        return f"hace {count} preguntas (máximo {limit}): {ctx.answer!r}"
    return None
