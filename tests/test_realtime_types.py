# -*- coding: utf-8 -*-
"""Concurrency regression tests for realtime circuit-breaker state."""

import threading
import time
import unittest

from data_provider.realtime_types import CircuitBreaker, RealtimeSource, UnifiedRealtimeQuote


class UnifiedRealtimeQuoteMetadataTestCase(unittest.TestCase):
    def test_metadata_defaults_are_filtered_from_to_dict(self):
        quote = UnifiedRealtimeQuote(code="600519", source=RealtimeSource.AKSHARE_EM)

        data = quote.to_dict()

        self.assertEqual(data["code"], "600519")
        self.assertEqual(data["source"], "akshare_em")
        self.assertNotIn("fetched_at", data)
        self.assertNotIn("provider_timestamp", data)
        self.assertNotIn("is_stale", data)
        self.assertNotIn("stale_seconds", data)
        self.assertNotIn("fallback_from", data)

    def test_metadata_is_included_when_present(self):
        quote = UnifiedRealtimeQuote(
            code="600519",
            source=RealtimeSource.TENCENT,
            price=1688.0,
            bid_price=1687.9,
            ask_price=1688.1,
            fetched_at="2026-05-31T10:00:05+00:00",
            provider_timestamp="2026-05-31T10:00:00+00:00",
            is_stale=False,
            stale_seconds=5,
            fallback_from="efinance",
        )

        data = quote.to_dict()

        self.assertEqual(data["fetched_at"], "2026-05-31T10:00:05+00:00")
        self.assertEqual(data["provider_timestamp"], "2026-05-31T10:00:00+00:00")
        self.assertIs(data["is_stale"], False)
        self.assertEqual(data["stale_seconds"], 5)
        self.assertEqual(data["fallback_from"], "efinance")
        self.assertEqual(data["bid_price"], 1687.9)
        self.assertEqual(data["ask_price"], 1688.1)


