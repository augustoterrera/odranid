from __future__ import annotations

import unittest
from unittest import mock

from app.core.models import ProductDocument, ProductSpecs


def _doc(pid: int = 1) -> ProductDocument:
    return ProductDocument(
        id=pid, title=f"Piso moneda {pid}", rubro="pisos", floor_design="moneda",
        specs=ProductSpecs(espesor_mm=3), content="goma moneda",
    )


class FakeCollection:
    def __init__(self) -> None:
        self.imported: list[dict] = []
        self.created = False

    def retrieve(self):
        if not self.created:
            raise RuntimeError("missing")
        return {}

    @property
    def documents(self):
        return self

    def import_(self, payload, options):
        self.imported.extend(payload)
        return [{"success": True} for _ in payload]

    @property
    def synonyms(self):
        return self

    def upsert(self, *_args):
        return None


class FakeCollections:
    def __init__(self) -> None:
        self.col = FakeCollection()

    def __getitem__(self, _name):
        return self.col

    def create(self, _schema):
        self.col.created = True


class FakeClient:
    def __init__(self) -> None:
        self.collections = FakeCollections()


class FakePostgresStore:
    def __init__(self, rows):
        self.rows = rows

    def list_products(self):
        return self.rows


def postgres_row(pid: int = 1, embedding=None) -> dict:
    return {
        "id": pid,
        "title": f"Piso moneda {pid}",
        "slug": f"piso-moneda-{pid}",
        "link": f"https://odranid.com.ar/producto/{pid}",
        "image": None,
        "price": 123.0,
        "currency": "ARS",
        "in_stock": True,
        "stock_text": "Hay stock",
        "rubro": "pisos",
        "category": "pisos_de_goma",
        "subcategory": None,
        "product_type": "rollo",
        "floor_kind": "diseno",
        "floor_design": "moneda",
        "material": "goma",
        "color": "negro",
        "environments": None,
        "brands": ["Odranid"],
        "categories": ["Pisos de goma"],
        "woo_tags": [],
        "technical_tags": ["goma", "diseno_moneda"],
        "espesor_mm": 3,
        "ancho_m": 1.2,
        "largo_m": None,
        "rendimiento_m2": None,
        "diametro_mm": None,
        "largo_manguera_m": None,
        "content": "goma moneda",
        "metadata": {},
        "raw_attributes": {},
        "embedding": embedding,
        "content_hash": "hash",
    }


