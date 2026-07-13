# -*- coding: utf-8 -*-
"""Regression tests for persisted runtime scheduler task events."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from src.repositories.runtime_scheduler_repo import RuntimeSchedulerRepository
from src.storage import DatabaseManager


class RuntimeSchedulerRepositoryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "runtime_scheduler_repo.db"
        DatabaseManager.reset_instance()
        self.db = DatabaseManager(db_url=f"sqlite:///{self.db_path}")

    def tearDown(self) -> None:
        DatabaseManager.reset_instance()
        self.temp_dir.cleanup()

    def test_records_and_filters_task_events(self) -> None:
        repo = RuntimeSchedulerRepository(self.db)
        repo.record_task_event(
            name="vnpy_paper_auto_trade",
            status="completed",
            message="trade completed",
            details={"submitted_count": 1, "skipped_count": 0},
            duration_seconds=0.12,
            timestamp=datetime(2026, 7, 2, 9, 31, 0),
        )
        repo.record_task_event(
            name="vnpy_paper_auto_retry",
            status="failed",
            message="retry failed",
            details={"error": "boom"},
            duration_seconds=0.1,
            timestamp=datetime(2026, 7, 2, 9, 32, 0),
        )

        filtered = repo.list_task_events(
            name="vnpy_paper_auto_retry",
            status="failed",
            limit=10,
        )
        recent = repo.list_task_events(limit=1)

        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["message"], "retry failed")
        self.assertEqual(filtered[0]["details"]["error"], "boom")
        self.assertEqual(filtered[0]["duration_seconds"], 0.1)
        self.assertEqual(recent[0]["name"], "vnpy_paper_auto_retry")

    def test_cleanup_task_events_deletes_rows_older_than_cutoff(self) -> None:
        repo = RuntimeSchedulerRepository(self.db)
        repo.record_task_event(
            name="vnpy_paper_auto_trade",
            status="completed",
            message="old event",
            timestamp=datetime(2026, 7, 1, 9, 31, 0),
        )
        repo.record_task_event(
            name="vnpy_paper_auto_trade",
            status="completed",
            message="new event",
            timestamp=datetime(2026, 7, 3, 9, 31, 0),
        )

        deleted = repo.cleanup_task_events(
            older_than=datetime(2026, 7, 2, 0, 0, 0),
        )
        remaining = repo.list_task_events(limit=10)

        self.assertEqual(deleted, 1)
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["message"], "new event")

    def test_list_task_events_filters_by_start_time(self) -> None:
        repo = RuntimeSchedulerRepository(self.db)
        repo.record_task_event(
            name="vnpy_paper_auto_trade",
            status="completed",
            message="outside window",
            timestamp=datetime(2026, 7, 1, 9, 31, 0),
        )
        repo.record_task_event(
            name="vnpy_paper_auto_trade",
            status="failed",
            message="inside window",
            timestamp=datetime(2026, 7, 3, 9, 31, 0),
        )

        events = repo.list_task_events(
            started_at=datetime(2026, 7, 2, 0, 0, 0),
            limit=5000,
        )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["message"], "inside window")


if __name__ == "__main__":
    unittest.main()
