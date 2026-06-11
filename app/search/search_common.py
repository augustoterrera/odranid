from __future__ import annotations

import re

from ..core.models import ProductDocument, SearchHit
from ..catalog.normalization import norm_num, norm_text


_FLOOR_DESIGN_TERMS = ["diseno", "moneda", "semilla", "rayado", "simil madera", "simil_madera", "antideslizante"]
_FLOOR_KIND_AMBIGUOUS = re.compile(r"no\s+importa|da\s+igual|cualquier|indistinto|o\s+con\s+diseno|liso\s+o\b")


def infer_floor_kind(text: str) -> str | None:
    """Garantía determinística del tipo de piso pedido (espejo de la regla 12 de intake).

    Si el cliente dijo "liso" (y no mencionó diseños), el filtro floor_kind=liso se aplica
    aunque el LLM no lo haya emitido: pedir liso y recibir moneda es el bug de producción
    que esto previene. Ante ambigüedad ("liso o con diseño", "no importa") no se infiere.
    """
    normalized = norm_text(text)
    if _FLOOR_KIND_AMBIGUOUS.search(normalized):
        return None
    has_liso = bool(re.search(r"\blisos?\b", normalized))
    has_design = any(term in normalized for term in _FLOOR_DESIGN_TERMS)
    if has_liso and not has_design:
        return "liso"
    if has_design and not has_liso:
        return "diseno"
    return None


def post_filter_specific_terms(query: str, hits: list[SearchHit], limit: int) -> list[SearchHit]:
    terms = specific_required_terms(query)
    if not terms:
        return hits[:limit]

    filtered = [hit for hit in hits if all(specific_term_matches(term, searchable_product_text(hit.product)) for term in terms)]
    return filtered[:limit]


def specific_required_terms(query: str) -> list[str]:
    text = norm_text(query)
    terms = []
    if has_word(text, "tejo") or has_word(text, "tejos"):
        terms.append("tejo")
    if has_word(text, "frisbee"):
        terms.append("frisbee")
    hose_terms = required_hose_terms(text)
    terms.extend(term for term in hose_terms if term not in terms)
    return terms


def required_hose_terms(text: str) -> list[str]:
    if not any(term in text for term in ["manguera", "riego", "jardin", "jardin", "diametro"]):
        return []

    terms: list[str] = []
    for match in re.finditer(r"\b(\d+\s*/\s*\d+)\b", text):
        terms.append(match.group(1).replace(" ", ""))

    for match in re.finditer(r"\b(\d+(?:[.,]\d+)?)\s*mm\b", text):
        value = norm_num(match.group(1))
        if value is not None:
            terms.append(rf"re:(?<![0-9]){value:g}\s*mm\b")

    for match in re.finditer(r"\b(\d+(?:[.,]\d+)?)\s*(?:interno|inter|int)\b", text):
        value = norm_num(match.group(1))
        if value is not None and value >= 10:
            terms.append(rf"re:(?<![0-9]){value:g}\s*mm\b")

    for match in re.finditer(r"\b(\d+(?:[.,]\d+)?)\s*cm\b", text):
        value = norm_num(match.group(1))
        if value is not None:
            terms.append(rf"re:(?<![0-9]){value:g}\s*cm\b|(?<![0-9]){value * 10:g}\s*mm\b")

    length_match = re.search(r"\b(\d+(?:[.,]\d+)?)\s*(?:m|mt|mts|metro|metros)\b", text)
    if length_match:
        value = norm_num(length_match.group(1))
        if value is not None and value >= 5:
            terms.append(f"{value:g}")

    return terms


def specific_term_matches(term: str, text: str) -> bool:
    if term.startswith("re:"):
        return bool(re.search(term[3:], text))
    return term in text


def searchable_product_text(product: ProductDocument) -> str:
    return norm_text(
        " ".join(
            [
                product.title,
                product.content,
                product.category,
                product.subcategory or "",
                " ".join(product.technical_tags),
            ]
        )
    )


def has_word(text: str, word: str) -> bool:
    return any(part == word for part in text.split())
