# -*- coding: utf-8 -*-
"""Tests for resumable full-market factor ingestion."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date
from unittest.mock import MagicMock

from src.config import Config
from src.repositories.stock_selection_factor_ingestion_job_repo import (
    StockSelectionFactorIngestionJobRepository,
)
from src.services.stock_selection_full_market_ingestion_service import (
    StockSelectionFullMarketIngestionService,
)
from src.storage import DatabaseManager


class _FakeIngestion:
    def __init__(self, calls, *, fail_on_call=None):
        self.calls = calls
        self.fail_on_call = fail_on_call

    def ingest(self, *, market, snapshot_dates, universe):
        symbols = [item["symbol"] for item in universe]
        self.calls.append(symbols)
        if self.fail_on_call is not None and len(self.calls) == self.fail_on_call:
            raise RuntimeError("batch source failure")
        row_count = len(symbols) * len(snapshot_dates)
        return {
            "row_count": row_count,
            "inserted": row_count,
            "updated": 0,
            "error_count": 0,
            "errors": [],
            "complete_row_count": row_count,
            "partial_row_count": 0,
            "exact_daily_row_count": row_count,
            "valuation_complete_row_count": row_count,
            "corporate_action_count": len(symbols),
            "corporate_action_inserted": len(symbols),
            "corporate_action_updated": 0,
        }


class StockSelectionFullMarketIngestionServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "full_market.db")
        self.env_path = os.path.join(self.temp_dir.name, ".env")
        with open(self.env_path, "w", encoding="utf-8") as env_file:
            env_file.write("STOCK_LIST=600001\n")
        self.original_env = {key: os.environ.get(key) for key in ("ENV_FILE", "DATABASE_PATH")}
        os.environ["ENV_FILE"] = self.env_path
        os.environ["DATABASE_PATH"] = self.db_path
        Config.reset_instance()
        DatabaseManager.reset_instance()
        self.db = DatabaseManager.get_instance()
        self.repository = StockSelectionFactorIngestionJobRepository(self.db)
        self.universe_service = MagicMock()
        self.universe_service.resolve_many.side_effect = lambda **kwargs: [
            {
                "snapshot_date": snapshot_date.isoformat(),
                "items": [
                    {"symbol": f"60000{index}", "name": f"stock-{index}", "industry": "sample"}
                    for index in range(1, 6)
                ],
                "truncated": False,
                "methodology": {"source": "fixture.lifecycle"},
            }
            for snapshot_date in kwargs["snapshot_dates"]
        ]

    def tearDown(self) -> None:
        DatabaseManager.reset_instance()
        Config.reset_instance()
        for key, value in self.original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temp_dir.cleanup()

    def test_batches_full_universe_and_checkpoints_completion(self) -> None:
        calls = []
        progress = []
        service = StockSelectionFullMarketIngestionService(
            repository=self.repository,
            universe_service=self.universe_service,
            ingestion_factory=lambda: _FakeIngestion(calls),
            progress_callback=lambda value, message: progress.append((value, message)),
        )
        job = service.create(
            market="cn",
            snapshot_dates=[date(2024, 1, 5), date(2024, 1, 12)],
            batch_size=2,
        )

        result = service.run(job["job_id"], task_id="task-1")

        self.assertEqual(calls, [
            ["600001", "600002"], ["600003", "600004"], ["600005"],
            ["600001", "600002"], ["600003", "600004"], ["600005"],
        ])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["next_offset"], 10)
        self.assertEqual(result["completed_batches"], 6)
        self.assertEqual(result["row_count"], 10)
        self.assertEqual(result["total_symbols"], 5)
        self.assertEqual(result["total_work_items"], 10)
        self.assertEqual(result["progress_pct"], 100)
        self.assertEqual(result["result"]["corporate_action_count"], 10)
        self.assertEqual(result["result"]["corporate_action_inserted"], 10)
        self.assertEqual(result["result"]["corporate_action_updated"], 0)
        self.assertEqual(result["result"]["complete_row_count"], 10)
        self.assertEqual(result["result"]["exact_daily_row_count"], 10)
        self.assertEqual(result["universe_source"], "fixture.lifecycle")
        self.assertTrue(progress)
        self.assertEqual(service.list_recent(limit=1)[0]["job_id"], job["job_id"])

    def test_failed_batch_resumes_from_last_successful_offset(self) -> None:
        first_calls = []
        service = StockSelectionFullMarketIngestionService(
            repository=self.repository,
            universe_service=self.universe_service,
            ingestion_factory=lambda: _FakeIngestion(first_calls, fail_on_call=2),
        )
        job = service.create(
            market="cn",
            snapshot_dates=[date(2024, 1, 5)],
            batch_size=2,
        )

        with self.assertRaisesRegex(RuntimeError, "batch source failure"):
            service.run(job["job_id"], task_id="task-1")
        failed = service.get(job["job_id"])
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["next_offset"], 2)

        resumed_calls = []
        resumed = StockSelectionFullMarketIngestionService(
            repository=self.repository,
            universe_service=self.universe_service,
            ingestion_factory=lambda: _FakeIngestion(resumed_calls),
        ).run(job["job_id"], task_id="task-2")

        self.assertEqual(resumed_calls, [["600003", "600004"], ["600005"]])
        self.assertEqual(resumed["status"], "completed")
        self.assertEqual(resumed["completed_batches"], 3)

    def test_force_takeover_rejects_checkpoints_from_the_old_task_lease(self) -> None:
        service = StockSelectionFullMarketIngestionService(
            repository=self.repository,
            universe_service=self.universe_service,
            ingestion_factory=lambda: _FakeIngestion([]),
        )
        job = service.create(
            market="cn",
            snapshot_dates=[date(2024, 1, 5)],
            batch_size=2,
        )
        self.repository.begin(job["job_id"], task_id="old-task")
        self.repository.begin(job["job_id"], task_id="new-task", force=True)

        with self.assertRaisesRegex(RuntimeError, "lease was replaced"):
            self.repository.checkpoint(
                job["job_id"],
                task_id="old-task",
                next_offset=2,
                batch_result={"row_count": 2},
            )


if __name__ == "__main__":
    unittest.main()
