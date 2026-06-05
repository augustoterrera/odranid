from __future__ import annotations

import unittest

from app.catalog.catalog_context import TTLStringCache


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class TTLStringCacheTests(unittest.TestCase):
    def test_caches_within_ttl_and_loads_once(self) -> None:
        clock = FakeClock()
        cache = TTLStringCache(ttl_seconds=300, clock=clock)
        calls = {"n": 0}

        def loader() -> str:
            calls["n"] += 1
            return f"ctx-{calls['n']}"

        self.assertEqual(cache.get(loader), "ctx-1")
        clock.now = 299  # dentro del TTL
        self.assertEqual(cache.get(loader), "ctx-1")
        self.assertEqual(calls["n"], 1)  # el loader corrió una sola vez

    def test_reloads_after_ttl_expires(self) -> None:
        clock = FakeClock()
        cache = TTLStringCache(ttl_seconds=300, clock=clock)
        calls = {"n": 0}

        def loader() -> str:
            calls["n"] += 1
            return f"ctx-{calls['n']}"

        self.assertEqual(cache.get(loader), "ctx-1")
        clock.now = 301  # pasó el TTL
        self.assertEqual(cache.get(loader), "ctx-2")

    def test_invalidate_forces_reload(self) -> None:
        cache = TTLStringCache(ttl_seconds=300, clock=FakeClock())
        calls = {"n": 0}

        def loader() -> str:
            calls["n"] += 1
            return f"ctx-{calls['n']}"

        self.assertEqual(cache.get(loader), "ctx-1")
        cache.invalidate()
        self.assertEqual(cache.get(loader), "ctx-2")

    def test_loader_error_is_not_cached(self) -> None:
        cache = TTLStringCache(ttl_seconds=300, clock=FakeClock())

        def failing() -> str:
            raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            cache.get(failing)
        # Tras el error no quedó nada cacheado: un loader bueno corre normalmente.
        self.assertEqual(cache.get(lambda: "ok"), "ok")


if __name__ == "__main__":
    unittest.main()
