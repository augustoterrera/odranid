from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.catalog.catalog_sync import run_catalog_to_postgres_sync


def raw_product(product_id: int = 101, name: str = "Piso Moneda Goma 3mm") -> dict[str, Any]:
    return {
        "id": product_id,
        "name": name,
        "slug": f"producto-{product_id}",
        "permalink": f"https://odranid.com.ar/producto/{product_id}",
        "description": "Piso de goma moneda 3 mm de espesor.",
        "is_in_stock": True,
        "stock_availability": {"text": "Hay stock"},
        "prices": {"price": "123456", "currency_code": "ARS", "currency_minor_unit": 2},
        "categories": [{"name": "Pisos de goma", "slug": "pisos-de-goma"}],
        "tags": [],
        "brands": [{"name": "Odranid"}],
        "attributes": [],
        "images": [{"src": "https://example.com/piso.jpg"}],
    }


class FakeStore:
    def __init__(self, existing: dict[int, list[float]] | None = None) -> None:
        self.existing = existing or {}
        self.rows: list[dict[str, Any]] = []
        self.requested_hashes: dict[int, str] = {}

    def existing_embeddings_by_content_hashes(self, content_hash_by_id):
        self.requested_hashes = dict(content_hash_by_id)
        return self.existing

    def upsert_products(self, rows: list[dict[str, Any]]) -> None:
        self.rows.extend(rows)


class FakeEmbedder:
    def __init__(self, vector: list[float] | None = None) -> None:
        self.vector = vector or [0.1, 0.2]
        self.calls: list[list[str]] = []

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        return [self.vector for _text in texts]


class CatalogSyncTests(unittest.TestCase):
    def test_sync_upserts_normalized_product_with_generated_embedding(self) -> None:
        settings = SimpleNamespace(openai_api_key="sk-test", embedding_model="fake-model")
        store = FakeStore()
        embedder = FakeEmbedder([0.3, 0.4])

        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_catalog_to_postgres_sync(
                raw_products=[raw_product()],
                settings_obj=settings,
                store=store,
                embedder=embedder,
                embedding_cache=Path(tmpdir) / "embeddings.json",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["rows_upserted"], 1)
        self.assertEqual(result["embeddings_generated"], 1)
        self.assertEqual(embedder.calls, [[store.rows[0]["content"]]])
        self.assertEqual(store.rows[0]["embedding"], [0.3, 0.4])
        self.assertIn("content_hash", store.rows[0])

    def test_sync_reuses_local_embedding_cache_for_unchanged_product(self) -> None:
        settings = SimpleNamespace(openai_api_key="sk-test", embedding_model="fake-model")

        with tempfile.TemporaryDirectory() as tmpdir:
            cache_path = Path(tmpdir) / "embeddings.json"
            first_store = FakeStore()
            first_embedder = FakeEmbedder([0.5, 0.6])
            run_catalog_to_postgres_sync(
                raw_products=[raw_product()],
                settings_obj=settings,
                store=first_store,
                embedder=first_embedder,
                embedding_cache=cache_path,
            )

            second_store = FakeStore()
            second_embedder = FakeEmbedder([9.9])
            result = run_catalog_to_postgres_sync(
                raw_products=[raw_product()],
                settings_obj=settings,
                store=second_store,
                embedder=second_embedder,
                embedding_cache=cache_path,
            )

        self.assertEqual(result["embeddings_cache_hit"], 1)
        self.assertEqual(result["embeddings_generated"], 0)
        self.assertEqual(second_embedder.calls, [])
        self.assertEqual(second_store.rows[0]["embedding"], [0.5, 0.6])

    def test_sync_reuses_postgres_embedding_when_hash_matches(self) -> None:
        settings = SimpleNamespace(openai_api_key="sk-test", embedding_model="fake-model")
        store = FakeStore(existing={101: [0.7, 0.8]})
        embedder = FakeEmbedder([9.9])

        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_catalog_to_postgres_sync(
                raw_products=[raw_product()],
                settings_obj=settings,
                store=store,
                embedder=embedder,
                embedding_cache=Path(tmpdir) / "embeddings.json",
            )

        self.assertEqual(result["embeddings_db_hit"], 1)
        self.assertEqual(result["embeddings_generated"], 0)
        self.assertEqual(embedder.calls, [])
        self.assertEqual(store.rows[0]["embedding"], [0.7, 0.8])


if __name__ == "__main__":
    unittest.main()
