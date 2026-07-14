# -*- coding: utf-8 -*-
"""Tests for bounded historical stock-selection factor ingestion."""

from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import MagicMock

import pandas as pd

from src.services.stock_selection_factor_ingestion_service import (
    StockSelectionFactorIngestionService,
)


class StockSelectionFactorIngestionServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = MagicMock()
        self.repository.upsert_many.return_value = {"inserted": 1, "updated": 0, "total": 1}

    @staticmethod
    def _daily_frame() -> pd.DataFrame:
        dates = pd.bdate_range("2023-09-01", "2024-01-10")
        base = pd.Series(range(len(dates)), dtype=float)
        return pd.DataFrame({
            "日期": dates,
            "开盘": 10 + base * 0.1,
            "收盘": 10.05 + base * 0.1,
            "最高": 10.2 + base * 0.1,
            "最低": 9.9 + base * 0.1,
            "成交量": 1_000_000 + base * 1000,
            "成交额": 100_000_000 + base * 100_000,
            "涨跌幅": 1.0,
            "换手率": 2.0,
        })

    @staticmethod
    def _valuation_frame(value: float) -> pd.DataFrame:
        return pd.DataFrame({
            "date": [date(2024, 1, 4), date(2024, 1, 8)],
            "value": [value, value * 10],
        })

    def test_ingest_uses_only_daily_and_valuation_values_available_by_snapshot_date(self) -> None:
        daily_fetcher = MagicMock(return_value=self._daily_frame())

        def valuation_fetcher(*, indicator: str, **_: object) -> pd.DataFrame:
            return self._valuation_frame({"市盈率(TTM)": 10, "市净率": 2, "总市值": 1e10}[indicator])

        service = StockSelectionFactorIngestionService(
            repository=self.repository,
            daily_fetcher=daily_fetcher,
            valuation_fetcher=valuation_fetcher,
        )
        result = service.ingest(
            market="cn",
            snapshot_dates=[date(2024, 1, 5)],
            universe=[{"symbol": "600519", "name": "贵州茅台", "industry": "白酒"}],
        )

        self.assertEqual(result["row_count"], 1)
        self.assertFalse(result["methodology"]["uses_future_values"])
        row = self.repository.upsert_many.call_args.kwargs["rows"][0]
        self.assertEqual(row["pe_ratio"], 10)
        self.assertEqual(row["pb_ratio"], 2)
        self.assertEqual(row["total_mv"], 1e10)
        self.assertEqual(row["source"]["valuation_dates"]["pe_ratio"], "2024-01-04")
        self.assertGreaterEqual(row["factors"]["daily_data_points"], 60)
        self.assertIsNotNone(row["factors"]["change_60d"])
        self.assertEqual(row["quality_status"], "complete")
        daily_fetcher.assert_called_once()

    def test_missing_exact_daily_bar_is_persisted_as_partial_for_audit(self) -> None:
        service = StockSelectionFactorIngestionService(
            repository=self.repository,
            daily_fetcher=MagicMock(return_value=self._daily_frame()),
            valuation_fetcher=MagicMock(return_value=self._valuation_frame(10)),
        )
        service.ingest(
            market="cn",
            snapshot_dates=[date(2024, 1, 6)],
            universe=[{"symbol": "600519", "name": "贵州茅台"}],
        )

        row = self.repository.upsert_many.call_args.kwargs["rows"][0]
        self.assertEqual(row["quality_status"], "partial")
        self.assertIn("price", row["missing_fields"])
        self.assertIn("turnover_rate", row["missing_fields"])

    def test_fetch_failure_does_not_abort_other_auditable_fields(self) -> None:
        service = StockSelectionFactorIngestionService(
            repository=self.repository,
            daily_fetcher=MagicMock(side_effect=RuntimeError("daily timeout")),
            valuation_fetcher=MagicMock(return_value=self._valuation_frame(10)),
        )
        result = service.ingest(
            market="cn",
            snapshot_dates=[date(2024, 1, 5)],
            universe=[{"symbol": "600519", "name": "贵州茅台"}],
        )

        self.assertEqual(result["error_count"], 1)
        self.assertEqual(result["errors"][0]["stage"], "daily")
        row = self.repository.upsert_many.call_args.kwargs["rows"][0]
        self.assertIsNone(row["price"])
        self.assertEqual(row["pe_ratio"], 10)

    def test_ingest_persists_tushare_corporate_actions_outside_selection_factors(self) -> None:
        action_repository = MagicMock()
        action_repository.upsert_many.return_value = {"inserted": 2, "updated": 0, "total": 2}
        action_fetcher = MagicMock(return_value=[
            {
                "symbol": "600519",
                "effective_date": date(2024, 1, 10),
                "action_type": "cash_dividend",
                "cash_dividend_per_share": 1.5,
                "source": "tushare.dividend",
                "source_record_key": "600519.SH|2024-01-10|cash_dividend",
            },
            {
                "symbol": "600519",
                "effective_date": date(2024, 1, 10),
                "action_type": "split_adjustment",
                "split_ratio": 1.2,
                "source": "tushare.dividend",
                "source_record_key": "600519.SH|2024-01-10|split_adjustment",
            },
        ])
        service = StockSelectionFactorIngestionService(
            repository=self.repository,
            daily_fetcher=MagicMock(return_value=self._daily_frame()),
            valuation_fetcher=MagicMock(return_value=self._valuation_frame(10)),
            corporate_action_repository=action_repository,
            corporate_action_fetcher=action_fetcher,
        )

        result = service.ingest(
            market="cn",
            snapshot_dates=[date(2024, 1, 5)],
            universe=[{"symbol": "600519", "name": "贵州茅台"}],
        )

        self.assertEqual(result["corporate_action_count"], 2)
        self.assertTrue(result["methodology"]["corporate_actions_do_not_feed_selection_factors"])
        action_fetcher.assert_called_once_with(
            stock_code="600519",
            start_date=date(2023, 12, 29),
            end_date=date(2025, 2, 8),
        )
        self.assertEqual(action_repository.upsert_many.call_args.kwargs["market"], "cn")
        self.assertEqual(len(action_repository.upsert_many.call_args.kwargs["rows"]), 2)

    def test_corporate_action_failure_is_audited_without_losing_factor_rows(self) -> None:
        service = StockSelectionFactorIngestionService(
            repository=self.repository,
            daily_fetcher=MagicMock(return_value=self._daily_frame()),
            valuation_fetcher=MagicMock(return_value=self._valuation_frame(10)),
            corporate_action_fetcher=MagicMock(side_effect=RuntimeError("dividend permission denied")),
        )
        result = service.ingest(
            market="cn",
            snapshot_dates=[date(2024, 1, 5)],
            universe=[{"symbol": "600519", "name": "贵州茅台"}],
        )

        self.assertEqual(result["row_count"], 1)
        self.assertEqual(result["corporate_action_count"], 0)
        self.assertIn("corporate_actions", [item["stage"] for item in result["errors"]])

    def test_requires_explicit_point_in_time_names_and_bounded_cn_inputs(self) -> None:
        service = StockSelectionFactorIngestionService(
            repository=self.repository,
            daily_fetcher=MagicMock(),
            valuation_fetcher=MagicMock(),
        )
        with self.assertRaisesRegex(ValueError, "market=cn"):
            service.ingest(
                market="us",
                snapshot_dates=[date(2024, 1, 5)],
                universe=[{"symbol": "AAPL", "name": "Apple"}],
            )
        with self.assertRaisesRegex(ValueError, "point-in-time name"):
            service.ingest(
                market="cn",
                snapshot_dates=[date(2024, 1, 5)],
                universe=[{"symbol": "600519"}],
            )


if __name__ == "__main__":
    unittest.main()
