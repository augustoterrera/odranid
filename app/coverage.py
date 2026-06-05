from __future__ import annotations

import math
import re

from .core.models import CoverageCalculation, ProductDocument, SearchResponse
from .normalization import norm_num, norm_text


def enrich_search_response(response: SearchResponse) -> SearchResponse:
    requested_m2 = extract_requested_m2(response.query)
    if requested_m2 is None:
        return response

    for hit in response.hits:
        hit.coverage = calculate_coverage(hit.product, requested_m2)

    response.requested_m2 = requested_m2
    return response


def extract_requested_m2(query: str) -> float | None:
    text = norm_text(query)
    patterns = [
        r"(\d+(?:[.,]\d+)?)\s*(?:m2|m²|mts2)\b",
        r"(\d+(?:[.,]\d+)?)\s*metros?\s*cuadrados?\b",
        r"cubrir\s*(\d+(?:[.,]\d+)?)\s*(?:m2|m²|mts2|metros?\s*cuadrados?)\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            value = norm_num(match.group(1))
            if value is not None and value > 0:
                return value

    dimension_area = extract_area_from_dimensions(text)
    if dimension_area is not None:
        return dimension_area
    return None


def extract_area_from_dimensions(text: str) -> float | None:
    if not any(
        term in text
        for term in ["cubrir", "superficie", "sector", "sectores", "aprox", "aproximadamente", "cuadrado", "cuadrados"]
    ):
        return None
    match = re.search(
        r"\b(\d+(?:[.,]\d+)?)\s*(?:m|mt|mts|metro|metros)?\s*(?:x|\*)\s*(\d+(?:[.,]\d+)?)\s*(?:m|mt|mts|metro|metros)?\b",
        text,
    )
    if not match:
        return None
    first = norm_num(match.group(1))
    second = norm_num(match.group(2))
    if first is None or second is None or first <= 0 or second <= 0:
        return None
    area = first * second
    return round(area, 2) if area > 0 else None


def calculate_coverage(product: ProductDocument, requested_m2: float) -> CoverageCalculation:
    specs = product.specs
    sale_unit = effective_sale_unit(product)

    if sale_unit == "m2":
        quantity = round_decimal(requested_m2)
        return CoverageCalculation(
            requested_m2=round_decimal(requested_m2),
            sale_unit=sale_unit,
            quantity_m2=quantity,
            message=f"Para cubrir {format_number(requested_m2)} m2, recomendar {format_number(quantity)} m2 de este producto.",
        )

    # Vendido cortado a medida: el cliente pide los metros que quiera, no se calcula cantidad.
    if sale_unit == "metro_lineal":
        return cut_to_measure_coverage(requested_m2, specs.ancho_m, sale_unit)

    # Rollos/cortes: solo contar unidades si es un rollo real (largo/rendimiento creíbles)
    # o un corte. Si no, el rendimiento es un dato basura (ej. largo=1 por defecto) y daría
    # cantidades absurdas ("15 rollos"); en ese caso se vende cortado a medida.
    coverage_m2, source = coverage_per_unit(product)
    if coverage_m2 is not None and coverage_m2 > 0 and (sale_unit == "corte" or has_roll_length(product)):
        rolls_needed = max(1, math.ceil(requested_m2 / coverage_m2))
        surplus = max(0.0, rolls_needed * coverage_m2 - requested_m2)
        unit_label = coverage_unit_label(sale_unit)
        return CoverageCalculation(
            requested_m2=round_decimal(requested_m2),
            sale_unit=sale_unit,
            coverage_m2=round_decimal(coverage_m2),
            coverage_source=source,
            rolls_needed=rolls_needed,
            surplus_m2=round_decimal(surplus),
            message=(
                f"Para cubrir {format_number(requested_m2)} m2, cada {unit_label} cubre "
                f"{format_number(coverage_m2)} m2. Necesitás {rolls_needed} {pluralize(unit_label, rolls_needed)}."
            ),
        )

    # Sin largo de rollo (solo ancho): se vende cortado a medida.
    if specs.ancho_m is not None and specs.ancho_m > 0:
        return cut_to_measure_coverage(requested_m2, specs.ancho_m, sale_unit)

    return CoverageCalculation(
        requested_m2=round_decimal(requested_m2),
        sale_unit=sale_unit,
        needs_advisor=True,
        message="El producto no tiene medidas suficientes para calcular cobertura automaticamente; derivar a asesor.",
    )


def cut_to_measure_coverage(requested_m2: float, ancho_m: float | None, sale_unit: str) -> CoverageCalculation:
    ancho_txt = f" (ancho {format_number(ancho_m)} m)" if ancho_m else ""
    return CoverageCalculation(
        requested_m2=round_decimal(requested_m2),
        sale_unit=sale_unit,
        coverage_source="corte_a_medida",
        message=(
            f"Este se vende cortado a medida{ancho_txt}: podés pedir la cantidad de metros que "
            f"necesites para cubrir tus {format_number(requested_m2)} m2."
        ),
    )


def effective_sale_unit(product: ProductDocument) -> str:
    sale_unit = product.product_type or "unidad"
    if sale_unit == "m2":
        return "m2"
    # Si tiene un rollo real (largo > 1m o rendimiento que implica largo), calcular rollos
    # aunque el tipo o la descripción digan "metro" — corrige misclasificaciones de ingesta.
    if has_roll_length(product):
        return "rollo" if sale_unit == "metro_lineal" else sale_unit
    # Sin rollo real y con señal de venta por metro: cortado a medida.
    if sale_unit == "metro_lineal" or is_linear_meter_product(product):
        return "metro_lineal"
    return sale_unit


def has_roll_length(product: ProductDocument) -> bool:
    specs = product.specs
    if specs.largo_m is not None and specs.largo_m > 1:
        return True
    if specs.rendimiento_m2 is not None and specs.ancho_m and specs.rendimiento_m2 > specs.ancho_m + 0.01:
        return True
    return False


def is_linear_meter_product(product: ProductDocument) -> bool:
    text = norm_text(" ".join([product.title, product.slug or "", product.content]))
    explicit_linear_terms = ["metro lineal", "metros lineales", "mt lineal", "mts lineales", "m lineal"]
    return any(needle in text for needle in explicit_linear_terms) or bool(
        re.search(r"\b(?:x|por)\s+metro\b(?!\s+cuadrado)", text)
    )


def coverage_per_unit(product: ProductDocument) -> tuple[float | None, str | None]:
    specs = product.specs
    if specs.rendimiento_m2 is not None and specs.rendimiento_m2 > 0:
        return specs.rendimiento_m2, "rendimiento_m2"
    if (
        specs.ancho_m is not None
        and specs.ancho_m > 0
        and specs.largo_m is not None
        and specs.largo_m > 0
    ):
        return specs.ancho_m * specs.largo_m, "ancho_m_x_largo_m"
    return None, None


def coverage_unit_label(sale_unit: str) -> str:
    if sale_unit == "rollo":
        return "rollo"
    if sale_unit == "corte":
        return "corte"
    return "unidad"


def round_decimal(value: float) -> float:
    return round(float(value), 2)


def format_number(value: float) -> str:
    rounded = round_decimal(value)
    if rounded.is_integer():
        return str(int(rounded))
    return f"{rounded:g}"


def pluralize(label: str, count: int) -> str:
    if count == 1:
        return label
    if label.endswith("s"):
        return label
    return f"{label}s"
