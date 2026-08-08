# -*- coding: utf-8 -*-
"""Tests for vn.py-style local paper trading service."""

from __future__ import annotations

import json
import importlib.util
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import text

from src.config import Config
from src.notification import ChannelAttemptResult, NotificationDispatchResult
from src.services.alert_service import AlertService
from src.services.decision_signal_service import DecisionSignalService
from src.services.cross_market_paper_strategy import (
    GLOBAL_MARKET_LINKED_THEMES,
    STRATEGY_ID as CROSS_MARKET_STRATEGY_ID,
    StrategyDecision,
    TradeFeeSchedule,
)
from src.services.cross_market_signal_service import CrossMarketSignalService
from src.services.vnpy_paper_trading_service import (
    AUTO_TRADE_RUN_LOCK_WAIT_SECONDS,
    CALIBRATION_SHADOW_AUTO_TRADE_STAGGER_SECONDS,
    CROSS_MARKET_OBSERVATION_LOCK_WAIT_SECONDS,
    CROSS_MARKET_OBSERVATION_TRIGGER_SOURCE,
    LLM_DYNAMIC_AGENT_PLAN_EVALUATOR_VERSION,
    LLM_DYNAMIC_AGENT_PLAN_PROMPT_VERSION,
    LLM_PRE_TRADE_REVIEW_EVALUATOR_VERSION,
    LLM_PRE_TRADE_REVIEW_PROMPT_VERSION,
    VnpyPaperSettings,
    VnpyPaperTradingService,
    _auto_trade_initial_delay_seconds,
    _cross_market_daily_evidence_status,
    _cross_market_intraday_entry_slot_audited,
    _cross_market_observation_initial_delay_seconds,
    _cross_market_session_order_activity_status,
    _last_auto_trade_ran_in_session,
    _next_daily_auto_trade_target,
    _resolve_auto_alphasift_llm_policy,
    build_calibration_shadow_schedule,
    build_vnpy_paper_trading_background_tasks,
)
from src.storage import (
    DatabaseManager,
    PortfolioDailySnapshot,
    StockDaily,
    StockSelectionAgentTradePlan,
)


class _FakeDataFetcherManager:
    def __init__(self, price: float = 10.0, boards_by_symbol=None) -> None:
        self.price = price
        self.boards_by_symbol = boards_by_symbol or {}

    def get_realtime_quote(self, symbol: str):
        amount = getattr(self, "amount", 200_000_000.0)
        volume = getattr(self, "volume", amount / max(self.price, 0.01))
        return SimpleNamespace(
            price=self.price,
            provider="unit-test",
            provider_timestamp=datetime.now(timezone.utc).isoformat(),
            open_price=getattr(self, "open_price", self.price),
            pre_close=getattr(self, "pre_close", self.price),
            amount=amount,
            volume=volume,
            volume_ratio=1.2,
            high=getattr(self, "high", self.price * 1.02),
            low=getattr(self, "low", self.price * 0.99),
        )

    def get_belong_boards(self, symbol: str):
        return self.boards_by_symbol.get(symbol, [])


class _SequenceDateTime(datetime):
    values = []

    @classmethod
    def now(cls, tz=None):
        return cls.values.pop(0)


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
        self.original_calibration_shadow_enabled = os.environ.get(
            "DSA_AGENT_CALIBRATION_SHADOW_ENABLED"
        )
        os.environ["DSA_AGENT_CALIBRATION_SHADOW_ENABLED"] = "false"
        Config.reset_instance()
        DatabaseManager.reset_instance()
        data_fetcher_manager = _FakeDataFetcherManager(price=10.0)
        cross_market_signal_service = CrossMarketSignalService(
            data_fetcher_manager=data_fetcher_manager,
            state_path=self.data_dir / "cross_market_strategy_state.json",
        )
        board_technical_service = MagicMock()
        board_technical_service.analyze_boards.return_value = {
            "available": True,
            "supportive": True,
            "near_resistance": False,
            "breakout_confirmed": False,
            "support_score": 100.0,
            "reason": "unit_test_board_support",
            "alerts": [{"kind": "near_60d_ma_support"}],
        }
        self.service = VnpyPaperTradingService(
            data_fetcher_manager=data_fetcher_manager,
            config_path=self.config_path,
            cross_market_signal_service=cross_market_signal_service,
            cross_market_board_technical_service=board_technical_service,
        )
        strong_us_signal = {
            "available": True,
            "strong": True,
            "score": 82.0,
            "sector_change_pct": 2.8,
            "advancing_ratio": 0.8,
            "leader_change_pct": 4.0,
            "premarket_reversal_invalidated": False,
        }
        self.service.cross_market_signal_service.get_us_close_theme_signal_for_cn_trade = MagicMock(
            return_value=strong_us_signal,
        )
        self.service.cross_market_signal_service.get_us_premarket_signal_for_cn_trade = MagicMock(
            return_value=strong_us_signal,
        )
        self.service.cross_market_signal_service.get_us_tech_signal_for_cn_trade = MagicMock(
            return_value={"available": True, "buy_allowed": True, "score": 65.0},
        )
        self.service.cross_market_signal_service.get_nasdaq_futures_signal_for_cn_trade = MagicMock(
            return_value={
                "available": True,
                "confirmed": True,
                "buy_allowed": True,
                "sell_fraction": 0.0,
                "reason": "nasdaq_futures_trend_confirmed",
                "sector_score_adjustment": 0.0,
            },
        )
        self.service.cross_market_signal_service.get_cpo_signal_for_cn_trade = MagicMock(
            return_value={"available": True, "supportive": True, "score": 30.0},
        )

        def asia_gate(*, theme, **_kwargs):
            if str(theme).lower() == "cpo":
                return {
                    "available": True,
                    "buy_allowed": True,
                    "reason": "cpo_independent_asia_gate",
                    "bypassed": True,
                    "korea": {
                        "status": "bypassed",
                        "buy_allowed": True,
                        "sell_fraction": 0.0,
                    },
                }
            return {
                "available": True,
                "buy_allowed": True,
                "reason": "unit_test_asia_markets_confirmed",
                "korea": {
                    "status": "hold",
                    "buy_allowed": True,
                    "sell_fraction": 0.0,
                },
                "japan": {"available": True, "buy_allowed": True},
                "supply_chain": {
                    "available": True,
                    "strong": True,
                    "score": 55.0,
                    "sector_change_pct": 0.7,
                    "advancing_ratio": 0.8,
                },
            }

        self.service.cross_market_signal_service.evaluate_asia_market_gate = MagicMock(
            side_effect=asia_gate,
        )
        self.service._cross_market_rotation_cache = (
            time.monotonic(),
            {
                "available": False,
                "tailwind": False,
                "reason": "unit_test_rotation_unavailable",
            },
        )

    def tearDown(self) -> None:
        DatabaseManager.reset_instance()
        Config.reset_instance()
        os.environ.pop("ENV_FILE", None)
        os.environ.pop("DATABASE_PATH", None)
        if self.original_calibration_shadow_enabled is None:
            os.environ.pop("DSA_AGENT_CALIBRATION_SHADOW_ENABLED", None)
        else:
            os.environ["DSA_AGENT_CALIBRATION_SHADOW_ENABLED"] = (
                self.original_calibration_shadow_enabled
            )
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

    def test_cross_market_settings_allow_existing_positions_for_range_adds(self) -> None:
        settings = replace(
            self.service.get_settings(),
            auto_strategy=CROSS_MARKET_STRATEGY_ID,
            initial_cash=100000.0,
            auto_max_results=10,
            auto_max_positions=10,
            auto_skip_existing_positions=True,
            auto_cash_per_order=50000.0,
            auto_max_total_position_pct=90.0,
            auto_max_industry_position_pct=50.0,
            auto_max_single_position_value=50000.0,
            auto_max_drawdown_pct=20.0,
            auto_consecutive_loss_limit=8,
            auto_sell_enabled=False,
            auto_score_weighted_allocation_enabled=True,
            auto_allocation_budget=90000.0,
            auto_cross_run_quality_gate_enabled=True,
            auto_market_light_gate_enabled=True,
            auto_market_breadth_gate_enabled=True,
            auto_hotspot_retreat_gate_enabled=True,
            auto_intraday_market_gate_enabled=True,
            auto_cross_market_gate_enabled=True,
            auto_signal_exit_enabled=True,
            auto_rebalance_enabled=True,
            auto_target_position_weights={"600000": 20.0},
            auto_target_industry_weights={"bank": 20.0},
            auto_llm_plan_enabled=True,
            auto_llm_review_enabled=True,
        )

        applied = self.service._apply_cross_market_strategy_settings(settings)

        self.assertEqual(applied.auto_market, "cn")
        self.assertEqual(applied.auto_max_results, 2)
        self.assertEqual(applied.auto_max_positions, 2)
        self.assertFalse(applied.auto_skip_existing_positions)
        self.assertEqual(applied.auto_cash_per_order, 50000.0)
        self.assertEqual(applied.auto_max_total_position_pct, 100.0)
        self.assertIsNone(applied.auto_max_total_position_value)
        self.assertEqual(applied.auto_max_industry_position_pct, 100.0)
        self.assertIsNone(applied.auto_max_industry_position_value)
        self.assertIsNone(applied.auto_max_single_position_value)
        self.assertEqual(applied.auto_daily_max_orders, 4)
        self.assertIsNone(applied.auto_daily_budget)
        self.assertEqual(applied.auto_min_cash_balance, 0.0)
        self.assertEqual(applied.auto_max_drawdown_pct, 8.0)
        self.assertEqual(applied.auto_consecutive_loss_limit, 3)
        self.assertTrue(applied.auto_sell_enabled)
        self.assertEqual(applied.auto_stop_loss_pct, 5.0)
        self.assertEqual(applied.auto_take_profit_pct, 8.0)
        self.assertEqual(applied.auto_trailing_stop_pct, 4.0)
        self.assertIsNone(applied.auto_max_holding_days)
        self.assertEqual(applied.auto_sell_position_pct, 100.0)
        self.assertIsNone(applied.auto_no_progress_days)
        self.assertIsNone(applied.auto_no_progress_min_return_pct)
        self.assertFalse(applied.auto_score_weighted_allocation_enabled)
        self.assertIsNone(applied.auto_allocation_budget)
        self.assertFalse(applied.auto_cross_run_quality_gate_enabled)
        self.assertFalse(applied.auto_market_light_gate_enabled)
        self.assertFalse(applied.auto_market_breadth_gate_enabled)
        self.assertFalse(applied.auto_hotspot_retreat_gate_enabled)
        self.assertFalse(applied.auto_intraday_market_gate_enabled)
        self.assertFalse(applied.auto_cross_market_gate_enabled)
        self.assertFalse(applied.auto_signal_exit_enabled)
        self.assertFalse(applied.auto_rebalance_enabled)
        self.assertEqual(applied.auto_target_position_weights, {})
        self.assertEqual(applied.auto_target_industry_weights, {})
        self.assertFalse(applied.auto_llm_plan_enabled)
        self.assertFalse(applied.auto_llm_review_enabled)

    def test_cross_market_signal_cash_allocation_compounds_current_equity(self) -> None:
        settings = self.service._apply_cross_market_strategy_settings(
            replace(
                self.service.get_settings(),
                auto_strategy=CROSS_MARKET_STRATEGY_ID,
                initial_cash=100000.0,
            )
        )

        amount, diagnostics = self.service._cross_market_signal_cash_allocation_cap(
            settings=settings,
            exposure_state={"total_equity": 120000.0},
            candidate={"_cross_market_target_position_pct": 50.0},
        )

        self.assertEqual(amount, 60000.0)
        self.assertEqual(diagnostics["allocation_basis"], "current_total_equity_pct")
        self.assertEqual(diagnostics["target_position_pct"], 50.0)
        self.assertEqual(diagnostics["total_equity_reference"], 120000.0)

    def test_cross_market_entry_cost_uses_current_equity_target_notional(self) -> None:
        self.service.data_fetcher_manager.boards_by_symbol["002281"] = [
            {"name": "CPO", "type": "concept", "change_pct": 1.2}
        ]
        settings = self.service._apply_cross_market_strategy_settings(
            replace(
                self.service.get_settings(),
                auto_strategy=CROSS_MARKET_STRATEGY_ID,
                initial_cash=100000.0,
            )
        )
        exposure = {
            "available": True,
            "held_symbols": set(),
            "position_values": {},
            "industry_values": {},
            "industry_available": True,
            "total_market_value": 0.0,
            "total_equity": 120000.0,
            "total_cash": 120000.0,
        }
        candidate = {
            "code": "002281",
            "score": 90.0,
            "expected_return_pct": 4.0,
            "is_core_stock": True,
            "source": "dsa_eastmoney_board_change_leader",
            "_cross_market_source_theme": "cpo",
            "_cross_market_entry_phase": "opening",
        }

        with patch.object(
            self.service.cross_market_signal_service,
            "get_cn_open_signal",
            return_value={
                "available": True,
                "regime": "low_open",
                "gap_pct": -0.6,
                "buy_allowed": True,
                "force_sell": False,
            },
        ), patch.object(
            self.service,
            "_cross_market_range_signal",
            return_value={},
        ), patch.object(
            self.service,
            "_cross_market_daily_pnl_pct",
            return_value=0.0,
        ):
            decision, evidence = self.service._cross_market_candidate_decision(
                candidate=candidate,
                symbol="002281",
                settings=settings,
                exposure_state=exposure,
            )

        expected_cost = TradeFeeSchedule().estimate_round_trip_cost_pct(
            notional=60000.0,
            instrument_type="stock",
            buy_slippage_bps=10.0,
            sell_slippage_bps=10.0,
        )
        self.assertEqual(decision.action, "buy")
        self.assertEqual(decision.target_position_pct, 50.0)
        self.assertEqual(evidence["estimated_order_notional"], 60000.0)
        self.assertEqual(evidence["estimated_round_trip_cost_pct"], expected_cost)

    def test_flat_open_staged_add_uses_current_equity_cost_basis(self) -> None:
        self.service.data_fetcher_manager.boards_by_symbol["002281"] = [
            {"name": "CPO", "type": "concept", "change_pct": 3.0}
        ]
        settings = self.service._apply_cross_market_strategy_settings(
            replace(
                self.service.get_settings(),
                auto_strategy=CROSS_MARKET_STRATEGY_ID,
                initial_cash=100000.0,
            )
        )
        strategy_position = {
            "status": "available",
            "quantity": 3000.0,
            "sellable_quantity": 3000.0,
            "gross_cost_basis": 30000.0,
            "entry_reasons": ["cpo_flat_open_staged_entry_confirmed"],
            "entry_theme": "cpo",
            "entry_themes": ["cpo"],
            "open_entry_order_count": 1,
            "flat_open_staged_entry": True,
        }
        exposure = {
            "available": True,
            "held_symbols": {"002281"},
            "position_values": {"002281": 30000.0},
            "industry_values": {},
            "industry_available": True,
            "total_market_value": 30000.0,
            "total_equity": 120000.0,
            "total_cash": 90000.0,
        }
        candidate = {
            "code": "002281",
            "score": 90.0,
            "expected_return_pct": 4.0,
            "is_core_stock": True,
            "source": "dsa_eastmoney_board_change_leader",
            "_cross_market_source_theme": "cpo",
            "_cross_market_entry_phase": "intraday_dip",
        }

        with patch.object(
            self.service.cross_market_signal_service,
            "get_cn_open_signal",
            return_value={
                "available": True,
                "regime": "flat_open",
                "gap_pct": 0.0,
                "buy_allowed": False,
                "force_sell": False,
            },
        ), patch.object(
            self.service,
            "_cross_market_strategy_position",
            return_value=strategy_position,
        ), patch.object(
            self.service,
            "_cross_market_range_signal",
            return_value={},
        ), patch.object(
            self.service,
            "_cross_market_daily_pnl_pct",
            return_value=0.0,
        ):
            decision, evidence = self.service._cross_market_candidate_decision(
                candidate=candidate,
                symbol="002281",
                settings=settings,
                exposure_state=exposure,
            )

        self.assertEqual(decision.action, "buy")
        self.assertEqual(
            decision.reason,
            "cpo_flat_open_staged_add_confirmed",
        )
        self.assertEqual(decision.target_position_pct, 25.0)
        self.assertEqual(evidence["strategy_cost_basis_pct"], 25.0)
        self.assertEqual(
            evidence["strategy_cost_basis_equity_reference"],
            120000.0,
        )
        self.assertEqual(evidence["estimated_order_notional"], 30000.0)

    def test_active_theme_scores_require_aggregate_us_tech_for_kr_themes(self) -> None:
        now = datetime(2026, 8, 4, 1, 35, tzinfo=timezone.utc)

        ready_scores, ready_evidence = self.service._cross_market_active_theme_scores(
            entry_phase="opening",
            now=now,
        )
        self.assertIn("memory", ready_scores)
        self.assertTrue(
            ready_evidence["memory"]["regular_us_technology_ready"]
        )

        self.service.cross_market_signal_service.get_us_tech_signal_for_cn_trade.return_value = {
            "available": True,
            "buy_allowed": False,
            "score": 39.9,
        }
        weak_scores, weak_evidence = self.service._cross_market_active_theme_scores(
            entry_phase="opening",
            now=now,
        )

        for theme in ("semiconductor", "memory", "equipment", "materials"):
            self.assertNotIn(theme, weak_scores)
            self.assertFalse(
                weak_evidence[theme]["regular_us_technology_ready"]
            )
        self.assertIn("artificial_intelligence", weak_scores)

    def test_active_theme_scores_recheck_thresholds_and_do_not_force_cpo(self) -> None:
        now = datetime(2026, 8, 4, 1, 35, tzinfo=timezone.utc)
        mislabeled_signal = {
            "available": True,
            "strong": True,
            "score": 82.0,
            "sector_change_pct": 0.1,
            "advancing_ratio": 0.8,
            "leader_change_pct": 4.0,
            "premarket_reversal_invalidated": False,
        }
        self.service.cross_market_signal_service.get_us_close_theme_signal_for_cn_trade.return_value = (
            mislabeled_signal
        )

        opening_scores, opening_evidence = (
            self.service._cross_market_active_theme_scores(
                entry_phase="opening",
                now=now,
            )
        )

        self.assertEqual(opening_scores, {})
        self.assertFalse(opening_evidence["cpo"]["close_theme_ready"])
        self.assertNotIn("cpo", opening_scores)

        valid_close = {
            **mislabeled_signal,
            "sector_change_pct": 2.8,
        }
        self.service.cross_market_signal_service.get_us_close_theme_signal_for_cn_trade.return_value = (
            valid_close
        )
        self.service.cross_market_signal_service.get_us_premarket_signal_for_cn_trade.return_value = (
            mislabeled_signal
        )

        intraday_scores, intraday_evidence = (
            self.service._cross_market_active_theme_scores(
                entry_phase="intraday_dip",
                now=now,
            )
        )

        self.assertEqual(intraday_scores, {})
        self.assertFalse(intraday_evidence["cpo"]["premarket_theme_ready"])
        self.assertNotIn("cpo", intraday_scores)

    def test_active_theme_scores_use_only_verified_asia_otc_supplement(self) -> None:
        now = datetime(2026, 8, 4, 1, 35, tzinfo=timezone.utc)
        asia_signal = {
            "available": True,
            "strong": True,
            "score": 55.0,
            "sector_change_pct": 0.7,
            "advancing_ratio": 0.8,
        }

        def coverage_shortfall(stage):
            reason = (
                "premarket_theme_coverage_insufficient"
                if stage == "premarket"
                else "us_close_theme_coverage_insufficient"
            )
            observed_at = "2026-08-03T20:00:00+00:00"
            stored_signal = {
                "available": False,
                "strong": False,
                "reason": reason,
            }
            return {
                **stored_signal,
                "theme": "mlcc",
                "signal_theme": "mlcc",
                "session_date": "2026-08-03",
                "observed_at": observed_at,
                "asia_supplement_eligible": True,
                "premarket_reversal_invalidated": False,
                "snapshot": {
                    "session_date": "2026-08-03",
                    "session_stage": stage,
                    "observed_at": observed_at,
                    "theme_signals": {"mlcc": stored_signal},
                },
            }

        default_signal = (
            self.service.cross_market_signal_service
            .get_us_close_theme_signal_for_cn_trade.return_value
        )

        def close_signal(*, theme, now):
            return coverage_shortfall("close") if theme == "mlcc" else default_signal

        def premarket_signal(*, theme, now):
            return (
                coverage_shortfall("premarket")
                if theme == "mlcc"
                else default_signal
            )

        with patch.object(
            self.service.cross_market_signal_service,
            "get_us_close_theme_signal_for_cn_trade",
            side_effect=close_signal,
        ), patch.object(
            self.service.cross_market_signal_service,
            "get_us_premarket_signal_for_cn_trade",
            side_effect=premarket_signal,
        ), patch.object(
            self.service.cross_market_signal_service,
            "get_asia_theme_signal_for_cn_trade",
            return_value=asia_signal,
        ):
            opening_scores, opening_evidence = (
                self.service._cross_market_active_theme_scores(
                    entry_phase="opening",
                    now=now,
                )
            )
            intraday_scores, intraday_evidence = (
                self.service._cross_market_active_theme_scores(
                    entry_phase="intraday_dip",
                    now=now,
                )
            )

        self.assertEqual(opening_scores["mlcc"], 55.0)
        self.assertTrue(opening_evidence["mlcc"]["asia_close_supplement_ready"])
        self.assertEqual(
            opening_evidence["mlcc"]["active_score_source"],
            "asia_supply_chain",
        )
        self.assertEqual(intraday_scores["mlcc"], 55.0)
        self.assertTrue(
            intraday_evidence["mlcc"]["asia_premarket_supplement_ready"]
        )

    def test_legacy_cross_market_campaign_fails_closed_until_explicit_migration(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_strategy": "cross_market_semiconductor_gold_v1.1",
                "auto_execution_mode": "vnpy_paper",
            },
            include_snapshot=False,
            include_recent_trades=False,
        )

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService"
        ) as alphasift:
            result = self.service.run_auto_trade_once()

        self.assertFalse(result["accepted"])
        self.assertTrue(result["skipped"])
        self.assertEqual(
            result["reason"],
            "legacy_cross_market_strategy_requires_explicit_migration",
        )
        self.assertEqual(result["strategy"], "cross_market_semiconductor_gold_v1.1")
        self.assertEqual(result["orders"], [])
        alphasift.assert_not_called()

    def test_legacy_cross_market_campaign_migrates_to_clean_owned_account(self) -> None:
        legacy_account = self.service.ensure_account()
        legacy_account_id = int(legacy_account["id"])
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_strategy": "cross_market_semiconductor_gold_v1.1",
                "auto_execution_mode": "vnpy_paper",
                "account_id": legacy_account_id,
            },
            include_snapshot=False,
            include_recent_trades=False,
        )

        result = self.service.migrate_cross_market_strategy(
            include_snapshot=False,
            include_recent_trades=False,
        )

        migrated = self.service.get_settings()
        self.assertEqual(migrated.auto_strategy, CROSS_MARKET_STRATEGY_ID)
        self.assertEqual(migrated.auto_execution_mode, "vnpy_paper")
        self.assertTrue(migrated.auto_trade_enabled)
        self.assertNotEqual(migrated.account_id, legacy_account_id)
        new_account = self.service._find_account(migrated.account_id)
        self.assertIsNotNone(new_account)
        assert new_account is not None
        self.assertEqual(new_account["owner_id"], CROSS_MARKET_STRATEGY_ID)
        self.assertEqual(
            result["diagnostics"]["cross_market_migration"]["previous_account_id"],
            legacy_account_id,
        )
        self.assertFalse(self.service._paper_account_has_positions(int(migrated.account_id)))

    def test_legacy_cross_market_campaign_reuses_clean_archived_target(self) -> None:
        legacy_account = self.service.ensure_account()
        legacy_account_id = int(legacy_account["id"])
        target = self.service.portfolio.create_account(
            name="V1.3 migration target",
            broker="vnpy_paper",
            market="cn",
            base_currency="CNY",
            owner_id=CROSS_MARKET_STRATEGY_ID,
        )
        target_account_id = int(target["id"])
        self.service.portfolio.record_cash_ledger(
            account_id=target_account_id,
            event_date=date.today(),
            direction="in",
            amount=100_000,
            currency="CNY",
            note="clean migration target initial cash",
        )
        self.service.portfolio.deactivate_account(target_account_id)
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_strategy": "cross_market_semiconductor_gold_v1.1",
                "auto_execution_mode": "vnpy_paper",
                "account_id": legacy_account_id,
            },
            include_snapshot=False,
            include_recent_trades=False,
        )

        result = self.service.migrate_cross_market_strategy(
            target_account_id=target_account_id,
            include_snapshot=False,
            include_recent_trades=False,
        )

        migrated = self.service.get_settings()
        self.assertEqual(migrated.account_id, target_account_id)
        self.assertEqual(migrated.auto_strategy, CROSS_MARKET_STRATEGY_ID)
        self.assertTrue(self.service._find_account(target_account_id)["is_active"])
        self.assertEqual(
            result["diagnostics"]["cross_market_migration"]["previous_account_id"],
            legacy_account_id,
        )

    def test_cross_market_candidate_quote_prefers_strict_timestamp_route(self) -> None:
        manager = MagicMock()
        strict_quote = SimpleNamespace(
            price=10.0,
            provider_timestamp=datetime.now(timezone.utc).isoformat(),
            source="tencent",
        )
        manager.get_realtime_quote_with_provider_timestamp.return_value = strict_quote
        manager.get_realtime_quote.return_value = SimpleNamespace(
            price=10.0,
            provider_timestamp=None,
            source="tushare",
        )
        self.service.data_fetcher_manager = manager

        quote = self.service._get_cross_market_realtime_quote("603398")

        self.assertIs(quote, strict_quote)
        manager.get_realtime_quote_with_provider_timestamp.assert_called_once_with("603398")
        manager.get_realtime_quote.assert_not_called()

    def test_cross_market_candidate_quote_falls_back_for_legacy_manager(self) -> None:
        quote = SimpleNamespace(
            price=10.0,
            provider_timestamp=datetime.now(timezone.utc).isoformat(),
        )
        manager = _FakeDataFetcherManager(price=10.0)
        manager.get_realtime_quote = MagicMock(return_value=quote)
        self.service.data_fetcher_manager = manager

        result = self.service._get_cross_market_realtime_quote("603398")

        self.assertIs(result, quote)
        manager.get_realtime_quote.assert_called_once_with("603398")

    def test_cross_market_candidate_refreshes_asia_before_final_cn_quote(self) -> None:
        manager = self.service.data_fetcher_manager
        manager.boards_by_symbol["000815"] = [
            {
                "name": "cloud computing",
                "type": "concept",
                "change_pct": 3.0,
            }
        ]
        settings = self.service._apply_cross_market_strategy_settings(
            replace(
                self.service.get_settings(),
                auto_strategy=CROSS_MARKET_STRATEGY_ID,
                initial_cash=100000.0,
            )
        )
        exposure = {
            "available": True,
            "held_symbols": set(),
            "position_values": {},
            "industry_values": {},
            "industry_available": True,
            "total_market_value": 0.0,
            "total_equity": 100000.0,
            "total_cash": 100000.0,
        }
        candidate = {
            "code": "000815",
            "score": 90.0,
            "expected_return_pct": 4.0,
            "source": "dsa_eastmoney_board_change_leader",
            "_cross_market_source_theme": "compute_services",
            "_cross_market_entry_phase": "opening",
        }
        call_order = []

        def asia_gate(*, theme, **kwargs):
            call_order.append(("asia", dict(kwargs)))
            return {
                "available": True,
                "buy_allowed": True,
                "reason": "unit_test_asia_markets_confirmed",
                "korea": {
                    "status": "hold",
                    "buy_allowed": True,
                    "sell_fraction": 0.0,
                },
                "japan": {"available": True, "buy_allowed": True},
                "supply_chain": {
                    "available": True,
                    "strong": True,
                    "reason": "not_required",
                },
            }

        def quote_after_asia(symbol):
            call_order.append(("quote", {"symbol": symbol}))
            return manager.get_realtime_quote(symbol)

        self.service.cross_market_signal_service.evaluate_asia_market_gate.reset_mock()
        self.service.cross_market_signal_service.evaluate_asia_market_gate.side_effect = (
            asia_gate
        )
        with patch.object(
            self.service.cross_market_signal_service,
            "get_cn_open_signal",
            return_value={
                "available": True,
                "regime": "low_open",
                "gap_pct": -0.6,
                "buy_allowed": True,
                "force_sell": False,
            },
        ), patch.object(
            self.service,
            "_get_cross_market_realtime_quote",
            side_effect=quote_after_asia,
        ), patch.object(
            self.service,
            "_cross_market_range_signal",
            return_value={},
        ), patch.object(
            self.service,
            "_cross_market_daily_pnl_pct",
            return_value=0.0,
        ):
            decision, _evidence = self.service._cross_market_candidate_decision(
                candidate=candidate,
                symbol="000815",
                settings=settings,
                exposure_state=exposure,
            )

        self.assertEqual(decision.action, "buy")
        self.assertEqual([item[0] for item in call_order[:2]], ["asia", "quote"])
        self.service.cross_market_signal_service.evaluate_asia_market_gate.assert_called_once_with(
            theme="compute_services",
            refresh=True,
            reuse_fresh_supply_chain=True,
        )
        self.assertNotIn("now", call_order[0][1])

    def test_cross_market_candidate_quote_uses_request_completion_time(self) -> None:
        started_at = datetime(2026, 7, 27, 6, 27, 30, tzinfo=timezone.utc)
        quote = SimpleNamespace(
            price=None,
            provider_timestamp=(started_at + timedelta(seconds=2)).isoformat(),
        )
        self.service.data_fetcher_manager.boards_by_symbol["002281"] = [
            {"name": "CPO 光模块", "type": "concept", "change_pct": 1.2}
        ]
        settings = replace(
            self.service.get_settings(),
            auto_strategy=CROSS_MARKET_STRATEGY_ID,
        )

        _SequenceDateTime.values = [
            started_at,
            started_at + timedelta(seconds=3),
        ]
        with patch(
            "src.services.vnpy_paper_trading_service.datetime",
            new=_SequenceDateTime,
        ), patch.object(
            self.service,
            "_get_cross_market_realtime_quote",
            return_value=quote,
        ), patch.object(
            self.service.cross_market_signal_service,
            "get_cn_open_signal",
            return_value={
                "available": True,
                "regime": "flat_open",
                "gap_pct": 0.0,
                "buy_allowed": False,
                "force_sell": False,
            },
        ):
            decision, evidence = self.service._cross_market_candidate_decision(
                candidate={"code": "002281", "concepts": ["CPO"]},
                symbol="002281",
                settings=settings,
                exposure_state={},
            )

        self.assertEqual(decision.reason, "candidate_realtime_price_unavailable")
        self.assertNotEqual(decision.reason, "candidate_realtime_quote_stale")
        self.assertEqual(evidence["theme"], "cpo")

    def test_cross_market_candidate_still_rejects_quote_after_completion(self) -> None:
        started_at = datetime(2026, 7, 27, 6, 27, 30, tzinfo=timezone.utc)
        quote = SimpleNamespace(
            price=10.0,
            provider_timestamp=(started_at + timedelta(seconds=5)).isoformat(),
        )
        self.service.data_fetcher_manager.boards_by_symbol["002281"] = [
            {"name": "CPO 光模块", "type": "concept", "change_pct": 1.2}
        ]
        settings = replace(
            self.service.get_settings(),
            auto_strategy=CROSS_MARKET_STRATEGY_ID,
        )

        _SequenceDateTime.values = [
            started_at,
            started_at + timedelta(seconds=3),
        ]
        with patch(
            "src.services.vnpy_paper_trading_service.datetime",
            new=_SequenceDateTime,
        ), patch.object(
            self.service,
            "_get_cross_market_realtime_quote",
            return_value=quote,
        ), patch.object(
            self.service.cross_market_signal_service,
            "get_cn_open_signal",
            return_value={"available": True, "gap_pct": 0.0},
        ):
            decision, evidence = self.service._cross_market_candidate_decision(
                candidate={"code": "002281", "concepts": ["CPO"]},
                symbol="002281",
                settings=settings,
                exposure_state={},
            )

        self.assertEqual(decision.reason, "candidate_realtime_quote_stale")
        self.assertEqual(evidence["quote_age_seconds"], -2.0)
        self.assertEqual(
            evidence["quote_validated_at"],
            (started_at + timedelta(seconds=3)).isoformat(),
        )

    def test_cross_market_range_quote_prefers_strict_timestamp_route(self) -> None:
        observed_at = datetime.now(timezone.utc)
        strict_quote = SimpleNamespace(
            price=10.0,
            provider_timestamp=observed_at.isoformat(),
            amount=12000.0,
            volume=1200.0,
        )
        manager = MagicMock()
        manager.get_realtime_quote_with_provider_timestamp.return_value = strict_quote
        manager.get_realtime_quote.side_effect = AssertionError(
            "generic quote route must not be used for cross-market range evidence"
        )
        self.service.data_fetcher_manager = manager

        with patch.object(
            self.service,
            "_cross_market_range_signal",
            return_value={"action": "sell", "reason": "range_upper_band"},
        ):
            result = self.service._cross_market_position_range_signal(
                symbol="603398",
                price=10.0,
                now=observed_at,
            )

        self.assertEqual(result["action"], "sell")
        manager.get_realtime_quote_with_provider_timestamp.assert_called_once_with("603398")
        manager.get_realtime_quote.assert_not_called()

    def test_cross_market_range_appends_live_session_without_rewriting_yesterday(self) -> None:
        today_cn = datetime.now(ZoneInfo("Asia/Shanghai")).date()
        completed_dates = pd.date_range(
            end=pd.Timestamp(today_cn) - pd.Timedelta(days=1),
            periods=28,
            freq="D",
        )
        frame = pd.DataFrame(
            {
                "high": [float(value) + 1.0 for value in range(28)],
                "low": [float(value) for value in range(28)],
                "close": [float(value) + 0.5 for value in range(28)],
            },
            index=completed_dates,
        )
        frame.loc[pd.Timestamp(today_cn)] = {
            "high": 999.0,
            "low": 0.01,
            "close": 888.0,
        }
        manager = MagicMock()
        manager.get_daily_data.return_value = (frame, "unit-test")
        self.service.data_fetcher_manager = manager

        with patch.object(
            self.service.cross_market_signal_engine,
            "calculate_range_indicators",
            return_value={"regime": "range", "action": "hold"},
        ) as calculate:
            result = self.service._cross_market_range_signal(
                symbol="603398",
                price=31.0,
                intraday_vwap=30.5,
                intraday_high=32.0,
                intraday_low=29.0,
            )

        self.assertEqual(result["regime"], "range")
        values = calculate.call_args.kwargs
        self.assertEqual(len(values["closes"]), 29)
        self.assertEqual(values["closes"][-2], 27.5)
        self.assertEqual(values["closes"][-1], 31.0)
        self.assertEqual(values["highs"][-1], 32.0)
        self.assertEqual(values["lows"][-1], 29.0)
        self.assertNotIn(888.0, values["closes"])
        self.assertEqual(values["intraday_vwap"], 30.5)

    def test_switching_vnpy_gateway_clears_previous_gateway_sync_state(self) -> None:
        self.service.update_settings(
            {"vnpy_gateway_name": "XTP"},
            include_snapshot=False,
            include_recent_trades=False,
        )
        self.service._record_vnpy_account_state(
            {"balance": 100000.0, "raw": {"gateway_name": "XTP"}}
        )

        self.service.update_settings(
            {"vnpy_gateway_name": "DSA_SIM"},
            include_snapshot=False,
            include_recent_trades=False,
        )

        self.assertEqual(self.service._read_vnpy_sync_state(), {})

    def test_vnpy_sync_state_records_current_gateway(self) -> None:
        self.service.update_settings(
            {"vnpy_gateway_name": "DSA_SIM"},
            include_snapshot=False,
            include_recent_trades=False,
        )

        self.service._record_vnpy_positions_state(
            {"positions": [], "raw": {}, "updated_at": "2026-07-24T00:00:00Z"}
        )

        self.assertEqual(
            self.service._read_vnpy_sync_state()["gateway_name"],
            "DSA_SIM",
        )

    def test_cross_market_observation_snapshot_requires_audited_stage_evidence(self) -> None:
        required_themes = sorted(GLOBAL_MARKET_LINKED_THEMES)
        session_date = "2026-07-28"
        full_coverage = {
            "available": True,
            "full_strategy_coverage": True,
            "required_themes": required_themes,
            "qualified_themes": required_themes,
            "missing_themes": [],
            "themes": {
                theme: {"available": True}
                for theme in required_themes
            },
        }
        premarket_evidence = {
            **full_coverage,
            "collection": {
                "latest_session_date": session_date,
                "required_stages_complete": True,
            },
            "latest_capture": {
                "available": True,
                "session_date": session_date,
                "session_stage": "premarket",
                "universe_size": 91,
                "collected_component_count": 49,
                "required_themes": required_themes,
            },
        }
        close_evidence = {
            **full_coverage,
            "collection": {
                "latest_session_date": session_date,
                "required_stages_complete": True,
            },
        }
        runtime = {
            "strategy_id": CROSS_MARKET_STRATEGY_ID,
            "cn_open": {"available": True, "gap_pct": -0.6},
            "us_tech": {"available": True, "score": 65.0},
            "nasdaq_futures": {
                "available": True,
                "confirmed": True,
                "buy_allowed": True,
            },
            "us_premarket": premarket_evidence,
            "us_close_themes": close_evidence,
            "asia_supply_chain": {
                "mlcc": {"available": False},
                "ccl": {"available": False},
            },
            "japan": {"available": True, "buy_allowed": True},
            "korea": {
                "fresh": True,
                "linked_technology_gate": {
                    "status": "neutral",
                    "reason": "korea_direction_unconfirmed",
                    "confirmation_span_seconds": 300.0,
                    "confirmation_duration_seconds": 300,
                },
            },
            "gold": {"available": True, "signal": {"score": 70.0}},
            "cpo": {"available": False, "reason": "optional"},
        }
        with patch.object(
            self.service.cross_market_signal_service,
            "get_runtime_status",
            return_value=runtime,
        ):
            ready = self.service._cross_market_observation_snapshot()
        self.assertEqual(ready["schema_version"], 6)
        self.assertEqual(ready["status"], "ready")
        self.assertEqual(ready["missing_requirements"], [])

        runtime["nasdaq_futures"] = {"available": True, "confirmed": False}
        with patch.object(
            self.service.cross_market_signal_service,
            "get_runtime_status",
            return_value=runtime,
        ):
            missing_nasdaq_futures = self.service._cross_market_observation_snapshot()
        self.assertEqual(
            missing_nasdaq_futures["missing_requirements"],
            ["nasdaq_futures_continuous_trend_available"],
        )
        runtime["nasdaq_futures"] = {
            "available": True,
            "confirmed": True,
            "buy_allowed": True,
        }

        runtime["us_premarket"] = {
            **premarket_evidence,
            "full_strategy_coverage": False,
            "qualified_themes": required_themes[:-1],
            "missing_themes": [required_themes[-1]],
        }
        with patch.object(
            self.service.cross_market_signal_service,
            "get_runtime_status",
            return_value=runtime,
        ):
            partial_premarket = self.service._cross_market_observation_snapshot()
        self.assertEqual(partial_premarket["status"], "ready")
        self.assertFalse(
            partial_premarket["evidence"]["us_theme_collection_audit"][
                "premarket_full_strategy_coverage"
            ]
        )

        runtime["us_premarket"]["latest_capture"] = {
            **premarket_evidence["latest_capture"],
            "collected_component_count": 0,
        }
        with patch.object(
            self.service.cross_market_signal_service,
            "get_runtime_status",
            return_value=runtime,
        ):
            missing_premarket_audit = (
                self.service._cross_market_observation_snapshot()
            )
        self.assertEqual(
            missing_premarket_audit["missing_requirements"],
            [
                "us_premarket_collection_audited",
                "us_theme_session_pair_available",
            ],
        )
        runtime["us_premarket"] = premarket_evidence

        runtime["korea"]["linked_technology_gate"][
            "confirmation_span_seconds"
        ] = 240.0
        with patch.object(
            self.service.cross_market_signal_service,
            "get_runtime_status",
            return_value=runtime,
        ):
            short_korea_window = self.service._cross_market_observation_snapshot()
        self.assertEqual(short_korea_window["status"], "unavailable")
        self.assertEqual(
            short_korea_window["missing_requirements"],
            ["korea_continuous_gate_available"],
        )

        runtime["korea"]["linked_technology_gate"][
            "confirmation_span_seconds"
        ] = 300.0
        runtime["gold"] = {"available": False}
        with patch.object(
            self.service.cross_market_signal_service,
            "get_runtime_status",
            return_value=runtime,
        ):
            unavailable = self.service._cross_market_observation_snapshot()
        self.assertEqual(unavailable["status"], "unavailable")
        self.assertEqual(unavailable["missing_requirements"], ["gold_signal_available"])

    def test_cross_market_run_persists_observation_snapshot_without_candidates(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_execution_mode": "dry_run",
                "auto_strategy": CROSS_MARKET_STRATEGY_ID,
            }
        )
        observation = {
            "schema_version": 1,
            "strategy_id": CROSS_MARKET_STRATEGY_ID,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "status": "ready",
            "required_checks": {
                "cn_open_available": True,
                "us_first_hour_and_close_available": True,
                "nasdaq_futures_continuous_trend_available": True,
                "us_premarket_themes_available": True,
                "korea_continuous_gate_available": True,
                "gold_signal_available": True,
            },
            "missing_requirements": [],
        }
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {"candidates": [], "warnings": []}

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ), patch.object(
            self.service,
            "_cross_market_observation_snapshot",
            return_value=observation,
        ):
            result = self.service.run_auto_trade_once()

        detail = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        self.assertEqual(
            detail["diagnostics"]["cross_market_observation"],
            observation,
        )

    def test_cross_market_global_entry_gate_blocks_only_hard_open_regimes(self) -> None:
        cases = (
            (0.19, "high_open", True, "cn_high_open_buy_blocked"),
            (0.189999, "flat_open", False, "candidate_screen_required"),
            (-1.6, "extreme_low_open", True, "cn_extreme_low_open_buy_blocked"),
            (-0.6, "low_open", False, "candidate_screen_required"),
            (0.0, "flat_open", False, "candidate_screen_required"),
        )

        for gap_pct, regime, blocked, reason in cases:
            with self.subTest(regime=regime):
                gate = self.service._cross_market_global_entry_gate({
                    "evidence": {
                        "cn_open": {
                            "available": True,
                            "regime": regime,
                            "gap_pct": gap_pct,
                        }
                    }
                })

                self.assertEqual(gate["blocked"], blocked)
                self.assertEqual(gate["reason"], reason)
                self.assertEqual(gate["classification_source"], "gap_pct")

    def test_cross_market_global_open_gate_runs_sells_then_skips_alphasift(
        self,
    ) -> None:
        self.service.update_settings({
            "auto_trade_enabled": True,
            "auto_trade_time_gate_enabled": False,
            "auto_execution_mode": "dry_run",
            "auto_strategy": CROSS_MARKET_STRATEGY_ID,
        })
        observation = {
            "schema_version": 6,
            "strategy_id": CROSS_MARKET_STRATEGY_ID,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "status": "ready",
            "required_checks": {"cn_open_available": True},
            "missing_requirements": [],
            "evidence": {
                "cn_open": {
                    "available": True,
                    "regime": "high_open",
                    "gap_pct": 0.6,
                    "buy_allowed": False,
                    "force_sell": True,
                }
            },
        }
        planned_sell = {
            "accepted": False,
            "status": "planned",
            "symbol": "002281",
            "side": "sell",
            "quantity": 200.0,
        }

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService"
        ) as alphasift_service, patch.object(
            self.service,
            "_cross_market_observation_snapshot",
            return_value=observation,
        ), patch.object(
            self.service,
            "_sync_cross_market_corporate_actions",
            return_value={"available": True},
        ), patch.object(
            self.service,
            "_run_cross_market_sell_checks",
            return_value=[planned_sell],
        ) as sell_checks:
            result = self.service.run_auto_trade_once()

        sell_checks.assert_called_once()
        alphasift_service.assert_not_called()
        self.assertTrue(result["accepted"])
        self.assertFalse(result["skipped"])
        self.assertEqual(result["reason"], "cn_high_open_buy_blocked")
        self.assertEqual(result["candidate_count"], 0)
        self.assertEqual(result["orders"], [planned_sell])

        detail = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        self.assertEqual(detail["status"], "completed")
        gate = detail["diagnostics"]["cross_market_global_entry_gate"]
        self.assertTrue(gate["blocked"])
        self.assertTrue(gate["sell_checks_completed_before_gate"])
        self.assertTrue(gate["candidate_screen_skipped"])
        self.assertEqual(
            detail["diagnostics"]["stage_timings"]["completed_stage"],
            "global_entry_gate",
        )

    def test_cross_market_active_orders_continue_or_cancel_after_revalidation(self) -> None:
        plans = [
            {
                "plan_uid": "cross-active-keep",
                "status": "submitted",
                "side": "buy",
                "planned_quantity": 100.0,
                "order_result": {
                    "raw": {"cross_market_strategy": {"theme": "memory"}},
                },
            },
            {
                "plan_uid": "cross-active-cancel",
                "status": "submitted",
                "side": "buy",
                "planned_quantity": 100.0,
                "order_result": {
                    "raw": {"cross_market_strategy": {"theme": "memory"}},
                },
            },
            {
                "plan_uid": "cross-active-reduced",
                "status": "submitted",
                "side": "sell",
                "planned_quantity": 100.0,
                "order_result": {
                    "raw": {"cross_market_strategy": {"theme": "memory"}},
                },
            },
        ]
        cancel_result = {
            "accepted": True,
            "status": "cancel_requested",
            "reason": "cross_market_active_signal_expired",
        }
        with patch.object(
            self.service,
            "_active_vnpy_trade_plans",
            return_value=plans,
        ), patch.object(
            self.service,
            "_reconcile_vnpy_trade_plan",
            return_value={"supported": False, "observed": False},
        ), patch.object(
            self.service,
            "_revalidate_cross_market_trade_plan",
            side_effect=[
                (None, 100.0, {"decision": {"action": "buy"}}),
                ("cross_market_plan_korea_direction_unconfirmed", 100.0, {"decision": {"action": "blocked"}}),
                (None, 50.0, {"sell_fraction": 0.5}),
            ],
        ), patch.object(
            self.service,
            "cancel_trade_plan",
            return_value=cancel_result,
        ) as cancel:
            result = self.service.revalidate_active_cross_market_trade_plans()

        self.assertEqual(result["cross_market_count"], 3)
        self.assertEqual(result["continued_count"], 1)
        self.assertEqual(result["cancel_requested_count"], 2)
        self.assertEqual(cancel.call_count, 2)
        self.assertEqual(
            cancel.call_args_list[0].kwargs["cancellation_context"]["revalidation_reason"],
            "cross_market_plan_korea_direction_unconfirmed",
        )
        self.assertEqual(
            cancel.call_args_list[1].kwargs["cancellation_reason"],
            "cross_market_active_quantity_reduced",
        )

    def test_cross_market_pending_buy_quantity_uses_current_equity_and_price(self) -> None:
        settings = self.service._apply_cross_market_strategy_settings(
            replace(
                self.service.get_settings(),
                auto_strategy=CROSS_MARKET_STRATEGY_ID,
            )
        )
        plan = {
            "symbol": "600519",
            "market": "cn",
            "planned_quantity": 5000.0,
            "planned_price": 10.0,
            "submitted_price": 10.0,
        }
        decision = StrategyDecision(
            action="buy",
            reason="semiconductor_us_close_opening_entry_confirmed",
            buy_allowed=True,
            target_position_pct=50.0,
        )
        exposure = {
            "total_equity": 120000.0,
            "total_cash": 120000.0,
        }
        current = {"candidate_intraday": {"price": 12.0}}

        approval_quantity, approval_audit, approval_reason = (
            self.service._cross_market_revalidated_buy_quantity(
                plan=plan,
                raw={"cross_market_strategy": {"theme": "semiconductor"}},
                settings=settings,
                decision=decision,
                current=current,
                exposure_state=exposure,
                active_order=False,
            )
        )
        active_quantity, active_audit, active_reason = (
            self.service._cross_market_revalidated_buy_quantity(
                plan=plan,
                raw={"cross_market_strategy": {"theme": "semiconductor"}},
                settings=settings,
                decision=decision,
                current=current,
                exposure_state=exposure,
                active_order=True,
            )
        )

        self.assertIsNone(approval_reason)
        self.assertEqual(approval_quantity, 4900.0)
        self.assertEqual(approval_audit["sizing_price"], 12.0)
        self.assertEqual(approval_audit["allocation_basis"], "current_total_equity_pct")
        self.assertIsNone(active_reason)
        self.assertEqual(active_quantity, 5000.0)
        self.assertEqual(active_audit["sizing_price"], 10.0)

    def test_cross_market_pending_buy_rechecks_account_risk_fuses(self) -> None:
        settings = self.service._apply_cross_market_strategy_settings(
            replace(
                self.service.get_settings(),
                auto_strategy=CROSS_MARKET_STRATEGY_ID,
            )
        )
        plan = {
            "symbol": "002281",
            "side": "buy",
            "market": "cn",
            "planned_quantity": 100.0,
        }
        raw = {
            "cross_market_strategy": {
                "theme": "cpo",
            }
        }

        for risk_reason in (
            "account_drawdown_limit_reached",
            "consecutive_loss_limit_reached",
        ):
            with self.subTest(risk_reason=risk_reason), patch.object(
                self.service.cross_market_signal_service,
                "get_cn_open_signal",
                return_value={"available": True, "gap_pct": -0.6},
            ), patch.object(
                self.service,
                "_sync_cross_market_corporate_actions",
                return_value={"available": True},
            ), patch.object(
                self.service,
                "_account_pre_trade_risk",
                return_value=(
                    risk_reason,
                    {
                        "status": "blocked",
                        "reason": risk_reason,
                    },
                ),
            ) as account_risk, patch.object(
                self.service,
                "_cross_market_candidate_decision",
            ) as candidate_decision:
                reason, quantity, evidence = (
                    self.service._revalidate_cross_market_trade_plan(
                        plan=plan,
                        raw=raw,
                        settings=settings,
                        active_order=True,
                    )
                )

            self.assertEqual(reason, f"cross_market_plan_{risk_reason}")
            self.assertEqual(quantity, 0.0)
            self.assertEqual(
                evidence["account_pre_trade_risk"]["reason"],
                risk_reason,
            )
            account_risk.assert_called_once_with(settings)
            candidate_decision.assert_not_called()

    def test_cross_market_partial_second_entry_keeps_its_remaining_position_slot(self) -> None:
        self.service.data_fetcher_manager.boards_by_symbol["002281"] = [
            {"name": "CPO", "type": "concept", "change_pct": 1.2}
        ]
        settings = self.service._apply_cross_market_strategy_settings(
            replace(
                self.service.get_settings(),
                auto_strategy=CROSS_MARKET_STRATEGY_ID,
            )
        )
        strategy_position = {
            "status": "available",
            "quantity": 2500.0,
            "sellable_quantity": 0.0,
            "gross_cost_basis": 25000.0,
            "entry_reasons": ["cpo_us_close_opening_entry_confirmed"],
            "entry_theme": "cpo",
            "entry_themes": ["cpo"],
            "open_entry_order_count": 1,
            "flat_open_staged_entry": False,
        }
        exposure = {
            "available": True,
            "held_symbols": {"600519", "002281"},
            "position_values": {"600519": 50000.0, "002281": 25000.0},
            "industry_values": {},
            "industry_available": True,
            "total_market_value": 75000.0,
            "total_equity": 100000.0,
            "total_cash": 25000.0,
        }
        candidate = {
            "code": "002281",
            "score": 90.0,
            "expected_return_pct": 4.0,
            "is_core_stock": True,
            "source": "dsa_eastmoney_board_change_leader",
            "_cross_market_source_theme": "cpo",
            "_cross_market_entry_phase": "opening",
            "cross_market_strategy": {
                "decision": {
                    "reason": "cpo_us_close_opening_entry_confirmed",
                }
            },
        }

        with patch.object(
            self.service.cross_market_signal_service,
            "get_cn_open_signal",
            return_value={
                "available": True,
                "regime": "low_open",
                "gap_pct": -0.6,
                "buy_allowed": True,
                "force_sell": False,
            },
        ), patch.object(
            self.service,
            "_cross_market_strategy_position",
            return_value=strategy_position,
        ), patch.object(
            self.service,
            "_cross_market_range_signal",
            return_value={},
        ), patch.object(
            self.service,
            "_cross_market_daily_pnl_pct",
            return_value=0.0,
        ):
            decision, evidence = self.service._cross_market_candidate_decision(
                candidate=candidate,
                symbol="002281",
                settings=settings,
                exposure_state=exposure,
                pending_buy_revalidation=True,
            )

        self.assertEqual(decision.action, "buy")
        self.assertEqual(decision.target_position_pct, 25.0)
        self.assertTrue(evidence["partial_entry_position_excluded"])
        self.assertEqual(evidence["risk"]["position_count"], 1)

    def test_completed_campaign_reconciles_then_cancels_active_cross_market_order(self) -> None:
        plan = {
            "plan_uid": "cross-active-campaign-completed",
            "status": "submitted",
            "side": "buy",
            "planned_quantity": 100.0,
            "order_result": {
                "raw": {"cross_market_strategy": {"theme": "memory"}},
            },
        }
        guard = {
            "block": True,
            "reason": "paper_campaign_completed",
            "completion_session_date": "2026-09-07",
            "current_session_date": "2026-09-08",
        }
        cancellation = {
            "accepted": True,
            "status": "cancel_requested",
            "reason": "paper_campaign_completed",
        }
        call_order = []

        def reconcile(_plan):
            call_order.append("reconcile")
            return {"supported": False, "observed": False}

        def cancel(*args, **kwargs):
            call_order.append("cancel")
            return cancellation

        with patch.object(
            self.service,
            "_active_vnpy_trade_plans",
            return_value=[plan],
        ), patch.object(
            self.service,
            "_cross_market_campaign_execution_guard",
            return_value=guard,
        ), patch.object(
            self.service,
            "_reconcile_vnpy_trade_plan",
            side_effect=reconcile,
        ), patch.object(
            self.service,
            "_revalidate_cross_market_trade_plan",
        ) as revalidate, patch.object(
            self.service,
            "cancel_trade_plan",
            side_effect=cancel,
        ) as cancel_plan:
            result = self.service.revalidate_active_cross_market_trade_plans()

        self.assertEqual(call_order, ["reconcile", "cancel"])
        self.assertEqual(result["cross_market_count"], 1)
        self.assertEqual(result["cancel_requested_count"], 1)
        revalidate.assert_not_called()
        context = cancel_plan.call_args.kwargs["cancellation_context"]
        self.assertEqual(
            cancel_plan.call_args.kwargs["cancellation_reason"],
            "paper_campaign_completed",
        )
        self.assertEqual(context["revalidation_reason"], "paper_campaign_completed")
        self.assertEqual(context["evidence"]["campaign_guard"], guard)

    def test_auto_alphasift_llm_policy_uses_realistic_probe_budget_by_default(self) -> None:
        with patch.dict(
            os.environ,
            {
                "VNPY_AUTO_ALPHASIFT_LLM_TIMEOUT_SEC": "45",
                "VNPY_AUTO_ALPHASIFT_LLM_PROBE_TIMEOUT_SEC": "",
            },
            clear=False,
        ):
            policy = _resolve_auto_alphasift_llm_policy()

        self.assertEqual(policy["normal_timeout_seconds"], 45)
        self.assertEqual(policy["probe_timeout_seconds"], 45)

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
                    "SET created_at = :updated_at, updated_at = :updated_at WHERE id = :id"
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

    def test_star_market_buy_requires_at_least_two_hundred_shares(self) -> None:
        rejected = self.service.submit_order(
            symbol="688981",
            side="buy",
            market="cn",
            cash_amount=10000,
            price=60.0,
        )
        with patch(
            "src.services.portfolio_service.PortfolioService._fetch_realtime_position_price",
            return_value=(60.0, "unit-test"),
        ):
            accepted = self.service.submit_order(
                symbol="688981",
                side="buy",
                market="cn",
                cash_amount=13000,
                price=60.0,
            )

        self.assertEqual(rejected["reason"], "cash_below_min_lot")
        self.assertIn("buy 200 shares", rejected["message"])
        self.assertTrue(accepted["accepted"])
        self.assertEqual(accepted["quantity"], 216.0)

    def test_bse_buy_requires_one_hundred_shares_and_allows_unit_increments(
        self,
    ) -> None:
        rejected = self.service.submit_order(
            symbol="920045",
            side="buy",
            market="cn",
            cash_amount=990,
            price=10.0,
        )
        with patch(
            "src.services.portfolio_service.PortfolioService._fetch_realtime_position_price",
            return_value=(10.0, "unit-test"),
        ):
            accepted = self.service.submit_order(
                symbol="920045",
                side="buy",
                market="cn",
                cash_amount=1050,
                price=10.0,
            )

        self.assertEqual(rejected["reason"], "cash_below_min_lot")
        self.assertTrue(accepted["accepted"])
        self.assertEqual(accepted["quantity"], 105.0)

    def test_vnpy_bse_exchange_callbacks_map_back_to_cn_market(self) -> None:
        for exchange in ("BSE", "BJ", "XBSE", SimpleNamespace(value="BSE")):
            with self.subTest(exchange=exchange):
                self.assertEqual(
                    self.service._market_from_vnpy_exchange(exchange),
                    "cn",
                )

    def test_cross_market_cn_partial_sells_follow_board_lot_rules(self) -> None:
        normalize = self.service._cross_market_cn_sell_order_quantity

        self.assertEqual(
            normalize(symbol="300308", target_quantity=266, sellable_quantity=800),
            200.0,
        )
        self.assertEqual(
            normalize(symbol="300308", target_quantity=50, sellable_quantity=800),
            0.0,
        )
        self.assertEqual(
            normalize(symbol="300308", target_quantity=50, sellable_quantity=50),
            50.0,
        )
        self.assertEqual(
            normalize(symbol="688981", target_quantity=266, sellable_quantity=800),
            266.0,
        )
        self.assertEqual(
            normalize(symbol="688981", target_quantity=150, sellable_quantity=800),
            0.0,
        )
        self.assertEqual(
            normalize(symbol="688981", target_quantity=150, sellable_quantity=150),
            150.0,
        )
        self.assertEqual(
            normalize(symbol="920045", target_quantity=266, sellable_quantity=800),
            266.0,
        )
        self.assertEqual(
            normalize(symbol="920045", target_quantity=50, sellable_quantity=800),
            0.0,
        )
        self.assertEqual(
            normalize(symbol="920045", target_quantity=50, sellable_quantity=50),
            50.0,
        )

    def test_cross_market_budget_rejects_star_order_below_minimum_quantity(self) -> None:
        settings = replace(
            self.service.get_settings(),
            auto_strategy=CROSS_MARKET_STRATEGY_ID,
            auto_market="cn",
            auto_score_weighted_allocation_enabled=False,
        )
        sizing = {"adjusted": False}
        budget = {
            "base_cash_amount": 10000.0 / 3.0,
            "quote_cash_amount": 10000.0 / 3.0,
            "base_currency": "CNY",
            "quote_currency": "CNY",
            "rate": 1.0,
        }

        amount, resolved_budget, reason = self.service._executable_target_weight_budget(
            symbol="688981",
            settings=settings,
            budget=budget,
            sizing=sizing,
            price=60.0,
        )

        self.assertEqual(amount, 0.0)
        self.assertIs(resolved_budget, budget)
        self.assertEqual(reason, "target_weight_below_min_lot")
        self.assertEqual(sizing["executable_quantity"], 0.0)
        self.assertTrue(sizing["lot_adjusted"])

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

    def test_cross_run_quality_snapshot_forwards_calibration_scope(self) -> None:
        settings = self.service.get_settings()
        created_from = datetime(2026, 4, 1)
        quality_service = MagicMock()
        quality_service.build_quality_snapshot.return_value = {
            "state": "insufficient_evidence",
            "reason": "mature_sample_count_below_threshold",
        }

        with patch(
            "src.services.vnpy_paper_trading_service.StockSelectionAgentBacktestService",
            return_value=quality_service,
        ):
            result = self.service._cross_run_quality_snapshot(
                settings,
                recent_run_context={"runs": []},
                refresh_missing=True,
                trigger_source_filter="agent_calibration_shadow",
                run_status_filter="completed",
                created_from=created_from,
            )

        self.assertEqual(result["state"], "insufficient_evidence")
        call = quality_service.build_quality_snapshot.call_args.kwargs
        self.assertTrue(call["refresh_missing"])
        self.assertEqual(call["trigger_source"], "agent_calibration_shadow")
        self.assertEqual(call["run_status"], "completed")
        self.assertEqual(call["created_from"], created_from)

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

    def test_price_unavailable_preserves_resolution_audit(self) -> None:
        with patch.object(
            self.service.data_fetcher_manager,
            "get_realtime_quote",
            side_effect=RuntimeError("configured_quote_failure"),
        ):
            result = self.service.submit_order(
                symbol="000001",
                side="buy",
                market="cn",
                cash_amount=1200,
                price=None,
                raw={"name": "missing-quote-candidate", "score": 89},
            )

        self.assertFalse(result["accepted"])
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "price_unavailable")
        self.assertEqual(result["cash_amount"], 1200.0)
        self.assertEqual(
            result["raw"]["price_resolution"],
            {"requested_price": None, "source": "unavailable"},
        )
        self.assertEqual(result["raw"]["name"], "missing-quote-candidate")
        account_id = int(self.service.get_settings().account_id)
        trades = self.service.portfolio.list_trade_events(account_id=account_id, page=1)
        self.assertEqual(trades["items"], [])

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

    def test_cross_market_status_snapshot_does_not_overwrite_closing_evidence(
        self,
    ) -> None:
        account = self.service.ensure_account()
        account_id = int(account["id"])
        self.service.update_settings({
            "account_id": account_id,
            "auto_strategy": CROSS_MARKET_STRATEGY_ID,
        })
        self.service._invalidate_status_snapshot_cache(account_id)
        snapshot = {
            "accounts": [{
                "account_id": account_id,
                "total_cash": 100000.0,
                "positions": [],
            }],
        }

        with patch.object(
            self.service.portfolio,
            "get_portfolio_snapshot",
            return_value=snapshot,
        ) as get_snapshot:
            status = self.service.get_status(
                include_snapshot=True,
                include_recent_trades=False,
            )

        self.assertEqual(status["snapshot"], snapshot)
        get_snapshot.assert_called_once_with(
            account_id=account_id,
            persist=False,
        )

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

    def test_config_payload_read_retries_transient_partial_write(self) -> None:
        transient_error = json.JSONDecodeError("empty", "", 0)
        with (
            patch.object(
                Path,
                "read_text",
                side_effect=[transient_error, '{"settings":{"auto_trade_enabled":true}}'],
            ) as read_text,
            patch("src.services.vnpy_paper_trading_service.time.sleep") as sleep,
        ):
            payload = self.service._read_config_payload()

        self.assertTrue(payload["settings"]["auto_trade_enabled"])
        self.assertEqual(read_text.call_count, 2)
        sleep.assert_called_once()

    def test_config_payload_write_atomically_replaces_target(self) -> None:
        payload = {"settings": {"auto_trade_enabled": True}, "revision": 2}

        with patch(
            "src.services.vnpy_paper_trading_service.os.replace",
            wraps=os.replace,
        ) as replace_file:
            self.service._write_config_payload(payload)

        replace_file.assert_called_once()
        source, target = replace_file.call_args.args
        self.assertEqual(Path(target), self.config_path)
        self.assertNotEqual(Path(source), self.config_path)
        self.assertEqual(json.loads(self.config_path.read_text(encoding="utf-8")), payload)
        self.assertEqual(list(self.data_dir.glob("*.tmp")), [])

    def test_config_payload_write_retries_transient_windows_replace_lock(self) -> None:
        payload = {"settings": {"auto_trade_enabled": True}, "revision": 3}
        real_replace = os.replace
        attempts = 0

        def flaky_replace(source, target):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise PermissionError(5, "transient Windows file lock", str(target))
            return real_replace(source, target)

        with (
            patch(
                "src.services.vnpy_paper_trading_service.os.replace",
                side_effect=flaky_replace,
            ) as replace_file,
            patch("src.services.vnpy_paper_trading_service.time.sleep") as sleep,
        ):
            self.service._write_config_payload(payload)

        self.assertEqual(replace_file.call_count, 2)
        sleep.assert_called_once()
        self.assertEqual(json.loads(self.config_path.read_text(encoding="utf-8")), payload)
        self.assertEqual(list(self.data_dir.glob("*.tmp")), [])

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
        self.assertEqual(audit["portfolio_change"]["status"], "planned")
        self.assertEqual(audit["portfolio_change"]["booked_plan_count"], 0)
        self.assertEqual(audit["portfolio_change"]["planned_plan_count"], 1)
        self.assertEqual(audit["portfolio_change"]["items"], [])

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
        self.service.agent_repo.merge_run_diagnostics(
            "recent-context-completed",
            {
                "llm_recap": {
                    "status": "completed",
                    "completed_at": "2026-07-20T12:00:00+00:00",
                    "model": "openai/test",
                    "prompt_version": "vnpy_paper_agent_recap_v1",
                    "content": "Keep the next run conservative. " + ("x" * 1300),
                }
            },
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
        self.assertEqual(context["schema_version"], 3)
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
        self.assertEqual(context["llm_recap_count"], 1)
        self.assertEqual(context["latest_llm_recap"]["run_uid"], "recent-context-completed")
        self.assertTrue(context["latest_llm_recap"]["truncated"])
        self.assertEqual(len(context["latest_llm_recap"]["content"]), 1200)
        self.assertEqual(
            context["llm_recap_policy"],
            "untrusted_context_only_dynamic_plan_guardrails_remain_authoritative",
        )
        system_prompt, dynamic_prompt = self.service._llm_dynamic_agent_plan_prompts({
            "recent_run_context": context,
        })
        self.assertIn("untrusted historical audit observations", system_prompt)
        self.assertIn("Keep the next run conservative", dynamic_prompt)

    def test_recent_agent_run_context_honors_trigger_source_isolation(self) -> None:
        for trigger_source, run_uid in (
            ("vnpy_paper_auto", "recent-formal"),
            ("agent_calibration_shadow", "recent-shadow"),
        ):
            run = self.service.agent_repo.create_run(
                run_uid=run_uid,
                trigger_source=trigger_source,
                strategy="dual_low",
                market="cn",
            )
            self.service.agent_repo.complete_run(
                run_id=int(run["id"]),
                status="completed",
                candidate_count=1,
                planned_count=0,
                submitted_count=0,
                skipped_count=1,
            )

        context = self.service._recent_agent_run_context(
            self.service.get_settings(),
            trigger_source="agent_calibration_shadow",
        )

        self.assertEqual(context["trigger_source"], "agent_calibration_shadow")
        self.assertEqual(context["run_count"], 1)
        self.assertEqual(context["latest_run_uid"], "recent-shadow")
        self.assertEqual([item["run_uid"] for item in context["runs"]], ["recent-shadow"])

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
        self.assertEqual(audit["portfolio_change"]["status"], "changed")
        self.assertEqual(audit["portfolio_change"]["booked_plan_count"], 1)
        self.assertEqual(audit["portfolio_change"]["pending_plan_count"], 0)
        self.assertEqual(
            audit["portfolio_change"]["items"],
            [
                {
                    "symbol": "600519",
                    "name": None,
                    "market": "cn",
                    "buy_quantity": 100.0,
                    "sell_quantity": 0.0,
                    "buy_notional": 1000.0,
                    "sell_notional": 0.0,
                    "plan_count": 1,
                    "trade_ids": [audit["decisions"][0]["trade_id"]],
                    "net_quantity": 100.0,
                    "net_cash_flow": -1000.0,
                }
            ],
        )
        portfolio_event = next(
            event for event in audit["timeline"] if event["stage"] == "portfolio_change"
        )
        self.assertEqual(portfolio_event["status"], "changed")
        self.assertEqual(portfolio_event["details"], audit["portfolio_change"])

    def test_cross_market_strategy_uses_base_screen_and_cpo_independent_gate(self) -> None:
        self.service.data_fetcher_manager.boards_by_symbol["002281"] = [
            {"name": "CPO 光模块", "type": "concept", "change_pct": 1.2}
        ]
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_execution_mode": "dry_run",
                "auto_strategy": CROSS_MARKET_STRATEGY_ID,
                "auto_max_results": 1,
                "auto_cash_per_order": 1200,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "candidates": [
                {
                    "code": "600036",
                    "name": "Bank candidate",
                    "industry": "bank",
                    "score": 99,
                    "price": 10.0,
                },
                {
                    "code": "002281",
                    "name": "CPO candidate",
                    "concepts": "CPO 光模块",
                    "score": 75,
                    "price": 10.0,
                    "expected_return_pct": 4.0,
                    "is_core_stock": True,
                    "source": "dsa_eastmoney_board_change_leader",
                    "_cross_market_source_theme": "cpo",
                },
            ],
            "warnings": [],
        }

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ), patch.object(
            self.service.cross_market_signal_service,
            "get_cn_open_signal",
            return_value={
                "available": True,
                "regime": "low_open",
                "gap_pct": -0.6,
                "buy_allowed": True,
                "force_sell": False,
            },
        ):
            result = self.service.run_auto_trade_once()

        self.assertEqual(result["planned_count"], 1, result)
        self.assertEqual(result["orders"][0]["status"], "planned")
        evidence = result["orders"][0]["raw"]["cross_market_strategy"]
        self.assertEqual(evidence["theme"], "cpo")
        self.assertEqual(evidence["korea"]["status"], "bypassed")
        self.assertEqual(evidence["sector"]["score"], 45.118836)
        self.assertEqual(evidence["candidate_score"], 75.0)
        self.assertTrue(evidence["core_stock"]["confirmed"])
        fake_alphasift.screen.assert_called_once()
        self.assertEqual(
            fake_alphasift.screen.call_args.kwargs["strategy"],
            "momentum_quality",
        )
        self.assertEqual(fake_alphasift.screen.call_args.kwargs["max_results"], 50)
        self.assertFalse(fake_alphasift.screen.call_args.kwargs["use_llm"])
        self.assertEqual(
            fake_alphasift.screen.call_args.kwargs["candidate_scope"],
            "cross_market_target_themes",
        )
        detail = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        self.assertEqual(
            detail["diagnostics"]["cross_market_theme_prefilter"]["unsupported_count"],
            0,
        )
        self.assertEqual(
            detail["diagnostics"]["cross_market_theme_prefilter"][
                "inactive_theme_skipped_count"
            ],
            1,
        )

    def test_cross_market_cpo_rejects_high_rank_without_core_leader_provenance(self) -> None:
        self.service.data_fetcher_manager.boards_by_symbol["002281"] = [
            {"name": "CPO 光模块", "type": "concept", "change_pct": 1.2}
        ]
        settings = replace(
            self.service.get_settings(),
            auto_strategy=CROSS_MARKET_STRATEGY_ID,
        )

        with patch.object(
            self.service.cross_market_signal_service,
            "get_cn_open_signal",
            return_value={
                "available": True,
                "regime": "low_open",
                "gap_pct": -0.6,
                "buy_allowed": True,
                "force_sell": False,
            },
        ):
            decision, evidence = self.service._cross_market_candidate_decision(
                candidate={
                    "code": "002281",
                    "concepts": ["CPO"],
                    "score": 99,
                    "expected_return_pct": 4.0,
                    "_cross_market_candidate_rank": 1,
                },
                symbol="002281",
                settings=settings,
                exposure_state={},
            )

        self.assertEqual(decision.reason, "cpo_core_stock_unconfirmed")
        self.assertFalse(evidence["core_stock"]["confirmed"])
        self.assertFalse(evidence["core_stock"]["is_core_stock"])
        self.assertIsNone(evidence["core_stock"]["source"])
        self.assertIsNone(evidence["core_stock"]["source_theme"])

    def test_cross_market_theme_prefilter_enriches_missing_theme_once(self) -> None:
        self.service.data_fetcher_manager.boards_by_symbol["002281"] = [
            {"name": "CPO 光模块", "type": "concept", "change_pct": 1.2}
        ]
        candidates = [{"code": "002281", "name": "candidate", "score": 75}]

        with patch.object(
            self.service.data_fetcher_manager,
            "get_belong_boards",
            wraps=self.service.data_fetcher_manager.get_belong_boards,
        ) as get_boards:
            first, first_diagnostics = self.service._cross_market_theme_prefilter(
                candidates
            )
            second, second_diagnostics = self.service._cross_market_theme_prefilter(
                candidates
            )

        self.assertEqual(first[0]["_cross_market_prefilter_theme"], "cpo")
        self.assertIn("_cross_market_prefilter_boards_observed_at", first[0])
        self.assertEqual(first_diagnostics["enriched_count"], 1)
        self.assertEqual(second[0]["_cross_market_prefilter_theme"], "cpo")
        self.assertEqual(second_diagnostics["cache_hit_count"], 1)
        get_boards.assert_called_once_with("002281")

    def test_cross_market_theme_prefilter_allows_only_cn_main_board_entries(
        self,
    ) -> None:
        candidates = [
            {
                "code": symbol,
                "_cross_market_source_theme": "cpo",
            }
            for symbol in (
                "000001",
                "001232",
                "002281",
                "003019",
                "600000",
                "601318",
                "603019",
                "605117",
                "300620",
                "688316",
                "920045",
            )
        ]

        selected, diagnostics = self.service._cross_market_theme_prefilter(
            candidates
        )

        self.assertEqual(
            [item["code"] for item in selected],
            [
                "000001",
                "001232",
                "002281",
                "003019",
                "600000",
                "601318",
                "603019",
                "605117",
            ],
        )
        self.assertEqual(diagnostics["trading_permission"], "cn_main_board_only")
        self.assertEqual(diagnostics["trading_permission_blocked_count"], 3)
        self.assertEqual(
            diagnostics["trading_permission_blocked_symbols"],
            ["300620", "688316", "920045"],
        )

    def test_cross_market_candidate_decision_rechecks_main_board_permission(
        self,
    ) -> None:
        settings = replace(
            self.service.get_settings(),
            auto_strategy=CROSS_MARKET_STRATEGY_ID,
        )

        with patch.object(
            self.service,
            "_cross_market_live_belong_boards",
        ) as live_boards:
            for symbol in ("300620", "688316", "920045"):
                with self.subTest(symbol=symbol):
                    decision, evidence = self.service._cross_market_candidate_decision(
                        candidate={"code": symbol},
                        symbol=symbol,
                        settings=settings,
                        exposure_state={},
                    )
                    self.assertEqual(decision.action, "blocked")
                    self.assertEqual(
                        decision.reason,
                        "trading_permission_main_board_only",
                    )
                    self.assertEqual(
                        evidence["trading_permission"],
                        "cn_main_board_only",
                    )

        live_boards.assert_not_called()

    def test_cross_market_specialized_gold_candidate_is_live_validated(self) -> None:
        self.service.data_fetcher_manager.boards_by_symbol["000417"] = [
            {"name": "商贸零售", "type": "industry", "change_pct": 2.0},
            {"name": "黄金概念", "type": "concept", "change_pct": 1.7},
            {"name": "参股银行", "type": "concept", "change_pct": 1.6},
        ]
        candidates = [{
            "code": "000417",
            "name": "合百集团",
            "concepts": ["黄金概念"],
            "score": 78.5,
            "source": "dsa_eastmoney_board_change_leader",
            "_cross_market_source_theme": "gold",
        }]

        selected, diagnostics = self.service._cross_market_theme_prefilter(candidates)

        self.assertEqual([item["code"] for item in selected], ["000417"])
        self.assertEqual(selected[0]["_cross_market_prefilter_theme"], "gold")
        self.assertEqual(diagnostics["supported_count"], 1)
        self.assertEqual(diagnostics["unsupported_count"], 0)
        self.assertEqual(diagnostics["enriched_count"], 1)
        self.assertEqual(diagnostics["source_theme_verified_count"], 1)
        self.assertEqual(diagnostics["source_theme_rejected_count"], 0)

    def test_cross_market_specialized_source_theme_mismatch_fails_closed(self) -> None:
        self.service.data_fetcher_manager.boards_by_symbol["600981"] = [
            {"name": "银行", "type": "industry", "change_pct": 1.0},
            {"name": "融资融券", "type": "concept", "change_pct": 0.5},
        ]
        candidate = {
            "code": "600981",
            "name": "mismatched candidate",
            "concepts": ["半导体"],
            "score": 80.0,
            "source": "dsa_eastmoney_board_change_leader",
            "_cross_market_source_theme": "semiconductor",
        }

        first, first_diagnostics = self.service._cross_market_theme_prefilter(
            [candidate]
        )
        second, second_diagnostics = self.service._cross_market_theme_prefilter(
            [candidate]
        )

        self.assertEqual(first, [])
        self.assertEqual(first_diagnostics["unsupported_count"], 1)
        self.assertEqual(first_diagnostics["source_theme_verified_count"], 0)
        self.assertEqual(first_diagnostics["source_theme_rejected_count"], 1)
        self.assertEqual(first_diagnostics["source_theme_unavailable_count"], 0)
        self.assertEqual(second, [])
        self.assertEqual(second_diagnostics["cache_hit_count"], 1)
        self.assertEqual(second_diagnostics["source_theme_rejected_count"], 1)

        settings = replace(
            self.service.get_settings(),
            auto_strategy=CROSS_MARKET_STRATEGY_ID,
        )
        decision, evidence = self.service._cross_market_candidate_decision(
            candidate=candidate,
            symbol="600981",
            settings=settings,
            exposure_state={},
        )

        self.assertEqual(decision.action, "blocked")
        self.assertEqual(decision.reason, "source_theme_live_board_mismatch")
        self.assertEqual(evidence["source_theme"], "semiconductor")
        self.assertNotIn("semiconductor", evidence["live_board_themes"])

    def test_cross_market_target_candidate_qualification_can_fill_two_from_active_theme(self) -> None:
        candidates = [
            {
                "code": "600981",
                "name": "technology",
                "concepts": ["半导体"],
                "source": "dsa_eastmoney_board_change_leader",
                "_cross_market_source_theme": "semiconductor",
            },
            {
                "code": "002281",
                "name": "cpo",
                "concepts": ["CPO"],
                "source": "dsa_eastmoney_board_change_leader",
                "_cross_market_source_theme": "cpo",
            },
            {
                "code": "000417",
                "name": "retailer",
                "concepts": ["黄金概念"],
                "source": "dsa_eastmoney_board_change_leader",
                "_cross_market_source_theme": "gold",
            },
            {
                "code": "002371",
                "name": "technology backup",
                "concepts": ["半导体"],
                "source": "dsa_eastmoney_board_change_leader",
                "_cross_market_source_theme": "semiconductor",
            },
            {
                "code": "300502",
                "name": "cpo backup",
                "concepts": ["CPO"],
                "source": "dsa_eastmoney_board_change_leader",
                "_cross_market_source_theme": "cpo",
            },
            {
                "code": "600547",
                "name": "gold miner",
                "concepts": ["黄金"],
                "source": "dsa_eastmoney_board_change_leader",
                "_cross_market_source_theme": "gold",
            },
        ]
        self.service.data_fetcher_manager.boards_by_symbol.update({
            "600981": [{"name": "半导体", "type": "industry"}],
            "002281": [{"name": "CPO 光模块", "type": "concept"}],
            "002371": [{"name": "半导体", "type": "industry"}],
            "000417": [
                {"name": "商贸零售", "type": "industry"},
                {"name": "黄金概念", "type": "concept"},
                {"name": "参股银行", "type": "concept"},
            ],
            "600547": [{"name": "黄金", "type": "industry"}],
        })

        with patch.object(
            self.service,
            "_cross_market_completed_atr_20_pct",
            return_value=3.0,
        ) as atr:
            selected, theme_diagnostics, edge_diagnostics = (
                self.service._cross_market_qualify_target_candidates(
                    candidates,
                    target_count=2,
                    active_theme_scores={"semiconductor": 80.0},
                )
            )

        self.assertEqual(
            [candidate["code"] for candidate in selected],
            ["600981", "002371"],
        )
        self.assertEqual(
            theme_diagnostics["selected_families"],
            ["semiconductor"],
        )
        self.assertEqual(theme_diagnostics["unsupported_count"], 0)
        self.assertEqual(theme_diagnostics["skipped_filled_family_count"], 0)
        self.assertEqual(theme_diagnostics["source_theme_verified_count"], 2)
        self.assertEqual(theme_diagnostics["source_theme_rejected_count"], 0)
        self.assertEqual(theme_diagnostics["source_theme_unavailable_count"], 0)
        self.assertEqual(edge_diagnostics["qualified_count"], 2)
        self.assertEqual(atr.call_count, 2)

    def test_cross_market_target_candidate_qualification_round_robins_active_themes(
        self,
    ) -> None:
        candidates = [
            {
                "code": "603679",
                "name": "compute one",
                "concepts": ["算力租赁"],
                "source": "dsa_eastmoney_board_change_leader",
                "_cross_market_source_theme": "compute_services",
            },
            {
                "code": "000815",
                "name": "compute two",
                "concepts": ["算力租赁"],
                "source": "dsa_eastmoney_board_change_leader",
                "_cross_market_source_theme": "compute_services",
            },
            {
                "code": "603296",
                "name": "compute three",
                "concepts": ["算力租赁"],
                "source": "dsa_eastmoney_board_change_leader",
                "_cross_market_source_theme": "compute_services",
            },
            {
                "code": "300996",
                "name": "compute four",
                "concepts": ["算力租赁"],
                "source": "dsa_eastmoney_board_change_leader",
                "_cross_market_source_theme": "compute_services",
            },
            {
                "code": "603660",
                "name": "ai one",
                "concepts": ["人工智能"],
                "source": "dsa_eastmoney_board_change_leader",
                "_cross_market_source_theme": "artificial_intelligence",
            },
            {
                "code": "002131",
                "name": "ai two",
                "concepts": ["人工智能"],
                "source": "dsa_eastmoney_board_change_leader",
                "_cross_market_source_theme": "artificial_intelligence",
            },
            {
                "code": "002281",
                "name": "cpo one",
                "concepts": ["CPO"],
                "source": "dsa_eastmoney_board_change_leader",
                "_cross_market_source_theme": "cpo",
            },
            {
                "code": "603186",
                "name": "ccl one",
                "concepts": ["覆铜板"],
                "source": "dsa_eastmoney_board_change_leader",
                "_cross_market_source_theme": "ccl",
            },
            {
                "code": "002517",
                "name": "gaming one",
                "concepts": ["游戏"],
                "source": "dsa_eastmoney_board_change_leader",
                "_cross_market_source_theme": "gaming",
            },
        ]
        self.service.data_fetcher_manager.boards_by_symbol.update({
            "603679": [{"name": "算力租赁", "type": "concept"}],
            "000815": [{"name": "算力租赁", "type": "concept"}],
            "603660": [{"name": "人工智能", "type": "concept"}],
            "002281": [{"name": "CPO", "type": "concept"}],
            "603186": [{"name": "覆铜板", "type": "concept"}],
            "002517": [{"name": "游戏", "type": "industry"}],
        })

        with patch.object(
            self.service,
            "_cross_market_completed_atr_20_pct",
            return_value=3.0,
        ) as atr:
            selected, theme_diagnostics, edge_diagnostics = (
                self.service._cross_market_qualify_target_candidates(
                    candidates,
                    target_count=6,
                    active_theme_scores={
                        "compute_services": 100.0,
                        "artificial_intelligence": 97.0,
                        "cpo": 96.0,
                        "ccl": 93.0,
                        "gaming": 70.0,
                    },
                )
            )

        self.assertEqual(
            [candidate["code"] for candidate in selected],
            ["603679", "603660", "002281", "603186", "002517", "000815"],
        )
        self.assertEqual(
            theme_diagnostics["candidate_family_order"],
            [
                "compute_services",
                "artificial_intelligence",
                "cpo",
                "ccl",
                "gaming",
            ],
        )
        self.assertEqual(
            theme_diagnostics["qualification_mode"],
            "active_theme_round_robin_fill",
        )
        self.assertEqual(theme_diagnostics["source_theme_verified_count"], 6)
        self.assertEqual(theme_diagnostics["inactive_theme_skipped_count"], 0)
        self.assertEqual(edge_diagnostics["qualified_count"], 6)
        self.assertEqual(atr.call_count, 6)

    def test_cross_market_auto_trade_uses_reserve_candidates_until_two_plans(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_execution_mode": "dry_run",
                "auto_strategy": CROSS_MARKET_STRATEGY_ID,
                "auto_max_results": 2,
            }
        )
        candidates = [
            {
                "code": f"60000{index}",
                "name": f"semiconductor candidate {index}",
                "industry": "semiconductor",
                "score": 90 - index,
                "price": 10.0,
                "turnover_amount": 200_000_000.0,
                "source": "dsa_eastmoney_board_change_leader",
                "_cross_market_source_theme": "semiconductor",
            }
            for index in range(1, 6)
        ]
        self.service.data_fetcher_manager.boards_by_symbol.update({
            f"60000{index}": [
                {"name": "半导体", "type": "industry", "change_pct": 1.0}
            ]
            for index in range(1, 6)
        })
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "quality_status": "ok",
            "candidates": candidates,
            "warnings": [],
            "source_errors": [],
        }
        blocked_one = StrategyDecision(
            action="blocked",
            reason="candidate_realtime_quote_unavailable",
        )
        blocked_two = StrategyDecision(
            action="blocked",
            reason="board_technical_evidence_unavailable",
        )
        buy = StrategyDecision(
            action="buy",
            reason="low_open_reclaim_confirmed",
            buy_allowed=True,
            target_tranche_delta=1,
            target_position_pct=50.0,
        )
        decision_evidence = {
            "strategy_id": CROSS_MARKET_STRATEGY_ID,
            "theme": "semiconductor",
            "candidate_intraday": {
                "price": 10.0,
                "amount": 200_000_000.0,
            },
        }

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ), patch.object(
            self.service,
            "_cross_market_active_theme_scores",
            return_value=({"semiconductor": 80.0}, {"entry_phase": "opening"}),
        ), patch.object(
            self.service,
            "_cross_market_completed_atr_20_pct",
            return_value=3.0,
        ), patch.object(
            self.service,
            "_cross_market_candidate_decision",
            side_effect=[
                (blocked_one, decision_evidence),
                (blocked_two, decision_evidence),
                (buy, decision_evidence),
                (buy, decision_evidence),
                (buy, decision_evidence),
            ],
        ) as candidate_decision:
            result = self.service.run_auto_trade_once()

        self.assertEqual(result["candidate_count"], 5)
        self.assertEqual(result["planned_count"], 2, result)
        self.assertEqual(result["skipped_count"], 2, result)
        self.assertEqual(candidate_decision.call_count, 4)
        self.assertEqual(
            [call.kwargs["symbol"] for call in candidate_decision.call_args_list],
            ["600001", "600002", "600003", "600004"],
        )
        self.assertEqual(
            [item["symbol"] for item in result["orders"] if item["status"] == "planned"],
            ["600003", "600004"],
        )
        detail = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        pool = detail["diagnostics"]["cross_market_candidate_decision_pool"]
        self.assertEqual(pool["order_activity_limit"], 2)
        self.assertEqual(pool["candidate_decision_limit"], 6)
        self.assertEqual(pool["reserve_candidate_count"], 4)

    def test_cross_market_formal_recovery_is_persisted_as_intraday_phase(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_execution_mode": "dry_run",
                "auto_strategy": CROSS_MARKET_STRATEGY_ID,
                "auto_max_results": 2,
            }
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
        ), patch.object(
            self.service,
            "_cross_market_active_theme_scores",
            return_value=({}, {"entry_phase": "intraday_dip"}),
        ) as active_themes:
            result = self.service.run_auto_trade_once(
                trigger_source_override="vnpy_paper_auto",
                allow_cross_market_intraday_entry_recheck=True,
            )

        detail = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        diagnostics = detail["diagnostics"]
        self.assertTrue(diagnostics["formal_recovery"])
        self.assertTrue(diagnostics["intraday_entry_recheck"])
        self.assertEqual(diagnostics["cross_market_entry_phase"], "intraday_dip")
        active_themes.assert_called_once_with(entry_phase="intraday_dip")

    def test_cross_market_board_leaders_attach_atr_without_dropping_missing_history(self) -> None:
        candidates = [
            {
                "code": "603583",
                "source": "dsa_eastmoney_board_change_leader",
            },
            {
                "code": "002281",
                "source": "dsa_eastmoney_board_change_leader",
            },
            {
                "code": "600547",
                "source": "other_source",
            },
        ]

        with patch.object(
            self.service,
            "_cross_market_completed_atr_20_pct",
            side_effect=[None, 2.4],
        ) as atr:
            selected, diagnostics = (
                self.service._cross_market_enrich_target_candidate_volatility(candidates)
            )

        self.assertEqual(
            [item["code"] for item in selected],
            ["603583", "002281", "600547"],
        )
        self.assertNotIn("atr_20_pct", selected[0])
        self.assertEqual(selected[1]["atr_20_pct"], 2.4)
        self.assertEqual(diagnostics["insufficient_history_count"], 1)
        self.assertEqual(diagnostics["enriched_count"], 1)
        self.assertEqual(atr.call_count, 2)

    def test_cross_market_range_signal_adds_to_existing_strategy_position(self) -> None:
        self.service.data_fetcher_manager.boards_by_symbol["002281"] = [
            {"name": "CPO", "type": "concept"}
        ]
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_execution_mode": "dry_run",
                "auto_strategy": CROSS_MARKET_STRATEGY_ID,
                "auto_max_results": 1,
            }
        )
        account = self.service.ensure_account()
        self.service.portfolio.record_trade(
            account_id=int(account["id"]),
            symbol="002281",
            trade_date=date.today() - timedelta(days=1),
            side="buy",
            quantity=100,
            price=10.0,
            market="cn",
            currency="CNY",
            note="vn.py paper | source=cross_market_auto_entry | price=explicit",
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "candidates": [{
                "code": "002281",
                "name": "CPO candidate",
                "score": 75,
                "price": 10.0,
                "expected_return_pct": 4.0,
                "is_core_stock": True,
                "source": "dsa_eastmoney_board_change_leader",
                "_cross_market_source_theme": "cpo",
                "adx14": 16.0,
                "ma20_slope_pct_per_day": 0.05,
                "range_low": 9.0,
                "rsi14": 32.0,
                "bollinger_position": 0.1,
            }],
            "warnings": [],
        }

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ), patch.object(
            self.service.cross_market_signal_service,
            "get_cn_open_signal",
            return_value={
                "available": True,
                "regime": "flat_open",
                "gap_pct": 0.0,
                "buy_allowed": False,
                "force_sell": False,
            },
        ):
            result = self.service.run_auto_trade_once()

        self.assertEqual(result["planned_count"], 1, result)
        evidence = result["orders"][0]["raw"]["cross_market_strategy"]
        self.assertEqual(evidence["decision"]["reason"], "range_add_tranche")
        self.assertEqual(evidence["strategy_position"]["current_tranche_count"], 1)

    def test_cross_market_tranche_count_tracks_remaining_strategy_cost_basis(self) -> None:
        initial_cash = 100000.0

        self.assertEqual(
            self.service._cross_market_current_tranche_count(
                strategy_position={"quantity": 100, "gross_cost_basis": 25000.0},
                initial_cash=initial_cash,
            ),
            1,
        )
        self.assertEqual(
            self.service._cross_market_current_tranche_count(
                strategy_position={"quantity": 200, "gross_cost_basis": 50000.0},
                initial_cash=initial_cash,
            ),
            2,
        )
        self.assertEqual(
            self.service._cross_market_current_tranche_count(
                strategy_position={"quantity": 300, "gross_cost_basis": 75000.0},
                initial_cash=initial_cash,
            ),
            2,
        )
        self.assertEqual(
            self.service._cross_market_current_tranche_count(
                strategy_position={"quantity": 0, "gross_cost_basis": 0},
                initial_cash=initial_cash,
            ),
            0,
        )

    def test_cross_market_tranche_count_groups_partial_fills_by_vnpy_order(self) -> None:
        account = self.service.ensure_account()
        account_id = int(account["id"])
        shared_note = (
            "vn.py paper | source=cross_market_auto_entry | "
            "vn.py callback vt_orderid=DSA_SIM.entry-one; "
            "entry_reason=cpo_flat_open_staged_entry_confirmed; entry_theme=cpo"
        )
        for index in range(2):
            self.service.portfolio.record_trade(
                account_id=account_id,
                symbol="002281",
                trade_date=date.today(),
                side="buy",
                quantity=100,
                price=150.0,
                market="cn",
                currency="CNY",
                trade_uid=f"partial-fill-{index}",
                note=shared_note,
            )

        one_order_position = self.service._cross_market_strategy_position(
            account_id=account_id,
            symbol="002281",
            market="cn",
            as_of=date.today(),
        )

        self.assertEqual(one_order_position["gross_cost_basis"], 30000.0)
        self.assertEqual(one_order_position["open_entry_order_count"], 1)
        self.assertEqual(
            self.service._cross_market_current_tranche_count(
                strategy_position=one_order_position,
                initial_cash=100000.0,
            ),
            1,
        )

        self.service.portfolio.record_trade(
            account_id=account_id,
            symbol="002281",
            trade_date=date.today(),
            side="buy",
            quantity=100,
            price=100.0,
            market="cn",
            currency="CNY",
            trade_uid="second-entry-order",
            note=(
                "vn.py paper | source=cross_market_auto_entry | "
                "vn.py callback vt_orderid=DSA_SIM.entry-two; "
                "entry_reason=range_add_tranche; entry_theme=cpo"
            ),
        )
        two_order_position = self.service._cross_market_strategy_position(
            account_id=account_id,
            symbol="002281",
            market="cn",
            as_of=date.today(),
        )
        self.assertEqual(two_order_position["open_entry_order_count"], 2)
        self.assertEqual(
            self.service._cross_market_current_tranche_count(
                strategy_position=two_order_position,
                initial_cash=100000.0,
            ),
            2,
        )

    def test_cross_market_auto_trade_rejects_immediate_local_paper_fill(self) -> None:
        self.service.data_fetcher_manager.boards_by_symbol["002281"] = [
            {"name": "CPO", "type": "concept", "change_pct": 1.2}
        ]
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_execution_mode": "paper",
                "auto_strategy": CROSS_MARKET_STRATEGY_ID,
                "auto_max_results": 1,
                "auto_cash_per_order": 1200,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "candidates": [{
                "code": "002281",
                "name": "CPO candidate",
                "score": 75,
                "price": 10.0,
                "expected_return_pct": 4.0,
                "is_core_stock": True,
                "source": "dsa_eastmoney_board_change_leader",
                "_cross_market_source_theme": "cpo",
            }],
            "warnings": [],
        }

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ), patch.object(
            self.service.cross_market_signal_service,
            "get_cn_open_signal",
            return_value={
                "available": True,
                "regime": "low_open",
                "gap_pct": -0.6,
                "buy_allowed": True,
                "force_sell": False,
            },
        ):
            result = self.service.run_auto_trade_once()

        self.assertEqual(result["submitted_count"], 0)
        self.assertEqual(result["orders"][0]["status"], "skipped")
        self.assertEqual(
            result["orders"][0]["reason"],
            "cross_market_next_minute_execution_unavailable",
        )
        self.assertEqual(
            result["orders"][0]["raw"]["required_execution"],
            "next_1m_vwap",
        )

    def test_cross_market_account_risk_blocks_before_candidate_analysis(self) -> None:
        self.service.data_fetcher_manager.boards_by_symbol["002281"] = [
            {"name": "CPO 光模块", "type": "concept", "change_pct": 1.2}
        ]
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_execution_mode": "dry_run",
                "auto_strategy": CROSS_MARKET_STRATEGY_ID,
                "auto_max_results": 1,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "quality_status": "ok",
            "candidates": [{
                "code": "002281",
                "name": "CPO candidate",
                "score": 75,
                "price": 10.0,
                "expected_return_pct": 4.0,
                "is_core_stock": True,
                "source": "dsa_eastmoney_board_change_leader",
                "_cross_market_source_theme": "cpo",
            }],
            "warnings": [],
            "source_errors": [],
        }

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ), patch.object(
            self.service,
            "_account_pre_trade_risk",
            return_value=(
                "account_drawdown_limit_reached",
                {
                    "status": "blocked",
                    "reason": "account_drawdown_limit_reached",
                },
            ),
        ), patch.object(
            self.service,
            "_cross_market_candidate_decision",
        ) as candidate_decision:
            result = self.service.run_auto_trade_once()

        self.assertEqual(result["submitted_count"], 0)
        self.assertEqual(result["skipped_count"], 1)
        self.assertEqual(
            result["orders"][0]["reason"],
            "account_drawdown_limit_reached",
        )
        candidate_decision.assert_not_called()

    def test_cross_market_auto_trade_sizes_two_main_board_orders_with_cost_reserve(self) -> None:
        self.service.data_fetcher_manager.price = 60.0
        self.service.data_fetcher_manager.boards_by_symbol["600777"] = [
            {"name": "CPO", "type": "concept", "change_pct": 1.2}
        ]
        self.service.data_fetcher_manager.boards_by_symbol["600778"] = [
            {"name": "CPO", "type": "concept", "change_pct": 1.2}
        ]
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_execution_mode": "dry_run",
                "auto_strategy": CROSS_MARKET_STRATEGY_ID,
                "auto_max_results": 1,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "candidates": [
                {
                    "code": "600777",
                    "name": "CPO main-board candidate one",
                    "score": 75,
                    "price": 10.0,
                    "expected_return_pct": 4.0,
                    "is_core_stock": True,
                    "source": "dsa_eastmoney_board_change_leader",
                    "_cross_market_source_theme": "cpo",
                },
                {
                    "code": "600778",
                    "name": "CPO main-board candidate two",
                    "score": 74,
                    "price": 10.0,
                    "expected_return_pct": 4.0,
                    "is_core_stock": True,
                    "source": "dsa_eastmoney_board_change_leader",
                    "_cross_market_source_theme": "cpo",
                },
            ],
            "warnings": [],
        }

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ), patch.object(
            self.service.cross_market_signal_service,
            "get_cn_open_signal",
            return_value={
                "available": True,
                "regime": "low_open",
                "gap_pct": -0.6,
                "buy_allowed": True,
                "force_sell": False,
            },
        ):
            result = self.service.run_auto_trade_once()

        self.assertEqual(result["planned_count"], 2)
        total_maximum_debit = 0.0
        for order in result["orders"]:
            with self.subTest(symbol=order["symbol"]):
                self.assertEqual(order["reason"], "dry_run")
                self.assertEqual(order["price"], 60.0)
                evidence = order["raw"]["cross_market_strategy"]
                self.assertEqual(evidence["candidate_intraday"]["price"], 60.0)
                self.assertEqual(
                    order["raw"]["target_weight_sizing"]["executable_quantity"],
                    800.0,
                )
                execution_budget = order["raw"]["cross_market_execution_budget"]
                self.assertEqual(
                    execution_budget["allocation_basis"],
                    "current_total_equity_pct",
                )
                self.assertEqual(execution_budget["target_position_pct"], 50.0)
                self.assertEqual(
                    execution_budget["total_equity_reference"],
                    100000.0,
                )
                self.assertEqual(execution_budget["cash_allocation_cap"], 50000.0)
                self.assertLess(execution_budget["signal_notional_cap"], 50000.0)
                self.assertTrue(execution_budget["fill_notional_capped_by_limit_price"])
                self.assertEqual(execution_budget["maximum_dynamic_slippage_bps"], 50.0)
                self.assertLessEqual(
                    execution_budget["signal_notional_cap"]
                    + execution_budget["reserved_buy_fees"]["total"],
                    execution_budget["cash_allocation_cap"],
                )
                actual_fees = TradeFeeSchedule().calculate(
                    side="buy",
                    notional=order["cash_amount"],
                    instrument_type="stock",
                )
                total_maximum_debit += order["cash_amount"] + actual_fees["total"]
        self.assertLessEqual(total_maximum_debit, 100000.0)

    def test_cross_market_low_open_rejects_high_score_stock_when_sector_is_weak(self) -> None:
        self.service.data_fetcher_manager.boards_by_symbol["002281"] = [
            {"name": "CPO", "type": "concept", "change_pct": -0.2}
        ]
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_execution_mode": "dry_run",
                "auto_strategy": CROSS_MARKET_STRATEGY_ID,
                "auto_max_results": 1,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "candidates": [{
                "code": "002281",
                "name": "CPO candidate",
                "score": 99,
                "price": 10.0,
                "expected_return_pct": 4.0,
                "is_core_stock": True,
                "source": "dsa_eastmoney_board_change_leader",
                "_cross_market_source_theme": "cpo",
            }],
            "warnings": [],
        }

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ), patch.object(
            self.service.cross_market_signal_service,
            "get_cn_open_signal",
            return_value={
                "available": True,
                "regime": "low_open",
                "gap_pct": -0.6,
                "buy_allowed": True,
                "force_sell": False,
            },
        ):
            result = self.service.run_auto_trade_once()

        self.assertEqual(result["planned_count"], 0)
        self.assertEqual(result["orders"][0]["reason"], "sector_signal_too_weak")
        evidence = result["orders"][0]["raw"]["cross_market_strategy"]
        self.assertEqual(evidence["candidate_score"], 99.0)
        self.assertEqual(evidence["sector"]["score"], 0.0)

    def test_cross_market_high_open_plans_only_t_plus_one_sellable_quantity(self) -> None:
        self.service.data_fetcher_manager.price = 10.3
        self.service.data_fetcher_manager.open_price = 10.2
        self.service.data_fetcher_manager.pre_close = 10.0
        self.service.data_fetcher_manager.high = 10.4
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_execution_mode": "dry_run",
                "auto_strategy": CROSS_MARKET_STRATEGY_ID,
                "auto_max_results": 1,
            }
        )
        account = self.service.ensure_account()
        account_id = int(account["id"])
        self.service.portfolio.record_trade(
            account_id=account_id,
            symbol="002281",
            trade_date=date.today() - timedelta(days=1),
            side="buy",
            quantity=200,
            price=10.0,
            market="cn",
            currency="CNY",
            note="vn.py paper | source=cross_market_auto_entry | price=explicit",
        )
        self.service.portfolio.record_trade(
            account_id=account_id,
            symbol="002281",
            trade_date=date.today(),
            side="buy",
            quantity=100,
            price=10.0,
            market="cn",
            currency="CNY",
            note="vn.py paper | source=cross_market_auto_entry | price=explicit",
        )
        self.service.data_fetcher_manager.boards_by_symbol["002281"] = [
            {"name": "CPO 光模块", "type": "concept"}
        ]
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {"candidates": [], "warnings": []}

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ), patch.object(
            self.service.cross_market_signal_service,
            "get_cn_open_signal",
            return_value={
                "available": True,
                "regime": "high_open",
                "gap_pct": 0.6,
                "buy_allowed": False,
                "force_sell": True,
            },
        ):
            result = self.service.run_auto_trade_once()

        sell = next(order for order in result["orders"] if order["side"] == "sell")
        self.assertEqual(sell["status"], "planned")
        self.assertEqual(sell["quantity"], 200.0)
        self.assertEqual(sell["raw"]["sellable_quantity"], 200.0)
        self.assertEqual(
            sell["raw"]["reason"],
            "next_day_high_open_trailing_exit",
        )

    def test_cross_market_index_high_open_does_not_sell_flat_open_stock(self) -> None:
        self.service.update_settings({
            "auto_trade_enabled": True,
            "auto_execution_mode": "dry_run",
            "auto_strategy": CROSS_MARKET_STRATEGY_ID,
        })
        account = self.service.ensure_account()
        self.service.portfolio.record_trade(
            account_id=int(account["id"]),
            symbol="002281",
            trade_date=date.today() - timedelta(days=1),
            side="buy",
            quantity=200,
            price=10.0,
            market="cn",
            currency="CNY",
            note=(
                "vn.py paper | source=cross_market_auto_entry | price=explicit | "
                "entry_reason=cpo_us_close_opening_entry_confirmed; entry_theme=cpo"
            ),
        )

        with patch.object(
            self.service,
            "_trading_window_diagnostics",
            return_value={"is_market_open_now": True, "phase": "intraday"},
        ), patch.object(
            self.service.cross_market_signal_service,
            "get_cn_open_signal",
            return_value={
                "available": True,
                "regime": "high_open",
                "gap_pct": 0.8,
                "buy_allowed": False,
                "force_sell": True,
            },
        ), patch.object(
            self.service,
            "_cross_market_position_range_signal",
            return_value={},
        ):
            result = self.service.run_cross_market_intraday_sell_monitor()

        self.assertEqual(result["candidate_count"], 1)
        self.assertEqual(result["planned_count"], 0)
        self.assertEqual(result["orders"], [])

    def test_cross_market_intraday_sell_uses_persisted_theme_and_nasdaq_futures(self) -> None:
        self.service.update_settings({
            "auto_trade_enabled": True,
            "auto_execution_mode": "dry_run",
            "auto_strategy": CROSS_MARKET_STRATEGY_ID,
        })
        account = self.service.ensure_account()
        self.service.portfolio.record_trade(
            account_id=int(account["id"]),
            symbol="002281",
            trade_date=date.today() - timedelta(days=1),
            side="buy",
            quantity=200,
            price=10.0,
            market="cn",
            currency="CNY",
            note=(
                "vn.py paper | source=cross_market_auto_entry | price=explicit | "
                "entry_reason=artificial_intelligence_us_close_opening_entry_confirmed; "
                "entry_theme=artificial_intelligence"
            ),
        )
        self.service.data_fetcher_manager.boards_by_symbol["002281"] = [
            {"name": "gold concept", "type": "concept"}
        ]

        with patch.object(
            self.service,
            "_trading_window_diagnostics",
            return_value={"is_market_open_now": True, "phase": "intraday"},
        ), patch.object(
            self.service.cross_market_signal_service,
            "get_cn_open_signal",
            return_value={
                "available": True,
                "regime": "flat_open",
                "gap_pct": 0.0,
                "buy_allowed": False,
                "force_sell": False,
            },
        ), patch.object(
            self.service.cross_market_signal_service,
            "get_nasdaq_futures_signal_for_cn_trade",
            return_value={
                "available": True,
                "confirmed": True,
                "sell_fraction": 0.5,
                "reason": "nasdaq_futures_severe_downtrend",
            },
        ) as nasdaq, patch.object(
            self.service,
            "_cross_market_position_range_signal",
            return_value={},
        ):
            result = self.service.run_cross_market_intraday_sell_monitor()

        self.assertEqual(result["planned_count"], 1)
        order = result["orders"][0]
        self.assertEqual(order["quantity"], 100.0)
        self.assertEqual(order["raw"]["theme"], "artificial_intelligence")
        self.assertEqual(order["raw"]["theme_source"], "persisted_entry_theme")
        self.assertEqual(order["raw"]["reason"], "nasdaq_futures_severe_downtrend")
        nasdaq.assert_called_once()

    def test_cross_market_hard_stop_precedes_high_open_exit(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_execution_mode": "dry_run",
                "auto_strategy": CROSS_MARKET_STRATEGY_ID,
                "auto_max_results": 1,
            }
        )
        account = self.service.ensure_account()
        self.service.portfolio.record_trade(
            account_id=int(account["id"]),
            symbol="002281",
            trade_date=date.today() - timedelta(days=1),
            side="buy",
            quantity=100,
            price=10.0,
            market="cn",
            currency="CNY",
            note="vn.py paper | source=cross_market_auto_entry | price=explicit",
        )
        self.service.data_fetcher_manager.price = 9.4
        self.service.data_fetcher_manager.boards_by_symbol["002281"] = [
            {"name": "CPO", "type": "concept"}
        ]
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {"candidates": [], "warnings": []}

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ), patch.object(
            self.service.cross_market_signal_service,
            "get_cn_open_signal",
            return_value={
                "available": True,
                "regime": "high_open",
                "gap_pct": 0.6,
                "buy_allowed": False,
                "force_sell": True,
            },
        ):
            result = self.service.run_auto_trade_once()

        sell = next(order for order in result["orders"] if order["side"] == "sell")
        self.assertEqual(sell["status"], "planned")
        self.assertEqual(sell["quantity"], 100.0)
        self.assertEqual(sell["raw"]["reason"], "hard_stop_loss")

    def test_cross_market_sell_fails_closed_on_stale_stock_quote(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_execution_mode": "dry_run",
                "auto_strategy": CROSS_MARKET_STRATEGY_ID,
            }
        )
        account = self.service.ensure_account()
        self.service.portfolio.record_trade(
            account_id=int(account["id"]),
            symbol="002281",
            trade_date=date.today() - timedelta(days=1),
            side="buy",
            quantity=100,
            price=10.0,
            market="cn",
            currency="CNY",
            note="vn.py paper | source=cross_market_auto_entry | price=explicit",
        )
        self.service.data_fetcher_manager.boards_by_symbol["002281"] = [
            {"name": "CPO", "type": "concept"}
        ]
        stale_quote = SimpleNamespace(
            price=9.0,
            provider_timestamp=(datetime.now(timezone.utc) - timedelta(seconds=121)).isoformat(),
            amount=9000.0,
            volume=1000.0,
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {"candidates": [], "warnings": []}

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ), patch.object(
            self.service.data_fetcher_manager,
            "get_realtime_quote",
            return_value=stale_quote,
        ), patch.object(
            self.service.cross_market_signal_service,
            "get_cn_open_signal",
            return_value={
                "available": True,
                "regime": "high_open",
                "gap_pct": 0.6,
                "buy_allowed": False,
                "force_sell": True,
            },
        ):
            result = self.service.run_auto_trade_once()

        self.assertFalse(any(order["side"] == "sell" for order in result["orders"]))

    def test_cross_market_sell_prefers_strict_timestamp_route(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_execution_mode": "dry_run",
                "auto_strategy": CROSS_MARKET_STRATEGY_ID,
            }
        )
        account = self.service.ensure_account()
        self.service.portfolio.record_trade(
            account_id=int(account["id"]),
            symbol="002281",
            trade_date=date.today() - timedelta(days=1),
            side="buy",
            quantity=100,
            price=10.0,
            market="cn",
            currency="CNY",
            note="vn.py paper | source=cross_market_auto_entry | price=explicit",
        )
        self.service.data_fetcher_manager.boards_by_symbol["002281"] = [
            {"name": "CPO", "type": "concept"}
        ]
        strict_quote = SimpleNamespace(
            price=9.4,
            provider_timestamp=datetime.now(timezone.utc).isoformat(),
            amount=9400.0,
            volume=1000.0,
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {"candidates": [], "warnings": []}
        flat_open = {
            "available": True,
            "regime": "flat",
            "gap_pct": 0.0,
            "buy_allowed": False,
            "force_sell": False,
        }

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ), patch.object(
            self.service.data_fetcher_manager,
            "get_realtime_quote_with_provider_timestamp",
            return_value=strict_quote,
            create=True,
        ) as strict_getter, patch.object(
            self.service.data_fetcher_manager,
            "get_realtime_quote",
            side_effect=AssertionError(
                "generic quote route must not be used for cross-market exits"
            ),
        ), patch.object(
            self.service.cross_market_signal_service,
            "get_cn_open_signal",
            return_value=flat_open,
        ):
            result = self.service.run_auto_trade_once()

        sell = next(order for order in result["orders"] if order["side"] == "sell")
        self.assertEqual(sell["raw"]["reason"], "hard_stop_loss")
        self.assertEqual(sell["raw"]["provider_timestamp"], strict_quote.provider_timestamp)
        strict_getter.assert_called_once_with("002281")

    def test_cross_market_sell_does_not_claim_manual_position(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_execution_mode": "dry_run",
                "auto_strategy": CROSS_MARKET_STRATEGY_ID,
            }
        )
        account = self.service.ensure_account()
        self.service.portfolio.record_trade(
            account_id=int(account["id"]),
            symbol="002281",
            trade_date=date.today() - timedelta(days=1),
            side="buy",
            quantity=100,
            price=10.0,
            market="cn",
            currency="CNY",
            note="manual position",
        )
        self.service.data_fetcher_manager.boards_by_symbol["002281"] = [
            {"name": "CPO", "type": "concept"}
        ]
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {"candidates": [], "warnings": []}

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ), patch.object(
            self.service.cross_market_signal_service,
            "get_cn_open_signal",
            return_value={
                "available": True,
                "regime": "high_open",
                "gap_pct": 0.6,
                "buy_allowed": False,
                "force_sell": True,
            },
        ):
            result = self.service.run_auto_trade_once()

        self.assertFalse(any(order["side"] == "sell" for order in result["orders"]))

    def test_cross_market_intraday_sell_monitor_runs_sell_only_for_strategy_position(self) -> None:
        self.service.data_fetcher_manager.price = 10.3
        self.service.data_fetcher_manager.open_price = 10.2
        self.service.data_fetcher_manager.pre_close = 10.0
        self.service.data_fetcher_manager.high = 10.4
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_execution_mode": "dry_run",
                "auto_strategy": CROSS_MARKET_STRATEGY_ID,
            }
        )
        account = self.service.ensure_account()
        self.service.portfolio.record_trade(
            account_id=int(account["id"]),
            symbol="002281",
            trade_date=date.today() - timedelta(days=1),
            side="buy",
            quantity=100,
            price=10.0,
            market="cn",
            currency="CNY",
            note="vn.py paper | source=cross_market_auto_entry | price=explicit",
        )
        self.service.data_fetcher_manager.boards_by_symbol["002281"] = [
            {"name": "CPO", "type": "concept"}
        ]

        with patch.object(
            self.service,
            "_trading_window_diagnostics",
            return_value={"is_market_open_now": True, "phase": "intraday"},
        ), patch.object(
            self.service,
            "_account_drawdown_diagnostics",
            return_value={"status": "blocked", "drawdown_pct": 8.0},
        ), patch.object(
            self.service.cross_market_signal_service,
            "get_cn_open_signal",
            return_value={
                "available": True,
                "regime": "high_open",
                "gap_pct": 0.6,
                "buy_allowed": False,
                "force_sell": True,
            },
        ), patch.object(
            self.service,
            "_cross_market_position_range_signal",
            return_value={},
        ), patch.object(
            self.service,
            "_cross_market_daily_pnl_pct",
            return_value=-3.0,
        ) as daily_loss_gate, patch.object(
            self.service.portfolio,
            "get_portfolio_snapshot",
            wraps=self.service.portfolio.get_portfolio_snapshot,
        ) as snapshot_reads:
            result = self.service.run_cross_market_intraday_sell_monitor()

        self.assertTrue(result["sell_only"])
        self.assertEqual(result["candidate_count"], 1)
        self.assertEqual(result["planned_count"], 1)
        self.assertEqual(result["submitted_count"], 0)
        self.assertEqual(result["orders"][0]["side"], "sell")
        self.assertEqual(
            result["orders"][0]["raw"]["reason"],
            "next_day_high_open_trailing_exit",
        )
        detail = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertEqual(detail["trigger_source"], "cross_market_intraday_sell_monitor")
        self.assertTrue(detail["diagnostics"]["sell_only"])
        self.assertFalse(detail["diagnostics"]["places_buy_orders"])
        daily_loss_gate.assert_not_called()
        self.assertGreater(snapshot_reads.call_count, 0)
        self.assertTrue(
            all(call.kwargs.get("persist") is False for call in snapshot_reads.call_args_list)
        )

    def test_cross_market_submitted_trailing_stop_keeps_peak_until_position_is_gone(self) -> None:
        self.service.update_settings({
            "auto_trade_enabled": True,
            "auto_execution_mode": "vnpy_paper",
            "auto_strategy": CROSS_MARKET_STRATEGY_ID,
        })
        account = self.service.ensure_account()
        account_id = int(account["id"])
        self.service.portfolio.record_trade(
            account_id=account_id,
            symbol="002281",
            trade_date=date.today() - timedelta(days=1),
            side="buy",
            quantity=100,
            price=10.0,
            market="cn",
            currency="CNY",
            note=(
                "vn.py paper | source=cross_market_auto_entry | price=explicit | "
                "entry_reason=cpo_us_close_opening_entry_confirmed; entry_theme=cpo"
            ),
        )
        self.service.data_fetcher_manager.price = 11.4
        self.service.data_fetcher_manager.open_price = 11.4
        self.service.data_fetcher_manager.pre_close = 11.4
        self.service.data_fetcher_manager.high = 11.5
        self.service._save_trailing_peaks({"002281": 12.0})

        def submitted_order(**kwargs):
            return {
                "accepted": True,
                "status": "submitted",
                "source": "unit_test_gateway",
                "symbol": kwargs["symbol"],
                "side": kwargs["side"],
                "quantity": kwargs["quantity"],
                "price": kwargs["price"],
                "reason": None,
                "raw": kwargs["raw"],
            }

        with patch.object(
            self.service,
            "_trading_window_diagnostics",
            return_value={"is_market_open_now": True, "phase": "intraday"},
        ), patch.object(
            self.service,
            "_sync_cross_market_corporate_actions",
            return_value={"available": True},
        ), patch.object(
            self.service.cross_market_signal_service,
            "get_cn_open_signal",
            return_value={
                "available": True,
                "regime": "flat_open",
                "gap_pct": 0.0,
                "buy_allowed": False,
                "force_sell": False,
            },
        ), patch.object(
            self.service,
            "submit_order",
            side_effect=submitted_order,
        ):
            submitted = self.service.run_cross_market_intraday_sell_monitor()

        self.assertEqual(submitted["submitted_count"], 1)
        self.assertEqual(submitted["orders"][0]["raw"]["reason"], "trailing_stop")
        self.assertEqual(self.service._load_trailing_peaks(), {"002281": 12.0})

        self.service.portfolio.record_trade(
            account_id=account_id,
            symbol="002281",
            trade_date=date.today(),
            side="sell",
            quantity=100,
            price=11.4,
            market="cn",
            currency="CNY",
            note="vn.py paper | source=cross_market_auto_exit | price=explicit",
        )
        with patch.object(
            self.service,
            "_trading_window_diagnostics",
            return_value={"is_market_open_now": True, "phase": "intraday"},
        ), patch.object(
            self.service,
            "_sync_cross_market_corporate_actions",
            return_value={"available": True},
        ):
            cleared = self.service.run_cross_market_intraday_sell_monitor()

        self.assertTrue(cleared["skipped"])
        self.assertEqual(cleared["reason"], "no_cross_market_strategy_positions")
        self.assertEqual(self.service._load_trailing_peaks(), {})

    def test_cross_market_intraday_sell_monitor_cpo_bypasses_korea_weakness(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_execution_mode": "dry_run",
                "auto_strategy": CROSS_MARKET_STRATEGY_ID,
            }
        )
        account = self.service.ensure_account()
        self.service.portfolio.record_trade(
            account_id=int(account["id"]),
            symbol="002281",
            trade_date=date.today() - timedelta(days=1),
            side="buy",
            quantity=100,
            price=10.0,
            market="cn",
            currency="CNY",
            note="vn.py paper | source=cross_market_auto_entry | price=explicit",
        )
        self.service.data_fetcher_manager.boards_by_symbol["002281"] = [
            {"name": "CPO", "type": "concept"}
        ]

        with patch.object(
            self.service,
            "_trading_window_diagnostics",
            return_value={"is_market_open_now": True, "phase": "intraday"},
        ), patch.object(
            self.service.cross_market_signal_service,
            "get_cn_open_signal",
            return_value={
                "available": True,
                "regime": "flat_open",
                "gap_pct": 0.0,
                "buy_allowed": False,
                "force_sell": False,
            },
        ), patch.object(
            self.service.cross_market_signal_service,
            "evaluate_korea_gate",
            side_effect=AssertionError("CPO sell checks must not query the Korea gate"),
        ) as korea_gate, patch.object(
            self.service,
            "_cross_market_position_range_signal",
            return_value={},
        ):
            result = self.service.run_cross_market_intraday_sell_monitor()

        self.assertEqual(result["candidate_count"], 1)
        self.assertEqual(result["planned_count"], 0)
        self.assertEqual(result["orders"], [])
        korea_gate.assert_not_called()

    def test_cross_market_range_sell_exits_single_active_strategy_tranche(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_execution_mode": "dry_run",
                "auto_strategy": CROSS_MARKET_STRATEGY_ID,
            }
        )
        account = self.service.ensure_account()
        self.service.portfolio.record_trade(
            account_id=int(account["id"]),
            symbol="002281",
            trade_date=date.today() - timedelta(days=1),
            side="buy",
            quantity=600,
            price=10.0,
            market="cn",
            currency="CNY",
            note="vn.py paper | source=cross_market_auto_entry | price=explicit",
        )
        self.service.data_fetcher_manager.boards_by_symbol["002281"] = [
            {"name": "CPO", "type": "concept"}
        ]

        with patch.object(
            self.service,
            "_trading_window_diagnostics",
            return_value={"is_market_open_now": True, "phase": "intraday"},
        ), patch.object(
            self.service.cross_market_signal_service,
            "get_cn_open_signal",
            return_value={
                "available": True,
                "regime": "flat_open",
                "gap_pct": 0.0,
                "buy_allowed": False,
                "force_sell": False,
            },
        ), patch.object(
            self.service,
            "_cross_market_position_range_signal",
            return_value={"regime": "range", "action": "sell"},
        ):
            result = self.service.run_cross_market_intraday_sell_monitor()

        self.assertEqual(result["planned_count"], 1)
        order = result["orders"][0]
        self.assertEqual(order["quantity"], 600.0)
        self.assertEqual(order["raw"]["requested_sell_fraction"], 1.0)
        self.assertEqual(order["raw"]["reason"], "range_exit_signal")

    def test_cross_market_daily_loss_uses_previous_snapshot_equity(self) -> None:
        account = self.service.ensure_account()
        settings = replace(self.service.get_settings(), account_id=int(account["id"]))
        observed_at = datetime(2026, 7, 24, 2, 0, tzinfo=timezone.utc)
        snapshots = [
            SimpleNamespace(snapshot_date=date(2026, 7, 23), total_equity=98000.0),
            SimpleNamespace(snapshot_date=date(2026, 7, 24), total_equity=99000.0),
        ]

        with patch.object(
            self.service.portfolio.repo,
            "list_daily_snapshots_for_risk",
            return_value=snapshots,
        ) as list_snapshots:
            daily_pnl_pct = self.service._cross_market_daily_pnl_pct(
                settings=settings,
                total_equity=99000.0,
                total_market_value=10000.0,
                observed_at=observed_at,
            )

        self.assertEqual(daily_pnl_pct, 1.020408)
        list_snapshots.assert_called_once_with(
            as_of=date(2026, 7, 24),
            cost_method="fifo",
            account_id=int(account["id"]),
            lookback_days=3650,
        )

    def test_cross_market_daily_loss_fails_closed_when_snapshots_fail(self) -> None:
        account = self.service.ensure_account()
        settings = replace(self.service.get_settings(), account_id=int(account["id"]))

        with patch.object(
            self.service.portfolio.repo,
            "list_daily_snapshots_for_risk",
            side_effect=RuntimeError("snapshot unavailable"),
        ):
            daily_pnl_pct = self.service._cross_market_daily_pnl_pct(
                settings=settings,
                total_equity=97000.0,
                total_market_value=10000.0,
            )

        self.assertIsNone(daily_pnl_pct)

    def test_cross_market_expected_edge_does_not_treat_atr_as_return(self) -> None:
        edge, source = self.service._cross_market_expected_gross_edge_pct(
            candidate={"raw": {"atr_20_pct": 2.4}},
            price=10.0,
            range_signal={},
        )

        self.assertIsNone(edge)
        self.assertIsNone(source)

    def test_cross_market_expected_edge_prefers_range_target(self) -> None:
        edge, source = self.service._cross_market_expected_gross_edge_pct(
            candidate={"raw": {"atr_20_pct": 2.4}},
            price=10.0,
            range_signal={
                "regime": "range",
                "action": "buy",
                "bollinger_upper": 10.6,
            },
        )

        self.assertAlmostEqual(edge or 0.0, 6.0)
        self.assertEqual(source, "range_bollinger_upper")

    def test_cross_market_expected_edge_fails_closed_without_evidence(self) -> None:
        edge, source = self.service._cross_market_expected_gross_edge_pct(
            candidate={"score": 90.0},
            price=10.0,
            range_signal={},
        )

        self.assertIsNone(edge)
        self.assertIsNone(source)

    def test_cross_market_expected_edge_does_not_fetch_atr_as_return_fallback(self) -> None:
        with patch.object(
            self.service,
            "_cross_market_completed_atr_20_pct",
            return_value=2.1,
        ) as atr:
            edge, source = self.service._cross_market_expected_gross_edge_pct(
                candidate={"score": 90.0},
                price=10.0,
                range_signal={},
                symbol="002281",
            )

        self.assertIsNone(edge)
        self.assertIsNone(source)
        atr.assert_not_called()

    def test_cross_market_corporate_actions_sync_once_and_adjust_strategy_lots(self) -> None:
        self.service.update_settings({"auto_strategy": CROSS_MARKET_STRATEGY_ID})
        settings = self.service._apply_cross_market_strategy_settings(
            self.service.get_settings()
        )
        account = self.service.ensure_account(settings=settings)
        self.service.portfolio.record_trade(
            account_id=int(account["id"]),
            symbol="600519",
            trade_date=date.today() - timedelta(days=1),
            side="buy",
            quantity=100,
            price=10.0,
            market="cn",
            currency="CNY",
            note="vn.py paper | source=cross_market_auto_entry | price=explicit",
        )
        fetch_actions = MagicMock(return_value=[
            {
                "symbol": "600519",
                "effective_date": date.today(),
                "action_type": "cash_dividend",
                "cash_dividend_per_share": 1.0,
                "source": "unit-test",
                "source_record_key": "600519|cash",
            },
            {
                "symbol": "600519",
                "effective_date": date.today(),
                "action_type": "split_adjustment",
                "split_ratio": 2.0,
                "source": "unit-test",
                "source_record_key": "600519|split",
            },
        ])
        self.service.data_fetcher_manager._fetchers = [
            SimpleNamespace(get_stock_corporate_actions=fetch_actions)
        ]

        first = self.service._sync_cross_market_corporate_actions(
            settings=settings,
            as_of=date.today(),
        )
        second = self.service._sync_cross_market_corporate_actions(
            settings=settings,
            as_of=date.today(),
        )
        events = self.service.portfolio.list_corporate_action_events(
            account_id=int(account["id"]),
            symbol="600519",
            page=1,
            page_size=20,
        )
        strategy_position = self.service._cross_market_strategy_position(
            account_id=int(account["id"]),
            symbol="600519",
            market="cn",
            as_of=date.today(),
        )

        self.assertTrue(first["available"])
        self.assertEqual(first["inserted_count"], 2)
        self.assertTrue(second["available"])
        self.assertEqual(second["reason"], "corporate_actions_already_synchronized")
        self.assertEqual(events["total"], 2)
        self.assertEqual(strategy_position["quantity"], 200.0)
        self.assertFalse(strategy_position["flat_open_staged_entry"])
        self.assertEqual(fetch_actions.call_count, 1)

    def test_cross_market_strategy_position_retains_flat_open_stage_reason(self) -> None:
        self.service.update_settings({"auto_strategy": CROSS_MARKET_STRATEGY_ID})
        settings = self.service._apply_cross_market_strategy_settings(
            self.service.get_settings()
        )
        account = self.service.ensure_account(settings=settings)
        self.service.portfolio.record_trade(
            account_id=int(account["id"]),
            symbol="600519",
            trade_date=date.today(),
            side="buy",
            quantity=100,
            price=10.0,
            market="cn",
            currency="CNY",
            note=(
                "vn.py paper | source=cross_market_auto_entry | "
                "entry_reason=memory_flat_open_staged_entry_confirmed"
            ),
        )

        strategy_position = self.service._cross_market_strategy_position(
            account_id=int(account["id"]),
            symbol="600519",
            market="cn",
            as_of=date.today(),
        )

        self.assertTrue(strategy_position["flat_open_staged_entry"])
        self.assertEqual(
            strategy_position["entry_reasons"],
            ["memory_flat_open_staged_entry_confirmed"],
        )

    def test_cross_market_next_day_high_open_only_sells_eligible_lots(self) -> None:
        account = self.service.ensure_account()
        account_id = int(account["id"])
        current_date = date.today()
        older_entry = current_date - timedelta(days=2)
        recent_entry = current_date - timedelta(days=1)
        for trade_date, trade_uid in (
            (older_entry, "older-entry"),
            (recent_entry, "recent-entry"),
        ):
            self.service.portfolio.record_trade(
                account_id=account_id,
                symbol="002281",
                trade_date=trade_date,
                side="buy",
                quantity=100,
                price=10.0,
                market="cn",
                currency="CNY",
                trade_uid=trade_uid,
                note=(
                    "vn.py paper | source=cross_market_auto_entry | "
                    "entry_reason=cpo_us_close_opening_entry_confirmed; entry_theme=cpo"
                ),
            )

        def next_session(entry_date):
            resolved = current_date if entry_date == recent_entry else recent_entry
            return resolved, {
                "available": True,
                "next_session_date": resolved.isoformat(),
                "reason": None,
            }

        with patch.object(
            self.service,
            "_cross_market_next_cn_session_date",
            side_effect=next_session,
        ):
            strategy_position = self.service._cross_market_strategy_position(
                account_id=account_id,
                symbol="002281",
                market="cn",
                as_of=current_date,
            )

        quote = SimpleNamespace(
            price=10.3,
            open_price=10.2,
            pre_close=10.0,
            high=10.4,
        )
        cn_signal = {"force_sell": True, "gap_pct": 0.8}
        signal = self.service._cross_market_next_day_high_open_exit_signal(
            quote=quote,
            cn_signal=cn_signal,
            strategy_position=strategy_position,
        )
        expired_signal = self.service._cross_market_next_day_high_open_exit_signal(
            quote=quote,
            cn_signal=cn_signal,
            strategy_position={
                **strategy_position,
                "next_session_exit_quantity": 0.0,
            },
        )

        self.assertEqual(strategy_position["quantity"], 200.0)
        self.assertEqual(strategy_position["next_session_exit_quantity"], 100.0)
        self.assertEqual(signal["action"], "sell")
        self.assertEqual(signal["sell_fraction"], 0.5)
        self.assertEqual(expired_signal["action"], "hold")
        self.assertEqual(
            expired_signal["reason"],
            "position_not_in_next_session_exit_window",
        )

    def test_cross_market_next_cn_session_skips_weekend(self) -> None:
        next_session, evidence = self.service._cross_market_next_cn_session_date(
            date(2026, 8, 7)
        )

        self.assertTrue(evidence["available"])
        self.assertEqual(next_session, date(2026, 8, 10))

    def test_cross_market_corporate_action_sync_failure_is_fail_closed(self) -> None:
        self.service.update_settings({"auto_strategy": CROSS_MARKET_STRATEGY_ID})
        settings = self.service._apply_cross_market_strategy_settings(
            self.service.get_settings()
        )
        account = self.service.ensure_account(settings=settings)
        self.service.portfolio.record_trade(
            account_id=int(account["id"]),
            symbol="600519",
            trade_date=date.today() - timedelta(days=1),
            side="buy",
            quantity=100,
            price=10.0,
            market="cn",
            currency="CNY",
            note="vn.py paper | source=cross_market_auto_entry | price=explicit",
        )
        self.service.data_fetcher_manager._fetchers = [
            SimpleNamespace(
                get_stock_corporate_actions=MagicMock(
                    side_effect=RuntimeError("provider unavailable")
                )
            )
        ]

        result = self.service._sync_cross_market_corporate_actions(
            settings=settings,
            as_of=date.today(),
        )

        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "corporate_action_sync_incomplete")
        self.assertEqual(result["failures"][0]["symbol"], "600519")

    def test_cross_market_intraday_sell_monitor_fails_closed_without_corporate_actions(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_execution_mode": "dry_run",
                "auto_strategy": CROSS_MARKET_STRATEGY_ID,
            }
        )

        with patch.object(
            self.service,
            "_trading_window_diagnostics",
            return_value={"is_market_open_now": True, "phase": "intraday"},
        ), patch.object(
            self.service,
            "_sync_cross_market_corporate_actions",
            return_value={"available": False, "reason": "provider_failed"},
        ), patch.object(self.service.agent_repo, "create_run") as create_run:
            result = self.service.run_cross_market_intraday_sell_monitor()

        self.assertTrue(result["skipped"])
        self.assertEqual(
            result["reason"],
            "cross_market_corporate_action_evidence_unavailable",
        )
        create_run.assert_not_called()

    def test_cross_market_intraday_sell_monitor_ignores_manual_position_without_run(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_execution_mode": "dry_run",
                "auto_strategy": CROSS_MARKET_STRATEGY_ID,
            }
        )
        account = self.service.ensure_account()
        self.service.portfolio.record_trade(
            account_id=int(account["id"]),
            symbol="002281",
            trade_date=date.today() - timedelta(days=1),
            side="buy",
            quantity=100,
            price=10.0,
            market="cn",
            currency="CNY",
            note="manual position",
        )

        with patch.object(
            self.service,
            "_trading_window_diagnostics",
            return_value={"is_market_open_now": True, "phase": "intraday"},
        ), patch.object(self.service.agent_repo, "create_run") as create_run:
            result = self.service.run_cross_market_intraday_sell_monitor()

        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "no_cross_market_strategy_positions")
        create_run.assert_not_called()

    def test_cross_market_intraday_sell_monitor_uses_confirmed_korea_weakness(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_execution_mode": "dry_run",
                "auto_strategy": CROSS_MARKET_STRATEGY_ID,
            }
        )
        account = self.service.ensure_account()
        self.service.portfolio.record_trade(
            account_id=int(account["id"]),
            symbol="688981",
            trade_date=date.today() - timedelta(days=1),
            side="buy",
            quantity=400,
            price=10.0,
            market="cn",
            currency="CNY",
            note="vn.py paper | source=cross_market_auto_entry | price=explicit",
        )
        self.service.data_fetcher_manager.boards_by_symbol["688981"] = [
            {"name": "半导体", "type": "industry"}
        ]

        with patch.object(
            self.service,
            "_trading_window_diagnostics",
            return_value={"is_market_open_now": True, "phase": "intraday"},
        ), patch.object(
            self.service.cross_market_signal_service,
            "get_cn_open_signal",
            return_value={
                "available": True,
                "regime": "flat_open",
                "gap_pct": 0.0,
                "buy_allowed": False,
                "force_sell": False,
            },
        ), patch.object(
            self.service.cross_market_signal_service,
            "evaluate_korea_gate",
            return_value={
                "status": "weak",
                "reason": "korea_moderate_weakness_confirmed",
                "buy_allowed": False,
                "sell_fraction": 0.5,
                "confirmed": True,
            },
        ) as korea_gate:
            result = self.service.run_cross_market_intraday_sell_monitor()

        self.assertEqual(result["planned_count"], 1)
        self.assertEqual(result["orders"][0]["quantity"], 200.0)
        self.assertEqual(
            result["orders"][0]["raw"]["reason"],
            "korea_moderate_weakness_confirmed",
        )
        korea_gate.assert_called_once_with(
            theme="semiconductor",
            refresh=True,
        )

    def test_cross_market_local_fill_books_commission_transfer_fee_and_tax(self) -> None:
        buy = self.service.submit_order(
            symbol="600519",
            side="buy",
            market="cn",
            quantity=100,
            price=10.0,
            source="cross_market_auto_entry",
            raw={"cross_market_strategy": {"theme": "semiconductor"}},
        )

        self.assertTrue(buy["accepted"])
        self.assertEqual(buy["fee"], 5.01)
        self.assertEqual(buy["tax"], 0.0)
        self.assertEqual(buy["net_cash_change"], -1005.01)
        self.assertEqual(buy["raw"]["execution_costs"]["schedule"], "cross_market_cn_v1")

        account_id = int(buy["account_id"])
        sell = self.service.submit_order(
            symbol="600519",
            side="sell",
            market="cn",
            quantity=100,
            price=11.0,
            source="cross_market_auto_exit",
            raw={"cross_market_strategy": {"theme": "semiconductor"}},
        )
        self.assertTrue(sell["accepted"])
        self.assertEqual(sell["fee"], 5.011)
        self.assertEqual(sell["tax"], 0.55)
        trades = self.service.portfolio.repo.list_trades(account_id, as_of=date.today())
        self.assertEqual(float(trades[-1].fee), 5.011)
        self.assertEqual(float(trades[-1].tax), 0.55)

        gold_stock = self.service.submit_order(
            symbol="600547",
            side="buy",
            market="cn",
            quantity=100,
            price=10.0,
            source="cross_market_auto_entry",
            raw={"cross_market_strategy": {"theme": "gold"}},
        )
        gold_etf = self.service.submit_order(
            symbol="518880",
            side="buy",
            market="cn",
            quantity=100,
            price=10.0,
            source="cross_market_auto_entry",
            raw={"cross_market_strategy": {"theme": "gold"}},
        )
        self.assertEqual(gold_stock["raw"]["execution_costs"]["instrument_type"], "stock")
        self.assertEqual(gold_stock["fee"], 5.01)
        self.assertEqual(gold_etf["raw"]["execution_costs"]["instrument_type"], "etf")
        self.assertEqual(gold_etf["fee"], 5.0)

    def test_cross_market_manual_plan_revalidates_signals_before_submission(self) -> None:
        self.service.data_fetcher_manager.boards_by_symbol["002281"] = [
            {"name": "CPO 光模块", "type": "concept", "change_pct": 1.2}
        ]
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_execution_mode": "manual_approval",
                "auto_strategy": CROSS_MARKET_STRATEGY_ID,
                "auto_max_results": 1,
                "auto_cash_per_order": 1200,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "candidates": [{
                "code": "002281",
                "score": 75,
                "price": 10.0,
                "expected_return_pct": 4.0,
                "is_core_stock": True,
                "source": "dsa_eastmoney_board_change_leader",
                "_cross_market_source_theme": "cpo",
            }],
            "warnings": [],
        }
        low_open = {
            "available": True,
            "regime": "low_open",
            "gap_pct": -0.6,
            "buy_allowed": True,
            "force_sell": False,
        }
        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ), patch.object(
            self.service.cross_market_signal_service,
            "get_cn_open_signal",
            return_value=low_open,
        ):
            planned = self.service.run_auto_trade_once()
        detail = self.service.agent_repo.get_run_detail(planned["agent_run_uid"])
        plan_uid = detail["trade_plans"][0]["plan_uid"]

        high_open = {**low_open, "regime": "high_open", "gap_pct": 0.6, "buy_allowed": False, "force_sell": True}
        with patch.object(
            self.service.cross_market_signal_service,
            "get_cn_open_signal",
            return_value=high_open,
        ):
            result = self.service.approve_trade_plan(plan_uid)

        self.assertFalse(result["accepted"])
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "cross_market_plan_cn_high_open_buy_blocked")
        self.assertEqual(
            result["raw"]["cross_market_revalidation"]["decision"]["action"],
            "blocked",
        )

    def test_cross_market_manual_sell_plan_revalidates_active_hard_stop(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_execution_mode": "manual_approval",
                "auto_strategy": CROSS_MARKET_STRATEGY_ID,
                "auto_max_results": 1,
            }
        )
        account = self.service.ensure_account()
        self.service.portfolio.record_trade(
            account_id=int(account["id"]),
            symbol="002281",
            trade_date=date.today() - timedelta(days=1),
            side="buy",
            quantity=100,
            price=10.0,
            market="cn",
            currency="CNY",
            note="vn.py paper | source=cross_market_auto_entry | price=explicit",
        )
        self.service.data_fetcher_manager.price = 9.4
        self.service.data_fetcher_manager.boards_by_symbol["002281"] = [
            {"name": "CPO", "type": "concept"}
        ]
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {"candidates": [], "warnings": []}
        flat_open = {
            "available": True,
            "regime": "flat",
            "gap_pct": 0.0,
            "buy_allowed": False,
            "force_sell": False,
        }

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ), patch.object(
            self.service.cross_market_signal_service,
            "get_cn_open_signal",
            return_value=flat_open,
        ):
            planned = self.service.run_auto_trade_once()
        detail = self.service.agent_repo.get_run_detail(planned["agent_run_uid"])
        sell_plan = next(item for item in detail["trade_plans"] if item["side"] == "sell")
        self.service.data_fetcher_manager.price = 9.3

        with patch.object(
            self.service.cross_market_signal_service,
            "get_cn_open_signal",
            return_value=flat_open,
        ):
            result = self.service.approve_trade_plan(sell_plan["plan_uid"])

        self.assertTrue(result["accepted"])
        self.assertEqual(result["quantity"], 100.0)
        self.assertEqual(result["price"], 9.3)
        self.assertEqual(
            result["raw"]["cross_market_revalidation"]["sell_reason"],
            "hard_stop_loss",
        )

    def test_cross_market_pending_sell_revalidation_prefers_strict_quote(self) -> None:
        account = self.service.ensure_account()
        self.service.portfolio.record_trade(
            account_id=int(account["id"]),
            symbol="002281",
            trade_date=date.today() - timedelta(days=1),
            side="buy",
            quantity=100,
            price=10.0,
            market="cn",
            currency="CNY",
            note="vn.py paper | source=cross_market_auto_entry | price=explicit",
        )
        strict_quote = SimpleNamespace(
            price=9.4,
            provider_timestamp=datetime.now(timezone.utc).isoformat(),
            amount=9400.0,
            volume=1000.0,
        )
        flat_open = {
            "available": True,
            "regime": "flat",
            "gap_pct": 0.0,
            "buy_allowed": False,
            "force_sell": False,
        }

        with patch.object(
            self.service,
            "_sync_cross_market_corporate_actions",
            return_value={"available": True},
        ), patch.object(
            self.service,
            "_account_drawdown_diagnostics",
            return_value={"status": "blocked", "drawdown_pct": 8.0},
        ), patch.object(
            self.service.data_fetcher_manager,
            "get_realtime_quote_with_provider_timestamp",
            return_value=strict_quote,
            create=True,
        ) as strict_getter, patch.object(
            self.service.data_fetcher_manager,
            "get_realtime_quote",
            side_effect=AssertionError(
                "generic quote route must not be used for pending cross-market exits"
            ),
        ), patch.object(
            self.service.cross_market_signal_service,
            "get_cn_open_signal",
            return_value=flat_open,
        ):
            reason, quantity, evidence = self.service._revalidate_cross_market_trade_plan(
                plan={
                    "symbol": "002281",
                    "side": "sell",
                    "market": "cn",
                    "planned_quantity": 100,
                },
                raw={"cross_market_strategy": {"theme": "cpo"}},
                settings=self.service.get_settings(),
            )

        self.assertIsNone(reason)
        self.assertEqual(quantity, 100.0)
        self.assertEqual(evidence["sell_reason"], "hard_stop_loss")
        self.assertEqual(evidence["quote"]["provider_timestamp"], strict_quote.provider_timestamp)
        strict_getter.assert_called_once_with("002281")

    def test_cross_market_pending_sell_quote_uses_request_completion_time(self) -> None:
        started_at = datetime(2026, 7, 27, 6, 27, 30, tzinfo=timezone.utc)
        quote = SimpleNamespace(
            price=9.4,
            provider_timestamp=(started_at + timedelta(seconds=2)).isoformat(),
            amount=9400.0,
            volume=1000.0,
        )
        strategy_position = {
            "status": "available",
            "quantity": 100.0,
            "sellable_quantity": 100.0,
            "gross_cost_basis": 1000.0,
            "entry_theme": "cpo",
            "entry_themes": ["cpo"],
            "entry_reasons": ["cpo_us_close_opening_entry_confirmed"],
            "open_entry_order_count": 1,
        }
        snapshot = {
            "accounts": [{
                "positions": [{
                    "symbol": "002281",
                    "market": "cn",
                    "currency": "CNY",
                    "quantity": 100.0,
                    "avg_cost": 10.0,
                }]
            }]
        }
        settings = self.service._apply_cross_market_strategy_settings(
            replace(
                self.service.get_settings(),
                auto_strategy=CROSS_MARKET_STRATEGY_ID,
            )
        )
        account = self.service.ensure_account(settings=settings)
        settings = replace(settings, account_id=int(account["id"]))

        _SequenceDateTime.values = [
            started_at,
            started_at + timedelta(seconds=3),
        ]
        with patch(
            "src.services.vnpy_paper_trading_service.datetime",
            new=_SequenceDateTime,
        ), patch.object(
            self.service,
            "_sync_cross_market_corporate_actions",
            return_value={"available": True},
        ), patch.object(
            self.service.cross_market_signal_service,
            "get_cn_open_signal",
            return_value={"available": True, "gap_pct": 0.0, "force_sell": False},
        ), patch.object(
            self.service.portfolio,
            "get_portfolio_snapshot",
            return_value=snapshot,
        ), patch.object(
            self.service,
            "_cross_market_strategy_position",
            return_value=strategy_position,
        ), patch.object(
            self.service,
            "_get_cross_market_realtime_quote",
            return_value=quote,
        ), patch.object(
            self.service,
            "_account_drawdown_diagnostics",
            return_value={"status": "ok"},
        ), patch.object(
            self.service.portfolio,
            "get_sellable_quantity",
            return_value=100.0,
        ):
            reason, quantity, evidence = self.service._revalidate_cross_market_trade_plan(
                plan={
                    "symbol": "002281",
                    "side": "sell",
                    "market": "cn",
                    "planned_quantity": 100.0,
                },
                raw={"cross_market_strategy": {"theme": "cpo"}},
                settings=settings,
            )

        self.assertIsNone(reason)
        self.assertEqual(quantity, 100.0)
        self.assertEqual(evidence["quote"]["age_seconds"], 1.0)
        self.assertEqual(
            evidence["quote"]["validated_at"],
            (started_at + timedelta(seconds=3)).isoformat(),
        )

    def test_cross_market_manual_sell_plan_expires_after_hard_stop_recovers(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_execution_mode": "manual_approval",
                "auto_strategy": CROSS_MARKET_STRATEGY_ID,
                "auto_max_results": 1,
            }
        )
        account = self.service.ensure_account()
        self.service.portfolio.record_trade(
            account_id=int(account["id"]),
            symbol="002281",
            trade_date=date.today() - timedelta(days=1),
            side="buy",
            quantity=100,
            price=10.0,
            market="cn",
            currency="CNY",
            note="vn.py paper | source=cross_market_auto_entry | price=explicit",
        )
        self.service.data_fetcher_manager.price = 9.4
        self.service.data_fetcher_manager.boards_by_symbol["002281"] = [
            {"name": "CPO", "type": "concept"}
        ]
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {"candidates": [], "warnings": []}
        flat_open = {
            "available": True,
            "regime": "flat",
            "gap_pct": 0.0,
            "buy_allowed": False,
            "force_sell": False,
        }

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ), patch.object(
            self.service.cross_market_signal_service,
            "get_cn_open_signal",
            return_value=flat_open,
        ):
            planned = self.service.run_auto_trade_once()
        detail = self.service.agent_repo.get_run_detail(planned["agent_run_uid"])
        sell_plan = next(item for item in detail["trade_plans"] if item["side"] == "sell")
        self.service.data_fetcher_manager.price = 10.0

        with patch.object(
            self.service.cross_market_signal_service,
            "get_cn_open_signal",
            return_value=flat_open,
        ):
            result = self.service.approve_trade_plan(sell_plan["plan_uid"])

        self.assertFalse(result["accepted"])
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "cross_market_plan_sell_signal_expired")

    def test_auto_trade_cn_selects_affordable_candidate_from_execution_pool(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_execution_mode": "paper",
                "auto_market": "cn",
                "auto_strategy": "dual_low",
                "auto_cash_per_order": 1000,
                "auto_max_results": 1,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "quality_status": "ok",
            "candidates": [
                {
                    "rank": 1,
                    "code": "000001",
                    "score": 81.66,
                    "price": 11.08,
                    "data_quality": "ok",
                },
                {
                    "rank": 2,
                    "code": "600016",
                    "score": 81.10,
                    "price": 3.53,
                    "data_quality": "ok",
                },
            ],
            "warnings": [],
            "source_errors": [],
        }

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ):
            result = self.service.run_auto_trade_once()

        self.assertEqual(fake_alphasift.screen.call_args.kwargs["max_results"], 5)
        self.assertEqual(result["candidate_count"], 1)
        self.assertEqual(result["submitted_count"], 1)
        self.assertEqual(result["skipped_count"], 0)
        self.assertEqual(result["orders"][0]["symbol"], "600016")
        self.assertEqual(result["orders"][0]["quantity"], 200.0)
        audit = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        assert audit is not None
        selection = audit["diagnostics"]["execution_candidate_selection"]
        self.assertTrue(selection["applied"])
        self.assertEqual(selection["screened_count"], 2)
        self.assertEqual(selection["selected_count"], 1)
        self.assertEqual(selection["excluded_count"], 1)
        self.assertEqual(selection["excluded"][0]["symbol"], "000001")
        self.assertEqual(selection["excluded"][0]["reason"], "cash_below_min_lot")
        self.assertEqual(audit["decisions"][0]["symbol"], "600016")

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
        ), patch.object(self.service.portfolio, "refresh_fx_pair", return_value={"available": False}):
            result = self.service.run_auto_trade_once()

        self.assertEqual(result["orders"][0]["reason"], "fx_rate_unavailable")
        self.assertEqual(result["orders"][0]["base_currency"], "CNY")
        self.assertEqual(result["orders"][0]["quote_currency"], "HKD")
        detail = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        self.assertFalse(detail["diagnostics"]["currency_budget"]["available"])

    def test_auto_order_budget_refreshes_fx_pair_before_first_us_trade(self) -> None:
        self.service.update_settings({"auto_market": "us", "auto_cash_per_order": 1000})

        def refresh_pair(**kwargs):
            self.service.portfolio.repo.save_fx_rate(
                from_currency=kwargs["from_currency"],
                to_currency=kwargs["to_currency"],
                rate_date=kwargs["as_of"],
                rate=0.14,
                source="unit-test",
            )
            return {"available": True}

        with patch.object(self.service.portfolio, "refresh_fx_pair", side_effect=refresh_pair) as refresh_mock:
            budget = self.service._auto_order_currency_budget(self.service.get_settings())

        self.assertTrue(budget["available"])
        self.assertEqual(budget["quote_currency"], "USD")
        self.assertAlmostEqual(budget["quote_cash_amount"], 140.0)
        refresh_mock.assert_called_once()

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

    def test_daily_auto_usage_counts_cross_market_entries(self) -> None:
        existing = self.service.submit_order(
            symbol="002281",
            side="buy",
            market="cn",
            quantity=100,
            price=10.0,
            source="cross_market_auto_entry",
        )
        self.assertTrue(existing["accepted"])
        self.service.update_settings(
            {
                "auto_daily_max_orders": 3,
                "auto_daily_budget": 5000,
            }
        )

        usage = self.service._daily_auto_trade_usage(self.service.get_settings())

        self.assertEqual(usage["order_count"], 1.0)
        self.assertEqual(usage["cash_amount"], 1000.0)

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
        self.assertEqual(
            performance["run_window"]["source"],
            "current_account_created_at",
        )
        self.assertIsNotNone(performance["run_window"]["created_from"])
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
        self.assertEqual(future_window["run_window"]["source"], "request")
        self.assertEqual(future_window["agent"]["candidate_count"], 0)
        self.assertEqual(future_window["agent"]["submitted_count"], 0)
        self.assertEqual(future_window["strategy_attribution"], [])
        self.assertEqual(future_window["industry_attribution"], [])

    def test_performance_summary_scopes_agent_runs_to_current_account_by_default(self) -> None:
        self.service.ensure_account()
        status = self.service.get_status(include_snapshot=False, include_recent_trades=False)
        account_created_at = self.service._parse_db_datetime(
            status["account"]["created_at"]
        )

        with patch.object(
            self.service.agent_repo,
            "list_runs",
            return_value={"items": [], "total": 0},
        ) as list_runs:
            performance = self.service.get_performance_summary(run_limit=10)

        self.assertEqual(
            list_runs.call_args.kwargs["created_from"],
            account_created_at - timedelta(seconds=1),
        )
        self.assertEqual(
            performance["diagnostics"]["run_window_source"],
            "current_account_created_at",
        )

    def test_performance_summary_preserves_explicit_agent_run_window(self) -> None:
        requested_start = datetime(2026, 7, 27, 1, 30, tzinfo=timezone.utc)

        with patch.object(
            self.service.agent_repo,
            "list_runs",
            return_value={"items": [], "total": 0},
        ) as list_runs:
            performance = self.service.get_performance_summary(
                run_limit=10,
                created_from=requested_start,
            )

        self.assertEqual(list_runs.call_args.kwargs["created_from"], requested_start)
        self.assertEqual(performance["run_window"]["source"], "request")
        self.assertEqual(
            performance["run_window"]["created_from"],
            requested_start.isoformat(),
        )

    def test_performance_summary_excludes_calibration_shadow_runs(self) -> None:
        self.service.ensure_account()
        formal = self.service.agent_repo.create_run(
            run_uid="performance-formal-run",
            trigger_source="vnpy_paper_auto",
            strategy="cross_market_semiconductor_gold_v1.1",
            market="cn",
        )
        shadow = self.service.agent_repo.create_run(
            run_uid="performance-shadow-run",
            trigger_source="agent_calibration_shadow",
            strategy="dual_low",
            market="cn",
        )
        for run in (formal, shadow):
            self.service.agent_repo.complete_run(
                run_id=run["id"],
                status="completed",
                candidate_count=1,
                submitted_count=0,
                skipped_count=1,
            )

        performance = self.service.get_performance_summary(run_limit=10)

        self.assertEqual(performance["run_window"]["run_count"], 1)
        self.assertEqual(
            performance["run_window"]["excluded_trigger_sources"],
            ["agent_calibration_shadow"],
        )
        self.assertEqual(
            [item["key"] for item in performance["strategy_attribution"]],
            ["cross_market_semiconductor_gold_v1.1"],
        )

    def test_performance_summary_reports_net_costs_and_profit_factor(self) -> None:
        raw = {"instrument_type": "stock"}
        self.service.submit_order(
            symbol="600519",
            side="buy",
            market="cn",
            quantity=100,
            price=10.0,
            source="cross_market_auto_entry",
            raw=raw,
        )
        self.service.submit_order(
            symbol="600519",
            side="sell",
            market="cn",
            quantity=50,
            price=12.0,
            source="cross_market_auto_exit",
            raw=raw,
        )
        self.service.submit_order(
            symbol="600519",
            side="sell",
            market="cn",
            quantity=50,
            price=9.0,
            source="cross_market_auto_exit",
            raw=raw,
        )

        metrics = self.service.get_performance_summary()["trade_metrics"]

        self.assertEqual(metrics["trade_count"], 3)
        self.assertAlmostEqual(metrics["total_fee"], 15.0205, places=6)
        self.assertAlmostEqual(metrics["total_tax"], 0.525, places=6)
        self.assertAlmostEqual(metrics["total_transaction_cost"], 15.5455, places=6)
        self.assertAlmostEqual(metrics["winning_trade_pnl"], 92.189, places=6)
        self.assertAlmostEqual(metrics["losing_trade_pnl"], -57.7345, places=6)
        self.assertAlmostEqual(metrics["realized_trade_pnl"], 34.4545, places=6)
        self.assertAlmostEqual(metrics["profit_factor"], 1.596775, places=6)
        self.assertEqual(metrics["cost_basis"], {
            "fees_and_taxes_in_realized_pnl": True,
            "slippage_in_fill_price": True,
            "slippage_method": "next_minute_vwap_dynamic_price_impact",
        })

    def test_completed_campaign_blocks_auto_trade_before_candidate_screening(self) -> None:
        self.service.update_settings({
            "auto_trade_enabled": True,
            "auto_execution_mode": "vnpy_paper",
            "auto_strategy": CROSS_MARKET_STRATEGY_ID,
            "auto_market": "cn",
        })
        guard = {
            "block": True,
            "reason": "paper_campaign_completed",
            "completion_session_date": "2026-09-07",
            "current_session_date": "2026-09-08",
        }

        with patch.object(
            self.service,
            "_cross_market_campaign_execution_guard",
            return_value=guard,
        ), patch.object(
            self.service,
            "_recent_agent_run_context",
        ) as recent_context:
            result = self.service.run_auto_trade_once()

        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "paper_campaign_completed")
        self.assertEqual(result["candidate_count"], 0)
        self.assertEqual(result["submitted_count"], 0)
        self.assertEqual(result["campaign_guard"], guard)
        recent_context.assert_not_called()

    def test_duplicate_formal_run_is_blocked_before_candidate_screening(self) -> None:
        self.service.update_settings({
            "auto_trade_enabled": True,
            "auto_execution_mode": "vnpy_paper",
            "auto_strategy": CROSS_MARKET_STRATEGY_ID,
            "auto_market": "cn",
        })
        cadence_guard = {
            "block": True,
            "reason": "formal_execution_already_fully_evidenced_today",
            "formal_evidence": {"run_uid": "formal-ready-run"},
        }

        with patch.object(
            self.service,
            "_cross_market_campaign_execution_guard",
            return_value={"block": False, "reason": "paper_campaign_in_progress"},
        ), patch.object(
            self.service,
            "_cross_market_formal_run_cadence_guard",
            return_value=cadence_guard,
        ), patch.object(
            self.service,
            "_recent_agent_run_context",
        ) as recent_context:
            result = self.service.run_auto_trade_once()

        self.assertTrue(result["skipped"])
        self.assertEqual(
            result["reason"],
            "formal_execution_already_fully_evidenced_today",
        )
        self.assertEqual(result["formal_cadence_guard"], cadence_guard)
        self.assertEqual(result["submitted_count"], 0)
        recent_context.assert_not_called()

    def test_formal_cadence_guard_only_allows_zero_activity_recovery(self) -> None:
        settings = VnpyPaperSettings(
            enabled=True,
            account_id=9,
            auto_trade_enabled=True,
            auto_strategy=CROSS_MARKET_STRATEGY_ID,
            auto_execution_mode="vnpy_paper",
        )
        window = {
            "is_market_open_now": True,
            "session_date": "2026-07-28",
        }
        degraded = {
            "ready": False,
            "reason": "fully_evidenced_formal_run_not_found",
            "run_observed": True,
            "run_uid": "formal-degraded-run",
            "planned_count": 0,
            "submitted_count": 0,
        }
        with patch.object(
            self.service,
            "_trading_window_diagnostics",
            return_value=window,
        ), patch(
            "src.services.vnpy_paper_trading_service._cross_market_daily_evidence_status",
            return_value=degraded,
        ), patch.object(
            self.service,
            "_cross_market_observation_snapshot",
            return_value={"status": "ready", "missing_requirements": []},
        ), patch.object(
            self.service.agent_repo,
            "list_runs",
            return_value={
                "items": [{
                    "run_uid": "formal-degraded-run",
                    "status": "completed",
                    "planned_count": 0,
                    "submitted_count": 0,
                    "settings": {
                        "account_id": 9,
                        "auto_execution_mode": "vnpy_paper",
                    },
                }],
            },
        ), patch.object(
            self.service.agent_repo,
            "get_run_detail",
            return_value={"decisions": []},
        ):
            normal = self.service._cross_market_formal_run_cadence_guard(
                settings,
                trigger_source="vnpy_paper_auto",
            )
            eligible = self.service._cross_market_formal_run_cadence_guard(
                settings,
                trigger_source="vnpy_paper_auto",
                allow_intraday_entry_recheck=True,
            )

        self.assertTrue(normal["block"])
        self.assertEqual(normal["reason"], "formal_execution_already_observed_today")
        self.assertFalse(eligible["block"])
        self.assertTrue(eligible["formal_recovery"])
        self.assertTrue(eligible["evidence_level_recovery"])
        self.assertEqual(eligible["reason"], "formal_intraday_entry_recheck_eligible")

        with patch.object(
            self.service,
            "_trading_window_diagnostics",
            return_value=window,
        ), patch(
            "src.services.vnpy_paper_trading_service._cross_market_daily_evidence_status",
            return_value={**degraded, "planned_count": 1},
        ), patch.object(
            self.service,
            "_cross_market_observation_snapshot",
        ) as current_evidence:
            blocked = self.service._cross_market_formal_run_cadence_guard(
                settings,
                trigger_source="vnpy_paper_auto",
                allow_intraday_entry_recheck=True,
            )

        self.assertTrue(blocked["block"])
        self.assertEqual(
            blocked["reason"],
            "formal_execution_activity_blocks_recovery",
        )
        current_evidence.assert_not_called()

    def test_formal_cadence_requires_opening_slot_or_existing_baseline(self) -> None:
        settings = VnpyPaperSettings(
            enabled=True,
            account_id=9,
            auto_trade_enabled=True,
            auto_strategy=CROSS_MARKET_STRATEGY_ID,
            auto_execution_mode="vnpy_paper",
        )
        window = {
            "is_market_open_now": True,
            "session_date": "2026-08-03",
        }
        no_formal = {
            "ready": False,
            "reason": "fully_evidenced_formal_run_not_found",
            "run_observed": False,
        }
        with patch.object(
            self.service,
            "_trading_window_diagnostics",
            return_value=window,
        ), patch(
            "src.services.vnpy_paper_trading_service._cross_market_daily_evidence_status",
            return_value=no_formal,
        ), patch(
            "src.services.vnpy_paper_trading_service._cross_market_formal_entry_slot",
            return_value=None,
        ) as formal_slot:
            late_initial = self.service._cross_market_formal_run_cadence_guard(
                settings,
                trigger_source="vnpy_paper_auto",
            )
            orphan_recovery = self.service._cross_market_formal_run_cadence_guard(
                settings,
                trigger_source="vnpy_paper_auto",
                allow_intraday_entry_recheck=True,
            )

        self.assertTrue(late_initial["block"])
        self.assertEqual(
            late_initial["reason"],
            "outside_cross_market_formal_entry_slot",
        )
        self.assertTrue(orphan_recovery["block"])
        self.assertEqual(
            orphan_recovery["reason"],
            "formal_recovery_requires_opening_baseline",
        )
        formal_slot.assert_called_once_with()

    def test_formal_cadence_guard_allows_transient_intraday_entry_recheck(self) -> None:
        settings = VnpyPaperSettings(
            enabled=True,
            account_id=9,
            auto_trade_enabled=True,
            auto_strategy=CROSS_MARKET_STRATEGY_ID,
            auto_execution_mode="vnpy_paper",
        )
        window = {
            "is_market_open_now": True,
            "session_date": "2026-07-30",
        }
        formal = {
            "ready": True,
            "reason": "fully_evidenced_run_found",
            "run_uid": "formal-ready-run",
        }
        runs = {
            "items": [{
                "run_uid": "formal-ready-run",
                "status": "completed",
                "planned_count": 0,
                "submitted_count": 0,
                "settings": {
                    "account_id": 9,
                    "auto_execution_mode": "vnpy_paper",
                },
            }],
        }
        detail = {
            "decisions": [{
                "action": "skip",
                "reason": "low_open_reclaim_unconfirmed",
            }],
        }
        with patch.object(
            self.service,
            "_trading_window_diagnostics",
            return_value=window,
        ), patch(
            "src.services.vnpy_paper_trading_service._cross_market_daily_evidence_status",
            return_value=formal,
        ), patch.object(
            self.service.agent_repo,
            "list_runs",
            return_value=runs,
        ), patch.object(
            self.service.agent_repo,
            "get_run_detail",
            return_value=detail,
        ):
            normal = self.service._cross_market_formal_run_cadence_guard(
                settings,
                trigger_source="vnpy_paper_auto",
            )
            recheck = self.service._cross_market_formal_run_cadence_guard(
                settings,
                trigger_source="vnpy_paper_auto",
                allow_intraday_entry_recheck=True,
            )

        self.assertTrue(normal["block"])
        self.assertFalse(recheck["block"])
        self.assertTrue(recheck["intraday_entry_recheck"])
        self.assertEqual(
            recheck["recoverable_reasons"],
            ["low_open_reclaim_unconfirmed"],
        )

        detail["decisions"].append({
            "action": "skip",
            "reason": "cn_high_open_buy_blocked",
        })
        with patch.object(
            self.service,
            "_trading_window_diagnostics",
            return_value=window,
        ), patch(
            "src.services.vnpy_paper_trading_service._cross_market_daily_evidence_status",
            return_value=formal,
        ), patch.object(
            self.service.agent_repo,
            "list_runs",
            return_value=runs,
        ), patch.object(
            self.service.agent_repo,
            "get_run_detail",
            return_value=detail,
        ):
            mixed = self.service._cross_market_formal_run_cadence_guard(
                settings,
                trigger_source="vnpy_paper_auto",
                allow_intraday_entry_recheck=True,
            )

        self.assertTrue(mixed["block"])
        self.assertEqual(mixed["reason"], "formal_execution_skip_not_recoverable")
        self.assertEqual(
            mixed["non_recoverable_reasons"],
            ["cn_high_open_buy_blocked"],
        )
        detail["decisions"].pop()

        runs["items"][0]["submitted_count"] = 1
        with patch.object(
            self.service,
            "_trading_window_diagnostics",
            return_value=window,
        ), patch(
            "src.services.vnpy_paper_trading_service._cross_market_daily_evidence_status",
            return_value=formal,
        ), patch.object(
            self.service.agent_repo,
            "list_runs",
            return_value=runs,
        ):
            active = self.service._cross_market_formal_run_cadence_guard(
                settings,
                trigger_source="vnpy_paper_auto",
                allow_intraday_entry_recheck=True,
            )

        self.assertTrue(active["block"])
        self.assertEqual(
            active["reason"],
            "formal_execution_activity_blocks_recovery",
        )

    def test_formal_cadence_guard_rechecks_when_cn_open_snapshot_arrives(self) -> None:
        settings = VnpyPaperSettings(
            enabled=True,
            account_id=9,
            auto_trade_enabled=True,
            auto_strategy=CROSS_MARKET_STRATEGY_ID,
            auto_execution_mode="vnpy_paper",
        )
        window = {
            "is_market_open_now": True,
            "session_date": "2026-08-04",
        }
        formal = {
            "ready": False,
            "reason": "fully_evidenced_formal_run_not_found",
            "run_observed": True,
            "run_uid": "formal-cn-open-lag-run",
            "planned_count": 0,
            "submitted_count": 0,
        }
        with patch.object(
            self.service,
            "_trading_window_diagnostics",
            return_value=window,
        ), patch(
            "src.services.vnpy_paper_trading_service._cross_market_daily_evidence_status",
            return_value=formal,
        ), patch.object(
            self.service,
            "_cross_market_observation_snapshot",
            return_value={"status": "ready", "missing_requirements": []},
        ), patch.object(
            self.service.agent_repo,
            "list_runs",
            return_value={
                "items": [{
                    "run_uid": "formal-cn-open-lag-run",
                    "status": "completed",
                    "planned_count": 0,
                    "submitted_count": 0,
                    "settings": {
                        "account_id": 9,
                        "auto_execution_mode": "vnpy_paper",
                    },
                }],
            },
        ), patch.object(
            self.service.agent_repo,
            "get_run_detail",
            return_value={
                "decisions": [{
                    "action": "skip",
                    "reason": "cn_open_signal_unavailable",
                }],
            },
        ):
            result = self.service._cross_market_formal_run_cadence_guard(
                settings,
                trigger_source="vnpy_paper_auto",
                allow_intraday_entry_recheck=True,
            )

        self.assertFalse(result["block"])
        self.assertTrue(result["formal_recovery"])
        self.assertEqual(
            result["recoverable_reasons"],
            ["cn_open_signal_unavailable"],
        )
        self.assertEqual(result["current_evidence"]["status"], "ready")

    def test_formal_cadence_guard_does_not_recheck_hard_entry_blocks(self) -> None:
        settings = VnpyPaperSettings(
            enabled=True,
            account_id=9,
            auto_trade_enabled=True,
            auto_strategy=CROSS_MARKET_STRATEGY_ID,
            auto_execution_mode="vnpy_paper",
        )
        window = {
            "is_market_open_now": True,
            "session_date": "2026-07-31",
        }
        formal = {
            "ready": True,
            "reason": "fully_evidenced_run_found",
            "run_uid": "formal-high-open-run",
        }
        with patch.object(
            self.service,
            "_trading_window_diagnostics",
            return_value=window,
        ), patch(
            "src.services.vnpy_paper_trading_service._cross_market_daily_evidence_status",
            return_value=formal,
        ), patch.object(
            self.service.agent_repo,
            "list_runs",
            return_value={
                "items": [{
                    "run_uid": "formal-high-open-run",
                    "status": "completed",
                    "planned_count": 0,
                    "submitted_count": 0,
                    "settings": {
                        "account_id": 9,
                        "auto_execution_mode": "vnpy_paper",
                    },
                }],
            },
        ), patch.object(
            self.service.agent_repo,
            "get_run_detail",
            return_value={
                "decisions": [{
                    "action": "skip",
                    "reason": "cn_high_open_buy_blocked",
                }],
            },
        ):
            result = self.service._cross_market_formal_run_cadence_guard(
                settings,
                trigger_source="vnpy_paper_auto",
                allow_intraday_entry_recheck=True,
            )

        self.assertTrue(result["block"])
        self.assertEqual(
            result["reason"],
            "formal_execution_skip_not_recoverable",
        )

    def test_intraday_slot_audit_is_scoped_to_account_and_execution_mode(self) -> None:
        service = MagicMock()
        settings = VnpyPaperSettings(
            account_id=9,
            auto_strategy=CROSS_MARKET_STRATEGY_ID,
            auto_execution_mode="vnpy_paper",
        )
        start = datetime(2026, 7, 30, 5, 30, tzinfo=timezone.utc)
        end = start + timedelta(minutes=2)
        other_account = {
            "settings": {
                "account_id": 8,
                "auto_execution_mode": "vnpy_paper",
            },
        }
        dry_run = {
            "settings": {
                "account_id": 9,
                "auto_execution_mode": "dry_run",
            },
        }
        service.agent_repo.list_runs.return_value = {
            "items": [other_account, dry_run],
            "total": 2,
        }

        self.assertFalse(
            _cross_market_intraday_entry_slot_audited(
                service,
                settings,
                start=start,
                end=end,
            )
        )

        service.agent_repo.list_runs.return_value = {
            "items": [
                other_account,
                dry_run,
                {
                    "settings": {
                        "account_id": 9,
                        "auto_execution_mode": "vnpy_paper",
                    },
                },
            ],
            "total": 3,
        }
        self.assertTrue(
            _cross_market_intraday_entry_slot_audited(
                service,
                settings,
                start=start,
                end=end,
            )
        )

    def test_session_order_activity_scans_all_sell_monitor_pages(self) -> None:
        service = MagicMock()
        settings = VnpyPaperSettings(
            account_id=9,
            auto_strategy=CROSS_MARKET_STRATEGY_ID,
            auto_execution_mode="vnpy_paper",
        )
        zero_activity = [{
            "run_uid": f"sell-monitor-{index}",
            "planned_count": 0,
            "submitted_count": 0,
            "settings": {
                "account_id": 9,
                "auto_execution_mode": "vnpy_paper",
            },
        } for index in range(100)]
        submitted = {
            "run_uid": "sell-monitor-submitted",
            "planned_count": 0,
            "submitted_count": 1,
            "settings": {
                "account_id": 9,
                "auto_execution_mode": "vnpy_paper",
            },
        }

        def list_runs(**kwargs):
            if kwargs["trigger_source"] != "cross_market_intraday_sell_monitor":
                return {"items": [], "total": 0}
            if kwargs["offset"] == 0:
                return {"items": zero_activity, "total": 101}
            return {"items": [submitted], "total": 101}

        service.agent_repo.list_runs.side_effect = list_runs

        status = _cross_market_session_order_activity_status(
            service,
            settings,
            {"session_date": "2026-07-30"},
        )

        self.assertTrue(status["available"])
        self.assertTrue(status["has_activity"])
        self.assertEqual(status["planned_count"], 0)
        self.assertEqual(status["submitted_count"], 1)
        self.assertEqual(
            status["activity_runs"][0]["run_uid"],
            "sell-monitor-submitted",
        )

    def test_campaign_guard_allows_completion_session_and_blocks_later_dates(self) -> None:
        account = self.service.ensure_account()
        self.service.update_settings({
            "auto_strategy": CROSS_MARKET_STRATEGY_ID,
            "account_id": int(account["id"]),
        })
        settings = self.service.get_settings()
        today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
        observation = {
            "campaign_active": True,
            "account_matches_current": True,
            "campaign_account_id": int(account["id"]),
            "current_account_id": int(account["id"]),
            "remaining_trading_days": 0,
        }

        with patch(
            "src.services.vnpy_paper_trading_service.CrossMarketAcceptanceService.get_status",
            return_value={
                "ready": True,
                "paper_observation": {
                    **observation,
                    "completion_session_date": today.isoformat(),
                },
            },
        ):
            same_day = self.service._cross_market_campaign_execution_guard(settings)
        with patch(
            "src.services.vnpy_paper_trading_service.CrossMarketAcceptanceService.get_status",
            return_value={
                "ready": True,
                "paper_observation": {
                    **observation,
                    "completion_session_date": (today - timedelta(days=1)).isoformat(),
                },
            },
        ):
            later_day = self.service._cross_market_campaign_execution_guard(settings)

        self.assertFalse(same_day["block"])
        self.assertEqual(same_day["reason"], "paper_campaign_completion_session_active")
        self.assertTrue(later_day["block"])
        self.assertEqual(later_day["reason"], "paper_campaign_completed")

    def test_cross_market_campaign_report_is_account_bound_and_detects_contamination(self) -> None:
        account = self.service.ensure_account()
        campaign_started_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        strategy_trade = self.service.submit_order(
            symbol="600519",
            side="buy",
            market="cn",
            quantity=100,
            price=10.0,
            source="cross_market_auto_entry",
            raw={"instrument_type": "stock"},
        )
        self.assertTrue(strategy_trade["accepted"])
        manual_trade = self.service.submit_order(
            symbol="000001",
            side="buy",
            market="cn",
            quantity=100,
            price=10.0,
            source="manual",
        )
        self.assertTrue(manual_trade["accepted"])
        acceptance = {
            "ready": True,
            "paper_observation": {
                "campaign_active": True,
                "campaign_started_at": campaign_started_at.isoformat(),
                "campaign_account_id": int(account["id"]),
                "current_account_id": int(account["id"]),
                "account_matches_current": True,
                "initial_equity": 100000.0,
                "required_trading_days": 30,
                "observed_trading_days": 30,
                "remaining_trading_days": 0,
                "degraded_trading_days": 0,
                "fully_evidenced_without_formal_execution_days": 1,
                "fully_evidenced_via_later_observation_days": 1,
                "completion_session_date": date.today().isoformat(),
                "ready": True,
            },
        }

        with patch(
            "src.services.portfolio_service.PortfolioService._fetch_realtime_position_price",
            return_value=(10.0, "unit-test"),
        ):
            report = self.service.get_cross_market_campaign_report(acceptance)

        self.assertFalse(report["is_final"])
        self.assertEqual(report["report_status"], "account_contaminated")
        self.assertEqual(
            report["report_window"]["date_to"],
            date.today().isoformat(),
        )
        self.assertTrue(report["report_window"]["bounded_to_completion_session"])
        self.assertEqual(report["transaction_count"], 1)
        self.assertEqual(report["non_strategy_transaction_count"], 1)
        self.assertEqual(report["transactions"][0]["id"], strategy_trade["trade_id"])
        self.assertAlmostEqual(
            report["performance"]["trade_metrics"]["total_transaction_cost"],
            5.01,
            places=6,
        )
        self.assertEqual(
            report["performance"]["equity_curve"][0]["date"],
            report["report_window"]["date_from"],
        )
        self.assertEqual(
            report["performance"]["equity_curve"][-1]["date"],
            date.today().isoformat(),
        )
        self.assertIn(
            "campaign_account_contains_non_strategy_transactions",
            report["warnings"],
        )
        self.assertIn(
            "campaign_contains_fully_evidenced_days_without_formal_execution",
            report["warnings"],
        )
        self.assertIn(
            "campaign_contains_later_observation_evidence_not_counted",
            report["warnings"],
        )

    def test_cross_market_campaign_report_fails_closed_on_account_mismatch(self) -> None:
        report = self.service.get_cross_market_campaign_report({
            "ready": False,
            "paper_observation": {
                "campaign_active": True,
                "campaign_started_at": datetime.now(timezone.utc).isoformat(),
                "campaign_account_id": 999,
                "current_account_id": None,
                "account_matches_current": False,
            },
        })

        self.assertFalse(report["is_final"])
        self.assertEqual(report["report_status"], "account_mismatch")
        self.assertEqual(report["transactions"], [])

    def test_cross_market_closing_snapshot_skips_before_cutoff(self) -> None:
        phase = SimpleNamespace(is_trading_day=True, warnings=[])
        now = datetime(2026, 7, 28, 6, 54, tzinfo=timezone.utc)

        with (
            patch(
                "src.services.vnpy_paper_trading_service.trading_calendar."
                "build_market_phase_context",
                return_value=phase,
            ),
            patch.object(
                self.service.portfolio,
                "get_portfolio_snapshot",
            ) as snapshot_mock,
        ):
            result = self.service.capture_cross_market_campaign_closing_snapshot(
                now=now
            )

        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "before_cn_closing_snapshot_cutoff")
        snapshot_mock.assert_not_called()

    def test_cross_market_closing_snapshot_fails_closed_without_calendar(self) -> None:
        phase = SimpleNamespace(
            is_trading_day=True,
            warnings=["calendar_unavailable"],
        )
        now = datetime(2026, 7, 28, 7, 5, tzinfo=timezone.utc)

        with patch(
            "src.services.vnpy_paper_trading_service.trading_calendar."
            "build_market_phase_context",
            return_value=phase,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "cross_market_closing_snapshot_calendar_unavailable",
            ):
                self.service.capture_cross_market_campaign_closing_snapshot(now=now)

    def test_cross_market_closing_snapshot_requires_completion_boundary(self) -> None:
        phase = SimpleNamespace(is_trading_day=True, warnings=[])
        now = datetime(2026, 7, 28, 7, 5, tzinfo=timezone.utc)
        acceptance = {
            "ready": True,
            "paper_observation": {
                "campaign_active": True,
            },
        }

        with (
            patch(
                "src.services.vnpy_paper_trading_service.trading_calendar."
                "build_market_phase_context",
                return_value=phase,
            ),
            patch(
                "src.services.vnpy_paper_trading_service."
                "CrossMarketAcceptanceService"
            ) as acceptance_service,
        ):
            acceptance_service.return_value.get_status.return_value = acceptance
            with self.assertRaisesRegex(
                RuntimeError,
                "cross_market_closing_snapshot_completion_boundary_unavailable",
            ):
                self.service.capture_cross_market_campaign_closing_snapshot(now=now)

    def test_cross_market_closing_snapshot_persists_once_after_cutoff(self) -> None:
        account = self.service.ensure_account()
        account_id = int(account["id"])
        self.service.update_settings({
            "account_id": account_id,
            "auto_strategy": CROSS_MARKET_STRATEGY_ID,
        })
        session_date = date(2026, 7, 28)
        now = datetime(2026, 7, 28, 7, 5, tzinfo=timezone.utc)
        phase = SimpleNamespace(is_trading_day=True, warnings=[])
        closing_row = SimpleNamespace(
            snapshot_date=session_date,
            updated_at=datetime(2026, 7, 28, 15, 5),
            payload=json.dumps({
                "data_quality": "ok",
                "fx_stale": False,
                "limitations": [],
                "positions": [],
            }),
        )
        acceptance = {
            "ready": False,
            "paper_observation": {
                "campaign_active": True,
                "campaign_account_id": account_id,
                "account_matches_current": True,
            },
        }

        with (
            patch(
                "src.services.vnpy_paper_trading_service.trading_calendar."
                "build_market_phase_context",
                return_value=phase,
            ),
            patch(
                "src.services.vnpy_paper_trading_service."
                "CrossMarketAcceptanceService"
            ) as acceptance_service,
            patch.object(
                self.service.portfolio.repo,
                "list_daily_snapshots_for_risk",
                side_effect=[[], [closing_row], [closing_row]],
            ),
            patch.object(
                self.service.portfolio,
                "get_portfolio_snapshot",
                return_value={"total_equity": 100000.0},
            ) as snapshot_mock,
        ):
            acceptance_service.return_value.get_status.return_value = acceptance
            first = self.service.capture_cross_market_campaign_closing_snapshot(
                now=now
            )
            second = self.service.capture_cross_market_campaign_closing_snapshot(
                now=now
            )

        self.assertFalse(first["skipped"])
        self.assertEqual(first["reason"], "closing_snapshot_persisted")
        self.assertEqual(first["account_id"], account_id)
        self.assertEqual(first["total_equity"], 100000.0)
        self.assertTrue(second["skipped"])
        self.assertEqual(second["reason"], "closing_snapshot_already_persisted")
        self.assertEqual(snapshot_mock.call_count, 2)
        self.assertEqual(
            snapshot_mock.call_args_list[0].kwargs,
            {
                "account_id": account_id,
                "as_of": session_date,
                "cost_method": "fifo",
                "persist": False,
            },
        )
        self.assertEqual(
            snapshot_mock.call_args_list[1].kwargs,
            {
                "account_id": account_id,
                "as_of": session_date,
                "cost_method": "fifo",
                "persist": True,
                "realtime_price_overrides": {},
            },
        )

    def test_cross_market_closing_snapshot_rejects_stale_position_quote(self) -> None:
        account = self.service.ensure_account()
        account_id = int(account["id"])
        self.service.update_settings({
            "account_id": account_id,
            "auto_strategy": CROSS_MARKET_STRATEGY_ID,
        })
        session_date = date(2026, 7, 28)
        now = datetime(2026, 7, 28, 7, 5, tzinfo=timezone.utc)
        phase = SimpleNamespace(is_trading_day=True, warnings=[])
        acceptance = {
            "ready": False,
            "paper_observation": {
                "campaign_active": True,
                "campaign_account_id": account_id,
                "account_matches_current": True,
            },
        }
        stale_quote = SimpleNamespace(
            price=12.0,
            provider="unit-test",
            provider_timestamp=(now - timedelta(minutes=3)).isoformat(),
        )

        with (
            patch(
                "src.services.vnpy_paper_trading_service.trading_calendar."
                "build_market_phase_context",
                return_value=phase,
            ),
            patch(
                "src.services.vnpy_paper_trading_service."
                "CrossMarketAcceptanceService"
            ) as acceptance_service,
            patch.object(
                self.service.portfolio.repo,
                "list_daily_snapshots_for_risk",
                return_value=[],
            ),
            patch.object(
                self.service.portfolio,
                "get_portfolio_snapshot",
                return_value={
                    "accounts": [{
                        "positions": [{"symbol": "600519"}],
                    }],
                },
            ) as snapshot_mock,
            patch.object(
                self.service,
                "_get_cross_market_realtime_quote",
                return_value=stale_quote,
            ),
        ):
            acceptance_service.return_value.get_status.return_value = acceptance
            with self.assertRaisesRegex(
                RuntimeError,
                "cross_market_closing_snapshot_quote_stale:600519",
            ):
                self.service.capture_cross_market_campaign_closing_snapshot(
                    now=now
                )

        snapshot_mock.assert_called_once_with(
            account_id=account_id,
            as_of=session_date,
            cost_method="fifo",
            persist=False,
        )

    def test_cross_market_final_report_excludes_post_completion_trades_and_prices(self) -> None:
        account = self.service.ensure_account()
        account_id = int(account["id"])
        completion_date = date.today()
        completion_snapshot_at = datetime.combine(
            completion_date,
            datetime.min.time(),
            tzinfo=ZoneInfo("Asia/Shanghai"),
        ).replace(hour=15)
        included = self.service.portfolio.record_trade(
            account_id=account_id,
            symbol="600519",
            trade_date=completion_date,
            side="buy",
            quantity=100,
            price=10.0,
            fee=5.0,
            market="cn",
            currency="CNY",
            note="source=cross_market_auto_entry",
        )
        self.service.portfolio.record_trade(
            account_id=account_id,
            symbol="000001",
            trade_date=completion_date + timedelta(days=1),
            side="buy",
            quantity=100,
            price=20.0,
            fee=5.0,
            market="cn",
            currency="CNY",
            note="source=cross_market_auto_entry",
        )
        run = self.service.agent_repo.create_run(
            run_uid="completed-campaign-execution-evidence",
            trigger_source="vnpy_paper_auto",
            strategy=CROSS_MARKET_STRATEGY_ID,
            market="cn",
        )
        self.service.agent_repo.record_trade_plan(
            plan_uid="completed-campaign-execution-plan",
            run_id=int(run["id"]),
            decision_id=None,
            symbol="600519",
            market="cn",
            side="buy",
            status="filled",
            execution_mode="vnpy_paper",
            planned_cash_amount=1000,
            planned_quantity=100,
            planned_price=10,
            submitted_quantity=100,
            submitted_price=10,
            trade_id=int(included["id"]),
            order_result={
                "status": "filled",
                "raw": {
                    "cross_market_strategy": {
                        "strategy_id": CROSS_MARKET_STRATEGY_ID,
                    },
                    "fill_sync": {
                        "trades": [{
                            "local_trade_id": int(included["id"]),
                            "execution": {
                                "mode": "next_minute_vwap",
                                "reference_price": 9.99,
                                "fill_price": 10.0,
                                "actual_slippage_bps": 10.01001,
                                "adverse_slippage_cost": 1.0,
                            },
                        }],
                    },
                },
            },
        )
        acceptance = {
            "ready": True,
            "paper_observation": {
                "campaign_active": True,
                "campaign_started_at": (
                    datetime.now(timezone.utc) - timedelta(days=1)
                ).isoformat(),
                "campaign_account_id": account_id,
                "current_account_id": account_id,
                "account_matches_current": True,
                "initial_equity": 100000.0,
                "required_trading_days": 30,
                "fully_evidenced_trading_days": 30,
                "remaining_trading_days": 0,
                "degraded_trading_days": 0,
                "observation_dates": [completion_date.isoformat()],
                "session_results": [{
                    "session_date": completion_date.isoformat(),
                    "evidence_status": "ready",
                    "fully_evidenced": True,
                }],
                "completion_session_date": completion_date.isoformat(),
                "ready": True,
            },
        }

        with patch(
            "src.services.portfolio_service.PortfolioService._fetch_realtime_position_price",
            return_value=(10.0, "unit-test"),
        ):
            self.service.portfolio.get_portfolio_snapshot(
                account_id=account_id,
                as_of=completion_date,
                persist=True,
                realtime_price_overrides={
                    "600519": {
                        "price": 10.0,
                        "provider": "unit-test",
                        "provider_timestamp": datetime.now(timezone.utc).isoformat(),
                    },
                },
            )
            with DatabaseManager.get_instance().get_session() as session:
                row = session.query(PortfolioDailySnapshot).filter_by(
                    account_id=account_id,
                    snapshot_date=completion_date,
                    cost_method="fifo",
                ).one()
                payload = json.loads(row.payload)
                for position in list(payload.get("positions") or []):
                    position["price_provider_timestamp"] = (
                        completion_snapshot_at - timedelta(seconds=30)
                    ).astimezone(timezone.utc).isoformat()
                row.payload = json.dumps(payload)
                row.updated_at = completion_snapshot_at.replace(tzinfo=None)
                session.commit()
            report = self.service.get_cross_market_campaign_report(acceptance)

        self.assertTrue(report["is_final"], report)
        self.assertFalse(report["strategy_accepted"])
        self.assertEqual(report["report_status"], "final_insufficient_sample")
        self.assertIn("campaign_statistical_sample_insufficient", report["warnings"])
        self.assertEqual(report["transaction_count"], 1)
        self.assertEqual(report["transactions"][0]["id"], included["id"])
        self.assertEqual(report["performance"]["total_market_value"], 1000.0)
        self.assertEqual(report["performance"]["trade_metrics"]["trade_count"], 1)
        self.assertEqual(
            report["performance"]["daily_snapshot_coverage"]["coverage_pct"],
            100.0,
        )
        self.assertEqual(
            report["performance"]["slippage_metrics"]["coverage_pct"],
            100.0,
        )
        self.assertEqual(
            report["report_window"]["date_to"],
            completion_date.isoformat(),
        )
        self.assertEqual(
            report["performance"]["equity_curve"][-1]["date"],
            completion_date.isoformat(),
        )

    def test_cross_market_campaign_report_keeps_zero_trade_daily_snapshots(self) -> None:
        account = self.service.ensure_account()
        account_id = int(account["id"])
        start_date = date.today() - timedelta(days=3)
        observation_dates = [
            start_date + timedelta(days=1),
            start_date + timedelta(days=2),
        ]
        for snapshot_date in observation_dates:
            self.service.portfolio.get_portfolio_snapshot(
                account_id=account_id,
                as_of=snapshot_date,
                persist=True,
            )
        with DatabaseManager.get_instance().get_session() as session:
            for snapshot_date in observation_dates:
                session.execute(
                    text(
                        f"UPDATE {PortfolioDailySnapshot.__tablename__} "
                        "SET updated_at = :updated_at "
                        "WHERE account_id = :account_id "
                        "AND snapshot_date = :snapshot_date"
                    ),
                    {
                        "updated_at": datetime.combine(
                            snapshot_date,
                            datetime.min.time(),
                        ).replace(hour=15, minute=5),
                        "account_id": account_id,
                        "snapshot_date": snapshot_date,
                    },
                )
            session.commit()
        acceptance = {
            "ready": False,
            "paper_observation": {
                "campaign_active": True,
                "campaign_started_at": (
                    datetime.combine(
                        start_date,
                        datetime.min.time(),
                        tzinfo=timezone.utc,
                    ) + timedelta(hours=1)
                ).isoformat(),
                "campaign_account_id": account_id,
                "current_account_id": account_id,
                "account_matches_current": True,
                "initial_equity": 100000.0,
                "required_trading_days": 30,
                "observed_trading_days": 2,
                "remaining_trading_days": 28,
                "degraded_trading_days": 0,
                "observation_dates": [
                    item.isoformat() for item in observation_dates
                ],
                "session_results": [
                    {
                        "session_date": item.isoformat(),
                        "evidence_status": "ready",
                        "fully_evidenced": True,
                    }
                    for item in observation_dates
                ],
            },
        }

        report = self.service.get_cross_market_campaign_report(acceptance)

        curve = report["performance"]["equity_curve"]
        self.assertEqual(
            [item["date"] for item in curve],
            [start_date.isoformat(), *[item.isoformat() for item in observation_dates]],
        )
        self.assertEqual([item["trade_count"] for item in curve], [0, 0, 0])
        self.assertEqual(len(report["performance"]["daily_returns"]), 3)
        self.assertEqual(
            report["performance"]["daily_snapshot_coverage"],
            {
                "observed_trading_days": 2,
                "covered_trading_days": 2,
                "missing_trading_days": 0,
                "non_closing_trading_days": 0,
                "invalid_valuation_trading_days": 0,
                "coverage_pct": 100.0,
                "missing_dates": [],
                "non_closing_dates": [],
                "invalid_valuation_dates": [],
                "valuation_requirements": (
                    "realtime_position_prices_no_stale_fx_no_limitations"
                ),
                "closing_time_cutoff": "14:55:00+08:00",
                "basis": "persisted_account_closing_snapshot",
            },
        )
        self.assertEqual(
            report["performance"]["risk_metrics"]["drawdown_basis"],
            "persisted_account_closing_snapshot",
        )
        self.assertNotIn(
            "campaign_daily_snapshot_evidence_incomplete",
            report["warnings"],
        )

    def test_cross_market_campaign_report_rejects_degraded_position_valuation(
        self,
    ) -> None:
        account = self.service.ensure_account()
        account_id = int(account["id"])
        snapshot_date = date.today()
        self.service.portfolio.record_trade(
            account_id=account_id,
            symbol="600519",
            trade_date=snapshot_date,
            side="buy",
            quantity=100,
            price=10.0,
            fee=5.0,
            market="cn",
            currency="CNY",
            note="source=cross_market_auto_entry",
        )
        acceptance = {
            "ready": False,
            "paper_observation": {
                "campaign_active": True,
                "campaign_started_at": (
                    datetime.now(timezone.utc) - timedelta(hours=1)
                ).isoformat(),
                "campaign_account_id": account_id,
                "current_account_id": account_id,
                "account_matches_current": True,
                "initial_equity": 100000.0,
                "required_trading_days": 30,
                "observed_trading_days": 1,
                "remaining_trading_days": 29,
                "degraded_trading_days": 0,
                "observation_dates": [snapshot_date.isoformat()],
                "session_results": [{
                    "session_date": snapshot_date.isoformat(),
                    "evidence_status": "ready",
                    "fully_evidenced": True,
                }],
            },
        }

        with patch(
            "src.services.portfolio_service.PortfolioService."
            "_fetch_realtime_position_price",
            return_value=(None, None),
        ):
            self.service.portfolio.get_portfolio_snapshot(
                account_id=account_id,
                as_of=snapshot_date,
                persist=True,
            )
            with DatabaseManager.get_instance().get_session() as session:
                session.execute(
                    text(
                        f"UPDATE {PortfolioDailySnapshot.__tablename__} "
                        "SET updated_at = :updated_at "
                        "WHERE account_id = :account_id "
                        "AND snapshot_date = :snapshot_date"
                    ),
                    {
                        "updated_at": datetime.combine(
                            snapshot_date,
                            datetime.min.time(),
                        ).replace(hour=15, minute=5),
                        "account_id": account_id,
                        "snapshot_date": snapshot_date,
                    },
                )
                session.commit()
            report = self.service.get_cross_market_campaign_report(acceptance)

        coverage = report["performance"]["daily_snapshot_coverage"]
        self.assertEqual(coverage["covered_trading_days"], 0)
        self.assertEqual(coverage["invalid_valuation_trading_days"], 1)
        self.assertEqual(
            coverage["invalid_valuation_dates"],
            [snapshot_date.isoformat()],
        )
        point = report["performance"]["equity_curve"][-1]
        self.assertFalse(point["valuation_ready"])
        self.assertIn(
            "position_price_unavailable:600519",
            point["valuation_issues"],
        )
        self.assertIn(
            "campaign_daily_snapshot_evidence_incomplete",
            report["warnings"],
        )

    def test_cross_market_campaign_report_compacts_formal_decision_audit(self) -> None:
        account = self.service.ensure_account()
        account_id = int(account["id"])
        self.service.update_settings({
            "account_id": account_id,
            "auto_strategy": CROSS_MARKET_STRATEGY_ID,
            "auto_execution_mode": "vnpy_paper",
        })
        settings = self.service.get_settings()
        run = self.service.agent_repo.create_run(
            run_uid="campaign-decision-audit-run",
            trigger_source="vnpy_paper_auto",
            strategy=CROSS_MARKET_STRATEGY_ID,
            market="cn",
            settings={},
            diagnostics={},
        )
        candidate = {
            "code": "300260",
            "name": "integration candidate",
            "score": 82.45,
            "price": 64.24,
            "data_quality": "partial",
            "_cross_market_prefilter_theme": "gold",
            "_cross_market_source_theme": "gold",
        }
        order = self.service._skipped_order(
            symbol="300260",
            side="buy",
            price=64.24,
            reason="cn_extreme_low_open_buy_blocked",
            raw={
                "cross_market_strategy": {
                    "strategy_id": CROSS_MARKET_STRATEGY_ID,
                    "theme": "other",
                },
            },
        )
        self.service._record_agent_decision(
            run_id=int(run["id"]),
            sequence=1,
            candidate=candidate,
            symbol="300260",
            settings=settings,
            action="skip",
            order=order,
            reason="cn_extreme_low_open_buy_blocked",
            risk_flags=["cn_extreme_low_open_buy_blocked"],
        )
        acceptance = {
            "ready": False,
            "paper_observation": {
                "campaign_active": True,
                "campaign_started_at": (
                    datetime.now(timezone.utc) - timedelta(minutes=1)
                ).isoformat(),
                "campaign_account_id": account_id,
                "current_account_id": account_id,
                "account_matches_current": True,
                "initial_equity": 100000.0,
                "required_trading_days": 30,
                "remaining_trading_days": 29,
                "degraded_trading_days": 0,
                "observation_dates": [],
                "session_results": [{
                    "session_date": date.today().isoformat(),
                    "evidence_status": "ready",
                    "missing_requirements": [],
                    "formal_execution": {
                        "observed": True,
                        "run_id": int(run["id"]),
                        "run_uid": run["run_uid"],
                        "run_status": "completed",
                        "execution_mode": "vnpy_paper",
                        "candidate_count": 1,
                        "planned_count": 0,
                        "submitted_count": 0,
                        "skipped_count": 1,
                    },
                }],
            },
        }

        report = self.service.get_cross_market_campaign_report(acceptance)

        metrics = report["decision_metrics"]
        self.assertEqual(metrics["coverage_pct"], 100.0)
        self.assertEqual(metrics["decision_count"], 1)
        self.assertEqual(metrics["action_counts"], {"skip": 1})
        self.assertEqual(metrics["status_counts"], {"skipped": 1})
        self.assertEqual(
            metrics["reason_counts"],
            {"cn_extreme_low_open_buy_blocked": 1},
        )
        self.assertEqual(metrics["theme_counts"], {"other": 1})
        self.assertEqual(metrics["theme_reclassification_count"], 1)
        decision = report["decision_audit"][0]["decisions"][0]
        self.assertEqual(decision["symbol"], "300260")
        self.assertEqual(decision["theme"], "other")
        self.assertEqual(decision["prefilter_theme"], "gold")
        self.assertTrue(decision["theme_reclassified"])
        self.assertEqual(decision["data_quality"], "partial")
        self.assertEqual(
            decision["trade_plan"]["skip_reason"],
            "cn_extreme_low_open_buy_blocked",
        )

        missing = self.service._cross_market_campaign_decision_audit({
            "session_results": [{
                "session_date": date.today().isoformat(),
                "formal_execution": {
                    "observed": True,
                    "run_uid": "missing-formal-run",
                },
            }],
        })
        self.assertEqual(missing["metrics"]["coverage_pct"], 0.0)
        self.assertEqual(missing["metrics"]["missing_formal_run_count"], 1)
        self.assertEqual(
            missing["sessions"][0]["detail_status"],
            "unavailable",
        )

    def test_cross_market_final_report_fails_closed_without_closing_snapshot(self) -> None:
        account = self.service.ensure_account()
        account_id = int(account["id"])
        completion_date = date.today() - timedelta(days=1)
        missing_date = completion_date - timedelta(days=1)
        self.service.portfolio.get_portfolio_snapshot(
            account_id=account_id,
            as_of=missing_date,
            persist=True,
        )
        self.service.portfolio.get_portfolio_snapshot(
            account_id=account_id,
            as_of=completion_date,
            persist=True,
        )
        with DatabaseManager.get_instance().get_session() as session:
            session.execute(
                text(
                    f"UPDATE {PortfolioDailySnapshot.__tablename__} "
                    "SET updated_at = :updated_at "
                    "WHERE account_id = :account_id "
                    "AND snapshot_date = :snapshot_date"
                ),
                {
                    "updated_at": datetime.combine(
                        missing_date,
                        datetime.min.time(),
                    ).replace(hour=9, minute=35),
                    "account_id": account_id,
                    "snapshot_date": missing_date,
                },
            )
            session.execute(
                text(
                    f"UPDATE {PortfolioDailySnapshot.__tablename__} "
                    "SET updated_at = :updated_at "
                    "WHERE account_id = :account_id "
                    "AND snapshot_date = :snapshot_date"
                ),
                {
                    "updated_at": datetime.combine(
                        completion_date,
                        datetime.min.time(),
                    ).replace(hour=15),
                    "account_id": account_id,
                    "snapshot_date": completion_date,
                },
            )
            session.commit()
        acceptance = {
            "ready": True,
            "paper_observation": {
                "campaign_active": True,
                "campaign_started_at": (
                    datetime.combine(
                        missing_date - timedelta(days=1),
                        datetime.min.time(),
                        tzinfo=timezone.utc,
                    )
                ).isoformat(),
                "campaign_account_id": account_id,
                "current_account_id": account_id,
                "account_matches_current": True,
                "initial_equity": 100000.0,
                "required_trading_days": 30,
                "fully_evidenced_trading_days": 30,
                "remaining_trading_days": 0,
                "degraded_trading_days": 0,
                "observation_dates": [
                    missing_date.isoformat(),
                    completion_date.isoformat(),
                ],
                "completion_session_date": completion_date.isoformat(),
                "ready": True,
            },
        }

        report = self.service.get_cross_market_campaign_report(acceptance)

        self.assertFalse(report["is_final"])
        self.assertEqual(report["report_status"], "execution_evidence_incomplete")
        self.assertIn(
            "campaign_daily_snapshot_evidence_incomplete",
            report["warnings"],
        )
        self.assertEqual(
            report["performance"]["daily_snapshot_coverage"]["missing_dates"],
            [],
        )
        self.assertEqual(
            report["performance"]["daily_snapshot_coverage"]["non_closing_dates"],
            [missing_date.isoformat()],
        )

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
                    "SET created_at = :updated_at, updated_at = :updated_at WHERE id = :id"
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
                    "SET created_at = :updated_at, updated_at = :updated_at WHERE id = :id"
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

    def test_cross_market_consecutive_loss_cooldown_uses_three_cn_trading_days(self) -> None:
        settings = self.service._apply_cross_market_strategy_settings(
            replace(
                self.service.get_settings(),
                auto_strategy=CROSS_MARKET_STRATEGY_ID,
                auto_consecutive_loss_limit=3,
            )
        )
        shanghai = timezone(timedelta(hours=8))
        triggered_at = datetime(2026, 7, 24, 14, 0, tzinfo=shanghai)
        performance = {
            "trade_metrics": {
                "current_consecutive_loss_count": 3,
                "max_consecutive_loss_count": 3,
                "last_closed_trade_id": 7,
                "last_closed_trade_at": triggered_at.isoformat(),
                "last_closed_trade_pnl": -100.0,
            },
        }

        def weekday_open(_market, value):
            return value.weekday() < 5

        with patch.object(
            self.service,
            "_paper_trade_performance",
            return_value=performance,
        ), patch(
            "src.services.vnpy_paper_trading_service.trading_calendar.is_market_open",
            side_effect=weekday_open,
        ), patch.object(
            self.service,
            "_now_utc",
            return_value=datetime(2026, 7, 29, 10, 0, tzinfo=shanghai).astimezone(timezone.utc),
        ):
            cooling = self.service._consecutive_loss_diagnostics(
                settings=settings,
                account={"id": 1},
                evaluate=False,
            )

        self.assertEqual(cooling["cooldown_basis"], "cn_trading_days")
        self.assertEqual(cooling["cooldown_trading_days"], 3)
        self.assertEqual(cooling["status"], "cooling_down")
        self.assertEqual(
            self.service._parse_utc_datetime(cooling["recover_at"]),
            datetime(2026, 7, 30, 0, 0, tzinfo=shanghai).astimezone(timezone.utc),
        )

        with patch.object(
            self.service,
            "_paper_trade_performance",
            return_value=performance,
        ), patch(
            "src.services.vnpy_paper_trading_service.trading_calendar.is_market_open",
            side_effect=weekday_open,
        ), patch.object(
            self.service,
            "_now_utc",
            return_value=datetime(2026, 7, 30, 10, 0, tzinfo=shanghai).astimezone(timezone.utc),
        ):
            recovered = self.service._consecutive_loss_diagnostics(
                settings=settings,
                account={"id": 1},
                evaluate=False,
            )
        self.assertEqual(recovered["status"], "cooldown_elapsed")
        self.assertFalse(recovered["guard_blocked"])

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

    def test_intraday_market_gate_uses_live_cn_index_and_breadth_without_snapshot(self) -> None:
        manager = MagicMock()
        provider_timestamp = datetime.now(timezone.utc).isoformat()
        manager.get_main_indices.return_value = [
            {
                "code": "sh000001",
                "name": "上证指数",
                "change_pct": -0.4,
                "provider_timestamp": provider_timestamp,
            },
            {
                "code": "sz399006",
                "name": "创业板指",
                "change_pct": 0.2,
                "provider_timestamp": provider_timestamp,
            },
        ]
        manager.get_market_stats.return_value = {
            "up_count": 1200,
            "down_count": 700,
            "flat_count": 100,
            "provider_timestamp": provider_timestamp,
            "provider_timestamp_coverage_pct": 100.0,
        }
        self.service.data_fetcher_manager = manager
        settings = replace(
            self.service.get_settings(),
            auto_intraday_market_gate_enabled=True,
            auto_intraday_index_min_change_pct=-1.0,
            auto_intraday_breadth_min_score=50,
        )

        with patch(
            "src.services.vnpy_paper_trading_service.load_previous_snapshot"
        ) as load_snapshot:
            reason, diagnostics = self.service._market_context_pre_trade_risk(settings)

        self.assertIsNone(reason)
        self.assertEqual(diagnostics["status"], "passed")
        self.assertEqual(diagnostics["schema_version"], 2)
        self.assertEqual(
            diagnostics["intraday_market"]["index"]["aggregate_change_pct"],
            -0.1,
        )
        self.assertEqual(diagnostics["intraday_market"]["breadth"]["score"], 60.0)
        manager.get_market_stats.assert_called_once_with(
            purpose="vnpy_paper_intraday_risk"
        )
        load_snapshot.assert_not_called()

    def test_intraday_market_gate_blocks_weak_live_index_before_breadth(self) -> None:
        manager = MagicMock()
        provider_timestamp = datetime.now(timezone.utc).isoformat()
        manager.get_main_indices.return_value = [
            {
                "code": "sh000001",
                "change_pct": -2.4,
                "provider_timestamp": provider_timestamp,
            },
            {
                "code": "sz399006",
                "change_pct": -1.8,
                "provider_timestamp": provider_timestamp,
            },
        ]
        self.service.data_fetcher_manager = manager
        settings = replace(
            self.service.get_settings(),
            auto_intraday_market_gate_enabled=True,
            auto_intraday_index_min_change_pct=-2.0,
        )

        reason, diagnostics = self.service._market_context_pre_trade_risk(settings)

        self.assertEqual(reason, "intraday_market_index_below_threshold")
        self.assertEqual(diagnostics["status"], "blocked")
        self.assertEqual(
            diagnostics["intraday_market"]["index"]["aggregate_change_pct"],
            -2.1,
        )
        manager.get_market_stats.assert_not_called()

    def test_intraday_market_gate_fails_closed_for_missing_cn_breadth(self) -> None:
        manager = MagicMock()
        manager.get_main_indices.return_value = [
            {"code": "sh000001", "change_pct": 0.1},
        ]
        manager.get_market_stats.return_value = {}
        self.service.data_fetcher_manager = manager
        settings = replace(
            self.service.get_settings(),
            auto_intraday_market_gate_enabled=True,
            auto_intraday_require_provider_timestamp=False,
        )

        reason, diagnostics = self.service._market_context_pre_trade_risk(settings)

        self.assertEqual(reason, "intraday_market_breadth_unavailable")
        self.assertEqual(diagnostics["status"], "unavailable")
        self.assertEqual(
            diagnostics["intraday_market"]["breadth"]["evidence_reason"],
            "breadth_counts_missing",
        )

    def test_intraday_market_gate_requires_provider_timestamp_by_default(self) -> None:
        manager = MagicMock()
        manager.get_main_indices.return_value = [
            {
                "code": "sh000001",
                "change_pct": 0.5,
                "provider": "akshare",
                "data_granularity": "realtime",
            }
        ]
        self.service.data_fetcher_manager = manager
        settings = replace(
            self.service.get_settings(),
            auto_intraday_market_gate_enabled=True,
        )

        reason, diagnostics = self.service._market_context_pre_trade_risk(settings)

        self.assertEqual(reason, "intraday_market_index_unavailable")
        index = diagnostics["intraday_market"]["index"]
        self.assertTrue(index["require_provider_timestamp"])
        self.assertEqual(
            index["rejected_indices"][0]["rejection_reason"],
            "provider_timestamp_required",
        )

    def test_intraday_market_gate_requires_fresh_breadth_provider_timestamp(self) -> None:
        manager = MagicMock()
        manager.get_main_indices.return_value = [
            {
                "code": "sh000001",
                "change_pct": 0.5,
                "provider_timestamp": datetime.now(timezone.utc).isoformat(),
                "data_granularity": "realtime",
            }
        ]
        manager.get_market_stats.return_value = {
            "up_count": 1200,
            "down_count": 700,
            "flat_count": 100,
            "provider": "akshare",
        }
        self.service.data_fetcher_manager = manager
        settings = replace(
            self.service.get_settings(),
            auto_intraday_market_gate_enabled=True,
        )

        reason, diagnostics = self.service._market_context_pre_trade_risk(settings)

        self.assertEqual(reason, "intraday_market_breadth_unavailable")
        breadth = diagnostics["intraday_market"]["breadth"]
        self.assertEqual(breadth["provider_timestamp_status"], "unavailable")
        self.assertEqual(
            breadth["evidence_reason"],
            "breadth_provider_timestamp_required",
        )

    def test_intraday_market_gate_rejects_partial_breadth_timestamp_coverage(self) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        manager = MagicMock()
        manager.get_main_indices.return_value = [
            {
                "code": "sh000001",
                "change_pct": 0.5,
                "provider": "tickflow",
                "provider_timestamp": now_iso,
                "data_granularity": "realtime",
            }
        ]
        manager.get_market_stats.return_value = {
            "up_count": 3000,
            "down_count": 1000,
            "flat_count": 100,
            "provider": "partial_source",
            "provider_timestamp": now_iso,
            "provider_timestamp_coverage_pct": 75.0,
        }
        self.service.data_fetcher_manager = manager
        settings = replace(
            self.service.get_settings(),
            auto_intraday_market_gate_enabled=True,
        )

        reason, diagnostics = self.service._market_context_pre_trade_risk(settings)

        self.assertEqual(reason, "intraday_market_breadth_unavailable")
        breadth = diagnostics["intraday_market"]["breadth"]
        self.assertEqual(breadth["provider_timestamp_status"], "fresh")
        self.assertEqual(breadth["provider_timestamp_coverage_pct"], 75.0)
        self.assertEqual(
            breadth["evidence_reason"],
            "breadth_provider_timestamp_required",
        )

    def test_intraday_market_gate_requires_explicit_complete_breadth_timestamp_coverage(self) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        manager = MagicMock()
        manager.get_main_indices.return_value = [
            {
                "code": "sh000001",
                "change_pct": 0.5,
                "provider_timestamp": now_iso,
                "data_granularity": "realtime",
            }
        ]
        manager.get_market_stats.return_value = {
            "up_count": 3000,
            "down_count": 1000,
            "flat_count": 100,
            "provider_timestamp": now_iso,
        }
        self.service.data_fetcher_manager = manager
        settings = replace(
            self.service.get_settings(),
            auto_intraday_market_gate_enabled=True,
        )

        reason, diagnostics = self.service._market_context_pre_trade_risk(settings)

        self.assertEqual(reason, "intraday_market_breadth_unavailable")
        breadth = diagnostics["intraday_market"]["breadth"]
        self.assertEqual(breadth["provider_timestamp_status"], "fresh")
        self.assertIsNone(breadth["provider_timestamp_coverage_pct"])
        self.assertEqual(
            breadth["evidence_reason"],
            "breadth_provider_timestamp_required",
        )

    def test_intraday_market_gate_rejects_explicit_end_of_day_index_fallback(self) -> None:
        manager = MagicMock()
        manager.get_main_indices.return_value = [
            {
                "code": "000001",
                "change_pct": 0.5,
                "provider": "tushare",
                "data_date": date.today().isoformat(),
                "data_granularity": "end_of_day",
            }
        ]
        self.service.data_fetcher_manager = manager
        settings = replace(
            self.service.get_settings(),
            auto_intraday_market_gate_enabled=True,
        )

        reason, diagnostics = self.service._market_context_pre_trade_risk(settings)

        self.assertEqual(reason, "intraday_market_index_unavailable")
        index = diagnostics["intraday_market"]["index"]
        self.assertEqual(index["evidence_reason"], "index_evidence_not_intraday")
        self.assertEqual(
            index["rejected_indices"][0]["rejection_reason"],
            "end_of_day_not_intraday",
        )
        manager.get_market_stats.assert_not_called()

    def test_intraday_market_gate_rejects_stale_provider_timestamp(self) -> None:
        manager = MagicMock()
        manager.get_main_indices.return_value = [
            {
                "code": "000001",
                "change_pct": 0.5,
                "provider": "tickflow",
                "provider_timestamp": (
                    datetime.now(timezone.utc) - timedelta(minutes=16)
                ).isoformat(),
                "data_granularity": "realtime",
            }
        ]
        self.service.data_fetcher_manager = manager
        settings = replace(
            self.service.get_settings(),
            auto_intraday_market_gate_enabled=True,
        )

        reason, diagnostics = self.service._market_context_pre_trade_risk(settings)

        self.assertEqual(reason, "intraday_market_index_unavailable")
        rejected = diagnostics["intraday_market"]["index"]["rejected_indices"][0]
        self.assertEqual(rejected["provider_timestamp_status"], "stale")
        self.assertEqual(rejected["rejection_reason"], "provider_timestamp_stale")

    def test_cross_market_gate_accepts_latest_closed_session_bar(self) -> None:
        manager = MagicMock()
        manager.get_main_indices.side_effect = lambda region: [
            {
                "code": "HSI" if region == "hk" else "SPX",
                "change_pct": -0.2,
                "provider": "yfinance",
                "data_date": (date.today() - timedelta(days=1)).isoformat(),
                "data_granularity": "session_bar",
            }
        ]
        self.service.data_fetcher_manager = manager
        settings = replace(
            self.service.get_settings(),
            auto_cross_market_gate_enabled=True,
            auto_cross_market_min_change_pct=-2.0,
        )

        reason, diagnostics = self.service._market_context_pre_trade_risk(settings)

        self.assertIsNone(reason)
        self.assertEqual(diagnostics["status"], "passed")
        evidence = diagnostics["cross_market"]["evidence"]
        self.assertTrue(all(item["require_intraday"] is False for item in evidence))
        self.assertTrue(
            all(item["indices"][0]["data_granularity"] == "session_bar" for item in evidence)
        )
        self.assertEqual(
            diagnostics["cross_market"]["quote_time_alignment"]["status"],
            "not_applicable",
        )

    def test_cross_market_gate_rejects_realtime_quotes_without_provider_time(self) -> None:
        manager = MagicMock()
        manager.get_main_indices.side_effect = lambda region: [
            {
                "code": "HSI" if region == "hk" else "SPX",
                "change_pct": -0.2,
                "provider": "example",
                "data_granularity": "realtime",
            }
        ]
        self.service.data_fetcher_manager = manager
        settings = replace(
            self.service.get_settings(),
            auto_cross_market_gate_enabled=True,
        )

        reason, diagnostics = self.service._market_context_pre_trade_risk(settings)

        self.assertEqual(reason, "cross_market_context_unavailable")
        self.assertEqual(
            diagnostics["cross_market"]["quote_time_alignment"]["reason"],
            "realtime_provider_timestamp_unavailable",
        )

    def test_cross_market_gate_rejects_realtime_quotes_beyond_time_skew(self) -> None:
        now = datetime.now(timezone.utc)
        manager = MagicMock()
        manager.get_main_indices.side_effect = lambda region: [
            {
                "code": "HSI" if region == "hk" else "SPX",
                "change_pct": -0.2,
                "provider": "example",
                "provider_timestamp": (
                    now if region == "hk" else now - timedelta(minutes=3)
                ).isoformat(),
                "data_granularity": "realtime",
            }
        ]
        self.service.data_fetcher_manager = manager
        settings = replace(
            self.service.get_settings(),
            auto_cross_market_gate_enabled=True,
        )

        reason, diagnostics = self.service._market_context_pre_trade_risk(settings)

        self.assertEqual(reason, "cross_market_context_unavailable")
        alignment = diagnostics["cross_market"]["quote_time_alignment"]
        self.assertEqual(alignment["reason"], "realtime_quote_skew_exceeded")
        self.assertGreater(alignment["observed_skew_seconds"], alignment["max_skew_seconds"])

    def test_cross_market_gate_accepts_realtime_quotes_within_time_skew(self) -> None:
        now = datetime.now(timezone.utc)
        manager = MagicMock()
        manager.get_main_indices.side_effect = lambda region: [
            {
                "code": "HSI" if region == "hk" else "SPX",
                "change_pct": -0.2,
                "provider": "example",
                "provider_timestamp": (
                    now if region == "hk" else now - timedelta(seconds=60)
                ).isoformat(),
                "data_granularity": "realtime",
            }
        ]
        self.service.data_fetcher_manager = manager
        settings = replace(
            self.service.get_settings(),
            auto_cross_market_gate_enabled=True,
        )

        reason, diagnostics = self.service._market_context_pre_trade_risk(settings)

        self.assertIsNone(reason)
        alignment = diagnostics["cross_market"]["quote_time_alignment"]
        self.assertEqual(alignment["status"], "bounded")
        self.assertEqual(alignment["observed_skew_seconds"], 60)

    def test_auto_trade_audits_cross_market_realtime_time_alignment_in_timeline(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_strategy": "dual_low",
                "auto_market": "cn",
                "auto_max_results": 1,
                "auto_cash_per_order": 1200,
                "auto_trade_time_gate_enabled": False,
                "auto_cross_market_gate_enabled": True,
            }
        )
        now = datetime.now(timezone.utc)
        manager = MagicMock()
        manager.get_main_indices.side_effect = lambda region: [
            {
                "code": "HSI" if region == "hk" else "SPX",
                "change_pct": -0.2,
                "provider": "example",
                "provider_timestamp": (
                    now if region == "hk" else now - timedelta(minutes=3)
                ).isoformat(),
                "data_granularity": "realtime",
            }
        ]
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "candidates": [{"code": "600519", "name": "贵州茅台", "score": 80, "price": 10.0}],
            "warnings": [],
        }
        self.service.data_fetcher_manager = manager

        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ):
            result = self.service.run_auto_trade_once()

        self.assertEqual(result["skipped_count"], 1)
        self.assertEqual(result["orders"][0]["reason"], "cross_market_context_unavailable")
        audit = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        assert audit is not None
        timeline = next(
            item for item in audit["timeline"] if item["stage"] == "market_context_risk"
        )
        self.assertIn("quote_time_status=unavailable", timeline["message"])
        self.assertIn("quote_time_skew=180", timeline["message"])
        self.assertIn("quote_time_max_skew=120", timeline["message"])

    def test_cross_market_gate_blocks_when_one_linked_market_breaks_threshold(self) -> None:
        manager = MagicMock()
        evidence = {
            "hk": [{"code": "HSI", "change_pct": -2.6}],
            "us": [
                {"code": "SPX", "change_pct": -0.3},
                {"code": "VIX", "change_pct": 8.0},
            ],
        }
        manager.get_main_indices.side_effect = lambda region: evidence[region]
        self.service.data_fetcher_manager = manager
        settings = replace(
            self.service.get_settings(),
            auto_cross_market_gate_enabled=True,
            auto_cross_market_min_change_pct=-2.0,
        )

        reason, diagnostics = self.service._market_context_pre_trade_risk(settings)

        self.assertEqual(reason, "cross_market_index_below_threshold")
        self.assertEqual(diagnostics["status"], "blocked")
        self.assertEqual(diagnostics["cross_market"]["linked_markets"], ["hk", "us"])
        self.assertEqual(diagnostics["cross_market"]["blocked_markets"], ["hk"])
        us_evidence = diagnostics["cross_market"]["evidence"][1]
        self.assertEqual(us_evidence["index_count"], 1)
        self.assertEqual(us_evidence["indices"][0]["code"], "SPX")

    def test_cross_market_gate_fails_closed_when_linked_quotes_are_empty(self) -> None:
        manager = MagicMock()
        manager.get_main_indices.side_effect = lambda region: (
            [{"code": "HSI", "change_pct": 0.1}] if region == "hk" else []
        )
        self.service.data_fetcher_manager = manager
        settings = replace(
            self.service.get_settings(),
            auto_cross_market_gate_enabled=True,
        )

        reason, diagnostics = self.service._market_context_pre_trade_risk(settings)

        self.assertEqual(reason, "cross_market_context_unavailable")
        self.assertEqual(diagnostics["status"], "unavailable")
        self.assertEqual(
            diagnostics["cross_market"]["evidence"][1]["evidence_reason"],
            "index_quotes_empty",
        )

    def test_intraday_market_index_timeout_fails_closed_with_bounded_audit(self) -> None:
        manager = MagicMock()

        def slow_indices(*, region):
            time.sleep(0.1)
            return [{"code": "sh000001", "change_pct": 0.1}]

        manager.get_main_indices.side_effect = slow_indices
        self.service.data_fetcher_manager = manager
        settings = replace(
            self.service.get_settings(),
            auto_intraday_market_gate_enabled=True,
        )

        started_at = time.monotonic()
        with patch(
            "src.services.vnpy_paper_trading_service.AUTO_MARKET_EVIDENCE_TIMEOUT_SECONDS",
            0.01,
        ):
            reason, diagnostics = self.service._market_context_pre_trade_risk(settings)
        elapsed = time.monotonic() - started_at

        self.assertEqual(reason, "intraday_market_index_unavailable")
        self.assertLess(elapsed, 0.08)
        index = diagnostics["intraday_market"]["index"]
        self.assertEqual(index["evidence_reason"], "index_fetch_timeout")
        self.assertEqual(index["timeout_seconds"], 0.01)
        self.assertGreaterEqual(index["duration_ms"], 5)
        time.sleep(0.11)

    def test_intraday_market_worker_pool_exhaustion_fails_closed_immediately(self) -> None:
        slots = self.service._market_evidence_slots
        acquired = []
        try:
            for _ in range(4):
                acquired.append(slots.acquire(blocking=False))
            self.assertTrue(all(acquired))
            evidence = self.service._live_market_index_evidence("cn")
        finally:
            for did_acquire in acquired:
                if did_acquire:
                    slots.release()

        self.assertFalse(evidence["available"])
        self.assertEqual(evidence["evidence_reason"], "index_worker_pool_exhausted")
        self.assertEqual(evidence["duration_ms"], 0)

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

    def test_cross_market_read_only_observation_bypasses_failure_fuse(self) -> None:
        self.service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_strategy": CROSS_MARKET_STRATEGY_ID,
                "auto_market": "cn",
                "auto_failure_fuse_enabled": True,
                "auto_failure_fuse_threshold": 2,
            }
        )
        for index in range(2):
            run = self.service.agent_repo.create_run(
                run_uid=f"failed-observation-{index}",
                trigger_source=CROSS_MARKET_OBSERVATION_TRIGGER_SOURCE,
                strategy=CROSS_MARKET_STRATEGY_ID,
                market="cn",
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
            result = self.service.run_auto_trade_once(
                execution_mode_override="dry_run",
                ignore_auto_trade_enabled=True,
                market_override="cn",
                strategy_override=CROSS_MARKET_STRATEGY_ID,
                max_results_override=3,
                trigger_source_override=CROSS_MARKET_OBSERVATION_TRIGGER_SOURCE,
            )

        self.assertTrue(result["accepted"])
        self.assertFalse(result["skipped"])
        self.assertEqual(result["submitted_count"], 0)
        audit = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        self.assertFalse(audit["settings"]["auto_failure_fuse_enabled"])
        self.assertNotIn("failure_fuse", audit["diagnostics"])

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

    def test_calibration_evidence_alert_records_only_readiness_transitions(self) -> None:
        pending = {
            "evidence_ready": False,
            "failure_count": 2,
            "required_markets": ["cn", "us"],
            "market_evidence": [{"market": "cn", "total_runs": 9}],
            "thresholds": {"min_runs_per_market": 20},
        }
        ready = {
            **pending,
            "evidence_ready": True,
            "failure_count": 0,
        }

        with patch.object(self.service, "_notify_auto_trade_alert_event") as notify:
            first = self.service.record_calibration_evidence_transition(pending)
            repeated = self.service.record_calibration_evidence_transition(pending)
            resolved = self.service.record_calibration_evidence_transition(ready)

        self.assertTrue(first["recorded"])
        self.assertEqual(first["status"], "degraded")
        self.assertFalse(repeated["recorded"])
        self.assertEqual(repeated["reason"], "state_unchanged")
        self.assertTrue(resolved["recorded"])
        self.assertEqual(resolved["status"], "resolved")
        self.assertFalse(resolved["previous_evidence_ready"])
        self.assertEqual(notify.call_count, 2)
        triggers = AlertService().list_triggers(target="vnpy_paper", page_size=10)["items"]
        self.assertEqual([item["status"] for item in triggers], ["resolved", "degraded"])
        saved = json.loads(triggers[0]["diagnostics"])
        self.assertTrue(saved["evidence_ready"])
        self.assertTrue(saved["read_only"])
        self.assertEqual(saved["market_counts"], ["cn:9:0:0:0"])

    def test_calibration_evidence_alert_failure_does_not_fail_monitoring(self) -> None:
        with patch(
            "src.services.alert_service.AlertService.get_latest_system_event",
            side_effect=RuntimeError("history unavailable"),
        ):
            result = self.service.record_calibration_evidence_transition({
                "evidence_ready": False,
            })

        self.assertFalse(result["recorded"])
        self.assertEqual(result["reason"], "alert_state_unavailable")

    def test_calibration_alert_retry_delivers_and_increments_persisted_attempt(self) -> None:
        alert_service = AlertService()
        trigger = alert_service.record_system_event(
            target="vnpy_paper",
            event_type="agent_calibration_evidence",
            status="degraded",
            reason="calibration_evidence_pending",
            data_source="vnpy_paper_auto",
            observed_value=0,
            threshold=1,
            diagnostics={"evidence_ready": False, "read_only": True},
        )
        alert_service.repo.record_notification_attempt({
            "trigger_id": trigger["id"],
            "channel": "feishu",
            "attempt": 1,
            "success": False,
            "error_code": "send_failed",
            "retryable": True,
        })
        dispatch = NotificationDispatchResult(
            dispatched=True,
            success=True,
            status="sent",
            channel_results=[
                ChannelAttemptResult(channel="feishu", success=True, latency_ms=15),
            ],
        )

        with patch("src.notification.NotificationService") as notification_cls:
            notification_cls.return_value.send_with_results.return_value = dispatch
            result = self.service.retry_latest_calibration_alert_notification(
                now=datetime.now(timezone.utc) + timedelta(minutes=6),
            )

        self.assertFalse(result["skipped"], result)
        self.assertEqual(result["reason"], "calibration_alert_retry_delivered")
        self.assertFalse(result["creates_agent_runs"])
        self.assertFalse(result["places_orders"])
        self.assertFalse(result["submits_orders"])
        self.assertEqual(result["delivery"]["status"], "delivered")
        self.assertEqual(result["delivery"]["retry_policy"]["status"], "succeeded")
        notifications = alert_service.list_notifications(
            trigger_id=trigger["id"],
            page_size=10,
        )["items"]
        self.assertEqual([item["attempt"] for item in notifications], [2, 1])
        self.assertTrue(notifications[0]["success"])

    def test_calibration_alert_retry_waits_and_stops_at_persisted_limit(self) -> None:
        alert_service = AlertService()
        trigger = alert_service.record_system_event(
            target="vnpy_paper",
            event_type="agent_calibration_evidence",
            status="degraded",
            reason="calibration_evidence_pending",
            data_source="vnpy_paper_auto",
            diagnostics={"evidence_ready": False},
        )
        alert_service.repo.record_notification_attempt({
            "trigger_id": trigger["id"],
            "channel": "feishu",
            "attempt": 1,
            "success": False,
            "error_code": "send_failed",
            "retryable": True,
        })

        with patch.object(
            self.service,
            "_send_auto_trade_alert_notification_safely",
            side_effect=AssertionError("retry window must be respected"),
        ):
            waiting = self.service.retry_latest_calibration_alert_notification()

        self.assertTrue(waiting["skipped"])
        self.assertEqual(waiting["reason"], "calibration_alert_retry_waiting")
        for attempt in (2, 3):
            alert_service.repo.record_notification_attempt({
                "trigger_id": trigger["id"],
                "channel": "feishu",
                "attempt": attempt,
                "success": False,
                "error_code": "send_failed",
                "retryable": True,
            })

        with patch.object(
            self.service,
            "_send_auto_trade_alert_notification_safely",
            side_effect=AssertionError("exhausted alert must not be sent"),
        ):
            exhausted = self.service.retry_latest_calibration_alert_notification(
                now=datetime.now(timezone.utc) + timedelta(days=1),
            )

        self.assertTrue(exhausted["skipped"])
        self.assertEqual(exhausted["reason"], "calibration_alert_retry_exhausted")
        self.assertEqual(exhausted["delivery"]["retry_policy"]["attempt"], 3)
        self.assertIsNone(exhausted["delivery"]["retry_policy"]["next_retry_at"])

    def test_calibration_alert_retry_absorbs_scheduler_boundary_jitter(self) -> None:
        recorded_at = datetime.now(timezone.utc).replace(microsecond=0)
        policy = self.service._calibration_alert_retry_policy(
            {
                "status": "failed",
                "attempts": [{
                    "attempt": 2,
                    "channel": "feishu",
                    "success": False,
                    "retryable": True,
                    "created_at": recorded_at.isoformat(),
                }],
            },
            now=recorded_at + timedelta(seconds=296),
        )

        self.assertEqual(policy["status"], "due")
        self.assertEqual(policy["attempt"], 2)
        self.assertEqual(
            policy["next_retry_at"],
            self.service._format_utc_datetime(recorded_at + timedelta(seconds=300)),
        )

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
        self.assertEqual(audit["decisions"][0]["position_plan"], decision_order["position_plan"])
        self.assertEqual(
            audit["decisions"][0]["strategy_evidence"],
            decision_order["strategy_evidence"],
        )
        self.assertEqual(audit["decisions"][0]["risk_review"], decision_order["risk_review"])
        self.assertEqual(audit["decisions"][0]["agent_review"], decision_order["agent_review"])
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

    def test_agent_decision_audit_fields_fall_back_to_legacy_order_result(self) -> None:
        run = self.service.agent_repo.create_run(
            run_uid="legacy-decision-audit",
            trigger_source="unit_test",
            strategy="dual_low",
            market="cn",
        )
        decision = self.service.agent_repo.record_decision(
            run_id=run["id"],
            sequence=1,
            symbol="600519",
            market="cn",
            action="buy",
            status="planned",
            order_result={
                "strategy_evidence": {"status": "detailed"},
                "position_plan": {"symbol": "600519"},
                "risk_review": {"status": "passed"},
                "agent_review": {"status": "passed"},
                "llm_review": {"status": "warning"},
            },
        )
        with self.service.agent_repo.db.get_session() as session:
            session.execute(
                text(
                    "UPDATE stock_selection_agent_decisions SET "
                    "strategy_evidence_json = NULL, position_plan_json = NULL, "
                    "risk_review_json = NULL, agent_review_json = NULL, llm_review_json = NULL "
                    "WHERE id = :decision_id"
                ),
                {"decision_id": decision["id"]},
            )
            session.commit()

        detail = self.service.agent_repo.get_run_detail("legacy-decision-audit")

        self.assertIsNotNone(detail)
        assert detail is not None
        legacy = detail["decisions"][0]
        self.assertEqual(legacy["strategy_evidence"], {"status": "detailed"})
        self.assertEqual(legacy["position_plan"], {"symbol": "600519"})
        self.assertEqual(legacy["risk_review"], {"status": "passed"})
        self.assertEqual(legacy["agent_review"], {"status": "passed"})
        self.assertEqual(legacy["llm_review"], {"status": "warning"})

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

    def test_candidate_strategy_evidence_keeps_bounded_cross_market_board_alerts(self) -> None:
        settings = self.service.get_settings()

        evidence = self.service._candidate_strategy_evidence(
            {
                "code": "688981",
                "cross_market_strategy": {
                    "strategy_id": CROSS_MARKET_STRATEGY_ID,
                    "theme": "semiconductor",
                    "entry_phase": "opening",
                    "decision": {
                        "action": "buy",
                        "reason": "opening_linked_theme_entry",
                        "score": 82.5,
                        "target_position_pct": 50.0,
                    },
                    "board_technical": {
                        "available": True,
                        "supportive": True,
                        "near_resistance": False,
                        "breakout_confirmed": False,
                        "support_score": 100.0,
                        "pressure_boards": ["芯片"],
                        "reason": "board_support_zone_confirmed",
                        "observed_at": "2026-07-31T01:35:00+00:00",
                        "primary_board": {
                            "name": "半导体",
                            "identifier": "BK1036",
                            "current_level": 1012.5,
                            "technical_windows": [5, 10, 20, 30, 60],
                            "ma5": 1018.0,
                            "ma10": 1020.0,
                            "ma20": 1024.0,
                            "ma30": 1022.0,
                            "ma60": 1010.0,
                            "support_5d": 1005.0,
                            "support_10d": 1000.0,
                            "support_20d": 995.0,
                            "support_30d": 980.0,
                            "support_60d": 960.0,
                            "resistance_5d": 1040.0,
                            "resistance_10d": 1060.0,
                            "resistance_20d": 1080.0,
                            "resistance_30d": 1100.0,
                            "resistance_60d": 1120.0,
                            "nearest_resistance": 1080.0,
                            "nearest_resistance_window": 20,
                            "resistance_distance_pct": 6.67,
                            "support_windows": [60],
                            "near_ma_support_windows": [60],
                            "near_swing_support_windows": [],
                            "support_resonance_count": 1,
                            "multi_period_support": False,
                            "near_ma60_support": True,
                            "supportive": True,
                        },
                        "alerts": [
                            {
                                "board": "半导体",
                                "kind": "near_60d_ma_support",
                                "window_days": 60,
                                "level": 1010.0,
                                "distance_pct": 0.25,
                                "unbounded": "must not leak",
                            },
                            *[
                                {
                                    "board": "半导体",
                                    "kind": f"extra_support_{index}",
                                    "window_days": index,
                                    "level": 1000.0 + index,
                                    "distance_pct": 0.1,
                                }
                                for index in range(11)
                            ],
                            {
                                "board": "芯片",
                                "kind": "near_board_resistance",
                                "window_days": 5,
                                "level": 1040.0,
                                "distance_pct": 1.2,
                            },
                        ],
                        "boards": [{"large": "raw payload must not be copied"}],
                    },
                    "rotation": {
                        "available": True,
                        "tailwind": True,
                        "reason": "defensive_rotation_boards_near_resistance",
                        "pressure_groups": ["bank", "liquor"],
                        "references": [{"large": "raw payload must not be copied"}],
                    },
                },
            },
            settings,
        )

        self.assertEqual(evidence["status"], "detailed")
        self.assertEqual(evidence["evidence_fields"], ["cross_market"])
        cross_market = evidence["cross_market"]
        self.assertEqual(cross_market["theme"], "semiconductor")
        self.assertEqual(cross_market["entry_phase"], "opening")
        self.assertEqual(
            cross_market["board_technical"]["primary_board"]["name"],
            "半导体",
        )
        self.assertTrue(
            cross_market["board_technical"]["primary_board"][
                "near_ma60_support"
            ]
        )
        self.assertEqual(
            cross_market["board_technical"]["primary_board"][
                "technical_windows"
            ],
            [5, 10, 20, 30, 60],
        )
        self.assertEqual(
            cross_market["board_technical"]["primary_board"]["ma5"],
            1018.0,
        )
        bounded_alerts = cross_market["board_technical"]["alerts"]
        self.assertEqual(len(bounded_alerts), 12)
        self.assertEqual(
            bounded_alerts[0],
            {
                "board": "芯片",
                "kind": "near_board_resistance",
                "window_days": 5,
                "level": 1040.0,
                "distance_pct": 1.2,
            },
        )
        self.assertIn(
            {
                "board": "半导体",
                "kind": "near_60d_ma_support",
                "window_days": 60,
                "level": 1010.0,
                "distance_pct": 0.25,
            },
            bounded_alerts,
        )
        self.assertTrue(all("unbounded" not in alert for alert in bounded_alerts))
        self.assertEqual(
            cross_market["board_technical"]["pressure_boards"],
            ["芯片"],
        )
        self.assertEqual(
            cross_market["rotation"]["pressure_groups"],
            ["bank", "liquor"],
        )
        self.assertNotIn("boards", cross_market["board_technical"])
        self.assertNotIn("references", cross_market["rotation"])

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

    def test_calibration_shadow_overrides_are_audited_without_persisting_settings(self) -> None:
        self.service.update_settings({
            "auto_trade_enabled": False,
            "auto_execution_mode": "paper",
            "auto_market": "cn",
            "auto_strategy": "dual_low",
            "auto_max_results": 5,
        })
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {"candidates": [], "warnings": []}
        fake_alphasift.strategies.return_value = {
            "strategies": [{"id": "momentum_quality", "market_scope": ["cn"]}],
        }

        quality_snapshot = {
            "state": "insufficient_evidence",
            "reason": "mature_sample_count_below_threshold",
            "gate_enabled": False,
            "gate_blocked": False,
        }
        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ), patch.object(
            self.service,
            "_cross_run_quality_snapshot",
            return_value=quality_snapshot,
        ) as build_quality, patch.object(
            self.service,
            "_record_last_auto_run",
        ) as record_last_run:
            result = self.service.run_auto_trade_once(
                execution_mode_override="dry_run",
                ignore_auto_trade_enabled=True,
                market_override="cn",
                strategy_override="momentum_quality",
                max_results_override=2,
                calibration_shadow=True,
                trigger_source_override="agent_calibration_shadow",
            )

        fake_alphasift.screen.assert_called_once()
        screen_call = fake_alphasift.screen.call_args.kwargs
        self.assertEqual(screen_call["market"], "cn")
        self.assertEqual(screen_call["strategy"], "momentum_quality")
        self.assertEqual(screen_call["max_results"], 2)
        audit = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        self.assertIsNotNone(audit)
        assert audit is not None
        self.assertTrue(audit["diagnostics"]["calibration_shadow"])
        self.assertEqual(audit["diagnostics"]["execution_mode"], "dry_run")
        self.assertEqual(audit["market"], "cn")
        self.assertEqual(audit["strategy"], "momentum_quality")
        self.assertEqual(audit["trigger_source"], "agent_calibration_shadow")
        self.assertEqual(audit["max_results"], 2)
        self.assertTrue(build_quality.call_args.kwargs["refresh_missing"])
        self.assertEqual(
            build_quality.call_args.kwargs["trigger_source_filter"],
            "agent_calibration_shadow",
        )
        self.assertEqual(
            build_quality.call_args.kwargs["run_status_filter"],
            "completed",
        )
        self.assertIsInstance(
            build_quality.call_args.kwargs["created_from"],
            datetime,
        )
        record_last_run.assert_not_called()
        persisted = self.service.get_settings()
        self.assertEqual(persisted.auto_market, "cn")
        self.assertEqual(persisted.auto_strategy, "dual_low")
        self.assertEqual(persisted.auto_max_results, 5)
        self.assertEqual(persisted.auto_execution_mode, "paper")
        self.assertFalse(persisted.auto_trade_enabled)

    def test_calibration_shadow_rejects_any_order_capable_execution_mode(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires dry_run"):
            self.service.run_auto_trade_once(
                execution_mode_override="paper",
                ignore_auto_trade_enabled=True,
                calibration_shadow=True,
            )

        with self.assertRaisesRegex(ValueError, "requires ignore_auto_trade_enabled"):
            self.service.run_auto_trade_once(
                execution_mode_override="dry_run",
                calibration_shadow=True,
            )

    def test_calibration_shadow_rejects_strategy_market_scope_mismatch_before_run(self) -> None:
        fake_alphasift = MagicMock()
        fake_alphasift.strategies.return_value = {
            "strategies": [{"id": "dual_low", "market_scope": ["cn"]}],
        }
        with patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ), self.assertRaisesRegex(ValueError, "does not support market 'us'"):
            self.service.run_auto_trade_once(
                execution_mode_override="dry_run",
                ignore_auto_trade_enabled=True,
                market_override="us",
                strategy_override="dual_low",
                calibration_shadow=True,
                trigger_source_override="agent_calibration_shadow",
            )
        self.assertEqual(self.service.agent_repo.list_recent_runs(limit=10), [])

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

    def test_cross_market_vnpy_submit_rejects_dsa_sim_fixed_delay_matcher(self) -> None:
        installed = _install_fake_vnpy_modules()
        main_engine = _FakeMainEngine()
        main_engine.get_gateway = MagicMock(
            return_value=types.SimpleNamespace(
                get_state_snapshot=lambda: {
                    "connected": True,
                    "matching": {"mode": "fixed_delay_limit"},
                }
            )
        )
        service = VnpyPaperTradingService(
            config_path=self.config_path,
            vnpy_main_engine=main_engine,
        )
        service.update_settings({"vnpy_gateway_name": "DSA_SIM"})
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
                source="cross_market_auto_entry",
            )
        finally:
            _restore_modules(installed)

        self.assertFalse(result["accepted"])
        self.assertEqual(
            result["reason"],
            "cross_market_next_minute_execution_unavailable",
        )
        self.assertEqual(
            result["raw"]["vnpy_execution"]["reason"],
            "dsa_sim_matching_mode_incompatible",
        )
        self.assertEqual(main_engine.calls, [])

    def test_cross_market_vnpy_submit_accepts_dsa_sim_next_minute_matcher(self) -> None:
        installed = _install_fake_vnpy_modules()
        main_engine = _FakeMainEngine()
        main_engine.get_gateway = MagicMock(
            return_value=types.SimpleNamespace(
                get_state_snapshot=lambda: {
                    "connected": True,
                    "matching": {"mode": "next_minute_vwap"},
                }
            )
        )
        service = VnpyPaperTradingService(
            config_path=self.config_path,
            vnpy_main_engine=main_engine,
        )
        service.update_settings({"vnpy_gateway_name": "DSA_SIM"})
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
                source="cross_market_auto_entry",
            )
        finally:
            _restore_modules(installed)

        self.assertTrue(result["accepted"])
        self.assertEqual(result["status"], "submitted")
        self.assertEqual(len(main_engine.calls), 1)

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
                "matching_mode": "fixed_delay_limit",
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
                if (
                    detail
                    and detail["trade_plans"][0]["status"] == "filled"
                    and detail["decisions"][0]["status"] == "filled"
                ):
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
    def test_cross_market_next_minute_fill_closes_service_ledger_loop(self) -> None:
        from vnpy.event import EventEngine
        from vnpy.trader.engine import MainEngine

        from src.services.vnpy_simulated_gateway import DsaSimulatedGateway

        now = datetime.now(timezone.utc)
        quotes = iter(
            [
                SimpleNamespace(
                    provider_timestamp=(now - timedelta(seconds=60)).isoformat(),
                    source="tencent",
                    price=10.0,
                    volume=100_000,
                    amount=1_000_000,
                    pre_close=9.8,
                    bid_price=9.99,
                    ask_price=10.01,
                ),
                SimpleNamespace(
                    provider_timestamp=now.isoformat(),
                    source="tencent",
                    price=10.1,
                    volume=110_000,
                    amount=1_100_500,
                    pre_close=9.8,
                    bid_price=10.04,
                    ask_price=10.06,
                ),
            ]
        )
        event_engine = EventEngine()
        main_engine = MainEngine(event_engine)
        main_engine.add_gateway(DsaSimulatedGateway, "DSA_SIM")
        main_engine.connect(
            {
                "matching_mode": "next_minute_vwap",
                "fill_delay_ms": 250,
                "quote_provider": lambda _symbol: next(quotes),
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
                "auto_strategy": CROSS_MARKET_STRATEGY_ID,
                "auto_initial_cash": 100000,
            }
        )
        settings = service.get_settings()
        account = service.ensure_account(settings=settings)
        run = service.agent_repo.create_run(
            run_uid="cross-market-next-minute-loop",
            trigger_source="vnpy_paper_auto",
            strategy=CROSS_MARKET_STRATEGY_ID,
            market="cn",
            settings={},
            diagnostics={},
        )
        candidate = {
            "code": "600519",
            "name": "integration stock",
            "score": 80,
            "price": 10.0,
            "strategy_theme": "semiconductor",
        }

        try:
            order = service._submit_vnpy_bridge_order(
                settings=settings,
                account_id=int(account["id"]),
                symbol="600519",
                side="buy",
                market="cn",
                quantity=100,
                price=10.2,
                cash_amount=1020,
                source="cross_market_auto_entry",
                raw={
                    "cross_market_strategy": {
                        "strategy_id": CROSS_MARKET_STRATEGY_ID,
                        "strategy_theme": "semiconductor",
                    },
                    "execution_costs": {"instrument_type": "stock"},
                },
            )
            self.assertTrue(order["accepted"])
            service._record_agent_decision(
                run_id=int(run["id"]),
                sequence=1,
                candidate=candidate,
                symbol="600519",
                settings=settings,
                action="buy",
                order=order,
                reason=None,
                risk_flags=[],
            )

            deadline = time.monotonic() + 5.0
            detail = None
            while time.monotonic() < deadline:
                detail = service.agent_repo.get_run_detail(run["run_uid"])
                plan_raw = (
                    detail["trade_plans"][0].get("order_result", {}).get("raw", {})
                    if detail and detail.get("trade_plans")
                    else {}
                )
                fill_trades = (
                    plan_raw.get("fill_sync", {}).get("trades", [])
                    if isinstance(plan_raw.get("fill_sync"), dict)
                    else []
                )
                if (
                    detail
                    and detail["trade_plans"][0]["status"] == "filled"
                    and detail["decisions"][0]["status"] == "filled"
                    and fill_trades
                    and isinstance(fill_trades[0].get("execution"), dict)
                ):
                    break
                time.sleep(0.05)

            self.assertIsNotNone(detail)
            assert detail is not None
            self.assertEqual(detail["trade_plans"][0]["status"], "filled")
            self.assertEqual(detail["decisions"][0]["status"], "filled")
            trades = service.portfolio.list_trade_events(
                account_id=int(account["id"]),
                page=1,
            )
            self.assertEqual(len(trades["items"]), 1)
            fill = trades["items"][0]
            self.assertEqual(fill["symbol"], "600519")
            self.assertEqual(fill["quantity"], 100.0)
            self.assertGreater(fill["price"], 10.07)
            self.assertGreaterEqual(fill["fee"], 5.0)
            self.assertEqual(fill["tax"], 0.0)
            snapshot = service.portfolio.get_portfolio_snapshot(
                account_id=int(account["id"]),
                persist=False,
            )
            account_snapshot = snapshot["accounts"][0]
            self.assertEqual(account_snapshot["positions"][0]["quantity"], 100.0)
            self.assertGreaterEqual(account_snapshot["fee_total"], 5.0)
            report = service.get_cross_market_campaign_report({
                "ready": False,
                "paper_observation": {
                    "campaign_active": True,
                    "campaign_started_at": (
                        now - timedelta(minutes=1)
                    ).isoformat(),
                    "campaign_account_id": int(account["id"]),
                    "current_account_id": int(account["id"]),
                    "account_matches_current": True,
                    "initial_equity": 100000.0,
                    "required_trading_days": 30,
                    "remaining_trading_days": 29,
                    "degraded_trading_days": 0,
                },
            })
            self.assertIn(
                "execution",
                report["transactions"][0],
                msg=(
                    f"detail_plan={detail['trade_plans'][0]!r}; "
                    f"listed_plans={service.agent_repo.list_trade_plans(limit=10)!r}; "
                    f"report_transaction={report['transactions'][0]!r}"
                ),
            )
            transaction_execution = report["transactions"][0]["execution"]
            self.assertEqual(transaction_execution["mode"], "next_minute_vwap")
            self.assertEqual(transaction_execution["reference_price"], 10.05)
            self.assertGreater(transaction_execution["actual_slippage_bps"], 0.0)
            slippage = report["performance"]["slippage_metrics"]
            self.assertEqual(slippage["covered_trade_count"], 1)
            self.assertEqual(slippage["coverage_pct"], 100.0)
            self.assertGreater(slippage["total_adverse_slippage_cost"], 0.0)
            metrics = report["performance"]["trade_metrics"]
            self.assertGreater(
                metrics["total_all_in_trading_cost"],
                metrics["total_transaction_cost"],
            )
            self.assertNotIn(
                "campaign_trade_slippage_evidence_incomplete",
                report["warnings"],
            )
        finally:
            bridge.unregister()
            main_engine.close()

    @unittest.skipUnless(importlib.util.find_spec("vnpy"), "optional vn.py runtime is not installed")
    def test_bse_next_minute_fill_closes_cn_service_ledger_loop(self) -> None:
        from vnpy.event import EventEngine
        from vnpy.trader.engine import MainEngine

        from src.services.vnpy_simulated_gateway import DsaSimulatedGateway

        now = datetime.now(timezone.utc)
        quotes = iter(
            [
                SimpleNamespace(
                    provider_timestamp=(now - timedelta(seconds=60)).isoformat(),
                    source="tencent",
                    price=10.0,
                    volume=100_000,
                    amount=1_000_000,
                    pre_close=9.8,
                    bid_price=9.99,
                    ask_price=10.01,
                ),
                SimpleNamespace(
                    provider_timestamp=now.isoformat(),
                    source="tencent",
                    price=10.1,
                    volume=110_000,
                    amount=1_100_500,
                    pre_close=9.8,
                    bid_price=10.04,
                    ask_price=10.06,
                ),
            ]
        )
        event_engine = EventEngine()
        main_engine = MainEngine(event_engine)
        main_engine.add_gateway(DsaSimulatedGateway, "DSA_SIM")
        main_engine.connect(
            {
                "matching_mode": "next_minute_vwap",
                "fill_delay_ms": 250,
                "quote_provider": lambda _symbol: next(quotes),
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
                "auto_strategy": CROSS_MARKET_STRATEGY_ID,
                "auto_initial_cash": 100000,
            }
        )
        settings = service.get_settings()
        account = service.ensure_account(settings=settings)
        run = service.agent_repo.create_run(
            run_uid="cross-market-bse-next-minute-loop",
            trigger_source="vnpy_paper_auto",
            strategy=CROSS_MARKET_STRATEGY_ID,
            market="cn",
            settings={},
            diagnostics={},
        )
        candidate = {
            "code": "920045",
            "name": "bse integration stock",
            "score": 80,
            "price": 10.0,
            "strategy_theme": "cpo",
        }

        try:
            order = service._submit_vnpy_bridge_order(
                settings=settings,
                account_id=int(account["id"]),
                symbol="920045",
                side="buy",
                market="cn",
                quantity=105,
                price=10.2,
                cash_amount=1071,
                source="cross_market_auto_entry",
                raw={
                    "cross_market_strategy": {
                        "strategy_id": CROSS_MARKET_STRATEGY_ID,
                        "strategy_theme": "cpo",
                    },
                    "execution_costs": {"instrument_type": "stock"},
                },
            )
            self.assertTrue(order["accepted"])
            self.assertEqual(order["raw"]["order_request_payload"]["exchange"], "BSE")
            service._record_agent_decision(
                run_id=int(run["id"]),
                sequence=1,
                candidate=candidate,
                symbol="920045",
                settings=settings,
                action="buy",
                order=order,
                reason=None,
                risk_flags=[],
            )

            deadline = time.monotonic() + 5.0
            detail = None
            trades = {"items": []}
            while time.monotonic() < deadline:
                detail = service.agent_repo.get_run_detail(run["run_uid"])
                trades = service.portfolio.list_trade_events(
                    account_id=int(account["id"]),
                    page=1,
                )
                if (
                    detail
                    and detail["trade_plans"][0]["status"] == "filled"
                    and trades["items"]
                ):
                    break
                time.sleep(0.05)

            self.assertIsNotNone(detail)
            assert detail is not None
            self.assertEqual(detail["trade_plans"][0]["status"], "filled")
            self.assertEqual(detail["decisions"][0]["status"], "filled")
            self.assertEqual(len(trades["items"]), 1)
            fill = trades["items"][0]
            self.assertEqual(fill["symbol"], "920045")
            self.assertEqual(fill["market"], "cn")
            self.assertEqual(fill["quantity"], 105.0)
            self.assertGreaterEqual(fill["fee"], 5.0)
            snapshot = service.portfolio.get_portfolio_snapshot(
                account_id=int(account["id"]),
                persist=False,
            )
            position = snapshot["accounts"][0]["positions"][0]
            self.assertEqual(position["symbol"], "920045")
            self.assertEqual(position["market"], "cn")
            self.assertEqual(position["quantity"], 105.0)
        finally:
            bridge.unregister()
            main_engine.close()

    @unittest.skipUnless(importlib.util.find_spec("vnpy"), "optional vn.py runtime is not installed")
    def test_cross_market_two_cost_reserved_slots_fill_without_cash_deficit(self) -> None:
        from vnpy.event import EventEngine
        from vnpy.trader.engine import MainEngine

        from src.services.vnpy_simulated_gateway import DsaSimulatedGateway

        now = datetime.now(timezone.utc)

        def quote_pair():
            return iter(
                [
                    SimpleNamespace(
                        provider_timestamp=(now - timedelta(seconds=60)).isoformat(),
                        source="tencent",
                        price=10.0,
                        volume=100_000,
                        amount=1_000_000,
                        pre_close=9.8,
                        bid_price=9.99,
                        ask_price=10.01,
                    ),
                    SimpleNamespace(
                        provider_timestamp=now.isoformat(),
                        source="tencent",
                        price=10.0,
                        volume=200_000,
                        amount=2_000_000,
                        pre_close=9.8,
                        bid_price=9.99,
                        ask_price=10.01,
                    ),
                ]
            )

        quotes = {
            "600777": quote_pair(),
            "600778": quote_pair(),
        }
        event_engine = EventEngine()
        main_engine = MainEngine(event_engine)
        main_engine.add_gateway(DsaSimulatedGateway, "DSA_SIM")
        main_engine.connect(
            {
                "initial_balance": 100000,
                "matching_mode": "next_minute_vwap",
                "fill_delay_ms": 250,
                "quote_provider": lambda symbol: next(quotes[symbol]),
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
                "initial_cash": 100000,
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
                "auto_execution_mode": "vnpy_paper",
                "vnpy_gateway_name": "DSA_SIM",
                "auto_strategy": CROSS_MARKET_STRATEGY_ID,
            }
        )
        settings = service._apply_cross_market_strategy_settings(service.get_settings())
        account = service.ensure_account(settings=settings)
        run = service.agent_repo.create_run(
            run_uid="cross-market-two-cost-reserved-slots",
            trigger_source="vnpy_paper_auto",
            strategy=CROSS_MARKET_STRATEGY_ID,
            market="cn",
            settings={},
            diagnostics={},
        )
        fee_schedule = TradeFeeSchedule()
        signal_notional_cap = fee_schedule.max_buy_notional_for_cash_budget(
            cash_budget=50000,
            instrument_type="stock",
        )

        try:
            for sequence, symbol in enumerate(("600777", "600778"), start=1):
                quantity = service._resolve_order_quantity(
                    symbol=symbol,
                    market="cn",
                    side="buy",
                    quantity=None,
                    cash_amount=signal_notional_cap,
                    price=10.0,
                )
                self.assertEqual(quantity, 4999.0)
                candidate = {
                    "code": symbol,
                    "name": f"integration stock {sequence}",
                    "score": 80 - sequence,
                    "price": 10.0,
                    "strategy_theme": "cpo",
                }
                order = service._submit_vnpy_bridge_order(
                    settings=settings,
                    account_id=int(account["id"]),
                    symbol=symbol,
                    side="buy",
                    market="cn",
                    quantity=quantity,
                    price=10.0,
                    cash_amount=quantity * 10.0,
                    source="cross_market_auto_entry",
                    raw={
                        "cross_market_strategy": {
                            "strategy_id": CROSS_MARKET_STRATEGY_ID,
                            "strategy_theme": "cpo",
                        },
                        "cross_market_execution_budget": {
                            "cash_allocation_cap": 50000.0,
                            "signal_notional_cap": signal_notional_cap,
                        },
                        "execution_costs": {"instrument_type": "stock"},
                    },
                )
                self.assertTrue(order["accepted"])
                service._record_agent_decision(
                    run_id=int(run["id"]),
                    sequence=sequence,
                    candidate=candidate,
                    symbol=symbol,
                    settings=settings,
                    action="buy",
                    order=order,
                    reason=None,
                    risk_flags=[],
                )

            deadline = time.monotonic() + 5.0
            detail = None
            while time.monotonic() < deadline:
                detail = service.agent_repo.get_run_detail(run["run_uid"])
                if (
                    detail
                    and len(detail["trade_plans"]) == 2
                    and all(item["status"] == "filled" for item in detail["trade_plans"])
                    and all(item["status"] == "filled" for item in detail["decisions"])
                ):
                    break
                time.sleep(0.05)

            self.assertIsNotNone(detail)
            assert detail is not None
            self.assertEqual(
                [item["status"] for item in detail["trade_plans"]],
                ["filled", "filled"],
            )
            trades = service.portfolio.list_trade_events(
                account_id=int(account["id"]),
                page=1,
            )["items"]
            self.assertEqual(len(trades), 2)
            self.assertEqual({item["symbol"] for item in trades}, {"600777", "600778"})
            self.assertTrue(all(item["quantity"] == 4999.0 for item in trades))
            self.assertTrue(all(item["fee"] > 5.0 for item in trades))
            snapshot = service.portfolio.get_portfolio_snapshot(
                account_id=int(account["id"]),
                persist=False,
            )["accounts"][0]
            expected_fees = sum(float(item["fee"]) for item in trades)
            self.assertGreaterEqual(snapshot["total_cash"], 0.0)
            self.assertAlmostEqual(
                snapshot["total_cash"],
                100000.0 - 2 * 49990.0 - expected_fees,
                places=6,
            )
            self.assertAlmostEqual(snapshot["fee_total"], expected_fees, places=6)
        finally:
            bridge.unregister()
            main_engine.close()

    @unittest.skipUnless(importlib.util.find_spec("vnpy"), "optional vn.py runtime is not installed")
    def test_cross_market_next_minute_sell_books_tax_and_t_plus_one_position(self) -> None:
        from vnpy.event import EventEngine
        from vnpy.trader.engine import MainEngine

        from src.services.vnpy_simulated_gateway import DsaSimulatedGateway

        now = datetime.now(timezone.utc)
        quotes = iter(
            [
                SimpleNamespace(
                    provider_timestamp=(now - timedelta(seconds=60)).isoformat(),
                    source="tencent",
                    price=10.0,
                    volume=100_000,
                    amount=1_000_000,
                    pre_close=10.0,
                    bid_price=9.99,
                    ask_price=10.01,
                ),
                SimpleNamespace(
                    provider_timestamp=now.isoformat(),
                    source="tencent",
                    price=9.9,
                    volume=110_000,
                    amount=1_099_500,
                    pre_close=10.0,
                    bid_price=9.89,
                    ask_price=9.91,
                ),
            ]
        )
        event_engine = EventEngine()
        main_engine = MainEngine(event_engine)
        main_engine.add_gateway(DsaSimulatedGateway, "DSA_SIM")
        main_engine.connect(
            {
                "matching_mode": "next_minute_vwap",
                "fill_delay_ms": 250,
                "quote_provider": lambda _symbol: next(quotes),
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
                "auto_strategy": CROSS_MARKET_STRATEGY_ID,
                "auto_initial_cash": 100000,
            }
        )
        settings = service.get_settings()
        account = service.ensure_account(settings=settings)
        account_id = int(account["id"])
        service.portfolio.record_trade(
            account_id=account_id,
            symbol="600519",
            trade_date=date.today() - timedelta(days=7),
            side="buy",
            quantity=100,
            price=9.5,
            market="cn",
            currency="CNY",
            trade_uid="cross-market-prior-position",
            dedup_hash=service._dedup_hash("cross-market-prior-position"),
            note="source=cross_market_auto_entry",
        )
        self.assertEqual(
            service.portfolio.get_sellable_quantity(
                account_id=account_id,
                symbol="600519",
                trade_date=date.today(),
                market="cn",
            ),
            100.0,
        )
        run = service.agent_repo.create_run(
            run_uid="cross-market-next-minute-sell-loop",
            trigger_source="cross_market_intraday_sell_monitor",
            strategy=CROSS_MARKET_STRATEGY_ID,
            market="cn",
            settings={},
            diagnostics={},
        )
        candidate = {
            "code": "600519",
            "name": "integration stock",
            "score": -80,
            "price": 10.0,
            "position_quantity": 100.0,
            "sellable_quantity": 100.0,
            "strategy_theme": "semiconductor",
        }

        try:
            order = service._submit_vnpy_bridge_order(
                settings=settings,
                account_id=account_id,
                symbol="600519",
                side="sell",
                market="cn",
                quantity=100,
                price=9.8,
                cash_amount=980,
                source="cross_market_auto_exit",
                raw={
                    "cross_market_strategy": {
                        "strategy_id": CROSS_MARKET_STRATEGY_ID,
                        "strategy_theme": "semiconductor",
                    },
                    "execution_costs": {"instrument_type": "stock"},
                },
            )
            self.assertTrue(order["accepted"])
            service._record_agent_decision(
                run_id=int(run["id"]),
                sequence=1,
                candidate=candidate,
                symbol="600519",
                settings=settings,
                action="sell",
                order=order,
                reason=None,
                risk_flags=[],
                side="sell",
            )

            deadline = time.monotonic() + 5.0
            detail = None
            while time.monotonic() < deadline:
                detail = service.agent_repo.get_run_detail(run["run_uid"])
                if (
                    detail
                    and detail["trade_plans"][0]["status"] == "filled"
                    and detail["decisions"][0]["status"] == "filled"
                ):
                    break
                time.sleep(0.05)

            self.assertIsNotNone(detail)
            assert detail is not None
            self.assertEqual(detail["trade_plans"][0]["status"], "filled")
            self.assertEqual(detail["decisions"][0]["status"], "filled")
            trades = service.portfolio.list_trade_events(account_id=account_id, page=1)
            self.assertEqual(len(trades["items"]), 2)
            fill = next(item for item in trades["items"] if item["side"] == "sell")
            self.assertEqual(fill["quantity"], 100.0)
            self.assertGreater(fill["price"], 9.8)
            self.assertGreaterEqual(fill["fee"], 5.0)
            self.assertGreater(fill["tax"], 0.0)
            self.assertAlmostEqual(
                fill["tax"],
                fill["quantity"] * fill["price"] * 0.0005,
                places=6,
            )
            self.assertEqual(
                service.portfolio.get_sellable_quantity(
                    account_id=account_id,
                    symbol="600519",
                    trade_date=date.today(),
                    market="cn",
                ),
                0.0,
            )
            snapshot = service.portfolio.get_portfolio_snapshot(
                account_id=account_id,
                persist=False,
            )
            account_snapshot = snapshot["accounts"][0]
            self.assertEqual(account_snapshot["positions"], [])
            self.assertGreaterEqual(account_snapshot["fee_total"], 5.0)
            self.assertGreater(account_snapshot["tax_total"], 0.0)
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
                "matching_mode": "fixed_delay_limit",
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

    @unittest.skipUnless(importlib.util.find_spec("vnpy"), "optional vn.py runtime is not installed")
    def test_builtin_simulated_gateway_isolates_mixed_agent_results(self) -> None:
        from vnpy.event import EventEngine
        from vnpy.trader.engine import MainEngine

        from src.services.vnpy_simulated_gateway import DsaSimulatedGateway

        event_engine = EventEngine()
        main_engine = MainEngine(event_engine)
        main_engine.add_gateway(DsaSimulatedGateway, "DSA_SIM")
        main_engine.connect(
            {
                "fill_delay_ms": 50,
                "matching_mode": "fixed_delay_limit",
                "reject_every_nth_order": 2,
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
                "auto_max_results": 3,
                "auto_cash_per_order": 1200,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "quality_status": "ok",
            "candidates": [
                {
                    "code": symbol,
                    "name": f"candidate-{index}",
                    "score": 90 - index,
                    "price": 10.0,
                    "amount": 200000000,
                }
                for index, symbol in enumerate(("600519", "000001", "300750"), start=1)
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

            self.assertEqual(len(result["orders"]), 3)
            deadline = time.monotonic() + 5.0
            detail = None
            expected_statuses = ["filled", "failed", "filled"]
            while time.monotonic() < deadline:
                detail = service.agent_repo.get_run_detail(result["agent_run_uid"])
                if (
                    detail
                    and [item["status"] for item in detail["trade_plans"]]
                    == expected_statuses
                    and [item["status"] for item in detail["decisions"]]
                    == expected_statuses
                ):
                    break
                time.sleep(0.05)

            self.assertIsNotNone(detail)
            assert detail is not None
            plans = detail["trade_plans"]
            decisions = detail["decisions"]
            self.assertEqual([item["status"] for item in plans], expected_statuses)
            self.assertEqual([item["status"] for item in decisions], expected_statuses)
            self.assertEqual(plans[1]["skip_reason"], "vnpy_order_rejected")
            self.assertEqual(
                plans[1]["order_result"]["message"],
                "simulated_configured_rejection",
            )
            self.assertEqual(detail["submitted_count"], 2)
            self.assertEqual(detail["skipped_count"], 1)

            account_id = int(service.get_settings().account_id)
            trades = service.portfolio.list_trade_events(account_id=account_id, page=1)
            self.assertEqual(len(trades["items"]), 2)
            self.assertEqual(
                {item["symbol"] for item in trades["items"]},
                {"600519", "300750"},
            )
            self.assertEqual(
                len({item["trade_uid"] for item in trades["items"]}),
                2,
            )
            gateway = main_engine.get_gateway("DSA_SIM")
            self.assertIsNotNone(gateway)
            assert gateway is not None
            state = gateway.get_state_snapshot()
            self.assertEqual(state["order_count"], 3)
            self.assertEqual(state["trade_count"], 2)
            self.assertEqual(state["active_order_ids"], [])
        finally:
            bridge.unregister()
            main_engine.close()

    @unittest.skipUnless(importlib.util.find_spec("vnpy"), "optional vn.py runtime is not installed")
    def test_builtin_simulated_gateway_isolates_missing_candidate_quote(self) -> None:
        from vnpy.event import EventEngine
        from vnpy.trader.engine import MainEngine

        from src.services.vnpy_simulated_gateway import DsaSimulatedGateway

        class SelectiveQuoteManager(_FakeDataFetcherManager):
            def get_realtime_quote(self, symbol: str):
                if symbol == "000001":
                    raise RuntimeError("configured_quote_failure")
                return super().get_realtime_quote(symbol)

        event_engine = EventEngine()
        main_engine = MainEngine(event_engine)
        main_engine.add_gateway(DsaSimulatedGateway, "DSA_SIM")
        main_engine.connect(
            {
                "fill_delay_ms": 50,
                "matching_mode": "fixed_delay_limit",
                "duplicate_trade_event_count": 2,
            },
            "DSA_SIM",
        )
        service = VnpyPaperTradingService(
            data_fetcher_manager=SelectiveQuoteManager(price=10.0),
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
                "auto_max_results": 3,
                "auto_cash_per_order": 1200,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "quality_status": "ok",
            "candidates": [
                {
                    "code": "600519",
                    "name": "priced-candidate-1",
                    "score": 90,
                    "price": 10.0,
                    "amount": 200000000,
                },
                {
                    "code": "000001",
                    "name": "missing-quote-candidate",
                    "score": 89,
                    "price": None,
                    "amount": 200000000,
                },
                {
                    "code": "300750",
                    "name": "priced-candidate-3",
                    "score": 88,
                    "price": 10.0,
                    "amount": 200000000,
                },
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

            self.assertEqual(
                [item["status"] for item in result["orders"]],
                ["submitted", "skipped", "submitted"],
            )
            self.assertEqual(result["orders"][1]["reason"], "price_unavailable")
            deadline = time.monotonic() + 5.0
            detail = None
            expected_statuses = ["filled", "skipped", "filled"]
            while time.monotonic() < deadline:
                detail = service.agent_repo.get_run_detail(result["agent_run_uid"])
                if (
                    detail
                    and [item["status"] for item in detail["trade_plans"]]
                    == expected_statuses
                    and [item["status"] for item in detail["decisions"]]
                    == expected_statuses
                ):
                    break
                time.sleep(0.05)

            self.assertIsNotNone(detail)
            assert detail is not None
            self.assertEqual(detail["submitted_count"], 2)
            self.assertEqual(detail["skipped_count"], 1)
            skipped_plan = detail["trade_plans"][1]
            self.assertEqual(skipped_plan["skip_reason"], "price_unavailable")
            self.assertEqual(
                skipped_plan["order_result"]["raw"]["price_resolution"],
                {"requested_price": None, "source": "unavailable"},
            )
            self.assertEqual(
                skipped_plan["order_result"]["raw"]["name"],
                "missing-quote-candidate",
            )

            account_id = int(service.get_settings().account_id)
            trades = service.portfolio.list_trade_events(account_id=account_id, page=1)
            self.assertEqual(len(trades["items"]), 2)
            self.assertEqual(
                {item["symbol"] for item in trades["items"]},
                {"600519", "300750"},
            )
            gateway = main_engine.get_gateway("DSA_SIM")
            self.assertIsNotNone(gateway)
            assert gateway is not None
            state = gateway.get_state_snapshot()
            self.assertEqual(state["order_count"], 2)
            self.assertEqual(state["trade_count"], 2)
            self.assertEqual(state["active_order_ids"], [])
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
        initial_audit = self.service.agent_repo.get_run_detail(
            run_result["agent_run_uid"]
        )
        self.assertIsNotNone(initial_audit)
        assert initial_audit is not None
        submitted_plan = initial_audit["trade_plans"][0]
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
        self.assertEqual(
            trades["items"][0]["trade_uid"],
            f"vnpy-trade-{self.service._dedup_hash('2026-07-03:SIM.T1')}",
        )
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
        stale_update = (
            self.service.agent_repo.update_trade_plan_and_decision_execution(
                plan_uid=str(submitted_plan["plan_uid"]),
                status="submitted",
                submitted_quantity=100,
                submitted_price=10.0,
                trade_id=None,
                skip_reason=None,
                order_result=run_result["orders"][0],
                expected_updated_at=submitted_plan["updated_at"],
            )
        )
        self.assertIsNotNone(stale_update)
        assert stale_update is not None
        self.assertTrue(stale_update["stale_write_skipped"])
        self.assertEqual(stale_update["trade_plan"]["status"], "filled")
        self.assertEqual(stale_update["decision"]["status"], "filled")
        decision = audit["decisions"][0]
        self.assertEqual(decision["position_plan"]["symbol"], "600519")
        self.assertEqual(decision["risk_review"]["status"], "passed")
        self.assertEqual(decision["agent_review"]["reviewer"], "rule_agent_v1")
        self.assertEqual(
            decision["order_result"]["strategy_evidence"],
            decision["strategy_evidence"],
        )
        with self.service.agent_repo.db.get_session() as session:
            persisted = session.execute(
                text(
                    "SELECT strategy_evidence_json, position_plan_json, "
                    "risk_review_json, agent_review_json, llm_review_json "
                    "FROM stock_selection_agent_decisions WHERE id = :decision_id"
                ),
                {"decision_id": decision["id"]},
            ).mappings().one()
        self.assertEqual(json.loads(persisted["position_plan_json"])["symbol"], "600519")
        self.assertEqual(json.loads(persisted["risk_review_json"])["status"], "passed")
        self.assertEqual(json.loads(persisted["agent_review_json"])["reviewer"], "rule_agent_v1")
        self.assertEqual(json.loads(persisted["llm_review_json"]), {})

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

        self.service.agent_repo.update_decision_execution(
            decision_id=int(decision["id"]),
            status="submitted",
            reason=None,
            quantity=100,
            price=10.2,
            trade_id=None,
            order_result=decision["order_result"],
        )
        repaired = self.service.sync_vnpy_trade_callback(
            vt_orderid="SIM.1",
            vt_tradeid="SIM.T1",
            symbol="600519",
            side="buy",
            market="cn",
            quantity=100,
            price=10.2,
        )
        repaired_audit = self.service.agent_repo.get_run_detail(
            run_result["agent_run_uid"]
        )
        self.assertTrue(repaired["raw"]["duplicate_callback"])
        assert repaired_audit is not None
        self.assertEqual(repaired_audit["trade_plans"][0]["status"], "filled")
        self.assertEqual(repaired_audit["decisions"][0]["status"], "filled")
        self.assertEqual(
            repaired_audit["decisions"][0]["trade_id"],
            callback["trade_id"],
        )

    def test_vnpy_order_callback_waits_for_trade_sync_and_preserves_execution(self) -> None:
        run = self.service.agent_repo.create_run(
            run_uid="serialized-vnpy-callback-run",
            trigger_source="vnpy_paper_auto",
            strategy=CROSS_MARKET_STRATEGY_ID,
            market="cn",
            settings={},
            diagnostics={},
        )
        self.service.agent_repo.record_trade_plan(
            plan_uid="serialized-vnpy-callback-plan",
            run_id=int(run["id"]),
            decision_id=None,
            symbol="600519",
            market="cn",
            side="buy",
            status="submitted",
            execution_mode="vnpy_paper",
            planned_cash_amount=1_020,
            planned_quantity=100,
            planned_price=10.2,
            submitted_quantity=100,
            submitted_price=10.2,
            order_result={
                "status": "submitted",
                "raw": {
                    "vt_orderid": "DSA_SIM.SERIAL",
                    "cross_market_strategy": {
                        "strategy_id": CROSS_MARKET_STRATEGY_ID,
                    },
                    "execution_costs": {"instrument_type": "stock"},
                },
            },
        )
        trade_update_entered = threading.Event()
        release_trade_update = threading.Event()
        original_update = (
            self.service.agent_repo.update_trade_plan_and_decision_execution
        )
        results: dict[str, object] = {}

        def delayed_update(**kwargs):
            if kwargs.get("status") == "filled":
                trade_update_entered.set()
                if not release_trade_update.wait(timeout=2):
                    raise TimeoutError("trade update release was not signalled")
            return original_update(**kwargs)

        def sync_trade() -> None:
            try:
                results["trade"] = self.service.sync_vnpy_trade_callback(
                    vt_orderid="DSA_SIM.SERIAL",
                    vt_tradeid="DSA_SIM.SERIAL.T1",
                    quantity=100,
                    price=10.1,
                    trade_date=date(2026, 7, 3),
                    raw={
                        "dsa_execution_mode": "next_minute_vwap",
                        "dsa_reference_price": 10.05,
                        "dsa_slippage_bps": 49.751244,
                    },
                )
            except Exception as exc:  # noqa: BLE001 - surfaced in the main test thread.
                results["trade_error"] = exc

        def sync_order() -> None:
            try:
                results["order"] = self.service.sync_vnpy_order_callback(
                    vt_orderid="DSA_SIM.SERIAL",
                    status="alltraded",
                    volume=100,
                    traded=100,
                    price=10.2,
                    raw={"reconciled_from_main_engine": True},
                )
            except Exception as exc:  # noqa: BLE001 - surfaced in the main test thread.
                results["order_error"] = exc

        with patch.object(
            self.service.agent_repo,
            "update_trade_plan_and_decision_execution",
            side_effect=delayed_update,
        ):
            trade_thread = threading.Thread(target=sync_trade)
            order_thread = threading.Thread(target=sync_order)
            trade_thread.start()
            self.assertTrue(trade_update_entered.wait(timeout=2))
            order_thread.start()
            time.sleep(0.05)
            self.assertTrue(order_thread.is_alive())
            self.assertNotIn("order", results)
            release_trade_update.set()
            trade_thread.join(timeout=2)
            order_thread.join(timeout=2)

        self.assertFalse(trade_thread.is_alive())
        self.assertFalse(order_thread.is_alive())
        self.assertNotIn("trade_error", results)
        self.assertNotIn("order_error", results)
        self.assertEqual(results["trade"]["status"], "filled")
        self.assertEqual(results["order"]["status"], "filled")
        plan = self.service.agent_repo.get_trade_plan(
            "serialized-vnpy-callback-plan"
        )
        assert plan is not None
        fill = plan["order_result"]["raw"]["fill_sync"]["trades"][0]
        self.assertEqual(fill["execution"]["mode"], "next_minute_vwap")
        self.assertEqual(fill["execution"]["reference_price"], 10.05)

    def test_vnpy_trade_id_can_repeat_on_a_later_trade_date(self) -> None:
        account = self.service.ensure_account()
        account_id = int(account["id"])
        self.service.portfolio.record_trade(
            account_id=account_id,
            symbol="000001",
            trade_date=date(2026, 7, 20),
            side="buy",
            quantity=100,
            price=10,
            market="cn",
            currency="CNY",
            trade_uid="vnpy-trade-DSA_SIM.1",
            dedup_hash=self.service._dedup_hash("vnpy-trade:DSA_SIM.1"),
        )
        run = self.service.agent_repo.create_run(
            run_uid="reused-vnpy-trade-id-run",
            trigger_source="vnpy_paper_auto",
            strategy="dual_low",
            market="cn",
            settings={},
            diagnostics={},
        )
        self.service.agent_repo.record_trade_plan(
            plan_uid="reused-vnpy-trade-id-plan",
            run_id=int(run["id"]),
            decision_id=None,
            symbol="000776",
            market="cn",
            side="buy",
            status="submitted",
            execution_mode="vnpy_paper",
            planned_cash_amount=1000,
            planned_quantity=100,
            planned_price=10,
            submitted_quantity=100,
            submitted_price=10,
            order_result={"status": "submitted", "raw": {"vt_orderid": "DSA_SIM.1"}},
        )

        callback = self.service.sync_vnpy_trade_callback(
            vt_orderid="DSA_SIM.1",
            vt_tradeid="DSA_SIM.1",
            quantity=100,
            price=10,
            trade_date=date(2026, 7, 21),
        )

        self.assertTrue(callback["accepted"])
        self.assertEqual(callback["status"], "filled")
        self.assertIsNotNone(callback["trade_id"])
        trades = self.service.portfolio.list_trade_events(account_id=account_id, page=1)
        self.assertEqual(len(trades["items"]), 2)
        self.assertEqual(
            len({item["trade_uid"] for item in trades["items"]}),
            2,
        )

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

    def test_cross_market_vnpy_partial_fills_charge_order_level_minimum_commission(self) -> None:
        run = self.service.agent_repo.create_run(
            run_uid="cross-market-fee-run",
            trigger_source="vnpy_paper_auto",
            strategy=CROSS_MARKET_STRATEGY_ID,
            market="cn",
            settings={},
            diagnostics={},
        )
        self.service.agent_repo.record_trade_plan(
            plan_uid="cross-market-fee-plan",
            run_id=int(run["id"]),
            decision_id=None,
            symbol="600519",
            market="cn",
            side="buy",
            status="submitted",
            execution_mode="vnpy_paper",
            planned_cash_amount=25_000,
            planned_quantity=2_500,
            planned_price=10,
            submitted_quantity=2_500,
            submitted_price=10,
            order_result={
                "status": "submitted",
                "raw": {
                    "vt_orderid": "DSA_SIM.FEES",
                    "cross_market_strategy": {
                        "strategy_id": CROSS_MARKET_STRATEGY_ID,
                    },
                    "execution_costs": {"instrument_type": "stock"},
                },
            },
        )

        first = self.service.sync_vnpy_trade_callback(
            vt_orderid="DSA_SIM.FEES",
            vt_tradeid="DSA_SIM.FEES.T1",
            quantity=1_000,
            price=10,
            trade_date=date(2026, 7, 6),
        )
        second = self.service.sync_vnpy_trade_callback(
            vt_orderid="DSA_SIM.FEES",
            vt_tradeid="DSA_SIM.FEES.T2",
            quantity=1_500,
            price=10,
            trade_date=date(2026, 7, 6),
        )

        self.assertEqual(first["status"], "part_filled")
        self.assertEqual(first["fee"], 5.1)
        self.assertEqual(first["tax"], 0.0)
        self.assertEqual(second["status"], "filled")
        self.assertEqual(second["fee"], 5.25)
        self.assertEqual(second["tax"], 0.0)
        self.assertEqual(
            [item["fee"] for item in second["raw"]["fill_sync"]["trades"]],
            [5.1, 0.15],
        )
        self.assertEqual(second["net_cash_change"], -25_005.25)

        self.service.agent_repo.record_trade_plan(
            plan_uid="cross-market-sell-fee-plan",
            run_id=int(run["id"]),
            decision_id=None,
            symbol="600519",
            market="cn",
            side="sell",
            status="submitted",
            execution_mode="vnpy_paper",
            planned_cash_amount=10_000,
            planned_quantity=1_000,
            planned_price=10,
            submitted_quantity=1_000,
            submitted_price=10,
            order_result={
                "status": "submitted",
                "raw": {
                    "vt_orderid": "DSA_SIM.SELL.FEES",
                    "cross_market_strategy": {
                        "strategy_id": CROSS_MARKET_STRATEGY_ID,
                    },
                    "execution_costs": {"instrument_type": "stock"},
                },
            },
        )
        sold = self.service.sync_vnpy_trade_callback(
            vt_orderid="DSA_SIM.SELL.FEES",
            vt_tradeid="DSA_SIM.SELL.FEES.T1",
            quantity=1_000,
            price=10,
            trade_date=date(2026, 7, 7),
        )

        self.assertEqual(sold["status"], "filled")
        self.assertEqual(sold["fee"], 5.1)
        self.assertEqual(sold["tax"], 5.0)
        self.assertEqual(sold["net_cash_change"], 9_989.9)

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

    def test_stale_observed_vnpy_order_is_cancelled_once_then_cancel_times_out(self) -> None:
        installed = _install_fake_vnpy_modules()
        main_engine = _FakeMainEngine()
        main_engine.orders["SIM.STALE"] = SimpleNamespace(
            vt_orderid="SIM.STALE",
            status="NOTTRADED",
            symbol="600519",
            direction="LONG",
            exchange="SSE",
            volume=100,
            traded=0,
            price=10,
        )
        self.service = VnpyPaperTradingService(
            data_fetcher_manager=_FakeDataFetcherManager(price=10.0),
            config_path=self.config_path,
            vnpy_main_engine=main_engine,
        )
        self.service.update_settings({"vnpy_gateway_name": "SIM"})
        run = self.service.agent_repo.create_run(
            run_uid="auto-cancel-stale-run",
            trigger_source="vnpy_paper_auto",
            strategy="dual_low",
            market="cn",
            settings={},
            diagnostics={},
        )
        plan = self.service.agent_repo.record_trade_plan(
            plan_uid="auto-cancel-stale-plan",
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
            order_result={"status": "submitted", "raw": {"vt_orderid": "SIM.STALE"}},
        )
        self._age_trade_plan(int(plan["id"]))

        try:
            cancelled = self.service.expire_stale_vnpy_trade_plans(max_plans=3, scan_limit=10)
            cancel_pending = self.service.agent_repo.get_trade_plan("auto-cancel-stale-plan")
            assert cancel_pending is not None
            cancel_order_result = dict(cancel_pending["order_result"])
            cancel_raw = dict(cancel_order_result["raw"])
            cancel_details = dict(cancel_raw["cancel"])
            cancel_details["requested_at"] = (
                datetime.now(timezone.utc) - timedelta(minutes=45)
            ).isoformat()
            cancel_raw["cancel"] = cancel_details
            cancel_order_result["raw"] = cancel_raw
            self.service.agent_repo.update_trade_plan_execution(
                plan_uid="auto-cancel-stale-plan",
                status="cancel_requested",
                submitted_quantity=100,
                submitted_price=10,
                trade_id=None,
                skip_reason=None,
                order_result=cancel_order_result,
            )
            expired = self.service.expire_stale_vnpy_trade_plans(max_plans=3, scan_limit=10)
            final_plan = self.service.agent_repo.get_trade_plan("auto-cancel-stale-plan")
        finally:
            _restore_modules(installed)

        self.assertEqual(cancelled["cancel_requested_count"], 1)
        self.assertEqual(cancelled["expired_count"], 0)
        self.assertEqual(cancelled["reconciled_count"], 1)
        self.assertEqual(len(main_engine.cancel_calls), 1)
        self.assertEqual(cancel_pending["status"], "cancel_requested")
        self.assertEqual(expired["cancel_requested_count"], 0)
        self.assertEqual(expired["expired_count"], 1)
        self.assertEqual(len(main_engine.cancel_calls), 1)
        assert final_plan is not None
        self.assertEqual(final_plan["status"], "failed")
        self.assertEqual(final_plan["skip_reason"], "vnpy_cancel_timeout")

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
        self.assertEqual(partial_audit["portfolio_change"]["status"], "pending")
        self.assertEqual(partial_audit["portfolio_change"]["booked_plan_count"], 0)
        self.assertEqual(partial_audit["portfolio_change"]["pending_plan_count"], 1)
        self.assertEqual(partial_audit["portfolio_change"]["items"], [])
        self.assertTrue(filled["accepted"])
        self.assertEqual(filled["status"], "filled")
        audit = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        self.assertIsNotNone(audit)
        assert audit is not None
        self.assertEqual(audit["trade_plans"][0]["status"], "filled")
        self.assertEqual(audit["trade_plans"][0]["trade_id"], filled["trade_id"])
        self.assertEqual(audit["decisions"][0]["status"], "filled")
        self.assertEqual(audit["portfolio_change"]["status"], "changed")
        self.assertEqual(audit["portfolio_change"]["booked_plan_count"], 1)
        self.assertEqual(audit["portfolio_change"]["items"][0]["net_quantity"], 100.0)

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

    def test_vnpy_account_status_diagnostics_redact_external_account_identity(self) -> None:
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
        self.assertEqual(sync_state["account"]["available"], 99000.0)
        self.assertNotIn("account_id", sync_state["account"])
        self.assertNotIn("raw", sync_state["account"])
        self.assertEqual(sync_state["position_count"], 1)
        self.assertEqual(sync_state["positions"][0]["symbol"], "600519")
        self.assertEqual(sync_state["positions"][0]["vt_symbol"], "600519.SSE")
        persisted_state = self.service._read_vnpy_sync_state()
        self.assertEqual(persisted_state["account"]["account_id"], "SIM.ACC")

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

    def test_completed_campaign_blocks_cross_market_trade_plan_retry(self) -> None:
        self.service.ensure_account(settings=self.service.get_settings())
        run = self.service.agent_repo.create_run(
            run_uid="retry-completed-campaign-run",
            trigger_source="vnpy_paper_auto",
            strategy=CROSS_MARKET_STRATEGY_ID,
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
            plan_uid="retry-completed-campaign-plan",
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
            order_result={
                "status": "skipped",
                "reason": "price_unavailable",
                "raw": {"cross_market_strategy": {"theme": "memory"}},
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
        guard = {
            "block": True,
            "reason": "paper_campaign_completed",
            "completion_session_date": "2026-09-07",
            "current_session_date": "2026-09-08",
        }

        with patch.object(
            self.service,
            "_cross_market_campaign_execution_guard",
            return_value=guard,
        ), patch.object(
            self.service,
            "_revalidate_cross_market_trade_plan",
        ) as revalidate, patch.object(
            self.service,
            "submit_order",
        ) as submit:
            retried = self.service.retry_trade_plan("retry-completed-campaign-plan")

        self.assertFalse(retried["accepted"])
        self.assertEqual(retried["status"], "skipped")
        self.assertEqual(retried["reason"], "paper_campaign_completed")
        self.assertEqual(retried["quantity"], 0.0)
        self.assertEqual(
            retried["raw"]["cross_market_revalidation"]["campaign_guard"],
            guard,
        )
        revalidate.assert_not_called()
        submit.assert_not_called()

        refreshed = self.service.agent_repo.get_trade_plan(
            "retry-completed-campaign-plan"
        )
        self.assertIsNotNone(refreshed)
        assert refreshed is not None
        self.assertEqual(refreshed["status"], "skipped")
        self.assertEqual(refreshed["skip_reason"], "paper_campaign_completed")
        retry_due, retry_reason = self.service._auto_retry_due(refreshed)
        self.assertFalse(retry_due)
        self.assertEqual(retry_reason, "trade_plan_not_retryable")

    def test_retry_trade_plan_rejects_plan_from_previous_account(self) -> None:
        previous_account = self.service.ensure_account()
        run = self.service.agent_repo.create_run(
            run_uid="retry-previous-account-run",
            trigger_source="vnpy_paper_auto",
            strategy="dual_low",
            market="cn",
            settings={
                "auto_execution_mode": "paper",
                "account_id": previous_account["id"],
            },
        )
        self.service.agent_repo.record_trade_plan(
            plan_uid="retry-previous-account-plan",
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
            skip_reason="price_unavailable",
        )
        reset = self.service.reset_account(
            include_snapshot=False,
            include_recent_trades=False,
        )
        self.assertNotEqual(reset["account"]["id"], previous_account["id"])

        with self.assertRaisesRegex(ValueError, "trade_plan_account_scope_mismatch"):
            self.service.retry_trade_plan("retry-previous-account-plan")

    def test_auto_retry_scan_excludes_previous_account_runs(self) -> None:
        previous_account = self.service.ensure_account()
        run = self.service.agent_repo.create_run(
            run_uid="auto-retry-previous-account-run",
            trigger_source="vnpy_paper_auto",
            strategy="dual_low",
            market="cn",
            settings={
                "auto_execution_mode": "paper",
                "account_id": previous_account["id"],
            },
        )
        self.service.agent_repo.record_trade_plan(
            plan_uid="auto-retry-previous-account-plan",
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
        )
        self.service.reset_account(
            include_snapshot=False,
            include_recent_trades=False,
        )

        candidates = self.service._auto_retry_candidate_trade_plans(scan_limit=20)

        self.assertEqual(candidates, [])

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
            row.created_at = stale_time
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
            row.created_at = stale_time
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

    def test_data_quality_gate_preserves_sell_and_persists_run_counts(self) -> None:
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
                "auto_cash_per_order": 1200,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "quality_status": "stale",
            "source_errors": ["last_good_cache_only"],
            "candidates": [
                {"code": "000001", "name": "平安银行", "score": 80, "price": 10.0},
            ],
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
        self.assertEqual(result["reason"], "data_quality_stale")
        self.assertEqual(result["candidate_count"], 1)
        self.assertEqual(result["planned_count"], 0)
        self.assertEqual(result["submitted_count"], 1)
        self.assertEqual(result["skipped_count"], 1)
        self.assertEqual(
            [(item["side"], item.get("reason")) for item in result["orders"]],
            [("sell", None), ("buy", "data_quality_stale")],
        )

        audit = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        self.assertIsNotNone(audit)
        assert audit is not None
        self.assertEqual(audit["status"], "completed")
        self.assertEqual(audit["candidate_count"], 1)
        self.assertEqual(audit["planned_count"], 0)
        self.assertEqual(audit["submitted_count"], 1)
        self.assertEqual(audit["skipped_count"], 1)
        self.assertEqual(
            [(item["side"], item["status"], item.get("skip_reason")) for item in audit["trade_plans"]],
            [("sell", "filled", None), ("buy", "skipped", "data_quality_stale")],
        )
        self.assertEqual(
            [(item["action"], item["reason"]) for item in audit["decisions"]],
            [("sell", "stop_loss_triggered"), ("skip", "data_quality_stale")],
        )

    def test_account_and_market_gates_preserve_risk_reducing_sell(self) -> None:
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
                "auto_min_cash_balance": 100000,
                "auto_market_light_gate_enabled": True,
                "auto_market_light_block_statuses": ["yellow"],
                "auto_strategy": "dual_low",
                "auto_max_results": 1,
                "auto_cash_per_order": 1200,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "quality_status": "ok",
            "candidates": [
                {"code": "000001", "name": "平安银行", "score": 80, "price": 10.0},
            ],
            "warnings": [],
        }

        with patch(
            "src.services.portfolio_service.PortfolioService._fetch_realtime_position_price",
            return_value=(9.0, "unit-test"),
        ), patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ), patch(
            "src.services.vnpy_paper_trading_service.load_previous_snapshot",
            return_value={
                "region": "cn",
                "trade_date": date.today().isoformat(),
                "status": "yellow",
                "score": 45,
            },
        ):
            result = self.service.run_auto_trade_once()

        self.assertEqual(result["submitted_count"], 1)
        self.assertEqual(result["skipped_count"], 1)
        self.assertEqual(
            [(item["side"], item.get("reason")) for item in result["orders"]],
            [("sell", None), ("buy", "cash_low_watermark")],
        )
        audit = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        self.assertIsNotNone(audit)
        assert audit is not None
        self.assertEqual(audit["submitted_count"], 1)
        self.assertEqual(audit["skipped_count"], 1)
        self.assertEqual(audit["diagnostics"]["account_risk"]["cash"], 99900.0)
        self.assertEqual(audit["diagnostics"]["account_risk"]["observed_value"], 99900.0)
        self.assertEqual(audit["diagnostics"]["account_risk"]["threshold"], 100000.0)
        self.assertEqual(audit["diagnostics"]["market_context_risk"]["status"], "blocked")
        self.assertEqual(
            audit["diagnostics"]["market_context_risk"]["reason"],
            "market_light_yellow",
        )
        self.assertEqual(
            [(item["action"], item["reason"]) for item in audit["decisions"]],
            [("sell", "stop_loss_triggered"), ("skip", "cash_low_watermark")],
        )

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

    def test_auto_trade_does_not_rebuy_symbol_exited_in_same_run(self) -> None:
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
                "auto_cash_per_order": 1200,
            }
        )
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "quality_status": "ok",
            "candidates": [
                {"code": "600519", "name": "贵州茅台", "score": 80, "price": 9.0},
            ],
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

        self.assertEqual(result["submitted_count"], 1)
        self.assertEqual(result["skipped_count"], 1)
        self.assertEqual(
            [(item["side"], item.get("reason")) for item in result["orders"]],
            [("sell", None), ("buy", "same_run_exit_reentry_blocked")],
        )
        audit = self.service.agent_repo.get_run_detail(result["agent_run_uid"])
        self.assertIsNotNone(audit)
        assert audit is not None
        self.assertEqual(audit["diagnostics"]["same_run_exit_symbols"], ["600519"])
        self.assertEqual(
            [(item["action"], item["reason"]) for item in audit["decisions"]],
            [("sell", "stop_loss_triggered"), ("skip", "same_run_exit_reentry_blocked")],
        )
        self.assertEqual(
            audit["trade_plans"][1]["skip_reason"],
            "same_run_exit_reentry_blocked",
        )

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

    def test_position_holding_days_reads_all_trade_pages(self) -> None:
        today = date.today()
        events = [
            {
                "id": index + 1,
                "trade_date": (today - timedelta(days=120 - index)).isoformat(),
                "side": "buy",
                "quantity": 1,
            }
            for index in range(101)
        ]
        events.reverse()

        def list_events(**kwargs):
            page = kwargs["page"]
            start = (page - 1) * 100
            return {
                "items": events[start : start + 100],
                "total": len(events),
                "page": page,
                "page_size": kwargs["page_size"],
            }

        with patch.object(
            self.service.portfolio,
            "list_trade_events",
            side_effect=list_events,
        ) as list_trade_events, patch.object(
            self.service.portfolio,
            "list_corporate_action_events",
            return_value={"items": [], "total": 0},
        ):
            holding_days = self.service._position_holding_days(account_id=1, symbol="600519")

        self.assertEqual(holding_days, 120)
        self.assertEqual(list_trade_events.call_count, 2)
        self.assertEqual(list_trade_events.call_args_list[0].kwargs["page_size"], 100)
        self.assertNotIn("side", list_trade_events.call_args_list[0].kwargs)

    def test_position_holding_days_resets_after_full_exit_and_reentry(self) -> None:
        today = date.today()
        payload = {
            "items": [
                {
                    "id": 3,
                    "trade_date": (today - timedelta(days=4)).isoformat(),
                    "side": "buy",
                    "quantity": 100,
                },
                {
                    "id": 2,
                    "trade_date": (today - timedelta(days=20)).isoformat(),
                    "side": "sell",
                    "quantity": 100,
                },
                {
                    "id": 1,
                    "trade_date": (today - timedelta(days=40)).isoformat(),
                    "side": "buy",
                    "quantity": 100,
                },
            ],
            "total": 3,
            "page": 1,
            "page_size": 100,
        }

        with patch.object(
            self.service.portfolio,
            "list_trade_events",
            return_value=payload,
        ), patch.object(
            self.service.portfolio,
            "list_corporate_action_events",
            return_value={"items": [], "total": 0},
        ):
            holding_days = self.service._position_holding_days(account_id=1, symbol="600519")

        self.assertEqual(holding_days, 4)

    def test_position_holding_days_replays_split_before_partial_exit(self) -> None:
        today = date.today()
        trades = {
            "items": [
                {
                    "id": 2,
                    "trade_date": (today - timedelta(days=10)).isoformat(),
                    "side": "sell",
                    "quantity": 100,
                },
                {
                    "id": 1,
                    "trade_date": (today - timedelta(days=40)).isoformat(),
                    "side": "buy",
                    "quantity": 100,
                },
            ],
            "total": 2,
        }
        actions = {
            "items": [
                {
                    "id": 1,
                    "effective_date": (today - timedelta(days=10)).isoformat(),
                    "action_type": "split_adjustment",
                    "split_ratio": 2.0,
                }
            ],
            "total": 1,
        }

        with patch.object(
            self.service.portfolio,
            "list_trade_events",
            return_value=trades,
        ), patch.object(
            self.service.portfolio,
            "list_corporate_action_events",
            return_value=actions,
        ) as list_actions:
            holding_days = self.service._position_holding_days(account_id=1, symbol="600519")

        self.assertEqual(holding_days, 40)
        self.assertEqual(list_actions.call_args.kwargs["action_type"], "split_adjustment")
        self.assertEqual(list_actions.call_args.kwargs["page_size"], 100)
        self.assertEqual(list_actions.call_args.kwargs["date_to"], today)

    def test_position_holding_days_replays_persisted_split_ledger(self) -> None:
        today = date.today()
        account = self.service.ensure_account()
        account_id = int(account["id"])
        self.service.portfolio.record_trade(
            account_id=account_id,
            symbol="600519",
            trade_date=today - timedelta(days=40),
            side="buy",
            quantity=100,
            price=10,
            market="cn",
            currency="CNY",
        )
        self.service.portfolio.record_corporate_action(
            account_id=account_id,
            symbol="600519",
            effective_date=today - timedelta(days=10),
            action_type="split_adjustment",
            split_ratio=2.0,
            market="cn",
            currency="CNY",
        )
        self.service.portfolio.record_trade(
            account_id=account_id,
            symbol="600519",
            trade_date=today - timedelta(days=10),
            side="sell",
            quantity=100,
            price=6,
            market="cn",
            currency="CNY",
        )

        holding_days = self.service._position_holding_days(
            account_id=account_id,
            symbol="600519",
        )

        self.assertEqual(holding_days, 40)

    def test_position_holding_days_fails_closed_when_split_history_is_unavailable(self) -> None:
        today = date.today()
        trades = {
            "items": [
                {
                    "id": 1,
                    "trade_date": (today - timedelta(days=40)).isoformat(),
                    "side": "buy",
                    "quantity": 100,
                }
            ],
            "total": 1,
        }

        with patch.object(
            self.service.portfolio,
            "list_trade_events",
            return_value=trades,
        ), patch.object(
            self.service.portfolio,
            "list_corporate_action_events",
            side_effect=RuntimeError("split history unavailable"),
        ):
            holding_days = self.service._position_holding_days(account_id=1, symbol="600519")

        self.assertIsNone(holding_days)

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
                    "fund_flow/tushare_ths": {
                        "status": "unavailable",
                        "successes": 0,
                        "failures": 1,
                        "trend_only": True,
                    },
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

    def test_auto_trade_background_task_waits_for_transient_agent_lock(self) -> None:
        fake_service = MagicMock()
        fake_service.get_settings.return_value = VnpyPaperSettings(
            enabled=True,
            auto_trade_enabled=True,
            auto_interval_minutes=5,
            auto_trade_time_gate_enabled=False,
        )
        fake_service.run_auto_trade_once.return_value = {
            "accepted": True,
            "skipped": False,
            "reason": None,
            "submitted_count": 0,
            "skipped_count": 0,
        }
        fake_lock = MagicMock()
        fake_lock.acquire.return_value = True

        with (
            patch.dict(
                os.environ,
                {"DSA_AGENT_CALIBRATION_SHADOW_ENABLED": "false"},
                clear=False,
            ),
            patch(
                "src.services.vnpy_paper_trading_service.VnpyPaperTradingService",
                return_value=fake_service,
            ),
            patch(
                "src.services.vnpy_paper_trading_service._AUTO_AGENT_RUN_LOCK",
                fake_lock,
            ),
        ):
            tasks = build_vnpy_paper_trading_background_tasks()
            result = next(
                task for task in tasks if task["name"] == "vnpy_paper_auto_trade"
            )["task"]()

        self.assertTrue(result["accepted"])
        fake_lock.acquire.assert_called_once_with(
            timeout=AUTO_TRADE_RUN_LOCK_WAIT_SECONDS
        )
        fake_lock.release.assert_called_once_with()
        fake_service.run_auto_trade_once.assert_called_once_with()

    def test_auto_trade_background_task_reports_lock_wait_timeout(self) -> None:
        fake_service = MagicMock()
        fake_service.get_settings.return_value = VnpyPaperSettings(
            enabled=True,
            auto_trade_enabled=True,
            auto_interval_minutes=5,
            auto_trade_time_gate_enabled=False,
        )
        fake_lock = MagicMock()
        fake_lock.acquire.return_value = False

        with (
            patch.dict(
                os.environ,
                {"DSA_AGENT_CALIBRATION_SHADOW_ENABLED": "false"},
                clear=False,
            ),
            patch(
                "src.services.vnpy_paper_trading_service.VnpyPaperTradingService",
                return_value=fake_service,
            ),
            patch(
                "src.services.vnpy_paper_trading_service._AUTO_AGENT_RUN_LOCK",
                fake_lock,
            ),
        ):
            tasks = build_vnpy_paper_trading_background_tasks()
            result = next(
                task for task in tasks if task["name"] == "vnpy_paper_auto_trade"
            )["task"]()

        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "auto_agent_run_lock_timeout")
        self.assertEqual(
            result["lock_wait_seconds"],
            AUTO_TRADE_RUN_LOCK_WAIT_SECONDS,
        )
        fake_lock.release.assert_not_called()
        fake_service.run_auto_trade_once.assert_not_called()

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

    def test_background_task_builder_adds_cross_market_korea_signal_collector(self) -> None:
        fake_service = MagicMock()
        fake_service.get_settings.return_value = SimpleNamespace(
            enabled=True,
            auto_trade_enabled=True,
            auto_interval_minutes=5,
            auto_strategy=CROSS_MARKET_STRATEGY_ID,
            auto_market="cn",
            auto_trade_time_gate_enabled=False,
            auto_execution_mode="paper",
        )
        fake_service.cross_market_signal_service.collect_korea_snapshot_if_open.return_value = {
            "accepted": True,
            "skipped": False,
            "reason": "korea_snapshot_collected",
        }
        fake_service.cross_market_signal_service.collect_japan_snapshot_if_open.return_value = {
            "accepted": True,
            "skipped": False,
            "reason": "japan_snapshot_collected",
        }
        fake_service.cross_market_signal_service.collect_asia_theme_snapshot_if_cn_open.return_value = {
            "accepted": True,
            "skipped": False,
            "reason": "asia_theme_snapshot_collected",
        }
        fake_service.cross_market_signal_service.collect_us_tech_snapshot_if_open.return_value = {
            "accepted": True,
            "skipped": False,
            "reason": "us_tech_snapshot_collected",
        }
        fake_service.cross_market_signal_service.collect_cpo_snapshot_if_open.return_value = {
            "accepted": True,
            "skipped": False,
            "reason": "cpo_snapshot_collected",
        }
        fake_service.cross_market_signal_service.collect_gold_snapshot_if_window.return_value = {
            "accepted": True,
            "skipped": False,
            "reason": "gold_snapshot_collected",
        }
        fake_service.cross_market_signal_service.collect_cn_open_snapshot_if_window.return_value = {
            "accepted": True,
            "skipped": False,
            "reason": "cn_open_snapshot_collected",
        }
        fake_service.run_cross_market_intraday_sell_monitor.return_value = {
            "accepted": True,
            "skipped": False,
            "reason": "cross_market_intraday_sell_checked",
            "submitted_count": 0,
            "sell_only": True,
        }
        fake_service.capture_cross_market_campaign_closing_snapshot.return_value = {
            "accepted": True,
            "skipped": True,
            "reason": "before_cn_closing_snapshot_cutoff",
        }

        with patch.dict(
            os.environ,
            {"DSA_AGENT_CALIBRATION_SHADOW_ENABLED": "false"},
            clear=False,
        ), patch(
            "src.services.vnpy_paper_trading_service.VnpyPaperTradingService",
            return_value=fake_service,
        ):
            tasks = build_vnpy_paper_trading_background_tasks()

        collector = next(
            task for task in tasks if task["name"] == "cross_market_korea_signal"
        )
        self.assertEqual(collector["interval_seconds"], 60)
        self.assertTrue(collector["run_immediately"])
        self.assertEqual(collector["task"]()["reason"], "korea_snapshot_collected")
        fake_service.cross_market_signal_service.collect_korea_snapshot_if_open.assert_called_once_with()
        fake_service.cross_market_signal_service.collect_japan_snapshot_if_open.assert_called_once_with()
        fake_service.cross_market_signal_service.collect_asia_theme_snapshot_if_cn_open.assert_called_once_with()
        us_collector = next(
            task for task in tasks if task["name"] == "cross_market_us_tech_signal"
        )
        self.assertEqual(us_collector["interval_seconds"], 60)
        self.assertTrue(us_collector["run_immediately"])
        self.assertEqual(us_collector["task"]()["reason"], "us_tech_snapshot_collected")
        fake_service.cross_market_signal_service.collect_us_tech_snapshot_if_open.assert_called_once_with()
        cpo_collector = next(
            task for task in tasks if task["name"] == "cross_market_cpo_signal"
        )
        self.assertEqual(cpo_collector["interval_seconds"], 60)
        self.assertTrue(cpo_collector["run_immediately"])
        self.assertEqual(cpo_collector["task"]()["reason"], "cpo_snapshot_collected")
        fake_service.cross_market_signal_service.collect_cpo_snapshot_if_open.assert_called_once_with()
        gold_collector = next(
            task for task in tasks if task["name"] == "cross_market_gold_signal"
        )
        self.assertEqual(gold_collector["interval_seconds"], 300)
        self.assertTrue(gold_collector["run_immediately"])
        self.assertEqual(gold_collector["task"]()["reason"], "gold_snapshot_collected")
        fake_service.cross_market_signal_service.collect_gold_snapshot_if_window.assert_called_once_with()
        cn_open_collector = next(
            task for task in tasks if task["name"] == "cross_market_cn_open_signal"
        )
        self.assertEqual(cn_open_collector["interval_seconds"], 60)
        self.assertTrue(cn_open_collector["run_immediately"])
        self.assertEqual(cn_open_collector["task"]()["reason"], "cn_open_snapshot_collected")
        fake_service.cross_market_signal_service.collect_cn_open_snapshot_if_window.assert_called_once_with()

        closing_snapshot = next(
            task
            for task in tasks
            if task["name"] == "cross_market_campaign_closing_snapshot"
        )
        self.assertEqual(closing_snapshot["interval_seconds"], 300)
        self.assertTrue(closing_snapshot["run_immediately"])
        self.assertEqual(
            closing_snapshot["task"]()["reason"],
            "before_cn_closing_snapshot_cutoff",
        )
        fake_service.capture_cross_market_campaign_closing_snapshot.assert_called_once_with()
        pending_revalidation = next(
            task
            for task in tasks
            if task["name"] == "cross_market_pending_order_revalidation"
        )
        self.assertEqual(pending_revalidation["interval_seconds"], 60)
        self.assertTrue(pending_revalidation["run_immediately"])
        pending_revalidation["task"]()
        fake_service.revalidate_active_cross_market_trade_plans.assert_called_once_with()
        sell_monitor = next(
            task
            for task in tasks
            if task["name"] == "cross_market_intraday_sell_monitor"
        )
        self.assertEqual(sell_monitor["interval_seconds"], 60)
        self.assertTrue(sell_monitor["run_immediately"])
        self.assertTrue(sell_monitor["task"]()["sell_only"])
        fake_service.run_cross_market_intraday_sell_monitor.assert_called_once_with()
        fake_service.run_cross_market_intraday_sell_monitor.reset_mock()
        with patch(
            "src.services.vnpy_paper_trading_service."
            "_cross_market_intraday_sell_reserved_window",
            return_value=True,
        ):
            reserved = sell_monitor["task"]()
        self.assertTrue(reserved["skipped"])
        self.assertEqual(reserved["reason"], "daily_cross_market_run_window_reserved")
        fake_service.run_cross_market_intraday_sell_monitor.assert_not_called()

    def test_asia_collector_isolates_japan_failure_from_other_routes(self) -> None:
        fake_service = MagicMock()
        fake_service.get_settings.return_value = SimpleNamespace(
            enabled=True,
            auto_trade_enabled=True,
            auto_interval_minutes=5,
            auto_strategy=CROSS_MARKET_STRATEGY_ID,
            auto_market="cn",
            auto_trade_time_gate_enabled=False,
            auto_execution_mode="paper",
        )
        signal_service = fake_service.cross_market_signal_service
        signal_service.collect_korea_snapshot_if_open.return_value = {
            "accepted": True,
            "skipped": False,
            "reason": "korea_snapshot_collected",
        }
        signal_service.collect_japan_snapshot_if_open.side_effect = ValueError(
            "japan_quote_unavailable:N225"
        )
        signal_service.collect_asia_theme_snapshot_if_cn_open.return_value = {
            "accepted": True,
            "skipped": False,
            "reason": "asia_theme_snapshot_collected",
        }

        with patch.dict(
            os.environ,
            {"DSA_AGENT_CALIBRATION_SHADOW_ENABLED": "false"},
            clear=False,
        ), patch(
            "src.services.vnpy_paper_trading_service.VnpyPaperTradingService",
            return_value=fake_service,
        ):
            collector = next(
                task
                for task in build_vnpy_paper_trading_background_tasks()
                if task["name"] == "cross_market_korea_signal"
            )
            result = collector["task"]()

        self.assertEqual(result["reason"], "korea_snapshot_collected")
        self.assertEqual(result["component_status"], "degraded")
        self.assertEqual(result["degraded_components"], ["japan"])
        self.assertEqual(result["japan"]["reason"], "japan_snapshot_failed")
        self.assertEqual(result["japan"]["error_type"], "ValueError")
        signal_service.collect_asia_theme_snapshot_if_cn_open.assert_called_once_with()

    def test_background_task_builder_adds_order_free_cross_market_observation(self) -> None:
        fake_service = MagicMock()
        fake_lock = MagicMock()
        fake_lock.acquire.return_value = True
        fake_service.get_settings.return_value = VnpyPaperSettings(
            enabled=True,
            auto_trade_enabled=False,
            auto_strategy="dual_low",
            cross_market_observation_enabled=True,
        )
        fake_service._trading_window_diagnostics.return_value = {
            "is_market_open_now": True,
            "session_date": "2026-07-28",
            "next_open_at": None,
        }
        fake_service.agent_repo.list_runs.return_value = {
            "items": [],
            "total": 0,
        }
        fake_service.run_auto_trade_once.return_value = {
            "accepted": True,
            "skipped": False,
            "reason": "completed",
            "submitted_count": 0,
        }

        with patch.dict(
            os.environ,
            {"DSA_AGENT_CALIBRATION_SHADOW_ENABLED": "false"},
            clear=False,
        ), patch(
            "src.services.vnpy_paper_trading_service.VnpyPaperTradingService",
            return_value=fake_service,
        ), patch(
            "src.services.vnpy_paper_trading_service._AUTO_AGENT_RUN_LOCK",
            fake_lock,
        ):
            tasks = build_vnpy_paper_trading_background_tasks()
            observation = next(
                task for task in tasks
                if task["name"] == "cross_market_paper_observation"
            )
            result = observation["task"]()

        names = [task["name"] for task in tasks]
        self.assertIn("cross_market_paper_observation", names)
        self.assertIn("cross_market_korea_signal", names)
        self.assertIn("cross_market_us_tech_signal", names)
        self.assertIn("cross_market_cpo_signal", names)
        self.assertIn("cross_market_gold_signal", names)
        self.assertIn("cross_market_cn_open_signal", names)
        self.assertNotIn("cross_market_pending_order_revalidation", names)
        self.assertEqual(observation["interval_seconds"], 5 * 60)
        self.assertFalse(observation["run_immediately"])
        self.assertEqual(observation["initial_delay_seconds"], 60)

        self.assertFalse(result["submits_orders"])
        fake_lock.acquire.assert_called_once_with(
            timeout=CROSS_MARKET_OBSERVATION_LOCK_WAIT_SECONDS
        )
        fake_lock.release.assert_called_once_with()
        fake_service.run_auto_trade_once.assert_called_once_with(
            execution_mode_override="dry_run",
            ignore_auto_trade_enabled=True,
            market_override="cn",
            strategy_override=CROSS_MARKET_STRATEGY_ID,
            max_results_override=3,
            trigger_source_override="cross_market_paper_observation",
        )

    def test_cross_market_observation_skips_after_daily_evidence_is_ready(self) -> None:
        required_themes = sorted(GLOBAL_MARKET_LINKED_THEMES)
        fake_service = MagicMock()
        fake_lock = MagicMock()
        fake_lock.acquire.return_value = True
        fake_service.get_settings.return_value = VnpyPaperSettings(
            enabled=True,
            account_id=9,
            auto_trade_enabled=False,
            auto_strategy=CROSS_MARKET_STRATEGY_ID,
            cross_market_observation_enabled=True,
        )
        fake_service._trading_window_diagnostics.return_value = {
            "is_market_open_now": True,
            "session_date": "2026-07-28",
        }
        fake_service.agent_repo.list_runs.return_value = {
            "items": [
                {
                    "run_uid": "formal-ready-run",
                    "status": "completed",
                    "created_at": "2026-07-28T09:35:15+08:00",
                    "settings": {
                        "account_id": 9,
                        "auto_execution_mode": "vnpy_paper",
                    },
                    "diagnostics": {
                        "execution_mode": "vnpy_paper",
                        "cross_market_observation": {
                            "schema_version": 5,
                            "strategy_id": CROSS_MARKET_STRATEGY_ID,
                            "status": "ready",
                            "checked_at": "2026-07-28T09:35:15+08:00",
                            "required_checks": {
                                "nasdaq_futures_continuous_trend_available": True,
                                "us_premarket_themes_available": True,
                                "us_close_themes_available": True,
                                "japan_market_available": True,
                            },
                            "evidence": {
                                "us_premarket": {
                                    "full_strategy_coverage": True,
                                    "required_themes": required_themes,
                                    "qualified_themes": required_themes,
                                    "missing_themes": [],
                                },
                                "us_close_themes": {
                                    "full_strategy_coverage": True,
                                    "required_themes": required_themes,
                                    "qualified_themes": required_themes,
                                    "missing_themes": [],
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
                        },
                    },
                }
            ],
            "total": 1,
        }

        with patch.dict(
            os.environ,
            {"DSA_AGENT_CALIBRATION_SHADOW_ENABLED": "false"},
            clear=False,
        ), patch(
            "src.services.vnpy_paper_trading_service.VnpyPaperTradingService",
            return_value=fake_service,
        ), patch(
            "src.services.vnpy_paper_trading_service._AUTO_AGENT_RUN_LOCK",
            fake_lock,
        ):
            observation = next(
                task
                for task in build_vnpy_paper_trading_background_tasks()
                if task["name"] == "cross_market_paper_observation"
            )
            result = observation["task"]()

        self.assertTrue(result["accepted"])
        self.assertTrue(result["skipped"])
        self.assertEqual(
            result["reason"],
            "observation_already_fully_evidenced_today",
        )
        self.assertEqual(
            result["daily_evidence"]["run_uid"],
            "formal-ready-run",
        )
        fake_service.run_auto_trade_once.assert_not_called()
        fake_lock.acquire.assert_called_once_with(
            timeout=CROSS_MARKET_OBSERVATION_LOCK_WAIT_SECONDS
        )
        fake_lock.release.assert_called_once_with()

    def test_cross_market_intraday_scan_reports_only_its_configured_slots(
        self,
    ) -> None:
        fake_service = MagicMock()
        fake_service.get_settings.return_value = VnpyPaperSettings(
            enabled=True,
            account_id=9,
            auto_trade_enabled=True,
            auto_strategy=CROSS_MARKET_STRATEGY_ID,
            auto_execution_mode="vnpy_paper",
            cross_market_observation_enabled=True,
        )
        fake_service._trading_window_diagnostics.return_value = {
            "is_market_open_now": True,
            "session_date": "2026-07-30",
        }

        with patch.dict(
            os.environ,
            {"DSA_AGENT_CALIBRATION_SHADOW_ENABLED": "false"},
            clear=False,
        ), patch(
            "src.services.vnpy_paper_trading_service.VnpyPaperTradingService",
            return_value=fake_service,
        ), patch(
            "src.services.vnpy_paper_trading_service._auto_trade_initial_delay_seconds",
            return_value=300,
        ), patch(
            "src.services.vnpy_paper_trading_service._cross_market_intraday_entry_slot",
            return_value=None,
        ):
            scan = next(
                task
                for task in build_vnpy_paper_trading_background_tasks()
                if task["name"] == "cross_market_intraday_entry_scan"
            )
            result = scan["task"]()

        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "outside_cross_market_entry_analysis_slot")
        self.assertEqual(result["configured_times"], ["10:40", "13:30", "14:30"])
        fake_service.run_auto_trade_once.assert_not_called()

    def test_cross_market_intraday_scan_recovers_zero_activity_formal_run(
        self,
    ) -> None:
        fake_service = MagicMock()
        fake_lock = MagicMock()
        fake_lock.acquire.return_value = True
        fake_service.get_settings.return_value = VnpyPaperSettings(
            enabled=True,
            account_id=9,
            auto_trade_enabled=True,
            auto_strategy=CROSS_MARKET_STRATEGY_ID,
            auto_execution_mode="vnpy_paper",
            cross_market_observation_enabled=True,
        )
        fake_service._trading_window_diagnostics.return_value = {
            "is_market_open_now": True,
            "session_date": "2026-07-28",
        }
        fake_service.agent_repo.list_runs.return_value = {
            "items": [{
                "run_uid": "formal-degraded-run",
                "status": "completed",
                "created_at": "2026-07-28T09:35:15+08:00",
                "candidate_count": 2,
                "planned_count": 0,
                "submitted_count": 0,
                "settings": {
                    "account_id": 9,
                    "auto_execution_mode": "vnpy_paper",
                },
                "diagnostics": {
                    "execution_mode": "vnpy_paper",
                    "cross_market_observation": {
                        "schema_version": 2,
                        "strategy_id": CROSS_MARKET_STRATEGY_ID,
                        "status": "unavailable",
                        "checked_at": "2026-07-28T09:35:15+08:00",
                        "missing_requirements": [
                            "korea_continuous_gate_available"
                        ],
                    },
                },
            }],
            "total": 1,
        }
        fake_service._cross_market_observation_snapshot.return_value = {
            "schema_version": 2,
            "strategy_id": CROSS_MARKET_STRATEGY_ID,
            "status": "ready",
            "missing_requirements": [],
        }
        fake_service.run_auto_trade_once.return_value = {
            "accepted": True,
            "skipped": False,
            "reason": "completed",
            "candidate_count": 2,
            "planned_count": 1,
            "submitted_count": 1,
            "skipped_count": 1,
        }

        with patch.dict(
            os.environ,
            {"DSA_AGENT_CALIBRATION_SHADOW_ENABLED": "false"},
            clear=False,
        ), patch(
            "src.services.vnpy_paper_trading_service.VnpyPaperTradingService",
            return_value=fake_service,
        ), patch(
            "src.services.vnpy_paper_trading_service._AUTO_AGENT_RUN_LOCK",
            fake_lock,
        ), patch(
            "src.services.vnpy_paper_trading_service._auto_trade_initial_delay_seconds",
            return_value=300,
        ), patch(
            "src.services.vnpy_paper_trading_service._cross_market_intraday_entry_slot",
            return_value=(
                "10:40",
                datetime(2026, 7, 28, 2, 40, tzinfo=timezone.utc),
                datetime(2026, 7, 28, 2, 42, tzinfo=timezone.utc),
            ),
        ), patch(
            "src.services.vnpy_paper_trading_service._cross_market_intraday_entry_slot_audited",
            return_value=False,
        ), patch(
            "src.services.vnpy_paper_trading_service._cross_market_daily_evidence_status",
            return_value={
                "ready": False,
                "run_observed": True,
                "planned_count": 0,
                "submitted_count": 0,
                "run_uid": "formal-degraded-run",
            },
        ):
            recovery = next(
                task
                for task in build_vnpy_paper_trading_background_tasks()
                if task["name"] == "cross_market_intraday_entry_scan"
            )
            result = recovery["task"]()

        self.assertEqual(recovery["interval_seconds"], 60)
        self.assertTrue(recovery["run_immediately"])
        self.assertEqual(result["analysis_slot"], "10:40")
        self.assertTrue(result["formal_recovery"])
        self.assertEqual(result["execution_mode"], "vnpy_paper")
        self.assertEqual(result["trigger_source"], "vnpy_paper_auto")
        self.assertEqual(result["submitted_count"], 1)
        fake_service.run_auto_trade_once.assert_called_once_with(
            trigger_source_override="vnpy_paper_auto",
            allow_cross_market_intraday_entry_recheck=True,
            max_results_override=2,
        )
        fake_lock.acquire.assert_called_once_with(
            timeout=AUTO_TRADE_RUN_LOCK_WAIT_SECONDS
        )
        fake_lock.release.assert_called_once_with()

    def test_cross_market_intraday_scan_stays_independent_without_formal_baseline(
        self,
    ) -> None:
        fake_service = MagicMock()
        fake_lock = MagicMock()
        fake_lock.acquire.return_value = True
        fake_service.get_settings.return_value = VnpyPaperSettings(
            enabled=True,
            account_id=9,
            auto_trade_enabled=True,
            auto_strategy=CROSS_MARKET_STRATEGY_ID,
            auto_execution_mode="vnpy_paper",
            cross_market_observation_enabled=True,
        )
        fake_service._trading_window_diagnostics.return_value = {
            "is_market_open_now": True,
            "session_date": "2026-07-30",
        }
        fake_service.run_auto_trade_once.return_value = {
            "accepted": True,
            "skipped": False,
            "reason": "completed",
            "candidate_count": 2,
            "planned_count": 1,
            "submitted_count": 1,
            "skipped_count": 1,
        }

        with patch.dict(
            os.environ,
            {"DSA_AGENT_CALIBRATION_SHADOW_ENABLED": "false"},
            clear=False,
        ), patch(
            "src.services.vnpy_paper_trading_service.VnpyPaperTradingService",
            return_value=fake_service,
        ), patch(
            "src.services.vnpy_paper_trading_service._AUTO_AGENT_RUN_LOCK",
            fake_lock,
        ), patch(
            "src.services.vnpy_paper_trading_service._auto_trade_initial_delay_seconds",
            return_value=300,
        ), patch(
            "src.services.vnpy_paper_trading_service._cross_market_intraday_entry_slot",
            return_value=(
                "10:40",
                datetime(2026, 7, 30, 2, 40, tzinfo=timezone.utc),
                datetime(2026, 7, 30, 2, 42, tzinfo=timezone.utc),
            ),
        ), patch(
            "src.services.vnpy_paper_trading_service._cross_market_intraday_entry_slot_audited",
            return_value=False,
        ), patch(
            "src.services.vnpy_paper_trading_service._cross_market_session_order_activity_status",
            return_value={
                "available": True,
                "has_activity": False,
                "planned_count": 0,
                "submitted_count": 0,
            },
        ), patch(
            "src.services.vnpy_paper_trading_service._cross_market_daily_evidence_status",
            return_value={
                "ready": False,
                "run_observed": False,
                "planned_count": 0,
                "submitted_count": 0,
            },
        ):
            scan = next(
                task
                for task in build_vnpy_paper_trading_background_tasks()
                if task["name"] == "cross_market_intraday_entry_scan"
            )
            result = scan["task"]()

        self.assertFalse(result["formal_recovery"])
        self.assertEqual(result["analysis_slot"], "10:40")
        self.assertEqual(
            result["trigger_source"],
            "cross_market_intraday_entry_scan",
        )
        fake_service._cross_market_formal_run_cadence_guard.assert_not_called()
        fake_service.run_auto_trade_once.assert_called_once_with(
            trigger_source_override="cross_market_intraday_entry_scan",
            allow_cross_market_intraday_entry_recheck=False,
            max_results_override=2,
        )
        fake_lock.release.assert_called_once_with()

    def test_cross_market_intraday_scan_runs_after_ready_formal_entry(self) -> None:
        fake_service = MagicMock()
        fake_lock = MagicMock()
        fake_lock.acquire.return_value = True
        fake_service.get_settings.return_value = VnpyPaperSettings(
            enabled=True,
            account_id=9,
            auto_trade_enabled=True,
            auto_strategy=CROSS_MARKET_STRATEGY_ID,
            auto_execution_mode="vnpy_paper",
            cross_market_observation_enabled=True,
        )
        window = {
            "is_market_open_now": True,
            "session_date": "2026-07-30",
        }
        formal = {
            "ready": True,
            "reason": "fully_evidenced_run_found",
            "run_uid": "formal-ready-transient-run",
        }
        fake_service._trading_window_diagnostics.return_value = window
        fake_service._cross_market_intraday_entry_recheck_guard.return_value = {
            "block": False,
            "reason": "formal_intraday_entry_recheck_eligible",
            "recoverable_reasons": ["low_open_reclaim_unconfirmed"],
        }
        fake_service._cross_market_observation_snapshot.return_value = {
            "schema_version": 2,
            "strategy_id": CROSS_MARKET_STRATEGY_ID,
            "status": "ready",
            "missing_requirements": [],
        }
        fake_service.run_auto_trade_once.return_value = {
            "accepted": True,
            "skipped": False,
            "reason": "completed",
            "candidate_count": 2,
            "planned_count": 1,
            "submitted_count": 1,
            "skipped_count": 1,
        }

        with patch.dict(
            os.environ,
            {"DSA_AGENT_CALIBRATION_SHADOW_ENABLED": "false"},
            clear=False,
        ), patch(
            "src.services.vnpy_paper_trading_service.VnpyPaperTradingService",
            return_value=fake_service,
        ), patch(
            "src.services.vnpy_paper_trading_service._AUTO_AGENT_RUN_LOCK",
            fake_lock,
        ), patch(
            "src.services.vnpy_paper_trading_service._cross_market_daily_evidence_status",
            return_value=formal,
        ), patch(
            "src.services.vnpy_paper_trading_service._auto_trade_initial_delay_seconds",
            return_value=300,
        ), patch(
            "src.services.vnpy_paper_trading_service._cross_market_intraday_entry_slot",
            return_value=(
                "13:30",
                datetime(2026, 7, 30, 5, 30, tzinfo=timezone.utc),
                datetime(2026, 7, 30, 5, 32, tzinfo=timezone.utc),
            ),
        ), patch(
            "src.services.vnpy_paper_trading_service._cross_market_intraday_entry_slot_audited",
            return_value=False,
        ):
            recovery = next(
                task
                for task in build_vnpy_paper_trading_background_tasks()
                if task["name"] == "cross_market_intraday_entry_scan"
            )
            result = recovery["task"]()

        self.assertFalse(result["formal_recovery"])
        self.assertEqual(result["analysis_slot"], "13:30")
        self.assertEqual(result["execution_mode"], "vnpy_paper")
        self.assertEqual(
            result["trigger_source"],
            "cross_market_intraday_entry_scan",
        )
        self.assertEqual(result["submitted_count"], 1)
        fake_service.run_auto_trade_once.assert_called_once_with(
            trigger_source_override="cross_market_intraday_entry_scan",
            allow_cross_market_intraday_entry_recheck=False,
            max_results_override=2,
        )
        fake_lock.release.assert_called_once_with()

    def test_cross_market_intraday_scan_stops_after_any_session_order_activity(self) -> None:
        fake_service = MagicMock()
        fake_lock = MagicMock()
        fake_lock.acquire.return_value = True
        fake_service.get_settings.return_value = VnpyPaperSettings(
            enabled=True,
            account_id=9,
            auto_trade_enabled=True,
            auto_strategy=CROSS_MARKET_STRATEGY_ID,
            auto_execution_mode="vnpy_paper",
        )
        fake_service._trading_window_diagnostics.return_value = {
            "is_market_open_now": True,
            "session_date": "2026-07-30",
        }

        with patch.dict(
            os.environ,
            {"DSA_AGENT_CALIBRATION_SHADOW_ENABLED": "false"},
            clear=False,
        ), patch(
            "src.services.vnpy_paper_trading_service.VnpyPaperTradingService",
            return_value=fake_service,
        ), patch(
            "src.services.vnpy_paper_trading_service._AUTO_AGENT_RUN_LOCK",
            fake_lock,
        ), patch(
            "src.services.vnpy_paper_trading_service._auto_trade_initial_delay_seconds",
            return_value=300,
        ), patch(
            "src.services.vnpy_paper_trading_service._cross_market_intraday_entry_slot",
            return_value=(
                "13:30",
                datetime(2026, 7, 30, 5, 30, tzinfo=timezone.utc),
                datetime(2026, 7, 30, 5, 32, tzinfo=timezone.utc),
            ),
        ), patch(
            "src.services.vnpy_paper_trading_service._cross_market_intraday_entry_slot_audited",
            return_value=False,
        ), patch(
            "src.services.vnpy_paper_trading_service._cross_market_session_order_activity_status",
            return_value={
                "available": True,
                "has_activity": True,
                "planned_count": 1,
                "submitted_count": 1,
            },
        ), patch(
            "src.services.vnpy_paper_trading_service._cross_market_daily_evidence_status",
        ) as daily_evidence:
            scan = next(
                task
                for task in build_vnpy_paper_trading_background_tasks()
                if task["name"] == "cross_market_intraday_entry_scan"
            )
            result = scan["task"]()

        self.assertTrue(result["skipped"])
        self.assertEqual(
            result["reason"],
            "cross_market_session_order_activity_blocks_later_entry",
        )
        self.assertEqual(result["analysis_slot"], "13:30")
        self.assertEqual(result["session_order_activity"]["submitted_count"], 1)
        daily_evidence.assert_not_called()
        fake_service.run_auto_trade_once.assert_not_called()
        fake_lock.release.assert_called_once_with()

    def test_cross_market_formal_recovery_stops_after_session_order_activity(self) -> None:
        fake_service = MagicMock()
        fake_lock = MagicMock()
        fake_lock.acquire.return_value = True
        fake_service.get_settings.return_value = VnpyPaperSettings(
            enabled=True,
            account_id=9,
            auto_trade_enabled=True,
            auto_strategy=CROSS_MARKET_STRATEGY_ID,
            auto_execution_mode="vnpy_paper",
        )
        fake_service._trading_window_diagnostics.return_value = {
            "is_market_open_now": True,
            "session_date": "2026-07-30",
        }

        with patch.dict(
            os.environ,
            {"DSA_AGENT_CALIBRATION_SHADOW_ENABLED": "false"},
            clear=False,
        ), patch(
            "src.services.vnpy_paper_trading_service.VnpyPaperTradingService",
            return_value=fake_service,
        ), patch(
            "src.services.vnpy_paper_trading_service._AUTO_AGENT_RUN_LOCK",
            fake_lock,
        ), patch(
            "src.services.vnpy_paper_trading_service._auto_trade_initial_delay_seconds",
            return_value=300,
        ), patch(
            "src.services.vnpy_paper_trading_service._cross_market_daily_evidence_status",
            return_value={
                "ready": False,
                "run_observed": True,
                "planned_count": 0,
                "submitted_count": 0,
                "run_uid": "formal-degraded-run",
            },
        ), patch(
            "src.services.vnpy_paper_trading_service._cross_market_session_order_activity_status",
            return_value={
                "available": True,
                "has_activity": True,
                "planned_count": 1,
                "submitted_count": 1,
            },
        ):
            recovery = next(
                task
                for task in build_vnpy_paper_trading_background_tasks()
                if task["name"] == "cross_market_formal_recovery"
            )
            result = recovery["task"]()

        self.assertTrue(result["skipped"])
        self.assertEqual(
            result["reason"],
            "formal_recovery_order_activity_already_recorded",
        )
        fake_service._cross_market_formal_run_cadence_guard.assert_not_called()
        fake_service.run_auto_trade_once.assert_not_called()
        fake_lock.release.assert_called_once_with()

    def test_cross_market_intraday_scan_never_repeats_an_audited_slot(self) -> None:
        fake_service = MagicMock()
        fake_lock = MagicMock()
        fake_lock.acquire.return_value = True
        fake_service.get_settings.return_value = VnpyPaperSettings(
            enabled=True,
            account_id=9,
            auto_trade_enabled=True,
            auto_strategy=CROSS_MARKET_STRATEGY_ID,
            auto_execution_mode="vnpy_paper",
        )
        fake_service._trading_window_diagnostics.return_value = {
            "is_market_open_now": True,
            "session_date": "2026-07-28",
        }
        fake_service.agent_repo.list_runs.return_value = {
            "items": [{
                "run_uid": "formal-active-run",
                "status": "completed",
                "created_at": "2026-07-28T09:35:15+08:00",
                "candidate_count": 2,
                "planned_count": 1,
                "submitted_count": 0,
                "settings": {
                    "account_id": 9,
                    "auto_execution_mode": "vnpy_paper",
                },
                "diagnostics": {
                    "execution_mode": "vnpy_paper",
                    "cross_market_observation": {
                        "schema_version": 2,
                        "strategy_id": CROSS_MARKET_STRATEGY_ID,
                        "status": "unavailable",
                        "checked_at": "2026-07-28T09:35:15+08:00",
                        "missing_requirements": ["gold_signal_available"],
                    },
                },
            }],
            "total": 1,
        }

        with patch.dict(
            os.environ,
            {"DSA_AGENT_CALIBRATION_SHADOW_ENABLED": "false"},
            clear=False,
        ), patch(
            "src.services.vnpy_paper_trading_service.VnpyPaperTradingService",
            return_value=fake_service,
        ), patch(
            "src.services.vnpy_paper_trading_service._AUTO_AGENT_RUN_LOCK",
            fake_lock,
        ), patch(
            "src.services.vnpy_paper_trading_service._auto_trade_initial_delay_seconds",
            return_value=300,
        ), patch(
            "src.services.vnpy_paper_trading_service._cross_market_intraday_entry_slot",
            return_value=(
                "14:30",
                datetime(2026, 7, 28, 6, 30, tzinfo=timezone.utc),
                datetime(2026, 7, 28, 6, 32, tzinfo=timezone.utc),
            ),
        ), patch(
            "src.services.vnpy_paper_trading_service._cross_market_intraday_entry_slot_audited",
            return_value=True,
        ):
            recovery = next(
                task
                for task in build_vnpy_paper_trading_background_tasks()
                if task["name"] == "cross_market_intraday_entry_scan"
            )
            result = recovery["task"]()

        self.assertTrue(result["skipped"])
        self.assertEqual(
            result["reason"],
            "cross_market_entry_analysis_slot_already_audited",
        )
        fake_service.run_auto_trade_once.assert_not_called()
        fake_lock.acquire.assert_not_called()
        fake_lock.release.assert_not_called()

    def test_cross_market_intraday_scan_marks_guarded_slot_as_audited(self) -> None:
        fake_service = MagicMock()
        fake_lock = MagicMock()
        fake_lock.acquire.return_value = True
        fake_service.get_settings.return_value = VnpyPaperSettings(
            enabled=True,
            account_id=9,
            auto_trade_enabled=True,
            auto_strategy=CROSS_MARKET_STRATEGY_ID,
            auto_execution_mode="vnpy_paper",
        )
        fake_service._trading_window_diagnostics.return_value = {
            "is_market_open_now": True,
            "session_date": "2026-07-28",
        }
        fake_service._cross_market_formal_run_cadence_guard.return_value = {
            "block": True,
            "reason": "formal_recovery_evidence_not_ready",
        }

        with patch.dict(
            os.environ,
            {"DSA_AGENT_CALIBRATION_SHADOW_ENABLED": "false"},
            clear=False,
        ), patch(
            "src.services.vnpy_paper_trading_service.VnpyPaperTradingService",
            return_value=fake_service,
        ), patch(
            "src.services.vnpy_paper_trading_service._AUTO_AGENT_RUN_LOCK",
            fake_lock,
        ), patch(
            "src.services.vnpy_paper_trading_service._cross_market_daily_evidence_status",
            return_value={
                "ready": False,
                "run_observed": True,
                "planned_count": 0,
                "submitted_count": 0,
            },
        ), patch(
            "src.services.vnpy_paper_trading_service._auto_trade_initial_delay_seconds",
            return_value=300,
        ), patch(
            "src.services.vnpy_paper_trading_service._cross_market_intraday_entry_slot",
            return_value=(
                "13:30",
                datetime(2026, 7, 28, 5, 30, tzinfo=timezone.utc),
                datetime(2026, 7, 28, 5, 32, tzinfo=timezone.utc),
            ),
        ), patch(
            "src.services.vnpy_paper_trading_service._cross_market_intraday_entry_slot_audited",
            return_value=False,
        ):
            scan = next(
                task
                for task in build_vnpy_paper_trading_background_tasks()
                if task["name"] == "cross_market_intraday_entry_scan"
            )
            first = scan["task"]()
            second = scan["task"]()

        self.assertEqual(first["reason"], "formal_recovery_evidence_not_ready")
        self.assertEqual(first["analysis_slot"], "13:30")
        self.assertEqual(
            second["reason"],
            "cross_market_entry_analysis_slot_already_audited",
        )
        fake_service._cross_market_formal_run_cadence_guard.assert_called_once()
        fake_service.run_auto_trade_once.assert_not_called()
        fake_lock.acquire.assert_called_once_with(
            timeout=AUTO_TRADE_RUN_LOCK_WAIT_SECONDS
        )
        fake_lock.release.assert_called_once_with()

    def test_cross_market_daily_evidence_rejects_legacy_four_minute_gate(self) -> None:
        fake_service = MagicMock()
        settings = VnpyPaperSettings(
            enabled=True,
            account_id=9,
            auto_strategy=CROSS_MARKET_STRATEGY_ID,
        )
        fake_service.agent_repo.list_runs.return_value = {
            "items": [
                {
                    "run_uid": "legacy-four-minute-run",
                    "status": "completed",
                    "created_at": "2026-07-28T09:35:15+08:00",
                    "settings": {
                        "account_id": 9,
                        "auto_execution_mode": "vnpy_paper",
                    },
                    "diagnostics": {
                        "execution_mode": "vnpy_paper",
                        "cross_market_observation": {
                            "schema_version": 1,
                            "strategy_id": CROSS_MARKET_STRATEGY_ID,
                            "status": "ready",
                            "checked_at": "2026-07-28T09:35:15+08:00",
                            "evidence": {
                                "korea": {
                                    "linked_technology_gate": {
                                        "confirmation_sample_count": 5,
                                        "confirmation_span_seconds": 240.0,
                                    },
                                },
                            },
                        },
                    },
                },
            ],
            "total": 1,
        }

        result = _cross_market_daily_evidence_status(
            fake_service,
            settings,
            {
                "is_market_open_now": True,
                "session_date": "2026-07-28",
            },
        )

        self.assertFalse(result["ready"])
        self.assertEqual(result["reason"], "fully_evidenced_run_not_found")

        formal = _cross_market_daily_evidence_status(
            fake_service,
            settings,
            {
                "is_market_open_now": True,
                "session_date": "2026-07-28",
            },
            formal_only=True,
        )
        self.assertFalse(formal["ready"])
        self.assertTrue(formal["run_observed"])
        self.assertEqual(formal["run_uid"], "legacy-four-minute-run")
        self.assertEqual(
            formal["reason"],
            "fully_evidenced_formal_run_not_found",
        )

    def test_cross_market_daily_evidence_does_not_accept_partial_formal_run(
        self,
    ) -> None:
        fake_service = MagicMock()
        settings = VnpyPaperSettings(
            enabled=True,
            account_id=9,
            auto_strategy=CROSS_MARKET_STRATEGY_ID,
        )
        fake_service.agent_repo.list_runs.return_value = {
            "items": [{
                "run_uid": "partial-formal-run",
                "status": "partial",
                "created_at": "2026-07-28T09:35:15+08:00",
                "planned_count": 0,
                "submitted_count": 0,
                "settings": {
                    "account_id": 9,
                    "auto_execution_mode": "vnpy_paper",
                },
                "diagnostics": {
                    "execution_mode": "vnpy_paper",
                    "cross_market_observation": {
                        "schema_version": 2,
                        "strategy_id": CROSS_MARKET_STRATEGY_ID,
                        "status": "ready",
                        "checked_at": "2026-07-28T09:35:15+08:00",
                    },
                },
            }],
            "total": 1,
        }

        formal = _cross_market_daily_evidence_status(
            fake_service,
            settings,
            {
                "is_market_open_now": True,
                "session_date": "2026-07-28",
            },
            formal_only=True,
        )

        self.assertFalse(formal["ready"])
        self.assertTrue(formal["run_observed"])
        self.assertEqual(formal["run_status"], "partial")
        self.assertEqual(formal["run_uid"], "partial-formal-run")

    def test_cross_market_daily_evidence_ignores_late_unmarked_formal_run(
        self,
    ) -> None:
        fake_service = MagicMock()
        settings = VnpyPaperSettings(
            enabled=True,
            account_id=9,
            auto_strategy=CROSS_MARKET_STRATEGY_ID,
        )
        fake_service.agent_repo.list_runs.return_value = {
            "items": [{
                "run_uid": "late-unmarked-formal-run",
                "status": "completed",
                "created_at": "2026-08-03T10:58:00+08:00",
                "planned_count": 0,
                "submitted_count": 0,
                "settings": {
                    "account_id": 9,
                    "auto_execution_mode": "vnpy_paper",
                },
                "diagnostics": {
                    "execution_mode": "vnpy_paper",
                    "cross_market_entry_phase": "intraday_dip",
                    "cross_market_observation": {
                        "strategy_id": CROSS_MARKET_STRATEGY_ID,
                        "status": "unavailable",
                        "checked_at": "2026-08-03T10:58:00+08:00",
                        "missing_requirements": ["cn_open_available"],
                    },
                },
            }],
            "total": 1,
        }

        formal = _cross_market_daily_evidence_status(
            fake_service,
            settings,
            {
                "is_market_open_now": True,
                "session_date": "2026-08-03",
            },
            formal_only=True,
        )

        self.assertFalse(formal["ready"])
        self.assertFalse(formal["run_observed"])
        self.assertIsNone(formal["run_uid"])

    def test_cross_market_observation_reports_agent_lock_timeout(self) -> None:
        fake_service = MagicMock()
        fake_service.get_settings.return_value = VnpyPaperSettings(
            enabled=True,
            auto_trade_enabled=False,
            auto_strategy=CROSS_MARKET_STRATEGY_ID,
            cross_market_observation_enabled=True,
        )
        fake_service._trading_window_diagnostics.return_value = {
            "is_market_open_now": True,
        }
        fake_lock = MagicMock()
        fake_lock.acquire.return_value = False

        with patch.dict(
            os.environ,
            {"DSA_AGENT_CALIBRATION_SHADOW_ENABLED": "false"},
            clear=False,
        ), patch(
            "src.services.vnpy_paper_trading_service.VnpyPaperTradingService",
            return_value=fake_service,
        ), patch(
            "src.services.vnpy_paper_trading_service._AUTO_AGENT_RUN_LOCK",
            fake_lock,
        ):
            tasks = build_vnpy_paper_trading_background_tasks()
            result = next(
                task for task in tasks
                if task["name"] == "cross_market_paper_observation"
            )["task"]()

        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "observation_agent_run_lock_timeout")
        self.assertEqual(
            result["lock_wait_seconds"],
            CROSS_MARKET_OBSERVATION_LOCK_WAIT_SECONDS,
        )
        fake_lock.release.assert_not_called()
        fake_service.run_auto_trade_once.assert_not_called()

    def test_cross_market_observation_starts_after_enabled_auto_trade(self) -> None:
        fake_service = MagicMock()
        fake_service._trading_window_diagnostics.return_value = {
            "is_market_open_now": True,
        }
        settings = VnpyPaperSettings(
            enabled=True,
            auto_trade_enabled=True,
            cross_market_observation_enabled=True,
        )

        delay = _cross_market_observation_initial_delay_seconds(
            fake_service,
            settings,
        )

        self.assertEqual(delay, 3 * 60)

    def test_background_task_builder_adds_order_free_multi_market_shadow_runs(self) -> None:
        fake_service = MagicMock()
        fake_service.get_settings.return_value = SimpleNamespace(
            enabled=True,
            auto_trade_enabled=False,
            auto_interval_minutes=5,
            auto_strategy="dual_low",
        )
        fake_service.retry_due_trade_plans.return_value = {}
        fake_service.agent_repo.list_recent_runs.return_value = []
        fake_service.run_auto_trade_once.side_effect = [
            {"accepted": True, "agent_run_uid": f"run-{index}", "candidate_count": 2,
             "planned_count": 2, "submitted_count": 0}
            for index in range(2)
        ]
        env = {
            "DSA_AGENT_CALIBRATION_SHADOW_ENABLED": "true",
            "DSA_AGENT_CALIBRATION_SHADOW_PAIRS": "cn:dual_low,us:us_large_cap_momentum",
            "DSA_AGENT_CALIBRATION_SHADOW_INTERVAL_MINUTES": "720",
            "DSA_AGENT_CALIBRATION_SHADOW_MAX_RESULTS": "2",
        }

        with patch.dict(os.environ, env, clear=False), patch(
            "src.services.vnpy_paper_trading_service.VnpyPaperTradingService",
            return_value=fake_service,
        ):
            tasks = build_vnpy_paper_trading_background_tasks()

        self.assertEqual(
            [task["name"] for task in tasks],
            [
                "vnpy_paper_auto_retry",
                "agent_calibration_shadow",
                "agent_calibration_evidence",
            ],
        )
        shadow = tasks[1]
        self.assertEqual(shadow["interval_seconds"], 720 * 60)
        self.assertEqual(shadow["initial_delay_seconds"], 300)
        result = shadow["task"]()
        self.assertEqual(result["completed_count"], 2)
        self.assertEqual(result["submitted_count"], 0)
        self.assertFalse(result["submits_orders"])
        calls = fake_service.run_auto_trade_once.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(
            {(call.kwargs["market_override"], call.kwargs["strategy_override"]) for call in calls},
            {
                ("cn", "dual_low"),
                ("us", "us_large_cap_momentum"),
            },
        )
        self.assertTrue(all(call.kwargs["execution_mode_override"] == "dry_run" for call in calls))
        self.assertTrue(all(call.kwargs["ignore_auto_trade_enabled"] for call in calls))
        self.assertTrue(all(call.kwargs["calibration_shadow"] for call in calls))
        self.assertTrue(all(call.kwargs["max_results_override"] == 2 for call in calls))
        self.assertTrue(all(
            call.kwargs["trigger_source_override"] == "agent_calibration_shadow"
            for call in calls
        ))
        evidence = tasks[2]
        self.assertEqual(evidence["interval_seconds"], 720 * 60)
        self.assertEqual(evidence["initial_delay_seconds"], 600)

    def test_background_task_builder_staggers_shadow_after_first_auto_trade(self) -> None:
        fake_service = MagicMock()
        fake_service.get_settings.return_value = VnpyPaperSettings(
            enabled=True,
            auto_trade_enabled=True,
            auto_interval_minutes=1440,
            auto_trade_time_gate_enabled=True,
        )
        fake_service.agent_repo.list_recent_runs.return_value = []
        env = {
            "DSA_AGENT_CALIBRATION_SHADOW_ENABLED": "true",
            "DSA_AGENT_CALIBRATION_SHADOW_PAIRS": "cn:dual_low",
        }

        with (
            patch.dict(os.environ, env, clear=False),
            patch(
                "src.services.vnpy_paper_trading_service.VnpyPaperTradingService",
                return_value=fake_service,
            ),
            patch(
                "src.services.vnpy_paper_trading_service."
                "_auto_trade_initial_delay_seconds",
                return_value=300,
            ),
            patch(
                "src.services.vnpy_paper_trading_service."
                "_calibration_shadow_initial_delay_seconds",
                return_value=300,
            ),
        ):
            tasks = build_vnpy_paper_trading_background_tasks()

        auto_trade = next(
            task for task in tasks if task["name"] == "vnpy_paper_auto_trade"
        )
        shadow = next(
            task for task in tasks if task["name"] == "agent_calibration_shadow"
        )
        evidence = next(
            task for task in tasks if task["name"] == "agent_calibration_evidence"
        )
        self.assertEqual(auto_trade["initial_delay_seconds"], 300)
        self.assertEqual(
            shadow["initial_delay_seconds"],
            300 + CALIBRATION_SHADOW_AUTO_TRADE_STAGGER_SECONDS,
        )
        self.assertEqual(
            evidence["initial_delay_seconds"],
            600 + CALIBRATION_SHADOW_AUTO_TRADE_STAGGER_SECONDS,
        )

    def test_calibration_evidence_monitor_is_read_only_and_reports_evidence_state(self) -> None:
        fake_service = MagicMock()
        del fake_service.db
        repository_db = MagicMock(name="repository_db")
        fake_service.agent_repo.db = repository_db
        fake_service.get_settings.return_value = SimpleNamespace(
            enabled=True,
            auto_trade_enabled=False,
            auto_interval_minutes=5,
            auto_strategy="dual_low",
        )
        env = {
            "DSA_AGENT_CALIBRATION_SHADOW_ENABLED": "true",
            "DSA_AGENT_CALIBRATION_SHADOW_PAIRS": "cn:dual_low,us:us_large_cap_momentum",
        }
        evidence_payload = {
            "window_days": 90,
            "evaluation": {
                "ok": False,
                "failures": [
                    "cn:mature_samples_below_threshold",
                    "us:observation_days_below_threshold",
                ],
                "required_markets": ["cn", "us"],
                "thresholds": {"min_mature_samples": 20},
                "markets": {
                    "cn": {
                        "ok": False,
                        "failures": ["mature_samples_below_threshold"],
                        "total_runs": 9,
                        "observed_runs": 9,
                        "observation_rate_pct": 100.0,
                        "latest_mature_sample_count": 0,
                        "observation_days": 2,
                        "latest_age_hours": 1.0,
                        "latest_state": "insufficient_evidence",
                    },
                    "us": {
                        "ok": False,
                        "failures": ["observation_days_below_threshold"],
                        "total_runs": 10,
                        "observed_runs": 10,
                        "observation_rate_pct": 100.0,
                        "latest_mature_sample_count": 0,
                        "observation_days": 1,
                        "latest_age_hours": 1.0,
                        "latest_state": "insufficient_evidence",
                    },
                },
            },
        }
        ready_payload = json.loads(json.dumps(evidence_payload))
        ready_payload["evaluation"]["ok"] = True
        ready_payload["evaluation"]["failures"] = []
        for market in ready_payload["evaluation"]["markets"].values():
            market["ok"] = True
            market["failures"] = []

        with patch.dict(os.environ, env, clear=False), patch(
            "src.services.vnpy_paper_trading_service.VnpyPaperTradingService",
            return_value=fake_service,
        ), patch(
            "src.services.vnpy_paper_trading_service.collect_persisted_calibration_evidence",
            side_effect=[evidence_payload, ready_payload],
        ) as collect:
            tasks = build_vnpy_paper_trading_background_tasks()
            monitor = next(task for task in tasks if task["name"] == "agent_calibration_evidence")
            result = monitor["task"]()
            ready_result = monitor["task"]()

        self.assertEqual(collect.call_count, 2)
        latest_collect = collect.call_args
        self.assertEqual(latest_collect.args, (fake_service.agent_repo,))
        self.assertEqual(latest_collect.kwargs["markets"], ["cn", "us"])
        self.assertIs(latest_collect.kwargs["quality_service"].db, repository_db)
        self.assertTrue(result["accepted"])
        self.assertFalse(result["evidence_ready"])
        self.assertEqual(result["reason"], "calibration_evidence_pending")
        self.assertEqual(result["failure_count"], 2)
        self.assertEqual(result["market_evidence"][0]["total_runs"], 9)
        self.assertTrue(result["read_only"])
        self.assertFalse(result["creates_agent_runs"])
        self.assertFalse(result["places_orders"])
        self.assertFalse(result["submits_orders"])
        self.assertTrue(ready_result["evidence_ready"])
        self.assertEqual(ready_result["reason"], "calibration_evidence_ready")
        self.assertEqual(ready_result["failure_count"], 0)
        self.assertEqual(
            fake_service.record_calibration_evidence_transition.call_count,
            2,
        )
        fake_service.run_auto_trade_once.assert_not_called()

    def test_calibration_shadow_honors_persisted_pair_cadence_across_restarts(self) -> None:
        fake_service = MagicMock()
        fake_service.get_settings.return_value = SimpleNamespace(
            enabled=True,
            auto_trade_enabled=False,
            auto_interval_minutes=5,
            auto_strategy="dual_low",
        )
        fake_service.agent_repo.list_recent_runs.return_value = [{
            "run_uid": "recent-shadow-run",
            "status": "completed",
            "created_at": datetime.now().isoformat(),
        }]
        env = {
            "DSA_AGENT_CALIBRATION_SHADOW_ENABLED": "true",
            "DSA_AGENT_CALIBRATION_SHADOW_PAIRS": "cn:dual_low",
            "DSA_AGENT_CALIBRATION_SHADOW_INTERVAL_MINUTES": "1440",
        }

        with patch.dict(os.environ, env, clear=False), patch(
            "src.services.vnpy_paper_trading_service.VnpyPaperTradingService",
            return_value=fake_service,
        ):
            tasks = build_vnpy_paper_trading_background_tasks()

        shadow = next(task for task in tasks if task["name"] == "agent_calibration_shadow")
        result = shadow["task"]()

        self.assertTrue(result["accepted"])
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "calibration_interval_not_elapsed")
        self.assertEqual(result["attempted_count"], 0)
        self.assertEqual(result["completed_count"], 0)
        self.assertEqual(result["cadence_skipped_count"], 1)
        self.assertEqual(result["cadence_skips"][0]["latest_run_uid"], "recent-shadow-run")
        self.assertGreater(result["cadence_skips"][0]["remaining_seconds"], 0)
        fake_service.run_auto_trade_once.assert_not_called()

    def test_calibration_shadow_first_run_waits_for_all_persisted_pair_cadences(self) -> None:
        fake_service = MagicMock()
        fake_service.get_settings.return_value = SimpleNamespace(
            enabled=True,
            auto_trade_enabled=False,
            auto_interval_minutes=5,
            auto_strategy="dual_low",
        )
        latest_at = datetime.now()
        fake_service.agent_repo.list_recent_runs.side_effect = [
            [{
                "run_uid": "recent-cn-shadow-run",
                "status": "completed",
                "created_at": (latest_at - timedelta(hours=2)).isoformat(),
            }],
            [{
                "run_uid": "recent-us-shadow-run",
                "status": "completed",
                "created_at": (latest_at - timedelta(hours=1)).isoformat(),
            }],
        ]
        env = {
            "DSA_AGENT_CALIBRATION_SHADOW_ENABLED": "true",
            "DSA_AGENT_CALIBRATION_SHADOW_PAIRS": (
                "cn:dual_low,us:us_large_cap_momentum"
            ),
            "DSA_AGENT_CALIBRATION_SHADOW_INTERVAL_MINUTES": "1440",
        }

        with patch.dict(os.environ, env, clear=False), patch(
            "src.services.vnpy_paper_trading_service.VnpyPaperTradingService",
            return_value=fake_service,
        ):
            tasks = build_vnpy_paper_trading_background_tasks()

        shadow = next(task for task in tasks if task["name"] == "agent_calibration_shadow")
        evidence = next(task for task in tasks if task["name"] == "agent_calibration_evidence")
        # The US sample is newer, so the shared task waits for its later cadence.
        self.assertGreaterEqual(shadow["initial_delay_seconds"], 22 * 60 * 60)
        self.assertLessEqual(shadow["initial_delay_seconds"], 23 * 60 * 60)
        self.assertEqual(
            evidence["initial_delay_seconds"],
            shadow["initial_delay_seconds"] + 300,
        )
        fake_service.run_auto_trade_once.assert_not_called()

    def test_calibration_shadow_schedule_exposes_each_pair_without_running_agent(self) -> None:
        fake_service = MagicMock()
        fake_service.get_settings.return_value = SimpleNamespace(
            auto_market="cn",
            auto_strategy="dual_low",
        )
        latest_at = datetime.now() - timedelta(hours=1)
        fake_service.agent_repo.list_recent_runs.side_effect = [
            [{
                "run_uid": "shadow-cn",
                "status": "completed",
                "created_at": latest_at.isoformat(),
            }],
            [],
        ]
        env = {
            "DSA_AGENT_CALIBRATION_SHADOW_ENABLED": "true",
            "DSA_AGENT_CALIBRATION_SHADOW_PAIRS": "cn:dual_low,us:us_large_cap_momentum",
            "DSA_AGENT_CALIBRATION_SHADOW_INTERVAL_MINUTES": "1440",
        }

        with patch.dict(os.environ, env, clear=False):
            result = build_calibration_shadow_schedule(fake_service)

        self.assertTrue(result["enabled"])
        self.assertTrue(result["read_only"])
        self.assertFalse(result["creates_agent_runs"])
        self.assertFalse(result["places_orders"])
        self.assertEqual(result["configured_count"], 2)
        self.assertEqual(result["eligible_count"], 1)
        self.assertEqual(result["items"][0]["market"], "cn")
        self.assertFalse(result["items"][0]["eligible"])
        self.assertIsNotNone(result["items"][0]["next_eligible_at"])
        self.assertEqual(
            result["all_eligible_at"],
            result["items"][0]["next_eligible_at"],
        )
        self.assertEqual(result["items"][1]["market"], "us")
        self.assertTrue(result["items"][1]["eligible"])
        fake_service.run_auto_trade_once.assert_not_called()

    def test_calibration_shadow_runs_stale_pairs_while_skipping_fresh_pairs(self) -> None:
        fake_service = MagicMock()
        fake_service.get_settings.return_value = SimpleNamespace(
            enabled=True,
            auto_trade_enabled=False,
            auto_interval_minutes=5,
            auto_strategy="dual_low",
        )
        fresh_cn_runs = [{
            "run_uid": "fresh-cn-shadow",
            "status": "completed",
            "created_at": datetime.now().isoformat(),
        }]
        stale_us_runs = [{
            "run_uid": "stale-us-shadow",
            "status": "completed",
            "created_at": (
                datetime.now() - timedelta(hours=23, minutes=57)
            ).isoformat(),
        }]
        fake_service.agent_repo.list_recent_runs.side_effect = [
            fresh_cn_runs,
            stale_us_runs,
            fresh_cn_runs,
            stale_us_runs,
        ]
        fake_service.run_auto_trade_once.return_value = {
            "accepted": True,
            "agent_run_uid": "new-us-shadow",
            "candidate_count": 2,
            "planned_count": 1,
            "submitted_count": 0,
        }
        env = {
            "DSA_AGENT_CALIBRATION_SHADOW_ENABLED": "true",
            "DSA_AGENT_CALIBRATION_SHADOW_PAIRS": "cn:dual_low,us:us_large_cap_momentum",
            "DSA_AGENT_CALIBRATION_SHADOW_INTERVAL_MINUTES": "1440",
        }

        with patch.dict(os.environ, env, clear=False), patch(
            "src.services.vnpy_paper_trading_service.VnpyPaperTradingService",
            return_value=fake_service,
        ):
            tasks = build_vnpy_paper_trading_background_tasks()

        shadow = next(task for task in tasks if task["name"] == "agent_calibration_shadow")
        result = shadow["task"]()

        self.assertTrue(result["accepted"])
        self.assertFalse(result["skipped"])
        self.assertEqual(result["reason"], "completed")
        self.assertEqual(result["attempted_count"], 1)
        self.assertEqual(result["completed_count"], 1)
        self.assertEqual(result["cadence_skipped_count"], 1)
        self.assertEqual(result["cadence_skips"][0]["market"], "cn")
        call = fake_service.run_auto_trade_once.call_args
        self.assertEqual(call.kwargs["market_override"], "us")
        self.assertEqual(call.kwargs["strategy_override"], "us_large_cap_momentum")

    def test_calibration_shadow_isolates_combinations_and_fails_task_on_any_error(self) -> None:
        fake_service = MagicMock()
        fake_service.get_settings.return_value = SimpleNamespace(
            enabled=True,
            auto_trade_enabled=False,
            auto_interval_minutes=5,
            auto_strategy="dual_low",
        )
        fake_service.run_auto_trade_once.side_effect = [
            RuntimeError("provider unavailable"),
            {
                "accepted": True,
                "agent_run_uid": "run-us",
                "candidate_count": 2,
                "planned_count": 2,
                "submitted_count": 0,
            },
        ]
        fake_service.agent_repo.list_recent_runs.return_value = []
        env = {
            "DSA_AGENT_CALIBRATION_SHADOW_ENABLED": "true",
            "DSA_AGENT_CALIBRATION_SHADOW_PAIRS": "cn:dual_low,us:dual_low",
        }

        with patch.dict(os.environ, env, clear=False), patch(
            "src.services.vnpy_paper_trading_service.VnpyPaperTradingService",
            return_value=fake_service,
        ):
            tasks = build_vnpy_paper_trading_background_tasks()

        shadow = next(task for task in tasks if task["name"] == "agent_calibration_shadow")
        with self.assertRaisesRegex(
            RuntimeError,
            "completed=1 failed=1 pairs=cn/dual_low",
        ) as raised:
            shadow["task"]()
        self.assertEqual(fake_service.run_auto_trade_once.call_count, 2)
        self.assertEqual(raised.exception.details["completed_count"], 1)
        self.assertEqual(raised.exception.details["failed_count"], 1)
        self.assertEqual(raised.exception.details["submitted_count"], 0)
        self.assertFalse(raised.exception.details["submits_orders"])

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
        fake_service.retry_latest_calibration_alert_notification.assert_called_once_with()

    def test_background_order_recovery_isolates_calibration_alert_history_failure(self) -> None:
        fake_service = MagicMock()
        fake_service.get_settings.return_value = SimpleNamespace(
            enabled=True,
            auto_trade_enabled=False,
            auto_interval_minutes=5,
        )
        fake_service.retry_due_trade_plans.return_value = {
            "accepted": True,
            "skipped": False,
            "attempted_count": 1,
            "submitted_count": 1,
            "skipped_count": 0,
            "failed_count": 0,
        }
        fake_service.retry_latest_calibration_alert_notification.side_effect = RuntimeError(
            "alert database unavailable"
        )

        with patch(
            "src.services.vnpy_paper_trading_service.VnpyPaperTradingService",
            return_value=fake_service,
        ):
            task = build_vnpy_paper_trading_background_tasks()[0]
            result = task["task"]()

        self.assertEqual(result["submitted_count"], 1)
        self.assertEqual(
            result["calibration_alert_retry"]["reason"],
            "calibration_alert_retry_unavailable",
        )
        self.assertEqual(result["calibration_alert_retry"]["error_type"], "RuntimeError")
        self.assertFalse(result["calibration_alert_retry"]["submits_orders"])

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
        ), patch(
            "src.services.vnpy_paper_trading_service.trading_calendar.build_next_trading_window_context",
            return_value={
                "available": True,
                "is_market_open_now": False,
                "next_open_at": next_open.isoformat(),
            },
        ):
            tasks = build_vnpy_paper_trading_background_tasks()

        self.assertEqual(tasks[0]["name"], "vnpy_paper_auto_trade")
        delay = tasks[0]["initial_delay_seconds"]
        self.assertIsInstance(delay, int)
        self.assertGreaterEqual(delay, 3890)
        self.assertLessEqual(delay, 3900)

    def test_background_task_builder_moves_daily_run_to_next_session_after_same_day_run(self) -> None:
        self.service.update_settings(
            {
                "enabled": True,
                "auto_trade_enabled": True,
                "auto_interval_minutes": 1440,
                "auto_execution_mode": "paper",
                "auto_trade_time_gate_enabled": True,
            }
        )
        self.service._record_last_auto_run(
            {
                "accepted": True,
                "agent_run_uid": "same-day-paper-run",
                "market": "cn",
                "strategy": "dual_low",
                "execution_mode": "paper",
                "trigger_source": "vnpy_paper_auto",
                "candidate_count": 3,
                "submitted_count": 1,
            }
        )
        market_today = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
        current_close = datetime.now(timezone.utc) + timedelta(hours=2)
        next_open = datetime.now(timezone.utc) + timedelta(hours=22)

        with patch(
            "src.services.vnpy_paper_trading_service.VNPY_PAPER_CONFIG_PATH",
            self.config_path,
        ), patch.object(
            VnpyPaperTradingService,
            "_trading_window_diagnostics",
            return_value={
                "available": True,
                "market": "cn",
                "is_trading_day": True,
                "session_date": market_today,
                "is_market_open_now": True,
                "current_close_at": current_close.isoformat(),
                "time_gate_enforced": True,
            },
        ), patch(
            "src.services.vnpy_paper_trading_service.trading_calendar.build_next_trading_window_context",
            return_value={
                "available": True,
                "is_market_open_now": False,
                "next_open_at": next_open.isoformat(),
            },
        ) as projected_window:
            tasks = build_vnpy_paper_trading_background_tasks()

        delay = tasks[0]["initial_delay_seconds"]
        self.assertGreaterEqual(delay, (22 * 60 * 60) + 290)
        self.assertLessEqual(delay, (22 * 60 * 60) + 300)
        projection_time = projected_window.call_args.kwargs["current_time"]
        self.assertGreater(projection_time, current_close)

    def test_daily_target_after_close_does_not_skip_the_next_session(self) -> None:
        now = datetime(2026, 7, 27, 7, 20, tzinfo=timezone.utc)
        current_close = datetime(2026, 7, 27, 7, 0, tzinfo=timezone.utc)
        next_open = datetime(2026, 7, 28, 1, 30, tzinfo=timezone.utc)
        settings = replace(
            self.service.get_settings(),
            auto_market="cn",
        )

        with patch(
            "src.services.vnpy_paper_trading_service._last_auto_trade_ran_in_session",
            return_value=True,
        ), patch(
            "src.services.vnpy_paper_trading_service."
            "trading_calendar.build_next_trading_window_context",
            return_value={
                "available": True,
                "is_market_open_now": False,
                "next_open_at": next_open.isoformat(),
            },
        ) as projected_window:
            target = _next_daily_auto_trade_target(
                self.service,
                settings,
                window={
                    "is_trading_day": True,
                    "session_date": "2026-07-27",
                    "is_market_open_now": False,
                    "current_close_at": current_close.isoformat(),
                },
                now=now,
            )

        self.assertEqual(target, next_open + timedelta(minutes=5))
        self.assertEqual(
            projected_window.call_args.kwargs["current_time"],
            now + timedelta(seconds=1),
        )

    def test_cross_market_daily_target_skips_missed_opening_slot(self) -> None:
        shanghai = ZoneInfo("Asia/Shanghai")
        now = datetime(2026, 8, 3, 10, 53, tzinfo=shanghai).astimezone(
            timezone.utc
        )
        current_close = datetime(
            2026,
            8,
            3,
            15,
            0,
            tzinfo=shanghai,
        ).astimezone(timezone.utc)
        next_open = datetime(
            2026,
            8,
            4,
            9,
            30,
            tzinfo=shanghai,
        ).astimezone(timezone.utc)
        settings = replace(
            self.service.get_settings(),
            auto_market="cn",
            auto_strategy=CROSS_MARKET_STRATEGY_ID,
        )

        with patch(
            "src.services.vnpy_paper_trading_service._last_auto_trade_ran_in_session",
            return_value=False,
        ), patch(
            "src.services.vnpy_paper_trading_service."
            "trading_calendar.build_next_trading_window_context",
            return_value={
                "available": True,
                "is_market_open_now": False,
                "next_open_at": next_open.isoformat(),
            },
        ) as projected_window:
            target = _next_daily_auto_trade_target(
                self.service,
                settings,
                window={
                    "is_trading_day": True,
                    "session_date": "2026-08-03",
                    "is_market_open_now": True,
                    "current_close_at": current_close.isoformat(),
                },
                now=now,
            )

        self.assertEqual(target, next_open + timedelta(minutes=5))
        self.assertEqual(
            projected_window.call_args.kwargs["current_time"],
            current_close + timedelta(seconds=1),
        )

    def test_cross_market_daily_target_keeps_upcoming_opening_slot(self) -> None:
        shanghai = ZoneInfo("Asia/Shanghai")
        now = datetime(2026, 8, 3, 9, 32, tzinfo=shanghai).astimezone(
            timezone.utc
        )
        formal_target = datetime(
            2026,
            8,
            3,
            9,
            35,
            tzinfo=shanghai,
        ).astimezone(timezone.utc)
        settings = replace(
            self.service.get_settings(),
            auto_market="cn",
            auto_strategy=CROSS_MARKET_STRATEGY_ID,
        )

        with patch(
            "src.services.vnpy_paper_trading_service._last_auto_trade_ran_in_session",
            return_value=False,
        ), patch(
            "src.services.vnpy_paper_trading_service."
            "trading_calendar.build_next_trading_window_context",
        ) as projected_window:
            target = _next_daily_auto_trade_target(
                self.service,
                settings,
                window={
                    "is_trading_day": True,
                    "session_date": "2026-08-03",
                    "is_market_open_now": True,
                },
                now=now,
            )

        self.assertEqual(target, formal_target)
        projected_window.assert_not_called()

    def test_observation_does_not_overwrite_last_formal_auto_run(self) -> None:
        self.service._record_last_auto_run(
            {
                "accepted": True,
                "agent_run_uid": "formal-run",
                "market": "cn",
                "strategy": CROSS_MARKET_STRATEGY_ID,
                "execution_mode": "vnpy_paper",
                "trigger_source": "vnpy_paper_auto",
                "cross_market_observation": {
                    "status": "unavailable",
                    "checked_at": "2026-07-28T01:35:00+00:00",
                    "missing_requirements": ["cn_open_available"],
                },
            }
        )
        self.service._record_last_auto_run(
            {
                "accepted": True,
                "agent_run_uid": "observation-run",
                "market": "cn",
                "strategy": CROSS_MARKET_STRATEGY_ID,
                "execution_mode": "dry_run",
                "trigger_source": CROSS_MARKET_OBSERVATION_TRIGGER_SOURCE,
            }
        )

        payload = self.service._read_config_payload()

        self.assertEqual(payload["last_auto_run"]["agent_run_uid"], "observation-run")
        self.assertEqual(
            payload["last_formal_auto_run"]["agent_run_uid"],
            "formal-run",
        )
        self.assertEqual(
            payload["last_formal_auto_run"]["evidence_status"],
            "unavailable",
        )
        self.assertFalse(payload["last_formal_auto_run"]["fully_evidenced"])
        status = self.service.get_status(
            include_snapshot=False,
            include_recent_trades=False,
        )
        self.assertEqual(status["last_auto_run"]["agent_run_uid"], "observation-run")
        self.assertEqual(
            status["last_formal_auto_run"]["agent_run_uid"],
            "formal-run",
        )
        self.assertIn("vnpy_adapter", status["diagnostics"])
        self.assertIn("trading_window", status["diagnostics"])

    def test_intraday_entry_does_not_overwrite_last_formal_auto_run(self) -> None:
        self.service._record_last_auto_run(
            {
                "accepted": True,
                "agent_run_uid": "formal-run",
                "market": "cn",
                "strategy": CROSS_MARKET_STRATEGY_ID,
                "account_id": 10,
                "execution_mode": "vnpy_paper",
                "trigger_source": "vnpy_paper_auto",
            }
        )
        self.service._record_last_auto_run(
            {
                "accepted": True,
                "agent_run_uid": "intraday-run",
                "market": "cn",
                "strategy": CROSS_MARKET_STRATEGY_ID,
                "account_id": 10,
                "execution_mode": "vnpy_paper",
                "trigger_source": "cross_market_intraday_entry_scan",
            }
        )

        payload = self.service._read_config_payload()

        self.assertEqual(payload["last_auto_run"]["agent_run_uid"], "intraday-run")
        self.assertEqual(payload["last_auto_run"]["account_id"], 10)
        self.assertEqual(
            payload["last_formal_auto_run"]["agent_run_uid"],
            "formal-run",
        )
        self.assertEqual(payload["last_formal_auto_run"]["account_id"], 10)

    def test_rejected_duplicate_does_not_overwrite_last_formal_auto_run(self) -> None:
        self.service._record_last_auto_run(
            {
                "accepted": True,
                "agent_run_uid": "formal-run",
                "market": "cn",
                "strategy": CROSS_MARKET_STRATEGY_ID,
                "account_id": 10,
                "execution_mode": "vnpy_paper",
                "trigger_source": "vnpy_paper_auto",
                "cross_market_observation": {
                    "status": "ready",
                    "checked_at": "2026-08-04T01:35:10+00:00",
                    "missing_requirements": [],
                },
            }
        )
        self.service._record_last_auto_run(
            {
                "accepted": False,
                "skipped": True,
                "reason": "formal_execution_already_fully_evidenced_today",
                "agent_run_uid": None,
                "market": "cn",
                "strategy": CROSS_MARKET_STRATEGY_ID,
                "account_id": 10,
                "execution_mode": "vnpy_paper",
                "trigger_source": "vnpy_paper_auto",
            }
        )

        payload = self.service._read_config_payload()

        self.assertEqual(payload["last_auto_run"]["reason"], (
            "formal_execution_already_fully_evidenced_today"
        ))
        self.assertEqual(
            payload["last_formal_auto_run"]["agent_run_uid"],
            "formal-run",
        )
        self.assertTrue(payload["last_formal_auto_run"]["fully_evidenced"])

    def test_schedule_context_is_account_bound_and_ignores_dry_run(self) -> None:
        shanghai = ZoneInfo("Asia/Shanghai")
        run_at = datetime(2026, 8, 4, 9, 35, tzinfo=shanghai)
        settings = replace(
            self.service.get_settings(),
            account_id=10,
            auto_market="cn",
            auto_strategy=CROSS_MARKET_STRATEGY_ID,
            auto_execution_mode="vnpy_paper",
        )
        self.service._write_config_payload({
            "last_formal_auto_run": {
                "ran_at": run_at.astimezone(timezone.utc).isoformat(),
                "market": "cn",
                "strategy": CROSS_MARKET_STRATEGY_ID,
                "account_id": 9,
                "execution_mode": "vnpy_paper",
                "trigger_source": "vnpy_paper_auto",
            }
        })
        unrelated_runs = [
            {
                "created_at": run_at.isoformat(),
                "market": "cn",
                "strategy": CROSS_MARKET_STRATEGY_ID,
                "trigger_source": "vnpy_paper_auto",
                "settings": {
                    "account_id": 10,
                    "auto_execution_mode": "dry_run",
                },
            },
            {
                "created_at": run_at.isoformat(),
                "market": "cn",
                "strategy": CROSS_MARKET_STRATEGY_ID,
                "trigger_source": "vnpy_paper_auto",
                "settings": {
                    "account_id": 9,
                    "auto_execution_mode": "vnpy_paper",
                },
            },
        ]
        self.service.agent_repo.list_runs = MagicMock(
            return_value={"items": unrelated_runs, "total": len(unrelated_runs)}
        )
        window = {
            "is_trading_day": True,
            "session_date": "2026-08-04",
        }

        self.assertFalse(
            _last_auto_trade_ran_in_session(self.service, settings, window)
        )

        qualifying_run = {
            "created_at": run_at.isoformat(),
            "market": "cn",
            "strategy": CROSS_MARKET_STRATEGY_ID,
            "trigger_source": "vnpy_paper_auto",
            "settings": {
                "account_id": 10,
                "auto_execution_mode": "vnpy_paper",
            },
        }
        self.service.agent_repo.list_runs.return_value = {
            "items": [*unrelated_runs, qualifying_run],
            "total": 3,
        }

        self.assertTrue(
            _last_auto_trade_ran_in_session(self.service, settings, window)
        )

    def test_schedule_context_enriches_legacy_formal_account(self) -> None:
        shanghai = ZoneInfo("Asia/Shanghai")
        run_at = datetime(2026, 8, 4, 9, 35, tzinfo=shanghai)
        settings = replace(
            self.service.get_settings(),
            account_id=10,
            auto_market="cn",
            auto_strategy=CROSS_MARKET_STRATEGY_ID,
            auto_execution_mode="vnpy_paper",
        )
        self.service._write_config_payload({
            "last_formal_auto_run": {
                "ran_at": run_at.astimezone(timezone.utc).isoformat(),
                "agent_run_uid": "legacy-formal-run",
                "market": "cn",
                "strategy": CROSS_MARKET_STRATEGY_ID,
                "execution_mode": "vnpy_paper",
                "trigger_source": "vnpy_paper_auto",
            }
        })
        self.service.agent_repo.get_run_detail = MagicMock(
            return_value={
                "run_uid": "legacy-formal-run",
                "settings": {
                    "account_id": 10,
                    "auto_execution_mode": "vnpy_paper",
                },
            }
        )
        self.service.agent_repo.list_runs = MagicMock(
            return_value={"items": [], "total": 0}
        )

        self.assertTrue(
            _last_auto_trade_ran_in_session(
                self.service,
                settings,
                {"is_trading_day": True, "session_date": "2026-08-04"},
            )
        )
        self.service.agent_repo.get_run_detail.assert_called_once_with(
            "legacy-formal-run"
        )
        self.service.agent_repo.list_runs.assert_not_called()

    def test_daily_schedule_recovers_formal_run_from_agent_audit_after_observation(self) -> None:
        self.service.update_settings(
            {
                "enabled": True,
                "auto_trade_enabled": True,
                "auto_interval_minutes": 1440,
                "auto_strategy": CROSS_MARKET_STRATEGY_ID,
                "auto_execution_mode": "vnpy_paper",
                "auto_trade_time_gate_enabled": True,
            }
        )
        self.service._record_last_auto_run(
            {
                "accepted": True,
                "agent_run_uid": "observation-run",
                "market": "cn",
                "strategy": CROSS_MARKET_STRATEGY_ID,
                "execution_mode": "dry_run",
                "trigger_source": CROSS_MARKET_OBSERVATION_TRIGGER_SOURCE,
            }
        )
        market_today = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
        current_close = datetime.now(timezone.utc) + timedelta(hours=2)
        next_open = datetime.now(timezone.utc) + timedelta(hours=22)
        formal_created_at = datetime.now().replace(microsecond=0).isoformat()
        self.service.agent_repo.list_runs = MagicMock(
            return_value={
                "items": [
                    {
                        "market": "cn",
                        "strategy": CROSS_MARKET_STRATEGY_ID,
                        "trigger_source": "vnpy_paper_auto",
                        "status": "completed",
                        "settings": {"auto_execution_mode": "vnpy_paper"},
                        "created_at": formal_created_at,
                    }
                ],
                "total": 1,
            }
        )

        settings = self.service.get_settings()
        with (
            patch.object(
                self.service,
                "_trading_window_diagnostics",
                return_value={
                    "available": True,
                    "market": "cn",
                    "is_trading_day": True,
                    "session_date": market_today,
                    "is_market_open_now": True,
                    "current_close_at": current_close.isoformat(),
                    "time_gate_enforced": True,
                },
            ),
            patch(
                "src.services.vnpy_paper_trading_service."
                "trading_calendar.build_next_trading_window_context",
                return_value={
                    "available": True,
                    "is_market_open_now": False,
                    "next_open_at": next_open.isoformat(),
                },
            ),
        ):
            delay = _auto_trade_initial_delay_seconds(self.service, settings)

        self.assertGreaterEqual(delay, (22 * 60 * 60) + 290)
        self.assertLessEqual(delay, (22 * 60 * 60) + 300)
        self.service.agent_repo.list_runs.assert_called_once_with(
            limit=100,
            offset=0,
            trigger_source="vnpy_paper_auto",
            strategy=CROSS_MARKET_STRATEGY_ID,
            market="cn",
        )


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
