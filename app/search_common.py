from __future__ import annotations

import re

from .core.models import ProductDocument, SearchHit
from .catalog.normalization import norm_num, norm_text


def post_filter_specific_terms(query: str, hits: list[SearchHit], limit: int) -> list[SearchHit]:
    terms = specific_required_terms(query)
    if not terms:
        return hits[:limit]

    filtered = [hit for hit in hits if all(term in searchable_product_text(hit.product) for term in terms)]
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

    length_match = re.search(r"\b(\d+(?:[.,]\d+)?)\s*(?:m|mt|mts|metro|metros)\b", text)
    if length_match:
        value = norm_num(length_match.group(1))
        if value is not None and value >= 5:
            terms.append(f"{value:g}")

    return terms


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
