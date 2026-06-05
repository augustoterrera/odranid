from __future__ import annotations

import unittest

from app.catalog.coverage import calculate_coverage, extract_requested_m2
from app.core.models import ProductDocument, ProductSpecs


class CoverageTests(unittest.TestCase):
    def test_extract_requested_m2(self) -> None:
        self.assertEqual(extract_requested_m2("cubrir 20m2"), 20)
        self.assertEqual(extract_requested_m2("necesito 2 m²"), 2)
        self.assertEqual(extract_requested_m2("son 12,5 metros cuadrados"), 12.5)

    def test_does_not_confuse_width_or_thickness_with_surface(self) -> None:
        self.assertIsNone(extract_requested_m2("ancho 2m"))
        self.assertIsNone(extract_requested_m2("piso 3mm"))
        self.assertIsNone(extract_requested_m2("ancho 1.20 metros"))

    def test_roll_coverage_uses_width_times_length(self) -> None:
        product = product_with_specs(
            product_type="rollo",
            specs=ProductSpecs(ancho_m=1.2, largo_m=10),
        )

        coverage = calculate_coverage(product, requested_m2=20)

        self.assertEqual(coverage.coverage_m2, 12)
        self.assertEqual(coverage.coverage_source, "ancho_m_x_largo_m")
        self.assertEqual(coverage.rolls_needed, 2)
        self.assertEqual(coverage.surplus_m2, 4)
        self.assertFalse(coverage.needs_advisor)

    def test_rendimiento_m2_is_preferred(self) -> None:
        product = product_with_specs(
            product_type="rollo",
            specs=ProductSpecs(ancho_m=2, largo_m=10, rendimiento_m2=15),
        )

        coverage = calculate_coverage(product, requested_m2=20)

        self.assertEqual(coverage.coverage_m2, 15)
        self.assertEqual(coverage.coverage_source, "rendimiento_m2")
        self.assertEqual(coverage.rolls_needed, 2)

    def test_product_sold_by_m2_returns_quantity_m2(self) -> None:
        product = product_with_specs(product_type="m2", specs=ProductSpecs())

        coverage = calculate_coverage(product, requested_m2=18.5)

        self.assertEqual(coverage.quantity_m2, 18.5)
        self.assertIsNone(coverage.rolls_needed)
        self.assertFalse(coverage.needs_advisor)

    def test_cut_coverage_message_uses_cut_label(self) -> None:
        product = product_with_specs(
            product_type="corte",
            specs=ProductSpecs(rendimiento_m2=1),
        )

        coverage = calculate_coverage(product, requested_m2=3)

        self.assertEqual(coverage.rolls_needed, 3)
        self.assertIn("cada corte", coverage.message)
        self.assertIn("3 cortes", coverage.message)

    def test_only_width_is_cut_to_measure(self) -> None:
        product = product_with_specs(
            product_type="unidad",
            specs=ProductSpecs(ancho_m=1.4),
        )

        coverage = calculate_coverage(product, requested_m2=20)

        self.assertIsNone(coverage.rolls_needed)
        self.assertIsNone(coverage.linear_meters_needed)
        self.assertEqual(coverage.coverage_source, "corte_a_medida")
        self.assertIn("cortado a medida", coverage.message)
        self.assertNotIn("lineal", coverage.message)
        self.assertFalse(coverage.needs_advisor)

    def test_meter_lineal_title_is_cut_to_measure(self) -> None:
        product = product_with_specs(
            product_type="rollo",
            specs=ProductSpecs(ancho_m=1.4, largo_m=1),
            title="Piso De Goma Liso Negro 3mm Ancho 1.40mt x Metro Lineal",
        )

        coverage = calculate_coverage(product, requested_m2=36)

        self.assertEqual(coverage.sale_unit, "metro_lineal")
        self.assertIsNone(coverage.rolls_needed)
        self.assertNotIn("metros lineales", coverage.message)
        self.assertIn("cortado a medida", coverage.message)

    def test_real_roll_with_linear_text_still_computes_rolls(self) -> None:
        # "Por Rollo (1.40m x 10m)" con texto "metro" en la descripcion: sigue siendo rollo.
        product = product_with_specs(
            product_type="rollo",
            specs=ProductSpecs(ancho_m=1.4, largo_m=10, rendimiento_m2=14),
            title="Pisos De Goma Negro Liso 2mm Por Rollo 1.40m x 10m por metro lineal",
        )

        coverage = calculate_coverage(product, requested_m2=20)

        self.assertEqual(coverage.sale_unit, "rollo")
        self.assertEqual(coverage.rolls_needed, 2)
        self.assertIn("rollo", coverage.message)
        self.assertNotIn("lineal", coverage.message)

    def test_metro_cuadrado_title_does_not_become_meter_lineal(self) -> None:
        product = product_with_specs(
            product_type="rollo",
            specs=ProductSpecs(ancho_m=1, largo_m=1),
            title="Piso de goma por metro cuadrado",
        )

        coverage = calculate_coverage(product, requested_m2=3)

        # No es metro_lineal (el titulo dice "cuadrado"); y sin largo real de rollo
        # no inventa una cantidad absurda: se vende cortado a medida.
        self.assertEqual(coverage.sale_unit, "rollo")
        self.assertNotIn("lineal", coverage.message)
        self.assertIn("cortado a medida", coverage.message)

    def test_fake_roll_without_real_length_is_cut_to_measure(self) -> None:
        # "X 1mt Ancho" tipado rollo pero sin largo real (largo=1, rend=1):
        # NO debe decir "15 rollos", sino cortado a medida.
        product = product_with_specs(
            product_type="rollo",
            specs=ProductSpecs(ancho_m=1, largo_m=1, rendimiento_m2=1),
            title="Piso Semilla 3mm X 1mt Ancho",
        )

        coverage = calculate_coverage(product, requested_m2=15)

        self.assertIsNone(coverage.rolls_needed)
        self.assertIn("cortado a medida", coverage.message)

    def test_missing_measures_needs_advisor(self) -> None:
        product = product_with_specs(product_type="unidad", specs=ProductSpecs())

        coverage = calculate_coverage(product, requested_m2=20)

        self.assertTrue(coverage.needs_advisor)
        self.assertIn("asesor", coverage.message)


def product_with_specs(product_type: str, specs: ProductSpecs, title: str = "Producto de prueba") -> ProductDocument:
    return ProductDocument(
        id=1,
        title=title,
        product_type=product_type,
        rubro="pisos",
        specs=specs,
        content="",
    )


if __name__ == "__main__":
    unittest.main()
