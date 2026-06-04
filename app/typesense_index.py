"""Typesense collection schema, document mapping and sync helpers.

Postgres (``catalog_products``) stays the system of record; Typesense is the
search index. This module knows how to:

- describe the ``catalog_products`` collection (fields + vector);
- turn a normalized :class:`ProductDocument` into a Typesense document;
- build the collection's synonym sets from the single domain vocabulary;
- (re)create the collection and push documents/synonyms.

The pure functions (``typesense_document``, ``synonym_payloads``,
``collection_schema``) carry the logic and are unit-tested without a live
server; the client-driven functions are thin wrappers over the Typesense SDK.
"""
from __future__ import annotations

from typing import Any

from .domain_synonyms import SYNONYM_GROUPS
from .models import ProductDocument

# Matches text-embedding-3-small and the pgvector vector(1536) column.
EMBEDDING_DIM = 1536

# Fields the hybrid query searches over.
QUERY_BY_FIELDS = ["title", "content", "technical_tags"]


def collection_schema(name: str) -> dict[str, Any]:
    """Schema for the catalog collection. rubro/in_stock are facets used as the
    only hard filters; the rest are optional attributes that feed ranking."""
    return {
        "name": name,
        "fields": [
            {"name": "title", "type": "string"},
            {"name": "content", "type": "string"},
            {"name": "rubro", "type": "string", "facet": True},
            {"name": "in_stock", "type": "bool", "facet": True},
            {"name": "category", "type": "string", "optional": True, "facet": True},
            {"name": "floor_kind", "type": "string", "optional": True, "facet": True},
            {"name": "floor_design", "type": "string", "optional": True, "facet": True},
            {"name": "material", "type": "string", "optional": True},
            {"name": "color", "type": "string", "optional": True},
            {"name": "product_type", "type": "string", "optional": True},
            {"name": "technical_tags", "type": "string[]", "optional": True, "facet": True},
            {"name": "espesor_mm", "type": "float", "optional": True, "facet": True},
            {"name": "ancho_m", "type": "float", "optional": True, "facet": True},
            {"name": "price", "type": "float", "optional": True},
            {"name": "link", "type": "string", "optional": True, "index": False},
            {"name": "embedding", "type": "float[]", "num_dim": EMBEDDING_DIM, "optional": True},
        ],
    }


def typesense_document(document: ProductDocument, embedding: list[float] | None = None) -> dict[str, Any]:
    """Map a normalized product to a Typesense document. Omits null optional
    fields so Typesense does not reject them."""
    specs = document.specs
    doc: dict[str, Any] = {
        "id": str(document.id),
        "title": document.title or "",
        "content": document.content or "",
        "rubro": document.rubro or "general",
        "in_stock": bool(document.in_stock),
        "category": document.category,
        "floor_kind": document.floor_kind,
        "floor_design": document.floor_design,
        "material": document.material,
        "color": document.color,
        "product_type": document.product_type,
        "technical_tags": document.technical_tags or [],
        "espesor_mm": specs.espesor_mm,
        "ancho_m": specs.ancho_m,
        "price": document.price,
        "link": document.link,
    }
    if embedding is not None:
        doc["embedding"] = embedding
    return {key: value for key, value in doc.items() if value is not None}


def synonym_payloads() -> list[dict[str, Any]]:
    """Multi-way synonym sets for the collection, from the domain vocabulary."""
    payloads = []
    for index, group in enumerate(SYNONYM_GROUPS):
        if len(group) < 2:
            continue
        payloads.append({"id": f"domain-{index}", "synonyms": list(group)})
    return payloads


# ---------------------------------------------------------------------------
# Client-driven helpers (thin wrappers over the Typesense SDK)
# ---------------------------------------------------------------------------


def ensure_collection(client: Any, name: str, *, recreate: bool = False) -> None:
    """Create the collection if missing. With recreate=True drop it first."""
    if recreate:
        try:
            client.collections[name].delete()
        except Exception:
            pass
    try:
        client.collections[name].retrieve()
        return
    except Exception:
        client.collections.create(collection_schema(name))


def upsert_synonyms(client: Any, name: str) -> None:
    for payload in synonym_payloads():
        client.collections[name].synonyms.upsert(payload["id"], {"synonyms": payload["synonyms"]})


def index_documents(
    client: Any,
    name: str,
    documents: list[ProductDocument],
    embeddings_by_id: dict[int, list[float] | None] | None = None,
) -> int:
    """Upsert documents into the collection. Returns how many were sent."""
    embeddings_by_id = embeddings_by_id or {}
    payload = [typesense_document(doc, embeddings_by_id.get(doc.id)) for doc in documents]
    if not payload:
        return 0
    client.collections[name].documents.import_(payload, {"action": "upsert"})
    return len(payload)


def sync_collection(
    client: Any,
    name: str,
    documents: list[ProductDocument],
    embeddings_by_id: dict[int, list[float] | None] | None = None,
    *,
    recreate: bool = False,
) -> int:
    """Ensure the collection + synonyms exist and (re)index the documents."""
    ensure_collection(client, name, recreate=recreate)
    upsert_synonyms(client, name)
    return index_documents(client, name, documents, embeddings_by_id)
