# -*- coding: utf-8 -*-
"""API tests for vn.py-style local paper trading routes."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import types
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import text

try:
    import litellm  # noqa: F401
except ModuleNotFoundError:
    sys.modules["litellm"] = MagicMock()

import src.auth as auth
from api.app import create_app
from api.deps import get_runtime_scheduler_service
from api.v1.endpoints.vnpy_paper_trading import (
    _auto_trade_timing_alignment,
    _system_health_payload,
    _with_valuation_health_history,
)
from src.config import Config
from src.repositories.portfolio_valuation_health_repo import (
    PortfolioValuationHealthRepository,
)
from src.repositories.runtime_scheduler_repo import RuntimeSchedulerRepository
from src.repositories.stock_selection_agent_repo import StockSelectionAgentRepository
from src.services.runtime_scheduler import RuntimeSchedulerService
from src.services.vnpy_paper_trading_service import VnpyPaperTradingService
from src.storage import DatabaseManager, StockSelectionAgentTradePlan


def _reset_auth_globals() -> None:
    auth._auth_enabled = None
    auth._session_secret = None
    auth._password_hash_salt = None
    auth._password_hash_stored = None
    auth._rate_limit = {}


class VnpyPaperTradingApiTestCase(unittest.TestCase):
    def setUp(self) -> None:
        _reset_auth_globals()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        self.env_path = self.data_dir / ".env"
        self.db_path = self.data_dir / "vnpy_paper_api_test.db"
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
        os.environ["DSA_RUNTIME_SCHEDULER_SUPPRESS_START"] = "true"
        os.environ["VNPY_RUNTIME_ENABLED"] = "false"
        Config.reset_instance()
        DatabaseManager.reset_instance()
        app = create_app(static_dir=self.data_dir / "empty-static")
        self.client = TestClient(app)

    def tearDown(self) -> None:
        DatabaseManager.reset_instance()
        Config.reset_instance()
        os.environ.pop("ENV_FILE", None)
        os.environ.pop("DATABASE_PATH", None)
        os.environ.pop("DSA_RUNTIME_SCHEDULER_SUPPRESS_START", None)
        os.environ.pop("VNPY_RUNTIME_ENABLED", None)
        self.temp_dir.cleanup()

    def _service(self) -> VnpyPaperTradingService:
        return VnpyPaperTradingService(config_path=self.config_path)

    def test_system_health_blocks_incomplete_required_industry_coverage(self) -> None:
        health = _system_health_payload({
            "enabled": True,
            "available": True,
            "settings": {
                "enabled": True,
                "auto_trade_enabled": True,
                "auto_execution_mode": "paper",
                "auto_max_industry_position_pct": 30,
            },
            "diagnostics": {
                "industry_exposure": {
                    "status": "partial",
                    "position_count": 2,
                    "resolved_position_count": 1,
                    "missing_position_count": 1,
                    "coverage_pct": 50,
                    "missing_symbols": ["000001"],
                    "resolution_mode": "risk_guard",
                },
            },
        })

        components = {item["key"]: item for item in health["components"]}
        industry = components["industry_exposure"]
        self.assertEqual(industry["status"], "blocked")
        self.assertTrue(industry["required"])
        self.assertEqual(industry["reason"], "industry_coverage_incomplete")
        self.assertEqual(industry["coverage_pct"], 50)
        self.assertIn("industry_coverage_incomplete", health["required_blockers"])

    def test_system_health_blocks_observed_peak_drawdown_limit(self) -> None:
        health = _system_health_payload({
            "enabled": True,
            "available": True,
            "settings": {
                "enabled": True,
                "auto_trade_enabled": True,
                "auto_execution_mode": "paper",
                "auto_max_drawdown_pct": 10,
            },
            "diagnostics": {
                "account_drawdown": {
                    "configured": True,
                    "status": "limit_reached",
                    "basis": "observed_equity_peak",
                    "equity": 105000,
                    "peak_equity": 120000,
                    "drawdown_pct": 12.5,
                    "threshold_pct": 10,
                },
            },
        })

        components = {item["key"]: item for item in health["components"]}
        drawdown = components["account_drawdown"]
        self.assertEqual(drawdown["status"], "blocked")
        self.assertTrue(drawdown["required"])
        self.assertEqual(drawdown["basis"], "observed_equity_peak")
        self.assertEqual(drawdown["peak_equity"], 120000)
        self.assertIn("account_drawdown_limit_reached", health["required_blockers"])

    def test_system_health_reports_valuation_source_health(self) -> None:
        health = _system_health_payload({
            "enabled": True,
            "available": True,
            "settings": {
                "enabled": True,
                "auto_trade_enabled": False,
                "auto_execution_mode": "paper",
            },
            "diagnostics": {"snapshot_requested": True},
            "snapshot": {
                "accounts": [{
                    "positions": [
                        {
                            "symbol": "600519",
                            "price_available": True,
                            "price_stale": False,
                            "price_source": "realtime",
                            "price_provider": "tencent",
                            "price_date": "2026-07-15",
                        },
                        {
                            "symbol": "000001",
                            "price_available": True,
                            "price_stale": True,
                            "price_source": "daily_close",
                            "price_provider": "stock_daily",
                            "price_date": "2026-07-14",
                        },
                        {
                            "symbol": "300750",
                            "price_available": False,
                            "price_stale": False,
                            "price_source": "unavailable",
                            "price_provider": None,
                        },
                        {
                            "symbol": "688981",
                            "price_available": None,
                            "price_stale": False,
                            "price_source": None,
                            "price_provider": None,
                        },
                    ],
                }],
            },
        })

        valuation = {
            item["key"]: item for item in health["components"]
        }["valuation"]
        self.assertEqual(valuation["status"], "warning")
        self.assertEqual(valuation["reason"], "valuation_degraded")
        self.assertEqual(valuation["coverage_pct"], 50.0)
        self.assertEqual(valuation["fresh_coverage_pct"], 25.0)
        self.assertEqual(valuation["available_count"], 2)
        self.assertEqual(valuation["fresh_count"], 1)
        self.assertEqual(valuation["missing_symbols"], ["300750"])
        self.assertEqual(valuation["stale_symbols"], ["000001"])
        self.assertEqual(valuation["unknown_symbols"], ["688981"])
        self.assertEqual(valuation["unknown_count"], 1)
        self.assertEqual(
            valuation["source_counts"],
            {"daily_close": 1, "realtime": 1, "unavailable": 1, "unknown": 1},
        )
        self.assertEqual(
            valuation["provider_counts"],
            {"stock_daily": 1, "tencent": 1, "unknown": 2},
        )
        self.assertEqual(valuation["oldest_price_date"], "2026-07-14")
        self.assertEqual(valuation["latest_price_date"], "2026-07-15")

    def test_valuation_health_history_deduplicates_and_aggregates_windows(self) -> None:
        repo = PortfolioValuationHealthRepository(DatabaseManager.get_instance())
        first_at = datetime(2026, 7, 10, 8, 1)
        degraded = {
            "status": "warning",
            "position_count": 4,
            "account_count": 1,
            "available_count": 3,
            "fresh_count": 2,
            "missing_count": 1,
            "unknown_count": 0,
            "stale_count": 1,
            "coverage_pct": 75.0,
            "fresh_coverage_pct": 50.0,
            "source_counts": {"daily_close": 2, "unavailable": 1, "realtime": 1},
            "provider_counts": {"stock_daily": 2, "unknown": 1, "tencent": 1},
        }
        ready = {
            **degraded,
            "status": "ready",
            "position_count": 8,
            "available_count": 8,
            "fresh_count": 8,
            "missing_count": 0,
            "stale_count": 0,
            # Aggregation must derive percentages from counts, not trust stale denormalized values.
            "coverage_pct": 1.0,
            "fresh_coverage_pct": 1.0,
            "provider_counts": {"tencent": 8},
        }

        first = repo.record_observation(degraded, observed_at=first_at)
        duplicate = repo.record_observation(ready, observed_at=first_at + timedelta(minutes=5))
        repo.record_observation(ready, observed_at=datetime(2026, 7, 16, 8, 1))
        repo.record_observation(
            {
                **ready,
                "position_count": 0,
                "account_count": 1,
                "available_count": 0,
                "fresh_count": 0,
                "coverage_pct": 100.0,
                "fresh_coverage_pct": 100.0,
                "provider_counts": {},
            },
            observed_at=datetime(2026, 7, 16, 9, 1),
        )
        trends = repo.trends(now=datetime(2026, 7, 17, 8, 1))

        self.assertTrue(first["inserted"])
        self.assertFalse(duplicate["inserted"])
        windows = {item["window_days"]: item for item in trends["windows"]}
        self.assertEqual(windows[7]["observation_count"], 3)
        self.assertEqual(windows[7]["health_observation_count"], 2)
        self.assertEqual(windows[7]["empty_position_observation_count"], 1)
        self.assertEqual(windows[7]["degraded_count"], 1)
        self.assertEqual(windows[7]["degraded_pct"], 50.0)
        self.assertEqual(windows[7]["average_coverage_pct"], 87.5)
        self.assertEqual(windows[7]["minimum_fresh_coverage_pct"], 50.0)
        self.assertEqual(windows[7]["position_observation_count"], 12)
        self.assertEqual(windows[7]["position_weighted_coverage_pct"], 91.67)
        self.assertEqual(windows[7]["position_weighted_fresh_coverage_pct"], 83.33)
        self.assertEqual(windows[7]["provider_attribution_coverage_pct"], 100.0)
        providers = {
            item["provider"]: item for item in windows[7]["provider_usage"]
        }
        self.assertEqual(windows[7]["provider_usage"][0]["provider"], "tencent")
        self.assertEqual(providers["tencent"]["position_observation_count"], 9)
        self.assertEqual(providers["tencent"]["observation_count"], 2)
        self.assertEqual(providers["tencent"]["share_pct"], 75.0)

        repo.record_observation(
            {
                **ready,
                "position_count": 0,
                "available_count": 0,
                "fresh_count": 0,
                "provider_counts": {},
            },
            observed_at=datetime(2026, 7, 16, 10, 1),
            scope="empty_accounts",
        )
        empty_window = repo.trends(
            now=datetime(2026, 7, 17, 8, 1),
            scope="empty_accounts",
        )["windows"][0]
        self.assertEqual(empty_window["observation_count"], 1)
        self.assertEqual(empty_window["health_observation_count"], 0)
        self.assertEqual(empty_window["empty_position_observation_count"], 1)
        self.assertIsNone(empty_window["average_coverage_pct"])
        self.assertIsNone(empty_window["position_weighted_coverage_pct"])
        self.assertIsNone(empty_window["degraded_pct"])
        self.assertIsNone(empty_window["latest_observed_at"])

    def test_valuation_health_history_failure_is_sanitized_and_non_blocking(self) -> None:
        payload = {
            "diagnostics": {"snapshot_requested": True},
            "snapshot": {"accounts": []},
        }
        with patch(
            "api.v1.endpoints.vnpy_paper_trading.PortfolioValuationHealthRepository",
            side_effect=RuntimeError("database unavailable at C:/secret/portfolio.db"),
        ):
            decorated = _with_valuation_health_history(payload)

        trends = decorated["diagnostics"]["valuation_health_trends"]
        self.assertFalse(trends["available"])
        self.assertEqual(trends["reason"], "valuation_health_history_unavailable")
        self.assertNotIn("error", trends)
        health = _system_health_payload(decorated)
        valuation = {item["key"]: item for item in health["components"]}["valuation"]
        self.assertEqual(valuation["status"], "ready")
        self.assertEqual(valuation["trends"], trends)

    def test_lightweight_status_reads_trends_without_recording_observation(self) -> None:
        repository = MagicMock()
        repository.trends.return_value = {
            "schema_version": 1,
            "windows": [{"window_days": 7, "observation_count": 3}],
        }
        with patch(
            "api.v1.endpoints.vnpy_paper_trading.PortfolioValuationHealthRepository",
            return_value=repository,
        ):
            decorated = _with_valuation_health_history({
                "diagnostics": {"snapshot_requested": False},
            })

        repository.record_observation.assert_not_called()
        repository.trends.assert_called_once_with()
        self.assertEqual(
            decorated["diagnostics"]["valuation_health_trends"]["windows"][0]["observation_count"],
            3,
        )

    def test_system_health_blocks_latched_drawdown_until_recovery_threshold(self) -> None:
        health = _system_health_payload({
            "enabled": True,
            "available": True,
            "settings": {
                "enabled": True,
                "auto_trade_enabled": True,
                "auto_execution_mode": "paper",
                "auto_max_drawdown_pct": 10,
                "auto_drawdown_recovery_hysteresis_pct": 2,
            },
            "diagnostics": {
                "account_drawdown": {
                    "configured": True,
                    "status": "recovery_pending",
                    "basis": "observed_equity_peak",
                    "equity": 91000,
                    "peak_equity": 100000,
                    "drawdown_pct": 9,
                    "threshold_pct": 10,
                    "recovery_hysteresis_pct": 2,
                    "recovery_threshold_pct": 8,
                    "drawdown_guard_latched": True,
                    "drawdown_guard_opened_at": "2026-07-15T09:30:00Z",
                },
            },
        })

        drawdown = {
            item["key"]: item for item in health["components"]
        }["account_drawdown"]
        self.assertEqual(drawdown["status"], "blocked")
        self.assertEqual(drawdown["reason"], "account_drawdown_recovery_pending")
        self.assertTrue(drawdown["drawdown_guard_latched"])
        self.assertEqual(drawdown["recovery_threshold_pct"], 8)
        self.assertIn("8%", drawdown["detail"])
        self.assertIn("account_drawdown_recovery_pending", health["required_blockers"])

    def test_system_health_blocks_consecutive_losses_during_cooldown(self) -> None:
        health = _system_health_payload({
            "enabled": True,
            "available": True,
            "settings": {
                "enabled": True,
                "auto_trade_enabled": True,
                "auto_execution_mode": "paper",
                "auto_consecutive_loss_limit": 3,
                "auto_consecutive_loss_cooldown_minutes": 120,
            },
            "diagnostics": {
                "consecutive_losses": {
                    "configured": True,
                    "status": "cooling_down",
                    "account_id": 1,
                    "current_streak": 3,
                    "max_streak": 4,
                    "limit": 3,
                    "cooldown_minutes": 120,
                    "cooldown_remaining_seconds": 3600,
                    "recover_at": "2026-07-15T03:00:00Z",
                    "last_closed_trade_id": 9,
                    "last_closed_trade_at": "2026-07-15T01:00:00Z",
                    "last_closed_trade_pnl": -120,
                    "guard_blocked": True,
                    "guard_opened_at": "2026-07-15T01:00:01Z",
                },
            },
        })

        component = {
            item["key"]: item for item in health["components"]
        }["consecutive_losses"]
        self.assertEqual(component["status"], "blocked")
        self.assertTrue(component["required"])
        self.assertEqual(component["reason"], "consecutive_loss_limit_reached")
        self.assertEqual(component["current_streak"], 3)
        self.assertEqual(component["limit"], 3)
        self.assertIn("consecutive_loss_limit_reached", health["required_blockers"])

    def test_system_health_reports_confirmed_runtime_connection(self) -> None:
        health = _system_health_payload({
            "enabled": True,
            "available": True,
            "settings": {
                "enabled": True,
                "auto_trade_enabled": True,
                "auto_execution_mode": "vnpy_paper",
            },
            "diagnostics": {
                "vnpy_bridge": {"available": True},
                "vnpy_runtime": {
                    "enabled": True,
                    "connect_on_start": True,
                    "connect": {
                        "request_accepted": True,
                        "connected": True,
                        "status": "connected",
                        "confirmation_source": "get_state_snapshot",
                    },
                },
            },
        })

        component = {
            item["key"]: item for item in health["components"]
        }["vnpy_bridge"]
        self.assertEqual(component["status"], "ready")
        self.assertEqual(component["reason"], "vnpy_bridge_ready")
        self.assertTrue(component["connection_confirmed"])
        self.assertEqual(
            component["connection_confirmation_source"],
            "get_state_snapshot",
        )

    def test_system_health_warns_when_runtime_connection_is_unconfirmed(self) -> None:
        health = _system_health_payload({
            "enabled": True,
            "available": True,
            "settings": {
                "enabled": True,
                "auto_trade_enabled": True,
                "auto_execution_mode": "vnpy_paper",
            },
            "diagnostics": {
                "vnpy_bridge": {"available": True},
                "vnpy_runtime": {
                    "enabled": True,
                    "connect_on_start": True,
                    "connect": {
                        "request_accepted": True,
                        "connected": False,
                        "status": "connect_requested",
                        "reason": "connection_unconfirmed",
                        "confirmation_source": "unavailable",
                    },
                },
            },
        })

        component = {
            item["key"]: item for item in health["components"]
        }["vnpy_bridge"]
        self.assertEqual(component["status"], "warning")
        self.assertEqual(
            component["reason"],
            "vnpy_gateway_connection_unconfirmed",
        )
        self.assertFalse(component["connection_confirmed"])
        self.assertIn("vnpy_gateway_connection_unconfirmed", health["warnings"])

    def test_system_health_blocks_failed_runtime_connection(self) -> None:
        health = _system_health_payload({
            "enabled": True,
            "available": True,
            "settings": {
                "enabled": True,
                "auto_trade_enabled": True,
                "auto_execution_mode": "vnpy_paper",
            },
            "diagnostics": {
                "vnpy_bridge": {"available": True},
                "vnpy_runtime": {
                    "enabled": True,
                    "connect_on_start": True,
                    "connect": {
                        "request_accepted": False,
                        "connected": False,
                        "status": "failed",
                        "reason": "connect_failed",
                        "message": "paper gateway login rejected",
                    },
                },
            },
        })

        component = {
            item["key"]: item for item in health["components"]
        }["vnpy_bridge"]
        self.assertEqual(component["status"], "blocked")
        self.assertEqual(component["reason"], "connect_failed")
        self.assertIn("connect_failed", health["required_blockers"])

    def test_system_health_explains_sanitized_production_preflight_failure(self) -> None:
        health = _system_health_payload({
            "enabled": True,
            "available": True,
            "settings": {
                "enabled": True,
                "auto_trade_enabled": True,
                "auto_execution_mode": "vnpy_paper",
            },
            "diagnostics": {
                "vnpy_bridge": {"available": True},
                "vnpy_runtime": {
                    "enabled": True,
                    "connect_on_start": True,
                    "production_preflight": {
                        "enabled": True,
                        "ok": False,
                        "failures": [
                            "builtin_gateway_not_external",
                            "default_setting_keys_missing",
                        ],
                    },
                    "connect": {
                        "attempted": False,
                        "request_accepted": False,
                        "connected": False,
                        "status": "failed",
                        "reason": "production_preflight_failed",
                    },
                },
            },
        })

        component = {
            item["key"]: item for item in health["components"]
        }["vnpy_bridge"]
        encoded = json.dumps(component, ensure_ascii=False)
        self.assertEqual(component["status"], "blocked")
        self.assertEqual(component["reason"], "production_preflight_failed")
        self.assertIn("builtin_gateway_not_external", component["detail"])
        self.assertEqual(
            component["production_preflight_failures"],
            [
                "builtin_gateway_not_external",
                "default_setting_keys_missing",
            ],
        )
        self.assertNotIn("settings_path", encoded)

    def test_system_health_blocks_injected_bridge_reporting_disconnected(self) -> None:
        health = _system_health_payload({
            "enabled": True,
            "available": True,
            "settings": {
                "enabled": True,
                "auto_trade_enabled": True,
                "auto_execution_mode": "vnpy_paper",
            },
            "diagnostics": {
                "vnpy_bridge": {
                    "available": True,
                    "connection_confirmed": False,
                    "connection_status": "disconnected",
                    "connection_confirmation_source": "gateway.connected",
                },
                "vnpy_runtime": {
                    "auto_reconnect": {
                        "enabled": True,
                        "running": True,
                        "attempt_count": 2,
                        "consecutive_failure_count": 2,
                        "current_interval_seconds": 120,
                        "max_interval_seconds": 300,
                        "confirmation_grace_seconds": 30,
                        "last_result": "failed",
                        "next_check_at": "2026-07-16T12:01:00+00:00",
                    },
                },
            },
        })

        component = {
            item["key"]: item for item in health["components"]
        }["vnpy_bridge"]
        self.assertEqual(component["status"], "blocked")
        self.assertEqual(component["reason"], "vnpy_gateway_disconnected")
        self.assertFalse(component["connection_confirmed"])
        self.assertEqual(component["connection_status"], "disconnected")
        self.assertEqual(
            component["connection_confirmation_source"],
            "gateway.connected",
        )
        self.assertTrue(component["auto_reconnect_enabled"])
        self.assertTrue(component["auto_reconnect_running"])
        self.assertEqual(component["auto_reconnect_attempt_count"], 2)
        self.assertEqual(
            component["auto_reconnect_consecutive_failure_count"],
            2,
        )
        self.assertEqual(
            component["auto_reconnect_current_interval_seconds"],
            120,
        )
        self.assertEqual(component["auto_reconnect_max_interval_seconds"], 300)
        self.assertEqual(
            component["auto_reconnect_confirmation_grace_seconds"],
            30,
        )
        self.assertEqual(component["auto_reconnect_last_result"], "failed")
        self.assertIn("自动重连监控运行中", component["detail"])

    def test_manual_gateway_reconnect_returns_runtime_audit(self) -> None:
        class RuntimeHandle:
            def __init__(self) -> None:
                self.diagnostics = {
                    "auto_reconnect": {
                        "attempt_count": 0,
                        "success_count": 0,
                    },
                    "connect": {
                        "status": "disconnected",
                        "connected": False,
                    },
                }

            def run_manual_reconnect(self):
                reconnect = self.diagnostics["auto_reconnect"]
                reconnect.update(
                    {
                        "attempt_count": 1,
                        "success_count": 1,
                        "last_check_result": "reconnect_attempted",
                        "last_result": "reconnected",
                        "last_trigger": "manual",
                    }
                )
                self.diagnostics["connect"] = {
                    "status": "connected",
                    "connected": True,
                    "reason": None,
                    "confirmation_source": "get_state_snapshot",
                }
                return reconnect

            def refresh_diagnostics(self):
                return self.diagnostics

        original = getattr(self.client.app.state, "vnpy_runtime_handle", None)
        self.client.app.state.vnpy_runtime_handle = RuntimeHandle()
        try:
            response = self.client.post("/api/v1/vnpy-paper/gateway/reconnect")
        finally:
            self.client.app.state.vnpy_runtime_handle = original

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["attempted"])
        self.assertTrue(payload["connected"])
        self.assertEqual(payload["status"], "connected")
        self.assertEqual(payload["result"], "reconnected")
        self.assertEqual(payload["reconnect"]["last_trigger"], "manual")

    def test_manual_gateway_reconnect_reports_unavailable_runtime(self) -> None:
        original = getattr(self.client.app.state, "vnpy_runtime_handle", None)
        self.client.app.state.vnpy_runtime_handle = None
        try:
            response = self.client.post("/api/v1/vnpy-paper/gateway/reconnect")
        finally:
            self.client.app.state.vnpy_runtime_handle = original

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json()["error"],
            "vnpy_runtime_unavailable",
        )

    def test_gateway_preflight_returns_sanitized_zero_connect_contract(self) -> None:
        class RuntimeHandle:
            def run_production_preflight(self):
                return {
                    "schema_version": 1,
                    "generated_at": "2026-07-22T04:00:00+00:00",
                    "ok": False,
                    "failures": [
                        "builtin_gateway_not_external",
                        "default_setting_keys_missing",
                    ],
                    "runtime_available": True,
                    "gateway_registered": True,
                    "external_gateway": False,
                    "gateway_class": "src.services.vnpy_simulated_gateway:DsaSimulatedGateway",
                    "gateway_name": "DSA_SIM",
                    "production_preflight_enabled": False,
                    "settings_provided": False,
                    "settings_valid": True,
                    "settings_source": "gateway_defaults",
                    "settings_inside_repository": False,
                    "default_setting_key_count": 5,
                    "provided_key_count": 0,
                    "missing_default_keys": ["initial_balance"],
                    "connect_attempted": False,
                    "subscriptions_created": False,
                    "orders_created": False,
                    "settings_path_exposed": False,
                    "settings_values_exposed": False,
                }

        original = getattr(self.client.app.state, "vnpy_runtime_handle", None)
        self.client.app.state.vnpy_runtime_handle = RuntimeHandle()
        try:
            response = self.client.get("/api/v1/vnpy-paper/gateway/preflight")
        finally:
            self.client.app.state.vnpy_runtime_handle = original

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["ok"])
        self.assertEqual(
            payload["failures"],
            ["builtin_gateway_not_external", "default_setting_keys_missing"],
        )
        self.assertFalse(payload["connect_attempted"])
        self.assertFalse(payload["settings_path_exposed"])
        self.assertNotIn("settings_path", payload)

    def test_gateway_preflight_reports_unavailable_runtime(self) -> None:
        original = getattr(self.client.app.state, "vnpy_runtime_handle", None)
        self.client.app.state.vnpy_runtime_handle = None
        try:
            response = self.client.get("/api/v1/vnpy-paper/gateway/preflight")
        finally:
            self.client.app.state.vnpy_runtime_handle = original

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"], "vnpy_runtime_unavailable")

    def test_status_and_manual_order_use_local_paper_account(self) -> None:
        with patch(
            "api.v1.endpoints.vnpy_paper_trading.VnpyPaperTradingService",
            side_effect=self._service,
        ), patch(
            "src.services.portfolio_service.PortfolioService._fetch_realtime_position_price",
            return_value=(10.0, "unit-test"),
        ):
            status_resp = self.client.get("/api/v1/vnpy-paper/status")
            order_resp = self.client.post(
                "/api/v1/vnpy-paper/orders",
                json={
                    "symbol": "600519",
                    "side": "buy",
                    "market": "cn",
                    "cash_amount": 1050,
                    "price": 10.0,
                },
            )
            observed_status_resp = self.client.get("/api/v1/vnpy-paper/status")

        self.assertEqual(status_resp.status_code, 200)
        self.assertTrue(status_resp.json()["enabled"])
        self.assertTrue(status_resp.json()["available"])
        health_components = {
            item["key"]: item
            for item in status_resp.json()["diagnostics"]["system_health"]["components"]
        }
        self.assertEqual(health_components["industry_exposure"]["status"], "disabled")
        self.assertEqual(
            health_components["industry_exposure"]["reason"],
            "industry_snapshot_unavailable",
        )
        self.assertEqual(health_components["industry_exposure"]["position_count"], 0)
        observed_health_components = {
            item["key"]: item
            for item in observed_status_resp.json()["diagnostics"]["system_health"]["components"]
        }
        valuation_trends = observed_health_components["valuation"]["trends"]
        self.assertEqual(valuation_trends["schema_version"], 1)
        self.assertEqual(valuation_trends["bucket_minutes"], 15)
        valuation_windows = {
            item["window_days"]: item for item in valuation_trends["windows"]
        }
        self.assertEqual(valuation_windows[7]["observation_count"], 1)
        self.assertEqual(valuation_windows[7]["average_coverage_pct"], 100.0)
        self.assertEqual(order_resp.status_code, 200)
        payload = order_resp.json()
        self.assertTrue(payload["accepted"])
        self.assertEqual(payload["quantity"], 100.0)
        self.assertEqual(payload["cash_amount"], 1000.0)
        self.assertEqual(payload["cash_amount_base"], 1000.0)
        self.assertEqual(payload["base_currency"], "CNY")
        self.assertEqual(payload["quote_currency"], "CNY")

    def test_reset_failure_fuse_endpoint_resets_baseline(self) -> None:
        service = self._service()
        service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_failure_fuse_enabled": True,
                "auto_failure_fuse_threshold": 2,
            },
            include_snapshot=False,
            include_recent_trades=False,
        )
        for index in range(2):
            run = service.agent_repo.create_run(
                run_uid=f"api-failed-run-{index}",
                trigger_source="vnpy_paper_auto",
                strategy="dual_low",
                market="cn",
                max_results=1,
                cash_per_order=1200,
            )
            service.agent_repo.complete_run(
                run_id=int(run["id"]),
                status="failed",
                candidate_count=0,
                planned_count=0,
                submitted_count=0,
                skipped_count=0,
                error="alphasift_unavailable",
                diagnostics={"stage": "alphasift_screen"},
            )

        with patch(
            "api.v1.endpoints.vnpy_paper_trading.VnpyPaperTradingService",
            side_effect=self._service,
        ):
            resp = self.client.post("/api/v1/vnpy-paper/failure-fuse/reset")

        self.assertEqual(resp.status_code, 200)
        fuse = resp.json()["diagnostics"]["failure_fuse"]
        self.assertTrue(fuse["enabled"])
        self.assertFalse(fuse["open"])
        self.assertEqual(fuse["consecutive_failure_count"], 0)
        self.assertIsNotNone(fuse["reset_at"])

    def test_manual_order_can_submit_through_injected_vnpy_main_engine(self) -> None:
        installed = _install_fake_vnpy_modules()
        main_engine = _FakeMainEngine()
        self.client.app.state.vnpy_main_engine = main_engine

        def service_factory(*, vnpy_main_engine=None, vnpy_event_engine=None):
            return VnpyPaperTradingService(
                config_path=self.config_path,
                vnpy_main_engine=vnpy_main_engine,
                vnpy_event_engine=vnpy_event_engine,
            )

        try:
            with patch(
                "api.v1.endpoints.vnpy_paper_trading.VnpyPaperTradingService",
                side_effect=service_factory,
            ):
                settings_resp = self.client.put(
                    "/api/v1/vnpy-paper/settings",
                    json={"vnpy_gateway_name": "SIM"},
                )
                order_resp = self.client.post(
                    "/api/v1/vnpy-paper/orders",
                    json={
                        "symbol": "600519",
                        "side": "buy",
                        "market": "cn",
                        "cash_amount": 1050,
                        "price": 10.0,
                        "execution_route": "vnpy_bridge",
                    },
                )
        finally:
            _restore_modules(installed)
            delattr(self.client.app.state, "vnpy_main_engine")

        self.assertEqual(settings_resp.status_code, 200)
        self.assertEqual(settings_resp.json()["settings"]["vnpy_gateway_name"], "SIM")
        self.assertEqual(order_resp.status_code, 200)
        payload = order_resp.json()
        self.assertTrue(payload["accepted"])
        self.assertEqual(payload["status"], "submitted")
        self.assertEqual(payload["source"], "vnpy_main_engine")
        self.assertEqual(payload["raw"]["vt_orderid"], "SIM.1")
        self.assertIsNone(payload["trade_id"])
        self.assertEqual(main_engine.calls[0][1], "SIM")
        self.assertEqual(main_engine.calls[0][0].symbol, "600519")

    def test_observed_manual_vnpy_order_can_be_cancelled_once(self) -> None:
        installed = _install_fake_vnpy_modules()
        main_engine = _FakeMainEngine()
        self.client.app.state.vnpy_main_engine = main_engine

        def service_factory(*, vnpy_main_engine=None, vnpy_event_engine=None):
            return VnpyPaperTradingService(
                config_path=self.config_path,
                vnpy_main_engine=vnpy_main_engine,
                vnpy_event_engine=vnpy_event_engine,
            )

        try:
            with patch(
                "api.v1.endpoints.vnpy_paper_trading.VnpyPaperTradingService",
                side_effect=service_factory,
            ):
                self.client.put(
                    "/api/v1/vnpy-paper/settings",
                    json={"vnpy_gateway_name": "SIM"},
                )
                submitted = self.client.post(
                    "/api/v1/vnpy-paper/orders",
                    json={
                        "symbol": "600519",
                        "side": "buy",
                        "market": "cn",
                        "quantity": 100,
                        "price": 10.0,
                        "execution_route": "vnpy_bridge",
                    },
                )
                observed = self.client.post(
                    "/api/v1/vnpy-paper/vnpy-events/orders",
                    json={
                        "vt_orderid": "SIM.1",
                        "status": "nottraded",
                        "symbol": "600519",
                        "side": "buy",
                        "market": "cn",
                        "volume": 100,
                        "traded": 0,
                        "price": 10.0,
                        "raw": {
                            "gateway_name": "SIM",
                            "orderid": "1",
                            "exchange": "SSE",
                        },
                    },
                )
                cancelled = self.client.post(
                    "/api/v1/vnpy-paper/orders/SIM.1/cancel",
                    json={"symbol": "600519", "market": "cn"},
                )
                duplicate = self.client.post(
                    "/api/v1/vnpy-paper/orders/SIM.1/cancel",
                    json={"symbol": "600519", "market": "cn"},
                )
        finally:
            _restore_modules(installed)
            delattr(self.client.app.state, "vnpy_main_engine")

        self.assertEqual(submitted.status_code, 200)
        self.assertEqual(observed.status_code, 200)
        self.assertEqual(cancelled.status_code, 200)
        payload = cancelled.json()
        self.assertTrue(payload["accepted"])
        self.assertEqual(payload["status"], "cancel_requested")
        self.assertEqual(payload["reason"], "vnpy_order_cancel_requested")
        self.assertEqual(payload["raw"]["vt_orderid"], "SIM.1")
        self.assertEqual(len(main_engine.cancel_calls), 1)
        cancel_request, gateway_name = main_engine.cancel_calls[0]
        self.assertEqual(gateway_name, "SIM")
        self.assertEqual(cancel_request.orderid, "1")
        self.assertEqual(cancel_request.symbol, "600519")
        self.assertEqual(duplicate.status_code, 400)
        self.assertIn("vnpy_order_not_cancelable", duplicate.json()["message"])

    def test_vnpy_trade_callback_endpoint_fills_submitted_agent_plan(self) -> None:
        installed = _install_fake_vnpy_modules()
        main_engine = _FakeMainEngine()
        self.client.app.state.vnpy_main_engine = main_engine

        def service_factory(*, vnpy_main_engine=None, vnpy_event_engine=None):
            return VnpyPaperTradingService(
                config_path=self.config_path,
                vnpy_main_engine=vnpy_main_engine,
                vnpy_event_engine=vnpy_event_engine,
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
                "api.v1.endpoints.vnpy_paper_trading.VnpyPaperTradingService",
                side_effect=service_factory,
            ), patch(
                "src.services.vnpy_paper_trading_service.AlphaSiftService",
                return_value=fake_alphasift,
            ):
                self.client.put(
                    "/api/v1/vnpy-paper/settings",
                    json={
                        "auto_trade_enabled": True,
                        "auto_trade_time_gate_enabled": False,
                        "auto_execution_mode": "vnpy_paper",
                        "vnpy_gateway_name": "SIM",
                        "auto_max_results": 1,
                        "auto_cash_per_order": 1200,
                    },
                )
                auto_resp = self.client.post("/api/v1/vnpy-paper/auto/run")
                callback_resp = self.client.post(
                    "/api/v1/vnpy-paper/vnpy-events/trades",
                    json={
                        "vt_orderid": "SIM.1",
                        "vt_tradeid": "SIM.T1",
                        "symbol": "600519",
                        "side": "buy",
                        "market": "cn",
                        "quantity": 100,
                        "price": 10.2,
                        "trade_date": "2026-07-03",
                        "raw": {"gateway_name": "SIM"},
                    },
                )
        finally:
            _restore_modules(installed)
            delattr(self.client.app.state, "vnpy_main_engine")

        self.assertEqual(auto_resp.status_code, 200)
        self.assertEqual(auto_resp.json()["orders"][0]["status"], "submitted")
        self.assertEqual(callback_resp.status_code, 200)
        callback = callback_resp.json()
        self.assertTrue(callback["accepted"])
        self.assertEqual(callback["status"], "filled")
        self.assertEqual(callback["raw"]["vt_orderid"], "SIM.1")
        self.assertIsNotNone(callback["trade_id"])

        run_uid = auto_resp.json()["agent_run_uid"]
        detail_resp = self.client.get(f"/api/v1/vnpy-paper/agent-runs/{run_uid}")
        self.assertEqual(detail_resp.status_code, 200)
        detail = detail_resp.json()
        self.assertEqual(detail["trade_plans"][0]["status"], "filled")
        self.assertEqual(detail["trade_plans"][0]["trade_id"], callback["trade_id"])
        self.assertEqual(detail["decisions"][0]["trade_id"], callback["trade_id"])

    def test_vnpy_order_account_and_position_event_endpoints_sync_diagnostics(self) -> None:
        installed = _install_fake_vnpy_modules()
        main_engine = _FakeMainEngine()
        self.client.app.state.vnpy_main_engine = main_engine

        def service_factory(*, vnpy_main_engine=None, vnpy_event_engine=None):
            return VnpyPaperTradingService(
                config_path=self.config_path,
                vnpy_main_engine=vnpy_main_engine,
                vnpy_event_engine=vnpy_event_engine,
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
                "api.v1.endpoints.vnpy_paper_trading.VnpyPaperTradingService",
                side_effect=service_factory,
            ), patch(
                "src.services.vnpy_paper_trading_service.AlphaSiftService",
                return_value=fake_alphasift,
            ):
                self.client.put(
                    "/api/v1/vnpy-paper/settings",
                    json={
                        "auto_trade_enabled": True,
                        "auto_trade_time_gate_enabled": False,
                        "auto_execution_mode": "vnpy_paper",
                        "vnpy_gateway_name": "SIM",
                        "auto_max_results": 1,
                        "auto_cash_per_order": 1200,
                    },
                )
                auto_resp = self.client.post("/api/v1/vnpy-paper/auto/run")
                order_resp = self.client.post(
                    "/api/v1/vnpy-paper/vnpy-events/orders",
                    json={
                        "vt_orderid": "SIM.1",
                        "status": "rejected",
                        "symbol": "600519",
                        "side": "buy",
                        "market": "cn",
                        "volume": 100,
                        "price": 10,
                        "rejected_reason": "unit rejected",
                    },
                )
                account_resp = self.client.post(
                    "/api/v1/vnpy-paper/vnpy-events/account",
                    json={
                        "account_id": "SIM.ACC",
                        "balance": 100000,
                        "available": 99000,
                        "frozen": 1000,
                        "currency": "CNY",
                    },
                )
                positions_resp = self.client.post(
                    "/api/v1/vnpy-paper/vnpy-events/positions",
                    json={
                        "positions": [
                            {
                                "vt_symbol": "600519.SSE",
                                "market": "cn",
                                "direction": "net",
                                "volume": 100,
                                "price": 10,
                            }
                        ],
                        "raw": {"gateway_name": "SIM"},
                    },
                )
                status_resp = self.client.get(
                    "/api/v1/vnpy-paper/status?include_snapshot=false&include_recent_trades=false"
                )
        finally:
            _restore_modules(installed)
            delattr(self.client.app.state, "vnpy_main_engine")

        self.assertEqual(auto_resp.status_code, 200)
        self.assertEqual(order_resp.status_code, 200)
        self.assertEqual(order_resp.json()["status"], "failed")
        self.assertEqual(order_resp.json()["reason"], "vnpy_order_rejected")
        self.assertEqual(account_resp.status_code, 200)
        self.assertTrue(account_resp.json()["accepted"])
        self.assertEqual(positions_resp.status_code, 200)
        self.assertTrue(positions_resp.json()["accepted"])

        run_uid = auto_resp.json()["agent_run_uid"]
        detail_resp = self.client.get(f"/api/v1/vnpy-paper/agent-runs/{run_uid}")
        self.assertEqual(detail_resp.status_code, 200)
        detail = detail_resp.json()
        self.assertEqual(detail["trade_plans"][0]["status"], "failed")
        self.assertEqual(detail["trade_plans"][0]["skip_reason"], "vnpy_order_rejected")
        self.assertEqual(detail["decisions"][0]["status"], "failed")

        sync_state = status_resp.json()["diagnostics"]["vnpy_sync_state"]
        self.assertEqual(sync_state["account"]["account_id"], "SIM.ACC")
        self.assertEqual(sync_state["position_count"], 1)
        self.assertEqual(sync_state["positions"][0]["symbol"], "600519")
        self.assertEqual(sync_state["recent_orders"][0]["status"], "rejected")

    def test_attach_vnpy_event_engine_endpoint_registers_injected_engine(self) -> None:
        installed = _install_fake_vnpy_modules()
        event_engine = _FakeEventEngine()
        self.client.app.state.vnpy_event_engine = event_engine

        def service_factory(*, vnpy_main_engine=None, vnpy_event_engine=None):
            return VnpyPaperTradingService(
                config_path=self.config_path,
                vnpy_main_engine=vnpy_main_engine,
                vnpy_event_engine=vnpy_event_engine,
            )

        try:
            with patch(
                "api.v1.endpoints.vnpy_paper_trading.VnpyPaperTradingService",
                side_effect=service_factory,
            ):
                attach_resp = self.client.post("/api/v1/vnpy-paper/vnpy-events/attach")
        finally:
            _restore_modules(installed)
            delattr(self.client.app.state, "vnpy_event_engine")
            if hasattr(self.client.app.state, "vnpy_paper_event_bridge"):
                delattr(self.client.app.state, "vnpy_paper_event_bridge")

        self.assertEqual(attach_resp.status_code, 200)
        payload = attach_resp.json()
        self.assertTrue(payload["accepted"])
        self.assertEqual(payload["status"], "attached")
        self.assertEqual(payload["raw"]["registered_count"], 4)
        self.assertIn("eOrder.", event_engine.handlers)

    def test_settings_update_reconciles_runtime_scheduler(self) -> None:
        scheduler = MagicMock()
        self.client.app.state.runtime_scheduler_service = scheduler

        with patch(
            "api.v1.endpoints.vnpy_paper_trading.VnpyPaperTradingService",
            side_effect=self._service,
        ):
            response = self.client.put(
                "/api/v1/vnpy-paper/settings",
                json={
                    "enabled": True,
                    "auto_trade_enabled": True,
                    "auto_score_weighted_allocation_enabled": True,
                    "auto_allocation_budget": 25000,
                    "auto_allocation_method": "score_inverse_volatility_20d",
                    "auto_risk_volatility_floor_pct": 7.5,
                    "auto_correlation_lookback_days": 90,
                    "auto_correlation_min_observations": 30,
                    "auto_max_pairwise_correlation": 0.75,
                    "auto_covariance_risk_penalty": 0.4,
                    "auto_interval_minutes": 5,
                    "auto_min_score": None,
                    "auto_exclude_st": True,
                    "auto_exclude_suspended": True,
                    "auto_exclude_price_limit": True,
                    "auto_min_turnover": 100000000,
                    "auto_min_data_quality_score": 72.5,
                    "auto_cross_run_quality_gate_enabled": True,
                    "auto_cross_run_horizon_days": 10,
                    "auto_cross_run_min_mature_samples": 20,
                    "auto_cross_run_min_win_rate_pct": 48,
                    "auto_cross_run_max_decisions": 300,
                    "auto_min_cash_balance": 5000,
                    "auto_max_drawdown_pct": 12,
                    "auto_drawdown_recovery_hysteresis_pct": 2,
                    "auto_consecutive_loss_limit": 3,
                    "auto_consecutive_loss_cooldown_minutes": 120,
                    "auto_max_single_position_value": 20000,
                    "auto_max_total_position_value": 80000,
                    "auto_max_total_position_pct": 80,
                    "auto_max_industry_position_value": 40000,
                    "auto_max_industry_position_pct": 40,
                    "auto_target_position_weights": {"600519": 2.5},
                    "auto_target_industry_weights": {"白酒": 8},
                    "auto_market_light_gate_enabled": True,
                    "auto_market_light_block_statuses": ["red", "yellow"],
                    "auto_market_context_max_age_days": 5,
                    "auto_market_breadth_gate_enabled": True,
                    "auto_market_breadth_min_score": 42,
                    "auto_hotspot_retreat_gate_enabled": True,
                    "auto_hotspot_retreat_min_drop": 30,
                    "auto_intraday_market_gate_enabled": True,
                    "auto_intraday_require_provider_timestamp": True,
                    "auto_intraday_index_min_change_pct": -1.5,
                    "auto_intraday_breadth_min_score": 45,
                    "auto_cross_market_gate_enabled": True,
                    "auto_cross_market_min_change_pct": -2.5,
                    "auto_failure_fuse_enabled": True,
                    "auto_failure_fuse_threshold": 2,
                    "auto_failure_fuse_auto_recovery_enabled": True,
                    "auto_failure_fuse_cooldown_minutes": 60,
                    "auto_trailing_stop_pct": 12,
                    "auto_llm_plan_enabled": True,
                    "auto_llm_review_enabled": True,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["settings"]["auto_trade_enabled"])
        self.assertTrue(response.json()["settings"]["auto_score_weighted_allocation_enabled"])
        self.assertEqual(response.json()["settings"]["auto_allocation_budget"], 25000)
        self.assertEqual(
            response.json()["settings"]["auto_allocation_method"],
            "score_inverse_volatility_20d",
        )
        self.assertEqual(response.json()["settings"]["auto_risk_volatility_floor_pct"], 7.5)
        self.assertEqual(response.json()["settings"]["auto_correlation_lookback_days"], 90)
        self.assertEqual(response.json()["settings"]["auto_correlation_min_observations"], 30)
        self.assertEqual(response.json()["settings"]["auto_max_pairwise_correlation"], 0.75)
        self.assertEqual(response.json()["settings"]["auto_covariance_risk_penalty"], 0.4)
        self.assertEqual(response.json()["settings"]["auto_interval_minutes"], 5)
        self.assertTrue(response.json()["settings"]["auto_exclude_st"])
        self.assertEqual(response.json()["settings"]["auto_min_turnover"], 100000000)
        self.assertEqual(response.json()["settings"]["auto_min_data_quality_score"], 72.5)
        self.assertTrue(response.json()["settings"]["auto_cross_run_quality_gate_enabled"])
        self.assertEqual(response.json()["settings"]["auto_cross_run_horizon_days"], 10)
        self.assertEqual(response.json()["settings"]["auto_cross_run_min_mature_samples"], 20)
        self.assertEqual(response.json()["settings"]["auto_cross_run_min_win_rate_pct"], 48)
        self.assertEqual(response.json()["settings"]["auto_cross_run_max_decisions"], 300)
        self.assertEqual(response.json()["settings"]["auto_min_cash_balance"], 5000)
        self.assertEqual(response.json()["settings"]["auto_max_drawdown_pct"], 12)
        self.assertEqual(
            response.json()["settings"]["auto_drawdown_recovery_hysteresis_pct"],
            2,
        )
        self.assertEqual(response.json()["settings"]["auto_consecutive_loss_limit"], 3)
        self.assertEqual(
            response.json()["settings"]["auto_consecutive_loss_cooldown_minutes"],
            120,
        )
        self.assertEqual(response.json()["settings"]["auto_max_single_position_value"], 20000)
        self.assertEqual(response.json()["settings"]["auto_max_total_position_value"], 80000)
        self.assertEqual(response.json()["settings"]["auto_max_total_position_pct"], 80)
        self.assertEqual(response.json()["settings"]["auto_max_industry_position_value"], 40000)
        self.assertEqual(response.json()["settings"]["auto_max_industry_position_pct"], 40)
        self.assertEqual(response.json()["settings"]["auto_target_position_weights"], {"600519": 2.5})
        self.assertEqual(response.json()["settings"]["auto_target_industry_weights"], {"白酒": 8.0})
        self.assertTrue(response.json()["settings"]["auto_market_light_gate_enabled"])
        self.assertEqual(response.json()["settings"]["auto_market_light_block_statuses"], ["red", "yellow"])
        self.assertEqual(response.json()["settings"]["auto_market_context_max_age_days"], 5)
        self.assertTrue(response.json()["settings"]["auto_market_breadth_gate_enabled"])
        self.assertEqual(response.json()["settings"]["auto_market_breadth_min_score"], 42)
        self.assertTrue(response.json()["settings"]["auto_hotspot_retreat_gate_enabled"])
        self.assertEqual(response.json()["settings"]["auto_hotspot_retreat_min_drop"], 30)
        self.assertTrue(response.json()["settings"]["auto_intraday_market_gate_enabled"])
        self.assertTrue(
            response.json()["settings"]["auto_intraday_require_provider_timestamp"]
        )
        self.assertEqual(
            response.json()["settings"]["auto_intraday_index_min_change_pct"], -1.5
        )
        self.assertEqual(
            response.json()["settings"]["auto_intraday_breadth_min_score"], 45
        )
        self.assertTrue(response.json()["settings"]["auto_cross_market_gate_enabled"])
        self.assertEqual(
            response.json()["settings"]["auto_cross_market_min_change_pct"], -2.5
        )
        self.assertTrue(response.json()["settings"]["auto_failure_fuse_enabled"])
        self.assertEqual(response.json()["settings"]["auto_failure_fuse_threshold"], 2)
        self.assertTrue(
            response.json()["settings"]["auto_failure_fuse_auto_recovery_enabled"]
        )
        self.assertEqual(
            response.json()["settings"]["auto_failure_fuse_cooldown_minutes"],
            60,
        )
        self.assertEqual(response.json()["settings"]["auto_trailing_stop_pct"], 12)
        self.assertTrue(response.json()["settings"]["auto_llm_plan_enabled"])
        self.assertTrue(response.json()["settings"]["auto_llm_review_enabled"])
        readiness = response.json()["diagnostics"]["auto_trade_readiness"]
        self.assertEqual(readiness["status"], "blocked")
        self.assertIn("scheduler_not_running", readiness["blockers"])
        self.assertIn("task_not_registered", readiness["blockers"])
        scheduler.reconcile_from_config.assert_called_once()

    def test_status_can_skip_slow_snapshot_payload(self) -> None:
        self.client.app.state.vnpy_runtime_diagnostics = {
            "enabled": False,
            "available": False,
            "reason": "disabled",
        }
        with patch(
            "api.v1.endpoints.vnpy_paper_trading.VnpyPaperTradingService",
            side_effect=self._service,
        ):
            ensure_resp = self.client.post(
                "/api/v1/vnpy-paper/account/ensure?include_snapshot=false&include_recent_trades=false"
            )
            status_resp = self.client.get(
                "/api/v1/vnpy-paper/status?include_snapshot=false&include_recent_trades=false"
            )

        self.assertEqual(ensure_resp.status_code, 200)
        self.assertEqual(status_resp.status_code, 200)
        payload = status_resp.json()
        self.assertIsNone(payload["snapshot"])
        self.assertEqual(payload["recent_trades"], [])
        self.assertEqual(payload["diagnostics"]["detail_level"], "summary")
        self.assertFalse(payload["diagnostics"]["snapshot_requested"])
        self.assertFalse(payload["diagnostics"]["recent_trades_requested"])
        self.assertIn("vnpy_adapter", payload["diagnostics"])
        self.assertIn("order_request_supported", payload["diagnostics"]["vnpy_adapter"])
        self.assertIn("vnpy_runtime", payload["diagnostics"])
        self.assertFalse(payload["diagnostics"]["vnpy_runtime"]["enabled"])
        self.assertEqual(payload["diagnostics"]["vnpy_runtime"]["reason"], "disabled")
        system_health = payload["diagnostics"]["system_health"]
        self.assertEqual(system_health["schema_version"], 1)
        self.assertEqual(system_health["status"], "disabled")
        self.assertFalse(system_health["ready"])
        self.assertEqual(system_health["next_action"], "enable_auto_trade")
        health_components = {
            item["key"]: item for item in system_health["components"]
        }
        self.assertTrue({
            "paper_ledger",
            "selection_source",
            "automation_loop",
            "scheduling_window",
            "trading_window",
            "valuation",
            "vnpy_bridge",
        }.issubset(health_components))
        self.assertEqual(health_components["paper_ledger"]["status"], "ready")
        self.assertEqual(health_components["selection_source"]["reason"], "auto_trade_disabled")
        self.assertEqual(health_components["automation_loop"]["reason"], "auto_trade_disabled")
        self.assertEqual(health_components["trading_window"]["reason"], "auto_trade_disabled")
        self.assertEqual(health_components["valuation"]["status"], "disabled")
        self.assertEqual(health_components["valuation"]["reason"], "snapshot_not_requested")
        self.assertEqual(health_components["vnpy_bridge"]["reason"], "vnpy_bridge_not_required")

    def test_reset_account_returns_clean_new_paper_account(self) -> None:
        with patch(
            "api.v1.endpoints.vnpy_paper_trading.VnpyPaperTradingService",
            side_effect=self._service,
        ), patch(
            "src.services.portfolio_service.PortfolioService._fetch_realtime_position_price",
            return_value=(10.0, "unit-test"),
        ):
            order_resp = self.client.post(
                "/api/v1/vnpy-paper/orders",
                json={
                    "symbol": "600519",
                    "side": "buy",
                    "market": "cn",
                    "quantity": 100,
                    "price": 10.0,
                },
            )
            reset_resp = self.client.post(
                "/api/v1/vnpy-paper/account/reset?include_snapshot=true&include_recent_trades=true"
            )
            accounts_resp = self.client.get("/api/v1/vnpy-paper/accounts?include_inactive=true")
            old_account_id = order_resp.json()["account_id"]
            cleanup_resp = self.client.post(
                "/api/v1/vnpy-paper/accounts/archived/cleanup",
                json={"account_ids": [old_account_id]},
            )
            cleaned_accounts_resp = self.client.get("/api/v1/vnpy-paper/accounts?include_inactive=true")
            hidden_accounts_resp = self.client.get(
                "/api/v1/vnpy-paper/accounts?include_inactive=true&include_hidden=true"
            )
            restore_resp = self.client.post(
                f"/api/v1/vnpy-paper/accounts/{old_account_id}/restore"
                "?include_snapshot=false&include_recent_trades=false"
            )
            restored_accounts_resp = self.client.get("/api/v1/vnpy-paper/accounts?include_inactive=true")

        self.assertEqual(order_resp.status_code, 200)
        self.assertEqual(reset_resp.status_code, 200)
        self.assertEqual(accounts_resp.status_code, 200)
        self.assertEqual(cleanup_resp.status_code, 200)
        self.assertEqual(cleaned_accounts_resp.status_code, 200)
        self.assertEqual(hidden_accounts_resp.status_code, 200)
        self.assertEqual(restore_resp.status_code, 200)
        self.assertEqual(restored_accounts_resp.status_code, 200)
        payload = reset_resp.json()
        accounts_payload = accounts_resp.json()
        cleanup_payload = cleanup_resp.json()
        cleaned_accounts_payload = cleaned_accounts_resp.json()
        hidden_accounts_payload = hidden_accounts_resp.json()
        restore_payload = restore_resp.json()
        restored_accounts_payload = restored_accounts_resp.json()
        self.assertTrue(payload["available"])
        self.assertNotEqual(payload["account"]["id"], old_account_id)
        self.assertEqual(payload["settings"]["account_id"], payload["account"]["id"])
        self.assertEqual(payload["snapshot"]["accounts"][0]["total_cash"], 100000.0)
        self.assertEqual(payload["snapshot"]["accounts"][0]["positions"], [])
        self.assertEqual(payload["recent_trades"], [])
        self.assertEqual(
            payload["diagnostics"]["account_reset"]["archived_account_id"],
            old_account_id,
        )
        self.assertEqual(
            payload["diagnostics"]["account_reset"]["new_account_id"],
            payload["account"]["id"],
        )
        self.assertEqual(accounts_payload["current_account_id"], payload["account"]["id"])
        self.assertEqual(accounts_payload["count"], 2)
        self.assertEqual(accounts_payload["items"][0]["id"], payload["account"]["id"])
        self.assertTrue(accounts_payload["items"][0]["is_current"])
        self.assertFalse(accounts_payload["items"][0]["archived"])
        self.assertEqual(accounts_payload["items"][1]["id"], old_account_id)
        self.assertFalse(accounts_payload["items"][1]["is_current"])
        self.assertTrue(accounts_payload["items"][1]["archived"])
        self.assertEqual(cleanup_payload["cleaned_account_ids"], [old_account_id])
        self.assertEqual(cleanup_payload["hidden_count_after"], 1)
        self.assertEqual(cleanup_payload["accounts"]["count"], 1)
        self.assertEqual(cleaned_accounts_payload["count"], 1)
        self.assertEqual(cleaned_accounts_payload["items"][0]["id"], payload["account"]["id"])
        self.assertEqual(hidden_accounts_payload["count"], 2)
        self.assertTrue(hidden_accounts_payload["items"][1]["cleanup_hidden"])
        self.assertEqual(restore_payload["account"]["id"], old_account_id)
        self.assertEqual(restore_payload["settings"]["account_id"], old_account_id)
        self.assertIsNone(restore_payload["snapshot"])
        self.assertEqual(restore_payload["recent_trades"], [])
        self.assertEqual(
            restore_payload["diagnostics"]["account_restore"]["previous_account_id"],
            payload["account"]["id"],
        )
        self.assertEqual(
            restore_payload["diagnostics"]["account_restore"]["deactivated_account_ids"],
            [payload["account"]["id"]],
        )
        self.assertEqual(restored_accounts_payload["current_account_id"], old_account_id)
        self.assertEqual(restored_accounts_payload["hidden_count"], 0)
        self.assertEqual(restored_accounts_payload["items"][0]["id"], old_account_id)
        self.assertTrue(restored_accounts_payload["items"][0]["is_current"])
        self.assertFalse(restored_accounts_payload["items"][0]["archived"])
        self.assertEqual(restored_accounts_payload["items"][1]["id"], payload["account"]["id"])
        self.assertTrue(restored_accounts_payload["items"][1]["archived"])

    def test_status_includes_runtime_scheduler_status(self) -> None:
        scheduler = MagicMock()
        scheduler.status.return_value = {
            "enabled": True,
            "running": False,
            "schedule_times": ["18:00"],
            "next_run_at": "2026-07-02T09:30:00",
            "last_run_at": None,
            "last_success_at": None,
            "last_error": None,
            "last_skipped_at": None,
            "last_skip_reason": None,
            "background_tasks": [{
                "name": "vnpy_paper_auto_trade",
                "interval_seconds": 300,
                "running": False,
                "overlap_guarded": True,
                "previous_generation_running": False,
                "last_run": None,
                "next_run_at": "2026-07-02T09:35:00",
            }],
            "task_events": [{
                "name": "vnpy_paper_auto_trade",
                "status": "skipped",
                "message": "Background task skipped: auto_trade_disabled",
                "timestamp": "2026-07-02T09:30:00",
                "duration_seconds": 0.12,
                "details": {
                    "reason": "auto_trade_disabled",
                    "submitted_count": 0,
                    "skipped_count": 0,
                },
            }],
        }
        self.client.app.state.runtime_scheduler_service = scheduler

        with patch(
            "api.v1.endpoints.vnpy_paper_trading.VnpyPaperTradingService",
            side_effect=self._service,
        ):
            response = self.client.get(
                "/api/v1/vnpy-paper/status?include_snapshot=false&include_recent_trades=false"
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["scheduler"]["enabled"])
        self.assertEqual(payload["scheduler"]["next_run_at"], "2026-07-02T09:30:00")
        self.assertEqual(payload["scheduler"]["background_tasks"][0]["name"], "vnpy_paper_auto_trade")
        self.assertEqual(payload["scheduler"]["background_tasks"][0]["next_run_at"], "2026-07-02T09:35:00")
        self.assertTrue(payload["scheduler"]["background_tasks"][0]["overlap_guarded"])
        self.assertFalse(payload["scheduler"]["background_tasks"][0]["previous_generation_running"])
        self.assertEqual(payload["scheduler"]["task_events"][0]["name"], "vnpy_paper_auto_trade")
        self.assertEqual(payload["scheduler"]["task_events"][0]["status"], "skipped")
        self.assertEqual(
            payload["scheduler"]["task_events"][0]["details"]["reason"],
            "auto_trade_disabled",
        )
        readiness = payload["diagnostics"]["auto_trade_readiness"]
        self.assertEqual(readiness["schema_version"], 1)
        self.assertEqual(readiness["status"], "disabled")
        self.assertFalse(readiness["ready"])
        self.assertEqual(readiness["next_action"], "enable_auto_trade")
        self.assertIn("auto_trade_disabled", readiness["disabled"])
        self.assertTrue(any(item["key"] == "auto_trade" for item in readiness["components"]))
        scheduler.status.assert_called_once()

    def test_readiness_uses_scheduler_loop_status_not_active_run(self) -> None:
        scheduler = MagicMock()
        scheduler.status.return_value = {
            "enabled": True,
            "running": False,
            "loop_running": True,
            "schedule_times": ["18:00"],
            "next_run_at": "2026-07-02T09:30:00",
            "last_run_at": None,
            "last_success_at": None,
            "last_error": None,
            "last_skipped_at": None,
            "last_skip_reason": None,
            "background_tasks": [{
                "name": "vnpy_paper_auto_trade",
                "interval_seconds": 300,
                "running": False,
                "last_run": None,
                "next_run_at": "2026-07-02T09:35:00",
            }],
            "task_events": [],
        }
        self.client.app.state.runtime_scheduler_service = scheduler
        service = self._service()
        service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
            },
            include_snapshot=False,
            include_recent_trades=False,
        )

        with patch(
            "api.v1.endpoints.vnpy_paper_trading.VnpyPaperTradingService",
            return_value=service,
        ), patch("src.services.alphasift_service.AlphaSiftService") as alphasift_service:
            alphasift_service.return_value.status.return_value = {
                "enabled": True,
                "available": True,
                "version": "0.2.0",
                "contract_version": "1",
                "strategy_count": 8,
            }
            response = self.client.get(
                "/api/v1/vnpy-paper/status?include_snapshot=false&include_recent_trades=false"
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        readiness = payload["diagnostics"]["auto_trade_readiness"]
        self.assertEqual(readiness["status"], "ready")
        self.assertNotIn("scheduler_not_running", readiness["blockers"])
        self.assertNotIn("task_not_registered", readiness["blockers"])
        self.assertEqual(payload["diagnostics"]["alphasift"]["strategy_count"], 8)
        backend = payload["diagnostics"]["backend"]
        self.assertEqual(backend["api_version"], "1.0.0")
        self.assertEqual(backend["vnpy_paper_contract_version"], 3)
        self.assertIsNotNone(backend["python_version"])
        self.assertIsNotNone(backend["process_started_at"])
        scheduler_component = next(
            item for item in readiness["components"] if item["key"] == "scheduler"
        )
        self.assertEqual(scheduler_component["status"], "ready")
        self.assertEqual(scheduler_component["reason"], "scheduler_loop_running")
        alphasift_component = next(
            item for item in readiness["components"] if item["key"] == "alphasift"
        )
        self.assertEqual(alphasift_component["status"], "ready")
        self.assertEqual(alphasift_component["reason"], "alphasift_available")
        system_health = payload["diagnostics"]["system_health"]
        self.assertEqual(system_health["status"], "ready")
        self.assertTrue(system_health["ready"])
        self.assertEqual(system_health["next_action"], "wait_for_next_scheduled_run")
        self.assertEqual(system_health["required_blockers"], [])
        health_components = {
            item["key"]: item for item in system_health["components"]
        }
        self.assertEqual(health_components["selection_source"]["status"], "ready")
        self.assertEqual(health_components["selection_source"]["reason"], "alphasift_ready")
        self.assertEqual(health_components["automation_loop"]["status"], "ready")
        self.assertEqual(health_components["automation_loop"]["reason"], "scheduler_loop_running")
        self.assertEqual(health_components["scheduling_window"]["reason"], "time_gate_not_enforced")
        self.assertEqual(health_components["trading_window"]["reason"], "time_gate_not_enforced")
        self.assertEqual(health_components["valuation"]["reason"], "snapshot_not_requested")
        self.assertEqual(health_components["backend_version"]["status"], "ready")
        self.assertEqual(
            health_components["backend_version"]["reason"],
            "backend_contract_compatible",
        )
        self.assertEqual(health_components["backend_version"]["contract_version"], 3)

    def test_readiness_exposes_persisted_last_auto_run_skip_reason(self) -> None:
        scheduler = MagicMock()
        scheduler.status.return_value = {
            "enabled": True,
            "running": False,
            "loop_running": True,
            "next_run_at": "2026-07-16T09:30:00+08:00",
            "background_tasks": [{
                "name": "vnpy_paper_auto_trade",
                "interval_seconds": 86400,
                "running": False,
                "next_run_at": "2026-07-16T09:30:00+08:00",
            }],
            "task_events": [],
        }
        self.client.app.state.runtime_scheduler_service = scheduler
        service = self._service()
        service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_trade_time_gate_enabled": False,
            },
            include_snapshot=False,
            include_recent_trades=False,
        )
        service._record_last_auto_run({
            "accepted": False,
            "skipped": True,
            "reason": "outside_trading_session",
            "agent_run_uid": "agent-last-skip",
            "agent_run_id": 42,
            "strategy": "dual_low",
            "market": "cn",
            "candidate_count": 0,
            "planned_count": 0,
            "submitted_count": 0,
            "skipped_count": 0,
        })

        with patch(
            "api.v1.endpoints.vnpy_paper_trading.VnpyPaperTradingService",
            return_value=service,
        ), patch("src.services.alphasift_service.AlphaSiftService") as alphasift_service:
            alphasift_service.return_value.status.return_value = {
                "enabled": True,
                "available": True,
                "strategy_count": 8,
            }
            response = self.client.get(
                "/api/v1/vnpy-paper/status?include_snapshot=false&include_recent_trades=false"
            )

        self.assertEqual(response.status_code, 200)
        readiness = response.json()["diagnostics"]["auto_trade_readiness"]
        self.assertEqual(readiness["status"], "warning")
        self.assertEqual(readiness["next_action"], "outside_trading_session")
        self.assertEqual(readiness["last_auto_run"]["agent_run_uid"], "agent-last-skip")
        self.assertEqual(readiness["last_auto_run"]["agent_run_id"], 42)
        component = next(
            item for item in readiness["components"] if item["key"] == "last_auto_run"
        )
        self.assertEqual(component["status"], "warning")
        self.assertEqual(component["reason"], "outside_trading_session")
        self.assertIn("agent-last-skip", component["detail"])
        health_component = next(
            item
            for item in response.json()["diagnostics"]["system_health"]["components"]
            if item["key"] == "last_auto_run"
        )
        self.assertEqual(health_component["status"], "warning")
        self.assertEqual(health_component["reason"], "outside_trading_session")
        self.assertEqual(health_component["agent_run_uid"], "agent-last-skip")
        self.assertEqual(health_component["agent_run_id"], 42)

    def test_readiness_warns_when_next_auto_run_is_outside_trading_window(self) -> None:
        scheduler = MagicMock()
        scheduler.status.return_value = {
            "enabled": True,
            "running": False,
            "loop_running": True,
            "schedule_times": ["18:00"],
            "next_run_at": "2026-07-03T08:30:00+08:00",
            "last_run_at": None,
            "last_success_at": None,
            "last_error": None,
            "last_skipped_at": None,
            "last_skip_reason": None,
            "background_tasks": [{
                "name": "vnpy_paper_auto_trade",
                "interval_seconds": 86400,
                "running": False,
                "last_run": None,
                "next_run_at": "2026-07-03T08:30:00+08:00",
            }],
            "task_events": [],
        }
        self.client.app.state.runtime_scheduler_service = scheduler
        service = self._service()
        service.update_settings(
            {
                "auto_trade_enabled": True,
                "auto_execution_mode": "paper",
                "auto_trade_time_gate_enabled": True,
            },
            include_snapshot=False,
            include_recent_trades=False,
        )

        with patch(
            "api.v1.endpoints.vnpy_paper_trading.VnpyPaperTradingService",
            return_value=service,
        ), patch(
            "src.services.vnpy_paper_trading_service.VnpyPaperTradingService._trading_window_diagnostics",
            return_value={
                "available": True,
                "market": "cn",
                "phase": "premarket",
                "is_market_open_now": False,
                "next_window_status": "same_session",
                "next_session_date": "2026-07-03",
                "next_open_at": "2026-07-03T09:30:00+08:00",
                "next_close_at": "2026-07-03T15:00:00+08:00",
                "time_gate_enabled": True,
                "time_gate_enforced": True,
                "execution_mode": "paper",
                "gate_reason": None,
            },
        ), patch("src.services.alphasift_service.AlphaSiftService") as alphasift_service:
            alphasift_service.return_value.status.return_value = {
                "enabled": True,
                "available": True,
                "strategy_count": 8,
            }
            response = self.client.get(
                "/api/v1/vnpy-paper/status?include_snapshot=false&include_recent_trades=false"
            )

        self.assertEqual(response.status_code, 200)
        readiness = response.json()["diagnostics"]["auto_trade_readiness"]
        self.assertEqual(readiness["status"], "warning")
        self.assertEqual(readiness["next_action"], "next_auto_run_before_window")
        self.assertEqual(
            readiness["timing_alignment"]["reason"],
            "next_auto_run_before_window",
        )
        timing_component = next(
            item for item in readiness["components"] if item["key"] == "timing_alignment"
        )
        self.assertEqual(timing_component["status"], "warning")
        self.assertEqual(timing_component["reason"], "next_auto_run_before_window")
        self.assertNotIn("scheduler_not_running", readiness["blockers"])

    def test_timing_alignment_projects_window_for_next_run_date(self) -> None:
        with patch(
            "api.v1.endpoints.vnpy_paper_trading.trading_calendar.build_next_trading_window_context",
            return_value={
                "available": True,
                "market": "cn",
                "session_date": "2026-07-23",
                "next_session_date": "2026-07-23",
                "is_market_open_now": True,
                "current_open_at": "2026-07-23T09:30:00+08:00",
                "current_close_at": "2026-07-23T15:00:00+08:00",
            },
        ) as projected_window:
            alignment = _auto_trade_timing_alignment(
                scheduler_status={},
                auto_trade_task={"next_run_at": "2026-07-23T09:35:00+08:00"},
                trading_window={
                    "market": "cn",
                    "session_date": "2026-07-22",
                    "is_market_open_now": False,
                    "next_open_at": "2026-07-22T13:00:00+08:00",
                    "next_close_at": "2026-07-22T15:00:00+08:00",
                },
                auto_trade_enabled=True,
                time_gate_enforced=True,
            )

        self.assertEqual(alignment["status"], "ready")
        self.assertEqual(alignment["reason"], "next_auto_run_in_window")
        self.assertEqual(alignment["window_session_date"], "2026-07-23")
        self.assertEqual(alignment["window_open_at"], "2026-07-23T09:30:00+08:00")
        projected_window.assert_called_once()

    def test_task_health_endpoint_summarizes_scheduler_events(self) -> None:
        scheduler = MagicMock()
        scheduler.status.return_value = {
            "enabled": True,
            "running": False,
            "schedule_times": ["18:00"],
            "next_run_at": "2026-07-02T09:30:00",
            "last_run_at": None,
            "last_success_at": None,
            "last_error": None,
            "last_skipped_at": None,
            "last_skip_reason": None,
            "background_tasks": [
                {
                    "name": "vnpy_paper_auto_trade",
                    "interval_seconds": 300,
                    "running": False,
                    "last_run": None,
                    "next_run_at": "2026-07-02T09:35:00",
                },
                {
                    "name": "vnpy_paper_auto_retry",
                    "interval_seconds": 300,
                    "running": False,
                    "last_run": None,
                    "next_run_at": "2026-07-02T09:36:00",
                },
            ],
            "task_events": [
                {
                    "name": "vnpy_paper_auto_trade",
                    "status": "completed",
                    "message": "ok",
                    "timestamp": "2026-07-02T09:31:00",
                    "duration_seconds": 0.12,
                    "details": {"submitted_count": 1},
                },
                {
                    "name": "vnpy_paper_auto_retry",
                    "status": "failed",
                    "message": "boom",
                    "timestamp": "2026-07-02T09:32:00",
                    "duration_seconds": 0.1,
                    "details": {"error": "boom"},
                },
            ],
        }
        self.client.app.state.runtime_scheduler_service = scheduler
        service = self._service()
        service.update_settings(
            {"auto_trade_enabled": True},
            include_snapshot=False,
            include_recent_trades=False,
        )

        with patch(
            "api.v1.endpoints.vnpy_paper_trading.VnpyPaperTradingService",
            return_value=service,
        ):
            response = self.client.get("/api/v1/vnpy-paper/task-health")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["overall_health"], "error")
        self.assertTrue(payload["scheduler_enabled"])
        self.assertTrue(payload["auto_trade_enabled"])
        self.assertEqual(payload["summary"]["error"], 1)
        items = {item["name"]: item for item in payload["items"]}
        self.assertEqual(items["vnpy_paper_auto_trade"]["health"], "healthy")
        self.assertEqual(items["vnpy_paper_auto_trade"]["last_event_status"], "completed")
        self.assertEqual(items["vnpy_paper_auto_retry"]["label"], "自动恢复扫描")
        self.assertEqual(items["vnpy_paper_auto_retry"]["health"], "error")
        self.assertEqual(items["vnpy_paper_auto_retry"]["reason"], "last_event_failed")
        self.assertEqual(items["vnpy_paper_auto_retry"]["last_event_message"], "boom")
        scheduler.status.assert_called_once()

    def test_task_health_endpoint_uses_persisted_scheduler_events(self) -> None:
        scheduler = MagicMock()
        scheduler.status.return_value = {
            "enabled": True,
            "running": False,
            "schedule_times": ["18:00"],
            "next_run_at": "2026-07-02T09:30:00",
            "last_run_at": None,
            "last_success_at": None,
            "last_error": None,
            "last_skipped_at": None,
            "last_skip_reason": None,
            "background_tasks": [
                {
                    "name": "vnpy_paper_auto_trade",
                    "interval_seconds": 300,
                    "running": False,
                    "last_run": None,
                    "next_run_at": "2026-07-02T09:35:00",
                },
                {
                    "name": "vnpy_paper_auto_retry",
                    "interval_seconds": 300,
                    "running": False,
                    "last_run": None,
                    "next_run_at": "2026-07-02T09:36:00",
                },
            ],
            "task_events": [],
        }
        scheduler.task_events.return_value = [{
            "name": "vnpy_paper_auto_retry",
            "status": "failed",
            "message": "persisted boom",
            "timestamp": "2026-07-02T09:32:00",
            "duration_seconds": 0.1,
            "details": {"error": "boom"},
        }]
        self.client.app.state.runtime_scheduler_service = scheduler
        service = self._service()
        service.update_settings(
            {"auto_trade_enabled": True},
            include_snapshot=False,
            include_recent_trades=False,
        )

        with patch(
            "api.v1.endpoints.vnpy_paper_trading.VnpyPaperTradingService",
            return_value=service,
        ):
            response = self.client.get("/api/v1/vnpy-paper/task-health")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        items = {item["name"]: item for item in payload["items"]}
        self.assertEqual(payload["overall_health"], "error")
        self.assertEqual(items["vnpy_paper_auto_retry"]["health"], "error")
        self.assertEqual(items["vnpy_paper_auto_retry"]["reason"], "last_event_failed")
        self.assertEqual(items["vnpy_paper_auto_retry"]["last_event_message"], "persisted boom")
        scheduler.task_events.assert_called_once_with(name=None, status=None, limit=100)

    def test_task_health_keeps_recovery_required_when_auto_buy_is_paused(self) -> None:
        scheduler = MagicMock()
        scheduler.status.return_value = {
            "enabled": True,
            "running": False,
            "loop_running": True,
            "background_tasks": [
                {
                    "name": "vnpy_paper_auto_retry",
                    "interval_seconds": 300,
                    "running": False,
                    "last_run": "2026-07-02T09:31:00",
                    "next_run_at": "2026-07-02T09:36:00",
                }
            ],
            "task_events": [
                {
                    "name": "vnpy_paper_auto_retry",
                    "status": "completed",
                    "message": "recovery completed",
                    "timestamp": "2026-07-02T09:31:00",
                    "duration_seconds": 0.1,
                    "details": {"reason": "auto_trade_disabled", "reconciled_count": 1},
                }
            ],
        }
        self.client.app.state.runtime_scheduler_service = scheduler
        service = self._service()
        service.update_settings(
            {"enabled": True, "auto_trade_enabled": False},
            include_snapshot=False,
            include_recent_trades=False,
        )

        with patch(
            "api.v1.endpoints.vnpy_paper_trading.VnpyPaperTradingService",
            return_value=service,
        ):
            response = self.client.get("/api/v1/vnpy-paper/task-health")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        items = {item["name"]: item for item in payload["items"]}
        self.assertEqual(items["vnpy_paper_auto_trade"]["health"], "disabled")
        self.assertFalse(items["vnpy_paper_auto_trade"]["required"])
        self.assertEqual(items["vnpy_paper_auto_retry"]["health"], "healthy")
        self.assertTrue(items["vnpy_paper_auto_retry"]["required"])
        self.assertTrue(items["vnpy_paper_auto_retry"]["registered"])

    def test_task_events_endpoint_filters_scheduler_events(self) -> None:
        scheduler = MagicMock()
        scheduler.task_events.return_value = [
            {
                "name": "vnpy_paper_auto_retry",
                "status": "failed",
                "message": "boom",
                "timestamp": "2026-07-02T09:32:00",
                "duration_seconds": 0.1,
                "details": {"error": "boom"},
            },
        ]
        self.client.app.state.runtime_scheduler_service = scheduler

        response = self.client.get(
            "/api/v1/vnpy-paper/task-events?name=vnpy_paper_auto_retry&status=failed&limit=10"
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["limit"], 10)
        self.assertEqual(payload["name"], "vnpy_paper_auto_retry")
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["items"][0]["name"], "vnpy_paper_auto_retry")
        self.assertEqual(payload["items"][0]["status"], "failed")
        scheduler.task_events.assert_called_once_with(
            name="vnpy_paper_auto_retry",
            status="failed",
            limit=10,
        )

    def test_task_events_endpoint_reads_persisted_scheduler_events(self) -> None:
        repo = RuntimeSchedulerRepository(DatabaseManager.get_instance())
        repo.record_task_event(
            name="vnpy_paper_auto_retry",
            status="failed",
            message="persisted failure",
            details={"error": "boom"},
            duration_seconds=0.2,
            timestamp=datetime(2026, 7, 2, 9, 32, 0),
        )
        self.client.app.state.runtime_scheduler_service = RuntimeSchedulerService(
            task_event_repository=repo,
        )

        response = self.client.get("/api/v1/vnpy-paper/task-events?status=failed&limit=5")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["items"][0]["name"], "vnpy_paper_auto_retry")
        self.assertEqual(payload["items"][0]["message"], "persisted failure")
        self.assertEqual(payload["items"][0]["details"]["error"], "boom")

    def test_task_event_summary_endpoint_aggregates_recent_events(self) -> None:
        scheduler = MagicMock()
        scheduler.task_events.return_value = [
            {
                "name": "vnpy_paper_auto_trade",
                "status": "completed",
                "message": "ok",
                "timestamp": "2026-07-02T09:31:00",
                "duration_seconds": 0.2,
                "details": {"submitted_count": 1},
            },
            {
                "name": "vnpy_paper_auto_trade",
                "status": "skipped",
                "message": "skip",
                "timestamp": "2026-07-02T09:32:00",
                "duration_seconds": 0.1,
                "details": {"reason": "auto_trade_disabled"},
            },
            {
                "name": "vnpy_paper_auto_retry",
                "status": "failed",
                "message": "boom",
                "timestamp": "2026-07-02T09:33:00",
                "duration_seconds": 0.3,
                "details": {"error": "boom"},
            },
        ]
        self.client.app.state.runtime_scheduler_service = scheduler

        response = self.client.get("/api/v1/vnpy-paper/task-event-summary?limit=20")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["limit"], 20)
        self.assertEqual(payload["count"], 3)
        self.assertEqual(payload["status_counts"]["completed"], 1)
        self.assertEqual(payload["status_counts"]["skipped"], 1)
        self.assertEqual(payload["status_counts"]["failed"], 1)
        items = {item["name"]: item for item in payload["items"]}
        self.assertEqual(items["vnpy_paper_auto_trade"]["total"], 2)
        self.assertEqual(items["vnpy_paper_auto_trade"]["skipped_count"], 1)
        self.assertEqual(items["vnpy_paper_auto_trade"]["last_skipped_at"], "2026-07-02T09:32:00")
        self.assertEqual(items["vnpy_paper_auto_retry"]["failed_count"], 1)
        self.assertEqual(items["vnpy_paper_auto_retry"]["failure_rate_pct"], 100.0)
        self.assertEqual(items["vnpy_paper_auto_retry"]["last_failed_at"], "2026-07-02T09:33:00")
        scheduler.task_events.assert_called_once_with(name=None, status=None, limit=20)

    def test_task_metrics_endpoint_aggregates_terminal_runs_over_time_window(self) -> None:
        scheduler = MagicMock()
        scheduler.task_events.return_value = [
            {
                "name": "vnpy_paper_auto_trade",
                "status": "started",
                "message": "start",
                "timestamp": "2026-07-12T09:30:00",
                "details": {},
            },
            {
                "name": "vnpy_paper_auto_trade",
                "status": "completed",
                "message": "ok",
                "timestamp": "2026-07-12T09:31:00",
                "duration_seconds": 1.0,
                "details": {},
            },
            {
                "name": "vnpy_paper_auto_trade",
                "status": "failed",
                "message": "failed once",
                "timestamp": "2026-07-13T09:31:00",
                "duration_seconds": 3.0,
                "details": {"error": "boom"},
            },
            {
                "name": "vnpy_paper_auto_retry",
                "status": "failed",
                "message": "failed twice",
                "timestamp": "2026-07-13T09:32:00",
                "duration_seconds": 2.0,
                "details": {"error": "boom"},
            },
        ]
        self.client.app.state.runtime_scheduler_service = scheduler

        response = self.client.get("/api/v1/vnpy-paper/task-metrics?days=7")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["window_days"], 7)
        self.assertEqual(payload["event_count"], 4)
        self.assertEqual(payload["started_count"], 1)
        self.assertEqual(payload["run_count"], 3)
        self.assertEqual(payload["completed_count"], 1)
        self.assertEqual(payload["failed_count"], 2)
        self.assertEqual(payload["success_rate_pct"], 33.33)
        self.assertEqual(payload["failure_rate_pct"], 66.67)
        self.assertEqual(payload["avg_duration_seconds"], 2.0)
        self.assertEqual(payload["p95_duration_seconds"], 3.0)
        self.assertEqual(payload["current_failure_streak"], 2)
        self.assertEqual(len(payload["daily"]), 2)
        self.assertEqual(payload["daily"][0]["success_rate_pct"], 100.0)
        self.assertEqual(payload["daily"][1]["failure_rate_pct"], 100.0)
        tasks = {item["name"]: item for item in payload["items"]}
        self.assertEqual(tasks["vnpy_paper_auto_trade"]["run_count"], 2)
        self.assertEqual(tasks["vnpy_paper_auto_trade"]["failure_rate_pct"], 50.0)
        call = scheduler.task_events.call_args.kwargs
        self.assertEqual(call["limit"], 5000)
        self.assertIsInstance(call["started_at"], datetime)

    def test_auto_run_accepts_temporary_dry_run_override(self) -> None:
        service = MagicMock()
        service.run_auto_trade_once.return_value = {
            "accepted": True,
            "skipped": False,
            "reason": None,
            "strategy": "dual_low",
            "market": "cn",
            "candidate_count": 1,
            "planned_count": 1,
            "submitted_count": 0,
            "skipped_count": 0,
            "orders": [],
            "messages": [],
        }

        with patch(
            "api.v1.endpoints.vnpy_paper_trading.VnpyPaperTradingService",
            return_value=service,
        ):
            response = self.client.post(
                "/api/v1/vnpy-paper/auto/run",
                json={
                    "execution_mode": "dry_run",
                    "ignore_auto_trade_enabled": True,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["planned_count"], 1)
        service.run_auto_trade_once.assert_called_once_with(
            execution_mode_override="dry_run",
            ignore_auto_trade_enabled=True,
        )

    def test_auto_run_api_persists_agent_audit_and_filled_trade_e2e(self) -> None:
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
                    "industry": "白酒",
                    "trading_status": "未停牌",
                    "limit_status": "未涨停",
                },
            ],
            "warnings": [],
            "source_errors": [],
        }

        with patch(
            "api.v1.endpoints.vnpy_paper_trading.VnpyPaperTradingService",
            side_effect=self._service,
        ), patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ), patch(
            "src.services.portfolio_service.PortfolioService._fetch_realtime_position_price",
            return_value=(10.0, "unit-test"),
        ):
            settings_resp = self.client.put(
                "/api/v1/vnpy-paper/settings?include_snapshot=false&include_recent_trades=false",
                json={
                    "auto_trade_enabled": True,
                    "auto_trade_time_gate_enabled": False,
                    "auto_strategy": "dual_low",
                    "auto_market": "cn",
                    "auto_max_results": 1,
                    "auto_cash_per_order": 1200,
                    "auto_min_score": 50,
                },
            )
            run_resp = self.client.post("/api/v1/vnpy-paper/auto/run")
            run_uid = run_resp.json()["agent_run_uid"]
            detail_resp = self.client.get(f"/api/v1/vnpy-paper/agent-runs/{run_uid}")
            performance_resp = self.client.get("/api/v1/vnpy-paper/performance?run_limit=10")
            filtered_performance_resp = self.client.get(
                "/api/v1/vnpy-paper/performance"
                "?run_limit=10&created_from=2999-01-01T00:00:00"
            )

        self.assertEqual(settings_resp.status_code, 200)
        self.assertEqual(run_resp.status_code, 200)
        run_payload = run_resp.json()
        self.assertTrue(run_payload["accepted"])
        self.assertEqual(run_payload["candidate_count"], 1)
        self.assertEqual(run_payload["submitted_count"], 1)
        self.assertEqual(run_payload["skipped_count"], 0)
        self.assertTrue(run_payload["orders"][0]["accepted"])
        self.assertEqual(run_payload["orders"][0]["quantity"], 100.0)
        self.assertIsNotNone(run_payload["orders"][0]["trade_id"])

        self.assertEqual(detail_resp.status_code, 200)
        detail = detail_resp.json()
        self.assertEqual(detail["status"], "completed")
        self.assertEqual(detail["candidate_count"], 1)
        self.assertEqual(detail["submitted_count"], 1)
        self.assertEqual(detail["decisions"][0]["symbol"], "600519")
        self.assertEqual(detail["decisions"][0]["status"], "filled")
        self.assertEqual(detail["decisions"][0]["position_plan"]["symbol"], "600519")
        self.assertEqual(detail["decisions"][0]["risk_review"]["status"], "passed")
        self.assertEqual(detail["decisions"][0]["agent_review"]["reviewer"], "rule_agent_v1")
        self.assertEqual(
            detail["decisions"][0]["order_result"]["strategy_evidence"],
            detail["decisions"][0]["strategy_evidence"],
        )
        self.assertEqual(detail["trade_plans"][0]["status"], "filled")
        self.assertEqual(detail["trade_plans"][0]["trade_id"], run_payload["orders"][0]["trade_id"])
        self.assertEqual(detail["portfolio_change"]["status"], "changed")
        self.assertEqual(detail["portfolio_change"]["items"][0]["symbol"], "600519")
        self.assertEqual(detail["portfolio_change"]["items"][0]["net_quantity"], 100.0)
        self.assertEqual(
            detail["portfolio_change"]["items"][0]["trade_ids"],
            [run_payload["orders"][0]["trade_id"]],
        )
        self.assertIn("timeline", detail)
        self.assertTrue(any(item["stage"] == "trade_plans" for item in detail["timeline"]))
        self.assertTrue(any(item["stage"] == "portfolio_change" for item in detail["timeline"]))

        self.assertEqual(performance_resp.status_code, 200)
        performance = performance_resp.json()
        self.assertEqual(performance["agent"]["candidate_count"], 1)
        self.assertEqual(performance["agent"]["submitted_count"], 1)
        self.assertEqual(performance["trade_metrics"]["trade_count"], 1)
        self.assertEqual(performance["trade_metrics"]["buy_count"], 1)
        self.assertEqual(performance["trade_metrics"]["gross_turnover"], 1000.0)
        self.assertEqual(performance["risk_metrics"]["turnover_pct"], 1.0)
        self.assertEqual(performance["risk_metrics"]["current_exposure_pct"], 1.0)
        self.assertGreaterEqual(len(performance["daily_returns"]), 1)
        self.assertEqual(performance["daily_returns"][-1]["trade_count"], 1)
        self.assertGreaterEqual(len(performance["monthly_returns"]), 1)
        self.assertEqual(performance["monthly_returns"][-1]["trade_count"], 1)
        self.assertEqual(performance["strategy_attribution"][0]["key"], "dual_low")
        self.assertEqual(performance["industry_attribution"][0]["key"], "白酒")
        self.assertEqual(performance["trade_plan_status_counts"], {"filled": 1})
        self.assertEqual(performance["traded_symbols"][0], {"key": "600519", "count": 1})

        self.assertEqual(filtered_performance_resp.status_code, 200)
        filtered_performance = filtered_performance_resp.json()
        self.assertEqual(filtered_performance["run_window"]["run_count"], 0)
        self.assertEqual(filtered_performance["run_window"]["total"], 0)
        self.assertEqual(filtered_performance["run_window"]["created_from"], "2999-01-01T00:00:00")
        self.assertEqual(filtered_performance["agent"]["submitted_count"], 0)
        self.assertEqual(filtered_performance["strategy_attribution"], [])
        self.assertEqual(filtered_performance["industry_attribution"], [])

    def test_auto_run_api_persists_risk_rejection_matrix_without_trades_e2e(self) -> None:
        fake_alphasift = MagicMock()
        fake_alphasift.screen.return_value = {
            "quality_status": "ok",
            "candidates": [
                {
                    "code": "600519",
                    "name": "贵州茅台",
                    "score": 90,
                    "price": 10.0,
                    "amount": 200000000,
                },
                {
                    "code": "000001",
                    "name": "ST测试",
                    "score": 85,
                    "price": 10.0,
                    "amount": 200000000,
                },
                {
                    "code": "300750",
                    "name": "宁德时代",
                    "score": 80,
                    "price": 20.0,
                    "amount": 200000000,
                    "is_suspended": True,
                },
                {
                    "code": "002594",
                    "name": "比亚迪",
                    "score": 75,
                    "price": 30.0,
                    "limit_up_price": 30.0,
                    "amount": 200000000,
                },
                {
                    "code": "601318",
                    "name": "中国平安",
                    "score": 70,
                    "price": 10.0,
                    "amount": "5000万",
                },
            ],
            "warnings": [],
            "source_errors": [],
        }

        with patch(
            "api.v1.endpoints.vnpy_paper_trading.VnpyPaperTradingService",
            side_effect=self._service,
        ), patch(
            "src.services.vnpy_paper_trading_service.AlphaSiftService",
            return_value=fake_alphasift,
        ):
            settings_resp = self.client.put(
                "/api/v1/vnpy-paper/settings?include_snapshot=false&include_recent_trades=false",
                json={
                    "auto_trade_enabled": True,
                    "auto_trade_time_gate_enabled": False,
                    "auto_strategy": "dual_low",
                    "auto_market": "cn",
                    "auto_max_results": 5,
                    "auto_cash_per_order": 1200,
                    "auto_min_score": 50,
                    "auto_min_turnover": 100000000,
                    "auto_symbol_blacklist": ["600519"],
                },
            )
            run_resp = self.client.post("/api/v1/vnpy-paper/auto/run")
            run_uid = run_resp.json()["agent_run_uid"]
            detail_resp = self.client.get(f"/api/v1/vnpy-paper/agent-runs/{run_uid}")
            trades_resp = self.client.get("/api/v1/portfolio/trades?page=1&page_size=20")

        expected_reasons = [
            "symbol_blacklisted",
            "st_or_delisting_risk",
            "suspended_stock",
            "price_limit_reached",
            "liquidity_below_threshold",
        ]
        self.assertEqual(settings_resp.status_code, 200)
        self.assertEqual(run_resp.status_code, 200)
        run_payload = run_resp.json()
        self.assertTrue(run_payload["accepted"])
        self.assertEqual(run_payload["candidate_count"], 5)
        self.assertEqual(run_payload["planned_count"], 0)
        self.assertEqual(run_payload["submitted_count"], 0)
        self.assertEqual(run_payload["skipped_count"], 5)
        self.assertEqual([item["reason"] for item in run_payload["orders"]], expected_reasons)

        self.assertEqual(detail_resp.status_code, 200)
        detail = detail_resp.json()
        self.assertEqual(detail["status"], "completed")
        self.assertEqual(detail["candidate_count"], 5)
        self.assertEqual(detail["submitted_count"], 0)
        self.assertEqual(detail["skipped_count"], 5)
        self.assertEqual([item["reason"] for item in detail["decisions"]], expected_reasons)
        self.assertEqual(
            [item["skip_reason"] for item in detail["trade_plans"]],
            expected_reasons,
        )
        self.assertEqual(trades_resp.status_code, 200)
        self.assertEqual(trades_resp.json()["total"], 0)

    def test_trade_plan_recovery_summary_endpoint_returns_matrix(self) -> None:
        repo = StockSelectionAgentRepository()
        run = repo.create_run(
            run_uid="recovery-api-run",
            trigger_source="unit-test",
            strategy="dual_low",
            market="cn",
            max_results=1,
            cash_per_order=1000,
            min_score=None,
            skip_existing_positions=True,
            settings={},
            diagnostics={},
        )
        stale_plan = repo.record_trade_plan(
            plan_uid="recovery-api-stale",
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
            skip_reason="vnpy_order_cancel_requested",
            order_result={"status": "cancel_requested", "raw": {"vt_orderid": "GATEWAY.2"}},
        )
        repo.record_trade_plan(
            plan_uid="recovery-api-retryable",
            run_id=int(run["id"]),
            decision_id=None,
            symbol="000001",
            market="cn",
            side="buy",
            status="failed",
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

        response = self.client.get("/api/v1/vnpy-paper/trade-plans/recovery-summary?limit=10")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["scanned_count"], 2)
        self.assertEqual(payload["stale_active_count"], 1)
        self.assertEqual(payload["retry_due_count"], 1)
        self.assertEqual(payload["status_counts"], {"cancel_requested": 1, "failed": 1})
        self.assertEqual(payload["items"][0]["plan_uid"], "recovery-api-stale")
        self.assertTrue(payload["items"][0]["stale_active"])

    def test_trade_plan_recovery_run_endpoint_triggers_bounded_scan(self) -> None:
        service = MagicMock()
        service.retry_due_trade_plans.return_value = {
            "accepted": True,
            "skipped": False,
            "expired_count": 1,
            "reconciled_count": 2,
            "protected_count": 3,
            "reconciliation_failed_count": 1,
            "scanned_count": 3,
            "attempted_count": 2,
            "submitted_count": 1,
            "skipped_count": 1,
            "failed_count": 0,
            "orders": [],
            "messages": ["vnpy_order_timeout:plan-1"],
        }

        with patch(
            "api.v1.endpoints.vnpy_paper_trading._service",
            return_value=service,
        ):
            response = self.client.post(
                "/api/v1/vnpy-paper/trade-plans/recovery/run?max_plans=2&scan_limit=50"
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["expired_count"], 1)
        self.assertEqual(payload["reconciled_count"], 2)
        self.assertEqual(payload["protected_count"], 3)
        self.assertEqual(payload["reconciliation_failed_count"], 1)
        self.assertEqual(payload["attempted_count"], 2)
        self.assertEqual(payload["messages"], ["vnpy_order_timeout:plan-1"])
        service.retry_due_trade_plans.assert_called_once_with(max_plans=2, scan_limit=50)

    def test_agent_run_audit_endpoints_return_list_and_detail(self) -> None:
        repo = StockSelectionAgentRepository()
        run = repo.create_run(
            run_uid="ss-agent-api-test",
            trigger_source="unit-test",
            strategy="dual_low",
            market="cn",
            max_results=3,
            cash_per_order=10000,
            min_score=50,
            skip_existing_positions=True,
            settings={"auto_trade_enabled": True},
            diagnostics={
                "data_quality": {"status": "ok"},
                "warnings": ["daily_source_fallback"],
                "source_errors": ["snapshot_timeout"],
                "source_health": {
                    "snapshot": {
                        "sina": {"failures": 2, "disabled": False},
                        "eastmoney": {"failures": 0, "disabled": False},
                    },
                    "candidate_context": {
                        "news": {
                            "status": "unavailable",
                            "failures": 1,
                            "successes": 0,
                            "last_rows": 0,
                            "errors": ["search_down"],
                        },
                        "fund_flow": {
                            "status": "ok",
                            "failures": 0,
                            "successes": 1,
                            "last_rows": 1,
                        },
                        "fund_flow/tushare_ths": {
                            "status": "unavailable",
                            "failures": 1,
                            "successes": 0,
                            "last_rows": 0,
                        },
                        "fund_flow/akshare": {
                            "status": "ok",
                            "failures": 0,
                            "successes": 1,
                            "last_rows": 1,
                        },
                        "news/bocha": {
                            "status": "unavailable",
                            "failures": 1,
                            "successes": 0,
                            "last_rows": 0,
                        },
                    },
                },
            },
        )
        decision = repo.record_decision(
            run_id=int(run["id"]),
            sequence=1,
            symbol="600519",
            name="贵州茅台",
            market="cn",
            action="buy",
            status="filled",
            score=80,
            cash_amount=1000,
            quantity=100,
            price=10,
            trade_id=1,
            rationale="unit rationale",
            order_result={
                "agent_review": {"status": "passed"},
                "llm_review": {"status": "passed"},
            },
        )
        repo.record_trade_plan(
            plan_uid="plan-api-test",
            run_id=int(run["id"]),
            decision_id=int(decision["id"]),
            symbol="600519",
            name="贵州茅台",
            market="cn",
            side="buy",
            status="filled",
            execution_mode="paper",
            planned_cash_amount=1000,
            submitted_quantity=100,
            submitted_price=10,
            trade_id=1,
            order_result={
                "agent_review": {"status": "passed"},
                "llm_review": {"status": "passed"},
            },
        )
        repo.complete_run(
            run_id=int(run["id"]),
            status="completed",
            candidate_count=1,
            planned_count=0,
            submitted_count=1,
            skipped_count=0,
        )

        list_resp = self.client.get("/api/v1/vnpy-paper/agent-runs?limit=5")
        export_resp = self.client.get("/api/v1/vnpy-paper/agent-runs/export?limit=5&include_details=true")
        detail_resp = self.client.get("/api/v1/vnpy-paper/agent-runs/ss-agent-api-test")

        self.assertEqual(list_resp.status_code, 200)
        self.assertEqual(export_resp.status_code, 200)
        self.assertEqual(detail_resp.status_code, 200)
        self.assertEqual(list_resp.json()["items"][0]["run_uid"], "ss-agent-api-test")
        self.assertGreaterEqual(list_resp.json()["total"], 1)
        export_payload = export_resp.json()
        self.assertEqual(export_payload["count"], 1)
        self.assertTrue(export_payload["include_details"])
        self.assertEqual(export_payload["items"][0]["run_uid"], "ss-agent-api-test")
        self.assertEqual(export_payload["items"][0]["decisions"][0]["symbol"], "600519")
        detail = detail_resp.json()
        self.assertEqual(detail["run_uid"], "ss-agent-api-test")
        self.assertEqual(detail["decisions"][0]["symbol"], "600519")
        self.assertEqual(detail["decisions"][0]["trade_id"], 1)
        self.assertEqual(detail["trade_plans"][0]["status"], "filled")
        self.assertEqual(detail["trade_plans"][0]["execution_mode"], "paper")
        self.assertEqual(detail["decisions"][0]["order_result"]["agent_review"]["status"], "passed")
        self.assertEqual(detail["decisions"][0]["order_result"]["llm_review"]["status"], "passed")
        self.assertEqual(detail["diagnostics"]["agent_workflow"]["status"], "executed")
        self.assertEqual(detail["diagnostics"]["agent_workflow"]["current_stage"], "execution")
        self.assertEqual(detail["diagnostics"]["agent_summary"]["review_quality"]["status"], "audited")
        self.assertEqual(detail["diagnostics"]["agent_summary"]["review_quality"]["score"], 100.0)
        self.assertEqual(detail["diagnostics"]["agent_summary"]["review_quality"]["risk_flags"], [])
        self.assertGreaterEqual(len(detail["timeline"]), 3)
        self.assertEqual(detail["timeline"][0]["stage"], "started")
        self.assertEqual(detail["timeline"][-1]["stage"], "completed")

        feedback_resp = self.client.put(
            "/api/v1/vnpy-paper/agent-runs/ss-agent-api-test/feedback",
            json={
                "verdict": "needs_changes",
                "note": "Reduce concentration before approval.",
                "reviewer": "risk-owner",
            },
        )
        self.assertEqual(feedback_resp.status_code, 200)
        feedback_payload = feedback_resp.json()
        self.assertTrue(feedback_payload["accepted"])
        self.assertEqual(feedback_payload["human_feedback"]["verdict"], "needs_changes")
        self.assertEqual(feedback_payload["human_feedback"]["reviewer"], "risk-owner")
        self.assertEqual(
            feedback_payload["run_detail"]["human_feedback"]["note"],
            "Reduce concentration before approval.",
        )
        feedback_events = [
            item
            for item in feedback_payload["run_detail"]["timeline"]
            if item["stage"] == "human_feedback"
        ]
        self.assertEqual(len(feedback_events), 1)
        self.assertEqual(feedback_events[0]["status"], "needs_changes")
        self.assertEqual(feedback_events[0]["details"]["reviewer"], "risk-owner")

        updated_feedback_resp = self.client.put(
            "/api/v1/vnpy-paper/agent-runs/ss-agent-api-test/feedback",
            json={
                "verdict": "approved",
                "note": "Risk sizing revised and accepted.",
                "reviewer": "risk-owner",
            },
        )
        self.assertEqual(updated_feedback_resp.status_code, 200)
        self.assertEqual(
            updated_feedback_resp.json()["human_feedback"]["id"],
            feedback_payload["human_feedback"]["id"],
        )
        self.assertEqual(
            updated_feedback_resp.json()["human_feedback"]["verdict"],
            "approved",
        )

        fake_analyzer = MagicMock()
        fake_analyzer.is_available.return_value = True
        fake_analyzer._call_litellm.return_value = (
            "### 本轮结论\n复盘完成",
            "openai/test",
            {"total_tokens": 12},
        )
        with patch("src.analyzer.GeminiAnalyzer", return_value=fake_analyzer):
            recap_resp = self.client.post(
                "/api/v1/vnpy-paper/agent-runs/ss-agent-api-test/llm-recap",
                json={"max_output_tokens": 600},
            )

        self.assertEqual(recap_resp.status_code, 200)
        recap_payload = recap_resp.json()
        self.assertTrue(recap_payload["accepted"])
        self.assertEqual(recap_payload["status"], "completed")
        self.assertEqual(recap_payload["llm_recap"]["status"], "completed")
        self.assertEqual(recap_payload["llm_recap"]["model"], "openai/test")
        self.assertIn("复盘完成", recap_payload["llm_recap"]["content"])
        self.assertEqual(
            recap_payload["run_detail"]["diagnostics"]["llm_recap"]["model"],
            "openai/test",
        )

        other_run = repo.create_run(
            run_uid="ss-agent-api-other",
            trigger_source="unit-test",
            strategy="quality_value",
            market="hk",
            max_results=1,
            cash_per_order=5000,
            settings={"auto_trade_enabled": True},
            diagnostics={"data_quality": {"status": "unavailable"}},
        )
        repo.complete_run(
            run_id=int(other_run["id"]),
            status="completed",
            candidate_count=0,
            planned_count=0,
            submitted_count=0,
            skipped_count=0,
        )
        filtered_list_resp = self.client.get(
            "/api/v1/vnpy-paper/agent-runs"
            "?limit=5&trigger_source=unit-test&strategy=dual_low&market=cn&status=completed"
            "&created_from=2000-01-01T00:00:00&created_to=2999-01-01T00:00:00"
        )
        filtered_export_resp = self.client.get(
            "/api/v1/vnpy-paper/agent-runs/export"
            "?limit=5&include_details=false&trigger_source=unit-test&strategy=dual_low&market=cn&status=completed"
            "&created_from=2000-01-01T00:00:00&created_to=2999-01-01T00:00:00"
        )
        daily_summary_resp = self.client.get(
            "/api/v1/vnpy-paper/agent-runs/daily-summary"
            f"?date={date.today().isoformat()}&trigger_source=unit-test"
            "&strategy=dual_low&market=cn&status=completed"
        )
        quality_trends_resp = self.client.get(
            "/api/v1/vnpy-paper/agent-runs/data-quality-trends"
            "?days=30&trigger_source=unit-test&strategy=dual_low&market=cn&status=completed"
        )
        all_quality_trends_resp = self.client.get(
            "/api/v1/vnpy-paper/agent-runs/data-quality-trends"
            "?days=30&trigger_source=unit-test&status=completed"
        )
        calibration_trends_resp = self.client.get(
            "/api/v1/vnpy-paper/agent-runs/return-risk-calibration-trends"
            "?days=30&trigger_source=unit-test&strategy=dual_low&market=cn&status=completed"
        )
        future_list_resp = self.client.get(
            "/api/v1/vnpy-paper/agent-runs"
            "?limit=5&trigger_source=unit-test&created_from=2999-01-01T00:00:00"
        )

        self.assertEqual(filtered_list_resp.status_code, 200)
        self.assertEqual(filtered_export_resp.status_code, 200)
        self.assertEqual(daily_summary_resp.status_code, 200)
        self.assertEqual(quality_trends_resp.status_code, 200)
        self.assertEqual(all_quality_trends_resp.status_code, 200)
        self.assertEqual(calibration_trends_resp.status_code, 200)
        self.assertEqual(future_list_resp.status_code, 200)
        self.assertEqual(
            [item["run_uid"] for item in filtered_list_resp.json()["items"]],
            ["ss-agent-api-test"],
        )
        self.assertEqual(filtered_list_resp.json()["total"], 1)
        self.assertEqual(
            filtered_list_resp.json()["items"][0]["human_feedback"]["verdict"],
            "approved",
        )
        self.assertEqual(filtered_export_resp.json()["count"], 1)
        self.assertEqual(filtered_export_resp.json()["items"][0]["run_uid"], "ss-agent-api-test")
        daily_summary = daily_summary_resp.json()
        self.assertEqual(daily_summary["run_count"], 1)
        self.assertEqual(daily_summary["candidate_count"], 1)
        self.assertEqual(daily_summary["submitted_count"], 1)
        self.assertEqual(daily_summary["agent_review_counts"], {"passed": 1})
        self.assertEqual(daily_summary["llm_review_counts"], {"passed": 1})
        self.assertEqual(daily_summary["review_quality_counts"], {"audited": 1})
        self.assertEqual(daily_summary["review_quality_flag_counts"], {})
        self.assertEqual(daily_summary["review_quality_score_avg"], 100.0)
        self.assertEqual(daily_summary["human_feedback_counts"], {"approved": 1})
        self.assertEqual(daily_summary["human_feedback_reviewed_count"], 1)
        self.assertEqual(daily_summary["workflow_status_counts"], {"executed": 1})
        self.assertEqual(daily_summary["workflow_stage_counts"], {"execution": 1})
        self.assertEqual(daily_summary["trade_plan_status_counts"], {"filled": 1})
        self.assertEqual(daily_summary["top_symbols"][0]["symbol"], "600519")
        quality_trends = quality_trends_resp.json()
        self.assertEqual(quality_trends["window_days"], 30)
        self.assertEqual(quality_trends["scanned_count"], 1)
        self.assertEqual(quality_trends["known_count"], 1)
        calibration_trends = calibration_trends_resp.json()
        self.assertEqual(calibration_trends["window_days"], 30)
        self.assertEqual(calibration_trends["scanned_count"], 1)
        self.assertEqual(calibration_trends["observed_count"], 0)
        self.assertEqual(calibration_trends["unknown_count"], 1)
        self.assertEqual(calibration_trends["health"], "collecting")
        current_forward_quality = calibration_trends["current_forward_quality"]
        self.assertTrue(current_forward_quality["available"])
        self.assertEqual(current_forward_quality["trigger_source"], "unit-test")
        self.assertEqual(current_forward_quality["run_status"], "completed")
        self.assertFalse(current_forward_quality["refresh_missing"])
        self.assertTrue(calibration_trends["methodology"]["overlapping_rolling_samples"])
        self.assertFalse(calibration_trends["methodology"]["independent_sample_count_claimed"])
        self.assertEqual(quality_trends["quality_counts"], {"ok": 1})
        self.assertEqual(quality_trends["degraded_count"], 0)
        self.assertEqual(quality_trends["degraded_rate_pct"], 0.0)
        self.assertEqual(quality_trends["warning_counts"], {"daily_source_fallback": 1})
        self.assertEqual(quality_trends["source_error_counts"], {"snapshot_timeout": 1})
        source_health = {
            item["key"]: item for item in quality_trends["source_health_items"]
        }
        self.assertEqual(source_health["snapshot/sina"]["degraded_rate_pct"], 100.0)
        self.assertEqual(source_health["snapshot/sina"]["max_failures"], 2)
        self.assertEqual(source_health["snapshot/eastmoney"]["latest_status"], "ok")
        self.assertEqual(
            source_health["candidate_context/news"]["degraded_rate_pct"],
            100.0,
        )
        self.assertEqual(
            source_health["candidate_context/news"]["latest_status"],
            "unavailable",
        )
        self.assertEqual(
            source_health["candidate_context/fund_flow"]["latest_status"],
            "ok",
        )
        self.assertEqual(
            source_health["candidate_context/fund_flow/tushare_ths"]["degraded_rate_pct"],
            100.0,
        )
        self.assertEqual(
            source_health["candidate_context/fund_flow/akshare"]["latest_status"],
            "ok",
        )
        self.assertEqual(
            source_health["candidate_context/news/bocha"]["latest_status"],
            "unavailable",
        )
        self.assertEqual(quality_trends["daily"][0]["quality_counts"], {"ok": 1})
        all_quality_trends = all_quality_trends_resp.json()
        self.assertEqual(all_quality_trends["quality_counts"], {"ok": 1, "unavailable": 1})
        self.assertEqual(all_quality_trends["degraded_count"], 1)
        self.assertEqual(all_quality_trends["degraded_rate_pct"], 50.0)
        self.assertEqual(all_quality_trends["health"], "error")
        self.assertEqual(future_list_resp.json()["items"], [])
        self.assertEqual(future_list_resp.json()["total"], 0)

        missing_feedback_resp = self.client.put(
            "/api/v1/vnpy-paper/agent-runs/missing-run/feedback",
            json={"verdict": "rejected"},
        )
        self.assertEqual(missing_feedback_resp.status_code, 404)

    def test_approve_manual_trade_plan_endpoint_submits_paper_trade(self) -> None:
        repo = StockSelectionAgentRepository()
        run = repo.create_run(
            run_uid="ss-agent-api-approval",
            trigger_source="unit-test",
            strategy="dual_low",
            market="cn",
            max_results=1,
            cash_per_order=1200,
            settings={"auto_execution_mode": "manual_approval"},
        )
        decision = repo.record_decision(
            run_id=int(run["id"]),
            sequence=1,
            symbol="600519",
            name="贵州茅台",
            market="cn",
            action="buy",
            status="planned",
            reason="pending_approval",
            score=80,
            cash_amount=1200,
            quantity=100,
            price=10,
        )
        repo.record_trade_plan(
            plan_uid="plan-api-approval",
            run_id=int(run["id"]),
            decision_id=int(decision["id"]),
            symbol="600519",
            name="贵州茅台",
            market="cn",
            side="buy",
            status="planned",
            execution_mode="manual_approval",
            planned_cash_amount=1200,
            planned_quantity=100,
            planned_price=10,
            skip_reason="pending_approval",
        )
        repo.complete_run(
            run_id=int(run["id"]),
            status="completed",
            candidate_count=1,
            planned_count=1,
            submitted_count=0,
            skipped_count=0,
        )

        with patch(
            "api.v1.endpoints.vnpy_paper_trading.VnpyPaperTradingService",
            side_effect=self._service,
        ):
            response = self.client.post("/api/v1/vnpy-paper/trade-plans/plan-api-approval/approve")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["accepted"])
        self.assertEqual(payload["status"], "filled")
        self.assertEqual(payload["symbol"], "600519")
        self.assertIsNotNone(payload["trade_id"])
        detail = repo.get_run_detail("ss-agent-api-approval")
        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertEqual(detail["planned_count"], 0)
        self.assertEqual(detail["submitted_count"], 1)
        self.assertEqual(detail["trade_plans"][0]["status"], "filled")
        self.assertEqual(detail["decisions"][0]["trade_id"], payload["trade_id"])

    def test_retry_manual_trade_plan_endpoint_delegates_to_service(self) -> None:
        service = MagicMock()
        service.retry_trade_plan.return_value = {
            "accepted": True,
            "status": "filled",
            "trade_id": 9,
            "account_id": 1,
            "symbol": "600519",
            "side": "buy",
            "quantity": 100,
            "price": 10,
            "cash_amount": 1000,
            "source": "vnpy_local_paper_ledger",
            "message": "Paper order filled.",
            "reason": None,
        }

        with patch(
            "api.v1.endpoints.vnpy_paper_trading.VnpyPaperTradingService",
            return_value=service,
        ):
            response = self.client.post("/api/v1/vnpy-paper/trade-plans/plan-api-retry/retry")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["accepted"])
        self.assertEqual(response.json()["trade_id"], 9)
        service.retry_trade_plan.assert_called_once_with("plan-api-retry")

    def test_cancel_submitted_trade_plan_endpoint_delegates_to_service(self) -> None:
        service = MagicMock()
        service.cancel_trade_plan.return_value = {
            "accepted": True,
            "status": "cancel_requested",
            "trade_id": None,
            "account_id": 1,
            "symbol": "600519",
            "side": "buy",
            "quantity": 100,
            "price": 10,
            "cash_amount": 1000,
            "source": "vnpy_main_engine",
            "message": "vn.py cancel request submitted.",
            "reason": "vnpy_order_cancel_requested",
        }

        with patch(
            "api.v1.endpoints.vnpy_paper_trading.VnpyPaperTradingService",
            return_value=service,
        ):
            response = self.client.post("/api/v1/vnpy-paper/trade-plans/plan-api-cancel/cancel")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["accepted"])
        self.assertEqual(response.json()["status"], "cancel_requested")
        service.cancel_trade_plan.assert_called_once_with("plan-api-cancel")

    def test_calibration_evidence_endpoint_is_read_only_and_uses_shadow_runs(self) -> None:
        payload = {
            "schema_version": 1,
            "generated_at": "2026-07-21T04:30:00+00:00",
            "window_days": 90,
            "filters": {
                "trigger_source": "agent_calibration_shadow",
                "status": "completed",
            },
            "evaluation": {
                "ok": False,
                "failures": ["cn:runs_below_threshold"],
                "required_markets": ["cn", "hk", "us"],
                "required_versions": [],
                "thresholds": {"min_runs_per_market": 20},
                "markets": {"cn": {"ok": False, "total_runs": 9}},
            },
            "methodology": {
                "read_only": True,
                "creates_agent_runs": False,
                "places_orders": False,
            },
        }

        alert_delivery = {
            "status": "failed",
            "trigger": {
                "id": 28,
                "status": "degraded",
                "reason": "calibration_evidence_pending",
                "triggered_at": "2026-07-21T04:52:11",
            },
            "attempt_count": 1,
            "successful_count": 0,
            "failed_count": 1,
            "retryable_failure_count": 1,
            "attempts": [{
                "attempt": 1,
                "channel": "feishu",
                "success": False,
                "error_code": "send_failed",
                "retryable": True,
            }],
            "retry_policy": {
                "status": "due",
                "attempt": 1,
                "max_attempts": 3,
                "interval_seconds": 300,
                "next_retry_at": None,
            },
        }
        scheduler = MagicMock()
        scheduler.status.return_value = {
            "background_tasks": [{
                "name": "agent_calibration_shadow",
                "next_run_at": "2026-07-23T11:06:13",
            }],
        }
        self.client.app.dependency_overrides[get_runtime_scheduler_service] = (
            lambda: scheduler
        )

        try:
            with patch(
                "api.v1.endpoints.vnpy_paper_trading.collect_persisted_calibration_evidence",
                return_value=payload,
            ) as collect, patch(
                "api.v1.endpoints.vnpy_paper_trading.build_calibration_shadow_schedule",
                return_value={
                    "schema_version": 1,
                    "enabled": True,
                    "interval_minutes": 1440,
                    "configured_count": 3,
                    "eligible_count": 0,
                    "next_eligible_at": "2026-07-23T10:57:05",
                    "all_eligible_at": "2026-07-23T11:05:45",
                    "items": [{
                        "market": "cn",
                        "strategy": "dual_low",
                        "eligible": False,
                        "next_eligible_at": "2026-07-23T10:57:05",
                    }],
                    "read_only": True,
                    "creates_agent_runs": False,
                    "places_orders": False,
                },
            ) as schedule, patch(
                "api.v1.endpoints.vnpy_paper_trading.AlertService",
            ) as alert_service:
                alert_service.return_value.get_latest_system_event_delivery.return_value = alert_delivery
                response = self.client.get(
                    "/api/v1/vnpy-paper/agent-runs/calibration-evidence"
                )
        finally:
            self.client.app.dependency_overrides.pop(
                get_runtime_scheduler_service,
                None,
            )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["evaluation"]["ok"])
        self.assertTrue(response.json()["methodology"]["read_only"])
        self.assertEqual(response.json()["alert_delivery"], alert_delivery)
        self.assertEqual(
            response.json()["sampling_schedule"]["next_eligible_at"],
            "2026-07-23T10:57:05",
        )
        self.assertEqual(
            response.json()["sampling_schedule"]["all_eligible_at"],
            "2026-07-23T11:05:45",
        )
        self.assertEqual(
            response.json()["sampling_schedule"]["next_scheduled_at"],
            "2026-07-23T11:06:13",
        )
        schedule.assert_called_once()
        self.assertEqual(
            collect.call_args.kwargs["markets"],
            ["cn", "hk", "us"],
        )
        self.assertIsNotNone(collect.call_args.kwargs["quality_service"])

    def test_calibration_evidence_endpoint_isolates_alert_history_failure(self) -> None:
        payload = {
            "schema_version": 1,
            "window_days": 90,
            "evaluation": {"ok": False},
            "methodology": {"read_only": True},
        }
        with patch(
            "api.v1.endpoints.vnpy_paper_trading.collect_persisted_calibration_evidence",
            return_value=payload,
        ), patch(
            "api.v1.endpoints.vnpy_paper_trading.AlertService",
            side_effect=RuntimeError("alert database unavailable"),
        ):
            response = self.client.get(
                "/api/v1/vnpy-paper/agent-runs/calibration-evidence"
            )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["evaluation"]["ok"])
        self.assertEqual(response.json()["alert_delivery"]["status"], "unavailable")

    def test_agent_backtest_endpoint_forwards_filters_and_returns_matrix(self) -> None:
        service = MagicMock()
        service.evaluate.return_value = {
            "generated_at": datetime(2026, 7, 14, 10, 0),
            "methodology": {
                "type": "point_in_time_candidate_forward_evaluation",
                "lookahead_protection": True,
            },
            "filters": {"strategy": "dual_low", "eval_windows": [1, 5]},
            "total": 2,
            "scanned_count": 2,
            "truncated": False,
            "refresh_attempted_count": 0,
            "refresh_succeeded_count": 0,
            "refresh_failed_count": 0,
            "refresh_skipped_not_due_count": 2,
            "refresh_source_counts": {"TencentFetcher": 1},
            "refresh_saved_row_count": 6,
            "refresh_resolved_anchor_count": 1,
            "refresh_unresolved_anchor_count": 0,
            "status_counts": {"filled": 1, "skipped": 1},
            "matrix": {
                "1": {
                    "eval_window_days": 1,
                    "sample_count": 2,
                    "completed_count": 1,
                    "coverage_pct": 50.0,
                    "win_rate_pct": 100.0,
                }
            },
            "strategy_matrix": {},
            "review_quality_matrix": [
                {
                    "key": "llm:openai/model-a:prompt-v2/eval-v1",
                    "source": "llm",
                    "reviewer": "llm_reviewer_v1",
                    "model": "openai/model-a",
                    "version": "prompt-v2/eval-v1",
                    "sample_count": 2,
                    "status_counts": {"passed": 1, "blocked": 1},
                    "horizons": {
                        "1": {
                            "passed_precision_pct": 100.0,
                            "blocked_avoidance_rate_pct": 100.0,
                        }
                    },
                }
            ],
            "review_policy_quality": {
                "policy": "llm_review_then_rule_agent",
                "source_counts": {"llm": 2},
                "horizons": {"1": {"passed_precision_pct": 100.0}},
            },
            "items": [],
        }

        with patch(
            "api.v1.endpoints.vnpy_paper_trading.StockSelectionAgentBacktestService",
            return_value=service,
        ):
            response = self.client.post(
                "/api/v1/vnpy-paper/agent-runs/backtest",
                json={
                    "strategy": "dual_low",
                    "market": "cn",
                    "created_from": "2026-07-01T00:00:00",
                    "created_to": "2026-07-14T23:59:59",
                    "eval_windows": [1, 5],
                    "include_skipped": True,
                    "max_decisions": 200,
                    "refresh_missing": False,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["methodology"]["lookahead_protection"])
        self.assertEqual(response.json()["refresh_skipped_not_due_count"], 2)
        self.assertEqual(
            response.json()["refresh_source_counts"],
            {"TencentFetcher": 1},
        )
        self.assertEqual(response.json()["refresh_resolved_anchor_count"], 1)
        self.assertEqual(response.json()["matrix"]["1"]["coverage_pct"], 50.0)
        self.assertEqual(response.json()["review_quality_matrix"][0]["model"], "openai/model-a")
        self.assertEqual(
            response.json()["review_policy_quality"]["policy"],
            "llm_review_then_rule_agent",
        )
        service.evaluate.assert_called_once()
        call = service.evaluate.call_args.kwargs
        self.assertEqual(call["strategy"], "dual_low")
        self.assertEqual(call["eval_windows"], [1, 5])
        self.assertEqual(call["max_decisions"], 200)

    def test_agent_backtest_endpoint_rejects_invalid_window(self) -> None:
        response = self.client.post(
            "/api/v1/vnpy-paper/agent-runs/backtest",
            json={"eval_windows": [0]},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "validation_error")

    def test_agent_cross_run_quality_endpoint_returns_current_gate_state(self) -> None:
        service = MagicMock()
        service.get_cross_run_quality_status.return_value = {
            "schema_version": 3,
            "generated_at": datetime(2026, 7, 14, 10, 0),
            "state": "insufficient_evidence",
            "reason": "mature_sample_count_below_threshold",
            "previous_state": None,
            "transition": None,
            "changed": False,
            "strategy": "dual_low",
            "market": "cn",
            "selection_quality_state": "insufficient_evidence",
            "selection_quality_reason": "mature_sample_count_below_threshold",
            "return_risk_objective_state": "insufficient_evidence",
            "return_risk_objective_reason": (
                "return_risk_mature_sample_count_below_threshold"
            ),
            "return_risk_objective_applied": False,
            "return_risk_objective": {
                "version": "candidate-return-risk-v1",
                "state": "insufficient_evidence",
            },
            "review_quality_state": "insufficient_evidence",
            "review_quality_reason": "review_mature_sample_count_below_threshold",
            "review_quality_applied": False,
            "review_policy_quality": {
                "policy": "llm_review_then_rule_agent",
                "horizons": {"5": {"passed_precision_pct": None}},
            },
            "horizon_days": 5,
            "min_mature_samples": 10,
            "min_win_rate_pct": 45.0,
            "max_decisions": 200,
            "refresh_missing": False,
            "refresh_attempted_count": 0,
            "refresh_succeeded_count": 0,
            "refresh_failed_count": 0,
            "refresh_skipped_not_due_count": 3,
            "refresh_source_counts": {},
            "refresh_saved_row_count": 0,
            "refresh_resolved_anchor_count": 0,
            "refresh_unresolved_anchor_count": 0,
            "sample_count": 3,
            "mature_sample_count": 0,
            "coverage_pct": 0.0,
            "win_rate_pct": None,
            "average_return_pct": None,
            "median_return_pct": None,
            "average_max_adverse_excursion_pct": None,
            "average_daily_return_pct": None,
            "daily_return_coverage_pct": None,
            "daily_return_volatility_pct": None,
            "downside_deviation_pct": None,
            "daily_expected_shortfall_20_pct": None,
            "return_risk_utility_pct": None,
            "unable_reason_counts": {"insufficient_forward_bars": 3},
            "lookahead_protection": True,
            "source": "persisted_agent_decisions_and_stock_daily",
            "truncated": False,
            "gate_enabled": False,
            "gate_blocked": False,
            "insufficient_evidence_blocks": False,
        }
        with patch(
            "api.v1.endpoints.vnpy_paper_trading.VnpyPaperTradingService",
            return_value=service,
        ):
            response = self.client.get("/api/v1/vnpy-paper/agent-runs/cross-run-quality")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["state"], "insufficient_evidence")
        self.assertFalse(response.json()["gate_blocked"])
        self.assertEqual(response.json()["refresh_skipped_not_due_count"], 3)
        self.assertEqual(response.json()["review_quality_state"], "insufficient_evidence")
        self.assertEqual(
            response.json()["return_risk_objective"]["version"],
            "candidate-return-risk-v1",
        )
        service.get_cross_run_quality_status.assert_called_once_with()


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


if __name__ == "__main__":
    unittest.main()
