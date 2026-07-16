# -*- coding: utf-8 -*-
"""Restart recovery tests for manager-level realtime provider health."""

import json
import tempfile
import unittest
from pathlib import Path

from data_provider.base import DataFetcherManager
from data_provider.realtime_types import CircuitBreaker


class RealtimeSourceHealthPersistenceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._original_breaker = DataFetcherManager._realtime_source_health
        DataFetcherManager.disable_realtime_source_health_persistence()
        DataFetcherManager._realtime_source_health = CircuitBreaker(
            failure_threshold=3,
            cooldown_seconds=300.0,
            half_open_max_calls=1,
        )

    def tearDown(self) -> None:
        DataFetcherManager.disable_realtime_source_health_persistence()
        DataFetcherManager._realtime_source_health = self._original_breaker

    def test_open_state_survives_reconstruction_and_success_clears_disk_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = str(Path(temp_dir) / "stock_analysis.db")
            DataFetcherManager.configure_realtime_source_health_persistence(database_path)
            for _ in range(3):
                DataFetcherManager._record_realtime_source_failure(
                    "efinance",
                    "cn",
                    "provider timeout",
                )

            state_path = Path(temp_dir) / "realtime_source_health.json"
            self.assertTrue(state_path.is_file())
            DataFetcherManager.disable_realtime_source_health_persistence()
            DataFetcherManager._realtime_source_health = CircuitBreaker(
                failure_threshold=3,
                cooldown_seconds=300.0,
                half_open_max_calls=1,
            )

            recovery = DataFetcherManager.configure_realtime_source_health_persistence(
                database_path
            )

            self.assertEqual(recovery["restored_sources"], 1)
            restored = DataFetcherManager.realtime_source_health_snapshot()["cn/efinance"]
            self.assertEqual(restored["state"], CircuitBreaker.OPEN)
            self.assertTrue(restored["disabled"])

            DataFetcherManager._record_realtime_source_success("efinance", "cn")
            persisted = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["states"], {})

    def test_corrupt_state_is_ignored_and_provider_remains_available(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "realtime_source_health.json"
            state_path.write_text("{not-json", encoding="utf-8")

            recovery = DataFetcherManager.configure_realtime_source_health_persistence(
                str(Path(temp_dir) / "stock_analysis.db")
            )

            self.assertTrue(recovery["ignored_invalid_state"])
            self.assertEqual(DataFetcherManager.realtime_source_health_snapshot(), {})
            self.assertTrue(
                DataFetcherManager._is_realtime_source_available("efinance", "cn")
            )

    def test_healthy_success_does_not_create_state_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = str(Path(temp_dir) / "stock_analysis.db")
            DataFetcherManager.configure_realtime_source_health_persistence(database_path)

            DataFetcherManager._record_realtime_source_success("efinance", "cn")

            self.assertFalse(
                (Path(temp_dir) / "realtime_source_health.json").exists()
            )


if __name__ == "__main__":
    unittest.main()
