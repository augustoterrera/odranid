from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.chat_memory import (
    ChatMemoryStore,
    apply_pending_slot_to_message,
    build_chat_memory_store_from_settings,
    build_memory_state,
    history_from_state,
    known_to_natural_text,
    pending_slot_from_intake,
    should_reset_conversation_state,
)
from app.models import AgentMessage
from app.models import ProductIntakeResponse


class ChatMemoryTests(unittest.TestCase):
    def test_pending_width_contextualizes_short_reply(self) -> None:
        message = apply_pending_slot_to_message("2m", {"pending_slot": "ancho_m"})

        self.assertEqual(message, "2m de ancho")

    def test_pending_surface_contextualizes_short_reply(self) -> None:
        message = apply_pending_slot_to_message("50", {"pending_slot": "requested_m2"})

        self.assertEqual(message, "50 m2")

    def test_pending_hose_length_contextualizes_short_reply(self) -> None:
        message = apply_pending_slot_to_message("15", {"pending_slot": "length_m"})

        self.assertEqual(message, "15 metros")

    def test_pending_slot_from_single_missing_slot(self) -> None:
        intake = ProductIntakeResponse(intent="pisos", missing=["ancho_m"], should_search=False)

        self.assertEqual(pending_slot_from_intake(intake), "ancho_m")

    def test_pending_slot_is_none_when_ready_to_search(self) -> None:
        intake = ProductIntakeResponse(intent="pisos", missing=[], should_search=True)

        self.assertIsNone(pending_slot_from_intake(intake))

    def test_build_memory_state_merges_known_values(self) -> None:
        state = build_memory_state(
            {"known": {"requested_m2": 50}},
            ProductIntakeResponse(
                intent="pisos",
                known={"floor_design": "moneda"},
                missing=["ancho_m"],
                should_search=False,
                next_question="¿Qué ancho buscás?",
            ),
            "ancho_m",
        )

        self.assertEqual(state["known"]["requested_m2"], 50)
        self.assertEqual(state["known"]["floor_design"], "moneda")
        self.assertEqual(state["missing"], ["espesor_mm", "ancho_m"])
        self.assertIsNone(state["pending_slot"])
        self.assertIn("espesor", state["last_question"])

    def test_history_from_state_exposes_known_context(self) -> None:
        history = history_from_state({"known": {"requested_m2": 50, "floor_design": "moneda"}})

        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].role, "user")
        self.assertIn("cubrir 50m2", history[0].content)
        self.assertIn("diseño moneda", history[0].content)

    def test_known_to_natural_text_is_parseable_for_floor_specs(self) -> None:
        text = known_to_natural_text(
            {
                "rubro": "pisos",
                "requested_m2": 50,
                "floor_design": "moneda",
                "espesor_mm": 3,
                "ancho_m": 2,
            }
        )

        self.assertIn("piso", text)
        self.assertIn("para cubrir 50m2", text)
        self.assertIn("diseño moneda", text)
        self.assertIn("espesor 3mm", text)
        self.assertIn("ancho 2m", text)

    def test_known_to_natural_text_preserves_product_reference_mode(self) -> None:
        text = known_to_natural_text(
            {
                "rubro": "pisos",
                "floor_kind": "liso",
                "espesor_mm": 3,
                "ancho_m": 1,
                "lookup_mode": "product_reference",
                "coverage_required": False,
            }
        )

        self.assertIn("vengo de la tienda online producto", text)

    def test_known_to_natural_text_supports_pet_specs(self) -> None:
        text = known_to_natural_text(
            {
                "rubro": "mascotas",
                "animal": "perro",
                "size": "grande",
                "toy_type": "pelota",
                "resistant": True,
            }
        )

        self.assertIn("juguete para mascota", text)
        self.assertIn("perro", text)
        self.assertIn("tamaño grande", text)
        self.assertIn("tipo pelota", text)
        self.assertIn("resistente", text)

    def test_build_memory_state_does_not_keep_stale_known_values(self) -> None:
        state = build_memory_state(
            {"known": {"floor_design": "semilla", "ancho_m": 1, "use": "gimnasio"}},
            ProductIntakeResponse(
                intent="pisos",
                known={"rubro": "pisos", "floor_design": "moneda", "ancho_m": 2},
                missing=[],
                should_search=True,
            ),
            None,
        )

        self.assertEqual(state["known"]["floor_design"], "moneda")
        self.assertEqual(state["known"]["ancho_m"], 2)
        self.assertNotIn("use", state["known"])

    def test_build_memory_state_recomputes_missing_after_merge(self) -> None:
        state = build_memory_state(
            {
                "known": {
                    "rubro": "pisos",
                    "requested_m2": 50,
                    "floor_kind": "diseno",
                    "ancho_m": 2,
                }
            },
            ProductIntakeResponse(
                intent="pisos",
                known={"rubro": "pisos", "espesor_mm": 3},
                missing=["ancho_m"],
                should_search=False,
                next_question="¿Qué ancho buscás?",
            ),
            "ancho_m",
        )

        self.assertEqual(state["known"]["ancho_m"], 2)
        self.assertEqual(state["known"]["espesor_mm"], 3)
        self.assertEqual(state["missing"], [])
        self.assertTrue(state["should_search"])
        self.assertIsNone(state["pending_slot"])

    def test_indifferent_design_counts_as_resolved_style(self) -> None:
        state = build_memory_state(
            {"known": {"rubro": "pisos", "requested_m2": 50}},
            ProductIntakeResponse(
                intent="pisos",
                known={"style_preference": "indiferente", "espesor_mm": 3, "ancho_m": 2},
                missing=[],
                should_search=True,
            ),
            None,
        )

        self.assertEqual(state["known"]["style_preference"], "indiferente")
        self.assertEqual(state["missing"], [])
        self.assertTrue(state["should_search"])

    def test_product_reference_does_not_require_surface(self) -> None:
        state = build_memory_state(
            {},
            ProductIntakeResponse(
                intent="pisos",
                known={
                    "rubro": "pisos",
                    "floor_design": "moneda",
                    "espesor_mm": 3,
                    "ancho_m": 1,
                    "lookup_mode": "product_reference",
                    "coverage_required": False,
                },
                missing=[],
                should_search=True,
            ),
            None,
        )

        self.assertEqual(state["missing"], [])
        self.assertTrue(state["should_search"])
        self.assertNotIn("requested_m2", state["missing"])

    def test_width_availability_lookup_drops_stale_width_filter(self) -> None:
        state = build_memory_state(
            {
                "known": {
                    "rubro": "pisos",
                    "floor_design": "simil_madera",
                    "espesor_mm": 2,
                    "ancho_m": 2,
                    "requested_m2": 7.5,
                }
            },
            ProductIntakeResponse(
                intent="pisos",
                known={
                    "rubro": "pisos",
                    "floor_design": "simil_madera",
                    "espesor_mm": 2,
                    "lookup_mode": "availability_width",
                    "coverage_required": False,
                },
                missing=[],
                should_search=True,
            ),
            None,
        )

        self.assertNotIn("ancho_m", state["known"])
        self.assertNotIn("requested_m2", state["known"])
        self.assertEqual(state["missing"], [])
        self.assertTrue(state["should_search"])

    def test_known_to_natural_text_marks_width_availability_lookup(self) -> None:
        text = known_to_natural_text(
            {
                "rubro": "pisos",
                "floor_design": "simil_madera",
                "espesor_mm": 2,
                "lookup_mode": "availability_width",
                "coverage_required": False,
            }
        )

        self.assertIn("consulta anchos disponibles", text)
        self.assertNotIn("vengo de la tienda online producto", text)

    def test_new_product_request_resets_previous_state(self) -> None:
        self.assertTrue(
            should_reset_conversation_state(
                "Busco piso para mi oficina quiero cubrir 50m2",
                {"known": {"floor_design": "semilla"}},
            )
        )

    def test_misspelled_new_product_request_resets_previous_state(self) -> None:
        self.assertTrue(
            should_reset_conversation_state(
                "Hola, nesesito 3 rollos de piso semilla negro",
                {"known": {"floor_design": "moneda"}},
            )
        )

    def test_short_slot_reply_does_not_reset_previous_state(self) -> None:
        self.assertFalse(
            should_reset_conversation_state(
                "2m",
                {"known": {"requested_m2": 50}, "pending_slot": "ancho_m"},
            )
        )

    def test_memory_store_uses_direct_postgres_database_url(self) -> None:
        store = build_chat_memory_store_from_settings(
            SimpleNamespace(database_url="postgresql://user:pass@localhost:5432/db")
        )

        self.assertIsInstance(store, ChatMemoryStore)
        assert store is not None
        self.assertEqual(store.database_url, "postgresql://user:pass@localhost:5432/db")


if __name__ == "__main__":
    unittest.main()
