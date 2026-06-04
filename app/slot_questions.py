"""Generadores de preguntas de slot y utilidades numéricas.

Esto NO es extracción de intención por keywords (eso lo hace el RequirementsAgent
vía LLM). Son helpers deterministas que operan sobre el estado YA estructurado que
devuelve el LLM: arman la próxima pregunta de slot faltante y calculan superficie
derivada de rollos. Ver AGENT.md, sección "Lo Que NO Cambia".
"""
from __future__ import annotations

from typing import Any


def floor_next_question(known: dict[str, Any], missing: list[str]) -> str:
    missing_set = set(missing)

    if missing == ["floor_kind_or_design", "espesor_mm", "ancho_m", "requested_m2"] and not known.get("use"):
        return "¿Para qué lo vas a usar? Por ejemplo: gimnasio, oficina, rampa, ascensor, salón o depósito."
    if missing == ["espesor_mm", "ancho_m"]:
        return "¿Qué espesor y ancho buscás? Por ejemplo: 3 mm y 1,20 m."
    if missing == ["floor_kind_or_design"]:
        return "¿Lo querés liso o con algún diseño, como moneda, semilla o rayado?"
    if missing == ["requested_m2"]:
        return "¿Cuántos m² necesitás cubrir?"
    if missing == ["requested_m2_confirmation"]:
        value = known.get("ambiguous_requested_m2")
        if value is not None:
            return f"Confirmame: ¿{value:g} metros cuadrados es lo que querés cubrir?"
        return "Confirmame: ¿esos metros son metros cuadrados a cubrir?"
    if missing == ["ancho_m"]:
        return "¿Qué ancho buscás? Por ejemplo: 1 m, 1,20 m o 1,50 m."
    if missing == ["espesor_mm"]:
        return "¿Qué espesor buscás? Por ejemplo: 2 mm, 2,5 mm o 3 mm."
    if missing == ["floor_kind_or_design", "ancho_m"]:
        return "¿Lo preferís liso o con diseño? ¿Y qué ancho te sirve?"
    if missing == ["floor_kind_or_design", "requested_m2"]:
        return "¿Lo preferís liso o con diseño? ¿Y cuántos m² necesitás cubrir?"

    if set(missing) == {"espesor_mm", "requested_m2_confirmation"}:
        value = known.get("ambiguous_requested_m2")
        confirmation = (
            f"¿{value:g} metros cuadrados es lo que querés cubrir?"
            if value is not None
            else "¿esos metros son metros cuadrados a cubrir?"
        )
        return f"¿Qué espesor buscás? Por ejemplo: 2 mm, 2,5 mm o 3 mm. Y confirmame: {confirmation}"

    questions = []
    if "floor_kind_or_design" in missing_set:
        questions.append("si lo querés liso o con diseño")
    if "espesor_mm" in missing_set:
        questions.append("el espesor")
    if "ancho_m" in missing_set:
        questions.append("el ancho")
    if "requested_m2" in missing_set:
        questions.append("cuántos m² necesitás cubrir")
    if "requested_m2_confirmation" in missing_set:
        value = known.get("ambiguous_requested_m2")
        if value is not None:
            questions.append(f"si {value:g} metros son m² a cubrir")
        else:
            questions.append("si esos metros son m² a cubrir")

    if not questions:
        return "¿Me pasás un poco más de detalle del piso que necesitás?"
    return f"Para ubicar la opción correcta, ¿me decís {join_human(questions)}?"


def hose_next_question(known: dict[str, Any], missing: list[str]) -> str:
    if missing == ["use"]:
        return "¿Para qué uso sería la manguera? Por ejemplo: riego, jardín, agua o industrial."
    if missing == ["diameter"]:
        return "¿Qué diámetro necesitás? Por ejemplo: 1/2, 3/4 o 1 pulgada."
    if missing == ["length_m"]:
        return "¿Cuántos metros necesitás?"

    questions = []
    if "use" in missing:
        questions.append("el uso")
    if "diameter" in missing:
        questions.append("el diámetro")
    if "length_m" in missing:
        questions.append("cuántos metros")
    return f"Para ubicar la manguera correcta, decime {join_human(questions)}."


def derived_roll_surface_m2(roll_count: int | None, roll_length_m: float | None, ancho_m: float | None) -> float | None:
    if roll_count is None or roll_length_m is None or ancho_m is None:
        return None
    return round(float(roll_count) * float(roll_length_m) * float(ancho_m), 2)


def join_human(values: list[str]) -> str:
    if not values:
        return ""
    if len(values) == 1:
        return values[0]
    if len(values) == 2:
        return f"{values[0]} y {values[1]}"
    return ", ".join(values[:-1]) + f" y {values[-1]}"
