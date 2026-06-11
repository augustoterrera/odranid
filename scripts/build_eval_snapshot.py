"""Regenera evals/fixtures/catalog_snapshot.json desde data/productos.json.

El snapshot es el catálogo CONGELADO que usan los evals conversacionales: productos ya
normalizados (ProductDocument), un subset acotado para que el archivo sea commiteable
(data/ está en .gitignore y el catálogo completo pesa ~10MB).

Correr cuando cambie la normalización o se quiera refrescar el surtido:
    PYTHONPATH=. uv run python scripts/build_eval_snapshot.py
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from app.catalog.normalization import extract_woocommerce_products, normalize_product

CATALOG_FILE = Path("data/productos.json")
SNAPSHOT_FILE = Path("evals/fixtures/catalog_snapshot.json")

# Mangueras es el rubro gigante (365): muestreamos priorizando los tipos que aparecen
# en las conversaciones reales. El resto de los rubros entra completo.
HOSE_PRIORITY = re.compile(r"riego|jard[ií]n|trenzad|espiral|cristal|reforzad|1/2|3/4", re.IGNORECASE)
MAX_HOSES = 90


def main() -> None:
    payload = json.loads(CATALOG_FILE.read_text(encoding="utf-8"))
    products = [normalize_product(raw) for raw in extract_woocommerce_products(payload)]

    hoses = [p for p in products if p.rubro == "mangueras"]
    others = [p for p in products if p.rubro != "mangueras"]

    priority = [p for p in hoses if HOSE_PRIORITY.search(p.title)]
    rest = [p for p in hoses if not HOSE_PRIORITY.search(p.title)]
    sampled_hoses = (priority + rest)[:MAX_HOSES]

    selected = sorted(others + sampled_hoses, key=lambda p: p.id)
    for product in selected:
        # Peso muerto para los evals: la búsqueda local usa title/content/tags.
        product.raw_attributes = {}
        product.metadata = {}
        product.image = None

    SNAPSHOT_FILE.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_FILE.write_text(
        json.dumps(
            {
                "generated_from": str(CATALOG_FILE),
                "products": [p.model_dump() for p in selected],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    by_rubro: dict[str, int] = {}
    for product in selected:
        by_rubro[product.rubro] = by_rubro.get(product.rubro, 0) + 1
    print(f"snapshot: {len(selected)} productos -> {SNAPSHOT_FILE}")
    print(by_rubro)


if __name__ == "__main__":
    main()
