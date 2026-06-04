"""Lógica de calzado: talles.

Los productos de calzado declaran su rango de talles en el título
("Del 21 Al 34", "34/45"). Cuando el cliente pide un talle puntual, una bota
cuyo rango no lo incluye no le sirve (no es un simple "menos relevante": no le
entra). Estas funciones puras extraen el talle pedido del mensaje y el rango del
título para poder descartar/posponer esas opciones. Son del dominio calzado, sin
estado, y se testean sin servidor — igual que ``coverage`` para pisos.
"""
from __future__ import annotations

import re

# Talles de calzado razonables (niño chico .. adulto grande). Acota falsos
# positivos con otros números del título (espesores, anchos, etc.).
MIN_TALLE = 18
MAX_TALLE = 50

# "talle 42", "talla 42", "n° 42", "nro 42" — exige la palabra para no confundir
# un talle con cualquier número suelto del mensaje.
_REQUESTED_TALLE = re.compile(r"\b(?:talles?|tallas?|n(?:ro|umero|[°ºo])?)\s*[:.]?\s*(\d{2})\b")

# "del 21 al 34", "21 al 34", "34/45": un rango explícito en el título.
_TALLE_RANGE = re.compile(r"\b(?:del\s*)?(\d{2})\s*(?:al|/)\s*(\d{2})\b")


def extract_requested_talle(text: str | None) -> int | None:
    if not text:
        return None
    match = _REQUESTED_TALLE.search(text.lower())
    if not match:
        return None
    value = int(match.group(1))
    return value if MIN_TALLE <= value <= MAX_TALLE else None


def parse_talle_range(title: str | None) -> tuple[int, int] | None:
    if not title:
        return None
    match = _TALLE_RANGE.search(title.lower())
    if not match:
        return None
    low, high = int(match.group(1)), int(match.group(2))
    if MIN_TALLE <= low <= high <= MAX_TALLE:
        return low, high
    return None


def talle_excluded(title: str | None, requested_talle: int) -> bool:
    """True si el título declara un rango de talles que NO incluye el pedido.
    Si el título no declara rango, no se puede descartar (devuelve False)."""
    talle_range = parse_talle_range(title)
    if talle_range is None:
        return False
    low, high = talle_range
    return not (low <= requested_talle <= high)
