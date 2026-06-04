from __future__ import annotations

import unittest

from app.models import ProductDocument, ProductSpecs
from app.typesense_index import (
    EMBEDDING_DIM,
    collection_schema,
    index_documents,
    synonym_payloads,
    sync_collection,
    typesense_document,
)


def _doc(**overrides) -> ProductDocument:
    base = dict(
        id=1,
        title="Piso moneda 3mm",
        content="Piso de goma moneda negro",
        rubro="pisos",
        category="pisos_de_goma",
        floor_kind="diseno",
        floor_design="moneda",
        material="goma",
        color="negro",
        in_stock=True,
        link="https://odranid.com.ar/producto/piso-moneda/",
        technical_tags=["antideslizante"],
        specs=ProductSpecs(espesor_mm=3, ancho_m=1.0, largo_m=10, rendimiento_m2=12),
    )
    base.update(overrides)
    return ProductDocument(**base)


class TypesenseDocumentTests(unittest.TestCase):
    def test_id_is_stringified_and_fields_mapped(self) -> None:
        doc = typesense_document(_doc(), embedding=[0.1, 0.2])
        self.assertEqual(doc["id"], "1")
        self.assertEqual(doc["rubro"], "pisos")
        self.assertEqual(doc["floor_design"], "moneda")
        self.assertEqual(doc["espesor_mm"], 3)
        self.assertEqual(doc["ancho_m"], 1.0)
        # largo_m y rendimiento_m2 son necesarios para que coverage calcule rollos.
        self.assertEqual(doc["largo_m"], 10)
        self.assertEqual(doc["rendimiento_m2"], 12)
        self.assertEqual(doc["embedding"], [0.1, 0.2])

    def test_null_optional_fields_are_dropped(self) -> None:
        doc = typesense_document(_doc(color=None, floor_design=None, specs=ProductSpecs()))
        self.assertNotIn("color", doc)
        self.assertNotIn("floor_design", doc)
        self.assertNotIn("espesor_mm", doc)
        self.assertNotIn("embedding", doc)
        # Required fields always present.
        self.assertIn("rubro", doc)
        self.assertIn("in_stock", doc)

    def test_schema_declares_vector_and_facets(self) -> None:
        schema = collection_schema("catalog_products")
        fields = {f["name"]: f for f in schema["fields"]}
        self.assertEqual(fields["embedding"]["num_dim"], EMBEDDING_DIM)
        self.assertTrue(fields["rubro"]["facet"])
        self.assertTrue(fields["in_stock"]["facet"])

    def test_synonym_payloads_from_domain_groups(self) -> None:
        payloads = synonym_payloads()
        all_terms = [term for p in payloads for term in p["synonyms"]]
        self.assertIn("caucho", all_terms)
        self.assertIn("pvc", all_terms)
        # Every payload is multi-way (>= 2 terms).
        self.assertTrue(all(len(p["synonyms"]) >= 2 for p in payloads))


class FakeCollection:
    def __init__(self) -> None:
        self.imported: list[dict] = []
        self.synonyms_upserted: list[tuple] = []
        self.retrieved = False
        self.created = False

    def retrieve(self):
        if not self.created:
            raise RuntimeError("not found")
        self.retrieved = True
        return {"name": "catalog_products"}

    @property
    def documents(self):
        return self

    def import_(self, payload, options):
        self.imported.extend(payload)
        return [{"success": True} for _ in payload]

    @property
    def synonyms(self):
        return self

    def upsert(self, syn_id, payload):
        self.synonyms_upserted.append((syn_id, payload))


class FakeCollections:
    def __init__(self) -> None:
        self.col = FakeCollection()
        self.create_calls: list[dict] = []

    def __getitem__(self, name):
        return self.col

    def create(self, schema):
        self.create_calls.append(schema)
        self.col.created = True


class FakeClient:
    def __init__(self) -> None:
        self.collections = FakeCollections()


class SyncTests(unittest.TestCase):
    def test_sync_creates_collection_indexes_docs_and_synonyms(self) -> None:
        client = FakeClient()
        count = sync_collection(client, "catalog_products", [_doc(), _doc(id=2)], {1: [0.1], 2: [0.2]})
        self.assertEqual(count, 2)
        self.assertEqual(len(client.collections.create_calls), 1)
        self.assertEqual(len(client.collections.col.imported), 2)
        self.assertTrue(client.collections.col.synonyms_upserted)

    def test_index_documents_skips_when_empty(self) -> None:
        client = FakeClient()
        client.collections.col.created = True
        self.assertEqual(index_documents(client, "catalog_products", []), 0)


if __name__ == "__main__":
    unittest.main()
