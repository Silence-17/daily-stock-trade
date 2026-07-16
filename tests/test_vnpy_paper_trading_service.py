# -*- coding: utf-8 -*-
"""Tests for vn.py-style local paper trading service."""

from __future__ import annotations

import json
import importlib.util
import os
import sys
import tempfile
import time
import types
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from sqlalchemy import text

from src.config import Config
from src.notification import ChannelAttemptResult, NotificationDispatchResult
from src.services.alert_service import AlertService
from src.services.decision_signal_service import DecisionSignalService
from src.services.vnpy_paper_trading_service import (
    LLM_DYNAMIC_AGENT_PLAN_EVALUATOR_VERSION,
    LLM_DYNAMIC_AGENT_PLAN_PROMPT_VERSION,
    LLM_PRE_TRADE_REVIEW_EVALUATOR_VERSION,
    LLM_PRE_TRADE_REVIEW_PROMPT_VERSION,
    VnpyPaperTradingService,
    _resolve_auto_alphasift_llm_policy,
    build_vnpy_paper_trading_background_tasks,
)
from src.storage import DatabaseManager, StockDaily, StockSelectionAgentTradePlan


class _FakeDataFetcherManager:
    def __init__(self, price: float = 10.0, boards_by_symbol=None) -> None:
        self.price = price
        self.boards_by_symbol = boards_by_symbol or {}

    def get_realtime_quote(self, symbol: str):
        return SimpleNamespace(price=self.price, provider="unit-test")

    def get_belong_boards(self, symbol: str):
        return self.boards_by_symbol.get(symbol, [])


class VnpyPaperTradingServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        self.env_path = self.data_dir / ".env"
        self.db_path = self.data_dir / "vnpy_paper_test.db"
        self.config_path = self.data_dir / "vnpy_paper.json"
        self.env_path.write_text(
            "\n".join(
                [
                    "STOCK_LIST=600519",
                    "GEMINI_API_KEY=test",
                    "ADMIN_AUTH_ENABLED=false",
                    "ENABLE_REALTIME_QUOTE=false",
                    f"DATABASE_PATH={self.db_path}",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        os.environ["ENV_FILE"] = str(self.env_path)
        os.environ["DATABASE_PATH"] = str(self.db_path)
        Config.reset_instance()
        DatabaseManager.reset_instance()
        self.service = VnpyPaperTradingService(
            data_fetcher_manager=_FakeDataFetcherManager(price=10.0),
            config_path=self.config_path,
        )

    def tearDown(self) -> None:
        DatabaseManager.reset_instance()
        Config.reset_instance()
        os.environ.pop("ENV_FILE", None)
        os.environ.pop("DATABASE_PATH", None)
        self.temp_dir.cleanup()

    def test_auto_alphasift_llm_policy_allows_bounded_environment_override(self) -> None:
        with patch.dict(
            os.environ,
            {
                "VNPY_AUTO_ALPHASIFT_LLM_TIMEOUT_SEC": "12",
                "VNPY_AUTO_ALPHASIFT_LLM_MAX_RETRIES": "3",
                "VNPY_AUTO_ALPHASIFT_LLM_FAILURE_THRESHOLD": "2",
                "VNPY_AUTO_ALPHASIFT_LLM_COOLDOWN_MINUTES": "15",
                "VNPY_AUTO_ALPHASIFT_LLM_PROBE_TIMEOUT_SEC": "30",
            },
            clear=False,
        ):
            policy = _resolve_auto_alphasift_llm_policy()

        self.assertEqual(policy["timeout_seconds"], 12)
        self.assertEqual(policy["normal_timeout_seconds"], 12)
        self.assertEqual(policy["max_retries"], 3)
        self.assertEqual(policy["failure_threshold"], 2)
        self.assertEqual(policy["cooldown_minutes"], 15)
        self.assertEqual(policy["probe_timeout_seconds"], 12)
        self.assertEqual(policy["probe_lease_seconds"], 300)
        self.assertTrue(policy["circuit_breaker_enabled"])
        self.assertTrue(policy["use_llm"])
        self.assertEqual(policy["state"], "closed")
        self.assertEqual(policy["fallback"], "screen_score")

    def test_auto_alphasift_llm_circuit_skips_repeated_legacy_failure(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
            },
            include_snapshot=False,
            include_recent_trades=False,
        )
        previous = self.service.agent_repo.create_run(
            run_uid="legacy-llm-ranking-failure",
            trigger_source="vnpy_paper_auto",
            strategy="dual_low",
            market="cn",
        )
        self.service.agent_repo.complete_run(
            run_id=int(previous["id"]),
            status="completed",
            candidate_count=1,
            planned_count=0,
            submitted_count=0,
            skipped_count=1,
            diagnostics={
                "alphasift_llm_policy": {"timeout_seconds": 45},
                "warnings": ["LLM ranking failed: fell back to screen_score"],
            },
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "quality_status": "ok",
            "candidates": [],
            "llm_ranked": False,
            "warnings": [],
            "source_errors": [],
        }

        with (
            patch.dict(
                os.environ,
                {
                    "VNPY_AUTO_ALPHASIFT_LLM_FAILURE_THRESHOLD": "1",
                    "VNPY_AUTO_ALPHASIFT_LLM_COOLDOWN_MINUTES": "60",
                },
                clear=False,
            ),
            patch(
                "src.services.vnpy_paper_trading_service.AlphaSiftService",
                return_value=fake_alphasift,
            ),
        ):
            result = self.service.run_auto_trade_once(execution_mode_override="dry_run")

        self.assertFalse(fake_alphasift.screen.call_args.kwargs["use_llm"])
        audit = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        assert audit is not None
        policy = audit["diagnostics"]["alphasift_llm_policy"]
        outcome = audit["diagnostics"]["alphasift_llm_result"]
        self.assertEqual(policy["state"], "open")
        self.assertEqual(policy["decision"], "circuit_open")
        self.assertEqual(policy["history_run_uids"], ["legacy-llm-ranking-failure"])
        self.assertEqual(outcome["status"], "skipped")
        self.assertEqual(outcome["reason"], "circuit_open")
        health_event = next(
            event for event in audit["timeline"] if event["stage"] == "llm_ranking_health"
        )
        self.assertEqual(health_event["status"], "skipped")
        self.assertIn("decision=circuit_open", health_event["message"])

    def test_auto_alphasift_llm_half_open_probe_success_closes_circuit(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
            },
            include_snapshot=False,
            include_recent_trades=False,
        )
        previous = self.service.agent_repo.create_run(
            run_uid="llm-ranking-failure-before-probe",
            trigger_source="vnpy_paper_auto",
            strategy="dual_low",
            market="cn",
        )
        self.service.agent_repo.complete_run(
            run_id=int(previous["id"]),
            status="completed",
            candidate_count=1,
            planned_count=0,
            submitted_count=0,
            skipped_count=1,
            diagnostics={
                "alphasift_llm_result": {
                    "status": "failure",
                    "reason": "timeout",
                }
            },
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "quality_status": "ok",
            "candidates": [],
            "llm_ranked": True,
            "warnings": [],
            "source_errors": [],
        }
        future = datetime.now(timezone.utc) + timedelta(minutes=61)

        with (
            patch.dict(
                os.environ,
                {
                    "VNPY_AUTO_ALPHASIFT_LLM_FAILURE_THRESHOLD": "1",
                    "VNPY_AUTO_ALPHASIFT_LLM_COOLDOWN_MINUTES": "60",
                    "VNPY_AUTO_ALPHASIFT_LLM_PROBE_TIMEOUT_SEC": "7",
                },
                clear=False,
            ),
            patch.object(self.service, "_now_utc", return_value=future),
            patch(
                "src.services.vnpy_paper_trading_service.AlphaSiftService",
                return_value=fake_alphasift,
            ),
        ):
            result = self.service.run_auto_trade_once(execution_mode_override="dry_run")
            closed_policy = self.service._auto_alphasift_llm_policy(
                self.service.get_settings()
            )

        call = fake_alphasift.screen.call_args.kwargs
        self.assertTrue(call["use_llm"])
        self.assertEqual(call["llm_timeout_seconds"], 7)
        audit = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        assert audit is not None
        self.assertEqual(audit["diagnostics"]["alphasift_llm_policy"]["state"], "half_open")
        self.assertEqual(audit["diagnostics"]["alphasift_llm_result"]["status"], "success")
        self.assertEqual(audit["diagnostics"]["alphasift_llm_result"]["state_after"], "closed")
        self.assertEqual(closed_policy["state"], "closed")
        self.assertEqual(closed_policy["consecutive_failures"], 0)

    def test_auto_alphasift_llm_allows_only_one_half_open_probe(self) -> None:
        settings = self.service.get_settings()
        failure = self.service.agent_repo.create_run(
            run_uid="llm-failure-before-active-probe",
            trigger_source="vnpy_paper_auto",
            strategy=settings.auto_strategy,
            market=settings.auto_market,
        )
        self.service.agent_repo.complete_run(
            run_id=int(failure["id"]),
            status="completed",
            candidate_count=0,
            submitted_count=0,
            skipped_count=0,
            diagnostics={"alphasift_llm_result": {"status": "failure", "reason": "timeout"}},
        )
        active_probe = self.service.agent_repo.create_run(
            run_uid="llm-active-recovery-probe",
            trigger_source="vnpy_paper_auto",
            strategy=settings.auto_strategy,
            market=settings.auto_market,
            diagnostics={
                "alphasift_llm_policy": {
                    "state": "half_open",
                    "decision": "recovery_probe",
                    "use_llm": True,
                }
            },
        )

        with patch.dict(
            os.environ,
            {
                "VNPY_AUTO_ALPHASIFT_LLM_FAILURE_THRESHOLD": "1",
                "VNPY_AUTO_ALPHASIFT_LLM_COOLDOWN_MINUTES": "1",
            },
            clear=False,
        ):
            policy = self.service._auto_alphasift_llm_policy(settings)

        self.assertEqual(policy["state"], "open")
        self.assertEqual(policy["decision"], "probe_in_progress")
        self.assertFalse(policy["use_llm"])
        self.assertEqual(policy["last_attempt_run_uid"], active_probe["run_uid"])
        self.assertIsNotNone(policy["probe_lease_expires_at"])

    def test_auto_alphasift_llm_expires_abandoned_half_open_probe(self) -> None:
        settings = self.service.get_settings()
        active_probe = self.service.agent_repo.create_run(
            run_uid="llm-abandoned-recovery-probe",
            trigger_source="vnpy_paper_auto",
            strategy=settings.auto_strategy,
            market=settings.auto_market,
            diagnostics={
                "alphasift_llm_policy": {
                    "state": "half_open",
                    "decision": "recovery_probe",
                    "use_llm": True,
                }
            },
        )
        stale_at = datetime.now() - timedelta(minutes=10)
        with DatabaseManager.get_instance().get_session() as session:
            session.execute(
                text(
                    "UPDATE stock_selection_agent_runs "
                    "SET created_at = :stale_at, updated_at = :stale_at "
                    "WHERE id = :run_id"
                ),
                {"stale_at": stale_at, "run_id": int(active_probe["id"])},
            )
            session.commit()

        with patch.dict(
            os.environ,
            {
                "VNPY_AUTO_ALPHASIFT_LLM_FAILURE_THRESHOLD": "1",
                "VNPY_AUTO_ALPHASIFT_LLM_COOLDOWN_MINUTES": "60",
            },
            clear=False,
        ):
            policy = self.service._auto_alphasift_llm_policy(settings)

        self.assertEqual(policy["state"], "open")
        self.assertEqual(policy["decision"], "circuit_open")
        self.assertFalse(policy["use_llm"])
        self.assertEqual(policy["consecutive_failures"], 1)
        self.assertEqual(policy["last_attempt_run_uid"], active_probe["run_uid"])

    def test_alphasift_llm_result_keeps_disabled_circuit_state(self) -> None:
        result = self.service._build_alphasift_llm_result(
            {"llm_ranked": False, "warnings": ["LLM ranking failed: timeout"]},
            {
                "use_llm": True,
                "circuit_breaker_enabled": False,
                "state": "disabled",
                "decision": "normal",
                "failure_threshold": 1,
                "consecutive_failures": 0,
                "timeout_seconds": 45,
            },
        )

        self.assertEqual(result["status"], "failure")
        self.assertEqual(result["reason"], "timeout")
        self.assertEqual(result["state_after"], "disabled")

    @staticmethod
    def _age_trade_plan(plan_id: int, *, minutes: int = 45) -> None:
        with DatabaseManager.get_instance().get_session() as session:
            session.execute(
                text(
                    f"UPDATE {StockSelectionAgentTradePlan.__tablename__} "
                    "SET updated_at = :updated_at WHERE id = :id"
                ),
                {"updated_at": datetime.now() - timedelta(minutes=minutes), "id": int(plan_id)},
            )
            session.commit()

    def test_cash_order_fills_cn_buy_in_100_share_lots(self) -> None:
        with patch(
            "src.services.portfolio_service.PortfolioService._fetch_realtime_position_price",
            return_value=(10.0, "unit-test"),
        ):
            result = self.service.submit_order(
                symbol="600519",
                side="buy",
                market="cn",
                cash_amount=1050,
                price=10.0,
            )
            status = self.service.get_status()

        self.assertTrue(result["accepted"])
        self.assertEqual(result["quantity"], 100.0)
        self.assertEqual(result["cash_amount"], 1000.0)
        account_snapshot = status["snapshot"]["accounts"][0]
        self.assertEqual(account_snapshot["total_cash"], 99000.0)
        self.assertEqual(account_snapshot["positions"][0]["symbol"], "600519")

    def test_data_quality_gate_defaults_to_guarded_floor_and_zero_disables(self) -> None:
        self.assertEqual(self.service.get_settings().auto_min_data_quality_score, 60.0)

        normalized = self.service.update_settings(
            {"auto_min_data_quality_score": None},
            include_snapshot=False,
            include_recent_trades=False,
        )
        self.assertEqual(normalized["settings"]["auto_min_data_quality_score"], 60.0)

        audit_only = self.service.update_settings(
            {"auto_min_data_quality_score": 0},
            include_snapshot=False,
            include_recent_trades=False,
        )
        self.assertEqual(audit_only["settings"]["auto_min_data_quality_score"], 0.0)

    def test_cross_run_quality_gate_blocks_buys_but_preserves_sell_checks(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_cross_run_quality_gate_enabled": True,
            },
            include_snapshot=False,
            include_recent_trades=False,
        )
        snapshot = {
            "state": "blocked",
            "reason": "forward_win_rate_below_threshold",
            "gate_enabled": True,
            "gate_blocked": True,
            "win_rate_pct": 30.0,
            "min_win_rate_pct": 45.0,
        }
        sell_order = {"accepted": True, "status": "filled", "side": "sell"}
        with (
            patch.object(self.service, "_cross_run_quality_snapshot", return_value=snapshot),
            patch.object(self.service, "_run_auto_sell_checks", return_value=[sell_order]),
            patch("src.services.vnpy_paper_trading_service.AlphaSiftService.screen") as screen,
        ):
            result = self.service.run_auto_trade_once()

        screen.assert_not_called()
        self.assertEqual(result["reason"], "cross_run_quality_gate_blocked")
        self.assertEqual(result["orders"], [sell_order])
        self.assertEqual(result["submitted_count"], 1)
        audit = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        self.assertEqual(audit["diagnostics"]["cross_run_quality"]["state"], "blocked")

    def test_cash_order_below_cn_lot_returns_actionable_skip_reason(self) -> None:
        result = self.service.submit_order(
            symbol="600519",
            side="buy",
            market="cn",
            cash_amount=10000,
            price=1193.01,
        )

        self.assertFalse(result["accepted"])
        self.assertEqual(result["reason"], "cash_below_min_lot")
        self.assertIn("one A-share lot", result["message"])

    def test_hk_order_requires_current_fx_rate_for_base_currency_cash_check(self) -> None:
        result = self.service.submit_order(
            symbol="00700",
            side="buy",
            market="hk",
            cash_amount=1000,
            price=100,
        )

        self.assertFalse(result["accepted"])
        self.assertEqual(result["reason"], "fx_rate_unavailable")
        self.assertEqual(result["raw"]["fx_conversion"]["quote_currency"], "HKD")
        self.assertTrue(result["raw"]["fx_conversion"]["stale"])

    def test_hk_order_records_quote_currency_and_base_currency_notional(self) -> None:
        self.service.portfolio.repo.save_fx_rate(
            from_currency="CNY",
            to_currency="HKD",
            rate_date=date.today(),
            rate=1.1,
            source="unit-test",
        )

        result = self.service.submit_order(
            symbol="00700",
            side="buy",
            market="hk",
            cash_amount=1100,
            price=100,
        )

        self.assertTrue(result["accepted"])
        self.assertEqual(result["cash_amount"], 1100)
        self.assertAlmostEqual(result["cash_amount_base"], 1000)
        self.assertEqual(result["base_currency"], "CNY")
        self.assertEqual(result["quote_currency"], "HKD")
        account_id = int(self.service.get_settings().account_id)
        trades = self.service.portfolio.list_trade_events(account_id=account_id, page=1)
        self.assertEqual(trades["items"][0]["currency"], "HKD")

    def test_hk_sell_remains_available_when_fx_rate_is_missing(self) -> None:
        account = self.service.ensure_account()
        account_id = int(account["id"])
        self.service.portfolio.record_trade(
            account_id=account_id,
            symbol="00700",
            trade_date=date.today(),
            side="buy",
            quantity=10,
            price=100,
            market="hk",
            currency="HKD",
            trade_uid="hk-position-without-fx",
        )

        result = self.service.submit_order(
            symbol="00700",
            side="sell",
            market="hk",
            quantity=10,
            price=101,
        )

        self.assertTrue(result["accepted"])
        self.assertEqual(result["quote_currency"], "HKD")
        self.assertTrue(result["raw"]["fx_conversion"]["stale"])

    def test_summary_status_skips_snapshot_and_recent_trades(self) -> None:
        self.service.submit_order(
            symbol="600519",
            side="buy",
            market="cn",
            quantity=100,
            price=10.0,
        )

        with patch.object(
            self.service.portfolio,
            "get_portfolio_snapshot",
            side_effect=AssertionError("snapshot should not be loaded"),
        ), patch.object(
            self.service.portfolio,
            "list_trade_events",
            side_effect=AssertionError("recent trades should not be loaded"),
        ):
            status = self.service.get_status(include_snapshot=False, include_recent_trades=False)

        self.assertIsNone(status["snapshot"])
        self.assertEqual(status["recent_trades"], [])
        self.assertEqual(status["diagnostics"]["detail_level"], "summary")
        self.assertFalse(status["diagnostics"]["snapshot_requested"])
        self.assertFalse(status["diagnostics"]["recent_trades_requested"])
        self.assertIn("vnpy_adapter", status["diagnostics"])
        self.assertIn("order_request_supported", status["diagnostics"]["vnpy_adapter"])
        self.assertEqual(
            status["diagnostics"]["industry_exposure"]["status"],
            "snapshot_not_requested",
        )

    def test_industry_exposure_diagnostics_reports_coverage_and_missing_symbols(self) -> None:
        settings = self.service.get_settings()
        diagnostics = self.service._industry_exposure_diagnostics(
            settings=settings,
            evaluated=True,
            snapshot={
                "accounts": [{
                    "positions": [
                        {
                            "symbol": "600519",
                            "market": "cn",
                            "quantity": 100,
                            "market_value_base": 1000,
                            "industry": "白酒",
                        },
                        {
                            "symbol": "000001",
                            "market": "hk",
                            "quantity": 200,
                            "market_value_base": 2000,
                        },
                    ],
                }],
            },
        )

        self.assertEqual(diagnostics["status"], "partial")
        self.assertEqual(diagnostics["position_count"], 2)
        self.assertEqual(diagnostics["resolved_position_count"], 1)
        self.assertEqual(diagnostics["missing_position_count"], 1)
        self.assertEqual(diagnostics["coverage_pct"], 50.0)
        self.assertEqual(diagnostics["industry_values"], {"白酒": 1000.0})
        self.assertEqual(diagnostics["missing_symbols"], ["000001"])
        self.assertEqual(diagnostics["resolution_mode"], "snapshot_only")

    def test_industry_exposure_diagnostics_resolves_boards_when_guard_is_configured(self) -> None:
        self.service.data_fetcher_manager.boards_by_symbol = {
            "600519": [{"name": "白酒", "type": "行业"}],
        }
        settings = replace(
            self.service.get_settings(),
            auto_max_industry_position_pct=30.0,
        )
        diagnostics = self.service._industry_exposure_diagnostics(
            settings=settings,
            evaluated=True,
            snapshot={
                "accounts": [{
                    "positions": [{
                        "symbol": "600519",
                        "market": "cn",
                        "quantity": 100,
                        "market_value_base": 1000,
                    }],
                }],
            },
        )

        self.assertEqual(diagnostics["status"], "complete")
        self.assertEqual(diagnostics["coverage_pct"], 100.0)
        self.assertEqual(diagnostics["industry_values"], {"白酒": 1000.0})
        self.assertEqual(diagnostics["resolution_mode"], "risk_guard")

    def test_status_snapshot_uses_short_ttl_cache_and_invalidates_after_trade(self) -> None:
        account = self.service.ensure_account()
        account_id = int(account["id"])
        self.service._invalidate_status_snapshot_cache(account_id)
        first_snapshot = {
            "accounts": [{
                "account_id": account_id,
                "total_cash": 100000.0,
                "positions": [],
            }],
        }
        second_snapshot = {
            "accounts": [{
                "account_id": account_id,
                "total_cash": 99000.0,
                "positions": [{"symbol": "600519", "quantity": 100}],
            }],
        }

        with patch.object(
            self.service.portfolio,
            "get_portfolio_snapshot",
            side_effect=[first_snapshot, second_snapshot],
        ) as get_snapshot, patch.object(
            self.service,
            "_account_cash",
            return_value=100000.0,
        ):
            first = self.service.get_status(include_snapshot=True, include_recent_trades=False)
            second = self.service.get_status(include_snapshot=True, include_recent_trades=False)
            order = self.service.submit_order(
                symbol="600519",
                side="buy",
                market="cn",
                quantity=100,
                price=10.0,
            )
            third = self.service.get_status(include_snapshot=True, include_recent_trades=False)

        self.assertEqual(get_snapshot.call_count, 2)
        self.assertFalse(first["diagnostics"]["snapshot_cache_hit"])
        self.assertTrue(second["diagnostics"]["snapshot_cache_hit"])
        self.assertTrue(order["accepted"])
        self.assertFalse(third["diagnostics"]["snapshot_cache_hit"])
        self.assertEqual(third["snapshot"]["accounts"][0]["total_cash"], 99000.0)

    def test_status_exposes_trading_window_diagnostics(self) -> None:
        fake_window = {
            "available": True,
            "market": "cn",
            "phase": "postmarket",
            "is_market_open_now": False,
            "next_window_status": "next_session",
            "next_session_date": "2026-07-03",
            "next_open_at": "2026-07-03T09:30:00+08:00",
            "next_close_at": "2026-07-03T15:00:00+08:00",
            "reason": None,
        }

        with patch(
            "src.services.vnpy_paper_trading_service.trading_calendar.build_next_trading_window_context",
            return_value=fake_window,
        ) as build_window:
            status = self.service.get_status(include_snapshot=False, include_recent_trades=False)

        build_window.assert_called_once_with(
            market="cn",
            trigger_source="vnpy_paper_auto",
            analysis_intent="auto",
        )
        window = status["diagnostics"]["trading_window"]
        self.assertEqual(window["next_window_status"], "next_session")
        self.assertEqual(window["next_open_at"], "2026-07-03T09:30:00+08:00")
        self.assertTrue(window["time_gate_enabled"])
        self.assertTrue(window["time_gate_enforced"])
        self.assertEqual(window["execution_mode"], "paper")
        self.assertIsNone(window["gate_reason"])

    def test_config_payload_accepts_utf8_bom(self) -> None:
        self.config_path.write_text(
            '\ufeff{"settings":{"auto_trade_enabled":true,"auto_interval_minutes":5}}',
            encoding="utf-8",
        )

        settings = self.service.get_settings()

        self.assertTrue(settings.auto_trade_enabled)
        self.assertEqual(settings.auto_interval_minutes, 5)

    def test_settings_inherit_runtime_gateway_name_when_not_saved(self) -> None:
        with patch.dict(os.environ, {"VNPY_GATEWAY_NAME": "DSA_SIM"}, clear=False):
            settings = self.service.get_settings()

        self.assertEqual(settings.vnpy_gateway_name, "DSA_SIM")

    def test_auto_trade_run_records_structured_agent_plan(self) -> None:
        result = self.service.run_auto_trade_once()

        detail = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        self.assertIsNotNone(detail)
        assert detail is not None
        plan = detail["diagnostics"]["agent_plan"]
        self.assertEqual(plan["schema_version"], 1)
        self.assertEqual(plan["objective"], "screen_online_candidates_and_generate_simulated_trade_plans")
        self.assertEqual(plan["strategy"], "dual_low")
        self.assertEqual(plan["market"], "cn")
        self.assertEqual(plan["execution_mode"], "paper")
        self.assertEqual(plan["plan_profile"]["mode"], "observe_only")
        self.assertEqual(plan["plan_profile"]["risk_level"], "observe_only")
        self.assertEqual(plan["execution_policy"]["order_route"], "local_paper")
        self.assertFalse(plan["execution_policy"]["submits_orders"])
        self.assertEqual(plan["sizing_plan"]["max_planned_cash"], 30000.0)
        self.assertEqual(plan["position_plan"]["cash_per_order"], 10000.0)
        self.assertEqual(plan["risk_budget"]["max_positions"], 10)
        self.assertIn("candidate_filters", plan["adaptive_controls"]["configured_layers"])
        self.assertIn("portfolio_limits", plan["adaptive_controls"]["configured_layers"])
        self.assertIn(
            "data_quality_stale",
            [item["reason"] for item in plan["adaptive_controls"]["degrade_actions"]],
        )
        self.assertTrue(plan["gates"]["time_gate_enabled"])
        self.assertEqual(plan["gates"]["acceptable_data_quality"], ["ok", "partial"])
        self.assertIn("stock_selection_agent_trade_plans", plan["expected_outputs"])
        self.assertIn("agent_review", plan["expected_outputs"])
        self.assertTrue(any(event["stage"] == "agent_plan" for event in detail["timeline"]))

    def test_cross_market_objective_tightens_rejected_us_run_without_widening_limits(self) -> None:
        settings = replace(
            self.service.get_settings(),
            auto_market="us",
            auto_max_results=4,
            auto_cash_per_order=10000,
            auto_min_score=70,
        )

        objective = self.service._build_cross_market_objective(
            settings,
            recent_run_context={
                "current_failure_streak": 0,
                "latest_human_feedback": {"verdict": "rejected"},
            },
            cross_run_quality={"state": "healthy"},
        )
        effective = self.service._apply_cross_market_objective(settings, objective)

        self.assertEqual(objective["primary_objective"], "volatility_and_gap_control")
        self.assertEqual(objective["mode"], "strict")
        self.assertEqual(objective["status"], "tightened")
        self.assertEqual(objective["market_risk_factor"], 0.75)
        self.assertEqual(objective["effective"]["max_results"], 1)
        self.assertEqual(objective["effective"]["cash_per_order"], 3750.0)
        self.assertEqual(effective.auto_max_results, 1)
        self.assertEqual(effective.auto_cash_per_order, 3750.0)
        self.assertEqual(effective.auto_min_score, 70)

        rejected_looser_values = self.service._apply_cross_market_objective(
            settings,
            {
                "applied_overrides": {
                    "auto_max_results": 8,
                    "auto_cash_per_order": 20000,
                    "auto_min_score": 60,
                }
            },
        )
        self.assertEqual(rejected_looser_values, settings)

        review_guarded = self.service._build_cross_market_objective(
            settings,
            recent_run_context={"current_failure_streak": 0},
            cross_run_quality={
                "state": "blocked",
                "review_quality_state": "blocked",
                "review_quality_applied": True,
                "return_risk_objective_state": "guarded",
                "return_risk_objective_applied": True,
            },
        )
        self.assertEqual(review_guarded["mode"], "strict")
        self.assertIn("review_quality_blocked", review_guarded["reasons"])
        self.assertIn("return_risk_objective_guarded", review_guarded["reasons"])

    def test_cross_market_objective_changes_screen_and_plan_execution_limits(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_market": "hk",
                "auto_max_results": 4,
                "auto_cash_per_order": 6000,
            },
            include_snapshot=False,
            include_recent_trades=False,
        )
        previous = self.service.agent_repo.create_run(
            run_uid="market-objective-needs-changes",
            trigger_source="vnpy_paper_auto",
            strategy="dual_low",
            market="hk",
        )
        self.service.agent_repo.complete_run(
            run_id=int(previous["id"]),
            status="completed",
            candidate_count=1,
            planned_count=0,
            submitted_count=0,
            skipped_count=0,
        )
        self.service.agent_repo.upsert_run_feedback(
            "market-objective-needs-changes",
            verdict="needs_changes",
            note="Reduce foreign-market exposure.",
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "quality_status": "ok",
            "candidates": [],
            "warnings": [],
            "source_errors": [],
        }

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ):
            result = self.service.run_auto_trade_once(execution_mode_override="dry_run")

        fake_alphasift.screen.assert_called_once_with(
            strategy="dual_low",
            market="hk",
            max_results=2,
            source_health_trends=[],
            use_llm=True,
            llm_timeout_seconds=45,
            llm_max_retries=0,
        )
        audit = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        assert audit is not None
        plan = audit["diagnostics"]["agent_plan"]
        objective = plan["market_objective"]
        self.assertEqual(plan["max_results"], 2)
        self.assertEqual(plan["cash_per_order"], 3600.0)
        self.assertEqual(objective["configured"]["max_results"], 4)
        self.assertEqual(objective["configured"]["cash_per_order"], 6000.0)
        self.assertEqual(objective["mode"], "guarded")
        self.assertIn("latest_human_feedback_needs_changes", objective["reasons"])

    def test_auto_trade_llm_dynamic_plan_overrides_screen_parameters_for_run(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_llm_plan_enabled": True,
                "auto_strategy": "dual_low",
                "auto_market": "cn",
                "auto_max_results": 3,
                "auto_cash_per_order": 5000,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.strategies.return_value = {
            "strategies": [
                {"id": "dual_low", "name": "Dual low"},
                {"id": "capital_heat", "name": "Capital heat"},
            ]
        }
        fake_alphasift.screen.return_value = {
            "quality_status": "ok",
            "candidates": [
                {"code": "600519", "score": 80, "price": 10.0, "amount": 200000000},
            ],
            "warnings": [],
            "source_errors": [],
            "source_health": {"snapshot": {"sina": {"failures": 1, "disabled": False}}},
            "source_routing": {
                "mode": "dynamic_health",
                "base_priority": "sina,efinance",
                "effective_priority": "efinance,sina",
                "adjusted": True,
                "candidate_context": {
                    "quote": {
                        "mode": "circuit_breaker_failover",
                        "sources": {
                            "cn/efinance": {
                                "state": "open",
                                "failures": 3,
                                "disabled": True,
                                "cooldown_remaining_seconds": 240,
                            }
                        },
                    },
                    "fund_flow": {
                        "mode": "circuit_breaker_failover",
                        "priority": ["tushare_ths", "akshare"],
                        "sources": {
                            "cn/tushare_ths": {
                                "state": "open",
                                "failures": 3,
                                "disabled": True,
                                "cooldown_remaining_seconds": 180,
                            },
                            "cn/akshare": {
                                "state": "closed",
                                "failures": 0,
                                "disabled": False,
                                "cooldown_remaining_seconds": 0,
                            },
                        },
                    },
                },
            },
        }
        fake_analyzer = MagicMock()
        fake_analyzer.is_available.return_value = True
        fake_analyzer._call_litellm.return_value = (
            json.dumps(
                {
                    "strategy": "capital_heat",
                    "market": "cn",
                    "max_results": 1,
                    "cash_per_order": 1200,
                    "min_score": 75,
                    "risk_level": "guarded",
                    "rationale": "Recent runs need a narrower heat strategy.",
                    "checks": [{"key": "budget", "status": "tightened"}],
                }
            ),
            "openai/test",
            {"total_tokens": 42},
        )

        trend_items = [
            {
                "group": "snapshot",
                "source": "sina",
                "observation_count": 10,
                "degraded_observation_count": 8,
            }
        ]
        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ), patch("src.analyzer.GeminiAnalyzer", return_value=fake_analyzer), patch.object(
            self.service.agent_repo,
            "summarize_data_quality_trends",
            return_value={"source_health_items": trend_items},
        ):
            result = self.service.run_auto_trade_once(execution_mode_override="dry_run")

        fake_alphasift.screen.assert_called_once_with(
            strategy="capital_heat",
            market="cn",
            max_results=1,
            source_health_trends=trend_items,
            use_llm=True,
            llm_timeout_seconds=45,
            llm_max_retries=0,
        )
        self.assertTrue(result["accepted"])
        self.assertEqual(result["strategy"], "capital_heat")
        self.assertEqual(result["planned_count"], 1)

        audit = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        self.assertIsNotNone(audit)
        assert audit is not None
        plan = audit["diagnostics"]["agent_plan"]
        dynamic_plan = plan["llm_dynamic_plan"]
        self.assertEqual(plan["strategy"], "capital_heat")
        self.assertEqual(plan["max_results"], 1)
        self.assertEqual(plan["cash_per_order"], 1200.0)
        self.assertEqual(
            audit["diagnostics"]["source_routing"]["effective_priority"],
            "efinance,sina",
        )
        quote_routing = audit["diagnostics"]["source_routing"]["candidate_context"][
            "quote"
        ]
        self.assertEqual(quote_routing["mode"], "circuit_breaker_failover")
        self.assertEqual(quote_routing["sources"]["cn/efinance"]["state"], "open")
        self.assertEqual(
            quote_routing["sources"]["cn/efinance"]["cooldown_remaining_seconds"],
            240,
        )
        fund_flow_routing = audit["diagnostics"]["source_routing"]["candidate_context"][
            "fund_flow"
        ]
        self.assertEqual(fund_flow_routing["priority"], ["tushare_ths", "akshare"])
        self.assertEqual(
            fund_flow_routing["sources"]["cn/tushare_ths"]["state"],
            "open",
        )
        self.assertEqual(
            fund_flow_routing["sources"]["cn/akshare"]["state"],
            "closed",
        )
        self.assertTrue(plan["gates"]["llm_dynamic_plan_enabled"])
        self.assertTrue(plan["gates"]["llm_dynamic_plan_applied"])
        self.assertEqual(audit["diagnostics"]["alphasift_llm_policy"]["timeout_seconds"], 45)
        self.assertEqual(audit["diagnostics"]["alphasift_llm_policy"]["decision"], "normal")
        timings = audit["diagnostics"]["stage_timings"]
        self.assertEqual(timings["schema_version"], 1)
        self.assertEqual(timings["completed_stage"], "candidate_decision_execution")
        for key in (
            "planning_seconds",
            "preflight_seconds",
            "alphasift_screen_seconds",
            "candidate_decision_execution_seconds",
            "total_seconds",
        ):
            self.assertGreaterEqual(timings[key], 0.0)
        performance_event = next(
            event for event in audit["timeline"] if event["stage"] == "performance"
        )
        self.assertEqual(performance_event["details"], timings)
        self.assertIn("AlphaSift=", performance_event["message"])
        self.assertEqual(dynamic_plan["status"], "accepted")
        self.assertEqual(dynamic_plan["model"], "openai/test")
        self.assertEqual(dynamic_plan["prompt_version"], LLM_DYNAMIC_AGENT_PLAN_PROMPT_VERSION)
        self.assertEqual(dynamic_plan["evaluator_version"], LLM_DYNAMIC_AGENT_PLAN_EVALUATOR_VERSION)
        self.assertEqual(dynamic_plan["applied_overrides"]["auto_strategy"], "capital_heat")
        self.assertEqual(dynamic_plan["applied_overrides"]["auto_max_results"], 1)
        self.assertEqual(dynamic_plan["applied_overrides"]["auto_cash_per_order"], 1200.0)
        self.assertEqual(dynamic_plan["applied_overrides"]["auto_min_score"], 75.0)
        self.assertEqual(plan["recent_run_context"]["run_count"], 0)
        self.assertIn(
            '"recent_run_context"',
            fake_analyzer._call_litellm.call_args.args[0],
        )
        audit_context = fake_analyzer._call_litellm.call_args.kwargs["audit_context"]
        self.assertEqual(audit_context["prompt_version"], LLM_DYNAMIC_AGENT_PLAN_PROMPT_VERSION)
        self.assertEqual(audit_context["evaluator_version"], LLM_DYNAMIC_AGENT_PLAN_EVALUATOR_VERSION)
        self.assertIn("llm_dynamic_plan", plan["adaptive_controls"]["configured_layers"])
        self.assertEqual(audit["trade_plans"][0]["planned_cash_amount"], 1200.0)

        settings_after = self.service.get_settings()
        self.assertEqual(settings_after.auto_strategy, "dual_low")
        self.assertEqual(settings_after.auto_max_results, 3)
        self.assertEqual(settings_after.auto_cash_per_order, 5000)
        self.assertIsNone(settings_after.auto_min_score)

    def test_recent_agent_run_context_summarizes_same_strategy_history(self) -> None:
        first = self.service.agent_repo.create_run(
            run_uid="recent-context-completed",
            trigger_source="vnpy_paper_auto",
            strategy="dual_low",
            market="cn",
        )
        self.service.agent_repo.complete_run(
            run_id=int(first["id"]),
            status="completed",
            candidate_count=4,
            planned_count=1,
            submitted_count=2,
            skipped_count=1,
            diagnostics={"data_quality": {"status": "ok"}},
        )
        self.service.agent_repo.upsert_run_feedback(
            "recent-context-completed",
            verdict="approved",
            note="Keep the risk sizing.",
            reviewer="qa-one",
        )
        second = self.service.agent_repo.create_run(
            run_uid="recent-context-failed",
            trigger_source="vnpy_paper_auto",
            strategy="dual_low",
            market="cn",
        )
        self.service.agent_repo.complete_run(
            run_id=int(second["id"]),
            status="failed",
            candidate_count=1,
            planned_count=0,
            submitted_count=0,
            skipped_count=1,
            error="source timeout",
            diagnostics={"data_quality": {"status": "unavailable"}},
        )
        self.service.agent_repo.upsert_run_feedback(
            "recent-context-failed",
            verdict="needs_changes",
            note="Source timeout needs a safer fallback.",
            reviewer="qa-two",
        )

        context = self.service._recent_agent_run_context(self.service.get_settings())

        self.assertEqual(context["scope"], "same_trigger_strategy_market")
        self.assertEqual(context["run_count"], 2)
        self.assertEqual(context["status_counts"], {"completed": 1, "failed": 1})
        self.assertEqual(context["data_quality_counts"], {"ok": 1, "unavailable": 1})
        self.assertEqual(context["candidate_count"], 5)
        self.assertEqual(context["submitted_count"], 2)
        self.assertEqual(context["submission_rate_pct"], 40.0)
        self.assertEqual(context["current_failure_streak"], 1)
        self.assertEqual(context["latest_run_uid"], "recent-context-failed")
        self.assertEqual(context["runs"][0]["error"], "source timeout")
        self.assertEqual(context["schema_version"], 2)
        self.assertEqual(
            context["human_feedback_counts"],
            {"approved": 1, "needs_changes": 1},
        )
        self.assertEqual(context["human_feedback_reviewed_count"], 2)
        self.assertTrue(context["human_feedback_attention_required"])
        self.assertEqual(
            context["human_feedback_policy"],
            "context_only_never_bypasses_risk_gates",
        )
        self.assertEqual(
            context["latest_human_feedback"]["verdict"],
            "needs_changes",
        )
        self.assertIn("safer fallback", context["runs"][0]["human_feedback"]["note"])

    def test_reset_account_archives_old_paper_account_and_creates_clean_ledger(self) -> None:
        with patch(
            "src.services.portfolio_service.PortfolioService._fetch_realtime_position_price",
            return_value=(10.0, "unit-test"),
        ):
            self.service.submit_order(
                symbol="600519",
                side="buy",
                market="cn",
                quantity=100,
                price=10.0,
            )
        old_status = self.service.get_status()
        old_account_id = int(old_status["account"]["id"])
        payload = self.service._read_config_payload()
        payload["auto_trailing_peaks"] = {"600519": 12.0}
        self.service._write_config_payload(payload)

        reset_status = self.service.reset_account()

        new_account_id = int(reset_status["account"]["id"])
        self.assertNotEqual(new_account_id, old_account_id)
        self.assertEqual(reset_status["settings"]["account_id"], new_account_id)
        self.assertEqual(reset_status["diagnostics"]["account_reset"]["archived_account_id"], old_account_id)
        self.assertEqual(reset_status["diagnostics"]["account_reset"]["new_account_id"], new_account_id)
        account_snapshot = reset_status["snapshot"]["accounts"][0]
        self.assertEqual(account_snapshot["total_cash"], 100000.0)
        self.assertEqual(account_snapshot["positions"], [])
        self.assertEqual(self.service._read_config_payload().get("auto_trailing_peaks"), {})
        accounts = self.service.portfolio.list_accounts(include_inactive=True)
        archived = next(item for item in accounts if int(item["id"]) == old_account_id)
        self.assertFalse(archived["is_active"])
        account_history = self.service.list_paper_accounts(include_inactive=True)
        self.assertEqual(account_history["current_account_id"], new_account_id)
        self.assertEqual(account_history["count"], 2)
        self.assertEqual([int(item["id"]) for item in account_history["items"]], [new_account_id, old_account_id])
        current = account_history["items"][0]
        old = account_history["items"][1]
        self.assertTrue(current["is_current"])
        self.assertFalse(current["archived"])
        self.assertFalse(old["is_current"])
        self.assertTrue(old["archived"])

        cleanup = self.service.cleanup_archived_paper_accounts([old_account_id])

        self.assertEqual(cleanup["cleaned_account_ids"], [old_account_id])
        self.assertEqual(cleanup["hidden_count_after"], 1)
        self.assertEqual(cleanup["accounts"]["count"], 1)
        self.assertEqual(
            self.service.list_paper_accounts(include_inactive=True)["items"][0]["id"],
            new_account_id,
        )
        hidden_history = self.service.list_paper_accounts(include_inactive=True, include_hidden=True)
        hidden_old = next(item for item in hidden_history["items"] if int(item["id"]) == old_account_id)
        self.assertTrue(hidden_old["cleanup_hidden"])

        restored_status = self.service.restore_paper_account(
            old_account_id,
            include_snapshot=False,
            include_recent_trades=False,
        )

        self.assertEqual(int(restored_status["account"]["id"]), old_account_id)
        self.assertEqual(restored_status["settings"]["account_id"], old_account_id)
        self.assertEqual(
            restored_status["diagnostics"]["account_restore"]["restored_account_id"],
            old_account_id,
        )
        self.assertEqual(
            restored_status["diagnostics"]["account_restore"]["previous_account_id"],
            new_account_id,
        )
        self.assertEqual(
            restored_status["diagnostics"]["account_restore"]["deactivated_account_ids"],
            [new_account_id],
        )
        restored_history = self.service.list_paper_accounts(include_inactive=True)
        self.assertEqual(restored_history["current_account_id"], old_account_id)
        self.assertEqual([int(item["id"]) for item in restored_history["items"]], [old_account_id, new_account_id])
        self.assertFalse(restored_history["items"][0]["archived"])
        self.assertTrue(restored_history["items"][1]["archived"])
        self.assertEqual(restored_history["hidden_count"], 0)
        self.assertEqual(self.service._read_config_payload().get("auto_trailing_peaks"), {})

    def test_restore_rejects_non_paper_account_without_switching_current_account(self) -> None:
        current = self.service.ensure_account()
        current_account_id = int(current["id"])
        manual_account = self.service.portfolio.create_account(
            name="Manual account",
            broker="manual",
            market="cn",
            base_currency="CNY",
        )

        with self.assertRaisesRegex(ValueError, "account_not_vnpy_paper"):
            self.service.restore_paper_account(int(manual_account["id"]))

        status = self.service.get_status(include_snapshot=False, include_recent_trades=False)
        self.assertEqual(int(status["account"]["id"]), current_account_id)
        self.assertEqual(status["settings"]["account_id"], current_account_id)
        paper_history = self.service.list_paper_accounts(include_inactive=True)
        self.assertEqual(paper_history["count"], 1)
        self.assertEqual(int(paper_history["items"][0]["id"]), current_account_id)
        self.assertTrue(paper_history["items"][0]["is_active"])

    def test_auto_trade_uses_alphasift_candidates_and_thresholds(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_strategy": "dual_low",
                "auto_max_results": 2,
                "auto_cash_per_order": 1200,
                "auto_min_score": 50,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "candidates": [
                {"code": "600519", "score": 80, "price": 10.0, "trading_status": "未停牌", "limit_status": "未涨停"},
                {"code": "000001", "score": 30, "price": 5.0},
            ],
            "warnings": ["unit-warning"],
        }

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ), patch(
            "src.services.portfolio_service.PortfolioService._fetch_realtime_position_price",
            return_value=(10.0, "unit-test"),
        ):
            result = self.service.run_auto_trade_once()

        self.assertTrue(result["accepted"])
        self.assertEqual(result["candidate_count"], 2)
        self.assertEqual(result["submitted_count"], 1)
        self.assertEqual(result["skipped_count"], 1)
        self.assertEqual(result["orders"][0]["symbol"], "600519")
        self.assertEqual(result["orders"][0]["quantity"], 100.0)
        self.assertEqual(result["orders"][1]["reason"], "score_below_threshold")
        self.assertEqual(result["messages"], ["unit-warning"])
        self.assertTrue(result["agent_run_uid"].startswith("ss-agent-"))

        audit = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        self.assertIsNotNone(audit)
        assert audit is not None
        self.assertEqual(audit["status"], "completed")
        self.assertEqual(audit["candidate_count"], 2)
        self.assertEqual(audit["submitted_count"], 1)
        self.assertEqual(audit["diagnostics"]["data_quality"]["status"], "partial")
        self.assertEqual(len(audit["decisions"]), 2)
        self.assertEqual(len(audit["trade_plans"]), 2)
        self.assertEqual(audit["decisions"][0]["symbol"], "600519")
        self.assertEqual(audit["decisions"][0]["status"], "filled")
        self.assertIsNotNone(audit["decisions"][0]["trade_id"])
        self.assertEqual(audit["decisions"][1]["reason"], "score_below_threshold")
        self.assertEqual(audit["trade_plans"][0]["status"], "filled")
        self.assertEqual(audit["trade_plans"][1]["status"], "skipped")

    def test_auto_trade_hk_fails_closed_without_current_fx_rate(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_market": "hk",
                "auto_cash_per_order": 1000,
                "auto_max_results": 1,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "quality_status": "ok",
            "candidates": [{"code": "00700", "name": "Tencent", "score": 80, "price": 11.0}],
            "warnings": [],
            "source_errors": [],
        }

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ):
            result = self.service.run_auto_trade_once()

        self.assertEqual(result["orders"][0]["reason"], "fx_rate_unavailable")
        self.assertEqual(result["orders"][0]["base_currency"], "CNY")
        self.assertEqual(result["orders"][0]["quote_currency"], "HKD")
        detail = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        self.assertFalse(detail["diagnostics"]["currency_budget"]["available"])

    def test_auto_trade_hk_converts_base_budget_but_audits_base_amount(self) -> None:
        self.service.portfolio.repo.save_fx_rate(
            from_currency="CNY",
            to_currency="HKD",
            rate_date=date.today(),
            rate=1.1,
            source="unit-test",
        )
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_market": "hk",
                "auto_cash_per_order": 1000,
                "auto_max_results": 1,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "quality_status": "ok",
            "candidates": [{"code": "00700", "name": "Tencent", "score": 80, "price": 11.0}],
            "warnings": [],
            "source_errors": [],
        }

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ):
            result = self.service.run_auto_trade_once()

        order = result["orders"][0]
        self.assertTrue(order["accepted"])
        self.assertAlmostEqual(order["cash_amount"], 1100)
        self.assertAlmostEqual(order["cash_amount_base"], 1000)
        self.assertEqual(order["quote_currency"], "HKD")
        detail = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        self.assertAlmostEqual(detail["decisions"][0]["cash_amount"], 1000)
        self.assertAlmostEqual(detail["trade_plans"][0]["planned_cash_amount"], 1000)
        account_id = int(self.service.get_settings().account_id)
        trades = self.service.portfolio.list_trade_events(account_id=account_id, page=1)
        self.assertEqual(trades["items"][0]["currency"], "HKD")

    def test_target_weight_gap_scales_cross_currency_and_daily_budget(self) -> None:
        self.service.portfolio.repo.save_fx_rate(
            from_currency="CNY",
            to_currency="HKD",
            rate_date=date.today(),
            rate=1.1,
            source="unit-test",
        )
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_market": "hk",
                "auto_execution_mode": "dry_run",
                "auto_cash_per_order": 1000,
                "auto_daily_budget": 600,
                "auto_target_position_weights": {"00700": 0.5},
                "auto_max_results": 1,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "quality_status": "ok",
            "candidates": [{"code": "00700", "name": "Tencent", "score": 80, "price": 11.0}],
            "warnings": [],
            "source_errors": [],
        }

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ):
            result = self.service.run_auto_trade_once()

        order = result["orders"][0]
        self.assertEqual(result["planned_count"], 1)
        self.assertAlmostEqual(order["cash_amount"], 550.0)
        self.assertAlmostEqual(order["cash_amount_base"], 500.0)
        self.assertEqual(order["quantity"], 50.0)
        self.assertEqual(order["raw"]["target_weight_sizing"]["resolved_base_amount"], 500.0)

    def test_auto_trade_daily_budget_converts_existing_hk_notional_to_base_currency(self) -> None:
        self.service.portfolio.repo.save_fx_rate(
            from_currency="CNY",
            to_currency="HKD",
            rate_date=date.today(),
            rate=1.1,
            source="unit-test",
        )
        existing = self.service.submit_order(
            symbol="00941",
            side="buy",
            market="hk",
            cash_amount=1100,
            price=11.0,
            source="alphasift_auto",
        )
        self.assertTrue(existing["accepted"])
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_market": "hk",
                "auto_cash_per_order": 1000,
                "auto_daily_budget": 1500,
                "auto_max_results": 1,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "quality_status": "ok",
            "candidates": [{"code": "00700", "name": "Tencent", "score": 80, "price": 11.0}],
            "warnings": [],
            "source_errors": [],
        }

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ):
            result = self.service.run_auto_trade_once()

        self.assertEqual(result["orders"][0]["reason"], "daily_budget_exceeded")

    def test_daily_budget_fails_closed_when_historical_fx_conversion_raises(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_daily_budget": 1500,
            }
        )
        existing = self.service.submit_order(
            symbol="600519",
            side="buy",
            market="cn",
            quantity=100,
            price=10.0,
            source="alphasift_auto",
        )
        self.assertTrue(existing["accepted"])

        with patch.object(
            self.service.portfolio,
            "convert_amount",
            side_effect=ValueError("invalid fx payload"),
        ):
            usage = self.service._daily_auto_trade_usage(self.service.get_settings())

        self.assertTrue(usage["fx_unavailable"])
        self.assertEqual(usage["order_count"], 1.0)
        self.assertEqual(usage["cash_amount"], 1500.0)

    def test_performance_summary_aggregates_account_and_agent_runs(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_strategy": "dual_low",
                "auto_max_results": 1,
                "auto_cash_per_order": 1200,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "quality_status": "ok",
            "candidates": [
                {"code": "600519", "score": 80, "price": 10.0, "amount": 200000000, "industry": "白酒"},
            ],
            "warnings": [],
            "source_errors": [],
        }

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ), patch(
            "src.services.portfolio_service.PortfolioService._fetch_realtime_position_price",
            return_value=(10.0, "unit-test"),
        ):
            self.service.run_auto_trade_once()
        self.service.submit_order(
            symbol="600519",
            side="sell",
            market="cn",
            quantity=100,
            price=12.0,
        )
        performance = self.service.get_performance_summary(run_limit=10)

        self.assertEqual(performance["initial_cash"], 100000.0)
        self.assertEqual(performance["total_equity"], 100200.0)
        self.assertEqual(performance["total_pnl"], 200.0)
        self.assertEqual(performance["return_pct"], 0.2)
        self.assertEqual(performance["run_window"]["run_count"], 1)
        self.assertEqual(performance["agent"]["candidate_count"], 1)
        self.assertEqual(performance["agent"]["submitted_count"], 1)
        self.assertEqual(performance["agent"]["fill_rate_pct"], 100.0)
        self.assertEqual(performance["trade_metrics"]["trade_count"], 2)
        self.assertEqual(performance["trade_metrics"]["buy_count"], 1)
        self.assertEqual(performance["trade_metrics"]["sell_count"], 1)
        self.assertEqual(performance["trade_metrics"]["gross_turnover"], 2200.0)
        self.assertEqual(performance["trade_metrics"]["win_rate_pct"], 100.0)
        self.assertEqual(performance["trade_metrics"]["average_sell_return_pct"], 20.0)
        self.assertEqual(performance["trade_metrics"]["realized_trade_pnl"], 200.0)
        self.assertEqual(performance["risk_metrics"]["turnover_pct"], 2.2)
        self.assertEqual(performance["risk_metrics"]["max_drawdown_pct"], 0.0)
        self.assertEqual(performance["risk_metrics"]["current_exposure_pct"], 0.0)
        self.assertGreaterEqual(performance["risk_metrics"]["curve_point_count"], 3)
        self.assertEqual(performance["equity_curve"][-1]["source"], "snapshot")
        self.assertGreaterEqual(len(performance["daily_returns"]), 1)
        self.assertEqual(performance["daily_returns"][-1]["equity"], 100200.0)
        self.assertEqual(performance["daily_returns"][-1]["cumulative_return_pct"], 0.2)
        self.assertEqual(performance["daily_returns"][-1]["trade_count"], 2)
        self.assertGreaterEqual(len(performance["monthly_returns"]), 1)
        self.assertEqual(performance["monthly_returns"][-1]["end_equity"], 100200.0)
        self.assertEqual(performance["monthly_returns"][-1]["cumulative_return_pct"], 0.2)
        self.assertEqual(performance["monthly_returns"][-1]["trade_count"], 2)
        self.assertEqual(performance["strategy_attribution"][0]["key"], "dual_low")
        self.assertEqual(performance["strategy_attribution"][0]["filled_plan_count"], 1)
        self.assertEqual(performance["strategy_attribution"][0]["filled_cash_amount"], 1000.0)
        self.assertEqual(performance["industry_attribution"][0]["key"], "白酒")
        self.assertEqual(performance["industry_attribution"][0]["filled_count"], 1)
        self.assertEqual(performance["industry_attribution"][0]["filled_cash_amount"], 1000.0)
        self.assertEqual(performance["trade_plan_status_counts"], {"filled": 1})
        self.assertEqual(performance["traded_symbols"][0], {"key": "600519", "count": 1})

        future_window = self.service.get_performance_summary(
            run_limit=10,
            created_from=datetime.now(timezone.utc) + timedelta(days=1),
        )
        self.assertEqual(future_window["run_window"]["run_count"], 0)
        self.assertEqual(future_window["run_window"]["total"], 0)
        self.assertIsNotNone(future_window["run_window"]["created_from"])
        self.assertEqual(future_window["agent"]["candidate_count"], 0)
        self.assertEqual(future_window["agent"]["submitted_count"], 0)
        self.assertEqual(future_window["strategy_attribution"], [])
        self.assertEqual(future_window["industry_attribution"], [])

    def test_trade_plan_recovery_summary_flags_stale_and_retryable_plans(self) -> None:
        run = self.service.agent_repo.create_run(
            run_uid="recovery-summary-run",
            trigger_source="unit-test",
            strategy="dual_low",
            market="cn",
            settings={},
            diagnostics={},
        )
        stale_plan = self.service.agent_repo.record_trade_plan(
            plan_uid="recovery-stale-submitted",
            run_id=int(run["id"]),
            decision_id=None,
            symbol="600519",
            market="cn",
            side="buy",
            status="submitted",
            execution_mode="vnpy_paper",
            planned_cash_amount=1000,
            planned_quantity=100,
            planned_price=10,
            submitted_quantity=100,
            submitted_price=10,
            order_result={"status": "submitted", "raw": {"vt_orderid": "GATEWAY.1"}},
        )
        self.service.agent_repo.record_trade_plan(
            plan_uid="recovery-retryable-skipped",
            run_id=int(run["id"]),
            decision_id=None,
            symbol="000001",
            market="cn",
            side="buy",
            status="skipped",
            execution_mode="paper",
            planned_cash_amount=1000,
            planned_quantity=100,
            planned_price=10,
            skip_reason="price_unavailable",
            order_result={"reason": "price_unavailable"},
        )
        with DatabaseManager.get_instance().get_session() as session:
            session.execute(
                text(
                    f"UPDATE {StockSelectionAgentTradePlan.__tablename__} "
                    "SET updated_at = :updated_at WHERE id = :id"
                ),
                {
                    "updated_at": datetime.now() - timedelta(minutes=45),
                    "id": int(stale_plan["id"]),
                },
            )
            session.commit()

        summary = self.service.get_trade_plan_recovery_summary(limit=10)

        self.assertEqual(summary["scanned_count"], 2)
        self.assertEqual(summary["status_counts"], {"skipped": 1, "submitted": 1})
        self.assertEqual(summary["stale_active_count"], 1)
        self.assertEqual(summary["cancellable_count"], 1)
        self.assertEqual(summary["retry_due_count"], 1)
        states = {item["plan_uid"]: item for item in summary["items"]}
        self.assertTrue(states["recovery-stale-submitted"]["stale_active"])
        self.assertEqual(states["recovery-stale-submitted"]["recovery_state"], "stale_active")
        self.assertEqual(states["recovery-stale-submitted"]["stale_reason"], "vnpy_order_timeout_candidate")
        self.assertTrue(states["recovery-retryable-skipped"]["retry_due"])
        self.assertEqual(states["recovery-retryable-skipped"]["recovery_state"], "retry_due")

    def test_expire_stale_vnpy_partial_fill_requires_manual_reconciliation(self) -> None:
        run = self.service.agent_repo.create_run(
            run_uid="recovery-partial-fill-run",
            trigger_source="vnpy_paper_auto",
            strategy="dual_low",
            market="cn",
            settings={},
            diagnostics={},
        )
        partial_plan = self.service.agent_repo.record_trade_plan(
            plan_uid="recovery-partial-fill-plan",
            run_id=int(run["id"]),
            decision_id=None,
            symbol="600519",
            market="cn",
            side="buy",
            status="part_filled",
            execution_mode="vnpy_paper",
            planned_cash_amount=1000,
            planned_quantity=100,
            planned_price=10,
            submitted_quantity=40,
            submitted_price=10.1,
            order_result={
                "status": "part_filled",
                "reason": "vnpy_order_partially_filled_waiting_trade_callback",
                "raw": {"vt_orderid": "GATEWAY.PARTIAL"},
            },
        )
        with DatabaseManager.get_instance().get_session() as session:
            session.execute(
                text(
                    f"UPDATE {StockSelectionAgentTradePlan.__tablename__} "
                    "SET updated_at = :updated_at WHERE id = :id"
                ),
                {
                    "updated_at": datetime.now() - timedelta(minutes=45),
                    "id": int(partial_plan["id"]),
                },
            )
            session.commit()

        result = self.service.expire_stale_vnpy_trade_plans(max_plans=3, scan_limit=10)
        detail = self.service.agent_repo.get_run_detail("recovery-partial-fill-run")
        recovery = self.service.get_trade_plan_recovery_summary(limit=10)

        self.assertEqual(result["expired_count"], 1)
        self.assertIn("expired_vnpy_orders:1", result["messages"])
        self.assertIsNotNone(detail)
        assert detail is not None
        plan = detail["trade_plans"][0]
        self.assertEqual(plan["status"], "failed")
        self.assertEqual(plan["skip_reason"], "vnpy_partial_fill_timeout")
        self.assertEqual(plan["order_result"]["reason"], "vnpy_partial_fill_timeout")
        self.assertEqual(plan["order_result"]["raw"]["timeout"]["previous_status"], "part_filled")
        states = {item["plan_uid"]: item for item in recovery["items"]}
        self.assertEqual(states["recovery-partial-fill-plan"]["recovery_state"], "not_retryable")
        self.assertFalse(states["recovery-partial-fill-plan"]["retryable"])
        self.assertEqual(
            states["recovery-partial-fill-plan"]["retry_block_reason"],
            "trade_plan_not_retryable_reason",
        )

    def test_auto_trade_skips_symbol_blacklist(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_strategy": "dual_low",
                "auto_max_results": 1,
                "auto_cash_per_order": 1200,
                "auto_symbol_blacklist": ["600519"],
                "auto_trade_time_gate_enabled": False,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "candidates": [
                {"code": "600519", "score": 80, "price": 10.0},
            ],
            "warnings": [],
        }

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ):
            result = self.service.run_auto_trade_once()

        self.assertTrue(result["accepted"])
        self.assertEqual(result["submitted_count"], 0)
        self.assertEqual(result["skipped_count"], 1)
        self.assertEqual(result["orders"][0]["reason"], "symbol_blacklisted")
        audit = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        self.assertIsNotNone(audit)
        assert audit is not None
        self.assertEqual(audit["settings"]["auto_symbol_blacklist"], ["600519"])
        self.assertEqual(audit["decisions"][0]["reason"], "symbol_blacklisted")
        self.assertEqual(audit["trade_plans"][0]["skip_reason"], "symbol_blacklisted")

    def test_auto_trade_filters_candidate_pre_trade_risks(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_strategy": "dual_low",
                "auto_max_results": 4,
                "auto_cash_per_order": 1200,
                "auto_trade_time_gate_enabled": False,
                "auto_min_turnover": 100000000,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "candidates": [
                {"code": "600519", "name": "ST测试", "score": 80, "price": 10.0, "amount": 200000000},
                {"code": "000001", "score": 80, "price": 10.0, "is_suspended": True, "amount": 200000000},
                {"code": "300750", "score": 80, "price": 20.0, "limit_up_price": 20.0, "amount": 200000000},
                {"code": "002594", "score": 80, "price": 10.0, "amount": "5000万"},
            ],
            "warnings": [],
        }

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ):
            result = self.service.run_auto_trade_once()

        self.assertTrue(result["accepted"])
        self.assertEqual(result["submitted_count"], 0)
        self.assertEqual(result["skipped_count"], 4)
        self.assertEqual(
            [order["reason"] for order in result["orders"]],
            [
                "st_or_delisting_risk",
                "suspended_stock",
                "price_limit_reached",
                "liquidity_below_threshold",
            ],
        )

        audit = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        self.assertIsNotNone(audit)
        assert audit is not None
        self.assertEqual(audit["settings"]["auto_min_turnover"], 100000000)
        self.assertEqual(
            [decision["reason"] for decision in audit["decisions"]],
            [
                "st_or_delisting_risk",
                "suspended_stock",
                "price_limit_reached",
                "liquidity_below_threshold",
            ],
        )
        self.assertEqual(
            [plan["skip_reason"] for plan in audit["trade_plans"]],
            [
                "st_or_delisting_risk",
                "suspended_stock",
                "price_limit_reached",
                "liquidity_below_threshold",
            ],
        )

    def test_auto_trade_fails_closed_for_required_candidate_fields(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_strategy": "dual_low",
                "auto_max_results": 2,
                "auto_cash_per_order": 1200,
                "auto_trade_time_gate_enabled": False,
                "auto_min_turnover": 100000000,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "candidates": [
                {
                    "code": "600519",
                    "score": 80,
                    "price": 10.0,
                    "amount": 200000000,
                    "data_quality": "partial",
                    "missing_fields": ["trading_status"],
                    "data_sources": ["snapshot"],
                },
                {
                    "code": "000001",
                    "score": 80,
                    "price": 10.0,
                    "is_st": False,
                    "is_suspended": False,
                    "is_limit_up": False,
                    "data_quality": "partial",
                    "missing_fields": ["amount"],
                    "data_sources": ["snapshot"],
                },
            ],
            "warnings": [],
        }

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ):
            result = self.service.run_auto_trade_once()

        self.assertEqual(result["submitted_count"], 0)
        self.assertEqual(result["skipped_count"], 2)
        self.assertEqual(
            [order["reason"] for order in result["orders"]],
            ["candidate_trading_status_unavailable", "liquidity_data_unavailable"],
        )
        audit = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        self.assertIsNotNone(audit)
        assert audit is not None
        self.assertEqual(
            [decision["reason"] for decision in audit["decisions"]],
            ["candidate_trading_status_unavailable", "liquidity_data_unavailable"],
        )
        self.assertEqual(
            audit["decisions"][0]["order_result"]["risk_review"]["candidate_data_quality"][
                "missing_fields"
            ],
            ["trading_status"],
        )

    def test_auto_trade_respects_account_cash_low_watermark(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_strategy": "dual_low",
                "auto_max_results": 1,
                "auto_cash_per_order": 1200,
                "auto_trade_time_gate_enabled": False,
                "auto_min_cash_balance": 1000,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "candidates": [
                {"code": "600519", "name": "贵州茅台", "score": 80, "price": 10.0},
            ],
            "warnings": [],
        }
        snapshot = {
            "total_cash": 500,
            "total_equity": 100000,
            "accounts": [{"total_cash": 500, "total_equity": 100000, "positions": []}],
        }

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ), patch.object(
            self.service.portfolio,
            "get_portfolio_snapshot",
            return_value=snapshot,
        ):
            result = self.service.run_auto_trade_once()

        self.assertEqual(result["submitted_count"], 0)
        self.assertEqual(result["skipped_count"], 1)
        self.assertEqual(result["orders"][0]["reason"], "cash_low_watermark")
        audit = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        self.assertIsNotNone(audit)
        assert audit is not None
        self.assertEqual(audit["settings"]["auto_min_cash_balance"], 1000)
        self.assertEqual(audit["decisions"][0]["reason"], "cash_low_watermark")
        self.assertEqual(audit["trade_plans"][0]["skip_reason"], "cash_low_watermark")
        triggers = AlertService().list_triggers(target="vnpy_paper", status="triggered", page_size=10)["items"]
        self.assertEqual(len(triggers), 1)
        self.assertEqual(triggers[0]["reason"], "cash_low_watermark")
        self.assertEqual(triggers[0]["observed_value"], 500.0)
        self.assertEqual(triggers[0]["threshold"], 1000.0)
        self.assertIn('"event_type": "cash_low_watermark"', triggers[0]["diagnostics"])
        self.assertIn('"account_risk"', triggers[0]["diagnostics"])

    def test_auto_trade_respects_account_drawdown_limit(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_strategy": "dual_low",
                "auto_max_results": 1,
                "auto_cash_per_order": 1200,
                "auto_trade_time_gate_enabled": False,
                "auto_max_drawdown_pct": 10,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "candidates": [
                {"code": "600519", "name": "贵州茅台", "score": 80, "price": 10.0},
            ],
            "warnings": [],
        }
        snapshot = {
            "total_cash": 10000,
            "total_equity": 85000,
            "accounts": [{"total_cash": 10000, "total_equity": 85000, "positions": []}],
        }

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ), patch.object(
            self.service.portfolio,
            "get_portfolio_snapshot",
            return_value=snapshot,
        ):
            result = self.service.run_auto_trade_once()

        self.assertEqual(result["submitted_count"], 0)
        self.assertEqual(result["skipped_count"], 1)
        self.assertEqual(result["orders"][0]["reason"], "account_drawdown_limit_reached")
        audit = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        self.assertIsNotNone(audit)
        assert audit is not None
        self.assertEqual(audit["settings"]["auto_max_drawdown_pct"], 10)
        self.assertEqual(audit["decisions"][0]["reason"], "account_drawdown_limit_reached")
        self.assertEqual(audit["trade_plans"][0]["skip_reason"], "account_drawdown_limit_reached")
        triggers = AlertService().list_triggers(target="vnpy_paper", status="triggered", page_size=10)["items"]
        self.assertEqual(len(triggers), 1)
        self.assertEqual(triggers[0]["reason"], "account_drawdown_limit_reached")
        self.assertEqual(triggers[0]["observed_value"], 15.0)
        self.assertEqual(triggers[0]["threshold"], 10.0)
        self.assertIn('"event_type": "account_drawdown_limit_reached"', triggers[0]["diagnostics"])
        self.assertIn('"account_risk"', triggers[0]["diagnostics"])

    def test_account_drawdown_uses_persisted_observed_equity_peak(self) -> None:
        self.service.update_settings({"auto_max_drawdown_pct": 10})
        settings = self.service.get_settings()
        peak_snapshot = {
            "total_equity": 120000,
            "accounts": [{"total_equity": 120000, "positions": []}],
        }
        with patch.object(
            self.service.portfolio,
            "get_portfolio_snapshot",
            return_value=peak_snapshot,
        ):
            reason, diagnostics = self.service._account_pre_trade_risk(settings)

        self.assertIsNone(reason)
        self.assertEqual(diagnostics["basis"], "observed_equity_peak")
        self.assertEqual(diagnostics["peak_equity"], 120000.0)
        self.assertEqual(diagnostics["drawdown_pct"], 0.0)

        reconstructed = VnpyPaperTradingService(
            data_fetcher_manager=_FakeDataFetcherManager(price=10.0),
            config_path=self.config_path,
        )
        lower_snapshot = {
            "total_equity": 105000,
            "accounts": [{"total_equity": 105000, "positions": []}],
        }
        with patch.object(
            reconstructed.portfolio,
            "get_portfolio_snapshot",
            return_value=lower_snapshot,
        ):
            reason, diagnostics = reconstructed._account_pre_trade_risk(
                reconstructed.get_settings()
            )

        self.assertEqual(reason, "account_drawdown_limit_reached")
        self.assertEqual(diagnostics["peak_equity"], 120000.0)
        self.assertEqual(diagnostics["equity"], 105000.0)
        self.assertEqual(diagnostics["drawdown_pct"], 12.5)

    def test_account_drawdown_guard_persists_hysteresis_and_records_recovery(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_max_drawdown_pct": 10,
                "auto_drawdown_recovery_hysteresis_pct": 2,
                "auto_max_results": 1,
                "auto_cash_per_order": 1200,
            }
        )
        settings = self.service.get_settings()
        trigger_snapshot = {
            "total_cash": 89000,
            "total_equity": 89000,
            "accounts": [{"total_cash": 89000, "total_equity": 89000, "positions": []}],
        }
        with patch.object(
            self.service.portfolio,
            "get_portfolio_snapshot",
            return_value=trigger_snapshot,
        ):
            reason, diagnostics = self.service._account_pre_trade_risk(settings)

        self.assertEqual(reason, "account_drawdown_limit_reached")
        self.assertTrue(diagnostics["drawdown_guard_latched"])
        self.assertEqual(diagnostics["drawdown_guard_transition"], "opened")
        self.assertEqual(diagnostics["recovery_threshold_pct"], 8.0)

        reconstructed = VnpyPaperTradingService(
            data_fetcher_manager=_FakeDataFetcherManager(price=10.0),
            config_path=self.config_path,
        )
        partial_snapshot = {
            "total_cash": 91000,
            "total_equity": 91000,
            "accounts": [{"total_cash": 91000, "total_equity": 91000, "positions": []}],
        }
        with patch.object(
            reconstructed.portfolio,
            "get_portfolio_snapshot",
            return_value=partial_snapshot,
        ):
            reason, diagnostics = reconstructed._account_pre_trade_risk(
                reconstructed.get_settings()
            )

        self.assertEqual(reason, "account_drawdown_limit_reached")
        self.assertEqual(diagnostics["drawdown_pct"], 9.0)
        self.assertTrue(diagnostics["drawdown_guard_latched"])
        self.assertIsNone(diagnostics["drawdown_guard_transition"])

        recovered_snapshot = {
            "total_cash": 93000,
            "total_equity": 93000,
            "accounts": [{"total_cash": 93000, "total_equity": 93000, "positions": []}],
        }
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "candidates": [
                {"code": "600519", "name": "贵州茅台", "score": 80, "price": 10.0},
            ],
            "warnings": [],
        }
        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ), patch.object(
            reconstructed.portfolio,
            "get_portfolio_snapshot",
            return_value=recovered_snapshot,
        ):
            result = reconstructed.run_auto_trade_once()

        audit = reconstructed.agent_repo.get_run_detail(result["agent_run_uid"])
        self.assertIsNotNone(audit)
        assert audit is not None
        account_risk = audit["diagnostics"]["account_risk"]
        self.assertEqual(account_risk["drawdown_pct"], 7.0)
        self.assertFalse(account_risk["drawdown_guard_latched"])
        self.assertEqual(account_risk["drawdown_guard_transition"], "recovered")
        self.assertIsNotNone(account_risk["drawdown_guard_last_recovered_at"])
        triggers = AlertService().list_triggers(
            target="vnpy_paper",
            status="resolved",
            page_size=10,
        )["items"]
        self.assertEqual(len(triggers), 1)
        self.assertEqual(triggers[0]["reason"], "account_drawdown_recovered")
        self.assertEqual(triggers[0]["observed_value"], 7.0)
        self.assertEqual(triggers[0]["threshold"], 8.0)

    def test_account_drawdown_recovery_hysteresis_is_clamped_to_limit(self) -> None:
        self.service.update_settings(
            {
                "auto_max_drawdown_pct": 10,
                "auto_drawdown_recovery_hysteresis_pct": 20,
            }
        )
        snapshot = {
            "total_equity": 100000,
            "accounts": [{"total_equity": 100000, "positions": []}],
        }
        with patch.object(
            self.service.portfolio,
            "get_portfolio_snapshot",
            return_value=snapshot,
        ):
            reason, diagnostics = self.service._account_pre_trade_risk(
                self.service.get_settings()
            )

        self.assertIsNone(reason)
        self.assertEqual(diagnostics["recovery_hysteresis_pct"], 10.0)
        self.assertEqual(diagnostics["recovery_threshold_pct"], 0.0)

    def test_consecutive_loss_guard_blocks_buys_and_recovers_after_cooldown(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_max_results": 1,
                "auto_cash_per_order": 1200,
                "auto_consecutive_loss_limit": 2,
                "auto_consecutive_loss_cooldown_minutes": 60,
            }
        )
        for symbol in ("600519", "000001"):
            bought = self.service.submit_order(
                symbol=symbol,
                side="buy",
                market="cn",
                quantity=100,
                price=10.0,
            )
            sold = self.service.submit_order(
                symbol=symbol,
                side="sell",
                market="cn",
                quantity=100,
                price=9.0,
            )
            self.assertTrue(bought["accepted"])
            self.assertTrue(sold["accepted"])

        performance = self.service.get_performance_summary(run_limit=10)
        metrics = performance["trade_metrics"]
        self.assertEqual(metrics["current_consecutive_loss_count"], 2)
        self.assertEqual(metrics["max_consecutive_loss_count"], 2)
        self.assertEqual(metrics["last_closed_trade_pnl"], -100.0)

        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "quality_status": "ok",
            "candidates": [
                {"code": "300750", "name": "宁德时代", "score": 80, "price": 10.0},
            ],
            "warnings": [],
            "source_errors": [],
        }
        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ):
            blocked = self.service.run_auto_trade_once()

        self.assertEqual(blocked["submitted_count"], 0)
        self.assertEqual(blocked["orders"][0]["reason"], "consecutive_loss_limit_reached")
        blocked_audit = self.service.agent_repo.get_run_detail(blocked["agent_run_uid"])
        self.assertIsNotNone(blocked_audit)
        assert blocked_audit is not None
        loss_diagnostics = blocked_audit["diagnostics"]["account_risk"]["consecutive_losses"]
        self.assertEqual(loss_diagnostics["status"], "cooling_down")
        self.assertEqual(loss_diagnostics["guard_transition"], "opened")
        opened = AlertService().list_triggers(
            target="vnpy_paper",
            status="triggered",
            page_size=10,
        )["items"]
        self.assertEqual(len(opened), 1)
        self.assertEqual(opened[0]["reason"], "consecutive_loss_limit_reached")

        future = datetime.now(timezone.utc) + timedelta(minutes=61)
        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ), patch.object(self.service, "_now_utc", return_value=future):
            recovered = self.service.run_auto_trade_once()

        self.assertEqual(recovered["submitted_count"], 1)
        recovered_audit = self.service.agent_repo.get_run_detail(recovered["agent_run_uid"])
        self.assertIsNotNone(recovered_audit)
        assert recovered_audit is not None
        recovered_losses = recovered_audit["diagnostics"]["account_risk"]["consecutive_losses"]
        self.assertEqual(recovered_losses["status"], "cooldown_elapsed")
        self.assertEqual(recovered_losses["guard_transition"], "recovered")
        resolved = AlertService().list_triggers(
            target="vnpy_paper",
            status="resolved",
            page_size=10,
        )["items"]
        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0]["reason"], "consecutive_loss_cooldown_elapsed")

    def test_consecutive_loss_guard_resets_after_profitable_close(self) -> None:
        self.service.update_settings(
            {
                "auto_consecutive_loss_limit": 1,
                "auto_consecutive_loss_cooldown_minutes": 1440,
            }
        )
        self.service.submit_order(
            symbol="600519",
            side="buy",
            market="cn",
            quantity=100,
            price=10.0,
        )
        self.service.submit_order(
            symbol="600519",
            side="sell",
            market="cn",
            quantity=100,
            price=9.0,
        )
        reason, opened = self.service._account_pre_trade_risk(self.service.get_settings())
        self.assertEqual(reason, "consecutive_loss_limit_reached")
        self.assertEqual(opened["consecutive_losses"]["guard_transition"], "opened")

        self.service.submit_order(
            symbol="000001",
            side="buy",
            market="cn",
            quantity=100,
            price=10.0,
        )
        self.service.submit_order(
            symbol="000001",
            side="sell",
            market="cn",
            quantity=100,
            price=11.0,
        )
        reason, recovered = self.service._account_pre_trade_risk(self.service.get_settings())

        self.assertIsNone(reason)
        self.assertEqual(recovered["consecutive_losses"]["current_streak"], 0)
        self.assertEqual(recovered["consecutive_losses"]["guard_transition"], "recovered")
        self.assertEqual(
            recovered["consecutive_losses"]["recovery_reason"],
            "consecutive_loss_streak_reset",
        )

    def test_auto_trade_respects_market_light_gate(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_strategy": "dual_low",
                "auto_max_results": 1,
                "auto_cash_per_order": 1200,
                "auto_trade_time_gate_enabled": False,
                "auto_market_light_gate_enabled": True,
                "auto_market_light_block_statuses": ["red", "yellow"],
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "candidates": [
                {"code": "600519", "name": "贵州茅台", "score": 80, "price": 10.0},
            ],
            "warnings": [],
        }

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ), patch(
            "src.services.vnpy_paper_trading_service.load_previous_snapshot",
            return_value={"region": "cn", "trade_date": date.today().isoformat(), "status": "yellow", "score": 45},
        ):
            result = self.service.run_auto_trade_once()

        self.assertEqual(result["submitted_count"], 0)
        self.assertEqual(result["skipped_count"], 1)
        self.assertEqual(result["orders"][0]["reason"], "market_light_yellow")
        audit = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        self.assertIsNotNone(audit)
        assert audit is not None
        self.assertEqual(audit["settings"]["auto_market_light_block_statuses"], ["red", "yellow"])
        self.assertEqual(audit["decisions"][0]["reason"], "market_light_yellow")
        self.assertEqual(audit["trade_plans"][0]["skip_reason"], "market_light_yellow")

    def test_auto_trade_respects_market_breadth_gate_and_audits_snapshot(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_strategy": "dual_low",
                "auto_max_results": 1,
                "auto_cash_per_order": 1200,
                "auto_trade_time_gate_enabled": False,
                "auto_market_breadth_gate_enabled": True,
                "auto_market_breadth_min_score": 40,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "candidates": [
                {"code": "600519", "name": "贵州茅台", "score": 80, "price": 10.0},
            ],
            "warnings": [],
        }
        snapshot = {
            "region": "cn",
            "trade_date": date.today().isoformat(),
            "status": "green",
            "score": 65,
            "dimensions": {
                "breadth": {"score": 28, "available": True},
                "index": {"score": 70, "available": True},
                "limit": {"score": 80, "available": True},
            },
        }

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ), patch(
            "src.services.vnpy_paper_trading_service.load_previous_snapshot",
            return_value=snapshot,
        ):
            result = self.service.run_auto_trade_once()

        self.assertEqual(result["submitted_count"], 0)
        self.assertEqual(result["orders"][0]["reason"], "market_breadth_below_threshold")
        audit = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        assert audit is not None
        diagnostics = audit["diagnostics"]["market_context_risk"]
        self.assertEqual(diagnostics["status"], "blocked")
        self.assertEqual(diagnostics["market_breadth"]["score"], 28)
        self.assertEqual(diagnostics["market_breadth"]["min_score"], 40)
        self.assertEqual(audit["decisions"][0]["reason"], "market_breadth_below_threshold")
        timeline = next(
            item for item in audit["timeline"] if item["stage"] == "market_context_risk"
        )
        self.assertEqual(timeline["status"], "blocked")
        self.assertIn("breadth=28", timeline["message"])
        self.assertIn("age_days=0", timeline["message"])
        plan = audit["diagnostics"]["agent_plan"]
        self.assertEqual(plan["gates"]["market_context_max_age_days"], 7)
        self.assertIn(
            "market_context_freshness",
            plan["adaptive_controls"]["configured_layers"],
        )
        stale_action = next(
            item
            for item in plan["adaptive_controls"]["degrade_actions"]
            if item["reason"] == "market_context_stale"
        )
        self.assertEqual(stale_action["max_age_days"], 7)

    def test_auto_trade_respects_hotspot_retreat_gate(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_strategy": "dual_low",
                "auto_max_results": 1,
                "auto_cash_per_order": 1200,
                "auto_trade_time_gate_enabled": False,
                "auto_hotspot_retreat_gate_enabled": True,
                "auto_hotspot_retreat_min_drop": 25,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "candidates": [
                {"code": "600519", "name": "贵州茅台", "score": 80, "price": 10.0},
            ],
            "warnings": [],
        }
        current = {
            "region": "cn",
            "trade_date": date.today().isoformat(),
            "status": "green",
            "score": 62,
            "dimensions": {
                "breadth": {"score": 58, "available": True},
                "index": {"score": 65, "available": True},
                "limit": {"score": 45, "available": True},
            },
        }
        previous = {
            "region": "cn",
            "trade_date": (date.today() - timedelta(days=1)).isoformat(),
            "status": "green",
            "score": 78,
            "dimensions": {
                "breadth": {"score": 70, "available": True},
                "index": {"score": 75, "available": True},
                "limit": {"score": 80, "available": True},
            },
        }

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ), patch(
            "src.services.vnpy_paper_trading_service.load_previous_snapshot",
            side_effect=[current, previous],
        ):
            result = self.service.run_auto_trade_once()

        self.assertEqual(result["submitted_count"], 0)
        self.assertEqual(result["orders"][0]["reason"], "hotspot_retreat_detected")
        audit = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        assert audit is not None
        retreat = audit["diagnostics"]["market_context_risk"]["hotspot_retreat"]
        self.assertEqual(retreat["current_score"], 45)
        self.assertEqual(retreat["previous_score"], 80)
        self.assertEqual(retreat["score_drop"], 35)
        self.assertEqual(retreat["proxy"], "market_light_limit_dimension")

    def test_hotspot_retreat_gate_fails_closed_without_previous_snapshot(self) -> None:
        settings = replace(
            self.service.get_settings(),
            auto_hotspot_retreat_gate_enabled=True,
            auto_hotspot_retreat_min_drop=25,
        )
        current = {
            "region": "cn",
            "trade_date": date.today().isoformat(),
            "status": "green",
            "score": 62,
            "dimensions": {
                "breadth": {"score": 58, "available": True},
                "index": {"score": 65, "available": True},
                "limit": {"score": 45, "available": True},
            },
        }

        with patch(
            "src.services.vnpy_paper_trading_service.load_previous_snapshot",
            side_effect=[current, None],
        ):
            reason, diagnostics = self.service._market_context_pre_trade_risk(settings)

        self.assertEqual(reason, "hotspot_retreat_unavailable")
        self.assertEqual(diagnostics["status"], "blocked")
        self.assertIsNone(diagnostics["hotspot_retreat"]["previous_score"])

    def test_market_breadth_gate_fails_closed_without_latest_snapshot(self) -> None:
        settings = replace(
            self.service.get_settings(),
            auto_market_breadth_gate_enabled=True,
        )

        with patch(
            "src.services.vnpy_paper_trading_service.load_previous_snapshot",
            return_value=None,
        ):
            reason, diagnostics = self.service._market_context_pre_trade_risk(settings)

        self.assertEqual(reason, "market_breadth_unavailable")
        self.assertEqual(diagnostics["status"], "unavailable")
        self.assertEqual(diagnostics["reason"], "market_breadth_unavailable")
        self.assertEqual(diagnostics["evidence_reason"], "market_context_missing")

    def test_market_light_gate_fails_closed_without_latest_snapshot(self) -> None:
        settings = replace(
            self.service.get_settings(),
            auto_market_light_gate_enabled=True,
        )

        with patch(
            "src.services.vnpy_paper_trading_service.load_previous_snapshot",
            return_value=None,
        ):
            reason, diagnostics = self.service._market_context_pre_trade_risk(settings)

        self.assertEqual(reason, "market_context_unavailable")
        self.assertEqual(diagnostics["status"], "unavailable")
        self.assertEqual(diagnostics["evidence_reason"], "market_context_missing")

    def test_market_context_gate_rejects_stale_snapshot(self) -> None:
        settings = replace(
            self.service.get_settings(),
            auto_market_light_gate_enabled=True,
            auto_market_context_max_age_days=7,
        )
        snapshot = {
            "region": "cn",
            "trade_date": (date.today() - timedelta(days=8)).isoformat(),
            "status": "green",
            "score": 70,
            "dimensions": {
                "breadth": {"score": 70, "available": True},
                "index": {"score": 70, "available": True},
                "limit": {"score": 70, "available": True},
            },
        }

        with patch(
            "src.services.vnpy_paper_trading_service.load_previous_snapshot",
            return_value=snapshot,
        ):
            reason, diagnostics = self.service._market_context_pre_trade_risk(settings)

        self.assertEqual(reason, "market_context_stale")
        self.assertEqual(diagnostics["status"], "blocked")
        self.assertEqual(diagnostics["freshness"]["status"], "stale")
        self.assertEqual(diagnostics["freshness"]["age_days"], 8)
        self.assertEqual(diagnostics["freshness"]["max_age_days"], 7)

    def test_market_context_gate_rejects_invalid_snapshot_date(self) -> None:
        settings = replace(
            self.service.get_settings(),
            auto_market_light_gate_enabled=True,
        )
        snapshot = {
            "region": "cn",
            "trade_date": "not-a-date",
            "status": "green",
            "score": 70,
        }

        with patch(
            "src.services.vnpy_paper_trading_service.load_previous_snapshot",
            return_value=snapshot,
        ):
            reason, diagnostics = self.service._market_context_pre_trade_risk(settings)

        self.assertEqual(reason, "market_context_unavailable")
        self.assertEqual(diagnostics["status"], "unavailable")
        self.assertEqual(diagnostics["evidence_reason"], "market_context_trade_date_invalid")
        self.assertEqual(diagnostics["freshness"]["status"], "invalid")

    def test_market_context_gate_rejects_future_snapshot_date(self) -> None:
        settings = replace(
            self.service.get_settings(),
            auto_market_light_gate_enabled=True,
        )
        snapshot = {
            "region": "cn",
            "trade_date": (date.today() + timedelta(days=1)).isoformat(),
            "status": "green",
            "score": 70,
        }

        with patch(
            "src.services.vnpy_paper_trading_service.load_previous_snapshot",
            return_value=snapshot,
        ):
            reason, diagnostics = self.service._market_context_pre_trade_risk(settings)

        self.assertEqual(reason, "market_context_unavailable")
        self.assertEqual(diagnostics["status"], "unavailable")
        self.assertEqual(diagnostics["evidence_reason"], "market_context_trade_date_future")
        self.assertEqual(diagnostics["freshness"]["status"], "invalid")

    def test_market_context_freshness_setting_survives_service_restart(self) -> None:
        self.service.update_settings(
            {
                "auto_market_light_gate_enabled": True,
                "auto_market_context_max_age_days": 3,
            }
        )
        restarted = VnpyPaperTradingService(
            data_fetcher_manager=_FakeDataFetcherManager(price=10.0),
            config_path=self.config_path,
        )
        settings = restarted.get_settings()
        snapshot = {
            "region": "cn",
            "trade_date": (date.today() - timedelta(days=4)).isoformat(),
            "status": "green",
            "score": 70,
            "dimensions": {},
        }

        with patch(
            "src.services.vnpy_paper_trading_service.load_previous_snapshot",
            return_value=snapshot,
        ):
            reason, diagnostics = restarted._market_context_pre_trade_risk(settings)

        self.assertEqual(settings.auto_market_context_max_age_days, 3)
        self.assertEqual(reason, "market_context_stale")
        self.assertEqual(diagnostics["freshness"]["age_days"], 4)
        self.assertEqual(diagnostics["freshness"]["max_age_days"], 3)

    def test_auto_trade_failure_fuse_skips_after_consecutive_failed_runs(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_strategy": "dual_low",
                "auto_market": "cn",
                "auto_failure_fuse_enabled": True,
                "auto_failure_fuse_threshold": 2,
            }
        )
        for index in range(2):
            run = self.service.agent_repo.create_run(
                run_uid=f"failed-run-{index}",
                trigger_source="vnpy_paper_auto",
                strategy="dual_low",
                market="cn",
                max_results=1,
                cash_per_order=1200,
                min_score=None,
                skip_existing_positions=True,
                settings={"auto_failure_fuse_enabled": True},
                diagnostics={"stage": "alphasift_screen"},
            )
            self.service.agent_repo.complete_run(
                run_id=int(run["id"]),
                status="failed",
                candidate_count=0,
                planned_count=0,
                submitted_count=0,
                skipped_count=0,
                message_count=0,
                error="alphasift_unavailable",
                diagnostics={"stage": "alphasift_screen"},
            )

        status = self.service.get_status(include_snapshot=False, include_recent_trades=False)
        fuse = status["diagnostics"]["failure_fuse"]
        self.assertTrue(fuse["enabled"])
        self.assertTrue(fuse["open"])
        self.assertEqual(fuse["threshold"], 2)
        self.assertEqual(fuse["consecutive_failure_count"], 2)

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            side_effect=AssertionError("fuse should skip before screening"),
        ):
            result = self.service.run_auto_trade_once()

        self.assertFalse(result["accepted"])
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "failure_fuse_open")
        audit = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        self.assertIsNotNone(audit)
        assert audit is not None
        self.assertEqual(audit["status"], "skipped")
        self.assertEqual(audit["error"], "failure_fuse_open")
        self.assertEqual(audit["diagnostics"]["failure_fuse"]["threshold"], 2)
        self.assertEqual(audit["diagnostics"]["failure_fuse"]["recent_statuses"], ["failed", "failed"])
        triggers = AlertService().list_triggers(target="vnpy_paper", status="triggered", page_size=10)["items"]
        self.assertEqual(len(triggers), 1)
        self.assertEqual(triggers[0]["reason"], "failure_fuse_open")
        self.assertIn('"event_type": "failure_fuse_open"', triggers[0]["diagnostics"])

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService.screen"
        ) as screen_again:
            repeated = self.service.run_auto_trade_once()
        screen_again.assert_not_called()
        self.assertEqual(repeated["reason"], "failure_fuse_open")
        repeated_status = self.service.get_status(
            include_snapshot=False,
            include_recent_trades=False,
        )["diagnostics"]["failure_fuse"]
        self.assertTrue(repeated_status["open"])
        self.assertIn(result["agent_run_uid"], triggers[0]["diagnostics"])

    def test_runtime_connection_event_reuses_alert_history_and_route(self) -> None:
        with patch.object(
            self.service,
            "_record_auto_trade_alert_event",
        ) as record_event:
            self.service.record_runtime_connection_event(
                event_type="vnpy_gateway_reconnect_failed",
                status="failed",
                reason="connect_failed",
                observed_value=1,
                diagnostics={"gateway_name": "SIM", "trigger": "monitor"},
            )

        record_event.assert_called_once_with(
            "vnpy_gateway_reconnect_failed",
            status="failed",
            reason="connect_failed",
            observed_value=1,
            threshold=None,
            diagnostics={"gateway_name": "SIM", "trigger": "monitor"},
        )

    def test_auto_trade_alert_event_sends_alert_notification_and_records_attempt(self) -> None:
        dispatch = NotificationDispatchResult(
            dispatched=True,
            success=True,
            status="sent",
            channel_results=[
                ChannelAttemptResult(channel="custom", success=True, latency_ms=12),
            ],
        )

        with patch("src.notification.NotificationService") as notification_cls:
            notification_cls.return_value.send_with_results.return_value = dispatch
            self.service._record_auto_trade_alert_event(
                "failure_fuse_open",
                status="triggered",
                reason="failure_fuse_open",
                observed_value=2,
                threshold=2,
                diagnostics={
                    "agent_run_uid": "notify-run",
                    "strategy": "dual_low",
                    "market": "cn",
                },
            )

        send = notification_cls.return_value.send_with_results
        send.assert_called_once()
        self.assertIn("vn.py paper auto trading alert", send.call_args.args[0])
        self.assertEqual(send.call_args.kwargs["route_type"], "alert")
        self.assertEqual(send.call_args.kwargs["severity"], "critical")
        self.assertIn("failure_fuse_open", send.call_args.kwargs["dedup_key"])
        triggers = AlertService().list_triggers(target="vnpy_paper", status="triggered", page_size=10)["items"]
        self.assertEqual(len(triggers), 1)
        notifications = AlertService().list_notifications(trigger_id=triggers[0]["id"], page_size=10)["items"]
        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0]["channel"], "custom")
        self.assertTrue(notifications[0]["success"])
        self.assertEqual(notifications[0]["latency_ms"], 12)

    def test_failure_fuse_auto_recovers_after_persisted_cooldown(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_failure_fuse_enabled": True,
                "auto_failure_fuse_threshold": 2,
                "auto_failure_fuse_auto_recovery_enabled": True,
                "auto_failure_fuse_cooldown_minutes": 60,
            }
        )
        for index in range(2):
            run = self.service.agent_repo.create_run(
                run_uid=f"failed-cooldown-run-{index}",
                trigger_source="vnpy_paper_auto",
                strategy="dual_low",
                market="cn",
                max_results=1,
                cash_per_order=1200,
            )
            self.service.agent_repo.complete_run(
                run_id=int(run["id"]),
                status="failed",
                candidate_count=0,
                planned_count=0,
                submitted_count=0,
                skipped_count=0,
                error="alphasift_unavailable",
            )
        probe = self.service.agent_repo.create_run(
            run_uid="cooldown-probe-run",
            trigger_source="vnpy_paper_auto",
            strategy="dual_low",
            market="cn",
            max_results=1,
            cash_per_order=1200,
        )
        settings = self.service.get_settings()
        opened_at = datetime.now(timezone.utc).replace(microsecond=0)

        with patch.object(self.service, "_now_utc", return_value=opened_at):
            reason, diagnostics = self.service._failure_fuse_reason(
                settings,
                current_run_id=int(probe["id"]),
            )
        self.assertEqual(reason, "failure_fuse_open")
        self.assertEqual(diagnostics["remaining_cooldown_seconds"], 3600)
        self.assertFalse(diagnostics["auto_recovery_due"])

        with patch.object(
            self.service,
            "_now_utc",
            return_value=opened_at + timedelta(minutes=60),
        ):
            reason, diagnostics = self.service._failure_fuse_reason(
                settings,
                current_run_id=int(probe["id"]),
            )
        self.assertIsNone(reason)
        self.assertTrue(diagnostics["auto_recovered"])
        self.assertFalse(diagnostics["open"])
        self.assertEqual(diagnostics["remaining_cooldown_seconds"], 0)

        payload = json.loads(self.service.config_path.read_text(encoding="utf-8"))
        self.assertNotIn("failure_fuse_opened_at", payload)
        self.assertEqual(
            payload["failure_fuse_reset_at"],
            diagnostics["auto_recovered_at"],
        )
        self.assertEqual(
            payload["failure_fuse_last_auto_recovered_at"],
            diagnostics["auto_recovered_at"],
        )

    def test_auto_trade_alert_event_records_no_channel_notification_attempt(self) -> None:
        dispatch = NotificationDispatchResult(
            dispatched=False,
            success=False,
            status="no_channel",
            message="notification route alert has no configured channel",
        )

        with patch("src.notification.NotificationService") as notification_cls:
            notification_cls.return_value.send_with_results.return_value = dispatch
            self.service._record_auto_trade_alert_event(
                "data_quality_unavailable",
                status="failed",
                reason="data_quality_unavailable",
                diagnostics={"agent_run_uid": "no-channel-run"},
            )

        triggers = AlertService().list_triggers(target="vnpy_paper", status="failed", page_size=10)["items"]
        self.assertEqual(len(triggers), 1)
        notifications = AlertService().list_notifications(trigger_id=triggers[0]["id"], page_size=10)["items"]
        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0]["channel"], "__no_channel__")
        self.assertFalse(notifications[0]["success"])
        self.assertEqual(notifications[0]["error_code"], "no_channel")
        self.assertFalse(notifications[0]["retryable"])

    def test_reset_failure_fuse_clears_status_baseline(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_strategy": "dual_low",
                "auto_market": "cn",
                "auto_failure_fuse_enabled": True,
                "auto_failure_fuse_threshold": 2,
            }
        )
        for index in range(2):
            run = self.service.agent_repo.create_run(
                run_uid=f"failed-reset-run-{index}",
                trigger_source="vnpy_paper_auto",
                strategy="dual_low",
                market="cn",
                max_results=1,
                cash_per_order=1200,
            )
            self.service.agent_repo.complete_run(
                run_id=int(run["id"]),
                status="failed",
                candidate_count=0,
                planned_count=0,
                submitted_count=0,
                skipped_count=0,
                error="alphasift_unavailable",
                diagnostics={"stage": "alphasift_screen"},
            )

        status_before = self.service.get_status(include_snapshot=False, include_recent_trades=False)
        self.assertTrue(status_before["diagnostics"]["failure_fuse"]["open"])

        status_after = self.service.reset_failure_fuse()

        fuse = status_after["diagnostics"]["failure_fuse"]
        self.assertTrue(fuse["enabled"])
        self.assertFalse(fuse["open"])
        self.assertEqual(fuse["consecutive_failure_count"], 0)
        self.assertIsNotNone(fuse["reset_at"])

    def test_auto_trade_dry_run_generates_plan_without_trade(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_execution_mode": "dry_run",
                "auto_strategy": "dual_low",
                "auto_max_results": 1,
                "auto_cash_per_order": 1200,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "candidates": [
                {
                    "code": "600519",
                    "name": "贵州茅台",
                    "score": 80,
                    "screen_score": 78,
                    "rank": 1,
                    "price": 10.0,
                    "factor_scores": {"value": 88.0, "momentum": 72.5},
                    "raw": {
                        "matched_rules": [
                            {"key": "pe_below_limit", "status": "passed", "value": 18.2},
                            {"key": "momentum_positive", "matched": True},
                        ],
                    },
                    "data_quality": "partial",
                    "missing_fields": ["industry"],
                    "data_sources": ["em_datacenter"],
                },
            ],
            "warnings": [],
        }

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ):
            result = self.service.run_auto_trade_once()

        self.assertTrue(result["accepted"])
        self.assertEqual(result["planned_count"], 1)
        self.assertEqual(result["submitted_count"], 0)
        self.assertEqual(result["skipped_count"], 0)
        self.assertEqual(result["orders"][0]["status"], "planned")

        audit = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        self.assertIsNotNone(audit)
        assert audit is not None
        self.assertEqual(audit["planned_count"], 1)
        summary = audit["diagnostics"]["agent_summary"]
        self.assertEqual(summary["schema_version"], 1)
        self.assertEqual(summary["outcome"], "planned")
        self.assertEqual(summary["candidate_count"], 1)
        self.assertEqual(summary["planned_count"], 1)
        self.assertEqual(summary["submitted_count"], 0)
        self.assertEqual(summary["skipped_count"], 0)
        self.assertEqual(summary["execution_mode"], "dry_run")
        self.assertEqual(summary["risk_review_counts"], {"passed": 1})
        self.assertEqual(summary["agent_review_counts"], {"warning": 1})
        self.assertEqual(summary["review_quality"]["status"], "guarded")
        self.assertEqual(summary["review_quality"]["score"], 90.0)
        self.assertTrue(summary["review_quality"]["human_review_recommended"])
        self.assertEqual(
            summary["review_quality"]["risk_flags"],
            ["agent_review_warning"],
        )
        daily_summary = self.service.agent_repo.summarize_daily_runs(target_date=date.today(), limit=5)
        self.assertEqual(daily_summary["review_quality_counts"], {"guarded": 1})
        self.assertEqual(daily_summary["health"], "warning")
        quality_trends = self.service.agent_repo.summarize_data_quality_trends(days=7)
        self.assertEqual(quality_trends["quality_counts"], {"ok": 1})
        self.assertEqual(quality_trends["degraded_count"], 0)
        self.assertEqual(quality_trends["degraded_rate_pct"], 0.0)
        self.assertEqual(quality_trends["daily"][0]["quality_counts"], {"ok": 1})
        self.assertTrue(any(event["stage"] == "agent_summary" for event in audit["timeline"]))
        workflow = audit["diagnostics"]["agent_workflow"]
        self.assertEqual(workflow["schema_version"], 1)
        self.assertEqual(workflow["status"], "planned")
        self.assertEqual(workflow["current_stage"], "trade_plan")
        self.assertEqual(workflow["next_action"], "review_or_approve_trade_plan")
        self.assertTrue(any(stage["key"] == "candidate_review" for stage in workflow["stages"]))
        self.assertEqual(audit["trade_plans"][0]["status"], "planned")
        self.assertEqual(audit["trade_plans"][0]["execution_mode"], "dry_run")
        self.assertIsNone(audit["trade_plans"][0]["trade_id"])
        decision_order = audit["decisions"][0]["order_result"]
        plan_order = audit["trade_plans"][0]["order_result"]
        self.assertEqual(decision_order["position_plan"]["symbol"], "600519")
        self.assertEqual(decision_order["position_plan"]["planned_cash_amount"], 1200.0)
        self.assertEqual(decision_order["position_plan"]["execution_mode"], "dry_run")
        strategy_evidence = decision_order["strategy_evidence"]
        self.assertEqual(strategy_evidence["schema_version"], 1)
        self.assertEqual(strategy_evidence["strategy"], "dual_low")
        self.assertEqual(strategy_evidence["status"], "detailed")
        self.assertEqual(strategy_evidence["rank"], 1)
        self.assertEqual(strategy_evidence["screen_score"], 78.0)
        self.assertEqual(strategy_evidence["final_score"], 80.0)
        self.assertEqual(strategy_evidence["factor_scores"], {"value": 88.0, "momentum": 72.5})
        self.assertEqual(strategy_evidence["matches"][0]["key"], "pe_below_limit")
        self.assertEqual(strategy_evidence["matches"][0]["status"], "passed")
        self.assertEqual(strategy_evidence["matches"][1]["matched"], True)
        self.assertEqual(
            strategy_evidence["evidence_fields"],
            ["rule_matches", "factor_scores", "screen_score", "final_score"],
        )
        self.assertEqual(decision_order["risk_review"]["status"], "passed")
        self.assertEqual(decision_order["risk_review"]["reason"], "dry_run")
        self.assertEqual(decision_order["risk_review"]["candidate_data_quality"]["status"], "partial")
        self.assertEqual(decision_order["risk_review"]["candidate_data_quality"]["score"], 67.0)
        self.assertEqual(decision_order["agent_review"]["status"], "warning")
        self.assertEqual(decision_order["agent_review"]["reason"], "data_quality_missing_fields")
        self.assertEqual(decision_order["agent_review"]["reviewer"], "rule_agent_v1")
        self.assertEqual(
            decision_order["risk_review"]["candidate_data_quality"]["missing_fields"],
            ["industry"],
        )
        self.assertEqual(decision_order["risk_review"]["candidate_data_quality"]["data_sources"], ["em_datacenter"])
        self.assertEqual(plan_order["position_plan"]["symbol"], "600519")
        self.assertEqual(plan_order["strategy_evidence"], strategy_evidence)
        self.assertEqual(plan_order["risk_review"]["review_source"], "rule_based_auto_trade")
        self.assertEqual(plan_order["risk_review"]["candidate_data_quality"]["status"], "partial")
        self.assertEqual(plan_order["agent_review"]["status"], "warning")
        trades = self.service.portfolio.list_trade_events(account_id=int(self.service.get_settings().account_id), page=1)
        self.assertEqual(trades["items"], [])

    def test_candidate_strategy_evidence_does_not_infer_missing_rule_matches(self) -> None:
        settings = self.service.get_settings()

        evidence = self.service._candidate_strategy_evidence(
            {
                "code": "600519",
                "score": 80,
                "reason": "low valuation summary",
                "factor_scores": {"missing": float("nan")},
            },
            settings,
        )

        self.assertEqual(evidence["status"], "summary_only")
        self.assertEqual(evidence["matches"], [])
        self.assertEqual(evidence["factor_scores"], {})
        self.assertEqual(evidence["rationale"], "low valuation summary")
        self.assertEqual(evidence["evidence_fields"], ["final_score", "rationale"])

    def test_auto_trade_llm_review_passes_candidate_before_plan(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_execution_mode": "dry_run",
                "auto_llm_review_enabled": True,
                "auto_strategy": "dual_low",
                "auto_max_results": 1,
                "auto_cash_per_order": 1200,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "candidates": [
                {
                    "code": "600519",
                    "name": "贵州茅台",
                    "score": 88,
                    "price": 10.0,
                    "data_quality": "ok",
                    "rationale": "score and liquidity pass",
                },
            ],
            "warnings": [],
        }
        fake_analyzer = MagicMock()
        fake_analyzer.is_available.return_value = True
        fake_analyzer._call_litellm.return_value = (
            '{"status":"passed","reason":"acceptable_candidate","summary":"Candidate can continue","checks":[]}',
            "openai/test",
            {"total_tokens": 18},
        )

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ), patch("src.analyzer.GeminiAnalyzer", return_value=fake_analyzer):
            result = self.service.run_auto_trade_once()

        self.assertEqual(result["planned_count"], 1)
        self.assertEqual(result["skipped_count"], 0)
        audit = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        self.assertIsNotNone(audit)
        assert audit is not None
        llm_review = audit["decisions"][0]["order_result"]["llm_review"]
        self.assertEqual(llm_review["status"], "passed")
        self.assertEqual(llm_review["model"], "openai/test")
        self.assertEqual(llm_review["prompt_version"], LLM_PRE_TRADE_REVIEW_PROMPT_VERSION)
        self.assertEqual(llm_review["evaluator_version"], LLM_PRE_TRADE_REVIEW_EVALUATOR_VERSION)
        self.assertFalse(llm_review["block_trade"])
        self.assertEqual(audit["trade_plans"][0]["order_result"]["llm_review"]["status"], "passed")
        self.assertEqual(audit["diagnostics"]["agent_summary"]["llm_review_counts"], {"passed": 1})
        self.assertEqual(audit["diagnostics"]["agent_summary"]["review_quality"]["status"], "audited")
        self.assertEqual(audit["diagnostics"]["agent_summary"]["review_quality"]["score"], 100.0)
        self.assertTrue(audit["diagnostics"]["agent_summary"]["review_quality"]["llm_review_enabled"])
        self.assertFalse(audit["diagnostics"]["agent_summary"]["review_quality"]["human_review_recommended"])
        audit_context = fake_analyzer._call_litellm.call_args.kwargs["audit_context"]
        self.assertEqual(audit_context["prompt_version"], LLM_PRE_TRADE_REVIEW_PROMPT_VERSION)
        self.assertEqual(audit_context["evaluator_version"], LLM_PRE_TRADE_REVIEW_EVALUATOR_VERSION)

    def test_auto_trade_llm_review_blocks_candidate_fail_closed(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_execution_mode": "dry_run",
                "auto_llm_review_enabled": True,
                "auto_strategy": "dual_low",
                "auto_max_results": 1,
                "auto_cash_per_order": 1200,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "candidates": [
                {
                    "code": "600519",
                    "name": "贵州茅台",
                    "score": 88,
                    "price": 10.0,
                    "data_quality": "ok",
                    "rationale": "too thin",
                },
            ],
            "warnings": [],
        }
        fake_analyzer = MagicMock()
        fake_analyzer.is_available.return_value = True
        fake_analyzer._call_litellm.return_value = (
            '{"status":"blocked","reason":"insufficient_rationale","summary":"Do not continue","checks":[]}',
            "openai/test",
            {"total_tokens": 21},
        )

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ), patch("src.analyzer.GeminiAnalyzer", return_value=fake_analyzer):
            result = self.service.run_auto_trade_once()

        self.assertEqual(result["planned_count"], 0)
        self.assertEqual(result["skipped_count"], 1)
        self.assertEqual(result["orders"][0]["reason"], "llm_review_insufficient_rationale")
        audit = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        self.assertIsNotNone(audit)
        assert audit is not None
        decision_order = audit["decisions"][0]["order_result"]
        self.assertEqual(decision_order["llm_review"]["status"], "blocked")
        self.assertEqual(decision_order["llm_review"]["reason"], "insufficient_rationale")
        self.assertEqual(decision_order["llm_review"]["prompt_version"], LLM_PRE_TRADE_REVIEW_PROMPT_VERSION)
        self.assertEqual(decision_order["llm_review"]["evaluator_version"], LLM_PRE_TRADE_REVIEW_EVALUATOR_VERSION)
        self.assertTrue(decision_order["llm_review"]["block_trade"])
        self.assertEqual(audit["trade_plans"][0]["skip_reason"], "llm_review_insufficient_rationale")
        self.assertEqual(audit["diagnostics"]["agent_summary"]["llm_review_counts"], {"blocked": 1})
        self.assertEqual(audit["diagnostics"]["agent_summary"]["review_quality"]["status"], "guarded")
        self.assertEqual(audit["diagnostics"]["agent_summary"]["review_quality"]["score"], 70.0)
        self.assertEqual(
            audit["diagnostics"]["agent_summary"]["review_quality"]["risk_flags"],
            ["agent_review_blocked", "llm_review_blocked"],
        )

    def test_auto_trade_dry_run_override_ignores_disabled_auto_without_persisting_mode(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": False,
                "auto_execution_mode": "paper",
                "auto_strategy": "dual_low",
                "auto_max_results": 1,
                "auto_cash_per_order": 1200,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "candidates": [
                {"code": "600519", "score": 80, "price": 10.0},
            ],
            "warnings": [],
        }

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ):
            result = self.service.run_auto_trade_once(
                execution_mode_override="dry_run",
                ignore_auto_trade_enabled=True,
            )

        self.assertTrue(result["accepted"])
        self.assertEqual(result["planned_count"], 1)
        self.assertEqual(result["submitted_count"], 0)
        self.assertEqual(result["orders"][0]["status"], "planned")
        audit = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        self.assertIsNotNone(audit)
        assert audit is not None
        self.assertEqual(audit["settings"]["auto_execution_mode"], "dry_run")
        self.assertTrue(audit["settings"]["auto_trade_enabled"])
        self.assertEqual(audit["diagnostics"]["execution_mode_override"], "dry_run")
        settings_after = self.service.get_settings()
        self.assertEqual(settings_after.auto_execution_mode, "paper")
        self.assertFalse(settings_after.auto_trade_enabled)

    def test_auto_trade_vnpy_paper_submits_order_to_main_engine_without_local_fill(self) -> None:
        installed = _install_fake_vnpy_modules()
        main_engine = _FakeMainEngine()
        self.service = VnpyPaperTradingService(
            data_fetcher_manager=_FakeDataFetcherManager(price=10.0),
            config_path=self.config_path,
            vnpy_main_engine=main_engine,
        )
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_execution_mode": "vnpy_paper",
                "vnpy_gateway_name": "SIM",
                "auto_strategy": "dual_low",
                "auto_max_results": 1,
                "auto_cash_per_order": 1200,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "quality_status": "ok",
            "candidates": [
                {"code": "600519", "name": "贵州茅台", "score": 80, "price": 10.0, "amount": 200000000},
            ],
            "warnings": [],
            "source_errors": [],
        }

        try:
            with patch(
                "src.services.vnpy_paper_trading_service.AlphaSiftService",
                return_value=fake_alphasift,
            ):
                result = self.service.run_auto_trade_once()
        finally:
            _restore_modules(installed)

        self.assertTrue(result["accepted"])
        self.assertEqual(result["submitted_count"], 1)
        self.assertEqual(result["orders"][0]["status"], "submitted")
        self.assertEqual(result["orders"][0]["source"], "vnpy_main_engine")
        self.assertEqual(result["orders"][0]["raw"]["vt_orderid"], "SIM.1")
        self.assertEqual(main_engine.calls[0][1], "SIM")
        self.assertEqual(main_engine.calls[0][0].symbol, "600519")

        audit = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        self.assertIsNotNone(audit)
        assert audit is not None
        self.assertEqual(audit["settings"]["auto_execution_mode"], "vnpy_paper")
        self.assertEqual(audit["settings"]["vnpy_gateway_name"], "SIM")
        self.assertEqual(audit["trade_plans"][0]["status"], "submitted")
        self.assertEqual(audit["trade_plans"][0]["execution_mode"], "vnpy_paper")
        self.assertIsNone(audit["trade_plans"][0]["trade_id"])
        trades = self.service.portfolio.list_trade_events(account_id=int(self.service.get_settings().account_id), page=1)
        self.assertEqual(trades["items"], [])

    def test_vnpy_submit_skips_gateway_that_reports_disconnected(self) -> None:
        installed = _install_fake_vnpy_modules()
        main_engine = _FakeMainEngine()
        main_engine.get_gateway = MagicMock(
            return_value=types.SimpleNamespace(
                get_state_snapshot=lambda: {"connected": False}
            )
        )
        service = VnpyPaperTradingService(
            config_path=self.config_path,
            vnpy_main_engine=main_engine,
        )
        service.update_settings({"vnpy_gateway_name": "SIM"})
        try:
            result = service._submit_vnpy_bridge_order(
                settings=service.get_settings(),
                account_id=1,
                symbol="600519",
                side="buy",
                market="cn",
                quantity=100,
                price=10,
                cash_amount=1000,
                source="unit_test",
            )
        finally:
            _restore_modules(installed)

        self.assertFalse(result["accepted"])
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "vnpy_gateway_disconnected")
        self.assertEqual(main_engine.calls, [])

    @unittest.skipUnless(importlib.util.find_spec("vnpy"), "optional vn.py runtime is not installed")
    def test_builtin_simulated_gateway_fills_agent_plan_through_real_event_engine(self) -> None:
        from vnpy.event import EventEngine
        from vnpy.trader.engine import MainEngine

        from src.services.vnpy_simulated_gateway import DsaSimulatedGateway

        event_engine = EventEngine()
        main_engine = MainEngine(event_engine)
        main_engine.add_gateway(DsaSimulatedGateway, "DSA_SIM")
        main_engine.connect(
            {
                "fill_delay_ms": 100,
                "duplicate_trade_event_count": 2,
            },
            "DSA_SIM",
        )
        service = VnpyPaperTradingService(
            data_fetcher_manager=_FakeDataFetcherManager(price=10.0),
            config_path=self.config_path,
            vnpy_main_engine=main_engine,
            vnpy_event_engine=event_engine,
        )
        bridge = service.attach_vnpy_event_engine(event_engine)
        service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_execution_mode": "vnpy_paper",
                "vnpy_gateway_name": "DSA_SIM",
                "auto_strategy": "dual_low",
                "auto_max_results": 1,
                "auto_cash_per_order": 1200,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "quality_status": "ok",
            "candidates": [
                {
                    "code": "600519",
                    "name": "贵州茅台",
                    "score": 80,
                    "price": 10.0,
                    "amount": 200000000,
                }
            ],
            "warnings": [],
            "source_errors": [],
        }

        try:
            with patch(
                "src.services.vnpy_paper_trading_service.AlphaSiftService",
                return_value=fake_alphasift,
            ):
                result = service.run_auto_trade_once()
            self.assertEqual(result["orders"][0]["status"], "submitted")

            deadline = time.monotonic() + 5.0
            detail = None
            while time.monotonic() < deadline:
                detail = service.agent_repo.get_run_detail(result["agent_run_uid"])
                if detail and detail["trade_plans"][0]["status"] == "filled":
                    break
                time.sleep(0.05)

            self.assertIsNotNone(detail)
            assert detail is not None
            self.assertEqual(detail["trade_plans"][0]["status"], "filled")
            self.assertEqual(detail["decisions"][0]["status"], "filled")
            account_id = int(service.get_settings().account_id)
            trades = service.portfolio.list_trade_events(account_id=account_id, page=1)
            self.assertEqual(len(trades["items"]), 1)
            self.assertEqual(trades["items"][0]["symbol"], "600519")
        finally:
            bridge.unregister()
            main_engine.close()

    @unittest.skipUnless(importlib.util.find_spec("vnpy"), "optional vn.py runtime is not installed")
    def test_builtin_simulated_gateway_rejection_fails_agent_plan_without_trade(self) -> None:
        from vnpy.event import EventEngine
        from vnpy.trader.engine import MainEngine

        from src.services.vnpy_simulated_gateway import DsaSimulatedGateway

        event_engine = EventEngine()
        main_engine = MainEngine(event_engine)
        main_engine.add_gateway(DsaSimulatedGateway, "DSA_SIM")
        main_engine.connect(
            {
                "fill_delay_ms": 50,
                "reject_every_nth_order": 1,
            },
            "DSA_SIM",
        )
        service = VnpyPaperTradingService(
            data_fetcher_manager=_FakeDataFetcherManager(price=10.0),
            config_path=self.config_path,
            vnpy_main_engine=main_engine,
            vnpy_event_engine=event_engine,
        )
        bridge = service.attach_vnpy_event_engine(event_engine)
        service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_execution_mode": "vnpy_paper",
                "vnpy_gateway_name": "DSA_SIM",
                "auto_strategy": "dual_low",
                "auto_max_results": 1,
                "auto_cash_per_order": 1200,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "quality_status": "ok",
            "candidates": [
                {
                    "code": "600519",
                    "name": "贵州茅台",
                    "score": 80,
                    "price": 10.0,
                    "amount": 200000000,
                }
            ],
            "warnings": [],
            "source_errors": [],
        }

        try:
            with patch(
                "src.services.vnpy_paper_trading_service.AlphaSiftService",
                return_value=fake_alphasift,
            ):
                result = service.run_auto_trade_once()

            deadline = time.monotonic() + 5.0
            detail = None
            while time.monotonic() < deadline:
                detail = service.agent_repo.get_run_detail(result["agent_run_uid"])
                if detail and detail["trade_plans"][0]["status"] == "failed":
                    break
                time.sleep(0.05)

            self.assertIsNotNone(detail)
            assert detail is not None
            plan = detail["trade_plans"][0]
            self.assertEqual(plan["status"], "failed")
            self.assertEqual(plan["skip_reason"], "vnpy_order_rejected")
            self.assertEqual(
                plan["order_result"]["message"],
                "simulated_configured_rejection",
            )
            account_id = int(service.get_settings().account_id)
            trades = service.portfolio.list_trade_events(account_id=account_id, page=1)
            self.assertEqual(trades["items"], [])
        finally:
            bridge.unregister()
            main_engine.close()

    def test_cancel_submitted_vnpy_paper_trade_plan_requests_gateway_cancel(self) -> None:
        installed = _install_fake_vnpy_modules()
        main_engine = _FakeMainEngine()
        self.service = VnpyPaperTradingService(
            data_fetcher_manager=_FakeDataFetcherManager(price=10.0),
            config_path=self.config_path,
            vnpy_main_engine=main_engine,
        )
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_execution_mode": "vnpy_paper",
                "vnpy_gateway_name": "SIM",
                "auto_strategy": "dual_low",
                "auto_max_results": 1,
                "auto_cash_per_order": 1200,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "quality_status": "ok",
            "candidates": [
                {"code": "600519", "name": "璐靛窞鑼呭彴", "score": 80, "price": 10.0, "amount": 200000000},
            ],
            "warnings": [],
            "source_errors": [],
        }

        try:
            with patch(
                "src.services.vnpy_paper_trading_service.AlphaSiftService",
                return_value=fake_alphasift,
            ):
                run_result = self.service.run_auto_trade_once()
            run_detail = self.service.agent_repo.get_run_detail(run_result["agent_run_uid"])
            assert run_detail is not None
            plan_uid = run_detail["trade_plans"][0]["plan_uid"]
            cancel_result = self.service.cancel_trade_plan(plan_uid)
            updated_detail = self.service.agent_repo.get_run_detail(run_result["agent_run_uid"])
        finally:
            _restore_modules(installed)

        self.assertTrue(cancel_result["accepted"])
        self.assertEqual(cancel_result["status"], "cancel_requested")
        self.assertEqual(cancel_result["reason"], "vnpy_order_cancel_requested")
        self.assertEqual(cancel_result["raw"]["cancel"]["cancel_request_payload"]["orderid"], "1")
        self.assertEqual(main_engine.cancel_calls[0][1], "SIM")
        self.assertEqual(main_engine.cancel_calls[0][0].orderid, "1")
        self.assertIsNotNone(updated_detail)
        assert updated_detail is not None
        self.assertEqual(updated_detail["trade_plans"][0]["status"], "cancel_requested")
        self.assertEqual(updated_detail["decisions"][0]["status"], "cancel_requested")
        self.assertEqual(updated_detail["diagnostics"]["agent_summary"]["trade_plan_status_counts"], {"cancel_requested": 1})
        self.assertEqual(updated_detail["submitted_count"], 1)

    def test_auto_trade_vnpy_paper_records_gateway_submit_exception(self) -> None:
        installed = _install_fake_vnpy_modules()
        main_engine = _FailingMainEngine(RuntimeError("gateway disconnected"))
        self.service = VnpyPaperTradingService(
            data_fetcher_manager=_FakeDataFetcherManager(price=10.0),
            config_path=self.config_path,
            vnpy_main_engine=main_engine,
        )
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_execution_mode": "vnpy_paper",
                "vnpy_gateway_name": "SIM",
                "auto_strategy": "dual_low",
                "auto_max_results": 1,
                "auto_cash_per_order": 1200,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "quality_status": "ok",
            "candidates": [
                {"code": "600519", "name": "璐靛窞鑼呭彴", "score": 80, "price": 10.0, "amount": 200000000},
            ],
            "warnings": [],
            "source_errors": [],
        }

        try:
            with patch(
                "src.services.vnpy_paper_trading_service.AlphaSiftService",
                return_value=fake_alphasift,
            ):
                result = self.service.run_auto_trade_once()
        finally:
            _restore_modules(installed)

        self.assertTrue(result["accepted"])
        self.assertEqual(result["submitted_count"], 0)
        self.assertEqual(result["skipped_count"], 1)
        self.assertEqual(result["orders"][0]["status"], "failed")
        self.assertEqual(result["orders"][0]["reason"], "vnpy_bridge_submit_failed")
        self.assertEqual(result["orders"][0]["source"], "vnpy_main_engine")
        self.assertEqual(result["orders"][0]["raw"]["error_type"], "RuntimeError")
        audit = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        self.assertIsNotNone(audit)
        assert audit is not None
        self.assertEqual(audit["trade_plans"][0]["status"], "failed")
        self.assertEqual(audit["trade_plans"][0]["skip_reason"], "vnpy_bridge_submit_failed")
        trades = self.service.portfolio.list_trade_events(account_id=int(self.service.get_settings().account_id), page=1)
        self.assertEqual(trades["items"], [])

    def test_vnpy_trade_callback_fills_submitted_plan_and_records_trade(self) -> None:
        installed = _install_fake_vnpy_modules()
        main_engine = _FakeMainEngine()
        self.service = VnpyPaperTradingService(
            data_fetcher_manager=_FakeDataFetcherManager(price=10.0),
            config_path=self.config_path,
            vnpy_main_engine=main_engine,
        )
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_execution_mode": "vnpy_paper",
                "vnpy_gateway_name": "SIM",
                "auto_strategy": "dual_low",
                "auto_max_results": 1,
                "auto_cash_per_order": 1200,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "quality_status": "ok",
            "candidates": [
                {"code": "600519", "name": "璐靛窞鑼呭彴", "score": 80, "price": 10.0, "amount": 200000000},
            ],
            "warnings": [],
            "source_errors": [],
        }

        try:
            with patch(
                "src.services.vnpy_paper_trading_service.AlphaSiftService",
                return_value=fake_alphasift,
            ):
                run_result = self.service.run_auto_trade_once()
        finally:
            _restore_modules(installed)

        self.assertEqual(run_result["orders"][0]["status"], "submitted")
        callback = self.service.sync_vnpy_trade_callback(
            vt_orderid="SIM.1",
            vt_tradeid="SIM.T1",
            symbol="600519",
            side="buy",
            market="cn",
            quantity=100,
            price=10.2,
            trade_date=date(2026, 7, 3),
            raw={"gateway_name": "SIM"},
        )

        self.assertTrue(callback["accepted"])
        self.assertEqual(callback["status"], "filled")
        self.assertEqual(callback["source"], "vnpy_main_engine")
        self.assertEqual(callback["raw"]["vt_orderid"], "SIM.1")
        self.assertEqual(callback["raw"]["vt_tradeid"], "SIM.T1")
        account_id = int(self.service.get_settings().account_id)
        trades = self.service.portfolio.list_trade_events(account_id=account_id, page=1)
        self.assertEqual(len(trades["items"]), 1)
        self.assertEqual(trades["items"][0]["trade_uid"], "vnpy-trade-SIM.T1")
        self.assertEqual(trades["items"][0]["price"], 10.2)

        audit = self.service.agent_repo.get_run_detail(run_result["agent_run_uid"])
        self.assertIsNotNone(audit)
        assert audit is not None
        self.assertEqual(audit["submitted_count"], 1)
        self.assertEqual(audit["skipped_count"], 0)
        self.assertEqual(audit["decisions"][0]["status"], "filled")
        self.assertEqual(audit["decisions"][0]["trade_id"], callback["trade_id"])
        self.assertEqual(audit["trade_plans"][0]["status"], "filled")
        self.assertEqual(audit["trade_plans"][0]["trade_id"], callback["trade_id"])
        self.assertEqual(audit["trade_plans"][0]["submitted_price"], 10.2)

        duplicate = self.service.sync_vnpy_trade_callback(
            vt_orderid="SIM.1",
            vt_tradeid="SIM.T1",
            symbol="600519",
            side="buy",
            market="cn",
            quantity=100,
            price=10.2,
        )
        self.assertTrue(duplicate["accepted"])
        self.assertEqual(duplicate["trade_id"], callback["trade_id"])
        self.assertTrue(duplicate["raw"]["duplicate_callback"])

    def test_vnpy_trade_callbacks_accumulate_multiple_fills_idempotently(self) -> None:
        run = self.service.agent_repo.create_run(
            run_uid="multi-fill-run",
            trigger_source="vnpy_paper_auto",
            strategy="dual_low",
            market="cn",
            settings={},
            diagnostics={},
        )
        self.service.agent_repo.record_trade_plan(
            plan_uid="multi-fill-plan",
            run_id=int(run["id"]),
            decision_id=None,
            symbol="600519",
            market="cn",
            side="buy",
            status="submitted",
            execution_mode="vnpy_paper",
            planned_cash_amount=1000,
            planned_quantity=100,
            planned_price=10,
            submitted_quantity=100,
            submitted_price=10,
            order_result={"status": "submitted", "raw": {"vt_orderid": "SIM.MULTI"}},
        )

        first = self.service.sync_vnpy_trade_callback(
            vt_orderid="SIM.MULTI",
            vt_tradeid="SIM.MULTI.T1",
            quantity=40,
            price=10,
            trade_date=date(2026, 7, 6),
        )
        order_state = self.service.sync_vnpy_order_callback(
            vt_orderid="SIM.MULTI",
            status="parttraded",
            volume=100,
            traded=40,
            price=10,
        )
        second = self.service.sync_vnpy_trade_callback(
            vt_orderid="SIM.MULTI",
            vt_tradeid="SIM.MULTI.T2",
            quantity=60,
            price=11,
            trade_date=date(2026, 7, 6),
        )
        duplicate = self.service.sync_vnpy_trade_callback(
            vt_orderid="SIM.MULTI",
            vt_tradeid="SIM.MULTI.T2",
            quantity=60,
            price=11,
            trade_date=date(2026, 7, 6),
        )

        self.assertEqual(first["status"], "part_filled")
        self.assertEqual(first["raw"]["fill_sync"]["cumulative_quantity"], 40.0)
        self.assertEqual(first["raw"]["fill_sync"]["remaining_quantity"], 60.0)
        self.assertEqual(order_state["raw"]["fill_sync"]["trade_count"], 1)
        self.assertEqual(second["status"], "filled")
        self.assertEqual(second["quantity"], 100.0)
        self.assertAlmostEqual(second["price"], 10.6)
        self.assertEqual(second["raw"]["fill_sync"]["trade_count"], 2)
        self.assertEqual(second["raw"]["fill_sync"]["cumulative_notional"], 1060.0)
        self.assertTrue(duplicate["raw"]["duplicate_callback"])
        account_id = int(self.service.get_settings().account_id)
        trades = self.service.portfolio.list_trade_events(account_id=account_id, page=1)
        self.assertEqual(len(trades["items"]), 2)

    def test_cancelled_order_after_partial_fill_keeps_actual_fill_summary(self) -> None:
        run = self.service.agent_repo.create_run(
            run_uid="partial-cancel-run",
            trigger_source="vnpy_paper_auto",
            strategy="dual_low",
            market="cn",
            settings={},
            diagnostics={},
        )
        self.service.agent_repo.record_trade_plan(
            plan_uid="partial-cancel-plan",
            run_id=int(run["id"]),
            decision_id=None,
            symbol="600519",
            market="cn",
            side="buy",
            status="submitted",
            execution_mode="vnpy_paper",
            planned_cash_amount=1000,
            planned_quantity=100,
            planned_price=10,
            submitted_quantity=100,
            submitted_price=10,
            order_result={"status": "submitted", "raw": {"vt_orderid": "SIM.CANCELLED"}},
        )
        partial = self.service.sync_vnpy_trade_callback(
            vt_orderid="SIM.CANCELLED",
            vt_tradeid="SIM.CANCELLED.T1",
            quantity=40,
            price=10.25,
        )

        cancelled = self.service.sync_vnpy_order_callback(
            vt_orderid="SIM.CANCELLED",
            status="cancelled",
            volume=100,
            traded=40,
            price=11,
        )
        refreshed = self.service.agent_repo.get_trade_plan("partial-cancel-plan")

        self.assertEqual(partial["status"], "part_filled")
        self.assertEqual(cancelled["status"], "failed")
        self.assertEqual(cancelled["reason"], "vnpy_order_cancelled")
        self.assertEqual(cancelled["quantity"], 40.0)
        self.assertEqual(cancelled["price"], 10.25)
        self.assertEqual(cancelled["raw"]["fill_sync"]["cumulative_quantity"], 40.0)
        self.assertIsNotNone(refreshed)
        assert refreshed is not None
        self.assertEqual(refreshed["submitted_quantity"], 40.0)
        self.assertEqual(refreshed["submitted_price"], 10.25)
        self.assertEqual(refreshed["trade_id"], partial["trade_id"])

    def test_active_vnpy_plan_reconciles_main_engine_fill_before_timeout(self) -> None:
        main_engine = _FakeMainEngine()
        main_engine.orders["SIM.RECOVER"] = SimpleNamespace(
            vt_orderid="SIM.RECOVER",
            status="ALLTRADED",
            symbol="600519",
            direction="LONG",
            exchange="SSE",
            volume=100,
            traded=100,
            price=10.2,
        )
        main_engine.trades = [
            SimpleNamespace(
                vt_orderid="SIM.RECOVER",
                vt_tradeid="SIM.RECOVER.T1",
                symbol="600519",
                direction="LONG",
                exchange="SSE",
                volume=100,
                price=10.2,
                datetime=datetime(2026, 7, 6, 2, 30, tzinfo=timezone.utc),
            )
        ]
        self.service.update_settings({"vnpy_gateway_name": "SIM"})
        run = self.service.agent_repo.create_run(
            run_uid="reconcile-filled-run",
            trigger_source="vnpy_paper_auto",
            strategy="dual_low",
            market="cn",
            settings={},
            diagnostics={},
        )
        plan = self.service.agent_repo.record_trade_plan(
            plan_uid="reconcile-filled-plan",
            run_id=int(run["id"]),
            decision_id=None,
            symbol="600519",
            market="cn",
            side="buy",
            status="submitted",
            execution_mode="vnpy_paper",
            planned_cash_amount=1000,
            planned_quantity=100,
            planned_price=10,
            submitted_quantity=100,
            submitted_price=10,
            order_result={"status": "submitted", "raw": {"vt_orderid": "SIM.RECOVER"}},
        )
        self._age_trade_plan(int(plan["id"]), minutes=5)
        self.service = VnpyPaperTradingService(
            data_fetcher_manager=_FakeDataFetcherManager(price=10.0),
            config_path=self.config_path,
            vnpy_main_engine=main_engine,
        )

        result = self.service.expire_stale_vnpy_trade_plans(max_plans=3, scan_limit=10)
        refreshed = self.service.agent_repo.get_trade_plan("reconcile-filled-plan")

        self.assertEqual(result["expired_count"], 0)
        self.assertEqual(result["reconciled_count"], 1)
        self.assertEqual(result["protected_count"], 1)
        self.assertEqual(result["reconciliation_failed_count"], 0)
        self.assertIsNotNone(refreshed)
        assert refreshed is not None
        self.assertEqual(refreshed["status"], "filled")
        self.assertEqual(refreshed["order_result"]["raw"]["fill_sync"]["trade_count"], 1)

    def test_trade_callback_during_cancel_request_is_not_lost(self) -> None:
        run = self.service.agent_repo.create_run(
            run_uid="cancel-race-run",
            trigger_source="vnpy_paper_auto",
            strategy="dual_low",
            market="cn",
            settings={},
            diagnostics={},
        )
        self.service.agent_repo.record_trade_plan(
            plan_uid="cancel-race-plan",
            run_id=int(run["id"]),
            decision_id=None,
            symbol="600519",
            market="cn",
            side="buy",
            status="cancel_requested",
            execution_mode="vnpy_paper",
            planned_cash_amount=1000,
            planned_quantity=100,
            planned_price=10,
            submitted_quantity=100,
            submitted_price=10,
            order_result={"status": "cancel_requested", "raw": {"vt_orderid": "SIM.CANCEL.RACE"}},
        )

        result = self.service.sync_vnpy_trade_callback(
            vt_orderid="SIM.CANCEL.RACE",
            vt_tradeid="SIM.CANCEL.RACE.T1",
            quantity=100,
            price=10.1,
        )
        refreshed = self.service.agent_repo.get_trade_plan("cancel-race-plan")

        self.assertTrue(result["accepted"])
        self.assertEqual(result["status"], "filled")
        self.assertIsNotNone(refreshed)
        assert refreshed is not None
        self.assertEqual(refreshed["status"], "filled")

    def test_late_trade_callback_recovers_timed_out_plan(self) -> None:
        run = self.service.agent_repo.create_run(
            run_uid="late-fill-run",
            trigger_source="vnpy_paper_auto",
            strategy="dual_low",
            market="cn",
            settings={},
            diagnostics={},
        )
        self.service.agent_repo.record_trade_plan(
            plan_uid="late-fill-plan",
            run_id=int(run["id"]),
            decision_id=None,
            symbol="600519",
            market="cn",
            side="buy",
            status="failed",
            execution_mode="vnpy_paper",
            planned_cash_amount=1000,
            planned_quantity=100,
            planned_price=10,
            submitted_quantity=100,
            submitted_price=10,
            skip_reason="vnpy_order_timeout",
            order_result={
                "status": "failed",
                "reason": "vnpy_order_timeout",
                "raw": {"vt_orderid": "SIM.LATE.FILL"},
            },
        )

        result = self.service.sync_vnpy_trade_callback(
            vt_orderid="SIM.LATE.FILL",
            vt_tradeid="SIM.LATE.FILL.T1",
            quantity=100,
            price=10.3,
        )
        refreshed = self.service.agent_repo.get_trade_plan("late-fill-plan")

        self.assertTrue(result["accepted"])
        self.assertEqual(result["status"], "filled")
        self.assertIsNotNone(refreshed)
        assert refreshed is not None
        self.assertEqual(refreshed["status"], "filled")
        self.assertIsNone(refreshed["skip_reason"])

    def test_active_vnpy_plan_without_gateway_evidence_waits_for_timeout(self) -> None:
        main_engine = _FakeMainEngine()
        self.service = VnpyPaperTradingService(
            data_fetcher_manager=_FakeDataFetcherManager(price=10.0),
            config_path=self.config_path,
            vnpy_main_engine=main_engine,
        )
        self.service.update_settings({"vnpy_gateway_name": "SIM"})
        run = self.service.agent_repo.create_run(
            run_uid="reconcile-empty-run",
            trigger_source="vnpy_paper_auto",
            strategy="dual_low",
            market="cn",
            settings={},
            diagnostics={},
        )
        plan = self.service.agent_repo.record_trade_plan(
            plan_uid="reconcile-empty-plan",
            run_id=int(run["id"]),
            decision_id=None,
            symbol="600519",
            market="cn",
            side="buy",
            status="submitted",
            execution_mode="vnpy_paper",
            planned_cash_amount=1000,
            planned_quantity=100,
            planned_price=10,
            submitted_quantity=100,
            submitted_price=10,
            order_result={"status": "submitted", "raw": {"vt_orderid": "SIM.EMPTY"}},
        )
        self._age_trade_plan(int(plan["id"]), minutes=5)

        result = self.service.expire_stale_vnpy_trade_plans(max_plans=3, scan_limit=10)
        refreshed = self.service.agent_repo.get_trade_plan("reconcile-empty-plan")

        self.assertEqual(result["scanned_count"], 1)
        self.assertEqual(result["expired_count"], 0)
        self.assertEqual(result["reconciled_count"], 0)
        self.assertEqual(result["failed_count"], 0)
        self.assertIsNotNone(refreshed)
        assert refreshed is not None
        self.assertEqual(refreshed["status"], "submitted")

    def test_vnpy_reconciliation_grace_skips_just_submitted_plan(self) -> None:
        main_engine = _FailingQueryMainEngine(AssertionError("gateway query should not run"))
        self.service = VnpyPaperTradingService(
            data_fetcher_manager=_FakeDataFetcherManager(price=10.0),
            config_path=self.config_path,
            vnpy_main_engine=main_engine,
        )
        self.service.update_settings({"vnpy_gateway_name": "SIM"})
        run = self.service.agent_repo.create_run(
            run_uid="reconcile-grace-run",
            trigger_source="vnpy_paper_auto",
            strategy="dual_low",
            market="cn",
            settings={},
            diagnostics={},
        )
        self.service.agent_repo.record_trade_plan(
            plan_uid="reconcile-grace-plan",
            run_id=int(run["id"]),
            decision_id=None,
            symbol="600519",
            market="cn",
            side="buy",
            status="submitted",
            execution_mode="vnpy_paper",
            planned_cash_amount=1000,
            planned_quantity=100,
            planned_price=10,
            submitted_quantity=100,
            submitted_price=10,
            order_result={"status": "submitted", "raw": {"vt_orderid": "SIM.GRACE"}},
        )

        result = self.service.expire_stale_vnpy_trade_plans(max_plans=3, scan_limit=10)

        self.assertEqual(result["scanned_count"], 0)
        self.assertEqual(result["reconciliation_failed_count"], 0)
        self.assertEqual(result["expired_count"], 0)

    def test_stale_vnpy_plan_query_failure_is_protected_from_expiration(self) -> None:
        main_engine = _FailingQueryMainEngine(RuntimeError("gateway cache unavailable"))
        self.service = VnpyPaperTradingService(
            data_fetcher_manager=_FakeDataFetcherManager(price=10.0),
            config_path=self.config_path,
            vnpy_main_engine=main_engine,
        )
        self.service.update_settings({"vnpy_gateway_name": "SIM"})
        run = self.service.agent_repo.create_run(
            run_uid="reconcile-failed-run",
            trigger_source="vnpy_paper_auto",
            strategy="dual_low",
            market="cn",
            settings={},
            diagnostics={},
        )
        plan = self.service.agent_repo.record_trade_plan(
            plan_uid="reconcile-failed-plan",
            run_id=int(run["id"]),
            decision_id=None,
            symbol="600519",
            market="cn",
            side="buy",
            status="submitted",
            execution_mode="vnpy_paper",
            planned_cash_amount=1000,
            planned_quantity=100,
            planned_price=10,
            submitted_quantity=100,
            submitted_price=10,
            order_result={"status": "submitted", "raw": {"vt_orderid": "SIM.FAIL"}},
        )
        self._age_trade_plan(int(plan["id"]))

        with patch.object(self.service, "_record_auto_trade_alert_event"):
            result = self.service.expire_stale_vnpy_trade_plans(max_plans=3, scan_limit=10)
        refreshed = self.service.agent_repo.get_trade_plan("reconcile-failed-plan")

        self.assertEqual(result["expired_count"], 0)
        self.assertEqual(result["reconciliation_failed_count"], 1)
        self.assertEqual(result["protected_count"], 1)
        self.assertEqual(result["failed_count"], 1)
        self.assertIsNotNone(refreshed)
        assert refreshed is not None
        self.assertEqual(refreshed["status"], "submitted")

    def test_terminal_vnpy_gateway_failures_are_not_automatically_retryable(self) -> None:
        for reason in (
            "vnpy_order_cancelled",
            "vnpy_order_rejected",
            "vnpy_order_failed",
            "vnpy_order_timeout",
            "vnpy_partial_fill_timeout",
            "vnpy_cancel_timeout",
        ):
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(ValueError, "trade_plan_not_retryable"):
                    self.service._validate_trade_plan_retry(
                        {
                            "status": "failed",
                            "skip_reason": reason,
                            "order_result": {"reason": reason},
                        }
                    )

    def test_vnpy_order_callback_parttraded_marks_plan_part_filled_until_trade_callback(self) -> None:
        installed = _install_fake_vnpy_modules()
        main_engine = _FakeMainEngine()
        self.service = VnpyPaperTradingService(
            data_fetcher_manager=_FakeDataFetcherManager(price=10.0),
            config_path=self.config_path,
            vnpy_main_engine=main_engine,
        )
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_execution_mode": "vnpy_paper",
                "vnpy_gateway_name": "SIM",
                "auto_strategy": "dual_low",
                "auto_max_results": 1,
                "auto_cash_per_order": 1200,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "quality_status": "ok",
            "candidates": [
                {"code": "600519", "name": "贵州茅台", "score": 80, "price": 10.0, "amount": 200000000},
            ],
            "warnings": [],
            "source_errors": [],
        }

        try:
            with patch(
                "src.services.vnpy_paper_trading_service.AlphaSiftService",
                return_value=fake_alphasift,
            ):
                result = self.service.run_auto_trade_once()
            partial = self.service.sync_vnpy_order_callback(
                vt_orderid="SIM.1",
                status="parttraded",
                symbol="600519",
                side="buy",
                market="cn",
                volume=100,
                traded=40,
                price=10.1,
            )
            partial_audit = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
            filled = self.service.sync_vnpy_trade_callback(
                vt_orderid="SIM.1",
                vt_tradeid="SIM.T-PART-FINAL",
                symbol="600519",
                side="buy",
                market="cn",
                quantity=100,
                price=10.2,
            )
        finally:
            _restore_modules(installed)

        self.assertTrue(partial["accepted"])
        self.assertEqual(partial["status"], "part_filled")
        self.assertEqual(partial["quantity"], 40.0)
        self.assertEqual(partial["price"], 10.1)
        self.assertEqual(partial["cash_amount"], 404.0)
        self.assertEqual(partial["reason"], "vnpy_order_partially_filled_waiting_trade_callback")
        self.assertIsNotNone(partial_audit)
        assert partial_audit is not None
        self.assertEqual(partial_audit["trade_plans"][0]["status"], "part_filled")
        self.assertEqual(partial_audit["trade_plans"][0]["submitted_quantity"], 40.0)
        self.assertEqual(partial_audit["trade_plans"][0]["submitted_price"], 10.1)
        self.assertIsNone(partial_audit["trade_plans"][0]["trade_id"])
        self.assertEqual(partial_audit["decisions"][0]["status"], "part_filled")
        self.assertTrue(filled["accepted"])
        self.assertEqual(filled["status"], "filled")
        audit = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        self.assertIsNotNone(audit)
        assert audit is not None
        self.assertEqual(audit["trade_plans"][0]["status"], "filled")
        self.assertEqual(audit["trade_plans"][0]["trade_id"], filled["trade_id"])
        self.assertEqual(audit["decisions"][0]["status"], "filled")

    def test_vnpy_order_callback_rejection_marks_submitted_plan_failed(self) -> None:
        installed = _install_fake_vnpy_modules()
        main_engine = _FakeMainEngine()
        self.service = VnpyPaperTradingService(
            data_fetcher_manager=_FakeDataFetcherManager(price=10.0),
            config_path=self.config_path,
            vnpy_main_engine=main_engine,
        )
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_execution_mode": "vnpy_paper",
                "vnpy_gateway_name": "SIM",
                "auto_strategy": "dual_low",
                "auto_max_results": 1,
                "auto_cash_per_order": 1200,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "quality_status": "ok",
            "candidates": [
                {"code": "600519", "name": "璐靛窞鑼呭彴", "score": 80, "price": 10.0, "amount": 200000000},
            ],
            "warnings": [],
            "source_errors": [],
        }

        try:
            with patch(
                "src.services.vnpy_paper_trading_service.AlphaSiftService",
                return_value=fake_alphasift,
            ):
                result = self.service.run_auto_trade_once()
            callback = self.service.sync_vnpy_order_callback(
                vt_orderid="SIM.1",
                status="拒单",
                symbol="600519",
                side="buy",
                market="cn",
                volume=100,
                price=10.0,
                rejected_reason="unit rejected",
            )
        finally:
            _restore_modules(installed)

        self.assertTrue(result["accepted"])
        self.assertEqual(result["orders"][0]["status"], "submitted")
        self.assertFalse(callback["accepted"])
        self.assertEqual(callback["status"], "failed")
        self.assertEqual(callback["reason"], "vnpy_order_rejected")
        audit = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        self.assertIsNotNone(audit)
        assert audit is not None
        self.assertEqual(audit["submitted_count"], 0)
        self.assertEqual(audit["skipped_count"], 1)
        self.assertEqual(audit["trade_plans"][0]["status"], "failed")
        self.assertEqual(audit["trade_plans"][0]["skip_reason"], "vnpy_order_rejected")
        self.assertEqual(audit["decisions"][0]["status"], "failed")

    def test_vnpy_account_and_position_callbacks_are_exposed_in_status_diagnostics(self) -> None:
        account_sync = self.service.sync_vnpy_account_callback(
            account_id="SIM.ACC",
            balance=100000,
            available=99000,
            frozen=1000,
            holding_profit=120,
            currency="CNY",
            raw={"gateway_name": "SIM"},
        )
        position_sync = self.service.sync_vnpy_positions_callback(
            positions=[
                {
                    "vt_symbol": "600519.SSE",
                    "market": "cn",
                    "direction": "net",
                    "volume": 100,
                    "price": 10.0,
                    "pnl": 120,
                }
            ],
            raw={"gateway_name": "SIM"},
        )
        status = self.service.get_status(include_snapshot=False, include_recent_trades=False)

        self.assertTrue(account_sync["accepted"])
        self.assertTrue(position_sync["accepted"])
        sync_state = status["diagnostics"]["vnpy_sync_state"]
        self.assertEqual(sync_state["account"]["account_id"], "SIM.ACC")
        self.assertEqual(sync_state["account"]["available"], 99000.0)
        self.assertEqual(sync_state["position_count"], 1)
        self.assertEqual(sync_state["positions"][0]["symbol"], "600519")
        self.assertEqual(sync_state["positions"][0]["vt_symbol"], "600519.SSE")

    def test_vnpy_event_engine_bridge_updates_submitted_plan_from_order_event(self) -> None:
        installed = _install_fake_vnpy_modules()
        event_engine = _FakeEventEngine()
        main_engine = _FakeMainEngine()
        main_engine.event_engine = event_engine
        self.service = VnpyPaperTradingService(
            data_fetcher_manager=_FakeDataFetcherManager(price=10.0),
            config_path=self.config_path,
            vnpy_main_engine=main_engine,
        )
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_execution_mode": "vnpy_paper",
                "vnpy_gateway_name": "SIM",
                "auto_strategy": "dual_low",
                "auto_max_results": 1,
                "auto_cash_per_order": 1200,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "quality_status": "ok",
            "candidates": [
                {"code": "600519", "name": "璐靛窞鑼呭彴", "score": 80, "price": 10.0, "amount": 200000000},
            ],
            "warnings": [],
            "source_errors": [],
        }

        try:
            with patch(
                "src.services.vnpy_paper_trading_service.AlphaSiftService",
                return_value=fake_alphasift,
            ):
                result = self.service.run_auto_trade_once()
            bridge = self.service.attach_vnpy_event_engine()
            event_engine.emit(
                "eOrder.",
                SimpleNamespace(
                    type="eOrder.",
                    data=SimpleNamespace(
                        vt_orderid="SIM.1",
                        status="rejected",
                        symbol="600519",
                        direction=SimpleNamespace(value="LONG"),
                        exchange=SimpleNamespace(value="SSE"),
                        volume=100,
                        traded=0,
                        price=10.0,
                        rejected_reason="unit rejected",
                    ),
                ),
            )
            bridge.unregister()
        finally:
            _restore_modules(installed)

        audit = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        self.assertIsNotNone(audit)
        assert audit is not None
        self.assertEqual(audit["trade_plans"][0]["status"], "failed")
        self.assertEqual(audit["trade_plans"][0]["skip_reason"], "vnpy_order_rejected")
        sync_state = self.service.get_status(include_snapshot=False, include_recent_trades=False)["diagnostics"][
            "vnpy_sync_state"
        ]
        self.assertEqual(sync_state["recent_orders"][0]["vt_orderid"], "SIM.1")

    def test_auto_trade_manual_approval_generates_plan_then_approves_trade(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": True,
                "auto_execution_mode": "manual_approval",
                "auto_strategy": "dual_low",
                "auto_max_results": 1,
                "auto_cash_per_order": 1200,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "candidates": [
                {"code": "600519", "name": "贵州茅台", "score": 80, "price": 10.0},
            ],
            "warnings": [],
        }

        with patch(
            "src.services.vnpy_paper_trading_service.trading_calendar.build_market_phase_context",
            side_effect=AssertionError("manual approval should not gate plan generation"),
        ), patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ):
            result = self.service.run_auto_trade_once()

        self.assertTrue(result["accepted"])
        self.assertEqual(result["planned_count"], 1)
        self.assertEqual(result["submitted_count"], 0)
        self.assertEqual(result["orders"][0]["reason"], "pending_approval")

        audit = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        self.assertIsNotNone(audit)
        assert audit is not None
        plan = audit["trade_plans"][0]
        self.assertEqual(plan["status"], "planned")
        self.assertEqual(plan["execution_mode"], "manual_approval")
        self.assertIsNone(plan["trade_id"])

        account = self.service.ensure_account(settings=self.service.get_settings())
        trades_before = self.service.portfolio.list_trade_events(
            account_id=int(account["id"]),
            page=1,
        )
        self.assertEqual(trades_before["items"], [])

        approved = self.service.approve_trade_plan(plan["plan_uid"])

        self.assertTrue(approved["accepted"])
        self.assertEqual(approved["status"], "filled")
        self.assertEqual(approved["symbol"], "600519")
        self.assertIsNotNone(approved["trade_id"])
        refreshed = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        self.assertIsNotNone(refreshed)
        assert refreshed is not None
        self.assertEqual(refreshed["planned_count"], 0)
        self.assertEqual(refreshed["submitted_count"], 1)
        self.assertEqual(refreshed["trade_plans"][0]["status"], "filled")
        self.assertEqual(refreshed["trade_plans"][0]["trade_id"], approved["trade_id"])
        self.assertEqual(refreshed["decisions"][0]["status"], "filled")
        self.assertEqual(refreshed["decisions"][0]["trade_id"], approved["trade_id"])

    def test_retry_manual_trade_plan_resubmits_skipped_plan(self) -> None:
        self.service.ensure_account(settings=self.service.get_settings())
        run = self.service.agent_repo.create_run(
            run_uid="retry-plan-run",
            trigger_source="unit-test",
            strategy="dual_low",
            market="cn",
            max_results=1,
            cash_per_order=1200,
            settings={"auto_execution_mode": "manual_approval"},
        )
        decision = self.service.agent_repo.record_decision(
            run_id=int(run["id"]),
            sequence=1,
            symbol="600519",
            name="贵州茅台",
            market="cn",
            action="buy",
            status="skipped",
            reason="price_unavailable",
            cash_amount=1200,
            quantity=None,
            price=10,
        )
        self.service.agent_repo.record_trade_plan(
            plan_uid="retry-plan-test",
            run_id=int(run["id"]),
            decision_id=int(decision["id"]),
            symbol="600519",
            name="贵州茅台",
            market="cn",
            side="buy",
            status="skipped",
            execution_mode="manual_approval",
            planned_cash_amount=1200,
            planned_price=10,
            skip_reason="price_unavailable",
        )
        self.service.agent_repo.complete_run(
            run_id=int(run["id"]),
            status="completed",
            candidate_count=1,
            planned_count=0,
            submitted_count=0,
            skipped_count=1,
        )

        retried = self.service.retry_trade_plan("retry-plan-test")

        self.assertTrue(retried["accepted"])
        self.assertEqual(retried["status"], "filled")
        refreshed = self.service.agent_repo.get_run_detail("retry-plan-run")
        self.assertIsNotNone(refreshed)
        assert refreshed is not None
        self.assertEqual(refreshed["submitted_count"], 1)
        self.assertEqual(refreshed["skipped_count"], 0)
        self.assertEqual(refreshed["trade_plans"][0]["status"], "filled")
        self.assertEqual(refreshed["trade_plans"][0]["order_result"]["retry"]["attempt_count"], 1)
        self.assertIsNone(refreshed["trade_plans"][0]["order_result"]["retry"]["next_retry_after"])
        self.assertEqual(refreshed["decisions"][0]["status"], "filled")

    def test_retry_auto_paper_trade_plan_resubmits_recoverable_skipped_plan(self) -> None:
        self.service.ensure_account(settings=self.service.get_settings())
        run = self.service.agent_repo.create_run(
            run_uid="retry-auto-paper-run",
            trigger_source="unit-test",
            strategy="dual_low",
            market="cn",
            max_results=1,
            cash_per_order=1200,
            settings={"auto_execution_mode": "paper"},
        )
        decision = self.service.agent_repo.record_decision(
            run_id=int(run["id"]),
            sequence=1,
            symbol="600519",
            name="贵州茅台",
            market="cn",
            action="buy",
            status="skipped",
            reason="price_unavailable",
            cash_amount=1200,
            quantity=None,
            price=10,
        )
        self.service.agent_repo.record_trade_plan(
            plan_uid="retry-auto-paper-plan",
            run_id=int(run["id"]),
            decision_id=int(decision["id"]),
            symbol="600519",
            name="贵州茅台",
            market="cn",
            side="buy",
            status="skipped",
            execution_mode="paper",
            planned_cash_amount=1200,
            planned_price=10,
            skip_reason="price_unavailable",
        )
        self.service.agent_repo.complete_run(
            run_id=int(run["id"]),
            status="completed",
            candidate_count=1,
            planned_count=0,
            submitted_count=0,
            skipped_count=1,
        )

        retried = self.service.retry_trade_plan("retry-auto-paper-plan")

        self.assertTrue(retried["accepted"])
        self.assertEqual(retried["status"], "filled")
        self.assertEqual(retried["retry"]["attempt_count"], 1)
        self.assertIsNone(retried["retry"]["next_retry_after"])
        refreshed = self.service.agent_repo.get_run_detail("retry-auto-paper-run")
        self.assertIsNotNone(refreshed)
        assert refreshed is not None
        self.assertEqual(refreshed["submitted_count"], 1)
        self.assertEqual(refreshed["skipped_count"], 0)
        self.assertEqual(refreshed["trade_plans"][0]["status"], "filled")
        self.assertEqual(refreshed["trade_plans"][0]["execution_mode"], "paper")
        self.assertEqual(refreshed["trade_plans"][0]["order_result"]["retry"]["attempt_count"], 1)
        self.assertEqual(refreshed["decisions"][0]["status"], "filled")

    def test_retry_trade_plan_blocks_unrecoverable_skipped_reason(self) -> None:
        run = self.service.agent_repo.create_run(
            run_uid="retry-unrecoverable-run",
            trigger_source="unit-test",
            strategy="dual_low",
            market="cn",
            max_results=1,
            cash_per_order=1200,
            settings={"auto_execution_mode": "paper"},
        )
        self.service.agent_repo.record_trade_plan(
            plan_uid="retry-unrecoverable-plan",
            run_id=int(run["id"]),
            decision_id=None,
            symbol="600519",
            name="贵州茅台",
            market="cn",
            side="buy",
            status="skipped",
            execution_mode="paper",
            planned_cash_amount=1200,
            planned_price=10,
            skip_reason="data_quality_stale",
        )

        with self.assertRaisesRegex(ValueError, "trade_plan_not_retryable"):
            self.service.retry_trade_plan("retry-unrecoverable-plan")

    def test_retry_trade_plan_respects_retry_cooldown(self) -> None:
        run = self.service.agent_repo.create_run(
            run_uid="retry-cooldown-run",
            trigger_source="unit-test",
            strategy="dual_low",
            market="cn",
            max_results=1,
            cash_per_order=1200,
            settings={"auto_execution_mode": "paper"},
        )
        next_retry_after = (
            datetime.now(timezone.utc) + timedelta(minutes=5)
        ).isoformat(timespec="seconds").replace("+00:00", "Z")
        self.service.agent_repo.record_trade_plan(
            plan_uid="retry-cooldown-plan",
            run_id=int(run["id"]),
            decision_id=None,
            symbol="600519",
            name="贵州茅台",
            market="cn",
            side="buy",
            status="failed",
            execution_mode="paper",
            planned_cash_amount=1200,
            planned_price=10,
            skip_reason="vnpy_bridge_submit_failed",
            order_result={
                "status": "failed",
                "reason": "vnpy_bridge_submit_failed",
                "retry": {
                    "attempt_count": 1,
                    "max_attempts": 3,
                    "next_retry_after": next_retry_after,
                },
            },
        )

        with self.assertRaisesRegex(ValueError, "trade_plan_retry_cooldown_active"):
            self.service.retry_trade_plan("retry-cooldown-plan")

    def test_retry_due_trade_plans_retries_due_auto_paper_plan(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
            }
        )
        run = self.service.agent_repo.create_run(
            run_uid="auto-retry-due-run",
            trigger_source="vnpy_paper_auto",
            strategy="dual_low",
            market="cn",
            max_results=1,
            cash_per_order=1200,
            settings={"auto_execution_mode": "paper"},
        )
        decision = self.service.agent_repo.record_decision(
            run_id=int(run["id"]),
            sequence=1,
            symbol="600519",
            name="贵州茅台",
            market="cn",
            action="buy",
            status="failed",
            reason="vnpy_bridge_submit_failed",
            cash_amount=1200,
            quantity=None,
            price=10,
        )
        next_retry_after = (
            datetime.now(timezone.utc) - timedelta(minutes=1)
        ).isoformat(timespec="seconds").replace("+00:00", "Z")
        self.service.agent_repo.record_trade_plan(
            plan_uid="auto-retry-due-plan",
            run_id=int(run["id"]),
            decision_id=int(decision["id"]),
            symbol="600519",
            name="贵州茅台",
            market="cn",
            side="buy",
            status="failed",
            execution_mode="paper",
            planned_cash_amount=1200,
            planned_price=10,
            skip_reason="vnpy_bridge_submit_failed",
            order_result={
                "status": "failed",
                "reason": "vnpy_bridge_submit_failed",
                "retry": {
                    "attempt_count": 1,
                    "max_attempts": 3,
                    "next_retry_after": next_retry_after,
                },
            },
        )
        self.service.agent_repo.complete_run(
            run_id=int(run["id"]),
            status="completed",
            candidate_count=1,
            planned_count=0,
            submitted_count=0,
            skipped_count=1,
        )

        result = self.service.retry_due_trade_plans(max_plans=2)

        self.assertEqual(result["scanned_count"], 1)
        self.assertEqual(result["attempted_count"], 1)
        self.assertEqual(result["submitted_count"], 1)
        self.assertEqual(result["orders"][0]["retry"]["attempt_count"], 2)
        refreshed = self.service.agent_repo.get_run_detail("auto-retry-due-run")
        self.assertIsNotNone(refreshed)
        assert refreshed is not None
        self.assertEqual(refreshed["trade_plans"][0]["status"], "filled")
        self.assertEqual(refreshed["trade_plans"][0]["order_result"]["retry"]["attempt_count"], 2)

    def test_retry_due_trade_plans_expires_stale_vnpy_submitted_plan(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_execution_mode": "vnpy_paper",
                "vnpy_gateway_name": "SIM",
            }
        )
        run = self.service.agent_repo.create_run(
            run_uid="auto-retry-timeout-run",
            trigger_source="vnpy_paper_auto",
            strategy="dual_low",
            market="cn",
            max_results=1,
            cash_per_order=1200,
            settings={"auto_execution_mode": "vnpy_paper"},
        )
        decision = self.service.agent_repo.record_decision(
            run_id=int(run["id"]),
            sequence=1,
            symbol="600519",
            name="贵州茅台",
            market="cn",
            action="buy",
            status="submitted",
            reason=None,
            cash_amount=1200,
            quantity=100,
            price=10,
        )
        self.service.agent_repo.record_trade_plan(
            plan_uid="auto-retry-timeout-plan",
            run_id=int(run["id"]),
            decision_id=int(decision["id"]),
            symbol="600519",
            name="贵州茅台",
            market="cn",
            side="buy",
            status="submitted",
            execution_mode="vnpy_paper",
            planned_cash_amount=1200,
            planned_quantity=100,
            planned_price=10,
            submitted_quantity=100,
            submitted_price=10,
            order_result={
                "accepted": True,
                "status": "submitted",
                "symbol": "600519",
                "side": "buy",
                "quantity": 100,
                "price": 10,
                "cash_amount": 1000,
                "source": "vnpy_main_engine",
                "reason": None,
                "raw": {"vt_orderid": "SIM.OLDORDER"},
            },
        )
        self.service.agent_repo.complete_run(
            run_id=int(run["id"]),
            status="completed",
            candidate_count=1,
            planned_count=0,
            submitted_count=1,
            skipped_count=0,
        )
        stale_time = datetime.now() - timedelta(minutes=45)
        with self.service.agent_repo.db.get_session() as session:
            row = session.get(StockSelectionAgentTradePlan, 1)
            self.assertIsNotNone(row)
            assert row is not None
            row.updated_at = stale_time
            session.commit()

        result = self.service.retry_due_trade_plans(max_plans=2)

        self.assertEqual(result["expired_count"], 1)
        self.assertEqual(result["attempted_count"], 0)
        refreshed = self.service.agent_repo.get_run_detail("auto-retry-timeout-run")
        self.assertIsNotNone(refreshed)
        assert refreshed is not None
        plan = refreshed["trade_plans"][0]
        self.assertEqual(plan["status"], "failed")
        self.assertEqual(plan["skip_reason"], "vnpy_order_timeout")
        self.assertEqual(plan["order_result"]["reason"], "vnpy_order_timeout")
        self.assertEqual(plan["order_result"]["raw"]["timeout"]["vt_orderid"], "SIM.OLDORDER")
        self.assertEqual(refreshed["decisions"][0]["status"], "failed")
        self.assertEqual(refreshed["decisions"][0]["reason"], "vnpy_order_timeout")
        triggers = AlertService().list_triggers(target="vnpy_paper", status="failed", page_size=10)["items"]
        self.assertEqual(len(triggers), 1)
        self.assertEqual(triggers[0]["reason"], "vnpy_order_timeout")
        self.assertEqual(triggers[0]["threshold"], 1800.0)
        self.assertIn('"event_type": "vnpy_order_timeout"', triggers[0]["diagnostics"])
        self.assertIn("auto-retry-timeout-plan", triggers[0]["diagnostics"])

    def test_retry_due_trade_plans_expires_stale_vnpy_cancel_requested_plan(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_execution_mode": "vnpy_paper",
                "vnpy_gateway_name": "SIM",
            }
        )
        run = self.service.agent_repo.create_run(
            run_uid="auto-retry-cancel-timeout-run",
            trigger_source="vnpy_paper_auto",
            strategy="dual_low",
            market="cn",
            max_results=1,
            cash_per_order=1200,
            settings={"auto_execution_mode": "vnpy_paper"},
        )
        decision = self.service.agent_repo.record_decision(
            run_id=int(run["id"]),
            sequence=1,
            symbol="600519",
            name="璐靛窞鑼呭彴",
            market="cn",
            action="buy",
            status="cancel_requested",
            reason="vnpy_order_cancel_requested",
            cash_amount=1200,
            quantity=100,
            price=10,
        )
        self.service.agent_repo.record_trade_plan(
            plan_uid="auto-retry-cancel-timeout-plan",
            run_id=int(run["id"]),
            decision_id=int(decision["id"]),
            symbol="600519",
            name="璐靛窞鑼呭彴",
            market="cn",
            side="buy",
            status="cancel_requested",
            execution_mode="vnpy_paper",
            planned_cash_amount=1200,
            planned_quantity=100,
            planned_price=10,
            submitted_quantity=100,
            submitted_price=10,
            order_result={
                "accepted": True,
                "status": "cancel_requested",
                "symbol": "600519",
                "side": "buy",
                "quantity": 100,
                "price": 10,
                "cash_amount": 1000,
                "source": "vnpy_main_engine",
                "reason": "vnpy_order_cancel_requested",
                "raw": {"vt_orderid": "SIM.CANCEL"},
            },
        )
        self.service.agent_repo.complete_run(
            run_id=int(run["id"]),
            status="completed",
            candidate_count=1,
            planned_count=0,
            submitted_count=1,
            skipped_count=0,
        )
        stale_time = datetime.now() - timedelta(minutes=45)
        with self.service.agent_repo.db.get_session() as session:
            row = session.get(StockSelectionAgentTradePlan, 1)
            self.assertIsNotNone(row)
            assert row is not None
            row.updated_at = stale_time
            session.commit()

        result = self.service.retry_due_trade_plans(max_plans=2)

        self.assertEqual(result["expired_count"], 1)
        self.assertEqual(result["attempted_count"], 0)
        refreshed = self.service.agent_repo.get_run_detail("auto-retry-cancel-timeout-run")
        self.assertIsNotNone(refreshed)
        assert refreshed is not None
        plan = refreshed["trade_plans"][0]
        self.assertEqual(plan["status"], "failed")
        self.assertEqual(plan["skip_reason"], "vnpy_cancel_timeout")
        self.assertEqual(plan["order_result"]["reason"], "vnpy_cancel_timeout")
        self.assertEqual(plan["order_result"]["raw"]["timeout"]["previous_status"], "cancel_requested")
        self.assertEqual(plan["order_result"]["raw"]["timeout"]["vt_orderid"], "SIM.CANCEL")
        self.assertEqual(refreshed["decisions"][0]["status"], "failed")
        self.assertEqual(refreshed["decisions"][0]["reason"], "vnpy_cancel_timeout")
        triggers = AlertService().list_triggers(target="vnpy_paper", status="failed", page_size=10)["items"]
        self.assertEqual(len(triggers), 1)
        self.assertEqual(triggers[0]["reason"], "vnpy_cancel_timeout")
        self.assertEqual(triggers[0]["threshold"], 1800.0)
        self.assertIn('"event_type": "vnpy_cancel_timeout"', triggers[0]["diagnostics"])
        self.assertIn("auto-retry-cancel-timeout-plan", triggers[0]["diagnostics"])

    def test_auto_trade_blocks_stale_data_quality_from_paper_execution(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_strategy": "dual_low",
                "auto_max_results": 1,
                "auto_cash_per_order": 1200,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "quality_status": "stale",
            "source_errors": ["last_good_cache_only"],
            "candidates": [
                {"code": "600519", "name": "贵州茅台", "score": 80, "price": 10.0},
            ],
            "warnings": [],
        }

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ):
            result = self.service.run_auto_trade_once()

        self.assertTrue(result["accepted"])
        self.assertEqual(result["reason"], "data_quality_stale")
        self.assertEqual(result["submitted_count"], 0)
        self.assertEqual(result["skipped_count"], 1)
        self.assertEqual(result["orders"][0]["reason"], "data_quality_stale")
        audit = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        self.assertIsNotNone(audit)
        assert audit is not None
        self.assertEqual(audit["diagnostics"]["data_quality"]["status"], "stale")
        timings = audit["diagnostics"]["stage_timings"]
        self.assertEqual(timings["completed_stage"], "candidate_decision_execution")
        self.assertIn("alphasift_screen_seconds", timings)
        self.assertGreaterEqual(timings["total_seconds"], timings["alphasift_screen_seconds"])
        self.assertEqual(audit["decisions"][0]["reason"], "data_quality_stale")
        self.assertEqual(audit["trade_plans"][0]["status"], "skipped")
        self.assertEqual(audit["trade_plans"][0]["skip_reason"], "data_quality_stale")
        triggers = AlertService().list_triggers(target="vnpy_paper", status="degraded", page_size=10)["items"]
        self.assertEqual(len(triggers), 1)
        self.assertEqual(triggers[0]["reason"], "data_quality_stale")
        self.assertEqual(triggers[0]["observed_value"], 1.0)
        self.assertIn('"event_type": "data_quality_stale"', triggers[0]["diagnostics"])
        self.assertIn(result["agent_run_uid"], triggers[0]["diagnostics"])

    def test_auto_trade_records_alert_when_alphasift_screen_fails(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_strategy": "dual_low",
                "auto_max_results": 1,
                "auto_cash_per_order": 1200,
            }
        )

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            side_effect=RuntimeError("screen boom"),
        ), self.assertRaisesRegex(RuntimeError, "screen boom"):
            self.service.run_auto_trade_once()

        triggers = AlertService().list_triggers(target="vnpy_paper", status="failed", page_size=10)["items"]
        self.assertEqual(len(triggers), 1)
        self.assertEqual(triggers[0]["reason"], "screen boom")
        self.assertIn('"event_type": "alphasift_screen_failed"', triggers[0]["diagnostics"])
        detail = self.service.agent_repo.list_recent_runs(trigger_source="vnpy_paper_auto", limit=1)[0]
        self.assertEqual(detail["status"], "failed")
        self.assertEqual(detail["error"], "screen boom")
        audit = self.service.agent_repo.get_run_detail(detail["run_uid"])
        self.assertIsNotNone(audit)
        assert audit is not None
        timings = audit["diagnostics"]["stage_timings"]
        self.assertEqual(timings["completed_stage"], "alphasift_screen")
        self.assertIn("planning_seconds", timings)
        self.assertIn("preflight_seconds", timings)
        self.assertGreaterEqual(timings["alphasift_screen_seconds"], 0.0)
        self.assertTrue(any(event["stage"] == "performance" for event in audit["timeline"]))

    def test_auto_trade_stop_loss_sells_existing_position_before_new_buys(self) -> None:
        self.service.submit_order(
            symbol="600519",
            side="buy",
            market="cn",
            quantity=100,
            price=10.0,
        )
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_sell_enabled": True,
                "auto_stop_loss_pct": 5,
                "auto_strategy": "dual_low",
                "auto_max_results": 1,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "candidates": [],
            "warnings": [],
        }

        with patch(
            "src.services.portfolio_service.PortfolioService._fetch_realtime_position_price",
            return_value=(9.0, "unit-test"),
        ), patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ):
            result = self.service.run_auto_trade_once()

        self.assertTrue(result["accepted"])
        self.assertEqual(result["candidate_count"], 0)
        self.assertEqual(result["submitted_count"], 1)
        self.assertEqual(result["orders"][0]["side"], "sell")
        self.assertEqual(result["orders"][0]["symbol"], "600519")
        self.assertEqual(result["orders"][0]["quantity"], 100.0)
        self.assertEqual(result["orders"][0]["price"], 9.0)

        audit = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        self.assertIsNotNone(audit)
        assert audit is not None
        self.assertEqual(audit["submitted_count"], 1)
        self.assertEqual(audit["decisions"][0]["action"], "sell")
        self.assertEqual(audit["decisions"][0]["reason"], "stop_loss_triggered")
        self.assertEqual(audit["trade_plans"][0]["side"], "sell")
        self.assertEqual(audit["trade_plans"][0]["status"], "filled")
        self.assertIsNone(audit["trade_plans"][0]["skip_reason"])

    def test_auto_trade_stop_loss_can_sell_partial_position(self) -> None:
        self.service.submit_order(
            symbol="600519",
            side="buy",
            market="cn",
            quantity=100,
            price=10.0,
        )
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_sell_enabled": True,
                "auto_stop_loss_pct": 5,
                "auto_sell_position_pct": 50,
                "auto_strategy": "dual_low",
                "auto_max_results": 1,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "candidates": [],
            "warnings": [],
        }

        with patch(
            "src.services.portfolio_service.PortfolioService._fetch_realtime_position_price",
            return_value=(9.0, "unit-test"),
        ), patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ):
            result = self.service.run_auto_trade_once()

        self.assertTrue(result["accepted"])
        self.assertEqual(result["submitted_count"], 1)
        self.assertEqual(result["orders"][0]["side"], "sell")
        self.assertEqual(result["orders"][0]["quantity"], 50.0)
        self.assertEqual(result["orders"][0]["price"], 9.0)

        audit = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        self.assertIsNotNone(audit)
        assert audit is not None
        self.assertEqual(audit["decisions"][0]["quantity"], 50.0)
        self.assertEqual(audit["decisions"][0]["raw_candidate"]["position_quantity"], 100.0)
        self.assertEqual(audit["decisions"][0]["raw_candidate"]["sell_position_pct"], 50.0)
        self.assertEqual(audit["trade_plans"][0]["planned_quantity"], 50.0)
        self.assertEqual(audit["trade_plans"][0]["submitted_quantity"], 50.0)
        self.assertEqual(
            audit["trade_plans"][0]["order_result"]["position_plan"]["planned_quantity"],
            50.0,
        )
        self.assertEqual(
            audit["trade_plans"][0]["order_result"]["position_plan"]["sizing_method"],
            "position_pct",
        )
        self.assertEqual(
            audit["trade_plans"][0]["order_result"]["position_plan"]["sell_position_pct"],
            50.0,
        )

    def test_auto_trade_signal_exit_sells_position_when_enabled(self) -> None:
        self.service.submit_order(
            symbol="600519",
            side="buy",
            market="cn",
            quantity=100,
            price=10.0,
        )
        DecisionSignalService().create_signal(
            {
                "stock_code": "SH600519",
                "stock_name": "贵州茅台",
                "market": "cn",
                "source_type": "manual",
                "source_agent": "unit-test",
                "source_report_id": 9301,
                "trace_id": "vnpy-signal-exit-9301",
                "trigger_source": "unit-test",
                "action": "sell",
                "confidence": 0.8,
                "score": 80,
                "horizon": "3d",
                "reason": "策略失效",
                "risk_summary": "跌破防守线",
            }
        )
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_sell_enabled": True,
                "auto_signal_exit_enabled": True,
                "auto_strategy": "dual_low",
                "auto_max_results": 1,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "candidates": [],
            "warnings": [],
        }

        with patch(
            "src.services.portfolio_service.PortfolioService._fetch_realtime_position_price",
            return_value=(10.0, "unit-test"),
        ), patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ):
            result = self.service.run_auto_trade_once()

        self.assertTrue(result["accepted"])
        self.assertEqual(result["submitted_count"], 1)
        self.assertEqual(result["orders"][0]["side"], "sell")
        self.assertEqual(result["orders"][0]["quantity"], 100.0)

        audit = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        self.assertIsNotNone(audit)
        assert audit is not None
        self.assertEqual(audit["decisions"][0]["reason"], "strategy_invalidated")
        self.assertEqual(audit["decisions"][0]["raw_candidate"]["decision_signal"]["action"], "sell")
        self.assertEqual(
            audit["trade_plans"][0]["order_result"]["risk_review"]["reason"],
            "strategy_invalidated",
        )
        self.assertEqual(
            audit["diagnostics"]["agent_plan"]["sell_policy"]["signal_exit_enabled"],
            True,
        )

    def test_auto_trade_no_progress_timeout_sells_position(self) -> None:
        self.service.submit_order(
            symbol="600519",
            side="buy",
            market="cn",
            quantity=100,
            price=10.0,
        )
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_sell_enabled": True,
                "auto_no_progress_days": 5,
                "auto_no_progress_min_return_pct": 1,
                "auto_strategy": "dual_low",
                "auto_max_results": 1,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "candidates": [],
            "warnings": [],
        }

        with patch(
            "src.services.portfolio_service.PortfolioService._fetch_realtime_position_price",
            return_value=(10.0, "unit-test"),
        ), patch.object(
            self.service,
            "_position_holding_days",
            return_value=5,
        ), patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ):
            result = self.service.run_auto_trade_once()

        self.assertTrue(result["accepted"])
        self.assertEqual(result["submitted_count"], 1)
        self.assertEqual(result["orders"][0]["side"], "sell")
        audit = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        self.assertIsNotNone(audit)
        assert audit is not None
        self.assertEqual(audit["decisions"][0]["reason"], "no_progress_timeout")
        self.assertEqual(audit["decisions"][0]["raw_candidate"]["holding_days"], 5)
        self.assertEqual(
            audit["diagnostics"]["agent_plan"]["sell_policy"]["no_progress_days"],
            5,
        )

    def test_auto_trade_rebalance_sells_single_position_excess_when_enabled(self) -> None:
        self.service.submit_order(
            symbol="600519",
            side="buy",
            market="cn",
            quantity=200,
            price=10.0,
        )
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_sell_enabled": True,
                "auto_rebalance_enabled": True,
                "auto_max_single_position_value": 1500,
                "auto_strategy": "dual_low",
                "auto_max_results": 1,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "candidates": [],
            "warnings": [],
        }

        with patch(
            "src.services.portfolio_service.PortfolioService._fetch_realtime_position_price",
            return_value=(10.0, "unit-test"),
        ), patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ):
            result = self.service.run_auto_trade_once()

        self.assertTrue(result["accepted"])
        self.assertEqual(result["candidate_count"], 0)
        self.assertEqual(result["submitted_count"], 1)
        self.assertEqual(result["orders"][0]["side"], "sell")
        self.assertEqual(result["orders"][0]["symbol"], "600519")
        self.assertEqual(result["orders"][0]["quantity"], 50.0)
        self.assertEqual(result["orders"][0]["price"], 10.0)

        audit = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        self.assertIsNotNone(audit)
        assert audit is not None
        self.assertEqual(audit["decisions"][0]["reason"], "rebalance_single_position_value_exceeded")
        self.assertEqual(audit["decisions"][0]["raw_candidate"]["position_quantity"], 200.0)
        self.assertEqual(
            audit["decisions"][0]["raw_candidate"]["rebalance_plan"]["excess_value"],
            500.0,
        )
        self.assertEqual(
            audit["diagnostics"]["agent_plan"]["sell_policy"]["rebalance_enabled"],
            True,
        )
        self.assertEqual(audit["trade_plans"][0]["planned_quantity"], 50.0)
        self.assertEqual(audit["trade_plans"][0]["submitted_quantity"], 50.0)

    def test_auto_trade_rebalance_sells_target_position_weight_excess(self) -> None:
        self.service.submit_order(
            symbol="600519",
            side="buy",
            market="cn",
            quantity=200,
            price=10.0,
        )
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_sell_enabled": True,
                "auto_rebalance_enabled": True,
                "auto_target_position_weights": {"600519": 1},
                "auto_strategy": "dual_low",
                "auto_max_results": 1,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "candidates": [],
            "warnings": [],
        }

        with patch(
            "src.services.portfolio_service.PortfolioService._fetch_realtime_position_price",
            return_value=(10.0, "unit-test"),
        ), patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ):
            result = self.service.run_auto_trade_once()

        self.assertTrue(result["accepted"])
        self.assertEqual(result["submitted_count"], 1)
        self.assertEqual(result["orders"][0]["side"], "sell")
        self.assertEqual(result["orders"][0]["symbol"], "600519")
        self.assertEqual(result["orders"][0]["quantity"], 100.0)

        audit = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        self.assertIsNotNone(audit)
        assert audit is not None
        self.assertEqual(audit["decisions"][0]["reason"], "rebalance_target_position_weight_exceeded")
        rebalance_plan = audit["decisions"][0]["raw_candidate"]["rebalance_plan"]
        self.assertEqual(rebalance_plan["target_weight_pct"], 1.0)
        self.assertEqual(rebalance_plan["threshold_value"], 1000.0)
        self.assertEqual(rebalance_plan["excess_value"], 1000.0)
        self.assertEqual(
            audit["diagnostics"]["agent_plan"]["sell_policy"]["target_position_weights"],
            {"600519": 1.0},
        )
        self.assertEqual(audit["trade_plans"][0]["planned_quantity"], 100.0)

    def test_auto_rebalance_plan_supports_target_industry_weight(self) -> None:
        status = self.service.update_settings(
            {
                "auto_rebalance_enabled": True,
                "auto_target_industry_weights": {"白酒": 1},
            },
            include_snapshot=False,
            include_recent_trades=False,
        )
        settings = self.service.get_settings()
        self.assertEqual(status["settings"]["auto_target_industry_weights"], {"白酒": 1.0})

        plans = self.service._auto_rebalance_plan_map(
            settings=settings,
            snapshot={
                "accounts": [
                    {
                        "total_equity": 100000,
                        "total_market_value": 2000,
                    }
                ]
            },
            positions=[
                {
                    "symbol": "600519",
                    "market": "cn",
                    "quantity": 200,
                    "last_price": 10.0,
                    "market_value": 2000,
                    "industry": "白酒",
                }
            ],
        )

        plan = plans[("cn", "600519")]
        self.assertEqual(plan["reason"], "rebalance_target_industry_weight_exceeded")
        self.assertEqual(plan["quantity"], 100.0)
        self.assertEqual(plan["threshold_value"], 1000.0)
        self.assertEqual(plan["current_value"], 2000.0)
        self.assertEqual(plan["target_weight_pct"], 1.0)
        self.assertEqual(plan["industry"], "白酒")

    def test_auto_trade_trailing_stop_sells_after_peak_drawdown(self) -> None:
        self.service.submit_order(
            symbol="600519",
            side="buy",
            market="cn",
            quantity=100,
            price=10.0,
        )
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_sell_enabled": True,
                "auto_trailing_stop_pct": 15,
                "auto_strategy": "dual_low",
                "auto_max_results": 1,
            }
        )
        payload = self.service._read_config_payload()
        payload["auto_trailing_peaks"] = {"600519": 12.0}
        self.service._write_config_payload(payload)
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "candidates": [],
            "warnings": [],
        }

        with patch(
            "src.services.portfolio_service.PortfolioService._fetch_realtime_position_price",
            return_value=(10.0, "unit-test"),
        ), patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ):
            result = self.service.run_auto_trade_once()

        self.assertEqual(result["submitted_count"], 1)
        self.assertEqual(result["orders"][0]["side"], "sell")
        self.assertEqual(result["orders"][0]["reason"], None)
        audit = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        self.assertIsNotNone(audit)
        assert audit is not None
        self.assertEqual(audit["decisions"][0]["reason"], "trailing_stop_triggered")
        self.assertEqual(audit["trade_plans"][0]["side"], "sell")
        self.assertEqual(audit["trade_plans"][0]["status"], "filled")
        self.assertEqual(self.service._read_config_payload().get("auto_trailing_peaks"), {})

    def test_auto_sell_vnpy_paper_submits_exit_order_without_local_fill(self) -> None:
        installed = _install_fake_vnpy_modules()
        main_engine = _FakeMainEngine()
        self.service = VnpyPaperTradingService(
            data_fetcher_manager=_FakeDataFetcherManager(price=9.0),
            config_path=self.config_path,
            vnpy_main_engine=main_engine,
        )
        self.service.submit_order(
            symbol="600519",
            side="buy",
            market="cn",
            quantity=100,
            price=10.0,
        )
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_execution_mode": "vnpy_paper",
                "vnpy_gateway_name": "SIM",
                "auto_sell_enabled": True,
                "auto_stop_loss_pct": 5,
                "auto_strategy": "dual_low",
                "auto_max_results": 1,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "quality_status": "ok",
            "candidates": [],
            "warnings": [],
            "source_errors": [],
        }

        try:
            with patch(
                "src.services.portfolio_service.PortfolioService._fetch_realtime_position_price",
                return_value=(9.0, "unit-test"),
            ), patch(
                "src.services.vnpy_paper_trading_service.AlphaSiftService",
                return_value=fake_alphasift,
            ):
                result = self.service.run_auto_trade_once()
                duplicate_result = self.service.run_auto_trade_once()
        finally:
            _restore_modules(installed)

        self.assertTrue(result["accepted"])
        self.assertEqual(result["submitted_count"], 1)
        self.assertEqual(result["orders"][0]["side"], "sell")
        self.assertEqual(result["orders"][0]["status"], "submitted")
        self.assertEqual(result["orders"][0]["source"], "vnpy_main_engine")
        self.assertEqual(main_engine.calls[0][1], "SIM")
        self.assertEqual(main_engine.calls[0][0].symbol, "600519")
        self.assertEqual(main_engine.calls[0][0].direction, "SHORT")
        self.assertEqual(len(main_engine.calls), 1)
        self.assertEqual(duplicate_result["orders"][0]["reason"], "active_vnpy_order_exists")

        trades = self.service.portfolio.list_trade_events(
            account_id=int(self.service.get_settings().account_id),
            page=1,
        )
        self.assertEqual(len(trades["items"]), 1)
        self.assertEqual(trades["items"][0]["side"], "buy")

        audit = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        self.assertIsNotNone(audit)
        assert audit is not None
        self.assertEqual(audit["trade_plans"][0]["side"], "sell")
        self.assertEqual(audit["trade_plans"][0]["status"], "submitted")
        self.assertEqual(audit["trade_plans"][0]["execution_mode"], "vnpy_paper")
        self.assertIsNone(audit["trade_plans"][0]["trade_id"])
        duplicate_audit = self.service.agent_repo.get_run_detail(duplicate_result["agent_run_uid"])
        self.assertIsNotNone(duplicate_audit)
        assert duplicate_audit is not None
        self.assertEqual(duplicate_audit["trade_plans"][0]["skip_reason"], "active_vnpy_order_exists")

    def test_auto_trade_vnpy_paper_skips_candidate_with_active_submitted_buy(self) -> None:
        installed = _install_fake_vnpy_modules()
        main_engine = _FakeMainEngine()
        self.service = VnpyPaperTradingService(
            data_fetcher_manager=_FakeDataFetcherManager(price=10.0),
            config_path=self.config_path,
            vnpy_main_engine=main_engine,
        )
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_execution_mode": "vnpy_paper",
                "vnpy_gateway_name": "SIM",
                "auto_strategy": "dual_low",
                "auto_max_results": 1,
                "auto_cash_per_order": 1200,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "quality_status": "ok",
            "candidates": [
                {"code": "600519", "name": "贵州茅台", "score": 80, "price": 10.0, "amount": 200000000},
            ],
            "warnings": [],
            "source_errors": [],
        }

        try:
            with patch(
                "src.services.vnpy_paper_trading_service.AlphaSiftService",
                return_value=fake_alphasift,
            ):
                first_result = self.service.run_auto_trade_once()
                second_result = self.service.run_auto_trade_once()
        finally:
            _restore_modules(installed)

        self.assertEqual(first_result["orders"][0]["status"], "submitted")
        self.assertEqual(second_result["submitted_count"], 0)
        self.assertEqual(second_result["skipped_count"], 1)
        self.assertEqual(second_result["orders"][0]["reason"], "active_vnpy_order_exists")
        self.assertEqual(len(main_engine.calls), 1)

        audit = self.service.agent_repo.get_run_detail(second_result["agent_run_uid"])
        self.assertIsNotNone(audit)
        assert audit is not None
        self.assertEqual(audit["decisions"][0]["reason"], "active_vnpy_order_exists")
        self.assertEqual(audit["trade_plans"][0]["skip_reason"], "active_vnpy_order_exists")

    def test_auto_trade_time_gate_blocks_paper_execution_outside_session(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": True,
                "auto_strategy": "dual_low",
                "auto_max_results": 1,
            }
        )
        fake_context = SimpleNamespace(
            phase=SimpleNamespace(value="postmarket"),
            is_market_open_now=False,
            to_dict=lambda: {"phase": "postmarket", "is_market_open_now": False},
        )

        with patch(
            "src.services.vnpy_paper_trading_service.trading_calendar.build_market_phase_context",
            return_value=fake_context,
        ) as build_phase, patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
        ) as alphasift_cls:
            result = self.service.run_auto_trade_once()

        build_phase.assert_called_once_with(
            market="cn",
            trigger_source="vnpy_paper_auto",
            analysis_intent="auto",
        )
        self.assertFalse(result["accepted"])
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "outside_trading_session")
        alphasift_cls.assert_not_called()
        audit = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        self.assertIsNotNone(audit)
        assert audit is not None
        self.assertEqual(audit["status"], "skipped")
        self.assertEqual(audit["error"], "outside_trading_session")
        self.assertEqual(audit["diagnostics"]["market_phase"]["phase"], "postmarket")

    def test_auto_trade_time_gate_does_not_block_dry_run_plans(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": True,
                "auto_execution_mode": "dry_run",
                "auto_strategy": "dual_low",
                "auto_max_results": 1,
                "auto_cash_per_order": 1200,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "candidates": [
                {"code": "600519", "score": 80, "price": 10.0},
            ],
            "warnings": [],
        }

        with patch(
            "src.services.vnpy_paper_trading_service.trading_calendar.build_market_phase_context",
            side_effect=AssertionError("dry-run should not ask for market phase"),
        ), patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ):
            result = self.service.run_auto_trade_once()

        self.assertTrue(result["accepted"])
        self.assertEqual(result["planned_count"], 1)
        self.assertEqual(result["orders"][0]["status"], "planned")

    def test_auto_trade_score_weighted_allocation_sizes_candidates_by_score(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_execution_mode": "dry_run",
                "auto_score_weighted_allocation_enabled": True,
                "auto_allocation_budget": 10000,
                "auto_cash_per_order": 10000,
                "auto_strategy": "dual_low",
                "auto_max_results": 2,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "candidates": [
                {"code": "600519", "score": 80, "price": 10.0},
                {"code": "000001", "score": 20, "price": 10.0},
            ],
            "warnings": [],
        }

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ):
            result = self.service.run_auto_trade_once()

        self.assertEqual(result["planned_count"], 2)
        self.assertEqual([item["cash_amount"] for item in result["orders"]], [8000.0, 2000.0])
        allocation = result["orders"][0]["raw"]["portfolio_allocation"]
        self.assertEqual(allocation["method"], "score_weighted_capped")
        self.assertEqual(allocation["score_weight"], 0.8)
        audit = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        self.assertIsNotNone(audit)
        assert audit is not None
        run_allocation = audit["diagnostics"]["portfolio_allocation"]
        self.assertEqual(run_allocation["resolved_budget"], 10000.0)
        self.assertEqual(run_allocation["allocated_budget"], 10000.0)
        position_plan = audit["decisions"][0]["order_result"]["position_plan"]
        self.assertEqual(position_plan["sizing_method"], "score_weighted_allocation")
        self.assertEqual(position_plan["portfolio_allocation"]["score_weight"], 0.8)

    def test_screen_data_quality_score_is_complete_with_healthy_observed_sources(self) -> None:
        candidates = [{
            "code": "600519",
            "data_quality": "ok",
            "missing_fields": [],
            "data_sources": ["snapshot", "daily"],
        }]
        screen = {
            "quality_status": "ok",
            "candidates": candidates,
            "source_health": {
                "snapshot": {"sina": {"successes": 1, "failures": 0, "last_rows": 5000}},
                "daily": {"eastmoney": {"successes": 1, "failures": 0, "last_rows": 120}},
            },
        }

        quality = self.service._screen_data_quality(screen, candidates)
        quality.update(self.service._screen_data_quality_score(screen, candidates, quality))

        self.assertEqual(quality["score"], 100.0)
        self.assertEqual(quality["grade"], "excellent")
        self.assertEqual(quality["components"]["source_health"]["provider_count"], 2)
        self.assertFalse(quality["methodology"]["uses_llm"])

    def test_screen_data_quality_score_includes_candidate_context_sources(self) -> None:
        candidates = [{
            "code": "600519",
            "data_quality": "ok",
            "missing_fields": [],
            "data_sources": ["snapshot", "daily"],
        }]
        screen = {
            "quality_status": "ok",
            "candidates": candidates,
            "source_health": {
                "snapshot": {
                    "sina": {"status": "ok", "successes": 1, "failures": 0},
                },
                "candidate_context": {
                    "quote": {"status": "ok", "successes": 1, "failures": 0},
                    "fund_flow": {"status": "ok", "successes": 1, "failures": 0},
                    "news": {"status": "unavailable", "successes": 0, "failures": 1},
                },
            },
        }

        quality = self.service._screen_data_quality(screen, candidates)
        quality.update(self.service._screen_data_quality_score(screen, candidates, quality))

        self.assertEqual(quality["score"], 95.0)
        source_component = quality["components"]["source_health"]
        self.assertEqual(source_component["score"], 75.0)
        self.assertEqual(source_component["provider_count"], 4)
        provider_scores = {
            item["source"]: item["score"] for item in source_component["providers"]
        }
        self.assertEqual(provider_scores["candidate_context.news"], 0.0)
        self.assertEqual(provider_scores["candidate_context.fund_flow"], 100.0)

    def test_default_data_quality_gate_blocks_poor_run(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_execution_mode": "dry_run",
                "auto_max_results": 1,
                "auto_cash_per_order": 10000,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "quality_status": "partial",
            "candidates": [{
                "code": "600519",
                "score": 80,
                "price": 10.0,
                "data_quality": "partial",
                "missing_fields": ["industry"],
                "data_sources": ["snapshot"],
            }],
            "source_health": {
                "snapshot": {
                    "sina": {"status": "unavailable", "failures": 1},
                },
                "candidate_context": {
                    "quote": {"status": "ok", "successes": 1},
                    "fund_flow": {"status": "unavailable", "failures": 1},
                    "news": {"status": "ok", "successes": 1},
                },
            },
            "warnings": ["snapshot_fallback", "fund_flow_unavailable"],
            "source_errors": ["sina_timeout", "capital_flow_unavailable"],
        }

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ):
            result = self.service.run_auto_trade_once()

        self.assertEqual(result["reason"], "data_quality_score_below_threshold")
        self.assertEqual(result["submitted_count"], 0)
        self.assertEqual(result["skipped_count"], 1)
        audit = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        self.assertIsNotNone(audit)
        assert audit is not None
        self.assertLess(audit["diagnostics"]["data_quality"]["score"], 60.0)
        self.assertEqual(
            audit["diagnostics"]["agent_plan"]["gates"]["min_data_quality_score"],
            60.0,
        )

    def test_zero_data_quality_threshold_keeps_audit_only_mode(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_execution_mode": "dry_run",
                "auto_min_data_quality_score": 0,
                "auto_max_results": 1,
                "auto_cash_per_order": 10000,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "quality_status": "partial",
            "candidates": [{
                "code": "600519",
                "score": 80,
                "price": 10.0,
                "data_quality": "partial",
                "missing_fields": ["industry"],
                "data_sources": ["snapshot"],
            }],
            "source_health": {
                "snapshot": {
                    "sina": {"status": "unavailable", "failures": 1},
                },
            },
            "warnings": ["snapshot_fallback"],
            "source_errors": ["sina_timeout"],
        }

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ):
            result = self.service.run_auto_trade_once()

        self.assertEqual(result["planned_count"], 1)
        self.assertEqual(result["submitted_count"], 0)
        self.assertEqual(result["skipped_count"], 0)
        audit = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        self.assertIsNotNone(audit)
        assert audit is not None
        self.assertLess(audit["diagnostics"]["data_quality"]["score"], 60.0)
        self.assertEqual(
            audit["diagnostics"]["agent_plan"]["gates"]["min_data_quality_score"],
            0.0,
        )

    def test_auto_trade_data_quality_score_gate_blocks_partial_run_below_threshold(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_execution_mode": "dry_run",
                "auto_min_data_quality_score": 75,
                "auto_max_results": 1,
                "auto_cash_per_order": 10000,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "quality_status": "partial",
            "candidates": [{
                "code": "600519",
                "score": 80,
                "price": 10.0,
                "data_quality": "partial",
                "missing_fields": ["industry", "trading_status"],
                "data_sources": ["snapshot"],
            }],
            "source_health": {
                "snapshot": {"sina": {"successes": 0, "failures": 2, "last_rows": 0}},
            },
            "warnings": [],
            "source_errors": [],
        }

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ), patch.object(
            self.service,
            "_record_auto_trade_alert_event",
        ) as record_alert:
            result = self.service.run_auto_trade_once()

        self.assertEqual(result["reason"], "data_quality_score_below_threshold")
        self.assertEqual(result["planned_count"], 0)
        self.assertEqual(result["orders"][0]["reason"], "data_quality_score_below_threshold")
        audit = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        assert audit is not None
        quality = audit["diagnostics"]["data_quality"]
        self.assertEqual(quality["score"], 60.15)
        self.assertEqual(quality["grade"], "guarded")
        self.assertEqual(
            audit["diagnostics"]["agent_plan"]["gates"]["min_data_quality_score"],
            75.0,
        )
        record_alert.assert_called_once()
        alert_args, alert_kwargs = record_alert.call_args
        self.assertEqual(alert_args[0], "data_quality_score_below_threshold")
        self.assertEqual(alert_kwargs["reason"], "data_quality_score_below_threshold")
        self.assertEqual(alert_kwargs["observed_value"], 60.15)
        self.assertEqual(alert_kwargs["threshold"], 75.0)

    def test_auto_trade_score_weighted_allocation_redistributes_candidate_cap(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_execution_mode": "dry_run",
                "auto_score_weighted_allocation_enabled": True,
                "auto_allocation_budget": 10000,
                "auto_cash_per_order": 10000,
                "auto_max_single_position_value": 6000,
                "auto_strategy": "dual_low",
                "auto_max_results": 2,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "candidates": [
                {"code": "600519", "score": 90, "price": 10.0},
                {"code": "000001", "score": 10, "price": 10.0},
            ],
            "warnings": [],
        }

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ):
            result = self.service.run_auto_trade_once()

        self.assertEqual([item["cash_amount"] for item in result["orders"]], [6000.0, 4000.0])
        self.assertEqual(
            result["orders"][0]["raw"]["portfolio_allocation"]["candidate_cap"],
            6000.0,
        )

    def test_auto_trade_score_weighted_allocation_shares_industry_headroom(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_execution_mode": "dry_run",
                "auto_score_weighted_allocation_enabled": True,
                "auto_allocation_budget": 10000,
                "auto_cash_per_order": 10000,
                "auto_max_industry_position_value": 6000,
                "auto_strategy": "dual_low",
                "auto_max_results": 3,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "candidates": [
                {"code": "600519", "score": 80, "price": 10.0, "industry": "白酒"},
                {"code": "000001", "score": 40, "price": 10.0, "industry": "白酒"},
                {"code": "000002", "score": 20, "price": 10.0, "industry": "地产"},
            ],
            "warnings": [],
        }

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ):
            result = self.service.run_auto_trade_once()

        self.assertEqual(result["planned_count"], 3)
        self.assertEqual([item["cash_amount"] for item in result["orders"]], [4000.0, 2000.0, 4000.0])
        liquor_total = sum(
            float(item["cash_amount"])
            for item in result["orders"]
            if item["raw"].get("industry") == "白酒"
        )
        self.assertEqual(liquor_total, 6000.0)

    def test_auto_trade_score_weighted_allocation_uses_daily_budget_headroom(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_execution_mode": "dry_run",
                "auto_score_weighted_allocation_enabled": True,
                "auto_allocation_budget": 10000,
                "auto_cash_per_order": 10000,
                "auto_daily_budget": 5000,
                "auto_strategy": "dual_low",
                "auto_max_results": 2,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "candidates": [
                {"code": "600519", "score": 80, "price": 10.0},
                {"code": "000001", "score": 20, "price": 10.0},
            ],
            "warnings": [],
        }

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ):
            result = self.service.run_auto_trade_once()

        self.assertEqual([item["cash_amount"] for item in result["orders"]], [4000.0, 1000.0])
        audit = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        self.assertIsNotNone(audit)
        assert audit is not None
        constraints = audit["diagnostics"]["portfolio_allocation"]["constraints"]
        daily = next(item for item in constraints if item["type"] == "daily_budget_headroom")
        self.assertEqual(daily["headroom"], 5000.0)

    def test_auto_trade_score_weighted_allocation_audits_cn_lot_residual(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_execution_mode": "dry_run",
                "auto_score_weighted_allocation_enabled": True,
                "auto_allocation_budget": 10000,
                "auto_cash_per_order": 10000,
                "auto_strategy": "dual_low",
                "auto_max_results": 2,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "candidates": [
                {"code": "600519", "score": 75, "price": 10.0},
                {"code": "000001", "score": 25, "price": 10.0},
            ],
            "warnings": [],
        }

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ):
            result = self.service.run_auto_trade_once()

        self.assertEqual([item["cash_amount"] for item in result["orders"]], [7000.0, 2000.0])
        self.assertTrue(result["orders"][0]["raw"]["target_weight_sizing"]["lot_adjusted"])
        audit = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        self.assertIsNotNone(audit)
        assert audit is not None
        allocation = audit["diagnostics"]["portfolio_allocation"]
        self.assertEqual(allocation["allocated_budget"], 10000.0)
        self.assertEqual(allocation["executable_planned_budget"], 9000.0)
        self.assertEqual(allocation["execution_residual"], 1000.0)

    def test_auto_trade_score_weighted_allocation_respects_remaining_daily_order_slots(self) -> None:
        self.service.submit_order(
            symbol="300001",
            side="buy",
            market="cn",
            quantity=100,
            price=10.0,
            source="alphasift_auto",
        )
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_execution_mode": "dry_run",
                "auto_score_weighted_allocation_enabled": True,
                "auto_allocation_budget": 10000,
                "auto_cash_per_order": 10000,
                "auto_daily_max_orders": 2,
                "auto_strategy": "dual_low",
                "auto_max_results": 2,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "candidates": [
                {"code": "600519", "score": 80, "price": 10.0},
                {"code": "000001", "score": 20, "price": 10.0},
            ],
            "warnings": [],
        }

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ):
            result = self.service.run_auto_trade_once()

        self.assertEqual(result["planned_count"], 1)
        self.assertEqual(result["skipped_count"], 1)
        self.assertEqual(result["orders"][0]["cash_amount"], 10000.0)
        self.assertEqual(result["orders"][1]["reason"], "daily_order_limit_reached")

    def test_auto_trade_inverse_volatility_allocation_prefers_lower_risk_at_equal_score(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_execution_mode": "dry_run",
                "auto_score_weighted_allocation_enabled": True,
                "auto_allocation_method": "score_inverse_volatility_20d",
                "auto_allocation_budget": 10000,
                "auto_cash_per_order": 10000,
                "auto_max_results": 2,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "candidates": [
                {"code": "600519", "score": 80, "price": 10.0, "volatility_20d_pct": 10},
                {"code": "000001", "score": 80, "price": 10.0, "volatility_20d_pct": 40},
            ],
            "warnings": [],
        }

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ):
            result = self.service.run_auto_trade_once()

        self.assertEqual([item["cash_amount"] for item in result["orders"]], [8000.0, 2000.0])
        allocation = result["orders"][0]["raw"]["portfolio_allocation"]
        self.assertEqual(allocation["method"], "score_inverse_volatility_20d_capped")
        self.assertEqual(allocation["risk_input"]["volatility_20d_pct"], 10.0)
        self.assertEqual(allocation["risk_input"]["source"], "candidate.volatility_20d_pct")
        self.assertEqual(allocation["score_weight"], 0.8)
        audit = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        assert audit is not None
        self.assertEqual(
            audit["diagnostics"]["portfolio_allocation"]["risk_model"]["name"],
            "inverse_volatility_20d",
        )
        self.assertEqual(
            audit["decisions"][0]["order_result"]["position_plan"]["sizing_method"],
            "score_inverse_volatility_20d_allocation",
        )

    def test_auto_trade_inverse_volatility_allocation_fails_closed_without_risk_input(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_execution_mode": "dry_run",
                "auto_score_weighted_allocation_enabled": True,
                "auto_allocation_method": "score_inverse_volatility_20d",
                "auto_allocation_budget": 10000,
                "auto_cash_per_order": 10000,
                "auto_max_results": 2,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "candidates": [
                {"code": "600519", "score": 80, "price": 10.0},
                {
                    "code": "000001",
                    "score": 80,
                    "price": 10.0,
                    "raw": {"volatility_20d_pct": 20},
                },
            ],
            "warnings": [],
        }

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ):
            result = self.service.run_auto_trade_once()

        self.assertEqual(result["planned_count"], 1)
        self.assertEqual(result["skipped_count"], 1)
        self.assertEqual(result["orders"][0]["reason"], "portfolio_allocation_risk_unavailable")
        self.assertEqual(result["orders"][1]["cash_amount"], 10000.0)
        risk_input = result["orders"][0]["raw"]["portfolio_allocation"]["risk_input"]
        self.assertEqual(risk_input["status"], "unavailable")
        self.assertEqual(risk_input["reason"], "missing_or_invalid_volatility_20d_pct")
        planned_risk = result["orders"][1]["raw"]["portfolio_allocation"]["risk_input"]
        self.assertEqual(planned_risk["source"], "candidate.raw.volatility_20d_pct")

    def test_auto_trade_inverse_volatility_allocation_applies_configured_floor(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_execution_mode": "dry_run",
                "auto_score_weighted_allocation_enabled": True,
                "auto_allocation_method": "score_inverse_volatility_20d",
                "auto_risk_volatility_floor_pct": 5,
                "auto_allocation_budget": 9000,
                "auto_cash_per_order": 9000,
                "auto_max_results": 2,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "candidates": [
                {"code": "600519", "score": 80, "price": 10.0, "volatility_20d_pct": 0},
                {"code": "000001", "score": 80, "price": 10.0, "volatility_20d_pct": 10},
            ],
            "warnings": [],
        }

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ):
            result = self.service.run_auto_trade_once()

        self.assertEqual([item["cash_amount"] for item in result["orders"]], [6000.0, 3000.0])
        first = result["orders"][0]["raw"]["portfolio_allocation"]
        self.assertTrue(first["risk_input"]["floor_applied"])
        self.assertEqual(first["risk_input"]["effective_volatility_pct"], 5.0)
        self.assertAlmostEqual(first["score_weight"], 2 / 3, places=6)

    def test_auto_trade_inverse_volatility_allocation_redistributes_candidate_cap(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_execution_mode": "dry_run",
                "auto_score_weighted_allocation_enabled": True,
                "auto_allocation_method": "score_inverse_volatility_20d",
                "auto_allocation_budget": 10000,
                "auto_cash_per_order": 10000,
                "auto_max_single_position_value": 6000,
                "auto_max_results": 2,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "candidates": [
                {"code": "600519", "score": 80, "price": 10.0, "volatility_20d_pct": 10},
                {"code": "000001", "score": 80, "price": 10.0, "volatility_20d_pct": 40},
            ],
            "warnings": [],
        }

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ):
            result = self.service.run_auto_trade_once()

        self.assertEqual([item["cash_amount"] for item in result["orders"]], [6000.0, 4000.0])

    def test_auto_trade_correlation_cap_excludes_lower_ranked_high_correlation_candidate(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_execution_mode": "dry_run",
                "auto_score_weighted_allocation_enabled": True,
                "auto_allocation_method": "score_inverse_volatility_20d_correlation_capped",
                "auto_allocation_budget": 10000,
                "auto_cash_per_order": 10000,
                "auto_max_results": 3,
                "auto_correlation_lookback_days": 20,
                "auto_correlation_min_observations": 5,
                "auto_max_pairwise_correlation": 0.8,
            }
        )
        prices = {"600519": 100.0, "000001": 50.0, "300750": 80.0}
        with DatabaseManager.get_instance().get_session() as session:
            for offset in range(21):
                move = 1.01 if offset % 2 else 0.99
                inverse_move = 0.99 if offset % 2 else 1.01
                if offset:
                    prices["600519"] *= move
                    prices["000001"] *= move
                    prices["300750"] *= inverse_move
                bar_date = date.today() - timedelta(days=20 - offset)
                for symbol, close in prices.items():
                    session.add(StockDaily(code=symbol, date=bar_date, close=close))
            session.commit()
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "candidates": [
                {"code": "600519", "score": 80, "price": 10.0, "volatility_20d_pct": 20},
                {"code": "000001", "score": 79, "price": 10.0, "volatility_20d_pct": 20},
                {"code": "300750", "score": 78, "price": 10.0, "volatility_20d_pct": 20},
            ],
            "warnings": [],
        }

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ):
            result = self.service.run_auto_trade_once()

        self.assertEqual(result["planned_count"], 2)
        self.assertEqual(result["skipped_count"], 1)
        self.assertEqual(result["orders"][1]["reason"], "portfolio_allocation_correlation_limit_reached")
        correlation = result["orders"][1]["raw"]["portfolio_allocation"]["risk_input"]["correlation"]
        self.assertTrue(correlation["limit_breached"])
        self.assertEqual(correlation["breached_by_symbol"], "600519")
        self.assertGreater(correlation["observed_correlation"], 0.99)
        self.assertEqual(
            [item["cash_amount"] for item in (result["orders"][0], result["orders"][2])],
            [5000.0, 4000.0],
        )

    def test_auto_trade_correlation_cap_fails_closed_without_trailing_history(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_execution_mode": "dry_run",
                "auto_score_weighted_allocation_enabled": True,
                "auto_allocation_method": "score_inverse_volatility_20d_correlation_capped",
                "auto_max_results": 1,
                "auto_correlation_min_observations": 5,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "candidates": [
                {"code": "600519", "score": 80, "price": 10.0, "volatility_20d_pct": 20},
            ],
            "warnings": [],
        }
        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ):
            result = self.service.run_auto_trade_once()

        self.assertEqual(result["planned_count"], 0)
        self.assertEqual(
            result["orders"][0]["reason"],
            "portfolio_allocation_correlation_data_unavailable",
        )

    def test_auto_trade_correlation_cap_resolves_prefixed_daily_code_variant(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_execution_mode": "dry_run",
                "auto_score_weighted_allocation_enabled": True,
                "auto_allocation_method": "score_inverse_volatility_20d_correlation_capped",
                "auto_max_results": 1,
                "auto_correlation_lookback_days": 20,
                "auto_correlation_min_observations": 5,
            }
        )
        close = 100.0
        with DatabaseManager.get_instance().get_session() as session:
            for offset in range(21):
                if offset:
                    close *= 1.01 if offset % 2 else 0.99
                session.add(
                    StockDaily(
                        code="600519.SH",
                        date=date.today() - timedelta(days=20 - offset),
                        close=close,
                    )
                )
            session.commit()
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "candidates": [
                {"code": "600519", "score": 80, "price": 10.0, "volatility_20d_pct": 20},
            ],
            "warnings": [],
        }
        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ):
            result = self.service.run_auto_trade_once()

        self.assertEqual(result["planned_count"], 1)
        correlation = result["orders"][0]["raw"]["portfolio_allocation"]["risk_input"]["correlation"]
        self.assertEqual(correlation["daily_code"], "600519.SH")
        self.assertGreaterEqual(correlation["observation_count"], 20)

    def test_auto_trade_covariance_optimizer_tracks_arbitrary_position_targets(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_execution_mode": "dry_run",
                "auto_score_weighted_allocation_enabled": True,
                "auto_allocation_method": "target_tracking_min_variance_20d",
                "auto_allocation_budget": 10000,
                "auto_cash_per_order": 10000,
                "auto_max_results": 2,
                "auto_correlation_lookback_days": 20,
                "auto_correlation_min_observations": 5,
                "auto_covariance_risk_penalty": 0,
                "auto_target_position_weights": {"600519": 8, "000001": 2},
            }
        )
        prices = {"600519": 100.0, "000001": 50.0}
        with DatabaseManager.get_instance().get_session() as session:
            for offset in range(21):
                if offset:
                    move = 1.01 if offset % 2 else 0.99
                    prices = {symbol: close * move for symbol, close in prices.items()}
                bar_date = date.today() - timedelta(days=20 - offset)
                for symbol, close in prices.items():
                    session.add(StockDaily(code=symbol, date=bar_date, close=close))
            session.commit()
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "candidates": [
                {"code": "600519", "score": 50, "price": 10.0},
                {"code": "000001", "score": 99, "price": 10.0},
            ],
            "warnings": [],
        }

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ):
            result = self.service.run_auto_trade_once()

        self.assertEqual([item["cash_amount"] for item in result["orders"]], [8000.0, 2000.0])
        audit = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        optimizer = audit["diagnostics"]["portfolio_allocation"]["optimizer"]
        self.assertEqual(optimizer["model"], "target_tracking_min_variance_20d_v1")
        self.assertEqual(optimizer["target_source"], "remaining_position_target_gaps")
        self.assertEqual(optimizer["weights_by_symbol"], {"600519": 0.8, "000001": 0.2})
        self.assertGreaterEqual(optimizer["observation_count"], 20)

    def test_auto_trade_covariance_optimizer_penalizes_high_variance_candidate(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_execution_mode": "dry_run",
                "auto_score_weighted_allocation_enabled": True,
                "auto_allocation_method": "target_tracking_min_variance_20d",
                "auto_allocation_budget": 10000,
                "auto_cash_per_order": 10000,
                "auto_max_results": 2,
                "auto_correlation_lookback_days": 20,
                "auto_correlation_min_observations": 5,
                "auto_covariance_risk_penalty": 1,
            }
        )
        prices = {"600519": 100.0, "000001": 50.0}
        with DatabaseManager.get_instance().get_session() as session:
            for offset in range(21):
                if offset:
                    low_move = 1.005 if offset % 2 else 0.995
                    high_move = 1.05 if offset % 2 else 0.95
                    prices["600519"] *= low_move
                    prices["000001"] *= high_move
                bar_date = date.today() - timedelta(days=20 - offset)
                for symbol, close in prices.items():
                    session.add(StockDaily(code=symbol, date=bar_date, close=close))
            session.commit()
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "candidates": [
                {"code": "600519", "score": 80, "price": 10.0},
                {"code": "000001", "score": 80, "price": 10.0},
            ],
            "warnings": [],
        }

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ):
            result = self.service.run_auto_trade_once()

        audit = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        optimizer = audit["diagnostics"]["portfolio_allocation"]["optimizer"]
        self.assertGreater(
            optimizer["weights_by_symbol"]["600519"],
            optimizer["weights_by_symbol"]["000001"],
        )
        self.assertGreater(result["orders"][0]["cash_amount"], result["orders"][1]["cash_amount"])

    def test_auto_trade_covariance_optimizer_fails_closed_without_history(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_execution_mode": "dry_run",
                "auto_score_weighted_allocation_enabled": True,
                "auto_allocation_method": "target_tracking_min_variance_20d",
                "auto_max_results": 1,
                "auto_correlation_min_observations": 5,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "candidates": [{"code": "600519", "score": 80, "price": 10.0}],
            "warnings": [],
        }

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ):
            result = self.service.run_auto_trade_once()

        self.assertEqual(result["planned_count"], 0)
        self.assertEqual(
            result["orders"][0]["reason"],
            "portfolio_allocation_covariance_data_unavailable",
        )

    def test_auto_trade_respects_max_positions_risk_limit(self) -> None:
        self.service.submit_order(
            symbol="600519",
            side="buy",
            market="cn",
            quantity=100,
            price=10.0,
        )
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_skip_existing_positions": False,
                "auto_max_positions": 1,
                "auto_strategy": "dual_low",
                "auto_max_results": 1,
                "auto_cash_per_order": 1200,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "candidates": [
                {"code": "000001", "score": 80, "price": 10.0},
            ],
            "warnings": [],
        }

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ):
            result = self.service.run_auto_trade_once()

        self.assertEqual(result["submitted_count"], 0)
        self.assertEqual(result["skipped_count"], 1)
        self.assertEqual(result["orders"][0]["reason"], "max_positions_reached")
        audit = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        self.assertIsNotNone(audit)
        assert audit is not None
        self.assertEqual(audit["decisions"][0]["reason"], "max_positions_reached")
        self.assertEqual(audit["trade_plans"][0]["skip_reason"], "max_positions_reached")
        trades = self.service.portfolio.list_trade_events(
            account_id=int(self.service.get_settings().account_id),
            page=1,
        )
        self.assertEqual(len(trades["items"]), 1)

    def test_auto_trade_respects_single_position_value_limit(self) -> None:
        self.service.submit_order(
            symbol="600519",
            side="buy",
            market="cn",
            quantity=100,
            price=10.0,
        )
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_skip_existing_positions": False,
                "auto_max_single_position_value": 1500,
                "auto_strategy": "dual_low",
                "auto_max_results": 1,
                "auto_cash_per_order": 1000,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "candidates": [
                {"code": "600519", "score": 80, "price": 10.0},
            ],
            "warnings": [],
        }

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ):
            result = self.service.run_auto_trade_once()

        self.assertEqual(result["submitted_count"], 0)
        self.assertEqual(result["orders"][0]["reason"], "single_position_value_limit_reached")
        audit = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        self.assertIsNotNone(audit)
        assert audit is not None
        self.assertEqual(audit["decisions"][0]["reason"], "single_position_value_limit_reached")
        self.assertEqual(audit["trade_plans"][0]["skip_reason"], "single_position_value_limit_reached")

    def test_auto_trade_sizes_buy_to_target_position_weight_gap(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_execution_mode": "dry_run",
                "auto_target_position_weights": {"600519": 5},
                "auto_strategy": "dual_low",
                "auto_max_results": 1,
                "auto_cash_per_order": 10000,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "candidates": [
                {"code": "600519", "score": 80, "price": 10.0},
            ],
            "warnings": [],
        }

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ):
            result = self.service.run_auto_trade_once()

        self.assertEqual(result["planned_count"], 1)
        self.assertEqual(result["skipped_count"], 0)
        self.assertEqual(result["orders"][0]["cash_amount"], 5000.0)
        self.assertEqual(result["orders"][0]["quantity"], 500.0)
        sizing = result["orders"][0]["raw"]["target_weight_sizing"]
        self.assertTrue(sizing["adjusted"])
        self.assertEqual(sizing["resolved_base_amount"], 5000.0)
        self.assertEqual(sizing["constraints"][0]["remaining_value"], 5000.0)
        audit = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        self.assertIsNotNone(audit)
        assert audit is not None
        self.assertEqual(audit["decisions"][0]["status"], "planned")
        self.assertEqual(audit["trade_plans"][0]["planned_cash_amount"], 5000.0)

    def test_auto_trade_replenishes_existing_explicit_target_at_max_positions(self) -> None:
        self.service.submit_order(
            symbol="600519",
            side="buy",
            market="cn",
            quantity=100,
            price=10.0,
        )
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_execution_mode": "dry_run",
                "auto_target_position_weights": {"600519": 5},
                "auto_strategy": "dual_low",
                "auto_max_positions": 1,
                "auto_skip_existing_positions": True,
                "auto_max_results": 1,
                "auto_cash_per_order": 10000,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "candidates": [{"code": "600519", "score": 80, "price": 10.0}],
            "warnings": [],
        }

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ):
            result = self.service.run_auto_trade_once()

        self.assertEqual(result["planned_count"], 1)
        self.assertEqual(result["skipped_count"], 0)
        self.assertEqual(result["orders"][0]["cash_amount"], 3000.0)
        self.assertEqual(result["orders"][0]["quantity"], 300.0)
        sizing = result["orders"][0]["raw"]["target_weight_sizing"]
        self.assertEqual(sizing["constraints"][0]["current_value"], 1000.0)
        self.assertEqual(sizing["constraints"][0]["remaining_value"], 3950.0)
        self.assertEqual(sizing["executable_base_amount"], 3000.0)
        self.assertTrue(sizing["lot_adjusted"])

    def test_auto_trade_skips_when_explicit_position_target_is_already_reached(self) -> None:
        self.service.submit_order(
            symbol="600519",
            side="buy",
            market="cn",
            quantity=500,
            price=10.0,
        )
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_execution_mode": "dry_run",
                "auto_target_position_weights": {"600519": 5},
                "auto_strategy": "dual_low",
                "auto_skip_existing_positions": True,
                "auto_max_results": 1,
                "auto_cash_per_order": 10000,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "candidates": [{"code": "600519", "score": 80, "price": 10.0}],
            "warnings": [],
        }

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ):
            result = self.service.run_auto_trade_once()

        self.assertEqual(result["planned_count"], 0)
        self.assertEqual(result["skipped_count"], 1)
        self.assertEqual(result["orders"][0]["reason"], "target_position_weight_reached")

    def test_auto_trade_respects_total_position_value_limit_for_plans(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_execution_mode": "dry_run",
                "auto_max_total_position_value": 1500,
                "auto_strategy": "dual_low",
                "auto_max_results": 2,
                "auto_cash_per_order": 1000,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "candidates": [
                {"code": "600519", "score": 80, "price": 10.0},
                {"code": "000001", "score": 80, "price": 10.0},
            ],
            "warnings": [],
        }

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ):
            result = self.service.run_auto_trade_once()

        self.assertEqual(result["planned_count"], 1)
        self.assertEqual(result["submitted_count"], 0)
        self.assertEqual(result["skipped_count"], 1)
        self.assertEqual(result["orders"][0]["status"], "planned")
        self.assertEqual(result["orders"][1]["reason"], "total_position_value_limit_reached")
        audit = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        self.assertIsNotNone(audit)
        assert audit is not None
        self.assertEqual(audit["decisions"][1]["reason"], "total_position_value_limit_reached")
        self.assertEqual(audit["trade_plans"][1]["skip_reason"], "total_position_value_limit_reached")

    def test_auto_trade_respects_total_position_pct_limit(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_max_total_position_pct": 5,
                "auto_strategy": "dual_low",
                "auto_max_results": 1,
                "auto_cash_per_order": 10000,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "candidates": [
                {"code": "600519", "score": 80, "price": 10.0},
            ],
            "warnings": [],
        }

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ):
            result = self.service.run_auto_trade_once()

        self.assertEqual(result["submitted_count"], 0)
        self.assertEqual(result["orders"][0]["reason"], "total_position_pct_limit_reached")
        audit = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        self.assertIsNotNone(audit)
        assert audit is not None
        self.assertEqual(audit["decisions"][0]["reason"], "total_position_pct_limit_reached")

    def test_auto_trade_respects_industry_position_value_limit(self) -> None:
        self.service.data_fetcher_manager.boards_by_symbol = {
            "600519": [{"name": "白酒", "type": "行业"}],
        }
        self.service.submit_order(
            symbol="600519",
            side="buy",
            market="cn",
            quantity=100,
            price=10.0,
        )
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_max_industry_position_value": 1500,
                "auto_strategy": "dual_low",
                "auto_max_results": 1,
                "auto_cash_per_order": 1000,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "candidates": [
                {"code": "000001", "score": 80, "price": 10.0, "industry": "白酒"},
            ],
            "warnings": [],
        }

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ):
            result = self.service.run_auto_trade_once()

        self.assertEqual(result["submitted_count"], 0)
        self.assertEqual(result["orders"][0]["reason"], "industry_position_value_limit_reached")
        audit = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        self.assertIsNotNone(audit)
        assert audit is not None
        self.assertEqual(audit["decisions"][0]["reason"], "industry_position_value_limit_reached")
        self.assertEqual(audit["trade_plans"][0]["skip_reason"], "industry_position_value_limit_reached")

    def test_auto_trade_respects_industry_position_pct_limit(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_max_industry_position_pct": 5,
                "auto_strategy": "dual_low",
                "auto_max_results": 1,
                "auto_cash_per_order": 10000,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "candidates": [
                {
                    "code": "600519",
                    "score": 80,
                    "price": 10.0,
                    "belong_boards": [{"name": "白酒", "type": "行业"}],
                },
            ],
            "warnings": [],
        }

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ):
            result = self.service.run_auto_trade_once()

        self.assertEqual(result["submitted_count"], 0)
        self.assertEqual(result["orders"][0]["reason"], "industry_position_pct_limit_reached")
        audit = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        self.assertIsNotNone(audit)
        assert audit is not None
        self.assertEqual(audit["decisions"][0]["reason"], "industry_position_pct_limit_reached")

    def test_auto_trade_respects_target_industry_weight_limit_for_plans(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_execution_mode": "dry_run",
                "auto_target_industry_weights": {"liquor": 1.5},
                "auto_strategy": "dual_low",
                "auto_max_results": 2,
                "auto_cash_per_order": 1000,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "candidates": [
                {"code": "600519", "score": 80, "price": 10.0, "industry": "liquor"},
                {"code": "000001", "score": 80, "price": 10.0, "industry": "liquor"},
            ],
            "warnings": [],
        }

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ):
            result = self.service.run_auto_trade_once()

        self.assertEqual(result["planned_count"], 1)
        self.assertEqual(result["submitted_count"], 0)
        self.assertEqual(result["skipped_count"], 1)
        self.assertEqual(result["orders"][0]["status"], "planned")
        self.assertEqual(result["orders"][1]["reason"], "target_weight_below_min_lot")
        sizing = result["orders"][1]["raw"]["target_weight_sizing"]
        self.assertEqual(sizing["resolved_base_amount"], 500.0)
        self.assertEqual(sizing["constraints"][0]["remaining_value"], 500.0)
        audit = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        self.assertIsNotNone(audit)
        assert audit is not None
        self.assertEqual(audit["decisions"][1]["reason"], "target_weight_below_min_lot")
        self.assertEqual(audit["trade_plans"][1]["skip_reason"], "target_weight_below_min_lot")

    def test_auto_trade_respects_daily_order_limit(self) -> None:
        self.service.submit_order(
            symbol="600519",
            side="buy",
            market="cn",
            quantity=100,
            price=10.0,
            source="alphasift_auto",
        )
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_daily_max_orders": 1,
                "auto_skip_existing_positions": False,
                "auto_strategy": "dual_low",
                "auto_max_results": 1,
                "auto_cash_per_order": 1200,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "candidates": [
                {"code": "000001", "score": 80, "price": 10.0},
            ],
            "warnings": [],
        }

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ):
            result = self.service.run_auto_trade_once()

        self.assertEqual(result["submitted_count"], 0)
        self.assertEqual(result["skipped_count"], 1)
        self.assertEqual(result["orders"][0]["reason"], "daily_order_limit_reached")
        audit = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        self.assertIsNotNone(audit)
        assert audit is not None
        self.assertEqual(audit["trade_plans"][0]["skip_reason"], "daily_order_limit_reached")

    def test_auto_trade_respects_daily_budget_limit(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_daily_budget": 1000,
                "auto_strategy": "dual_low",
                "auto_max_results": 1,
                "auto_cash_per_order": 1200,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "candidates": [
                {"code": "600519", "score": 80, "price": 10.0},
            ],
            "warnings": [],
        }

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ):
            result = self.service.run_auto_trade_once()

        self.assertEqual(result["submitted_count"], 0)
        self.assertEqual(result["skipped_count"], 1)
        self.assertEqual(result["orders"][0]["reason"], "daily_budget_exceeded")
        audit = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        self.assertIsNotNone(audit)
        assert audit is not None
        self.assertEqual(audit["decisions"][0]["reason"], "daily_budget_exceeded")
        self.assertEqual(audit["trade_plans"][0]["skip_reason"], "daily_budget_exceeded")

    def test_background_task_builder_reflects_persisted_auto_settings(self) -> None:
        self.service.update_settings(
            {
                "enabled": True,
                "auto_trade_enabled": True,
                "auto_interval_minutes": 5,
            }
        )

        with patch(
            "src.services.vnpy_paper_trading_service.VNPY_PAPER_CONFIG_PATH",
            self.config_path,
        ):
            tasks = build_vnpy_paper_trading_background_tasks()

        self.assertEqual(len(tasks), 2)
        self.assertEqual(tasks[0]["name"], "vnpy_paper_auto_trade")
        self.assertEqual(tasks[0]["interval_seconds"], 5 * 60)
        self.assertEqual(tasks[1]["name"], "vnpy_paper_auto_retry")
        self.assertEqual(tasks[1]["interval_seconds"], 5 * 60)
        self.assertTrue(tasks[1]["run_immediately"])

    def test_background_task_builder_keeps_recovery_when_auto_trade_is_paused(self) -> None:
        self.service.update_settings(
            {
                "enabled": True,
                "auto_trade_enabled": False,
                "auto_interval_minutes": 5,
            }
        )

        with patch(
            "src.services.vnpy_paper_trading_service.VNPY_PAPER_CONFIG_PATH",
            self.config_path,
        ):
            tasks = build_vnpy_paper_trading_background_tasks()

        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["name"], "vnpy_paper_auto_retry")
        self.assertEqual(tasks[0]["interval_seconds"], 5 * 60)
        self.assertTrue(tasks[0]["run_immediately"])

    def test_background_task_builder_injects_runtime_engines_into_service(self) -> None:
        main_engine = object()
        event_engine = object()
        fake_service = MagicMock()
        fake_service.get_settings.return_value = SimpleNamespace(
            enabled=True,
            auto_trade_enabled=False,
            auto_interval_minutes=5,
        )
        fake_service.retry_due_trade_plans.return_value = {
            "accepted": True,
            "skipped": False,
            "attempted_count": 0,
            "submitted_count": 0,
            "skipped_count": 0,
            "failed_count": 0,
        }

        with patch(
            "src.services.vnpy_paper_trading_service.VnpyPaperTradingService",
            return_value=fake_service,
        ) as service_factory:
            tasks = build_vnpy_paper_trading_background_tasks(
                vnpy_main_engine=main_engine,
                vnpy_event_engine=event_engine,
            )

        service_factory.assert_called_once_with(
            vnpy_main_engine=main_engine,
            vnpy_event_engine=event_engine,
        )
        self.assertEqual([task["name"] for task in tasks], ["vnpy_paper_auto_retry"])
        tasks[0]["task"]()
        fake_service.retry_due_trade_plans.assert_called_once_with()

    def test_retry_scan_recovers_orders_without_resubmitting_when_auto_trade_is_paused(self) -> None:
        self.service.update_settings({"enabled": True, "auto_trade_enabled": False})
        recovery = {
            "scanned_count": 1,
            "expired_count": 0,
            "reconciled_count": 1,
            "protected_count": 1,
            "reconciliation_failed_count": 0,
            "failed_count": 0,
            "messages": ["reconciled_vnpy_orders:1"],
        }

        with patch.object(
            self.service,
            "expire_stale_vnpy_trade_plans",
            return_value=recovery,
        ) as recover, patch.object(
            self.service,
            "retry_trade_plan",
            side_effect=AssertionError("paused auto trade must not resubmit plans"),
        ):
            result = self.service.retry_due_trade_plans(max_plans=3, scan_limit=10)

        recover.assert_called_once_with(max_plans=3, scan_limit=10)
        self.assertFalse(result["skipped"])
        self.assertEqual(result["reason"], "auto_trade_disabled")
        self.assertEqual(result["reconciled_count"], 1)
        self.assertEqual(result["attempted_count"], 0)
        self.assertEqual(result["submitted_count"], 0)

    def test_background_task_builder_aligns_first_auto_trade_run_to_next_window(self) -> None:
        self.service.update_settings(
            {
                "enabled": True,
                "auto_trade_enabled": True,
                "auto_interval_minutes": 1440,
                "auto_execution_mode": "paper",
                "auto_trade_time_gate_enabled": True,
            }
        )
        next_open = datetime.now(timezone.utc) + timedelta(seconds=3600)

        with patch(
            "src.services.vnpy_paper_trading_service.VNPY_PAPER_CONFIG_PATH",
            self.config_path,
        ), patch.object(
            VnpyPaperTradingService,
            "_trading_window_diagnostics",
            return_value={
                "is_market_open_now": False,
                "next_open_at": next_open.isoformat(),
                "time_gate_enforced": True,
            },
        ):
            tasks = build_vnpy_paper_trading_background_tasks()

        self.assertEqual(tasks[0]["name"], "vnpy_paper_auto_trade")
        delay = tasks[0]["initial_delay_seconds"]
        self.assertIsInstance(delay, int)
        self.assertGreaterEqual(delay, 3590)
        self.assertLessEqual(delay, 3600)


