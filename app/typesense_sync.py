"""Reusable catalog -> Typesense sync.

Shared by the admin endpoint (manual full rebuild) and the Celery beat task
(periodic upsert refresh). Self-contained so it runs in any process (api or
worker) without relying on FastAPI app globals.
"""
from __future__ import annotations

import json
from pathlib import Path

from .config import settings
from .embeddings import OpenAIEmbeddingClient, chunked
from .models import ProductDocument
from .normalization import extract_woocommerce_products, normalize_product
from .typesense_client import build_typesense_client
from .typesense_index import sync_collection
from .woocommerce import build_client_from_settings


class TypesenseSyncError(RuntimeError):
    pass


def build_catalog_documents() -> list[ProductDocument]:
    """Load the catalog from the local snapshot if present, else WooCommerce."""
    path = settings.catalog_file
    if path is not None and Path(path).exists():
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        raw_products = extract_woocommerce_products(payload)
    else:
        raw_products = build_client_from_settings(settings).fetch_products()
    return [normalize_product(product) for product in raw_products]


def embeddings_for(documents: list[ProductDocument], batch_size: int = 64) -> dict[int, list[float] | None]:
    if not settings.openai_api_key:
        return {}
    embedder = OpenAIEmbeddingClient(settings.openai_api_key, settings.embedding_model)
    embeddings_by_id: dict[int, list[float] | None] = {}
    for batch in chunked(documents, batch_size):
        vectors = embedder.embed_many([document.content for document in batch])
        for document, vector in zip(batch, vectors, strict=True):
            embeddings_by_id[document.id] = vector
    return embeddings_by_id


def run_typesense_sync(*, recreate: bool = False) -> dict[str, object]:
    """Build/refresh the Typesense index from the normalized catalog.

    ``recreate=True`` drops and rebuilds the collection (manual full rebuild);
    ``recreate=False`` upserts in place (safe for the periodic refresh).
    """
    if not settings.typesense_api_key:
        raise TypesenseSyncError("ODRANID_TYPESENSE_API_KEY is required to sync Typesense")

    documents = build_catalog_documents()
    embeddings_by_id = embeddings_for(documents)
    client = build_typesense_client()
    indexed = sync_collection(
        client,
        settings.typesense_collection,
        documents,
        embeddings_by_id,
        recreate=recreate,
    )
    return {"ok": True, "indexed": indexed, "embeddings": len(embeddings_by_id)}
