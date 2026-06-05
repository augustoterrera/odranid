"""Reusable catalog -> Typesense sync.

Shared by the admin endpoint (manual full rebuild) and the Celery beat task
(periodic upsert refresh). Self-contained so it runs in any process (api or
worker) without relying on FastAPI app globals.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .core.config import settings
from .db_search import product_from_row
from .embeddings import OpenAIEmbeddingClient, chunked
from .core.models import ProductDocument
from .normalization import extract_woocommerce_products, normalize_product
from .postgres_store import PostgresStoreError, build_postgres_store_from_settings
from .typesense_client import build_typesense_client
from .typesense_index import sync_collection
from .woocommerce import build_client_from_settings


class TypesenseSyncError(RuntimeError):
    pass


@dataclass(frozen=True)
class CatalogDocumentBatch:
    documents: list[ProductDocument]
    embeddings_by_id: dict[int, list[float] | None] = field(default_factory=dict)

    def __iter__(self):
        return iter(self.documents)

    def __len__(self) -> int:
        return len(self.documents)

    def __getitem__(self, index: int) -> ProductDocument:
        return self.documents[index]


def build_catalog_documents(store: Any | None = None) -> CatalogDocumentBatch:
    """Load catalog documents and any stored embeddings.

    In production, Postgres is the system of record. The snapshot/WooCommerce
    path remains only for local/test runs without a configured database URL.
    """
    if settings.database_url:
        active_store = store or build_postgres_store_from_settings(settings)
        rows = active_store.list_products()
        documents = [product_from_row(row) for row in rows]
        embeddings_by_id = {int(row["id"]): row.get("embedding") for row in rows if row.get("embedding") is not None}
        return CatalogDocumentBatch(documents=documents, embeddings_by_id=embeddings_by_id)

    return CatalogDocumentBatch(documents=build_fallback_catalog_documents(), embeddings_by_id={})


def build_fallback_catalog_documents() -> list[ProductDocument]:
    """Load the catalog from the local snapshot if present, else WooCommerce."""
    path = settings.catalog_file
    if path is not None and Path(path).exists():
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        raw_products = extract_woocommerce_products(payload)
    else:
        raw_products = build_client_from_settings(settings).fetch_products()
    return [normalize_product(product) for product in raw_products]


def embeddings_for(
    documents: list[ProductDocument],
    batch_size: int = 64,
    existing_embeddings_by_id: dict[int, list[float] | None] | None = None,
) -> dict[int, list[float] | None]:
    embeddings_by_id: dict[int, list[float] | None] = {
        int(product_id): vector
        for product_id, vector in (existing_embeddings_by_id or {}).items()
        if vector is not None
    }
    missing_documents = [document for document in documents if document.id not in embeddings_by_id]
    if not missing_documents or not settings.openai_api_key:
        return embeddings_by_id

    embedder = OpenAIEmbeddingClient(settings.openai_api_key, settings.embedding_model)
    for batch in chunked(missing_documents, batch_size):
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

    try:
        catalog_batch = coerce_catalog_batch(build_catalog_documents())
    except PostgresStoreError as exc:
        raise TypesenseSyncError(str(exc)) from exc

    documents = catalog_batch.documents
    embeddings_by_id = embeddings_for(documents, existing_embeddings_by_id=catalog_batch.embeddings_by_id)
    client = build_typesense_client()
    indexed = sync_collection(
        client,
        settings.typesense_collection,
        documents,
        embeddings_by_id,
        recreate=recreate,
    )
    return {"ok": True, "indexed": indexed, "embeddings": len(embeddings_by_id)}


def coerce_catalog_batch(value: CatalogDocumentBatch | list[ProductDocument]) -> CatalogDocumentBatch:
    if isinstance(value, CatalogDocumentBatch):
        return value
    return CatalogDocumentBatch(documents=value, embeddings_by_id={})