def _install_fake_vnpy_modules() -> dict[str, object]:
    names = [
        "vnpy",
        "vnpy.event",
        "vnpy.trader",
        "vnpy.trader.event",
        "vnpy.trader.engine",
        "vnpy.trader.object",
        "vnpy.trader.constant",
    ]
    previous = {name: sys.modules.get(name) for name in names}
    vnpy_module = types.ModuleType("vnpy")
    event_module = types.ModuleType("vnpy.event")
    trader_module = types.ModuleType("vnpy.trader")
    trader_event_module = types.ModuleType("vnpy.trader.event")
    engine_module = types.ModuleType("vnpy.trader.engine")
    object_module = types.ModuleType("vnpy.trader.object")
    constant_module = types.ModuleType("vnpy.trader.constant")

    class EventEngine:
        pass

    class MainEngine:
        pass

    class Exchange:
        SSE = "SSE"
        SZSE = "SZSE"
        SEHK = "SEHK"
        SMART = "SMART"

    class Direction:
        LONG = "LONG"
        SHORT = "SHORT"

    class Offset:
        NONE = "NONE"

    class OrderType:
        LIMIT = "LIMIT"
        MARKET = "MARKET"

    class OrderRequest:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class CancelRequest:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    event_module.EventEngine = EventEngine
    engine_module.MainEngine = MainEngine
    trader_event_module.EVENT_ORDER = "eOrder."
    trader_event_module.EVENT_TRADE = "eTrade."
    trader_event_module.EVENT_ACCOUNT = "eAccount."
    trader_event_module.EVENT_POSITION = "ePosition."
    constant_module.Exchange = Exchange
    constant_module.Direction = Direction
    constant_module.Offset = Offset
    constant_module.OrderType = OrderType
    object_module.OrderRequest = OrderRequest
    object_module.CancelRequest = CancelRequest
    vnpy_module.event = event_module
    vnpy_module.trader = trader_module
    trader_module.engine = engine_module
    trader_module.event = trader_event_module
    trader_module.object = object_module
    trader_module.constant = constant_module
    sys.modules["vnpy"] = vnpy_module
    sys.modules["vnpy.event"] = event_module
    sys.modules["vnpy.trader"] = trader_module
    sys.modules["vnpy.trader.event"] = trader_event_module
    sys.modules["vnpy.trader.engine"] = engine_module
    sys.modules["vnpy.trader.object"] = object_module
    sys.modules["vnpy.trader.constant"] = constant_module
    return previous


