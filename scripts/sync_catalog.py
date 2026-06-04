from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402
from app.embeddings import OpenAIEmbeddingClient, chunked  # noqa: E402
from app.models import ProductDocument  # noqa: E402
from app.normalization import extract_woocommerce_products, normalize_product  # noqa: E402
from app.postgres_store import build_postgres_store_from_settings  # noqa: E402
from app.woocommerce import build_client_from_settings  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync Odranid catalog into Postgres")
    parser.add_argument("--from-file", type=Path, default=None, help="Use a local WooCommerce snapshot instead of the API")
    parser.add_argument("--no-embeddings", action="store_true", help="Upsert rows without generating embeddings")
    parser.add_argument("--dry-run", action="store_true", help="Normalize and optionally embed, but do not write to the database")
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--embedding-batch-size", type=int, default=64)
    parser.add_argument("--embedding-cache", type=Path, default=ROOT / ".cache" / "embeddings.json")
    args = parser.parse_args()

    products = load_products(args.from_file)
    print(f"productos_fuente={len(products)}")

    documents = [normalize_product(product) for product in products]
    print(f"productos_normalizados={len(documents)}")

    content_hash_by_id = {document.id: content_hash(document.content) for document in documents}
    embeddings_by_id: dict[int, list[float] | None] = {}
    if args.no_embeddings:
        embeddings_by_id = {document.id: None for document in documents}
        print("embeddings=omitidos")
    else:
        if not settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is required, or run with --no-embeddings")
        cache = load_embedding_cache(args.embedding_cache)
        embedder = OpenAIEmbeddingClient(settings.openai_api_key, settings.embedding_model)

        missing_documents = []
        for document in documents:
            cache_key = embedding_cache_key(settings.embedding_model, content_hash_by_id[document.id])
            vector = cache.get(cache_key)
            if vector is not None:
                embeddings_by_id[document.id] = vector
            else:
                missing_documents.append(document)

        print(f"embeddings_cache_hit={len(embeddings_by_id)}/{len(documents)}")

        for batch in chunked(missing_documents, args.embedding_batch_size):
            vectors = embedder.embed_many([document.content for document in batch])
            for document, vector in zip(batch, vectors, strict=True):
                embeddings_by_id[document.id] = vector
                cache[embedding_cache_key(settings.embedding_model, content_hash_by_id[document.id])] = vector
            save_embedding_cache(args.embedding_cache, cache)
            print(f"embeddings_generados={len(embeddings_by_id)}/{len(documents)}")

    rows = [to_catalog_row(document, embeddings_by_id.get(document.id), content_hash_by_id[document.id]) for document in documents]
    if args.dry_run:
        print(f"dry_run=true rows_preparadas={len(rows)}")
        return

    store = build_postgres_store_from_settings(settings)

    uploaded = 0
    for batch in chunked(rows, args.batch_size):
        store.upsert_products(batch)
        uploaded += len(batch)
        print(f"upsert={uploaded}/{len(rows)}")

    print("sync_ok=true")


def load_products(path: Path | None) -> list[dict[str, Any]]:
    if path is not None:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return extract_woocommerce_products(payload)

    return build_client_from_settings(settings).fetch_products()


def to_catalog_row(document: ProductDocument, embedding: list[float] | None, document_content_hash: str | None = None) -> dict[str, Any]:
    specs = document.specs
    row = {
        "id": document.id,
        "title": document.title,
        "slug": document.slug,
        "link": document.link,
        "image": document.image,
        "price": document.price,
        "currency": document.currency,
        "in_stock": document.in_stock,
        "stock_text": document.stock_text,
        "rubro": document.rubro,
        "category": document.category,
        "subcategory": document.subcategory,
        "product_type": document.product_type,
        "floor_kind": document.floor_kind,
        "floor_design": document.floor_design,
        "material": document.material,
        "color": document.color,
        "environments": document.environments,
        "brands": document.brands,
        "categories": document.categories,
        "woo_tags": document.woo_tags,
        "technical_tags": document.technical_tags,
        "espesor_mm": specs.espesor_mm,
        "ancho_m": specs.ancho_m,
        "largo_m": specs.largo_m,
        "rendimiento_m2": specs.rendimiento_m2,
        "diametro_mm": specs.diametro_mm,
        "largo_manguera_m": specs.largo_manguera_m,
        "content": document.content,
        "metadata": document.metadata,
        "raw_attributes": document.raw_attributes,
        "content_hash": document_content_hash or content_hash(document.content),
    }
    if embedding is not None:
        row["embedding"] = embedding
    return row


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def embedding_cache_key(model: str, document_content_hash: str) -> str:
    return f"{model}:{document_content_hash}"


def load_embedding_cache(path: Path) -> dict[str, list[float]]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return {}
    return {str(key): value for key, value in payload.items() if isinstance(value, list)}


def save_embedding_cache(path: Path, cache: dict[str, list[float]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache), encoding="utf-8")


if __name__ == "__main__":
    main()
