# -*- coding: utf-8 -*-
"""API contract tests for point-in-time AlphaSift replay."""

from __future__ import annotations

import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.v1.endpoints.alphasift import router
from src.services.task_queue import TaskStatus


class AlphaSiftReplayApiTestCase(unittest.TestCase):
    def setUp(self) -> None:
        app = FastAPI()
        app.include_router(router, prefix="/api/v1/alphasift")
        self.client = TestClient(app)

    def test_import_factor_snapshot_forwards_normalized_rows(self) -> None:
        repository = MagicMock()
        repository.upsert_many.return_value = {"inserted": 1, "updated": 0, "total": 1}
        with patch(
            "api.v1.endpoints.alphasift.StockSelectionFactorSnapshotRepository",
            return_value=repository,
        ):
            response = self.client.post(
                "/api/v1/alphasift/replay/snapshots",
                json={
                    "market": "cn",
                    "snapshot_date": "2024-01-05",
                    "rows": [{
                        "symbol": "600519",
                        "name": "贵州茅台",
                        "price": 1600,
                        "factors": {"rsi14": 55},
                        "source": {"daily": "akshare"},
                    }],
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["snapshot_date"], "2024-01-05")
        call = repository.upsert_many.call_args.kwargs
        self.assertEqual(call["market"], "cn")
        self.assertEqual(call["rows"][0]["factors"]["rsi14"], 55)

    def test_compatibility_endpoint_returns_coverage_diagnostics(self) -> None:
        service = MagicMock()
        service.compatibility.return_value = {
            "strategy": "dual_low",
            "market": "cn",
            "snapshot_date": "2024-01-05",
            "universe_count": 100,
            "hard_coverage_ratio": 0.99,
            "score_coverage_ratio": 0.95,
        }
        with patch(
            "api.v1.endpoints.alphasift.StockSelectionStrategyReplayService",
            return_value=service,
        ):
            response = self.client.get(
                "/api/v1/alphasift/replay/compatibility"
                "?strategy=dual_low&market=cn&snapshot_date=2024-01-05"
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["hard_coverage_ratio"], 0.99)

    def test_historical_universe_endpoint_forwards_asof_date(self) -> None:
        service = MagicMock()
        service.resolve.return_value = {
            "market": "cn",
            "snapshot_date": "2024-01-05",
            "total_count": 1,
            "returned_count": 1,
            "truncated": False,
            "items": [{"symbol": "600001", "name": "sample"}],
            "methodology": {"uses_current_universe_fallback": False},
        }
        with patch(
            "api.v1.endpoints.alphasift.StockSelectionHistoricalUniverseService",
            return_value=service,
        ):
            response = self.client.get(
                "/api/v1/alphasift/replay/universe?snapshot_date=2024-01-05&market=cn&limit=100"
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"][0]["symbol"], "600001")
        self.assertEqual(service.resolve.call_args.kwargs["snapshot_date"], date(2024, 1, 5))
        self.assertEqual(service.resolve.call_args.kwargs["limit"], 100)

    def test_replay_endpoint_fails_closed_when_snapshot_is_incomplete(self) -> None:
        service = MagicMock()
        service.replay.side_effect = ValueError("hard-filter field coverage is below the replay threshold")
        with patch(
            "api.v1.endpoints.alphasift.StockSelectionStrategyReplayService",
            return_value=service,
        ):
            response = self.client.post(
                "/api/v1/alphasift/replay/run",
                json={
                    "strategy": "dual_low",
                    "market": "cn",
                    "snapshot_date": "2024-01-05",
                },
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"]["error"], "strategy_replay_not_ready")

    def test_portfolio_backtest_endpoint_forwards_cost_and_benchmark_assumptions(self) -> None:
        service = MagicMock()
        service.run.return_value = {
            "strategy": "dual_low",
            "market": "cn",
            "snapshot_count": 2,
            "metrics": {"total_return_pct": 10, "excess_return_pct": 5},
            "periods": [],
        }
        with patch(
            "api.v1.endpoints.alphasift.StockSelectionPortfolioBacktestService",
            return_value=service,
        ):
            response = self.client.post(
                "/api/v1/alphasift/replay/portfolio-backtest",
                json={
                    "strategy": "dual_low",
                    "market": "cn",
                    "date_from": "2024-01-01",
                    "date_to": "2024-02-01",
                    "top_k": 3,
                    "final_holding_bars": 10,
                    "initial_capital": 200000,
                    "cost_profile": "cn_retail_reference",
                    "commission_bps": 4,
                    "minimum_commission": 5,
                    "sell_tax_bps": 5,
                    "sell_tax_mode": "cn_historical_stamp_duty",
                    "slippage_bps": 6,
                    "benchmark_symbol": "000300",
                    "enforce_tradeability": True,
                    "accounting_mode": "cash_ledger",
                    "target_weights": {"600519": 60, "000001": 30},
                    "corporate_actions": [{
                        "symbol": "600519",
                        "effective_date": "2024-01-15",
                        "action_type": "cash_dividend",
                        "cash_dividend_per_share": 1.5,
                    }, {
                        "symbol": "000001",
                        "effective_date": "2024-01-20",
                        "action_type": "split_adjustment",
                        "split_ratio": 1.5,
                        "cash_in_lieu_price": 9.8,
                    }],
                    "include_persisted_corporate_actions": False,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["metrics"]["excess_return_pct"], 5)
        call = service.run.call_args.kwargs
        self.assertEqual(call["top_k"], 3)
        self.assertEqual(call["cost_profile"], "cn_retail_reference")
        self.assertEqual(call["commission_bps"], 4)
        self.assertEqual(call["minimum_commission"], 5)
        self.assertEqual(call["sell_tax_bps"], 5)
        self.assertEqual(call["sell_tax_mode"], "cn_historical_stamp_duty")
        self.assertEqual(call["benchmark_symbol"], "000300")
        self.assertTrue(call["enforce_tradeability"])
        self.assertEqual(call["accounting_mode"], "cash_ledger")
        self.assertEqual(call["target_weights"], {"600519": 60, "000001": 30})
        self.assertEqual(call["corporate_actions"][0]["symbol"], "600519")
        self.assertEqual(call["corporate_actions"][0]["effective_date"], date(2024, 1, 15))
        self.assertEqual(call["corporate_actions"][0]["cash_dividend_per_share"], 1.5)
        self.assertEqual(call["corporate_actions"][1]["effective_date"], date(2024, 1, 20))
        self.assertEqual(call["corporate_actions"][1]["cash_in_lieu_price"], 9.8)
        self.assertFalse(call["include_persisted_corporate_actions"])

    def test_historical_factor_ingestion_runs_as_background_task(self) -> None:
        queue = MagicMock()
        queue.submit_background_task.return_value = SimpleNamespace(
            task_id="factor-task-1",
            trace_id="factor-task-1",
            status=TaskStatus.PENDING,
            message="历史因子采集任务已提交",
        )
        with patch("api.v1.endpoints.alphasift.get_task_queue", return_value=queue):
            response = self.client.post(
                "/api/v1/alphasift/replay/ingestion/tasks",
                json={
                    "market": "cn",
                    "snapshot_dates": ["2024-01-05", "2024-01-12"],
                    "universe": [{"symbol": "600519", "name": "贵州茅台", "industry": "白酒"}],
                },
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["task_id"], "factor-task-1")
        self.assertEqual(response.json()["snapshot_count"], 2)
        call = queue.submit_background_task.call_args
        self.assertEqual(call.kwargs["report_type"], "alphasift_factor_ingestion")
        self.assertTrue(callable(call.args[0]))

    def test_full_market_ingestion_creates_persistent_job_and_background_task(self) -> None:
        service = MagicMock()
        service.create.return_value = {
            "job_id": "full-job-1",
            "status": "pending",
            "market": "cn",
            "snapshot_dates": ["2024-01-05"],
            "total_symbols": 5000,
            "next_offset": 0,
            "progress_pct": 0,
        }
        queue = MagicMock()
        queue.submit_background_task.return_value = SimpleNamespace(task_id="queue-task-1")
        with (
            patch(
                "api.v1.endpoints.alphasift.StockSelectionFullMarketIngestionService",
                return_value=service,
            ),
            patch("api.v1.endpoints.alphasift.get_task_queue", return_value=queue),
            patch("api.v1.endpoints.alphasift.uuid.uuid4") as uuid4,
        ):
            uuid4.return_value.hex = "queue-task-1"
            response = self.client.post(
                "/api/v1/alphasift/replay/full-market-ingestion/jobs",
                json={
                    "market": "cn",
                    "snapshot_dates": ["2024-01-05"],
                    "batch_size": 25,
                },
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["job_id"], "full-job-1")
        self.assertEqual(response.json()["task_id"], "queue-task-1")
        service.create.assert_called_once()
        self.assertTrue(callable(queue.submit_background_task.call_args.args[0]))

    def test_full_market_ingestion_resume_rejects_active_job_without_force(self) -> None:
        service = MagicMock()
        service.get.return_value = {"job_id": "full-job-1", "status": "processing"}
        with patch(
            "api.v1.endpoints.alphasift.StockSelectionFullMarketIngestionService",
            return_value=service,
        ):
            response = self.client.post(
                "/api/v1/alphasift/replay/full-market-ingestion/jobs/full-job-1/resume"
            )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["error"], "full_market_ingestion_job_active")

    def test_full_market_ingestion_lists_persistent_jobs(self) -> None:
        service = MagicMock()
        service.list_recent.return_value = [{"job_id": "full-job-1", "status": "failed"}]
        with patch(
            "api.v1.endpoints.alphasift.StockSelectionFullMarketIngestionService",
            return_value=service,
        ):
            response = self.client.get(
                "/api/v1/alphasift/replay/full-market-ingestion/jobs?limit=5"
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"][0]["job_id"], "full-job-1")
        service.list_recent.assert_called_once_with(limit=5)


if __name__ == "__main__":
    unittest.main()
