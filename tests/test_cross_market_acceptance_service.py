# -*- coding: utf-8 -*-
"""Acceptance gate tests for the cross-market strategy."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from src.services.cross_market_acceptance_service import CrossMarketAcceptanceService
from src.services.cross_market_paper_strategy import (
    GLOBAL_MARKET_LINKED_THEMES,
    STRATEGY_ID,
)


class _RunRepository:
    def __init__(self) -> None:
        self.runs = []

    def list_runs(self, *, limit=100, offset=0, **filters):
        trigger_source = filters.get("trigger_source")
        matching = [
            item
            for item in self.runs
            if item.get("trigger_source", "vnpy_paper_auto") == trigger_source
        ]
        items = matching[offset : offset + limit]
        return {"items": items, "total": len(matching)}


class CrossMarketAcceptanceServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repository = _RunRepository()
        self.service = CrossMarketAcceptanceService(
            repository=self.repository,
            state_path=Path(self.temp_dir.name) / "acceptance.json",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def _passing_result():
        return {
            "session_count": 500,
            "validation": {
                "passed": True,
                "sessions_passed": True,
                "trades_passed": True,
                "profit_factor_passed": True,
                "drawdown_passed": True,
                "lookahead_passed": True,
                "theme_coverage_passed": True,
                "range_coverage_passed": True,
                "ablation_effects_passed": True,
            },
        }

    @staticmethod
    def _verified_request_payload():
        return {
            "manifest_sha256": "a" * 64,
            "frames_sha256": "b" * 64,
            "frame_count": 500,
            "provenance_schema_version": 1,
            "source_artifacts_verified": True,
            "frames_binding_verified": True,
            "raw_artifact_count": 4,
            "raw_artifacts_sha256": "c" * 64,
        }

    @staticmethod
    def _ready_observation(run_at: datetime):
        required_themes = sorted(GLOBAL_MARKET_LINKED_THEMES)

        def full_theme_coverage():
            return {
                "full_strategy_coverage": True,
                "required_themes": list(required_themes),
                "qualified_themes": list(required_themes),
                "missing_themes": [],
            }

        return {
            "schema_version": 5,
            "strategy_id": STRATEGY_ID,
            "checked_at": run_at.isoformat(),
            "status": "ready",
            "required_checks": {
                "cn_open_available": True,
                "us_first_hour_and_close_available": True,
                "nasdaq_futures_continuous_trend_available": True,
                "us_premarket_themes_available": True,
                "us_close_themes_available": True,
                "japan_market_available": True,
                "korea_continuous_gate_available": True,
                "gold_signal_available": True,
            },
            "missing_requirements": [],
            "evidence": {
                "us_premarket": full_theme_coverage(),
                "us_close_themes": full_theme_coverage(),
                "nasdaq_futures": {
                    "code": "NQ00Y",
                    "available": True,
                    "confirmed": True,
                    "confirmation_sample_count": 3,
                    "confirmation_span_seconds": 120.0,
                },
                "korea": {
                    "linked_technology_gate": {
                        "confirmation_span_seconds": 300.0,
                        "confirmation_duration_seconds": 300,
                    },
                },
            },
        }

    @staticmethod
    def _ready_stage_aligned_observation(run_at: datetime):
        required_themes = sorted(GLOBAL_MARKET_LINKED_THEMES)
        session_date = "2026-07-28"
        partial_coverage = {
            "available": True,
            "full_strategy_coverage": False,
            "required_themes": required_themes,
            "qualified_themes": required_themes[:4],
            "missing_themes": required_themes[4:],
            "themes": {
                theme: {"available": theme in required_themes[:4]}
                for theme in required_themes
            },
        }
        return {
            "schema_version": 6,
            "strategy_id": STRATEGY_ID,
            "checked_at": run_at.isoformat(),
            "status": "ready",
            "required_checks": {
                "cn_open_available": True,
                "us_first_hour_and_close_available": True,
                "nasdaq_futures_continuous_trend_available": True,
                "us_premarket_collection_audited": True,
                "us_close_collection_audited": True,
                "us_theme_session_pair_available": True,
                "japan_market_available": True,
                "korea_continuous_gate_available": True,
                "gold_signal_available": True,
            },
            "missing_requirements": [],
            "evidence": {
                "us_premarket": {
                    **partial_coverage,
                    "latest_capture": {
                        "available": True,
                        "session_date": session_date,
                        "session_stage": "premarket",
                        "universe_size": 91,
                        "collected_component_count": 49,
                        "required_themes": required_themes,
                    },
                },
                "us_close_themes": {
                    **partial_coverage,
                    "qualified_themes": required_themes[:-1],
                    "missing_themes": required_themes[-1:],
                    "collection": {
                        "latest_session_date": session_date,
                        "required_stages_complete": True,
                    },
                },
                "us_theme_collection_audit": {
                    "required_themes": required_themes,
                    "premarket_collection_audited": True,
                    "close_collection_audited": True,
                    "session_pair_available": True,
                    "premarket_session_date": session_date,
                    "close_session_date": session_date,
                },
                "nasdaq_futures": {
                    "code": "NQ00Y",
                    "available": True,
                    "confirmed": True,
                    "confirmation_sample_count": 3,
                    "confirmation_span_seconds": 120.0,
                },
                "korea": {
                    "linked_technology_gate": {
                        "confirmation_span_seconds": 300.0,
                        "confirmation_duration_seconds": 300,
                    },
                },
            },
        }

    def test_synthetic_replay_does_not_automatically_start_forward_campaign(self) -> None:
        evidence = self.service.record_backtest(
            result=self._passing_result(),
            dataset_kind="deterministic_fixture",
            dataset_id="fixture-500",
            data_sources=["unit-test"],
            minute_bar_source="generated",
            request_payload={"frames": [1]},
        )
        with patch(
            "src.services.cross_market_acceptance_service.is_market_open",
            return_value=True,
        ):
            status = self.service.get_status()

        self.assertFalse(evidence["historical_eligible"])
        self.assertFalse(status["historical_ready"])
        self.assertFalse(status["ready"])
        self.assertFalse(status["paper_observation"]["campaign_active"])

    def test_active_legacy_campaign_requires_explicit_reset(self) -> None:
        self.service._write_state({
            "paper_campaign": {
                "schema_version": 2,
                "strategy_id": "cross_market_semiconductor_gold_v1.1",
                "status": "active",
                "started_at": datetime.now(timezone.utc).isoformat(),
                "required_trading_days": 30,
            },
        })

        with self.assertRaisesRegex(
            ValueError,
            "paper_campaign_strategy_mismatch_requires_reset",
        ):
            self.service.start_paper_campaign(reset=False)

    def test_forward_campaign_starts_without_historical_backtest(self) -> None:
        started = self.service.start_paper_campaign()
        repeated = self.service.start_paper_campaign()

        self.assertTrue(started["started"])
        self.assertFalse(repeated["started"])
        self.assertFalse(started["status"]["historical_required"])
        self.assertFalse(started["status"]["historical_ready"])
        self.assertTrue(started["status"]["paper_observation"]["campaign_active"])
        self.assertEqual(
            started["status"]["paper_observation"]["required_trading_days"],
            30,
        )

    def test_legacy_ready_observation_without_full_korea_duration_is_degraded(self) -> None:
        started = self.service.start_paper_campaign()
        started_at = datetime.fromisoformat(started["campaign"]["started_at"])
        shanghai = ZoneInfo("Asia/Shanghai")
        session_date = started_at.astimezone(shanghai).date()
        while True:
            session_date += timedelta(days=1)
            if session_date.weekday() < 5:
                break
        run_at = datetime.combine(session_date, time(9, 35), tzinfo=shanghai)
        legacy_observation = self._ready_observation(run_at)
        legacy_observation["schema_version"] = 1
        legacy_observation["evidence"] = {
            "korea": {
                "linked_technology_gate": {
                    "confirmation_sample_count": 5,
                    "confirmation_span_seconds": 240.0,
                },
            },
        }
        self.repository.runs.append({
            "id": 1,
            "run_uid": "legacy-four-minute-korea-run",
            "created_at": run_at.isoformat(),
            "status": "completed",
            "trigger_source": "vnpy_paper_auto",
            "settings": {"auto_execution_mode": "vnpy_paper"},
            "diagnostics": {
                "cross_market_observation": legacy_observation,
            },
        })

        with patch(
            "src.services.cross_market_acceptance_service.is_market_open",
            return_value=True,
        ):
            observation = self.service.get_status()["paper_observation"]

        self.assertEqual(observation["observed_trading_days"], 1)
        self.assertEqual(observation["fully_evidenced_trading_days"], 0)
        self.assertEqual(observation["degraded_trading_days"], 1)
        self.assertEqual(
            observation["missing_requirement_counts"],
            {
                "korea_continuous_gate_available": 1,
                "nasdaq_futures_continuous_trend_available": 1,
                "us_close_themes_available": 1,
                "us_premarket_themes_available": 1,
            },
        )
        self.assertEqual(
            observation["session_results"][0]["missing_requirements"],
            [
                "korea_continuous_gate_available",
                "nasdaq_futures_continuous_trend_available",
                "us_close_themes_available",
                "us_premarket_themes_available",
            ],
        )

    def test_schema_three_ready_observation_without_nq00y_is_degraded(self) -> None:
        observation = self._ready_observation(datetime.now(timezone.utc))
        observation["schema_version"] = 3
        observation["required_checks"].pop(
            "nasdaq_futures_continuous_trend_available"
        )
        observation["evidence"].pop("nasdaq_futures")

        normalized = self.service._normalize_observation_contract(observation)

        self.assertEqual(normalized["status"], "unavailable")
        self.assertEqual(
            normalized["missing_requirements"],
            [
                "nasdaq_futures_continuous_trend_available",
                "us_close_themes_available",
                "us_premarket_themes_available",
            ],
        )
        self.assertIn(
            "nasdaq_futures_trend_evidence_unproven",
            normalized["contract_reason"],
        )
        self.assertIn(
            "us_premarket_theme_evidence_unproven",
            normalized["contract_reason"],
        )

    def test_schema_five_ready_observation_requires_complete_theme_proof(self) -> None:
        observation = self._ready_observation(datetime.now(timezone.utc))
        observation["evidence"]["us_premarket"]["qualified_themes"].pop()

        normalized = self.service._normalize_observation_contract(observation)

        self.assertEqual(normalized["status"], "unavailable")
        self.assertEqual(
            normalized["missing_requirements"],
            ["us_premarket_themes_available"],
        )
        self.assertIn(
            "us_premarket_theme_evidence_unproven",
            normalized["contract_reason"],
        )

    def test_schema_six_accepts_audited_partial_theme_coverage(self) -> None:
        observation = self._ready_stage_aligned_observation(
            datetime.now(timezone.utc)
        )

        normalized = self.service._normalize_observation_contract(observation)

        self.assertEqual(normalized["status"], "ready")
        self.assertEqual(normalized["missing_requirements"], [])

    def test_schema_six_requires_same_us_session_pair(self) -> None:
        observation = self._ready_stage_aligned_observation(
            datetime.now(timezone.utc)
        )
        observation["evidence"]["us_close_themes"]["collection"][
            "latest_session_date"
        ] = "2026-07-29"
        observation["evidence"]["us_theme_collection_audit"][
            "close_session_date"
        ] = "2026-07-29"

        normalized = self.service._normalize_observation_contract(observation)

        self.assertEqual(normalized["status"], "unavailable")
        self.assertEqual(
            normalized["missing_requirements"],
            ["us_theme_session_pair_available"],
        )
        self.assertIn(
            "us_theme_session_pair_unproven",
            normalized["contract_reason"],
        )

    def test_forward_campaign_binds_account_and_rejects_other_account_runs(self) -> None:
        state_path = Path(self.temp_dir.name) / "account-bound-acceptance.json"
        service = CrossMarketAcceptanceService(
            repository=self.repository,
            state_path=state_path,
            current_account_id=9,
        )
        started = service.start_paper_campaign(initial_equity=100000.0)
        started_at = datetime.fromisoformat(started["campaign"]["started_at"])
        shanghai = ZoneInfo("Asia/Shanghai")
        session_date = started_at.astimezone(shanghai).date()
        while True:
            session_date += timedelta(days=1)
            if session_date.weekday() < 5:
                break
        run_at = datetime.combine(session_date, time(9, 35), tzinfo=shanghai)
        for run_id, account_id in ((1, 9), (2, 10)):
            self.repository.runs.append({
                "id": run_id,
                "created_at": run_at.isoformat(),
                "status": "completed",
                "trigger_source": "vnpy_paper_auto",
                "settings": {
                    "account_id": account_id,
                    "auto_execution_mode": "vnpy_paper",
                },
                "diagnostics": {
                    "cross_market_observation": self._ready_observation(run_at),
                },
            })

        with patch(
            "src.services.cross_market_acceptance_service.is_market_open",
            return_value=True,
        ):
            status = service.get_status()["paper_observation"]

        self.assertEqual(started["campaign"]["account_id"], 9)
        self.assertEqual(started["campaign"]["initial_equity"], 100000.0)
        self.assertTrue(status["account_matches_current"])
        self.assertEqual(status["campaign_account_id"], 9)
        self.assertEqual(status["observed_trading_days"], 1)
        self.assertEqual(status["rejected_run_counts"], {"run_account_mismatch": 1})

        mismatched = CrossMarketAcceptanceService(
            repository=self.repository,
            state_path=state_path,
            current_account_id=10,
        ).get_status()["paper_observation"]
        self.assertFalse(mismatched["account_matches_current"])
        self.assertFalse(mismatched["ready"])

    def test_active_legacy_campaign_can_bind_account_without_reset(self) -> None:
        state_path = Path(self.temp_dir.name) / "legacy-active-acceptance.json"
        legacy = CrossMarketAcceptanceService(
            repository=self.repository,
            state_path=state_path,
        ).start_paper_campaign()
        bound = CrossMarketAcceptanceService(
            repository=self.repository,
            state_path=state_path,
            current_account_id=9,
        ).start_paper_campaign(initial_equity=100000.0)

        self.assertFalse(bound["started"])
        self.assertTrue(bound["account_bound"])
        self.assertEqual(bound["campaign"]["started_at"], legacy["campaign"]["started_at"])
        self.assertEqual(bound["campaign"]["account_id"], 9)
        self.assertEqual(bound["campaign"]["initial_equity"], 100000.0)

    def test_forward_campaign_discards_legacy_backtest_without_resetting_active_start(self) -> None:
        self.service.record_backtest(
            result=self._passing_result(),
            dataset_kind="deterministic_fixture",
            dataset_id="legacy-fixture",
            data_sources=["unit-test"],
            minute_bar_source="generated",
            request_payload={"frames": [1]},
        )

        started = self.service.start_paper_campaign()
        repeated = self.service.start_paper_campaign()

        self.assertTrue(started["historical_evidence_cleared"])
        self.assertFalse(repeated["historical_evidence_cleared"])
        self.assertEqual(
            repeated["campaign"]["started_at"],
            started["campaign"]["started_at"],
        )
        self.assertEqual(
            repeated["status"]["historical_backtest"]["reason"],
            "historical_backtest_not_required",
        )

    def test_unhashed_historical_claim_cannot_start_paper_observation(self) -> None:
        evidence = self.service.record_backtest(
            result=self._passing_result(),
            dataset_kind="historical_market",
            dataset_id="unverified-upload",
            data_sources=["claimed-vendor"],
            minute_bar_source="claimed-1m",
            request_payload={"frame_count": 500},
        )

        self.assertFalse(evidence["provenance_verified"])
        self.assertFalse(evidence["historical_eligible"])

    def test_hashes_without_verified_source_artifacts_are_not_real_history(self) -> None:
        evidence = self.service.record_backtest(
            result=self._passing_result(),
            dataset_kind="historical_market",
            dataset_id="weak-hash-only-claim",
            data_sources=["claimed-vendor"],
            minute_bar_source="claimed-1m",
            request_payload={
                "manifest_sha256": "a" * 64,
                "frames_sha256": "b" * 64,
                "frame_count": 500,
            },
        )

        self.assertFalse(evidence["provenance_verified"])
        self.assertFalse(evidence["historical_eligible"])

    def test_legacy_passed_result_without_scope_coverage_is_not_historical(self) -> None:
        result = self._passing_result()
        result["validation"] = {"passed": True}

        evidence = self.service.record_backtest(
            result=result,
            dataset_kind="historical_market",
            dataset_id="legacy-incomplete-coverage",
            data_sources=["vendor"],
            minute_bar_source="vendor-1m",
            request_payload=self._verified_request_payload(),
        )

        self.assertFalse(evidence["historical_eligible"])
        self.assertFalse(self.service.get_status()["historical_ready"])

    def test_forward_campaign_counts_30_distinct_subsequent_trading_days(self) -> None:
        evidence = self.service.record_backtest(
            result=self._passing_result(),
            dataset_kind="historical_market",
            dataset_id="vendor-2024-2026",
            data_sources=["vendor-daily", "vendor-cross-market"],
            minute_bar_source="vendor-cn-1m",
            request_payload=self._verified_request_payload(),
        )
        self.assertTrue(evidence["historical_eligible"])
        campaign = self.service.start_paper_campaign()
        recorded_at = datetime.fromisoformat(campaign["campaign"]["started_at"])
        shanghai = ZoneInfo("Asia/Shanghai")
        current = recorded_at.astimezone(shanghai).date() + timedelta(days=1)
        while len(self.repository.runs) < 30:
            if current.weekday() < 5:
                run_at = datetime.combine(current, time(9, 35), tzinfo=shanghai)
                self.repository.runs.append({
                    "created_at": run_at.isoformat(),
                    "status": "completed",
                    "trigger_source": "vnpy_paper_auto",
                    "settings": {"auto_execution_mode": "vnpy_paper"},
                    "diagnostics": {
                        "cross_market_observation": self._ready_observation(run_at),
                    },
                })
            current += timedelta(days=1)
        post_completion_at = datetime.combine(
            current,
            time(9, 35),
            tzinfo=shanghai,
        )
        self.repository.runs.append({
            "created_at": post_completion_at.isoformat(),
            "status": "completed",
            "trigger_source": "cross_market_paper_observation",
            "settings": {"auto_execution_mode": "dry_run"},
            "diagnostics": {
                "cross_market_observation": self._ready_observation(
                    post_completion_at
                ),
            },
        })
        rejected_at = datetime.combine(
            recorded_at.astimezone(shanghai).date() + timedelta(days=1),
            time(9, 40),
            tzinfo=shanghai,
        )
        self.repository.runs.append({
            "created_at": rejected_at.isoformat(),
            "status": "completed",
            "settings": {"auto_execution_mode": "paper"},
            "diagnostics": {
                "cross_market_observation": self._ready_observation(rejected_at),
            },
        })

        with patch(
            "src.services.cross_market_acceptance_service.is_market_open",
            return_value=True,
        ):
            status = self.service.get_status()

        self.assertFalse(status["historical_ready"])
        self.assertEqual(
            status["historical_backtest"]["reason"],
            "historical_backtest_not_required",
        )
        self.assertEqual(status["paper_observation"]["observed_trading_days"], 30)
        self.assertEqual(
            status["paper_observation"]["fully_evidenced_trading_days"],
            30,
        )
        self.assertEqual(status["paper_observation"]["degraded_trading_days"], 0)
        self.assertEqual(status["paper_observation"]["data_completeness_pct"], 100.0)
        self.assertEqual(
            status["paper_observation"]["formal_execution_trading_days"],
            30,
        )
        self.assertEqual(
            status["paper_observation"]["evidence_only_trading_days"],
            0,
        )
        self.assertEqual(
            status["paper_observation"][
                "fully_evidenced_without_formal_execution_days"
            ],
            0,
        )
        self.assertEqual(
            status["paper_observation"]["qualified_paper_trading_days"],
            30,
        )
        self.assertEqual(status["paper_observation"]["remaining_trading_days"], 0)
        self.assertEqual(
            status["paper_observation"]["completion_basis"],
            "fully_evidenced_formal_vnpy_paper_run",
        )
        self.assertEqual(
            status["paper_observation"]["qualified_trigger_sources"],
            ["vnpy_paper_auto"],
        )
        self.assertEqual(
            status["paper_observation"]["qualified_execution_modes"],
            ["vnpy_paper"],
        )
        self.assertEqual(
            status["paper_observation"]["observation_trigger_sources"],
            ["cross_market_paper_observation", "vnpy_paper_auto"],
        )
        self.assertEqual(
            status["paper_observation"]["observation_execution_modes"],
            ["dry_run", "vnpy_paper"],
        )
        session_results = status["paper_observation"]["session_results"]
        self.assertEqual(len(session_results), 30)
        self.assertTrue(all(item["fully_evidenced"] for item in session_results))
        self.assertTrue(all(item["evidence_status"] == "ready" for item in session_results))
        self.assertEqual(session_results[0]["session_date"], status["paper_observation"]["first_observation_date"])
        self.assertEqual(session_results[-1]["session_date"], status["paper_observation"]["latest_observation_date"])
        self.assertEqual(
            status["paper_observation"]["completion_session_date"],
            session_results[-1]["session_date"],
        )
        self.assertIsNotNone(status["paper_observation"]["completed_at"])
        self.assertEqual(
            status["paper_observation"]["post_completion_observation_days"],
            1,
        )
        self.assertEqual(
            status["paper_observation"]["rejected_run_counts"]["execution_mode_not_qualified"],
            1,
        )
        self.assertTrue(status["ready"])

    def test_ready_dry_runs_do_not_advance_paper_trading_completion(self) -> None:
        campaign = self.service.start_paper_campaign()
        shanghai = ZoneInfo("Asia/Shanghai")
        current = (
            datetime.fromisoformat(campaign["campaign"]["started_at"])
            .astimezone(shanghai)
            .date()
            + timedelta(days=1)
        )
        while len(self.repository.runs) < 30:
            if current.weekday() < 5:
                run_at = datetime.combine(current, time(9, 40), tzinfo=shanghai)
                self.repository.runs.append({
                    "created_at": run_at.isoformat(),
                    "status": "completed",
                    "trigger_source": "cross_market_paper_observation",
                    "settings": {"auto_execution_mode": "dry_run"},
                    "diagnostics": {
                        "cross_market_observation": self._ready_observation(run_at),
                    },
                })
            current += timedelta(days=1)

        with patch(
            "src.services.cross_market_acceptance_service.is_market_open",
            return_value=True,
        ):
            status = self.service.get_status()

        observation = status["paper_observation"]
        self.assertEqual(observation["observed_trading_days"], 30)
        self.assertEqual(observation["fully_evidenced_trading_days"], 30)
        self.assertEqual(observation["qualified_paper_trading_days"], 0)
        self.assertEqual(observation["remaining_trading_days"], 30)
        self.assertIsNone(observation["completion_session_date"])
        self.assertFalse(observation["ready"])
        self.assertFalse(status["ready"])
        self.assertTrue(
            all(
                item["qualifies_for_campaign"] is False
                for item in observation["session_results"]
            )
        )

    def test_intraday_entry_runs_do_not_advance_or_observe_campaign_days(
        self,
    ) -> None:
        campaign = self.service.start_paper_campaign()
        shanghai = ZoneInfo("Asia/Shanghai")
        run_date = (
            datetime.fromisoformat(campaign["campaign"]["started_at"])
            .astimezone(shanghai)
            .date()
            + timedelta(days=1)
        )
        while run_date.weekday() >= 5:
            run_date += timedelta(days=1)
        run_at = datetime.combine(run_date, time(10, 40), tzinfo=shanghai)
        self.repository.runs.append({
            "created_at": run_at.isoformat(),
            "status": "completed",
            "trigger_source": "cross_market_intraday_entry_scan",
            "settings": {"auto_execution_mode": "vnpy_paper"},
            "candidate_count": 2,
            "planned_count": 2,
            "submitted_count": 2,
            "diagnostics": {
                "cross_market_observation": self._ready_observation(run_at),
            },
        })

        with patch(
            "src.services.cross_market_acceptance_service.is_market_open",
            return_value=True,
        ):
            observation = self.service.get_status()["paper_observation"]

        self.assertEqual(observation["observed_trading_days"], 0)
        self.assertEqual(observation["qualified_paper_trading_days"], 0)
        self.assertEqual(observation["remaining_trading_days"], 30)
        self.assertEqual(observation["session_results"], [])

    def test_partial_formal_run_cannot_advance_paper_trading_completion(self) -> None:
        campaign = self.service.start_paper_campaign()
        shanghai = ZoneInfo("Asia/Shanghai")
        run_date = (
            datetime.fromisoformat(campaign["campaign"]["started_at"])
            .astimezone(shanghai)
            .date()
            + timedelta(days=1)
        )
        while run_date.weekday() >= 5:
            run_date += timedelta(days=1)
        run_at = datetime.combine(run_date, time(9, 35), tzinfo=shanghai)
        self.repository.runs.append({
            "created_at": run_at.isoformat(),
            "status": "partial",
            "trigger_source": "vnpy_paper_auto",
            "settings": {
                "account_id": campaign["campaign"].get("account_id"),
                "auto_execution_mode": "vnpy_paper",
            },
            "diagnostics": {
                "cross_market_observation": self._ready_observation(run_at),
            },
        })

        with patch(
            "src.services.cross_market_acceptance_service.is_market_open",
            return_value=True,
        ):
            observation = self.service.get_status()["paper_observation"]

        self.assertEqual(observation["observed_trading_days"], 0)
        self.assertEqual(observation["qualified_paper_trading_days"], 0)
        self.assertEqual(observation["remaining_trading_days"], 30)
        self.assertEqual(
            observation["rejected_run_counts"],
            {"run_not_completed": 1},
        )

    def test_failed_closed_session_counts_as_degraded_observation_day(self) -> None:
        self.service.record_backtest(
            result=self._passing_result(),
            dataset_kind="historical_market",
            dataset_id="vendor-observation-gate",
            data_sources=["vendor"],
            minute_bar_source="vendor-1m",
            request_payload=self._verified_request_payload(),
        )
        campaign = self.service.start_paper_campaign()
        shanghai = ZoneInfo("Asia/Shanghai")
        first_date = datetime.fromisoformat(campaign["campaign"]["started_at"]).astimezone(shanghai).date()
        while True:
            first_date += timedelta(days=1)
            if first_date.weekday() < 5:
                break
        in_session = datetime.combine(first_date, time(9, 35), tzinfo=shanghai)
        outside_session = datetime.combine(first_date + timedelta(days=1), time(18, 0), tzinfo=shanghai)
        self.repository.runs.extend([
            {
                "created_at": in_session.isoformat(),
                "status": "completed",
                "settings": {"auto_execution_mode": "dry_run"},
                "diagnostics": {
                    "cross_market_observation": {
                        **self._ready_observation(in_session),
                        "status": "unavailable",
                        "missing_requirements": ["gold_signal_available"],
                    },
                },
            },
            {
                "created_at": outside_session.isoformat(),
                "status": "completed",
                "settings": {"auto_execution_mode": "vnpy_paper"},
                "diagnostics": {
                    "cross_market_observation": self._ready_observation(outside_session),
                },
            },
        ])

        with patch(
            "src.services.cross_market_acceptance_service.is_market_open",
            return_value=True,
        ):
            status = self.service.get_status()

        observation = status["paper_observation"]
        self.assertEqual(observation["observed_trading_days"], 1)
        self.assertEqual(observation["fully_evidenced_trading_days"], 0)
        self.assertEqual(observation["degraded_trading_days"], 1)
        self.assertEqual(observation["data_completeness_pct"], 0.0)
        self.assertEqual(
            observation["missing_requirement_counts"],
            {"gold_signal_available": 1},
        )
        self.assertEqual(len(observation["session_results"]), 1)
        self.assertEqual(
            observation["session_results"][0]["evidence_status"],
            "degraded",
        )
        self.assertEqual(
            observation["session_results"][0]["missing_requirements"],
            ["gold_signal_available"],
        )
        self.assertEqual(
            observation["rejected_run_counts"],
            {"run_outside_cn_observation_session": 1},
        )

    def test_degraded_day_does_not_satisfy_thirty_day_completion_gate(self) -> None:
        campaign = self.service.start_paper_campaign()
        shanghai = ZoneInfo("Asia/Shanghai")
        current = (
            datetime.fromisoformat(campaign["campaign"]["started_at"])
            .astimezone(shanghai)
            .date()
            + timedelta(days=1)
        )
        while len(self.repository.runs) < 30:
            if current.weekday() < 5:
                run_at = datetime.combine(current, time(9, 35), tzinfo=shanghai)
                observation = self._ready_observation(run_at)
                if not self.repository.runs:
                    observation = {
                        **observation,
                        "status": "unavailable",
                        "missing_requirements": ["gold_signal_available"],
                    }
                self.repository.runs.append({
                    "created_at": run_at.isoformat(),
                    "status": "completed",
                    "trigger_source": "vnpy_paper_auto",
                    "settings": {"auto_execution_mode": "vnpy_paper"},
                    "diagnostics": {"cross_market_observation": observation},
                })
            current += timedelta(days=1)

        with patch(
            "src.services.cross_market_acceptance_service.is_market_open",
            return_value=True,
        ):
            status = self.service.get_status()

        observation = status["paper_observation"]
        self.assertEqual(observation["observed_trading_days"], 30)
        self.assertEqual(observation["fully_evidenced_trading_days"], 29)
        self.assertEqual(observation["degraded_trading_days"], 1)
        self.assertEqual(observation["remaining_trading_days"], 1)
        self.assertFalse(observation["ready"])
        self.assertFalse(status["ready"])

    def test_ready_rerun_upgrades_degraded_observation_for_same_session(self) -> None:
        campaign = self.service.start_paper_campaign()
        shanghai = ZoneInfo("Asia/Shanghai")
        session_date = datetime.fromisoformat(
            campaign["campaign"]["started_at"]
        ).astimezone(shanghai).date()
        while True:
            session_date += timedelta(days=1)
            if session_date.weekday() < 5:
                break
        first_run = datetime.combine(session_date, time(9, 35), tzinfo=shanghai)
        second_run = datetime.combine(session_date, time(9, 37), tzinfo=shanghai)
        self.repository.runs.extend([
            {
                "id": 1,
                "created_at": first_run.isoformat(),
                "status": "completed",
                "trigger_source": "vnpy_paper_auto",
                "candidate_count": 3,
                "planned_count": 1,
                "submitted_count": 1,
                "skipped_count": 2,
                "settings": {"auto_execution_mode": "vnpy_paper"},
                "diagnostics": {
                    "cross_market_observation": {
                        **self._ready_observation(first_run),
                        "status": "unavailable",
                        "missing_requirements": ["gold_signal_available"],
                    },
                },
            },
            {
                "id": 2,
                "created_at": second_run.isoformat(),
                "status": "completed",
                "trigger_source": "vnpy_paper_auto",
                "settings": {"auto_execution_mode": "vnpy_paper"},
                "diagnostics": {
                    "formal_recovery": True,
                    "intraday_entry_recheck": True,
                    "cross_market_observation": self._ready_observation(second_run),
                },
            },
        ])

        with patch(
            "src.services.cross_market_acceptance_service.is_market_open",
            return_value=True,
        ):
            observation = self.service.get_status()["paper_observation"]

        self.assertEqual(observation["observed_trading_days"], 1)
        self.assertEqual(observation["fully_evidenced_trading_days"], 1)
        self.assertEqual(observation["qualified_paper_trading_days"], 1)
        self.assertEqual(observation["remaining_trading_days"], 29)
        self.assertFalse(observation["ready"])
        self.assertEqual(observation["degraded_trading_days"], 0)
        self.assertEqual(observation["missing_requirement_counts"], {})
        self.assertEqual(observation["session_results"][0]["run_id"], 2)
        self.assertEqual(observation["session_results"][0]["evidence_status"], "ready")
        self.assertTrue(
            observation["session_results"][0]["formal_execution"]["formal_recovery"]
        )

    def test_late_unmarked_formal_run_cannot_advance_campaign(self) -> None:
        campaign = self.service.start_paper_campaign()
        shanghai = ZoneInfo("Asia/Shanghai")
        session_date = datetime.fromisoformat(
            campaign["campaign"]["started_at"]
        ).astimezone(shanghai).date()
        while True:
            session_date += timedelta(days=1)
            if session_date.weekday() < 5:
                break
        run_at = datetime.combine(session_date, time(10, 58), tzinfo=shanghai)
        self.repository.runs.append({
            "id": 1,
            "run_uid": "late-unmarked-formal",
            "created_at": run_at.isoformat(),
            "status": "completed",
            "trigger_source": "vnpy_paper_auto",
            "settings": {"auto_execution_mode": "vnpy_paper"},
            "diagnostics": {
                "cross_market_entry_phase": "intraday_dip",
                "cross_market_observation": self._ready_observation(run_at),
            },
        })

        with patch(
            "src.services.cross_market_acceptance_service.is_market_open",
            return_value=True,
        ):
            observation = self.service.get_status()["paper_observation"]

        self.assertEqual(observation["observed_trading_days"], 1)
        self.assertEqual(observation["fully_evidenced_trading_days"], 1)
        self.assertEqual(observation["formal_execution_trading_days"], 0)
        self.assertEqual(observation["qualified_paper_trading_days"], 0)
        self.assertEqual(
            observation["formal_execution_rejected_counts"],
            {"formal_entry_outside_opening_window": 1},
        )
        self.assertFalse(observation["session_results"][0]["qualifies_for_campaign"])

    def test_formal_entry_uses_persisted_start_time_before_run_creation(self) -> None:
        campaign = self.service.start_paper_campaign()
        shanghai = ZoneInfo("Asia/Shanghai")
        session_date = datetime.fromisoformat(
            campaign["campaign"]["started_at"]
        ).astimezone(shanghai).date()
        while True:
            session_date += timedelta(days=1)
            if session_date.weekday() < 5:
                break
        started_at = datetime.combine(session_date, time(9, 30), tzinfo=shanghai)
        created_at = datetime.combine(session_date, time(9, 33), tzinfo=shanghai)
        self.repository.runs.append({
            "id": 1,
            "run_uid": "slow-opening-formal",
            "created_at": created_at.isoformat(),
            "status": "completed",
            "trigger_source": "vnpy_paper_auto",
            "settings": {"auto_execution_mode": "vnpy_paper"},
            "diagnostics": {
                "formal_entry_started_at": started_at.isoformat(),
                "analysis_slot": "09:30",
                "cross_market_observation": self._ready_observation(created_at),
            },
        })

        with patch(
            "src.services.cross_market_acceptance_service.is_market_open",
            return_value=True,
        ):
            observation = self.service.get_status()["paper_observation"]

        self.assertEqual(observation["formal_execution_trading_days"], 1)
        self.assertEqual(observation["qualified_paper_trading_days"], 1)
        self.assertEqual(observation["formal_execution_rejected_counts"], {})

    def test_legacy_0935_baseline_remains_eligible_after_window_migration(self) -> None:
        campaign = self.service.start_paper_campaign()
        shanghai = ZoneInfo("Asia/Shanghai")
        session_date = datetime.fromisoformat(
            campaign["campaign"]["started_at"]
        ).astimezone(shanghai).date()
        while True:
            session_date += timedelta(days=1)
            if session_date.weekday() < 5:
                break
        started_at = datetime.combine(session_date, time(9, 35), tzinfo=shanghai)
        created_at = datetime.combine(session_date, time(9, 38), tzinfo=shanghai)
        self.repository.runs.append({
            "id": 1,
            "run_uid": "legacy-0935-formal",
            "created_at": created_at.isoformat(),
            "status": "completed",
            "trigger_source": "vnpy_paper_auto",
            "settings": {"auto_execution_mode": "vnpy_paper"},
            "diagnostics": {
                "formal_entry_started_at": started_at.isoformat(),
                "analysis_slot": "09:35",
                "cross_market_observation": self._ready_observation(created_at),
            },
        })

        with patch(
            "src.services.cross_market_acceptance_service.is_market_open",
            return_value=True,
        ):
            observation = self.service.get_status()["paper_observation"]

        self.assertEqual(observation["formal_execution_trading_days"], 1)
        self.assertEqual(observation["qualified_paper_trading_days"], 1)
        self.assertEqual(observation["formal_execution_rejected_counts"], {})

    def test_formal_recovery_requires_opening_baseline_for_acceptance(self) -> None:
        campaign = self.service.start_paper_campaign()
        shanghai = ZoneInfo("Asia/Shanghai")
        session_date = datetime.fromisoformat(
            campaign["campaign"]["started_at"]
        ).astimezone(shanghai).date()
        while True:
            session_date += timedelta(days=1)
            if session_date.weekday() < 5:
                break
        run_at = datetime.combine(session_date, time(10, 40), tzinfo=shanghai)
        self.repository.runs.append({
            "id": 1,
            "run_uid": "orphan-formal-recovery",
            "created_at": run_at.isoformat(),
            "status": "completed",
            "trigger_source": "vnpy_paper_auto",
            "settings": {"auto_execution_mode": "vnpy_paper"},
            "diagnostics": {
                "formal_recovery": True,
                "intraday_entry_recheck": True,
                "cross_market_observation": self._ready_observation(run_at),
            },
        })

        with patch(
            "src.services.cross_market_acceptance_service.is_market_open",
            return_value=True,
        ):
            observation = self.service.get_status()["paper_observation"]

        self.assertEqual(observation["formal_execution_trading_days"], 0)
        self.assertEqual(observation["qualified_paper_trading_days"], 0)
        self.assertEqual(
            observation["formal_execution_rejected_counts"],
            {"formal_recovery_without_opening_baseline": 1},
        )

    def test_completed_recovery_can_follow_failed_opening_baseline(self) -> None:
        campaign = self.service.start_paper_campaign()
        shanghai = ZoneInfo("Asia/Shanghai")
        session_date = datetime.fromisoformat(
            campaign["campaign"]["started_at"]
        ).astimezone(shanghai).date()
        while True:
            session_date += timedelta(days=1)
            if session_date.weekday() < 5:
                break
        baseline_at = datetime.combine(session_date, time(9, 30), tzinfo=shanghai)
        recovery_at = datetime.combine(session_date, time(10, 40), tzinfo=shanghai)
        self.repository.runs.extend([
            {
                "id": 1,
                "run_uid": "failed-opening-baseline",
                "created_at": baseline_at.isoformat(),
                "status": "failed",
                "trigger_source": "vnpy_paper_auto",
                "settings": {"auto_execution_mode": "vnpy_paper"},
                "diagnostics": {
                    "formal_entry_started_at": baseline_at.isoformat(),
                    "analysis_slot": "09:30",
                },
            },
            {
                "id": 2,
                "run_uid": "completed-formal-recovery",
                "created_at": recovery_at.isoformat(),
                "status": "completed",
                "trigger_source": "vnpy_paper_auto",
                "settings": {"auto_execution_mode": "vnpy_paper"},
                "diagnostics": {
                    "formal_recovery": True,
                    "intraday_entry_recheck": True,
                    "cross_market_observation": self._ready_observation(
                        recovery_at
                    ),
                },
            },
        ])

        with patch(
            "src.services.cross_market_acceptance_service.is_market_open",
            return_value=True,
        ):
            observation = self.service.get_status()["paper_observation"]

        self.assertEqual(observation["qualified_paper_trading_days"], 1)
        self.assertEqual(observation["rejected_run_counts"], {"run_not_completed": 1})
        self.assertEqual(observation["formal_execution_rejected_counts"], {})
        formal = observation["session_results"][0]["formal_execution"]
        self.assertEqual(formal["run_uid"], "completed-formal-recovery")
        self.assertTrue(formal["formal_recovery"])

    def test_ready_formal_run_is_preferred_over_newer_dry_run(self) -> None:
        campaign = self.service.start_paper_campaign()
        shanghai = ZoneInfo("Asia/Shanghai")
        session_date = datetime.fromisoformat(
            campaign["campaign"]["started_at"]
        ).astimezone(shanghai).date()
        while True:
            session_date += timedelta(days=1)
            if session_date.weekday() < 5:
                break
        formal_at = datetime.combine(session_date, time(9, 35), tzinfo=shanghai)
        observation_at = datetime.combine(session_date, time(9, 40), tzinfo=shanghai)
        self.repository.runs.extend([
            {
                "id": 1,
                "run_uid": "formal-ready",
                "created_at": formal_at.isoformat(),
                "status": "completed",
                "trigger_source": "vnpy_paper_auto",
                "candidate_count": 3,
                "planned_count": 1,
                "submitted_count": 1,
                "skipped_count": 2,
                "settings": {"auto_execution_mode": "vnpy_paper"},
                "diagnostics": {
                    "cross_market_observation": self._ready_observation(formal_at),
                },
            },
            {
                "id": 2,
                "run_uid": "observation-ready",
                "created_at": observation_at.isoformat(),
                "status": "completed",
                "trigger_source": "cross_market_paper_observation",
                "settings": {"auto_execution_mode": "dry_run"},
                "diagnostics": {
                    "cross_market_observation": self._ready_observation(
                        observation_at
                    ),
                },
            },
        ])

        with patch(
            "src.services.cross_market_acceptance_service.is_market_open",
            return_value=True,
        ):
            observation = self.service.get_status()["paper_observation"]
            session = observation["session_results"][0]

        self.assertEqual(session["run_id"], 1)
        self.assertEqual(session["run_uid"], "formal-ready")
        self.assertEqual(session["execution_mode"], "vnpy_paper")
        self.assertEqual(
            observation["fully_evidenced_formal_execution_days"],
            1,
        )
        self.assertEqual(observation["qualified_paper_trading_days"], 1)
        self.assertTrue(session["qualifies_for_campaign"])
        self.assertTrue(session["formal_execution"]["observed"])
        self.assertTrue(session["formal_execution"]["fully_evidenced"])
        self.assertEqual(session["formal_execution"]["run_uid"], "formal-ready")
        self.assertEqual(session["formal_execution"]["execution_mode"], "vnpy_paper")
        self.assertEqual(session["formal_execution"]["candidate_count"], 3)
        self.assertEqual(session["formal_execution"]["planned_count"], 1)
        self.assertEqual(session["formal_execution"]["submitted_count"], 1)
        self.assertEqual(session["formal_execution"]["skipped_count"], 2)

    def test_full_schema_four_observation_beats_legacy_formal_run(self) -> None:
        campaign = self.service.start_paper_campaign()
        shanghai = ZoneInfo("Asia/Shanghai")
        session_date = datetime.fromisoformat(
            campaign["campaign"]["started_at"]
        ).astimezone(shanghai).date()
        while True:
            session_date += timedelta(days=1)
            if session_date.weekday() < 5:
                break
        formal_at = datetime.combine(session_date, time(9, 35), tzinfo=shanghai)
        observation_at = datetime.combine(session_date, time(9, 40), tzinfo=shanghai)
        legacy_formal = self._ready_observation(formal_at)
        legacy_formal["schema_version"] = 1
        legacy_formal["evidence"] = {
            "korea": {
                "linked_technology_gate": {
                    "confirmation_sample_count": 5,
                    "confirmation_span_seconds": 240.0,
                },
            },
        }
        self.repository.runs.extend([
            {
                "id": 1,
                "run_uid": "formal-legacy-four-minute",
                "created_at": formal_at.isoformat(),
                "status": "completed",
                "trigger_source": "vnpy_paper_auto",
                "candidate_count": 2,
                "planned_count": 0,
                "submitted_count": 0,
                "skipped_count": 2,
                "settings": {"auto_execution_mode": "vnpy_paper"},
                "diagnostics": {"cross_market_observation": legacy_formal},
            },
            {
                "id": 2,
                "run_uid": "observation-schema-four-ready",
                "created_at": observation_at.isoformat(),
                "status": "completed",
                "trigger_source": "cross_market_paper_observation",
                "settings": {"auto_execution_mode": "dry_run"},
                "diagnostics": {
                    "cross_market_observation": self._ready_observation(
                        observation_at
                    ),
                },
            },
        ])

        with patch(
            "src.services.cross_market_acceptance_service.is_market_open",
            return_value=True,
        ):
            observation = self.service.get_status()["paper_observation"]

        self.assertEqual(observation["fully_evidenced_trading_days"], 1)
        self.assertEqual(observation["degraded_trading_days"], 0)
        session = observation["session_results"][0]
        self.assertEqual(session["run_id"], 2)
        self.assertEqual(session["run_uid"], "observation-schema-four-ready")
        self.assertEqual(session["execution_mode"], "dry_run")
        self.assertFalse(session["qualifies_for_campaign"])
        self.assertEqual(observation["formal_execution_trading_days"], 1)
        self.assertEqual(observation["fully_evidenced_formal_execution_days"], 0)
        self.assertEqual(observation["qualified_paper_trading_days"], 0)
        self.assertEqual(observation["remaining_trading_days"], 30)
        self.assertFalse(observation["ready"])
        self.assertEqual(
            observation["fully_evidenced_via_later_observation_days"],
            1,
        )
        self.assertEqual(
            observation["fully_evidenced_via_later_observation_dates"],
            [session_date.isoformat()],
        )
        self.assertTrue(session["formal_execution"]["observed"])
        self.assertFalse(session["formal_execution"]["fully_evidenced"])
        self.assertEqual(
            session["formal_execution"]["run_uid"],
            "formal-legacy-four-minute",
        )
        self.assertIn(
            "korea_continuous_gate_available",
            session["formal_execution"]["missing_requirements"],
        )
        self.assertEqual(session["formal_execution"]["run_status"], "completed")
        self.assertEqual(
            session["formal_execution"]["trigger_source"],
            "vnpy_paper_auto",
        )
        self.assertEqual(session["formal_execution"]["execution_mode"], "vnpy_paper")
        self.assertEqual(session["formal_execution"]["candidate_count"], 2)
        self.assertEqual(session["formal_execution"]["planned_count"], 0)
        self.assertEqual(session["formal_execution"]["submitted_count"], 0)
        self.assertEqual(session["formal_execution"]["skipped_count"], 2)

    def test_degraded_day_uses_the_most_complete_observation(self) -> None:
        campaign = self.service.start_paper_campaign()
        shanghai = ZoneInfo("Asia/Shanghai")
        session_date = datetime.fromisoformat(
            campaign["campaign"]["started_at"]
        ).astimezone(shanghai).date()
        while True:
            session_date += timedelta(days=1)
            if session_date.weekday() < 5:
                break
        first_run = datetime.combine(session_date, time(9, 35), tzinfo=shanghai)
        second_run = datetime.combine(session_date, time(9, 40), tzinfo=shanghai)
        self.repository.runs.extend([
            {
                "id": 1,
                "created_at": first_run.isoformat(),
                "status": "completed",
                "trigger_source": "vnpy_paper_auto",
                "settings": {"auto_execution_mode": "vnpy_paper"},
                "diagnostics": {
                    "cross_market_observation": {
                        **self._ready_observation(first_run),
                        "status": "unavailable",
                        "missing_requirements": [
                            "gold_signal_available",
                            "korea_continuous_gate_available",
                        ],
                    },
                },
            },
            {
                "id": 2,
                "created_at": second_run.isoformat(),
                "status": "completed",
                "trigger_source": "vnpy_paper_auto",
                "settings": {"auto_execution_mode": "vnpy_paper"},
                "diagnostics": {
                    "cross_market_observation": {
                        **self._ready_observation(second_run),
                        "status": "unavailable",
                        "missing_requirements": ["gold_signal_available"],
                    },
                },
            },
        ])

        with patch(
            "src.services.cross_market_acceptance_service.is_market_open",
            return_value=True,
        ):
            observation = self.service.get_status()["paper_observation"]

        self.assertEqual(observation["observed_trading_days"], 1)
        self.assertEqual(observation["degraded_trading_days"], 1)
        self.assertEqual(
            observation["missing_requirement_counts"],
            {"gold_signal_available": 1},
        )

    def test_synthetic_result_does_not_overwrite_real_history_evidence(self) -> None:
        real = self.service.record_backtest(
            result=self._passing_result(),
            dataset_kind="historical_market",
            dataset_id="real-history",
            data_sources=["vendor"],
            minute_bar_source="vendor-1m",
            request_payload=self._verified_request_payload(),
        )
        synthetic = self.service.record_backtest(
            result=self._passing_result(),
            dataset_kind="deterministic_fixture",
            dataset_id="fixture",
            data_sources=["unit-test"],
            minute_bar_source="generated",
            request_payload={"frames": [2]},
        )

        self.assertTrue(real["persisted"])
        self.assertFalse(synthetic["persisted"])
        self.assertTrue(synthetic["preserved_prior_real_history"])
        self.assertEqual(self.service.get_status()["historical_backtest"]["dataset_id"], "real-history")

    def test_backtest_record_preserves_active_forward_campaign(self) -> None:
        campaign = self.service.start_paper_campaign()
        self.service.record_backtest(
            result=self._passing_result(),
            dataset_kind="deterministic_fixture",
            dataset_id="fixture",
            data_sources=["unit-test"],
            minute_bar_source="generated",
            request_payload={"frames": [1]},
        )

        status = self.service.get_status()
        self.assertTrue(status["paper_observation"]["campaign_active"])
        self.assertEqual(
            status["paper_observation"]["campaign_started_at"],
            datetime.fromisoformat(campaign["campaign"]["started_at"])
            .astimezone(ZoneInfo("UTC"))
            .isoformat(),
        )


if __name__ == "__main__":
    unittest.main()
