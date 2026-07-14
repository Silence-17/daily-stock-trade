# -*- coding: utf-8 -*-
"""Forward-evaluation tests for persisted stock-selection Agent decisions."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date, datetime
from unittest.mock import patch

from src.config import Config
from src.repositories.stock_selection_agent_repo import StockSelectionAgentRepository
from src.services.stock_selection_agent_backtest_service import StockSelectionAgentBacktestService
from src.storage import DatabaseManager, StockDaily, StockSelectionAgentDecision


class StockSelectionAgentBacktestServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "agent_backtest.db")
        self.env_path = os.path.join(self.temp_dir.name, ".env")
        with open(self.env_path, "w", encoding="utf-8") as env_file:
            env_file.write("STOCK_LIST=600519,000001\n")
        self.original_env = {key: os.environ.get(key) for key in ("ENV_FILE", "DATABASE_PATH")}
        os.environ["ENV_FILE"] = self.env_path
        os.environ["DATABASE_PATH"] = self.db_path
        Config.reset_instance()
        DatabaseManager.reset_instance()
        self.db = DatabaseManager.get_instance()
        self.repo = StockSelectionAgentRepository(self.db)
        self.service = StockSelectionAgentBacktestService(self.db)

    def tearDown(self) -> None:
        DatabaseManager.reset_instance()
        Config.reset_instance()
        for key, value in self.original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temp_dir.cleanup()

    def _record_decision(
        self,
        *,
        run_uid: str,
        strategy: str,
        symbol: str,
        status: str,
        price: float | None,
        created_at: datetime,
        order_result: dict | None = None,
        action: str = "buy",
    ) -> int:
        run = self.repo.create_run(
            run_uid=run_uid,
            trigger_source="unit_test",
            strategy=strategy,
            market="cn",
        )
        decision = self.repo.record_decision(
            run_id=run["id"],
            sequence=1,
            symbol=symbol,
            market="cn",
            action=action,
            status=status,
            score=80,
            price=price,
            order_result=order_result,
        )
        with self.db.get_session() as session:
            row = session.get(StockSelectionAgentDecision, decision["id"])
            assert row is not None
            row.created_at = created_at
            session.commit()
        return int(decision["id"])

    def test_evaluate_uses_only_strictly_later_bars_and_builds_horizon_matrix(self) -> None:
        self._record_decision(
            run_uid="agent-forward-1",
            strategy="dual_low",
            symbol="600519",
            status="filled",
            price=100.0,
            created_at=datetime(2024, 1, 1, 10, 0),
        )
        with self.db.get_session() as session:
            session.add_all(
                [
                    StockDaily(code="600519", date=date(2024, 1, 1), high=999, low=1, close=999),
                    StockDaily(code="600519", date=date(2024, 1, 2), high=105, low=99, close=103),
                    StockDaily(code="600519", date=date(2024, 1, 3), high=104, low=94, close=95),
                ]
            )
            session.commit()

        result = self.service.evaluate(eval_windows=[1, 2], neutral_band_pct=2)

        self.assertEqual(result["scanned_count"], 1)
        self.assertTrue(result["methodology"]["lookahead_protection"])
        self.assertFalse(result["methodology"]["reruns_historical_strategy"])
        item = result["items"][0]
        self.assertEqual(item["forward_bar_count"], 2)
        self.assertAlmostEqual(item["horizons"]["1"]["stock_return_pct"], 3.0)
        self.assertAlmostEqual(item["horizons"]["1"]["max_favorable_excursion_pct"], 5.0)
        self.assertAlmostEqual(item["horizons"]["2"]["stock_return_pct"], -5.0)
        self.assertEqual(result["matrix"]["1"]["win_count"], 1)
        self.assertEqual(result["matrix"]["2"]["loss_count"], 1)
        self.assertEqual(result["strategy_matrix"]["dual_low"]["1"]["coverage_pct"], 100.0)

    def test_review_quality_matrix_separates_rule_and_llm_versions_across_horizons(self) -> None:
        rule_passed = {
            "agent_review": {
                "schema_version": 1,
                "status": "passed",
                "reviewer": "rule_agent_v1",
            },
            "llm_review": {
                "schema_version": 1,
                "status": "passed",
                "reviewer": "llm_reviewer_v1",
                "model": "openai/model-a",
                "prompt_version": "prompt-v2",
                "evaluator_version": "eval-v1",
            },
        }
        rule_blocked = {
            "agent_review": {
                "schema_version": 1,
                "status": "blocked",
                "reviewer": "rule_agent_v1",
            },
            "llm_review": {
                "schema_version": 1,
                "status": "blocked",
                "reviewer": "llm_reviewer_v1",
                "model": "openai/model-b",
                "prompt_version": "prompt-v2",
                "evaluator_version": "eval-v1",
            },
        }
        self._record_decision(
            run_uid="review-quality-pass",
            strategy="dual_low",
            symbol="600519",
            status="filled",
            price=100.0,
            created_at=datetime(2024, 1, 1, 10, 0),
            order_result=rule_passed,
        )
        self._record_decision(
            run_uid="review-quality-block",
            strategy="dual_low",
            symbol="000001",
            status="skipped",
            price=10.0,
            created_at=datetime(2024, 1, 1, 10, 0),
            order_result=rule_blocked,
            action="skip",
        )
        with self.db.get_session() as session:
            session.add_all(
                [
                    StockDaily(code="600519", date=date(2024, 1, 2), high=106, low=99, close=105),
                    StockDaily(code="600519", date=date(2024, 1, 3), high=111, low=104, close=110),
                    StockDaily(code="000001", date=date(2024, 1, 2), high=10, low=8.8, close=9),
                    StockDaily(code="000001", date=date(2024, 1, 3), high=9.2, low=7.8, close=8),
                ]
            )
            session.commit()

        result = self.service.evaluate(eval_windows=[1, 2], include_skipped=True)

        groups = {item["key"]: item for item in result["review_quality_matrix"]}
        rule = groups["rule_agent:rule_agent_v1:schema_v1"]
        self.assertEqual(rule["sample_count"], 2)
        self.assertEqual(rule["status_counts"], {"blocked": 1, "passed": 1})
        self.assertEqual(rule["horizons"]["1"]["passed_precision_pct"], 100.0)
        self.assertEqual(rule["horizons"]["1"]["blocked_avoidance_rate_pct"], 100.0)
        self.assertEqual(rule["horizons"]["2"]["return_spread_pct"], 30.0)
        self.assertIn("llm:openai/model-a:prompt-v2/eval-v1", groups)
        self.assertIn("llm:openai/model-b:prompt-v2/eval-v1", groups)
        self.assertEqual(
            result["items"][0]["reviews"][0]["source"],
            "rule_agent",
        )

    def test_include_skipped_false_excludes_risk_rejected_candidates(self) -> None:
        anchor = datetime(2024, 1, 1, 10, 0)
        self._record_decision(
            run_uid="agent-filled",
            strategy="dual_low",
            symbol="600519",
            status="filled",
            price=100.0,
            created_at=anchor,
        )
        self._record_decision(
            run_uid="agent-skipped",
            strategy="quality_value",
            symbol="000001",
            status="skipped",
            price=10.0,
            created_at=anchor,
            action="skip",
        )

        all_result = self.service.evaluate(eval_windows=[1], include_skipped=True)
        executable_result = self.service.evaluate(eval_windows=[1], include_skipped=False)

        self.assertEqual(all_result["total"], 2)
        self.assertEqual(executable_result["total"], 1)
        self.assertEqual(executable_result["items"][0]["run_uid"], "agent-filled")

    def test_missing_price_and_forward_bars_are_audited_as_unable(self) -> None:
        self._record_decision(
            run_uid="agent-no-price",
            strategy="dual_low",
            symbol="600519",
            status="skipped",
            price=None,
            created_at=datetime(2024, 1, 1, 10, 0),
        )

        result = self.service.evaluate(eval_windows=[1, 5])

        self.assertEqual(result["matrix"]["1"]["completed_count"], 0)
        self.assertEqual(result["matrix"]["1"]["unable_reason_counts"], {"invalid_anchor_price": 1})
        self.assertEqual(result["matrix"]["5"]["coverage_pct"], 0.0)

    def test_strategy_and_created_at_filters_apply_to_decisions(self) -> None:
        self._record_decision(
            run_uid="agent-january-dual",
            strategy="dual_low",
            symbol="600519",
            status="filled",
            price=100.0,
            created_at=datetime(2024, 1, 5, 10, 0),
        )
        self._record_decision(
            run_uid="agent-february-quality",
            strategy="quality_value",
            symbol="000001",
            status="filled",
            price=10.0,
            created_at=datetime(2024, 2, 5, 10, 0),
        )

        result = self.service.evaluate(
            strategy="dual_low",
            created_from=datetime(2024, 1, 1),
            created_to=datetime(2024, 1, 31, 23, 59),
            eval_windows=[1],
        )

        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["run_uid"], "agent-january-dual")
        self.assertEqual(set(result["strategy_matrix"]), {"dual_low"})

    def test_rejects_invalid_windows_and_reversed_date_range(self) -> None:
        with self.assertRaisesRegex(ValueError, "eval_windows"):
            self.service.evaluate(eval_windows=[0])
        with self.assertRaisesRegex(ValueError, "created_from"):
            self.service.evaluate(
                created_from=datetime(2024, 2, 1),
                created_to=datetime(2024, 1, 1),
            )

    def test_quality_snapshot_tracks_insufficient_blocked_guarded_and_recovery_states(self) -> None:
        base_result = {
            "generated_at": datetime(2024, 1, 31, 12, 0),
            "truncated": False,
            "matrix": {
                "5": {
                    "sample_count": 12,
                    "completed_count": 4,
                    "coverage_pct": 33.33,
                    "win_rate_pct": 50.0,
                    "average_return_pct": 1.0,
                    "median_return_pct": 0.5,
                    "average_max_adverse_excursion_pct": -2.0,
                    "unable_reason_counts": {"insufficient_forward_bars": 8},
                }
            },
        }
        with patch.object(self.service, "evaluate", return_value=base_result):
            insufficient = self.service.build_quality_snapshot(
                strategy="dual_low",
                market="cn",
                min_mature_samples=10,
            )
        self.assertEqual(insufficient["state"], "insufficient_evidence")

        base_result["matrix"]["5"].update(
            completed_count=12,
            coverage_pct=100.0,
            win_rate_pct=40.0,
        )
        with patch.object(self.service, "evaluate", return_value=base_result):
            blocked = self.service.build_quality_snapshot(
                strategy="dual_low",
                market="cn",
                min_mature_samples=10,
                min_win_rate_pct=45,
                previous_state="healthy",
            )
        self.assertEqual(blocked["state"], "blocked")
        self.assertEqual(blocked["transition"], "healthy->blocked")

        base_result["matrix"]["5"].update(win_rate_pct=50.0, average_return_pct=-0.2)
        with patch.object(self.service, "evaluate", return_value=base_result):
            guarded = self.service.build_quality_snapshot(
                strategy="dual_low",
                market="cn",
                previous_state="blocked",
            )
        self.assertEqual(guarded["state"], "guarded")

        base_result["matrix"]["5"].update(average_return_pct=0.8)
        with patch.object(self.service, "evaluate", return_value=base_result):
            healthy = self.service.build_quality_snapshot(
                strategy="dual_low",
                market="cn",
                previous_state="blocked",
            )
        self.assertEqual(healthy["state"], "healthy")
        self.assertEqual(healthy["transition"], "blocked->healthy")


if __name__ == "__main__":
    unittest.main()
