from __future__ import annotations

import unittest
from unittest import mock

from app.models import ProductDocument, ProductSpecs


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


class RunSyncTests(unittest.TestCase):
    def test_sync_indexes_catalog_without_openai(self) -> None:
        from app import typesense_sync

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

    def test_sync_requires_api_key(self) -> None:
        from app import typesense_sync

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


if __name__ == "__main__":
    unittest.main()
