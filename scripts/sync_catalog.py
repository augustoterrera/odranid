from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.catalog.catalog_sync import DEFAULT_EMBEDDING_CACHE, run_catalog_to_postgres_sync  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync Odranid catalog into Postgres")
    parser.add_argument("--from-file", type=Path, default=None, help="Use a local WooCommerce snapshot instead of the API")
    parser.add_argument("--no-embeddings", action="store_true", help="Upsert rows without generating embeddings")
    parser.add_argument("--dry-run", action="store_true", help="Normalize and optionally embed, but do not write to the database")
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--embedding-batch-size", type=int, default=64)
    parser.add_argument("--embedding-cache", type=Path, default=DEFAULT_EMBEDDING_CACHE)
    args = parser.parse_args()

    run_catalog_to_postgres_sync(
        from_file=args.from_file,
        no_embeddings=args.no_embeddings,
        dry_run=args.dry_run,
        batch_size=args.batch_size,
        embedding_batch_size=args.embedding_batch_size,
        embedding_cache=args.embedding_cache,
        progress=print,
    )


if __name__ == "__main__":
    main()