class RunSyncTests(unittest.TestCase):
    def test_sync_indexes_catalog_without_openai(self) -> None:
        from app.catalog import typesense_sync

        client = FakeClient()
        with mock.patch.object(typesense_sync.settings, "typesense_api_key", "k"), \
             mock.patch.object(typesense_sync.settings, "openai_api_key", None), \
             mock.patch.object(typesense_sync, "build_catalog_documents", return_value=[_doc(1), _doc(2)]), \
             mock.patch.object(typesense_sync, "build_typesense_client", return_value=client):
            result = typesense_sync.run_typesense_sync(recreate=False)

        self.assertTrue(result["ok"])
        self.assertEqual(result["indexed"], 2)
        self.assertEqual(result["embeddings"], 0)
        self.assertEqual(len(client.collections.col.imported), 2)

    def test_build_catalog_documents_reads_postgres_store(self) -> None:
        from app.catalog import typesense_sync

        store = FakePostgresStore([postgres_row(1, embedding=[0.1, 0.2])])
        with mock.patch.object(typesense_sync.settings, "database_url", "postgresql://db"):
            batch = typesense_sync.build_catalog_documents(store=store)

        self.assertEqual(len(batch.documents), 1)
        self.assertEqual(batch.documents[0].id, 1)
        self.assertEqual(batch.documents[0].floor_design, "moneda")
        self.assertEqual(batch.embeddings_by_id, {1: [0.1, 0.2]})

    def test_sync_uses_postgres_embeddings_without_openai_call(self) -> None:
        from app.catalog import typesense_sync

        client = FakeClient()
        store = FakePostgresStore([postgres_row(1, embedding=[0.1, 0.2])])
        with mock.patch.object(typesense_sync.settings, "typesense_api_key", "k"), \
             mock.patch.object(typesense_sync.settings, "database_url", "postgresql://db"), \
             mock.patch.object(typesense_sync.settings, "openai_api_key", "sk-test"), \
             mock.patch.object(typesense_sync, "build_postgres_store_from_settings", return_value=store), \
             mock.patch.object(typesense_sync, "build_typesense_client", return_value=client), \
             mock.patch.object(typesense_sync, "OpenAIEmbeddingClient") as embedder_cls:
            result = typesense_sync.run_typesense_sync(recreate=False)

        self.assertTrue(result["ok"])
        self.assertEqual(result["indexed"], 1)
        self.assertEqual(result["embeddings"], 1)
        self.assertEqual(client.collections.col.imported[0]["embedding"], [0.1, 0.2])
        embedder_cls.assert_not_called()

    def test_embeddings_for_only_embeds_missing_vectors(self) -> None:
        from app.catalog import typesense_sync

        embedder = mock.Mock()
        embedder.embed_many.return_value = [[0.9]]
        with mock.patch.object(typesense_sync.settings, "openai_api_key", "sk-test"), \
             mock.patch.object(typesense_sync, "OpenAIEmbeddingClient", return_value=embedder):
            result = typesense_sync.embeddings_for([_doc(1), _doc(2)], existing_embeddings_by_id={1: [0.1]})

        self.assertEqual(result, {1: [0.1], 2: [0.9]})
        embedder.embed_many.assert_called_once_with(["goma moneda"])

    def test_sync_requires_api_key(self) -> None:
        from app.catalog import typesense_sync

        with mock.patch.object(typesense_sync.settings, "typesense_api_key", None):
            with self.assertRaises(typesense_sync.TypesenseSyncError):
                typesense_sync.run_typesense_sync()


class CatalogTaskTests(unittest.TestCase):
    def test_task_skips_without_api_key(self) -> None:
        from app.tasks import catalog_tasks

        with mock.patch.object(catalog_tasks.settings, "typesense_api_key", None):
            result = catalog_tasks.sync_typesense_catalog.run()
        self.assertFalse(result["ok"])
        self.assertIn("skipped", result)

    def test_catalog_task_skips_without_required_settings(self) -> None:
        from app.tasks import catalog_tasks

        with mock.patch.object(catalog_tasks.settings, "openai_api_key", None), \
             mock.patch.object(catalog_tasks.settings, "database_url", "postgresql://db"):
            result = catalog_tasks.sync_catalog_to_postgres.run()
        self.assertEqual(result["skipped"], "no_openai_api_key")

        with mock.patch.object(catalog_tasks.settings, "openai_api_key", "sk-test"), \
             mock.patch.object(catalog_tasks.settings, "database_url", None):
            result = catalog_tasks.sync_catalog_to_postgres.run()
        self.assertEqual(result["skipped"], "no_database_url")

    def test_catalog_task_runs_sync(self) -> None:
        from app.tasks import catalog_tasks

        with mock.patch.object(catalog_tasks.settings, "openai_api_key", "sk-test"), \
             mock.patch.object(catalog_tasks.settings, "database_url", "postgresql://db"), \
             mock.patch.object(catalog_tasks, "run_catalog_to_postgres_sync", return_value={"ok": True}) as run_sync:
            result = catalog_tasks.sync_catalog_to_postgres.run()

        self.assertTrue(result["ok"])
        run_sync.assert_called_once_with()

    def test_catalog_task_is_scheduled(self) -> None:
        from app.celery_app import celery_app

        self.assertIn("app.tasks.catalog_tasks.sync_catalog_to_postgres", celery_app.conf.task_routes)
        self.assertEqual(
            celery_app.conf.beat_schedule["sync-catalog-to-postgres"]["task"],
            "app.tasks.catalog_tasks.sync_catalog_to_postgres",
        )


if __name__ == "__main__":
    unittest.main()
