# -*- coding: utf-8 -*-
"""Long-window calibration trend tests for persisted stock-selection Agent runs."""

from __future__ import annotations

import os
import tempfile
import unittest

from src.config import Config
from src.repositories.stock_selection_agent_repo import StockSelectionAgentRepository
from src.storage import DatabaseManager


class StockSelectionAgentCalibrationTrendsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "agent-calibration.db")
        self.env_path = os.path.join(self.temp_dir.name, ".env")
        with open(self.env_path, "w", encoding="utf-8") as env_file:
            env_file.write("STOCK_LIST=600519\n")
        self.original_env = {key: os.environ.get(key) for key in ("ENV_FILE", "DATABASE_PATH")}
        os.environ["ENV_FILE"] = self.env_path
        os.environ["DATABASE_PATH"] = self.db_path
        Config.reset_instance()
        DatabaseManager.reset_instance()
        self.repo = StockSelectionAgentRepository(DatabaseManager.get_instance())

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
    def _quality(
        *,
        state: str,
        utility: float,
        applied: bool,
        mature_samples: int,
        transition: str | None = None,
    ) -> dict:
        return {
            "schema_version": 3,
            "state": state,
            "transition": transition,
            "mature_sample_count": mature_samples,
            "gate_blocked": state == "blocked",
            "return_risk_objective_state": state,
            "return_risk_objective_reason": f"objective_{state}",
            "return_risk_objective_applied": applied,
            "return_risk_utility_pct": utility,
            "return_risk_objective": {
                "version": "candidate-return-risk-v1",
                "state": state,
                "metrics": {
                    "return_risk_utility_pct": utility,
                    "daily_return_coverage_pct": 100.0,
                },
            },
        }

    def test_summarizes_observed_unknown_daily_and_grouped_snapshots(self) -> None:
        self.repo.create_run(
            run_uid="legacy-hk",
            trigger_source="unit_test",
            strategy="dual_low",
            market="hk",
            diagnostics={"cross_run_quality": {"state": "insufficient_evidence"}},
        )
        self.repo.create_run(
            run_uid="guarded-cn",
            trigger_source="unit_test",
            strategy="dual_low",
            market="cn",
            diagnostics={
                "cross_run_quality": self._quality(
                    state="guarded",
                    utility=-0.4,
                    applied=True,
                    mature_samples=12,
                    transition="insufficient_evidence->guarded",
                )
            },
        )
        self.repo.create_run(
            run_uid="healthy-us",
            trigger_source="unit_test",
            strategy="momentum_quality",
            market="us",
            diagnostics={
                "cross_run_quality": self._quality(
                    state="healthy",
                    utility=0.8,
                    applied=False,
                    mature_samples=20,
                )
            },
        )

        result = self.repo.summarize_return_risk_calibration_trends(days=30)

        self.assertEqual(result["total"], 3)
        self.assertEqual(result["observed_count"], 2)
        self.assertEqual(result["unknown_count"], 1)
        self.assertEqual(result["observation_rate_pct"], 66.67)
        self.assertEqual(result["state_counts"], {"guarded": 1, "healthy": 1})
        self.assertEqual(result["version_counts"], {"candidate-return-risk-v1": 2})
        self.assertEqual(result["market_counts"], {"cn": 1, "us": 1})
        self.assertEqual(result["applied_count"], 1)
        self.assertEqual(result["applied_rate_pct"], 50.0)
        self.assertEqual(result["average_utility_pct"], 0.2)
        self.assertEqual(result["minimum_utility_pct"], -0.4)
        self.assertEqual(result["maximum_utility_pct"], 0.8)
        self.assertEqual(result["latest_mature_sample_count"], 20)
        self.assertEqual(result["max_mature_sample_count"], 20)
        self.assertEqual(result["latest"]["run_uid"], "healthy-us")
        self.assertEqual(result["latest"]["state"], "healthy")
        self.assertEqual(result["health"], "ok")
        self.assertEqual(len(result["groups"]), 2)
        self.assertEqual(result["daily"][0]["run_snapshot_count"], 2)
        self.assertEqual(result["daily"][0]["average_utility_pct"], 0.2)
        self.assertTrue(result["methodology"]["overlapping_rolling_samples"])
        self.assertFalse(result["methodology"]["independent_sample_count_claimed"])

        cn_result = self.repo.summarize_return_risk_calibration_trends(
            days=30,
            market="cn",
        )
        self.assertEqual(cn_result["total"], 1)
        self.assertEqual(cn_result["observed_count"], 1)
        self.assertEqual(cn_result["latest"]["run_uid"], "guarded-cn")
        self.assertEqual(cn_result["health"], "warning")

    def test_shadow_trends_reject_legacy_unscoped_quality_snapshots(self) -> None:
        legacy = self.repo.create_run(
            run_uid="shadow-legacy",
            trigger_source="agent_calibration_shadow",
            strategy="dual_low",
            market="cn",
            diagnostics={
                "cross_run_quality": self._quality(
                    state="healthy",
                    utility=0.5,
                    applied=False,
                    mature_samples=20,
                )
            },
        )
        scoped_quality = self._quality(
            state="insufficient_evidence",
            utility=0.0,
            applied=False,
            mature_samples=0,
        )
        scoped_quality.update({
            "trigger_source": "agent_calibration_shadow",
            "run_status": "completed",
        })
        scoped = self.repo.create_run(
            run_uid="shadow-scoped",
            trigger_source="agent_calibration_shadow",
            strategy="dual_low",
            market="cn",
            diagnostics={"cross_run_quality": scoped_quality},
        )
        for run in (legacy, scoped):
            self.repo.complete_run(
                run_id=int(run["id"]),
                status="completed",
                candidate_count=0,
                planned_count=0,
                submitted_count=0,
                skipped_count=0,
            )

        result = self.repo.summarize_return_risk_calibration_trends(
            days=30,
            trigger_source="agent_calibration_shadow",
            market="cn",
            status="completed",
        )

        self.assertEqual(result["total"], 2)
        self.assertEqual(result["observed_count"], 1)
        self.assertEqual(result["unknown_count"], 1)
        self.assertEqual(result["scope_mismatch_count"], 1)
        self.assertEqual(result["run_strategy_counts"], {"dual_low": 2})
        self.assertEqual(result["strategy_counts"], {"dual_low": 1})
        self.assertEqual(result["latest"]["run_uid"], "shadow-scoped")
        self.assertEqual(result["latest_mature_sample_count"], 0)
        self.assertIn(
            "trigger_source=agent_calibration_shadow",
            result["methodology"]["calibration_shadow_scope_requirement"],
        )


if __name__ == "__main__":
    unittest.main()
