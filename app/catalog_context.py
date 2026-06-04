from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path

from .models import ProductDocument


class CatalogContextCache:
    def __init__(self, cache_file: Path):
        self.cache_file = cache_file
        self._context: str | None = None

    def get(self, products: list[ProductDocument]) -> str:
        if self._context is None:
            self._context = self.build(products)
            self.cache_file.write_text(self._context, encoding="utf-8")
        return self._context

    def invalidate(self) -> None:
        self._context = None
        if self.cache_file.exists():
            self.cache_file.unlink()

    def build(self, products: list[ProductDocument]) -> str:
        by_rubro = Counter(p.rubro for p in products)
        by_category = Counter(p.category for p in products)
        floor_designs = Counter(p.floor_design for p in products if p.floor_design)
        floor_widths = sorted({p.specs.ancho_m for p in products if p.rubro == "pisos" and p.specs.ancho_m is not None})
        floor_thickness = sorted({p.specs.espesor_mm for p in products if p.rubro == "pisos" and p.specs.espesor_mm is not None})

        stock_by_rubro: dict[str, int] = defaultdict(int)
        for product in products:
            if product.in_stock:
                stock_by_rubro[product.rubro] += 1

        lines = [
            "CONTEXTO CACHEADO DEL CATALOGO ODRANID",
            f"Productos indexados: {len(products)}",
            "",
            "Rubros disponibles:",
            *[f"- {name}: {count} productos, {stock_by_rubro.get(name, 0)} en stock" for name, count in by_rubro.most_common()],
            "",
            "Categorias principales mas frecuentes:",
            *[f"- {name}: {count}" for name, count in by_category.most_common(12)],
            "",
            "Facetas criticas para pisos:",
            f"- Espesores detectados en mm: {', '.join(format_number(x) for x in floor_thickness) or 'N/D'}",
            f"- Anchos detectados en m: {', '.join(format_number(x) for x in floor_widths) or 'N/D'}",
            f"- Disenos: {', '.join(f'{name} ({count})' for name, count in floor_designs.most_common()) or 'N/D'}",
            "",
            "Regla operativa:",
            "- La IA no debe inventar productos, medidas ni links.",
            "- Primero debe extraer intencion y facetas; despues buscar.",
            "- Si una faceta exacta no devuelve resultados, relajar medidas antes que rubro/diseno.",
            "- Los m2 de cobertura del cliente no son ancho ni espesor; son una necesidad de calculo.",
        ]
        return "\n".join(lines)


def format_number(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{value:g}"