def _restore_modules(previous: dict[str, object]) -> None:
    for name, module in previous.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


class _FakeMainEngine:
    def __init__(self) -> None:
        self.calls = []
        self.cancel_calls = []
        self.orders = {}
        self.trades = []

    def send_order(self, request, gateway_name):
        self.calls.append((request, gateway_name))
        return f"{gateway_name}.1"

    def cancel_order(self, request, gateway_name):
        self.cancel_calls.append((request, gateway_name))
        return True

    def get_order(self, vt_orderid):
        return self.orders.get(vt_orderid)

    def get_all_trades(self):
        return list(self.trades)


class _FakeEventEngine:
    def __init__(self) -> None:
        self.handlers = {}

    def register(self, event_type, handler):
        self.handlers.setdefault(event_type, []).append(handler)

    def unregister(self, event_type, handler):
        handlers = self.handlers.get(event_type, [])
        self.handlers[event_type] = [item for item in handlers if item is not handler]

    def emit(self, event_type, event):
        for handler in list(self.handlers.get(event_type, [])):
            handler(event)


class _FailingMainEngine(_FakeMainEngine):
    def __init__(self, exc: Exception) -> None:
        super().__init__()
        self.exc = exc

    def send_order(self, request, gateway_name):
        self.calls.append((request, gateway_name))
        raise self.exc


class _FailingQueryMainEngine(_FakeMainEngine):
    def __init__(self, exc: Exception) -> None:
        super().__init__()
        self.exc = exc

    def get_order(self, vt_orderid):
        raise self.exc


if __name__ == "__main__":
    unittest.main()