class CircuitBreakerConcurrencyTestCase(unittest.TestCase):
    def test_persisted_half_open_state_restores_without_probe_lease(self):
        now = time.time()
        breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=60.0)
        breaker.record_failure("src", "timeout")
        with breaker._lock:
            breaker._states["src"]["state"] = CircuitBreaker.HALF_OPEN
            breaker._states["src"]["half_open_calls"] = 1

        payload = breaker.export_state(saved_at=now)
        self.assertEqual(payload["states"]["src"]["state"], CircuitBreaker.OPEN)

        restored = CircuitBreaker(failure_threshold=1, cooldown_seconds=60.0)
        count = restored.restore_state(payload, max_age_seconds=3600, now=now + 1)

        self.assertEqual(count, 1)
        snapshot = restored.get_snapshot()["src"]
        self.assertEqual(snapshot["state"], CircuitBreaker.OPEN)
        self.assertEqual(snapshot["half_open_calls"], 0)
        self.assertFalse(restored.is_available("src"))

    def test_expired_restored_open_state_allows_one_probe(self):
        now = time.time()
        payload = {
            "version": CircuitBreaker.STATE_SCHEMA_VERSION,
            "saved_at": now,
            "states": {
                "src": {
                    "state": CircuitBreaker.OPEN,
                    "failures": 3,
                    "last_failure_time": now - 120,
                    "last_error": "timeout",
                }
            },
        }
        restored = CircuitBreaker(
            failure_threshold=3,
            cooldown_seconds=60.0,
            half_open_max_calls=1,
        )
        restored.restore_state(payload, max_age_seconds=3600, now=now)

        self.assertTrue(restored.is_available("src"))
        self.assertFalse(restored.is_available("src"))

    def test_stale_persisted_state_is_rejected(self):
        now = time.time()
        breaker = CircuitBreaker()
        payload = {
            "version": CircuitBreaker.STATE_SCHEMA_VERSION,
            "saved_at": now - 7200,
            "states": {},
        }

        with self.assertRaisesRegex(ValueError, "stale"):
            breaker.restore_state(payload, max_age_seconds=3600, now=now)

    def test_half_open_allows_only_one_concurrent_probe(self):
        breaker = CircuitBreaker(
            failure_threshold=1,
            cooldown_seconds=0.01,
            half_open_max_calls=1,
        )
        breaker.record_failure("akshare_em", "boom")
        time.sleep(0.02)

        barrier = threading.Barrier(2)
        allowed = []
        errors = []

        def worker():
            try:
                barrier.wait(timeout=1)
                allowed.append(breaker.is_available("akshare_em"))
            except Exception as exc:  # pragma: no cover - thread collection
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)

        self.assertEqual(errors, [])
        self.assertCountEqual(allowed, [True, False])
        self.assertEqual(breaker.get_status()["akshare_em"], CircuitBreaker.HALF_OPEN)

    def test_concurrent_record_updates_keep_state_consistent(self):
        breaker = CircuitBreaker(
            failure_threshold=3,
            cooldown_seconds=60.0,
            half_open_max_calls=1,
        )
        barrier = threading.Barrier(4)
        errors = []

        def record_success():
            try:
                barrier.wait(timeout=1)
                for _ in range(100):
                    breaker.record_success("tushare")
            except Exception as exc:  # pragma: no cover - thread collection
                errors.append(exc)

        def record_failure():
            try:
                barrier.wait(timeout=1)
                for _ in range(100):
                    breaker.record_failure("tushare", "network")
            except Exception as exc:  # pragma: no cover - thread collection
                errors.append(exc)

        threads = [
            threading.Thread(target=record_success),
            threading.Thread(target=record_success),
            threading.Thread(target=record_failure),
            threading.Thread(target=record_failure),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)

        self.assertEqual(errors, [])
        state = breaker._states["tushare"]
        self.assertIn(state["state"], {CircuitBreaker.CLOSED, CircuitBreaker.OPEN, CircuitBreaker.HALF_OPEN})
        self.assertGreaterEqual(state["failures"], 0)
        self.assertGreaterEqual(state["half_open_calls"], 0)

    def test_half_open_not_stuck_after_inconclusive_probe(self):
        """Regression: a probe that returns None (no record_success/record_failure)
        must not permanently block the source in HALF_OPEN."""
        breaker = CircuitBreaker(
            failure_threshold=1,
            cooldown_seconds=0.01,
            half_open_max_calls=1,
        )
        # Trip the breaker
        breaker.record_failure("src", "boom")
        time.sleep(0.02)

        # Probe goes through (OPEN -> HALF_OPEN -> slot consumed)
        self.assertTrue(breaker.is_available("src"))
        self.assertEqual(breaker.get_status()["src"], CircuitBreaker.HALF_OPEN)

        # Simulate ambiguous None result via record_inconclusive
        breaker.record_inconclusive("src")
        self.assertEqual(breaker.get_status()["src"], CircuitBreaker.OPEN)

        # After cooldown, source becomes available again
        time.sleep(0.02)
        self.assertTrue(breaker.is_available("src"))

    def test_half_open_self_heals_without_callback(self):
        """If neither record_success/record_failure/record_inconclusive is called,
        the HALF_OPEN state self-heals after another cooldown period."""
        breaker = CircuitBreaker(
            failure_threshold=1,
            cooldown_seconds=0.01,
            half_open_max_calls=1,
        )
        breaker.record_failure("src", "boom")
        time.sleep(0.02)

        # First probe consumes the slot
        self.assertTrue(breaker.is_available("src"))
        # Slot exhausted, blocked
        self.assertFalse(breaker.is_available("src"))

        # After cooldown, self-healing allows another probe
        time.sleep(0.02)
        self.assertTrue(breaker.is_available("src"))

    def test_record_inconclusive_noop_in_closed(self):
        """record_inconclusive must be a no-op when the breaker is CLOSED."""
        breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=60.0)
        breaker.record_success("src")
        breaker.record_inconclusive("src")
        self.assertEqual(breaker.get_status()["src"], CircuitBreaker.CLOSED)

    def test_snapshot_exposes_cooldown_without_consuming_half_open_probe(self):
        breaker = CircuitBreaker(
            failure_threshold=1,
            cooldown_seconds=60.0,
            half_open_max_calls=1,
        )
        breaker.record_failure("src", "provider timeout")

        opened = breaker.get_snapshot()["src"]

        self.assertEqual(opened["state"], CircuitBreaker.OPEN)
        self.assertEqual(opened["failures"], 1)
        self.assertTrue(opened["disabled"])
        self.assertGreater(opened["cooldown_remaining_seconds"], 0)
        self.assertEqual(opened["half_open_calls"], 0)
        self.assertEqual(opened["last_error"], "provider timeout")

        with breaker._lock:
            breaker._states["src"]["last_failure_time"] -= breaker.cooldown_seconds + 1
        before_probe = breaker.get_snapshot()["src"]
        self.assertEqual(before_probe["state"], CircuitBreaker.OPEN)
        self.assertEqual(before_probe["half_open_calls"], 0)

        self.assertTrue(breaker.is_available("src"))
        after_probe = breaker.get_snapshot()["src"]
        self.assertEqual(after_probe["state"], CircuitBreaker.HALF_OPEN)
        self.assertEqual(after_probe["half_open_calls"], 1)

        breaker.record_success("src")
        recovered = breaker.get_snapshot()["src"]
        self.assertEqual(recovered["state"], CircuitBreaker.CLOSED)
        self.assertEqual(recovered["failures"], 0)
        self.assertIsNone(recovered["last_error"])


if __name__ == "__main__":
    unittest.main()
