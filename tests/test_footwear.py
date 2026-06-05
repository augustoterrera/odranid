from __future__ import annotations

import unittest

from app.catalog.footwear import extract_requested_talle, parse_talle_range, talle_excluded


class FootwearTests(unittest.TestCase):
    def test_extract_requested_talle_needs_keyword(self) -> None:
        self.assertEqual(extract_requested_talle("Para lluvia talla 42"), 42)
        self.assertEqual(extract_requested_talle("necesito talle 38"), 38)
        self.assertEqual(extract_requested_talle("n° 40"), 40)
        # Sin la palabra talle/talla no asume un número suelto.
        self.assertIsNone(extract_requested_talle("quiero botas para lluvia"))
        # No confunde medidas de piso con talles.
        self.assertIsNone(extract_requested_talle("piso 3mm ancho 1.20"))

    def test_parse_talle_range(self) -> None:
        self.assertEqual(parse_talle_range("Bota Para Niños Del 21 Al 34"), (21, 34))
        self.assertEqual(parse_talle_range("Bota Lluvia 34/45"), (34, 45))
        self.assertIsNone(parse_talle_range("Bota Negra Caña Alta Industrial"))
        # No interpreta medidas con 'x' como rango de talles.
        self.assertIsNone(parse_talle_range("Piso 1,20x15mts rollo"))

    def test_talle_excluded(self) -> None:
        # 42 no entra en una bota de niños 21-34.
        self.assertTrue(talle_excluded("Bota Para Niños Del 21 Al 34", 42))
        # 30 sí entra.
        self.assertFalse(talle_excluded("Bota Para Niños Del 21 Al 34", 30))
        # Sin rango declarado no se puede descartar.
        self.assertFalse(talle_excluded("Bota Negra Caña Alta Industrial", 42))


if __name__ == "__main__":
    unittest.main()
