from __future__ import annotations

from .models import ProductFilters, ProductIntakeResponse


_RUBBER_FLOOR_DESIGNS = {"moneda", "semilla", "semilla_melon", "rayado"}


def filters_from_intake(intake: ProductIntakeResponse) -> ProductFilters:
    """Build ProductFilters directly from LLM-extracted intake — no keyword parsing."""
    known = intake.known or {}
    rubro = known.get("rubro")
    category = known.get("category")
    floor_design = known.get("floor_design")
    tags = known.get("tags") or []

    # moneda/semilla/rayado are rubber (simil goma) designs — they live in the pisos
    # category, not pisos_vinilicos. Remove the incorrect category so the search
    # finds the right products.
    if category == "pisos_vinilicos" and floor_design in _RUBBER_FLOOR_DESIGNS:
        category = None

    # When rubro=pisos and no specific category (and no simil_madera design), exclude vinilico.
    exclude_vinilico = (
        rubro == "pisos"
        and category is None
        and floor_design != "simil_madera"
    )

    return ProductFilters(
        rubro=rubro,
        category=category,
        floor_kind=known.get("floor_kind"),
        floor_design=floor_design,
        espesor_mm=known.get("espesor_mm"),
        ancho_m=known.get("ancho_m"),
        material=known.get("material"),
        color=known.get("color"),
        tags=tags if isinstance(tags, list) else [],
        in_stock_only=True,
        exclude_vinilico=exclude_vinilico,
    )
