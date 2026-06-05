from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.config import settings  # noqa: E402
from app.woocommerce import build_client_from_settings  # noqa: E402


def main() -> None:
    output_path = Path(sys.argv[1]) if len(sys.argv) > 1 else settings.catalog_file
    client = build_client_from_settings(settings)
    products = client.fetch_products()
    output_path.write_text(json.dumps(products, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"productos_descargados={len(products)}")
    print(f"archivo={output_path}")


if __name__ == "__main__":
    main()
