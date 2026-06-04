from __future__ import annotations

import unittest

from app.domain_synonyms import (
    RUBBER_FLOOR_DESIGNS,
    compatible_designs,
    synonyms_for,
)


class DomainSynonymsTests(unittest.TestCase):
    def test_synonyms_are_bidirectional_within_a_group(self) -> None:
        self.assertIn("caucho", synonyms_for("goma"))
        self.assertIn("goma", synonyms_for("caucho"))

    def test_simil_goma_groups_pvc_and_vinilico(self) -> None:
        group = synonyms_for("pvc")
        self.assertIn("simil goma", group)
        self.assertIn("vinilico", group)

    def test_unknown_term_returns_itself(self) -> None:
        self.assertEqual(synonyms_for("inexistente"), ["inexistente"])

    def test_rubber_designs_are_the_canonical_set(self) -> None:
        self.assertEqual(RUBBER_FLOOR_DESIGNS, frozenset({"moneda", "semilla", "semilla_melon", "rayado"}))

    def test_semilla_is_compatible_with_semilla_melon(self) -> None:
        self.assertEqual(compatible_designs("semilla"), frozenset({"semilla", "semilla_melon"}))

    def test_design_without_alternatives_maps_to_itself(self) -> None:
        self.assertEqual(compatible_designs("moneda"), frozenset({"moneda"}))
        self.assertEqual(compatible_designs(None), frozenset())


if __name__ == "__main__":
    unittest.main()
