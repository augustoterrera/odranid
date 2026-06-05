"""Single source of truth for Odranid domain vocabulary.

Holds the search-side synonyms, the floor-design classification rules and the
rubro vocabulary that used to be duplicated across ``normalization.py``,
``query_parser.py``, the agent prompt and the agent code. Other modules import
from here so the domain knowledge lives in one place:

- the Typesense index config (Fase 3.3/3.4) builds its synonym sets from ``SYNONYM_GROUPS``;
- ``normalization.floor_taxonomy`` classifies designs from ``FLOOR_DESIGN_RULES``;
- ``query_parser`` reads ``RUBBER_FLOOR_DESIGNS``;
- the agent prompt references these instead of re-encoding them.

This module is intentionally dependency-free (pure data + trivial helpers) so
anything can import it without circular imports.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Search synonyms
# ---------------------------------------------------------------------------
# Each inner list is a set of terms that should be treated as equivalent at
# search time. Order within a group is not significant. Keep terms lowercased.
SYNONYM_GROUPS: list[list[str]] = [
    ["goma", "caucho"],
    ["simil goma", "pvc", "vinilico"],
    ["ranurado", "con diseno", "diseno"],
    ["ignifugo", "ignifuga", "retardante de llama", "antillama"],
    ["antideslizante", "antirresbalante"],
    ["alto transito", "trafico intenso", "uso intensivo"],
]

# ---------------------------------------------------------------------------
# Rubro vocabulary
# ---------------------------------------------------------------------------
RUBROS: tuple[str, ...] = ("pisos", "mangueras", "hogar", "calzado", "mascotas", "general")

# ---------------------------------------------------------------------------
# Floor-design classification
# ---------------------------------------------------------------------------
# Ordered rules: the first group whose any keyword appears in the normalized
# product text wins. Mirrors the historical order in normalization.floor_taxonomy
# (behavior-preserving extraction).
FLOOR_DESIGN_RULES: list[tuple[list[str], str, str]] = [
    (["simil madera", "madera"], "diseno", "simil_madera"),
    (["vinilico"], "diseno", "vinilico"),
    (["semilla melon"], "diseno", "semilla_melon"),
    (["semilla"], "diseno", "semilla"),
    (["moneda"], "diseno", "moneda"),
    (["rayado", "rayada", "estriado"], "diseno", "rayado"),
]
DEFAULT_FLOOR: tuple[str, None] = ("liso", None)

# Rubber (simil goma) designs — these live in the pisos category, never in
# pisos_vinilicos. Used by the agent filter builder.
RUBBER_FLOOR_DESIGNS: frozenset[str] = frozenset({"moneda", "semilla", "semilla_melon", "rayado"})

# Designs that are mutually compatible alternatives at search time (a request
# for ``semilla`` should also surface ``semilla_melon``).
DESIGN_COMPATIBILITY: dict[str, frozenset[str]] = {
    "semilla": frozenset({"semilla", "semilla_melon"}),
}


def synonyms_for(term: str) -> list[str]:
    """Return all terms equivalent to ``term`` (including itself)."""
    needle = term.strip().lower()
    for group in SYNONYM_GROUPS:
        if needle in group:
            return list(group)
    return [needle]


def compatible_designs(design: str | None) -> frozenset[str]:
    """Return the set of designs that satisfy a request for ``design``."""
    if not design:
        return frozenset()
    return DESIGN_COMPATIBILITY.get(design, frozenset({design}))
