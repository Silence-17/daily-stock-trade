# -*- coding: utf-8 -*-
"""Tests for persisted historical stock-selection corporate actions."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date

from src.repositories.stock_selection_corporate_action_repo import (
    StockSelectionCorporateActionRepository,
)
from src.storage import DatabaseManager


class StockSelectionCorporateActionRepositoryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        DatabaseManager.reset_instance()
        path = os.path.join(self.temp_dir.name, "corporate-actions.db")
        self.db = DatabaseManager(db_url=f"sqlite:///{path}")
        self.repository = StockSelectionCorporateActionRepository(self.db)

    def tearDown(self) -> None:
        DatabaseManager.reset_instance()
        self.temp_dir.cleanup()

    def test_upsert_is_idempotent_and_range_query_is_bounded(self) -> None:
        row = {
            "symbol": "600519",
            "effective_date": date(2024, 1, 10),
            "action_type": "cash_dividend",
            "cash_dividend_per_share": 1.0,
            "source": "tushare.dividend",
            "source_record_key": "600519.SH|2024-01-10|cash_dividend",
        }
        first = self.repository.upsert_many(market="cn", rows=[row])
        second = self.repository.upsert_many(
            market="cn",
            rows=[{**row, "cash_dividend_per_share": 1.5}],
        )

        self.assertEqual(first, {"inserted": 1, "updated": 0, "total": 1})
        self.assertEqual(second, {"inserted": 0, "updated": 1, "total": 1})
        rows = self.repository.list_range(
            market="cn",
            date_from=date(2024, 1, 1),
            date_to=date(2024, 1, 31),
            symbols=["600519"],
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["cash_dividend_per_share"], 1.5)
        self.assertEqual(
            self.repository.list_range(
                market="cn",
                date_from=date(2024, 2, 1),
                date_to=date(2024, 2, 29),
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()
