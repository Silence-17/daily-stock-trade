# -*- coding: utf-8 -*-
"""Tests for point-in-time factor snapshots and AlphaSift replay."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date

from src.config import Config
from src.repositories.stock_selection_factor_snapshot_repo import (
    StockSelectionFactorSnapshotRepository,
)
from src.services.stock_selection_strategy_replay_service import (
    StockSelectionStrategyReplayService,
)
from src.storage import DatabaseManager


class StockSelectionStrategyReplayServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "strategy_replay.db")
        self.env_path = os.path.join(self.temp_dir.name, ".env")
        with open(self.env_path, "w", encoding="utf-8") as env_file:
            env_file.write("STOCK_LIST=600519,000001\n")
        self.original_env = {key: os.environ.get(key) for key in ("ENV_FILE", "DATABASE_PATH")}
        os.environ["ENV_FILE"] = self.env_path
        os.environ["DATABASE_PATH"] = self.db_path
        Config.reset_instance()
        DatabaseManager.reset_instance()
        self.db = DatabaseManager.get_instance()
        self.repo = StockSelectionFactorSnapshotRepository(self.db)
        self.service = StockSelectionStrategyReplayService(repository=self.repo)
        self.snapshot_date = date(2024, 1, 5)

    def tearDown(self) -> None:
        DatabaseManager.reset_instance()
        Config.reset_instance()
        for key, value in self.original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temp_dir.cleanup()

    @staticmethod
    def _complete_row(symbol: str, *, pe_ratio: float, pb_ratio: float, price: float) -> dict:
        return {
            "symbol": symbol,
            "name": f"测试{symbol}",
            "industry": "测试行业",
            "price": price,
            "change_pct": 1.0,
            "amount": 500_000_000,
            "volume": 10_000_000,
            "turnover_rate": 2.5,
            "volume_ratio": 1.3,
            "pe_ratio": pe_ratio,
            "pb_ratio": pb_ratio,
            "total_mv": 20_000_000_000,
            "factors": {
                "change_60d": 6.0,
                "signal_score": 70.0,
                "rsi14": 55.0,
                "volatility_20d_pct": 18.0,
                "max_drawdown_20d_pct": -6.0,
                "atr_20_pct": 2.0,
            },
            "source": {"daily": "unit_test", "valuation": "unit_test"},
            "quality_status": "complete",
        }

    def test_repository_upsert_is_idempotent_and_preserves_factor_metadata(self) -> None:
        first = self._complete_row("600519", pe_ratio=12, pb_ratio=1.5, price=100)
        result = self.repo.upsert_many(market="cn", snapshot_date=self.snapshot_date, rows=[first])
        updated = {
            **first,
            "price": 101,
            "factors": {**first["factors"], "rsi14": 56, "symbol": "must-not-override"},
        }
        second_result = self.repo.upsert_many(
            market="cn",
            snapshot_date=self.snapshot_date,
            rows=[updated],
        )

        rows = self.repo.list_for_date(market="cn", snapshot_date=self.snapshot_date)
        self.assertEqual(result, {"inserted": 1, "updated": 0, "total": 1})
        self.assertEqual(second_result, {"inserted": 0, "updated": 1, "total": 1})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["price"], 101)
        self.assertEqual(rows[0]["symbol"], "600519")
        self.assertEqual(rows[0]["rsi14"], 56)
        self.assertEqual(rows[0]["source"]["valuation"], "unit_test")

    def test_compatibility_audits_missing_fields_and_replay_fails_closed(self) -> None:
        self.repo.upsert_many(
            market="cn",
            snapshot_date=self.snapshot_date,
            rows=[{"symbol": "600519", "name": "贵州茅台", "price": 100}],
        )

        compatibility = self.service.compatibility(
            strategy="dual_low",
            market="cn",
            snapshot_date=self.snapshot_date,
        )

        self.assertEqual(compatibility["universe_count"], 1)
        self.assertEqual(compatibility["hard_coverage_ratio"], 0.0)
        self.assertIn("pe_ratio", compatibility["hard_missing_counts"])
        self.assertIn("rsi14", compatibility["score_missing_counts"])
        with self.assertRaisesRegex(ValueError, "hard-filter field coverage"):
            self.service.replay(
                strategy="dual_low",
                market="cn",
                snapshot_date=self.snapshot_date,
            )

    def test_replay_uses_only_requested_snapshot_and_returns_ranked_candidates(self) -> None:
        rows = [
            self._complete_row("600519", pe_ratio=9, pb_ratio=1.0, price=50),
            self._complete_row("000001", pe_ratio=14, pb_ratio=1.8, price=12),
            self._complete_row("300001", pe_ratio=30, pb_ratio=3.0, price=25),
        ]
        self.repo.upsert_many(market="cn", snapshot_date=self.snapshot_date, rows=rows)
        self.repo.upsert_many(
            market="cn",
            snapshot_date=date(2024, 1, 8),
            rows=[self._complete_row("999999", pe_ratio=1, pb_ratio=0.1, price=5)],
        )

        result = self.service.replay(
            strategy="dual_low",
            market="cn",
            snapshot_date=self.snapshot_date,
            max_results=2,
        )

        self.assertEqual(result["universe_count"], 3)
        self.assertEqual(result["filtered_count"], 2)
        self.assertEqual(result["candidate_count"], 2)
        self.assertNotIn("999999", {item["symbol"] for item in result["candidates"]})
        self.assertGreaterEqual(
            result["candidates"][0]["screen_score"],
            result["candidates"][1]["screen_score"],
        )
        self.assertTrue(result["methodology"]["point_in_time"])
        self.assertFalse(result["methodology"]["uses_current_snapshot_fallback"])

    def test_replay_rejects_unknown_strategy_empty_date_and_invalid_thresholds(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown AlphaSift strategy"):
            self.service.compatibility(
                strategy="does_not_exist",
                market="cn",
                snapshot_date=self.snapshot_date,
            )
        with self.assertRaisesRegex(ValueError, "no point-in-time factor snapshot"):
            self.service.replay(
                strategy="dual_low",
                market="cn",
                snapshot_date=self.snapshot_date,
            )
        with self.assertRaisesRegex(ValueError, "coverage thresholds"):
            self.service.replay(
                strategy="dual_low",
                market="cn",
                snapshot_date=self.snapshot_date,
                min_hard_coverage=0,
            )


if __name__ == "__main__":
    unittest.main()
