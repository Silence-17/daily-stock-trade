# -*- coding: utf-8 -*-
"""Tests for vn.py-style local paper trading service."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import types
import unittest
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
    build_vnpy_paper_trading_background_tasks,
)
from src.storage import DatabaseManager, StockSelectionAgentTradePlan


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

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ), patch("src.analyzer.GeminiAnalyzer", return_value=fake_analyzer):
            result = self.service.run_auto_trade_once(execution_mode_override="dry_run")

        fake_alphasift.screen.assert_called_once_with(strategy="capital_heat", market="cn", max_results=1)
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
        self.assertTrue(plan["gates"]["llm_dynamic_plan_enabled"])
        self.assertTrue(plan["gates"]["llm_dynamic_plan_applied"])
        self.assertEqual(dynamic_plan["status"], "accepted")
        self.assertEqual(dynamic_plan["model"], "openai/test")
        self.assertEqual(dynamic_plan["prompt_version"], LLM_DYNAMIC_AGENT_PLAN_PROMPT_VERSION)
        self.assertEqual(dynamic_plan["evaluator_version"], LLM_DYNAMIC_AGENT_PLAN_EVALUATOR_VERSION)
        self.assertEqual(dynamic_plan["applied_overrides"]["auto_strategy"], "capital_heat")
        self.assertEqual(dynamic_plan["applied_overrides"]["auto_max_results"], 1)
        self.assertEqual(dynamic_plan["applied_overrides"]["auto_cash_per_order"], 1200.0)
        self.assertEqual(dynamic_plan["applied_overrides"]["auto_min_score"], 75.0)
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
            return_value={"region": "cn", "trade_date": "2026-07-02", "status": "yellow", "score": 45},
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
        self.assertIn(result["agent_run_uid"], triggers[0]["diagnostics"])

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
                    "price": 10.0,
                    "data_quality": "partial",
                    "missing_fields": ["amount", "trading_status"],
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
        self.assertEqual(decision_order["risk_review"]["status"], "passed")
        self.assertEqual(decision_order["risk_review"]["reason"], "dry_run")
        self.assertEqual(decision_order["risk_review"]["candidate_data_quality"]["status"], "partial")
        self.assertEqual(decision_order["agent_review"]["status"], "warning")
        self.assertEqual(decision_order["agent_review"]["reason"], "data_quality_missing_fields")
        self.assertEqual(decision_order["agent_review"]["reviewer"], "rule_agent_v1")
        self.assertEqual(
            decision_order["risk_review"]["candidate_data_quality"]["missing_fields"],
            ["amount", "trading_status"],
        )
        self.assertEqual(decision_order["risk_review"]["candidate_data_quality"]["data_sources"], ["em_datacenter"])
        self.assertEqual(plan_order["position_plan"]["symbol"], "600519")
        self.assertEqual(plan_order["risk_review"]["review_source"], "rule_based_auto_trade")
        self.assertEqual(plan_order["risk_review"]["candidate_data_quality"]["status"], "partial")
        self.assertEqual(plan_order["agent_review"]["status"], "warning")
        trades = self.service.portfolio.list_trade_events(account_id=int(self.service.get_settings().account_id), page=1)
        self.assertEqual(trades["items"], [])

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
                status="rejected",
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

    def test_auto_trade_respects_target_position_weight_limit(self) -> None:
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

        self.assertEqual(result["planned_count"], 0)
        self.assertEqual(result["skipped_count"], 1)
        self.assertEqual(result["orders"][0]["reason"], "target_position_weight_limit_reached")
        audit = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        self.assertIsNotNone(audit)
        assert audit is not None
        self.assertEqual(audit["decisions"][0]["reason"], "target_position_weight_limit_reached")
        self.assertEqual(audit["trade_plans"][0]["skip_reason"], "target_position_weight_limit_reached")

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
        self.assertEqual(result["orders"][1]["reason"], "target_industry_weight_limit_reached")
        audit = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        self.assertIsNotNone(audit)
        assert audit is not None
        self.assertEqual(audit["decisions"][1]["reason"], "target_industry_weight_limit_reached")
        self.assertEqual(audit["trade_plans"][1]["skip_reason"], "target_industry_weight_limit_reached")

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

    def send_order(self, request, gateway_name):
        self.calls.append((request, gateway_name))
        return f"{gateway_name}.1"

    def cancel_order(self, request, gateway_name):
        self.cancel_calls.append((request, gateway_name))
        return True


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


if __name__ == "__main__":
    unittest.main()
