from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.catalog.normalization import extract_woocommerce_products, normalize_product  # noqa: E402


def main() -> None:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data" / "productos.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    products = [normalize_product(product) for product in extract_woocommerce_products(payload)]

    print(f"productos_normalizados={len(products)}")
    print_section("rubros", Counter(p.rubro for p in products))
    print_section("categorias", Counter(p.category for p in products))
    print_section("disenos_piso", Counter(p.floor_design for p in products if p.floor_design))
    print_section("espesores_piso_mm", Counter(str(p.specs.espesor_mm) for p in products if p.rubro == "pisos" and p.specs.espesor_mm is not None))
    print_section("anchos_piso_m", Counter(str(p.specs.ancho_m) for p in products if p.rubro == "pisos" and p.specs.ancho_m is not None))
    print_section("tags_tecnicos", Counter(tag for p in products for tag in p.technical_tags))


def print_section(title: str, counter: Counter[str]) -> None:
    print(f"\n[{title}]")
    for name, count in counter.most_common(30):
        print(f"{count:>4} {name}")


if __name__ == "__main__":
    main()
