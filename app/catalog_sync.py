from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import settings
from .embeddings import OpenAIEmbeddingClient, chunked
from .models import ProductDocument
from .normalization import extract_woocommerce_products, normalize_product
from .postgres_store import build_postgres_store_from_settings
from .woocommerce import build_client_from_settings


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EMBEDDING_CACHE = PROJECT_ROOT / ".cache" / "embeddings.json"


class CatalogSyncError(RuntimeError):
    pass


@dataclass(frozen=True)
class CatalogSyncResult:
    ok: bool
    products_source: int
    products_normalized: int
    rows_upserted: int
    embeddings_cache_hit: int
    embeddings_db_hit: int
    embeddings_generated: int
    dry_run: bool = False
    embeddings_skipped: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "products_source": self.products_source,
            "products_normalized": self.products_normalized,
            "rows_upserted": self.rows_upserted,
            "embeddings_cache_hit": self.embeddings_cache_hit,
            "embeddings_db_hit": self.embeddings_db_hit,
            "embeddings_generated": self.embeddings_generated,
            "dry_run": self.dry_run,
            "embeddings_skipped": self.embeddings_skipped,
        }


def run_catalog_to_postgres_sync(
    *,
    from_file: Path | None = None,
    no_embeddings: bool = False,
    dry_run: bool = False,
    batch_size: int = 50,
    embedding_batch_size: int = 64,
    embedding_cache: Path | None = None,
    settings_obj: Any = settings,
    store: Any | None = None,
    raw_products: list[dict[str, Any]] | None = None,
    embedder: Any | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, object]:
    """Sync WooCommerce (or a snapshot) into Postgres.

    Embeddings are reused first from the local cache keyed by model + content
    hash, then from Postgres when the stored content_hash still matches.
    """
    if batch_size <= 0:
        raise CatalogSyncError("batch_size must be greater than zero")
    if embedding_batch_size <= 0:
        raise CatalogSyncError("embedding_batch_size must be greater than zero")

    if raw_products is None:
        raw_products = load_products(from_file, settings_obj)
    _emit(progress, f"productos_fuente={len(raw_products)}")

    documents = [normalize_product(product) for product in raw_products]
    _emit(progress, f"productos_normalizados={len(documents)}")

    active_store = store
    if active_store is None and not dry_run:
        active_store = build_postgres_store_from_settings(settings_obj)

    cache_path = embedding_cache or DEFAULT_EMBEDDING_CACHE
    content_hash_by_id = {document.id: content_hash(document.content) for document in documents}
    embeddings_by_id: dict[int, list[float] | None] = {}
    cache_hits = 0
    db_hits = 0
    generated = 0

    if no_embeddings:
        embeddings_by_id = {document.id: None for document in documents}
        _emit(progress, "embeddings=omitidos")
    else:
        if not settings_obj.openai_api_key:
            raise CatalogSyncError("OPENAI_API_KEY is required, or run with no_embeddings=True")

        cache = load_embedding_cache(cache_path)
        missing_documents: list[ProductDocument] = []
        for document in documents:
            cache_key = embedding_cache_key(settings_obj.embedding_model, content_hash_by_id[document.id])
            vector = cache.get(cache_key)
            if vector is not None:
                embeddings_by_id[document.id] = vector
                cache_hits += 1
            else:
                missing_documents.append(document)

        _emit(progress, f"embeddings_cache_hit={cache_hits}/{len(documents)}")

        db_embeddings = existing_embeddings_for_hashes(active_store, content_hash_by_id)
        if db_embeddings:
            still_missing: list[ProductDocument] = []
            for document in missing_documents:
                vector = db_embeddings.get(document.id)
                if vector is None:
                    still_missing.append(document)
                    continue
                embeddings_by_id[document.id] = vector
                cache[embedding_cache_key(settings_obj.embedding_model, content_hash_by_id[document.id])] = vector
                db_hits += 1
            missing_documents = still_missing
            save_embedding_cache(cache_path, cache)

        if db_hits:
            _emit(progress, f"embeddings_db_hit={db_hits}/{len(documents)}")

        active_embedder = embedder or OpenAIEmbeddingClient(settings_obj.openai_api_key, settings_obj.embedding_model)
        for batch in chunked(missing_documents, embedding_batch_size):
            vectors = active_embedder.embed_many([document.content for document in batch])
            for document, vector in zip(batch, vectors, strict=True):
                embeddings_by_id[document.id] = vector
                cache[embedding_cache_key(settings_obj.embedding_model, content_hash_by_id[document.id])] = vector
                generated += 1
            save_embedding_cache(cache_path, cache)
            _emit(progress, f"embeddings_generados={len(embeddings_by_id)}/{len(documents)}")

    rows = [to_catalog_row(document, embeddings_by_id.get(document.id), content_hash_by_id[document.id]) for document in documents]
    if dry_run:
        _emit(progress, f"dry_run=true rows_preparadas={len(rows)}")
        return CatalogSyncResult(
            ok=True,
            products_source=len(raw_products),
            products_normalized=len(documents),
            rows_upserted=0,
            embeddings_cache_hit=cache_hits,
            embeddings_db_hit=db_hits,
            embeddings_generated=generated,
            dry_run=True,
            embeddings_skipped=no_embeddings,
        ).as_dict()

    if active_store is None:
        active_store = build_postgres_store_from_settings(settings_obj)

    uploaded = 0
    for batch in chunked(rows, batch_size):
        active_store.upsert_products(batch)
        uploaded += len(batch)
        _emit(progress, f"upsert={uploaded}/{len(rows)}")

    _emit(progress, "sync_ok=true")
    return CatalogSyncResult(
        ok=True,
        products_source=len(raw_products),
        products_normalized=len(documents),
        rows_upserted=uploaded,
        embeddings_cache_hit=cache_hits,
        embeddings_db_hit=db_hits,
        embeddings_generated=generated,
        dry_run=False,
        embeddings_skipped=no_embeddings,
    ).as_dict()


def load_products(path: Path | None, settings_obj: Any = settings) -> list[dict[str, Any]]:
    if path is not None:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return extract_woocommerce_products(payload)

    return build_client_from_settings(settings_obj).fetch_products()


def existing_embeddings_for_hashes(store: Any | None, content_hash_by_id: Mapping[int, str]) -> dict[int, list[float]]:
    if store is None or not content_hash_by_id:
        return {}
    method = getattr(store, "existing_embeddings_by_content_hashes", None)
    if method is None:
        return {}
    return dict(method(content_hash_by_id))


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
    return {str(key): [float(value) for value in vector] for key, vector in payload.items() if isinstance(vector, list)}


def save_embedding_cache(path: Path, cache: dict[str, list[float]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache), encoding="utf-8")


def _emit(progress: Callable[[str], None] | None, message: str) -> None:
    if progress is not None:
        progress(message)
