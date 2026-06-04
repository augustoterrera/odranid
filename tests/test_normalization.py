from __future__ import annotations

import unittest

from app.normalization import floor_taxonomy


def _product(name: str, description: str = "") -> dict:
    return {"name": name, "slug": name.lower().replace(" ", "-"), "description": description}


class FloorTaxonomyTests(unittest.TestCase):
    """Safety net for the FLOOR_DESIGN_RULES extraction into domain_synonyms.

    Order matters: 'semilla melon' must win over 'semilla', and 'simil madera'
    over the bare 'madera' fallback.
    """

    def test_moneda(self) -> None:
        self.assertEqual(floor_taxonomy(_product("Piso moneda"), {}), ("diseno", "moneda"))

    def test_semilla(self) -> None:
        self.assertEqual(floor_taxonomy(_product("Piso semilla"), {}), ("diseno", "semilla"))

    def test_semilla_melon_wins_over_semilla(self) -> None:
        self.assertEqual(floor_taxonomy(_product("Piso semilla melon"), {}), ("diseno", "semilla_melon"))

    def test_simil_madera(self) -> None:
        self.assertEqual(floor_taxonomy(_product("Piso simil madera"), {}), ("diseno", "simil_madera"))

    def test_rayado_variants(self) -> None:
        self.assertEqual(floor_taxonomy(_product("Piso estriado"), {}), ("diseno", "rayado"))

    def test_vinilico(self) -> None:
        self.assertEqual(floor_taxonomy(_product("Piso vinilico"), {}), ("diseno", "vinilico"))

    def test_liso_default(self) -> None:
        self.assertEqual(floor_taxonomy(_product("Piso liso negro"), {}), ("liso", None))


if __name__ == "__main__":
    unittest.main()
