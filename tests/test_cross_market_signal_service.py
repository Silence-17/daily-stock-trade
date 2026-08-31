# -*- coding: utf-8 -*-
"""Runtime evidence tests for the global sector-rotation paper strategy."""

from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pandas as pd

from data_provider.realtime_types import RealtimeSource, UnifiedRealtimeQuote
from src.services.cross_market_signal_service import (
    ASIA_THEME_EVIDENCE_CODES,
    CrossMarketSignalService,
    KOREA_LIVE_PROVIDER_CLOCK_SKEW_MARGIN_SECONDS,
    NASDAQ_FUTURES_CAPTURE_THROTTLE_SECONDS,
    NASDAQ_FUTURES_STATE_KEY,
    US_CLOSE_THEME_CAPTURE_ATTEMPTS_STATE_KEY,
    US_PREMARKET_CAPTURE_ATTEMPTS_STATE_KEY,
    US_PREMARKET_A_SHARE_THEME_MAP,
    US_PREMARKET_THEME_CODES,
    US_THEME_COLLECTION_MAX_WORKERS,
)


NOW = datetime(2026, 7, 23, 1, 40, tzinfo=timezone.utc)


class _QuoteManager:
    def __init__(self) -> None:
        self.available_fetchers = ["KoreaInvestmentFetcher", "YfinanceFetcher"]
        self.provider_at = NOW
        self.change_pct = 1.0
        self.missing_code = None
        self.calls = []
        self.open_price = 100.0
        self.pre_close = 100.0
        self.price = 100.0
        self.amount = 10000.0
        self.volume = 100.0

    def get_realtime_quote(self, code):
        self.calls.append(code)
        if code == self.missing_code:
            return None
        return UnifiedRealtimeQuote(
            code=code,
            source=RealtimeSource.FALLBACK,
            provider_timestamp=self.provider_at.isoformat(),
            fetched_at=self.provider_at.isoformat(),
            market="kr",
            currency="KRW",
            data_quality="partial",
            price=self.price,
            change_pct=self.change_pct,
            open_price=self.open_price,
            pre_close=self.pre_close,
            amount=self.amount,
            volume=self.volume,
        )

    def get_daily_data(self, code, days=30):
        self.calls.append((code, days))
        dates = pd.date_range(end="2026-07-21", periods=25, freq="D")
        closes = [80.0 + index for index in range(25)]
        return pd.DataFrame({"date": dates, "close": closes}), "unit-test"


class _IntelligenceService:
    def __init__(self, items, *, default_published_at=None):
        self.items = items
        self.calls = []
        self.template_calls = []
        self.failed_templates = set()
        self.empty_templates = set()
        self.default_published_at = default_published_at or next(
            (
                item.get("published_at")
                for item in items
                if isinstance(item, dict) and item.get("published_at") is not None
            ),
            datetime.now(timezone.utc),
        )

    def list_items(self, **filters):
        self.calls.append(filters)
        return {"items": list(self.items), "total": len(self.items)}

    def fetch_template_items(self, template_id, *, limit=50):
        self.template_calls.append((template_id, limit))
        if template_id in self.failed_templates:
            raise RuntimeError(f"{template_id} unavailable")
        if template_id in self.empty_templates:
            items = []
        elif template_id == "newsnow-jin10" and self.items:
            items = list(self.items)
        else:
            items = [{
                "title": "Neutral macro source heartbeat",
                "published_at": self.default_published_at,
            }]
        return {
            "ok": True,
            "template_id": template_id,
            "requested_limit": limit,
            "fetched_count": len(items),
            "exhausted": True,
            "items": items,
        }


class CrossMarketSignalServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_path = Path(self.temp_dir.name) / "cross-market-state.json"
        self.manager = _QuoteManager()
        self.service = CrossMarketSignalService(
            data_fetcher_manager=self.manager,
            state_path=self.state_path,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_runtime_snapshot_retention_is_bounded_per_signal_type(self) -> None:
        service = CrossMarketSignalService(
            data_fetcher_manager=self.manager,
            state_path=self.state_path,
            max_runtime_snapshots=5,
        )
        for sequence in range(8):
            service._append_snapshot("korea_snapshots", {"sequence": sequence})
        for sequence in range(3):
            service._append_snapshot("us_tech_snapshots", {"sequence": sequence})

        state = service._read_state()

        self.assertEqual(
            [item["sequence"] for item in state["korea_snapshots"]],
            [3, 4, 5, 6, 7],
        )
        self.assertEqual(
            [item["sequence"] for item in state["us_tech_snapshots"]],
            [0, 1, 2],
        )
        self.assertFalse(self.state_path.with_suffix(".json.tmp").exists())

    def test_runtime_status_audits_equipment_and_material_theme_mappings(self) -> None:
        unavailable = {
            "available": False,
            "strong": False,
            "reason": "test_unavailable",
        }
        with patch.object(
            self.service,
            "get_us_premarket_signal_for_cn_trade",
            return_value=unavailable,
        ) as premarket, patch.object(
            self.service,
            "get_us_close_theme_signal_for_cn_trade",
            return_value=unavailable,
        ) as close:
            runtime = self.service.get_runtime_status(now=NOW)

        self.assertEqual(runtime["us_premarket"]["theme_count"], 11)
        self.assertEqual(runtime["us_close_themes"]["theme_count"], 11)
        self.assertFalse(runtime["us_premarket"]["full_strategy_coverage"])
        self.assertFalse(runtime["us_close_themes"]["full_strategy_coverage"])
        self.assertEqual(len(runtime["us_premarket"]["missing_themes"]), 11)
        self.assertEqual(len(runtime["us_close_themes"]["missing_themes"]), 11)
        self.assertFalse(runtime["us_premarket"]["latest_capture"]["available"])
        self.assertEqual(
            runtime["us_premarket"]["latest_capture"]["theme_count"],
            11,
        )
        self.assertIn("equipment", runtime["us_premarket"]["themes"])
        self.assertIn("materials", runtime["us_close_themes"]["themes"])
        self.assertIn(
            "equipment",
            [call.kwargs["theme"] for call in premarket.call_args_list],
        )
        self.assertIn(
            "materials",
            [call.kwargs["theme"] for call in close.call_args_list],
        )

    def test_runtime_status_exposes_latest_raw_premarket_capture(self) -> None:
        unavailable = {
            "available": False,
            "strong": False,
            "reason": "test_unavailable",
        }
        theme_signals = {
            signal_theme: {
                "available": signal_theme in {"semiconductor", "storage"},
            }
            for signal_theme in US_PREMARKET_A_SHARE_THEME_MAP.values()
        }
        self.service._append_snapshot("us_premarket_snapshots", {
            "observed_at": (NOW - timedelta(seconds=30)).isoformat(),
            "session_date": "2026-07-22",
            "session_stage": "premarket",
            "universe_size": 91,
            "streamed_component_count": 30,
            "gap_fill_request_count": 61,
            "collected_component_count": 49,
            "reused_fresh_component_count": 5,
            "theme_signals": theme_signals,
            "components": {"NVDA": {"change_pct": 1.0}},
            "component_errors": {"TTDKY": "quote_unavailable"},
        })

        with patch.object(
            self.service,
            "get_us_premarket_signal_for_cn_trade",
            return_value=unavailable,
        ), patch.object(
            self.service,
            "get_us_close_theme_signal_for_cn_trade",
            return_value=unavailable,
        ):
            runtime = self.service.get_runtime_status(now=NOW)

        latest = runtime["us_premarket"]["latest_capture"]
        self.assertTrue(latest["available"])
        self.assertEqual(latest["session_date"], "2026-07-22")
        self.assertEqual(latest["age_seconds"], 30)
        self.assertEqual(latest["universe_size"], 91)
        self.assertEqual(latest["streamed_component_count"], 30)
        self.assertEqual(latest["gap_fill_request_count"], 61)
        self.assertEqual(latest["collected_component_count"], 49)
        self.assertEqual(latest["reused_fresh_component_count"], 5)
        self.assertEqual(
            latest["available_themes"],
            ["semiconductor", "memory"],
        )
        self.assertEqual(latest["available_theme_count"], 2)
        self.assertEqual(latest["theme_count"], 11)
        self.assertEqual(latest["component_error_count"], 1)
        self.assertFalse(latest["full_snapshot_coverage"])
        self.assertIn("equipment", latest["missing_themes"])

    def test_runtime_theme_coverage_allows_asia_supply_chain_supplement(self) -> None:
        available = {"available": True, "strong": False, "reason": "available"}
        unavailable = {
            "available": False,
            "strong": False,
            "reason": "unavailable",
        }

        def coverage_shortfall(session_stage):
            reason = (
                "premarket_theme_coverage_insufficient"
                if session_stage == "premarket"
                else "us_close_theme_coverage_insufficient"
            )
            observed_at = (NOW - timedelta(hours=5)).isoformat()
            stored_signal = {
                "available": False,
                "strong": False,
                "reason": reason,
            }
            return {
                **stored_signal,
                "session_date": "2026-07-22",
                "observed_at": observed_at,
                "snapshot": {
                    "session_date": "2026-07-22",
                    "session_stage": session_stage,
                    "observed_at": observed_at,
                    "theme_signals": {"mlcc": stored_signal},
                },
            }

        def premarket(*, theme, now):
            return coverage_shortfall("premarket") if theme == "mlcc" else available

        def asia_supply(*, theme, now):
            return available if theme == "mlcc" else unavailable

        with patch.object(
            self.service,
            "get_us_premarket_signal_for_cn_trade",
            side_effect=premarket,
        ), patch.object(
            self.service,
            "get_us_close_theme_signal_for_cn_trade",
            return_value=available,
        ), patch.object(
            self.service,
            "get_asia_theme_signal_for_cn_trade",
            side_effect=asia_supply,
        ), patch.object(
            self.service,
            "_snapshot_collection_status",
            return_value={"required_stages_complete": True},
        ):
            runtime = self.service.get_runtime_status(now=NOW)

        self.assertTrue(runtime["us_premarket"]["full_strategy_coverage"])
        self.assertEqual(runtime["us_premarket"]["supplemented_themes"], ["mlcc"])
        self.assertEqual(runtime["us_premarket"]["qualified_theme_count"], 11)
        self.assertEqual(runtime["us_premarket"]["missing_themes"], [])
        self.assertTrue(runtime["us_close_themes"]["full_strategy_coverage"])

        def close_with_missing_mlcc(*, theme, now):
            return coverage_shortfall("close") if theme == "mlcc" else available

        with patch.object(
            self.service,
            "get_us_premarket_signal_for_cn_trade",
            return_value=available,
        ), patch.object(
            self.service,
            "get_us_close_theme_signal_for_cn_trade",
            side_effect=close_with_missing_mlcc,
        ), patch.object(
            self.service,
            "get_asia_theme_signal_for_cn_trade",
            return_value=available,
        ), patch.object(
            self.service,
            "_snapshot_collection_status",
            return_value={"required_stages_complete": True},
        ):
            runtime = self.service.get_runtime_status(now=NOW)

        self.assertTrue(runtime["us_close_themes"]["full_strategy_coverage"])
        self.assertEqual(runtime["us_close_themes"]["missing_themes"], [])
        self.assertEqual(
            runtime["us_close_themes"]["supplemented_themes"],
            ["mlcc"],
        )
        self.assertEqual(runtime["us_close_themes"]["qualified_theme_count"], 11)

    def test_runtime_theme_coverage_rejects_supplement_without_us_snapshot(self) -> None:
        available = {"available": True, "strong": False, "reason": "available"}

        def missing_premarket_session(*, theme, now):
            if theme != "mlcc":
                return available
            return {
                "available": False,
                "strong": False,
                "reason": "prior_us_premarket_signal_unavailable",
            }

        def missing_close_session(*, theme, now):
            if theme != "mlcc":
                return available
            return {
                "available": False,
                "strong": False,
                "reason": "prior_us_close_theme_signal_unavailable",
            }

        with patch.object(
            self.service,
            "get_us_premarket_signal_for_cn_trade",
            side_effect=missing_premarket_session,
        ), patch.object(
            self.service,
            "get_us_close_theme_signal_for_cn_trade",
            side_effect=missing_close_session,
        ), patch.object(
            self.service,
            "get_asia_theme_signal_for_cn_trade",
            return_value=available,
        ), patch.object(
            self.service,
            "_snapshot_collection_status",
            return_value={"required_stages_complete": True},
        ):
            runtime = self.service.get_runtime_status(now=NOW)

        self.assertFalse(runtime["us_premarket"]["full_strategy_coverage"])
        self.assertFalse(runtime["us_close_themes"]["full_strategy_coverage"])
        self.assertEqual(runtime["us_premarket"]["supplemented_themes"], [])
        self.assertEqual(runtime["us_close_themes"]["supplemented_themes"], [])
        self.assertEqual(runtime["us_premarket"]["missing_themes"], ["mlcc"])
        self.assertEqual(runtime["us_close_themes"]["missing_themes"], ["mlcc"])

    def test_runtime_theme_supplement_pairs_prior_us_with_current_asia_session(self) -> None:
        prior_us_session = date(2026, 7, 22)
        current_cn_session = date(2026, 7, 23)
        premarket_observed_at = datetime(
            2026,
            7,
            22,
            13,
            25,
            tzinfo=timezone.utc,
        )
        close_observed_at = datetime(
            2026,
            7,
            22,
            20,
            1,
            tzinfo=timezone.utc,
        )
        close_theme_signals = {
            signal_theme: {
                "available": signal_theme != "mlcc",
                "strong": False,
                "reason": (
                    "us_close_theme_coverage_insufficient"
                    if signal_theme == "mlcc"
                    else "us_close_theme_not_strong"
                ),
                "score": 0.0,
                "sector_change_pct": 0.0,
            }
            for signal_theme in set(US_PREMARKET_A_SHARE_THEME_MAP.values())
        }
        premarket_theme_signals = {
            signal_theme: {
                "available": signal_theme != "mlcc",
                "strong": False,
                "reason": (
                    "premarket_theme_coverage_insufficient"
                    if signal_theme == "mlcc"
                    else "us_premarket_theme_not_strong"
                ),
                "score": 0.0,
                "sector_change_pct": 0.0,
            }
            for signal_theme in set(US_PREMARKET_A_SHARE_THEME_MAP.values())
        }
        self.service._append_snapshot("us_premarket_snapshots", {
            "observed_at": premarket_observed_at.isoformat(),
            "session_date": prior_us_session.isoformat(),
            "session_stage": "premarket",
            "theme_signals": premarket_theme_signals,
            "components": {},
            "component_errors": {},
        })
        self.service._append_snapshot("us_close_theme_snapshots", {
            "observed_at": close_observed_at.isoformat(),
            "session_date": prior_us_session.isoformat(),
            "session_stage": "close",
            "theme_signals": close_theme_signals,
            "components": {},
            "component_errors": {},
        })

        asia_observed_at = NOW - timedelta(seconds=30)
        self.service._append_snapshot("asia_theme_snapshots", {
            "observed_at": asia_observed_at.isoformat(),
            "session_date": current_cn_session.isoformat(),
            "themes": {
                "mlcc": {
                    "available": True,
                    "strong": False,
                    "reason": "asia_theme_not_strong",
                },
            },
            "components": {
                code: {
                    "change_pct": 0.5,
                    "provider_timestamp": asia_observed_at.isoformat(),
                    "source": "unit-test",
                }
                for code in ASIA_THEME_EVIDENCE_CODES["mlcc"]
            },
            "component_errors": {},
        })

        def effective_session(market, **_kwargs):
            return prior_us_session if market == "us" else current_cn_session

        with patch(
            "src.services.cross_market_signal_service.trading_calendar."
            "get_effective_trading_date",
            side_effect=effective_session,
        ), patch.object(
            self.service,
            "_classify_asia_evidence_time",
            return_value={
                "accepted": True,
                "stage": "continuous",
                "age_seconds": 30.0,
            },
        ):
            premarket_signal = self.service.get_us_premarket_signal_for_cn_trade(
                theme="mlcc",
                now=NOW,
            )
            close_signal = self.service.get_us_close_theme_signal_for_cn_trade(
                theme="mlcc",
                now=NOW,
            )
            runtime = self.service.get_runtime_status(now=NOW)

        self.assertTrue(premarket_signal["asia_supplement_eligible"])
        self.assertTrue(close_signal["asia_supplement_eligible"])
        self.assertEqual(runtime["us_premarket"]["supplemented_themes"], ["mlcc"])
        self.assertTrue(runtime["us_close_themes"]["full_strategy_coverage"])
        self.assertEqual(
            runtime["us_close_themes"]["supplemented_themes"],
            ["mlcc"],
        )
        self.assertEqual(runtime["us_close_themes"]["missing_themes"], [])
        self.assertEqual(
            runtime["us_close_themes"]["themes"]["mlcc"]["session_date"],
            prior_us_session.isoformat(),
        )
        self.assertEqual(
            runtime["asia_supply_chain"]["mlcc"]["observed_at"],
            asia_observed_at.isoformat(),
        )

    def test_runtime_theme_coverage_rejects_missing_or_unrelated_supplement(self) -> None:
        available = {"available": True, "strong": False, "reason": "available"}
        unavailable = {
            "available": False,
            "strong": False,
            "reason": "unavailable",
        }

        def missing_semiconductor(*, theme, now):
            return unavailable if theme == "semiconductor" else available

        with patch.object(
            self.service,
            "get_us_premarket_signal_for_cn_trade",
            side_effect=missing_semiconductor,
        ), patch.object(
            self.service,
            "get_us_close_theme_signal_for_cn_trade",
            side_effect=missing_semiconductor,
        ), patch.object(
            self.service,
            "get_asia_theme_signal_for_cn_trade",
            return_value=available,
        ), patch.object(
            self.service,
            "_snapshot_collection_status",
            return_value={"required_stages_complete": True},
        ):
            unrelated = self.service.get_runtime_status(now=NOW)

        self.assertFalse(unrelated["us_premarket"]["full_strategy_coverage"])
        self.assertFalse(unrelated["us_close_themes"]["full_strategy_coverage"])
        self.assertEqual(unrelated["us_premarket"]["missing_themes"], ["semiconductor"])
        self.assertEqual(
            unrelated["us_close_themes"]["missing_themes"],
            ["semiconductor"],
        )

        def missing_mlcc(*, theme, now):
            return unavailable if theme == "mlcc" else available

        with patch.object(
            self.service,
            "get_us_premarket_signal_for_cn_trade",
            side_effect=missing_mlcc,
        ), patch.object(
            self.service,
            "get_us_close_theme_signal_for_cn_trade",
            side_effect=missing_mlcc,
        ), patch.object(
            self.service,
            "get_asia_theme_signal_for_cn_trade",
            return_value=unavailable,
        ), patch.object(
            self.service,
            "_snapshot_collection_status",
            return_value={"required_stages_complete": True},
        ):
            stale = self.service.get_runtime_status(now=NOW)

        self.assertFalse(stale["us_premarket"]["full_strategy_coverage"])
        self.assertFalse(stale["us_close_themes"]["full_strategy_coverage"])
        self.assertEqual(stale["us_premarket"]["supplemented_themes"], [])
        self.assertEqual(stale["us_close_themes"]["supplemented_themes"], [])
        self.assertEqual(stale["us_premarket"]["missing_themes"], ["mlcc"])
        self.assertEqual(stale["us_close_themes"]["missing_themes"], ["mlcc"])

    def test_collects_persists_and_restores_five_minute_korea_confirmation(self) -> None:
        for offset in range(6):
            sample_at = NOW + timedelta(minutes=offset)
            self.manager.provider_at = sample_at
            self.service.collect_korea_snapshot(now=sample_at)

        restored = CrossMarketSignalService(
            data_fetcher_manager=self.manager,
            state_path=self.state_path,
        )
        gate = restored.evaluate_korea_gate(
            theme="memory",
            now=NOW + timedelta(minutes=5),
            refresh=False,
        )
        status = restored.get_status(now=NOW + timedelta(minutes=5))

        self.assertEqual(gate["status"], "hold")
        self.assertTrue(gate["buy_allowed"])
        self.assertEqual(status["korea_snapshot_count"], 6)
        self.assertTrue(status["fresh"])
        self.assertTrue(status["quote_route"]["kis_available"])
        self.assertFalse(status["quote_route"]["naver_available"])
        self.assertTrue(status["quote_route"]["yfinance_available"])

    def test_decision_refresh_preserves_spaced_korea_confirmation(self) -> None:
        for offset in range(6):
            sample_at = NOW + timedelta(minutes=offset)
            self.manager.provider_at = sample_at
            self.service.collect_korea_snapshot(now=sample_at)

        decision_at = NOW + timedelta(minutes=5, seconds=10)
        self.manager.provider_at = decision_at
        gate = self.service.evaluate_korea_gate(
            theme="memory",
            now=decision_at,
            refresh=True,
        )

        self.assertEqual(gate["status"], "hold")
        self.assertTrue(gate["buy_allowed"])
        self.assertEqual(gate["confirmation_sample_count"], 6)
        self.assertGreaterEqual(gate["confirmation_span_seconds"], 300)
        self.assertEqual(gate["confirmation_duration_seconds"], 300)
        self.assertEqual(gate["confirmation_min_gap_seconds"], 45)
        self.assertEqual(gate["confirmation_max_gap_seconds"], 90)

    def test_live_korea_gate_uses_collection_completion_clock(self) -> None:
        started_at = datetime(2026, 8, 4, 1, 35, 0, tzinfo=timezone.utc)
        completed_at = started_at + timedelta(seconds=3)

        with patch(
            "src.services.cross_market_signal_service._utc_now",
            side_effect=[started_at, completed_at],
        ), patch.object(
            self.service,
            "collect_korea_snapshot_if_open",
            return_value={"accepted": True, "skipped": False},
        ) as collect_korea, patch.object(
            self.service,
            "load_korea_snapshots",
            return_value=[],
        ) as load_korea:
            gate = self.service.evaluate_korea_gate(
                theme="memory",
                refresh=True,
            )

        collect_korea.assert_called_once_with(now=None)
        load_korea.assert_called_once_with(now=completed_at)
        self.assertEqual(gate["evaluated_at"], completed_at.isoformat())

    def test_japan_gate_requires_nikkei_and_topix_to_rise_together(self) -> None:
        self.manager.provider_at = NOW
        snapshot = self.service.collect_japan_snapshot(now=NOW)
        rising = self.service.get_japan_market_gate(now=NOW)

        self.assertTrue(snapshot["buy_allowed"])
        self.assertTrue(rising["available"])
        self.assertTrue(rising["buy_allowed"])
        self.assertEqual(self.manager.calls, ["N225", "TOPX"])

        def mixed_quote(code):
            quote = self.manager.get_realtime_quote(code)
            if code == "TOPX" and quote is not None:
                quote.change_pct = -0.2
            return quote

        with patch.object(
            self.service,
            "_get_timestamped_quote",
            side_effect=mixed_quote,
        ):
            mixed_at = NOW + timedelta(seconds=30)
            self.manager.provider_at = mixed_at
            self.service.collect_japan_snapshot(now=mixed_at)
        mixed = self.service.get_japan_market_gate(now=mixed_at)

        self.assertTrue(mixed["available"])
        self.assertFalse(mixed["buy_allowed"])
        self.assertEqual(mixed["reason"], "nikkei_topix_not_both_rising")

    def test_japan_scheduled_market_closure_is_neutral_evidence(self) -> None:
        holiday = datetime(2026, 8, 11, 1, 35, tzinfo=timezone.utc)

        gate = self.service.get_japan_market_gate(now=holiday)

        self.assertTrue(gate["available"])
        self.assertTrue(gate["buy_allowed"])
        self.assertTrue(gate["neutral"])
        self.assertEqual(
            gate["reason"],
            "japan_scheduled_market_closure_neutral",
        )
        self.assertEqual(gate["session_date"], "2026-08-11")
        self.assertFalse(gate["market_phase"]["is_trading_day"])

    def test_japan_close_gate_carries_only_until_cn_market_close(self) -> None:
        japan_close = datetime(2026, 8, 3, 6, 30, tzinfo=timezone.utc)
        cn_intraday = datetime(2026, 8, 3, 6, 45, tzinfo=timezone.utc)
        self.manager.provider_at = japan_close

        snapshot = self.service.collect_japan_snapshot(now=cn_intraday)
        carried = self.service.get_japan_market_gate(now=cn_intraday)
        expired = self.service.get_japan_market_gate(
            now=datetime(2026, 8, 3, 7, 1, tzinfo=timezone.utc)
        )

        self.assertEqual(snapshot["evidence_stage"], "same_session_close")
        self.assertTrue(carried["available"])
        self.assertTrue(carried["buy_allowed"])
        self.assertEqual(carried["evidence_stage"], "same_session_close")
        self.assertFalse(expired["available"])
        self.assertEqual(expired["reason"], "japan_market_snapshot_not_fresh")

    def test_same_session_close_rejects_provider_older_than_five_minutes(self) -> None:
        timing = self.service._classify_asia_evidence_time(
            code="TOPX",
            provider_at=datetime(2026, 8, 3, 6, 24, 59, tzinfo=timezone.utc),
            now=datetime(2026, 8, 3, 6, 45, tzinfo=timezone.utc),
        )

        self.assertFalse(timing["accepted"])
        self.assertEqual(timing["reason"], "provider_time_not_near_session_close")

    def test_korea_close_confirmation_carries_only_until_cn_market_close(self) -> None:
        korea_close = datetime(2026, 8, 3, 6, 30, tzinfo=timezone.utc)
        for offset in range(6):
            sample_at = korea_close - timedelta(minutes=5 - offset, seconds=5)
            self.manager.provider_at = sample_at
            self.service.collect_korea_snapshot(now=sample_at)

        cn_intraday = datetime(2026, 8, 3, 6, 45, tzinfo=timezone.utc)
        carried = self.service.evaluate_korea_gate(
            theme="memory",
            now=cn_intraday,
            refresh=False,
        )
        broad = self.service.get_korea_broad_market_gate(now=cn_intraday)
        expired = self.service.evaluate_korea_gate(
            theme="memory",
            now=datetime(2026, 8, 3, 7, 1, tzinfo=timezone.utc),
            refresh=False,
        )

        self.assertTrue(carried["buy_allowed"])
        self.assertEqual(carried["evidence_stage"], "same_session_close")
        self.assertTrue(broad["available"])
        self.assertEqual(broad["evidence_stage"], "same_session_close")
        self.assertEqual(expired["status"], "unavailable")

    def test_combined_asia_gate_uses_closing_evidence_at_1430_scan(self) -> None:
        asia_close = datetime(2026, 8, 3, 6, 30, tzinfo=timezone.utc)
        cn_intraday = datetime(2026, 8, 3, 6, 45, tzinfo=timezone.utc)
        for offset in range(6):
            sample_at = asia_close - timedelta(minutes=5 - offset, seconds=5)
            self.manager.provider_at = sample_at
            self.service.collect_korea_snapshot(now=sample_at)
        self.manager.provider_at = asia_close
        self.service.collect_japan_snapshot(now=cn_intraday)

        def quote_for_code(code):
            quote = self.manager.get_realtime_quote(code)
            provider_at = (
                datetime(2026, 8, 3, 5, 30, tzinfo=timezone.utc)
                if code.endswith((".TW", ".TWO"))
                else asia_close
            )
            quote.provider_timestamp = provider_at.isoformat()
            return quote

        with patch.object(
            self.service,
            "_get_timestamped_quote",
            side_effect=quote_for_code,
        ):
            gate = self.service.evaluate_asia_market_gate(
                theme="mlcc",
                now=cn_intraday,
                refresh=True,
            )

        self.assertTrue(gate["available"])
        self.assertTrue(gate["buy_allowed"])
        self.assertEqual(gate["korea"]["evidence_stage"], "same_session_close")
        self.assertEqual(gate["japan"]["evidence_stage"], "same_session_close")
        self.assertEqual(
            gate["supply_chain"]["evidence_stage_counts"],
            {"same_session_close": 5},
        )

    def test_live_asia_gate_uses_collection_completion_clock(self) -> None:
        started_at = datetime(2026, 8, 4, 1, 35, 0, tzinfo=timezone.utc)
        completed_at = started_at + timedelta(seconds=4)
        korea_gate = {
            "available": True,
            "status": "hold",
            "buy_allowed": True,
        }
        japan_gate = {
            "available": True,
            "buy_allowed": True,
        }

        with patch(
            "src.services.cross_market_signal_service._utc_now",
            side_effect=[started_at, completed_at],
        ), patch.object(
            self.service,
            "collect_korea_snapshot_if_open",
            return_value={"accepted": True, "skipped": False},
        ) as collect_korea, patch.object(
            self.service,
            "collect_japan_snapshot_if_open",
            return_value={"accepted": True, "skipped": False},
        ) as collect_japan, patch.object(
            self.service,
            "get_korea_broad_market_gate",
            return_value=korea_gate,
        ) as get_korea, patch.object(
            self.service,
            "get_japan_market_gate",
            return_value=japan_gate,
        ) as get_japan:
            gate = self.service.evaluate_asia_market_gate(
                theme="compute_services",
                refresh=True,
            )

        self.assertTrue(gate["buy_allowed"])
        collect_korea.assert_called_once_with(now=None)
        collect_japan.assert_called_once_with(now=None)
        get_korea.assert_called_once_with(now=completed_at)
        get_japan.assert_called_once_with(now=completed_at)

    def test_asia_gate_treats_mild_mixed_markets_as_neutral(self) -> None:
        korea_gate = {
            "available": True,
            "status": "neutral",
            "buy_allowed": False,
            "mean_change_pct": -0.2,
        }
        japan_gate = {
            "available": True,
            "buy_allowed": False,
            "mean_change_pct": -0.3,
        }

        with patch.object(
            self.service,
            "get_korea_broad_market_gate",
            return_value=korea_gate,
        ), patch.object(
            self.service,
            "get_japan_market_gate",
            return_value=japan_gate,
        ):
            gate = self.service.evaluate_asia_market_gate(
                theme="compute_services",
                now=NOW,
                refresh=False,
            )

        self.assertTrue(gate["available"])
        self.assertTrue(gate["buy_allowed"])
        self.assertFalse(gate["positive_confirmation"])
        self.assertEqual(gate["reason"], "asia_markets_not_severely_weak")

    def test_asia_gate_bypasses_non_technology_theme_downside(self) -> None:
        with patch.object(
            self.service,
            "get_korea_broad_market_gate",
        ) as get_korea, patch.object(
            self.service,
            "get_japan_market_gate",
        ) as get_japan:
            gate = self.service.evaluate_asia_market_gate(
                theme="pharma",
                now=NOW,
                refresh=False,
            )

        self.assertTrue(gate["available"])
        self.assertTrue(gate["buy_allowed"])
        self.assertEqual(gate["score"], 50.0)
        self.assertEqual(
            gate["reason"],
            "asia_gate_not_applicable_non_technology_theme",
        )
        get_korea.assert_not_called()
        get_japan.assert_not_called()

    def test_asia_gate_blocks_only_material_market_downside(self) -> None:
        korea_gate = {
            "available": True,
            "status": "neutral",
            "buy_allowed": False,
            "mean_change_pct": -0.2,
        }
        japan_gate = {
            "available": True,
            "buy_allowed": False,
            "mean_change_pct": -0.8,
        }

        with patch.object(
            self.service,
            "get_korea_broad_market_gate",
            return_value=korea_gate,
        ), patch.object(
            self.service,
            "get_japan_market_gate",
            return_value=japan_gate,
        ):
            gate = self.service.evaluate_asia_market_gate(
                theme="compute_services",
                now=NOW,
                refresh=False,
            )

        self.assertTrue(gate["available"])
        self.assertFalse(gate["buy_allowed"])
        self.assertEqual(gate["reason"], "asia_market_severe_downside")
        self.assertEqual(gate["severe_downside_markets"], ["japan"])

    def test_asia_gate_reuses_fresh_supply_chain_for_candidate_recheck(
        self,
    ) -> None:
        korea_gate = {
            "available": True,
            "status": "hold",
            "buy_allowed": True,
        }
        japan_gate = {"available": True, "buy_allowed": True}
        supply_chain = {
            "available": True,
            "strong": True,
            "observed_at": NOW.isoformat(),
            "age_seconds": 20.0,
            "evidence_stage": "continuous",
        }

        with patch.object(
            self.service,
            "collect_korea_snapshot_if_open",
            return_value={"accepted": True, "skipped": False},
        ), patch.object(
            self.service,
            "collect_japan_snapshot_if_open",
            return_value={"accepted": True, "skipped": False},
        ), patch.object(
            self.service,
            "get_korea_broad_market_gate",
            return_value=korea_gate,
        ), patch.object(
            self.service,
            "get_japan_market_gate",
            return_value=japan_gate,
        ), patch.object(
            self.service,
            "get_asia_theme_signal_for_cn_trade",
            return_value=supply_chain,
        ) as get_supply_chain, patch.object(
            self.service,
            "collect_asia_theme_snapshot",
        ) as collect_supply_chain:
            gate = self.service.evaluate_asia_market_gate(
                theme="ccl",
                now=NOW,
                refresh=True,
                reuse_fresh_supply_chain=True,
            )

        self.assertTrue(gate["buy_allowed"])
        self.assertTrue(gate["supply_chain_refresh"]["reused_fresh"])
        self.assertFalse(gate["supply_chain_refresh"]["performed"])
        self.assertEqual(get_supply_chain.call_count, 2)
        collect_supply_chain.assert_not_called()

    def test_asia_gate_refreshes_unavailable_supply_chain_for_candidate_recheck(
        self,
    ) -> None:
        korea_gate = {
            "available": True,
            "status": "hold",
            "buy_allowed": True,
        }
        japan_gate = {"available": True, "buy_allowed": True}
        refreshed_supply_chain = {
            "available": True,
            "strong": True,
            "observed_at": NOW.isoformat(),
            "age_seconds": 0.0,
            "evidence_stage": "continuous",
        }

        with patch.object(
            self.service,
            "collect_korea_snapshot_if_open",
            return_value={"accepted": True, "skipped": False},
        ), patch.object(
            self.service,
            "collect_japan_snapshot_if_open",
            return_value={"accepted": True, "skipped": False},
        ), patch.object(
            self.service,
            "get_korea_broad_market_gate",
            return_value=korea_gate,
        ), patch.object(
            self.service,
            "get_japan_market_gate",
            return_value=japan_gate,
        ), patch.object(
            self.service,
            "get_asia_theme_signal_for_cn_trade",
            side_effect=[
                {"available": False, "reason": "asia_theme_signal_stale"},
                refreshed_supply_chain,
            ],
        ), patch.object(
            self.service,
            "collect_asia_theme_snapshot",
            return_value={"themes": {"ccl": refreshed_supply_chain}},
        ) as collect_supply_chain:
            gate = self.service.evaluate_asia_market_gate(
                theme="ccl",
                now=NOW,
                refresh=True,
                reuse_fresh_supply_chain=True,
            )

        self.assertTrue(gate["buy_allowed"])
        self.assertFalse(gate["supply_chain_refresh"]["reused_fresh"])
        self.assertTrue(gate["supply_chain_refresh"]["performed"])
        collect_supply_chain.assert_called_once_with(now=NOW)

    def test_asia_supply_chain_snapshot_covers_mlcc_and_ccl(self) -> None:
        self.manager.provider_at = NOW
        snapshot = self.service.collect_asia_theme_snapshot(now=NOW)

        self.assertTrue(snapshot["themes"]["mlcc"]["strong"])
        self.assertTrue(snapshot["themes"]["ccl"]["strong"])
        self.assertGreaterEqual(
            len(snapshot["themes"]["mlcc"]["available_codes"]),
            3,
        )
        self.assertGreaterEqual(
            len(snapshot["themes"]["ccl"]["available_codes"]),
            3,
        )
        self.assertIn("6274.TWO", snapshot["components"])
        self.assertNotIn("6274.TW", snapshot["components"])

    def test_asia_supply_chain_accepts_japan_lunch_break_close(self) -> None:
        current = datetime(2026, 8, 3, 2, 40, tzinfo=timezone.utc)

        def quote_for_code(code):
            quote = self.manager.get_realtime_quote(code)
            provider_at = (
                datetime(2026, 8, 3, 2, 30, tzinfo=timezone.utc)
                if code.endswith(".T")
                else current
            )
            quote.provider_timestamp = provider_at.isoformat()
            return quote

        with patch.object(
            self.service,
            "_get_timestamped_quote",
            side_effect=quote_for_code,
        ):
            snapshot = self.service.collect_asia_theme_snapshot(now=current)

        self.assertTrue(snapshot["themes"]["mlcc"]["available"])
        self.assertEqual(
            snapshot["themes"]["mlcc"]["evidence_stage_counts"],
            {"session_break_carry": 3, "live": 2},
        )

    def test_japan_lunch_carry_rejects_quote_updated_after_break_started(self) -> None:
        current = datetime(2026, 8, 3, 2, 40, tzinfo=timezone.utc)
        timing = self.service._classify_asia_evidence_time(
            code="6981.T",
            provider_at=datetime(2026, 8, 3, 2, 39, tzinfo=timezone.utc),
            now=current,
        )

        self.assertFalse(timing["accepted"])
        self.assertEqual(timing["reason"], "provider_time_not_near_session_break")

    def test_asia_supply_chain_accepts_same_day_closes_until_cn_close(self) -> None:
        current = datetime(2026, 8, 3, 6, 45, tzinfo=timezone.utc)

        def quote_for_code(code):
            quote = self.manager.get_realtime_quote(code)
            if code.endswith((".TW", ".TWO")):
                provider_at = datetime(2026, 8, 3, 5, 30, tzinfo=timezone.utc)
            else:
                provider_at = datetime(2026, 8, 3, 6, 30, tzinfo=timezone.utc)
            quote.provider_timestamp = provider_at.isoformat()
            return quote

        with patch.object(
            self.service,
            "_get_timestamped_quote",
            side_effect=quote_for_code,
        ):
            snapshot = self.service.collect_asia_theme_snapshot(now=current)
        mlcc = self.service.get_asia_theme_signal_for_cn_trade(
            theme="mlcc",
            now=current,
        )
        ccl = self.service.get_asia_theme_signal_for_cn_trade(
            theme="ccl",
            now=current,
        )

        self.assertTrue(mlcc["available"])
        self.assertTrue(ccl["available"])
        self.assertEqual(
            snapshot["themes"]["mlcc"]["evidence_stage_counts"],
            {"same_session_close": 5},
        )
        self.assertEqual(
            snapshot["themes"]["ccl"]["evidence_stage_counts"],
            {"same_session_close": 5},
        )

    def test_asia_signal_uses_latest_still_valid_snapshot_after_transient_failure(self) -> None:
        close_at = datetime(2026, 8, 3, 6, 30, 1, tzinfo=timezone.utc)

        def close_quote_for_code(code):
            quote = self.manager.get_realtime_quote(code)
            provider_at = (
                datetime(2026, 8, 3, 5, 30, tzinfo=timezone.utc)
                if code.endswith((".TW", ".TWO"))
                else datetime(2026, 8, 3, 6, 30, tzinfo=timezone.utc)
            )
            quote.provider_timestamp = provider_at.isoformat()
            return quote

        with patch.object(
            self.service,
            "_get_timestamped_quote",
            side_effect=close_quote_for_code,
        ):
            valid = self.service.collect_asia_theme_snapshot(now=close_at)

        failed_at = close_at + timedelta(minutes=1)
        self.service._append_snapshot("asia_theme_snapshots", {
            "observed_at": failed_at.isoformat(),
            "session_date": "2026-08-03",
            "themes": {
                "mlcc": {
                    "available": False,
                    "strong": False,
                    "reason": "asia_theme_coverage_insufficient",
                },
                "ccl": {
                    "available": False,
                    "strong": False,
                    "reason": "asia_theme_coverage_insufficient",
                },
            },
            "components": {},
            "component_errors": {"6981.T": "temporary_provider_failure"},
        })

        selected = self.service.get_asia_theme_signal_for_cn_trade(
            theme="mlcc",
            now=datetime(2026, 8, 3, 6, 45, tzinfo=timezone.utc),
        )
        expired = self.service.get_asia_theme_signal_for_cn_trade(
            theme="mlcc",
            now=datetime(2026, 8, 3, 7, 1, tzinfo=timezone.utc),
        )

        self.assertTrue(valid["themes"]["mlcc"]["available"])
        self.assertTrue(selected["available"])
        self.assertEqual(selected["observed_at"], close_at.isoformat())
        self.assertEqual(selected["latest_observed_at"], failed_at.isoformat())
        self.assertEqual(selected["skipped_newer_snapshot_count"], 1)
        self.assertEqual(
            selected["selection_reason"],
            "latest_currently_valid_asia_theme_snapshot",
        )
        self.assertFalse(expired["available"])
        self.assertNotIn("selection_reason", expired)

    def test_asia_close_carry_expires_at_cn_close_and_never_crosses_day(self) -> None:
        cn_postmarket = datetime(2026, 8, 3, 7, 1, tzinfo=timezone.utc)

        def quote_for_code(code):
            quote = self.manager.get_realtime_quote(code)
            if code.endswith((".TW", ".TWO")):
                provider_at = datetime(2026, 8, 3, 5, 30, tzinfo=timezone.utc)
            else:
                provider_at = datetime(2026, 8, 3, 6, 30, tzinfo=timezone.utc)
            quote.provider_timestamp = provider_at.isoformat()
            return quote

        with patch.object(
            self.service,
            "_get_timestamped_quote",
            side_effect=quote_for_code,
        ):
            snapshot = self.service.collect_asia_theme_snapshot(now=cn_postmarket)
            next_day = self.service.collect_asia_theme_snapshot(
                now=datetime(2026, 8, 4, 2, 40, tzinfo=timezone.utc)
            )

        self.assertFalse(snapshot["themes"]["mlcc"]["available"])
        self.assertFalse(snapshot["themes"]["ccl"]["available"])
        self.assertTrue(
            all(
                reason == "same_session_close_carry_expired"
                for reason in snapshot["component_errors"].values()
            )
        )
        self.assertFalse(next_day["themes"]["mlcc"]["available"])
        self.assertTrue(
            all(
                reason == "evidence_outside_current_session"
                for reason in next_day["component_errors"].values()
            )
        )

    def test_scheduled_asia_collection_runs_through_cn_cash_session(self) -> None:
        cn_intraday = datetime(2026, 8, 3, 6, 45, tzinfo=timezone.utc)
        with patch.object(
            self.service,
            "collect_asia_theme_snapshot",
            return_value={"themes": {}},
        ) as collect:
            result = self.service.collect_asia_theme_snapshot_if_cn_open(
                now=cn_intraday,
            )
            skipped = self.service.collect_asia_theme_snapshot_if_cn_open(
                now=datetime(2026, 8, 3, 7, 1, tzinfo=timezone.utc),
            )

        self.assertFalse(result["skipped"])
        self.assertEqual(result["reason"], "asia_theme_snapshot_collected")
        self.assertTrue(skipped["skipped"])
        self.assertEqual(skipped["reason"], "outside_cn_trading_session")
        collect.assert_called_once_with(now=cn_intraday)

    def test_live_asia_collection_validates_against_network_completion(self) -> None:
        self.manager.provider_at = NOW + timedelta(seconds=2)

        with patch(
            "src.services.cross_market_signal_service._utc_now",
            side_effect=[
                NOW,
                NOW + timedelta(seconds=5),
                NOW + timedelta(seconds=5),
            ],
        ):
            snapshot = self.service.collect_asia_theme_snapshot()

        self.assertEqual(snapshot["collection_started_at"], NOW.isoformat())
        self.assertEqual(snapshot["collection_duration_seconds"], 5.0)
        self.assertEqual(
            snapshot["observed_at"],
            (NOW + timedelta(seconds=5)).isoformat(),
        )

    def test_close_theme_pairs_same_session_premarket_and_marks_reversal(self) -> None:
        premarket_at = datetime(2026, 7, 23, 13, 25, tzinfo=timezone.utc)
        close_at = datetime(2026, 7, 23, 20, 0, tzinfo=timezone.utc)
        cn_at = datetime(2026, 7, 24, 1, 35, tzinfo=timezone.utc)
        signal_theme = "semiconductor_equipment"
        self.service._append_snapshot("us_premarket_snapshots", {
            "observed_at": premarket_at.isoformat(),
            "session_date": "2026-07-23",
            "session_stage": "premarket",
            "theme_signals": {
                signal_theme: {
                    "available": True,
                    "strong": True,
                    "score": 85.0,
                    "sector_change_pct": 3.0,
                    "advancing_ratio": 0.8,
                    "leader_change_pct": 4.0,
                },
            },
        })
        self.service._append_snapshot("us_close_theme_snapshots", {
            "observed_at": close_at.isoformat(),
            "session_date": "2026-07-23",
            "session_stage": "close",
            "theme_signals": {
                signal_theme: {
                    "available": True,
                    "strong": False,
                    "score": -20.0,
                    "sector_change_pct": -0.8,
                    "advancing_ratio": 0.2,
                    "leader_change_pct": 0.1,
                },
            },
        })

        with patch(
            "src.services.cross_market_signal_service.trading_calendar.get_effective_trading_date",
            return_value=date(2026, 7, 23),
        ):
            signal = self.service.get_us_close_theme_signal_for_cn_trade(
                theme="equipment",
                now=cn_at,
            )

        self.assertEqual(
            US_PREMARKET_A_SHARE_THEME_MAP["equipment"],
            signal_theme,
        )
        self.assertTrue(signal["available"])
        self.assertTrue(signal["premarket_reversal_invalidated"])
        self.assertEqual(signal["paired_premarket"]["session_date"], "2026-07-23")

    def test_five_korea_samples_spanning_four_minutes_do_not_confirm(self) -> None:
        for offset in range(5):
            sample_at = NOW + timedelta(minutes=offset)
            self.manager.provider_at = sample_at
            self.service.collect_korea_snapshot(now=sample_at)

        gate = self.service.evaluate_korea_gate(
            theme="memory",
            now=NOW + timedelta(minutes=4),
            refresh=False,
        )

        self.assertEqual(gate["status"], "unavailable")
        self.assertEqual(gate["reason"], "korea_confirmation_duration_insufficient")
        self.assertFalse(gate["buy_allowed"])
        self.assertFalse(gate["confirmed"])

    def test_runtime_status_requires_five_minute_korea_confirmation(self) -> None:
        self.service.collect_korea_snapshot(now=NOW)
        with patch.object(
            self.service,
            "get_us_tech_signal_for_cn_trade",
            return_value={"available": True, "buy_allowed": True},
        ), patch.object(
            self.service,
            "get_nasdaq_futures_signal_for_cn_trade",
            return_value={
                "available": True,
                "confirmed": True,
                "buy_allowed": True,
            },
        ), patch.object(
            self.service,
            "get_cn_open_signal",
            return_value={"available": True, "buy_allowed": True},
        ), patch.object(
            self.service,
            "get_gold_signal_for_cn_trade",
            return_value={"available": False, "buy_allowed": False},
        ):
            early = self.service.get_runtime_status(now=NOW)
            for offset in range(1, 6):
                sample_at = NOW + timedelta(minutes=offset)
                self.manager.provider_at = sample_at
                self.service.collect_korea_snapshot(now=sample_at)
            confirmed = self.service.get_runtime_status(now=NOW + timedelta(minutes=5))

        self.assertFalse(early["buy_paths"]["linked_technology_ready"])
        self.assertEqual(
            early["korea"]["linked_technology_gate"]["reason"],
            "korea_confirmation_samples_insufficient",
        )
        self.assertTrue(confirmed["buy_paths"]["linked_technology_ready"])

    def test_collection_fails_closed_without_complete_fresh_provider_evidence(self) -> None:
        self.manager.missing_code = "KQ11"
        with self.assertRaisesRegex(ValueError, "korea_quote_unavailable:KQ11"):
            self.service.collect_korea_snapshot(now=NOW)
        self.assertFalse(self.state_path.exists())

        self.manager.missing_code = None
        self.manager.provider_at = NOW - timedelta(seconds=121)
        with self.assertRaisesRegex(ValueError, "korea_evidence_stale"):
            self.service.collect_korea_snapshot(now=NOW)
        self.assertFalse(self.state_path.exists())

    def test_live_korea_collection_validates_against_network_completion_time(self) -> None:
        original_get_quote = self.manager.get_realtime_quote

        def progressing_quote(code):
            self.manager.provider_at = NOW + timedelta(
                seconds=len(self.manager.calls) + 1
            )
            return original_get_quote(code)

        self.manager.get_realtime_quote = progressing_quote
        with patch(
            "src.services.cross_market_signal_service._utc_now",
            side_effect=[
                NOW,
                NOW + timedelta(seconds=5),
                NOW + timedelta(seconds=5),
            ],
        ):
            snapshot = self.service.collect_korea_snapshot()

        self.assertEqual(
            snapshot["collection_started_at"],
            NOW.isoformat(),
        )
        self.assertEqual(snapshot["collection_duration_seconds"], 5.0)
        self.assertEqual(snapshot["observed_at"], (NOW + timedelta(seconds=5)).isoformat())

    def test_korea_collection_still_rejects_provider_time_after_completion(self) -> None:
        self.manager.provider_at = NOW + timedelta(seconds=2)

        with self.assertRaisesRegex(ValueError, "korea_evidence_from_future"):
            self.service.collect_korea_snapshot(now=NOW)

        self.assertFalse(self.state_path.exists())

    def test_live_korea_collection_waits_for_small_provider_clock_skew(self) -> None:
        self.manager.provider_at = NOW + timedelta(seconds=1.2)
        settled_at = NOW + timedelta(
            seconds=0.2 + KOREA_LIVE_PROVIDER_CLOCK_SKEW_MARGIN_SECONDS
        )

        with patch(
            "src.services.cross_market_signal_service._utc_now",
            side_effect=[NOW, NOW, settled_at, settled_at],
        ), patch("src.services.cross_market_signal_service.time.sleep") as sleep:
            snapshot = self.service.collect_korea_snapshot()

        sleep.assert_called_once()
        self.assertAlmostEqual(
            sleep.call_args.args[0],
            0.2 + KOREA_LIVE_PROVIDER_CLOCK_SKEW_MARGIN_SECONDS,
        )
        self.assertEqual(snapshot["observed_at"], settled_at.isoformat())
        self.assertEqual(
            snapshot["components"]["005930.KS"]["provider_timestamp"],
            (NOW + timedelta(seconds=1.2)).isoformat(),
        )

    def test_cpo_gate_does_not_fetch_korea_quotes(self) -> None:
        gate = self.service.evaluate_korea_gate(theme="cpo", now=NOW)

        self.assertEqual(gate["status"], "bypassed")
        self.assertEqual(self.manager.calls, [])

    def test_scheduled_collection_skips_outside_korea_session_without_fetching(self) -> None:
        phase = SimpleNamespace(
            is_market_open_now=False,
            to_dict=lambda: {"phase": "postmarket", "is_market_open_now": False},
        )
        with patch(
            "src.services.cross_market_signal_service.trading_calendar.build_market_phase_context",
            return_value=phase,
        ):
            result = self.service.collect_korea_snapshot_if_open(now=NOW)

        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "outside_korea_trading_session")
        self.assertEqual(self.manager.calls, [])

    def test_scheduled_collection_fetches_during_korea_session(self) -> None:
        phase = SimpleNamespace(
            is_market_open_now=True,
            to_dict=lambda: {"phase": "intraday", "is_market_open_now": True},
        )
        with patch(
            "src.services.cross_market_signal_service.trading_calendar.build_market_phase_context",
            return_value=phase,
        ):
            result = self.service.collect_korea_snapshot_if_open(now=NOW)

        self.assertFalse(result["skipped"])
        self.assertEqual(result["reason"], "korea_snapshot_collected")
        self.assertEqual(len(self.manager.calls), 4)

    def test_live_scheduled_collection_uses_basket_completion_time(self) -> None:
        original_get_quote = self.manager.get_realtime_quote

        def progressing_quote(code):
            self.manager.provider_at = NOW + timedelta(
                seconds=len(self.manager.calls) + 1
            )
            return original_get_quote(code)

        self.manager.get_realtime_quote = progressing_quote
        phase = SimpleNamespace(
            is_market_open_now=True,
            to_dict=lambda: {"phase": "intraday", "is_market_open_now": True},
        )
        with patch(
            "src.services.cross_market_signal_service.trading_calendar.build_market_phase_context",
            return_value=phase,
        ), patch(
            "src.services.cross_market_signal_service._utc_now",
            side_effect=[
                NOW,
                NOW,
                NOW + timedelta(seconds=5),
                NOW + timedelta(seconds=5),
            ],
        ):
            result = self.service.collect_korea_snapshot_if_open()

        self.assertFalse(result["skipped"])
        self.assertEqual(result["snapshot"]["collection_duration_seconds"], 5.0)
        self.assertEqual(
            result["snapshot"]["observed_at"],
            (NOW + timedelta(seconds=5)).isoformat(),
        )

    def test_us_first_hour_and_close_signals_are_frozen_for_next_cn_session(self) -> None:
        first_hour = datetime(2026, 7, 23, 14, 30, tzinfo=timezone.utc)
        us_close = datetime(2026, 7, 23, 20, 0, tzinfo=timezone.utc)
        self.manager.provider_at = first_hour
        self.manager.change_pct = 1.0
        first_hour_snapshot = self.service.collect_us_tech_snapshot(
            now=first_hour,
            session_stage="first_hour",
        )
        self.manager.provider_at = us_close
        self.manager.change_pct = 1.5
        snapshot = self.service.collect_us_tech_snapshot(
            now=us_close,
            session_stage="close",
        )
        next_cn_open = datetime(2026, 7, 24, 1, 30, tzinfo=timezone.utc)
        signal = self.service.get_us_tech_signal_for_cn_trade(now=next_cn_open)
        runtime = self.service.get_runtime_status(now=next_cn_open)
        before_close = self.service.get_us_tech_signal_for_cn_trade(
            now=us_close - timedelta(minutes=1)
        )

        self.assertEqual(snapshot["session_stage"], "close")
        self.assertTrue(signal["available"])
        self.assertTrue(signal["buy_allowed"])
        self.assertEqual(signal["session_date"], "2026-07-23")
        self.assertEqual(signal["first_hour_score"], first_hour_snapshot["score"])
        self.assertEqual(signal["close_score"], snapshot["score"])
        self.assertAlmostEqual(
            signal["score"],
            0.4 * first_hour_snapshot["score"] + 0.6 * snapshot["score"],
            places=6,
        )
        self.assertFalse(before_close["available"])
        collection = runtime["us_tech"]["collection"]
        self.assertEqual(collection["latest_session_date"], "2026-07-23")
        self.assertEqual(collection["session_stage_counts"], {"first_hour": 1, "close": 1})
        self.assertTrue(collection["required_stages_complete"])

    def test_nq00y_requires_continuous_samples_and_blocks_bearish_technology(self) -> None:
        first_at = datetime(2026, 7, 23, 1, 30, tzinfo=timezone.utc)
        samples = (
            (first_at, 100.0, -0.2),
            (first_at + timedelta(minutes=1), 99.8, -0.4),
            (first_at + timedelta(minutes=2), 99.4, -0.7),
        )
        for observed_at, price, change_pct in samples:
            self.manager.provider_at = observed_at
            self.manager.price = price
            self.manager.change_pct = change_pct
            snapshot = self.service.collect_nasdaq_futures_snapshot(now=observed_at)
            self.assertEqual(snapshot["code"], "NQ00Y")

        signal = self.service.get_nasdaq_futures_signal_for_cn_trade(
            now=samples[-1][0],
        )

        self.assertTrue(signal["available"])
        self.assertTrue(signal["confirmed"])
        self.assertFalse(signal["buy_allowed"])
        self.assertEqual(signal["reason"], "nasdaq_futures_downtrend_blocks_entry")
        self.assertLess(signal["trend_change_pct"], -0.5)
        self.assertEqual(signal["confirmation_sample_count"], 3)
        self.assertEqual(signal["confirmation_span_seconds"], 120.0)
        self.assertLess(signal["sector_score_adjustment"], 0.0)
        self.assertGreaterEqual(signal["score"], 0.0)
        self.assertLessEqual(signal["score"], 100.0)
        self.assertLess(signal["score"], 50.0)
        self.assertLess(signal["directional_score"], 0.0)

    def test_nq00y_severe_downtrend_reduces_half_and_stale_evidence_fails_closed(self) -> None:
        first_at = datetime(2026, 7, 23, 1, 30, tzinfo=timezone.utc)
        for offset, price in enumerate((100.0, 99.6, 99.1)):
            observed_at = first_at + timedelta(minutes=offset)
            self.manager.provider_at = observed_at
            self.manager.price = price
            self.manager.change_pct = -1.6
            self.service.collect_nasdaq_futures_snapshot(now=observed_at)

        severe = self.service.get_nasdaq_futures_signal_for_cn_trade(
            now=first_at + timedelta(minutes=2),
        )
        stale = self.service.get_nasdaq_futures_signal_for_cn_trade(
            now=first_at + timedelta(minutes=5),
        )

        self.assertEqual(severe["reason"], "nasdaq_futures_severe_downtrend")
        self.assertEqual(severe["sell_fraction"], 0.5)
        self.assertFalse(stale["available"])
        self.assertEqual(stale["reason"], "nasdaq_futures_signal_stale")

    def test_scheduled_us_task_collects_nq00y_during_cn_session(self) -> None:
        cn_at = datetime(2026, 7, 23, 1, 30, tzinfo=timezone.utc)
        self.manager.provider_at = cn_at
        self.manager.price = 28453.0
        self.manager.change_pct = 0.4

        result = self.service.collect_us_tech_snapshot_if_open(now=cn_at)

        self.assertFalse(result["skipped"])
        self.assertEqual(result["reason"], "nasdaq_futures_snapshot_collected")
        self.assertEqual(result["snapshot"]["code"], "NQ00Y")
        self.assertEqual(self.manager.calls, ["NQ00Y"])

    def test_nq00y_throttle_allows_each_minute_despite_collection_jitter(self) -> None:
        first_scheduled_at = datetime(2026, 7, 23, 1, 25, tzinfo=timezone.utc)
        first_observed_at = first_scheduled_at + timedelta(seconds=2)
        self.service._append_snapshot(
            NASDAQ_FUTURES_STATE_KEY,
            {
                "session_date": "2026-07-23",
                "session_stage": "cn_intraday",
                "observed_at": first_observed_at.isoformat(),
            },
        )

        due = self.service._session_stage_capture_due(
            state_key=NASDAQ_FUTURES_STATE_KEY,
            session_date="2026-07-23",
            session_stage="cn_intraday",
            now=first_scheduled_at + timedelta(seconds=60),
            minimum_interval_seconds=NASDAQ_FUTURES_CAPTURE_THROTTLE_SECONDS,
        )

        self.assertTrue(due)

    def test_premarket_universe_is_globally_deduplicated_and_theme_normalized(self) -> None:
        self.manager.provider_at = NOW
        self.manager.change_pct = 3.5

        snapshot = self.service.collect_us_premarket_snapshot(now=NOW)

        flattened = [
            code
            for codes in US_PREMARKET_THEME_CODES.values()
            for code in codes
        ]
        self.assertIn("P", flattened)
        self.assertNotIn("PSTG", flattened)
        self.assertNotIn("KYOCY", flattened)
        self.assertEqual(len(flattened), len(set(flattened)))
        self.assertEqual(snapshot["universe_size"], len(set(flattened)))
        self.assertEqual(snapshot["collected_component_count"], len(set(flattened)))
        self.assertEqual(
            snapshot["collection_worker_count"],
            min(US_THEME_COLLECTION_MAX_WORKERS, len(set(flattened))),
        )
        for theme, signal in snapshot["theme_signals"].items():
            with self.subTest(theme=theme):
                self.assertTrue(signal["available"])
                self.assertTrue(signal["strong"])
                self.assertEqual(signal["coverage_ratio"], 1.0)
        self.assertEqual(snapshot["theme_signals"]["mlcc"]["required_count"], 2)
        self.assertEqual(
            snapshot["theme_signals"]["mlcc"]["codes"],
            ["TTDKY", "MRAAY"],
        )

    def test_mlcc_requires_both_mapped_otc_leaders_at_premarket_and_close(self) -> None:
        self.manager.provider_at = NOW

        complete_close = self.service.collect_us_close_theme_snapshot(now=NOW)
        complete_mlcc = complete_close["theme_signals"]["mlcc"]
        self.assertTrue(complete_mlcc["available"])
        self.assertEqual(complete_mlcc["required_count"], 2)
        self.assertEqual(complete_mlcc["codes"], ["TTDKY", "MRAAY"])

        self.manager.missing_code = "MRAAY"
        partial_service = CrossMarketSignalService(
            data_fetcher_manager=self.manager,
            state_path=Path(self.temp_dir.name) / "partial-mlcc-state.json",
        )
        premarket = partial_service.collect_us_premarket_snapshot(now=NOW)
        close = partial_service.collect_us_close_theme_snapshot(now=NOW)

        for snapshot, reason in (
            (premarket, "premarket_theme_coverage_insufficient"),
            (close, "us_close_theme_coverage_insufficient"),
        ):
            with self.subTest(session_stage=snapshot["session_stage"]):
                mlcc = snapshot["theme_signals"]["mlcc"]
                self.assertFalse(mlcc["available"])
                self.assertEqual(mlcc["reason"], reason)
                self.assertEqual(mlcc["required_count"], 2)
                self.assertEqual(mlcc["available_codes"], ["TTDKY"])
                self.assertEqual(mlcc["missing_codes"], ["MRAAY"])
                self.assertEqual(mlcc["coverage_ratio"], 0.5)

    def test_premarket_collection_uses_extended_hours_timestamp_route(self) -> None:
        strict_calls = []

        def premarket_quote(code):
            strict_calls.append(code)
            return UnifiedRealtimeQuote(
                code=code,
                source=RealtimeSource.FALLBACK,
                provider_timestamp=NOW.isoformat(),
                fetched_at=NOW.isoformat(),
                market="us",
                currency="USD",
                data_quality="partial",
                price=103.0,
                change_pct=3.0,
                volume=100.0,
            )

        self.manager.get_cross_market_us_premarket_quote_with_provider_timestamp = (
            premarket_quote
        )
        with patch.object(
            self.manager,
            "get_realtime_quote",
            side_effect=AssertionError("regular-session route must not serve premarket"),
        ):
            snapshot = self.service.collect_us_premarket_snapshot(now=NOW)

        self.assertEqual(set(strict_calls), set(snapshot["components"]))
        self.assertEqual(snapshot["collected_component_count"], len(strict_calls))

    def test_premarket_collection_uses_batch_stream_then_retries_only_gaps(self) -> None:
        codes = sorted({
            code
            for theme_codes in US_PREMARKET_THEME_CODES.values()
            for code in theme_codes
        })

        def quote(code):
            return UnifiedRealtimeQuote(
                code=code,
                source=RealtimeSource.YAHOO_CHART,
                provider_timestamp=NOW.isoformat(),
                fetched_at=NOW.isoformat(),
                market="us",
                currency="USD",
                data_quality="partial",
                price=103.0,
                change_pct=3.0,
            )

        bulk = MagicMock(return_value={code: quote(code) for code in codes if code != "INTC"})
        single = MagicMock(side_effect=quote)
        self.manager.get_cross_market_us_premarket_quotes_with_provider_timestamps = (
            bulk
        )
        self.manager.get_cross_market_us_premarket_quote_with_provider_timestamp = (
            single
        )

        snapshot = self.service.collect_us_premarket_snapshot(now=NOW)

        bulk.assert_called_once_with(codes)
        single.assert_called_once_with("INTC")
        self.assertEqual(snapshot["streamed_quote_count"], len(codes) - 1)
        self.assertEqual(snapshot["streamed_component_count"], len(codes) - 1)
        self.assertEqual(snapshot["stream_rejected_component_count"], 0)
        self.assertEqual(snapshot["gap_fill_request_count"], 1)
        self.assertEqual(snapshot["collected_component_count"], len(codes))

    def test_premarket_collection_gap_fills_stale_stream_tick(self) -> None:
        codes = sorted({
            code
            for theme_codes in US_PREMARKET_THEME_CODES.values()
            for code in theme_codes
        })

        def quote(code: str, provider_at: datetime) -> UnifiedRealtimeQuote:
            return UnifiedRealtimeQuote(
                code=code,
                source=RealtimeSource.YAHOO_STREAMER,
                provider_timestamp=provider_at.isoformat(),
                fetched_at=NOW.isoformat(),
                market="us",
                currency="USD",
                data_quality="partial",
                price=103.0,
                change_pct=3.0,
            )

        streamed = {
            code: quote(
                code,
                NOW - timedelta(seconds=121) if code == "INTC" else NOW,
            )
            for code in codes
        }
        bulk = MagicMock(return_value=streamed)
        single = MagicMock(side_effect=lambda code: quote(code, NOW))
        self.manager.get_cross_market_us_premarket_quotes_with_provider_timestamps = (
            bulk
        )
        self.manager.get_cross_market_us_premarket_quote_with_provider_timestamp = (
            single
        )

        snapshot = self.service.collect_us_premarket_snapshot(now=NOW)

        bulk.assert_called_once_with(codes)
        single.assert_called_once_with("INTC")
        self.assertEqual(snapshot["streamed_quote_count"], len(codes))
        self.assertEqual(snapshot["streamed_component_count"], len(codes) - 1)
        self.assertEqual(snapshot["stream_rejected_component_count"], 1)
        self.assertEqual(snapshot["stream_rejections"]["INTC"], "evidence_stale")
        self.assertEqual(snapshot["gap_fill_request_count"], 1)
        self.assertEqual(
            snapshot["components"]["INTC"]["provider_timestamp"],
            NOW.isoformat(),
        )

    def test_premarket_collection_reuses_only_fresh_same_session_stream_ticks(self) -> None:
        collected_at = datetime(2026, 7, 23, 13, 25, tzinfo=timezone.utc)
        previous_at = collected_at - timedelta(seconds=40)
        self.service._append_snapshot("us_premarket_snapshots", {
            "observed_at": previous_at.isoformat(),
            "session_date": collected_at.astimezone(
                ZoneInfo("America/New_York")
            ).date().isoformat(),
            "session_stage": "premarket",
            "components": {
                "INTC": {
                    "change_pct": 1.25,
                    "price": 42.0,
                    "provider_timestamp": previous_at.isoformat(),
                    "source": "yahoo_chart",
                    "data_quality": "partial",
                }
            },
        })
        self.manager.provider_at = collected_at
        self.manager.missing_code = "INTC"

        snapshot = self.service.collect_us_premarket_snapshot(
            now=collected_at,
            session_open_at=collected_at + timedelta(minutes=5),
        )

        self.assertIn("INTC", snapshot["components"])
        self.assertEqual(snapshot["reused_fresh_component_count"], 1)
        self.assertEqual(
            snapshot["components"]["INTC"]["provider_timestamp"],
            previous_at.isoformat(),
        )

    def test_zero_theme_capture_is_persisted_for_fresh_next_minute_reuse(self) -> None:
        first_at = datetime(2026, 7, 23, 13, 25, tzinfo=timezone.utc)
        second_at = first_at + timedelta(minutes=1)
        session_open_at = first_at + timedelta(minutes=5)

        def quote(code: str, observed_at: datetime) -> UnifiedRealtimeQuote:
            return UnifiedRealtimeQuote(
                code=code,
                source=RealtimeSource.YAHOO_CHART,
                provider_timestamp=observed_at.isoformat(),
                fetched_at=observed_at.isoformat(),
                market="us",
                currency="USD",
                data_quality="partial",
                price=103.0,
                change_pct=3.0,
            )

        bulk = MagicMock(side_effect=[
            {"ASML": quote("ASML", first_at)},
            {
                "AMAT": quote("AMAT", second_at),
                "LRCX": quote("LRCX", second_at),
            },
        ])
        single = MagicMock(return_value=None)
        self.manager.get_cross_market_us_premarket_quotes_with_provider_timestamps = (
            bulk
        )
        self.manager.get_cross_market_us_premarket_quote_with_provider_timestamp = (
            single
        )

        with self.assertRaisesRegex(
            ValueError,
            "us_premarket_evidence_unavailable",
        ):
            self.service.collect_us_premarket_snapshot(
                now=first_at,
                session_open_at=session_open_at,
            )

        first_state = self.service._read_state()
        self.assertEqual(
            len(first_state[US_PREMARKET_CAPTURE_ATTEMPTS_STATE_KEY]),
            1,
        )
        self.assertFalse(
            first_state[US_PREMARKET_CAPTURE_ATTEMPTS_STATE_KEY][0][
                "snapshot_persisted"
            ]
        )
        self.assertEqual(first_state.get("us_premarket_snapshots", []), [])
        first_summary = self.service._latest_premarket_capture_summary(
            first_state,
            current=first_at,
            required_themes=list(US_PREMARKET_A_SHARE_THEME_MAP),
        )
        self.assertTrue(first_summary["available"])
        self.assertFalse(first_summary["snapshot_persisted"])
        self.assertEqual(first_summary["available_theme_count"], 0)

        snapshot = self.service.collect_us_premarket_snapshot(
            now=second_at,
            session_open_at=session_open_at,
        )

        equipment = snapshot["theme_signals"]["semiconductor_equipment"]
        self.assertTrue(equipment["available"])
        self.assertEqual(equipment["codes"], ["ASML", "AMAT", "LRCX"])
        self.assertEqual(snapshot["reused_fresh_component_count"], 1)
        self.assertTrue(snapshot["snapshot_persisted"])
        self.assertNotIn("ASML", snapshot["component_errors"])
        final_state = self.service._read_state()
        self.assertEqual(
            len(final_state[US_PREMARKET_CAPTURE_ATTEMPTS_STATE_KEY]),
            2,
        )
        self.assertEqual(len(final_state["us_premarket_snapshots"]), 1)

    def test_zero_theme_capture_does_not_reuse_component_after_120_seconds(self) -> None:
        first_at = datetime(2026, 7, 23, 13, 25, tzinfo=timezone.utc)
        second_at = first_at + timedelta(seconds=121)
        session_open_at = first_at + timedelta(minutes=10)

        def quote(code: str, observed_at: datetime) -> UnifiedRealtimeQuote:
            return UnifiedRealtimeQuote(
                code=code,
                source=RealtimeSource.YAHOO_CHART,
                provider_timestamp=observed_at.isoformat(),
                fetched_at=observed_at.isoformat(),
                market="us",
                currency="USD",
                data_quality="partial",
                price=103.0,
                change_pct=3.0,
            )

        bulk = MagicMock(side_effect=[
            {"ASML": quote("ASML", first_at)},
            {
                "AMAT": quote("AMAT", second_at),
                "LRCX": quote("LRCX", second_at),
            },
        ])
        self.manager.get_cross_market_us_premarket_quotes_with_provider_timestamps = (
            bulk
        )
        self.manager.get_cross_market_us_premarket_quote_with_provider_timestamp = (
            MagicMock(return_value=None)
        )

        for collected_at in (first_at, second_at):
            with self.assertRaisesRegex(
                ValueError,
                "us_premarket_evidence_unavailable",
            ):
                self.service.collect_us_premarket_snapshot(
                    now=collected_at,
                    session_open_at=session_open_at,
                )

        state = self.service._read_state()
        self.assertEqual(
            len(state[US_PREMARKET_CAPTURE_ATTEMPTS_STATE_KEY]),
            2,
        )
        self.assertEqual(state.get("us_premarket_snapshots", []), [])
        latest = state[US_PREMARKET_CAPTURE_ATTEMPTS_STATE_KEY][-1]
        self.assertNotIn("ASML", latest["components"])
        self.assertEqual(latest["component_errors"]["ASML"], "quote_unavailable")

    def test_zero_theme_close_capture_is_persisted_for_fresh_retry_reuse(self) -> None:
        first_at = datetime(2026, 7, 23, 20, 0, 15, tzinfo=timezone.utc)
        second_at = first_at + timedelta(minutes=1)
        available_codes = {"ASML"}
        provider_at = first_at

        def get_quote(code: str):
            if code not in available_codes:
                return None
            return UnifiedRealtimeQuote(
                code=code,
                source=RealtimeSource.TENCENT,
                provider_timestamp=provider_at.isoformat(),
                fetched_at=provider_at.isoformat(),
                market="us",
                currency="USD",
                data_quality="partial",
                price=103.0,
                change_pct=1.0,
            )

        with patch.object(
            self.service,
            "_get_timestamped_us_quote",
            side_effect=get_quote,
        ):
            with self.assertRaisesRegex(
                ValueError,
                "us_close_theme_evidence_unavailable",
            ):
                self.service.collect_us_close_theme_snapshot(now=first_at)

            first_state = self.service._read_state()
            self.assertEqual(
                len(first_state[US_CLOSE_THEME_CAPTURE_ATTEMPTS_STATE_KEY]),
                1,
            )
            self.assertFalse(
                first_state[US_CLOSE_THEME_CAPTURE_ATTEMPTS_STATE_KEY][0][
                    "snapshot_persisted"
                ]
            )
            self.assertEqual(first_state.get("us_close_theme_snapshots", []), [])

            available_codes = {"AMAT", "LRCX"}
            provider_at = second_at
            snapshot = self.service.collect_us_close_theme_snapshot(now=second_at)

        equipment = snapshot["theme_signals"]["semiconductor_equipment"]
        self.assertTrue(equipment["available"])
        self.assertEqual(equipment["codes"], ["ASML", "AMAT", "LRCX"])
        self.assertEqual(snapshot["reused_fresh_component_count"], 1)
        self.assertTrue(snapshot["snapshot_persisted"])
        self.assertNotIn("ASML", snapshot["component_errors"])
        final_state = self.service._read_state()
        self.assertEqual(
            len(final_state[US_CLOSE_THEME_CAPTURE_ATTEMPTS_STATE_KEY]),
            2,
        )
        self.assertEqual(len(final_state["us_close_theme_snapshots"]), 1)

    def test_incomplete_premarket_capture_can_retry_at_next_minute(self) -> None:
        first_at = datetime(2026, 7, 23, 13, 25, tzinfo=timezone.utc)
        self.manager.provider_at = first_at
        first = self.service.collect_us_tech_snapshot_if_open(now=first_at)
        self.manager.provider_at = first_at + timedelta(minutes=1)

        second = self.service.collect_us_tech_snapshot_if_open(
            now=first_at + timedelta(minutes=1)
        )

        self.assertEqual(first["reason"], "us_premarket_snapshot_collected")
        self.assertEqual(second["reason"], "us_premarket_snapshot_collected")

    def test_scheduled_premarket_warms_stream_before_capture_window(self) -> None:
        warmup_at = datetime(2026, 7, 23, 13, 14, 30, tzinfo=timezone.utc)
        codes = sorted({
            code
            for theme_codes in US_PREMARKET_THEME_CODES.values()
            for code in theme_codes
        })
        bulk = MagicMock(return_value={})
        self.manager.get_cross_market_us_premarket_quotes_with_provider_timestamps = (
            bulk
        )

        result = self.service.collect_us_tech_snapshot_if_open(now=warmup_at)

        self.assertTrue(result["accepted"])
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "us_premarket_stream_warmed")
        self.assertEqual(result["streamed_quote_count"], 0)
        bulk.assert_called_once_with(codes)
        state = self.service._read_state()
        self.assertEqual(state.get("us_premarket_snapshots", []), [])
        self.assertEqual(
            state.get(US_PREMARKET_CAPTURE_ATTEMPTS_STATE_KEY, []),
            [],
        )

    def test_empty_premarket_capture_is_retryable_evidence_skip(self) -> None:
        premarket_at = datetime(2026, 7, 23, 13, 25, tzinfo=timezone.utc)

        with patch.object(
            self.service,
            "collect_us_premarket_snapshot",
            side_effect=ValueError("us_premarket_evidence_unavailable"),
        ) as collect:
            result = self.service.collect_us_tech_snapshot_if_open(
                now=premarket_at
            )

        self.assertFalse(result["accepted"])
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "us_premarket_evidence_unavailable")
        self.assertFalse(result["evidence_ready"])
        self.assertTrue(result["retryable"])
        self.assertEqual(result["retry_after_seconds"], 30)
        collect.assert_called_once()

    def test_unexpected_premarket_value_error_still_fails(self) -> None:
        premarket_at = datetime(2026, 7, 23, 13, 25, tzinfo=timezone.utc)

        with patch.object(
            self.service,
            "collect_us_premarket_snapshot",
            side_effect=ValueError("unexpected_provider_contract"),
        ), self.assertRaisesRegex(ValueError, "unexpected_provider_contract"):
            self.service.collect_us_tech_snapshot_if_open(now=premarket_at)

    def test_premarket_capture_accepts_last_full_minute_before_open(self) -> None:
        premarket_at = datetime(2026, 7, 23, 13, 29, 30, tzinfo=timezone.utc)
        self.manager.provider_at = premarket_at - timedelta(seconds=10)

        result = self.service.collect_us_tech_snapshot_if_open(now=premarket_at)

        self.assertEqual(result["reason"], "us_premarket_snapshot_collected")
        self.assertEqual(
            result["snapshot"]["regular_session_open_at"],
            datetime(2026, 7, 23, 13, 30, tzinfo=timezone.utc).isoformat(),
        )

    def test_premarket_collection_rejects_regular_session_minutes(self) -> None:
        session_open_at = datetime(2026, 7, 23, 13, 30, tzinfo=timezone.utc)
        collected_at = session_open_at + timedelta(seconds=60)

        def premarket_quote(code):
            provider_at = (
                session_open_at
                if code == "INTC"
                else session_open_at - timedelta(seconds=30)
            )
            return UnifiedRealtimeQuote(
                code=code,
                source=RealtimeSource.FALLBACK,
                provider_timestamp=provider_at.isoformat(),
                fetched_at=provider_at.isoformat(),
                market="us",
                currency="USD",
                data_quality="partial",
                price=103.0,
                change_pct=3.0,
                volume=100.0,
            )

        self.manager.get_cross_market_us_premarket_quote_with_provider_timestamp = (
            premarket_quote
        )
        snapshot = self.service.collect_us_premarket_snapshot(
            now=collected_at,
            session_open_at=session_open_at,
        )

        self.assertNotIn("INTC", snapshot["components"])
        self.assertEqual(
            snapshot["component_errors"]["INTC"],
            "provider_timestamp_not_premarket",
        )
        self.assertEqual(
            snapshot["regular_session_open_at"],
            session_open_at.isoformat(),
        )

    def test_scheduled_premarket_uses_calendar_open_across_dst(self) -> None:
        premarket_times = (
            datetime(2026, 7, 23, 13, 25, tzinfo=timezone.utc),
            datetime(2026, 12, 1, 14, 25, tzinfo=timezone.utc),
        )

        for premarket_at in premarket_times:
            with self.subTest(premarket_at=premarket_at):
                self.manager.provider_at = premarket_at - timedelta(seconds=10)
                result = self.service.collect_us_tech_snapshot_if_open(
                    now=premarket_at
                )

                self.assertFalse(result["skipped"])
                self.assertEqual(result["reason"], "us_premarket_snapshot_collected")
                self.assertEqual(
                    result["snapshot"]["regular_session_open_at"],
                    (premarket_at + timedelta(minutes=5)).isoformat(),
                )

    def test_premarket_theme_tolerates_one_missing_leader_but_audits_it(self) -> None:
        self.manager.provider_at = NOW
        self.manager.change_pct = 3.5
        self.manager.missing_code = "INTC"

        snapshot = self.service.collect_us_premarket_snapshot(now=NOW)
        signal = snapshot["theme_signals"]["semiconductor"]

        self.assertTrue(signal["available"])
        self.assertTrue(signal["strong"])
        self.assertIn("INTC", signal["missing_codes"])
        self.assertIn("INTC", snapshot["component_errors"])

    def test_premarket_signal_maps_storage_to_a_share_memory_theme(self) -> None:
        self.manager.provider_at = NOW
        self.manager.change_pct = 3.5
        snapshot = self.service.collect_us_premarket_snapshot(now=NOW)
        expected_session = snapshot["session_date"]

        with patch(
            "src.services.cross_market_signal_service.trading_calendar.get_effective_trading_date",
            return_value=date.fromisoformat(expected_session),
        ):
            signal = self.service.get_us_premarket_signal_for_cn_trade(
                theme="memory",
                now=NOW,
            )

        self.assertTrue(signal["available"])
        self.assertTrue(signal["strong"])
        self.assertEqual(signal["signal_theme"], "storage")
        self.assertEqual(signal["theme"], "memory")

    def test_premarket_signal_keeps_same_session_available_evidence(self) -> None:
        session_date = date(2026, 7, 22)
        available_at = NOW - timedelta(minutes=2)
        unavailable_at = NOW - timedelta(minutes=1)
        self.service._append_snapshot("us_premarket_snapshots", {
            "observed_at": available_at.isoformat(),
            "session_date": session_date.isoformat(),
            "session_stage": "premarket",
            "theme_signals": {
                "storage": {
                    "available": True,
                    "strong": True,
                    "reason": "us_premarket_theme_strong",
                    "score": 82.0,
                },
            },
        })
        self.service._append_snapshot("us_premarket_snapshots", {
            "observed_at": unavailable_at.isoformat(),
            "session_date": session_date.isoformat(),
            "session_stage": "premarket",
            "theme_signals": {
                "storage": {
                    "available": False,
                    "strong": False,
                    "reason": "premarket_theme_coverage_insufficient",
                },
            },
        })

        with patch(
            "src.services.cross_market_signal_service.trading_calendar.get_effective_trading_date",
            return_value=session_date,
        ):
            signal = self.service.get_us_premarket_signal_for_cn_trade(
                theme="memory",
                now=NOW,
            )

        self.assertTrue(signal["available"])
        self.assertTrue(signal["strong"])
        self.assertEqual(signal["observed_at"], available_at.isoformat())
        self.assertEqual(signal["score"], 82.0)

    def test_us_tech_collection_uses_dedicated_timestamped_route(self) -> None:
        collected_at = datetime(2026, 7, 24, 13, 35, tzinfo=timezone.utc)
        strict_calls = []

        def strict_us_quote(code):
            strict_calls.append(code)
            return UnifiedRealtimeQuote(
                code=code,
                source=RealtimeSource.TENCENT,
                provider_timestamp=collected_at.isoformat(),
                fetched_at=collected_at.isoformat(),
                market="us",
                currency="USD",
                data_quality="partial",
                price=100.0,
                change_pct=1.0,
                volume=100.0,
            )

        self.manager.get_cross_market_us_quote_with_provider_timestamp = strict_us_quote
        with patch.object(
            self.manager,
            "get_realtime_quote",
            side_effect=AssertionError("generic route must not be used for US evidence"),
        ):
            snapshot = self.service.collect_us_tech_snapshot(
                now=collected_at,
                session_stage="first_hour",
            )

        self.assertEqual(strict_calls, ["SMH", "SOXX", "MU", "WDC", "IXIC"])
        self.assertEqual(snapshot["session_stage"], "first_hour")
        self.assertEqual(
            {item["source"] for item in snapshot["components"].values()},
            {"tencent"},
        )

    def test_live_scheduled_us_collection_uses_basket_completion_time(self) -> None:
        started_at = datetime(2026, 7, 23, 14, 0, tzinfo=timezone.utc)
        original_get_quote = self.manager.get_realtime_quote

        def progressing_quote(code):
            self.manager.provider_at = started_at + timedelta(
                seconds=len(self.manager.calls) + 1
            )
            return original_get_quote(code)

        self.manager.get_realtime_quote = progressing_quote
        phase = SimpleNamespace(
            is_market_open_now=True,
            market_local_time=started_at.astimezone(timezone(timedelta(hours=-4))),
            to_dict=lambda: {"phase": "intraday", "is_market_open_now": True},
        )
        with patch(
            "src.services.cross_market_signal_service.trading_calendar.build_market_phase_context",
            return_value=phase,
        ), patch(
            "src.services.cross_market_signal_service._utc_now",
            side_effect=[
                started_at,
                started_at,
                started_at + timedelta(seconds=6),
                started_at + timedelta(seconds=6),
            ],
        ):
            result = self.service.collect_us_tech_snapshot_if_open()

        snapshot = result["snapshot"]
        self.assertFalse(result["skipped"])
        self.assertEqual(snapshot["collection_started_at"], started_at.isoformat())
        self.assertEqual(snapshot["collection_duration_seconds"], 6.0)
        self.assertEqual(
            snapshot["observed_at"],
            (started_at + timedelta(seconds=6)).isoformat(),
        )

    def test_us_collection_still_rejects_provider_time_after_completion(self) -> None:
        self.manager.provider_at = NOW + timedelta(seconds=2)

        with self.assertRaisesRegex(ValueError, "us_tech_evidence_from_future"):
            self.service.collect_us_tech_snapshot(now=NOW, session_stage="first_hour")

        self.assertFalse(self.state_path.exists())

    def test_friday_us_signal_remains_available_for_monday_cn_session(self) -> None:
        friday_first_hour = datetime(2026, 7, 24, 14, 30, tzinfo=timezone.utc)
        friday_close = datetime(2026, 7, 24, 20, 0, tzinfo=timezone.utc)
        monday_cn_open = datetime(2026, 7, 27, 1, 35, tzinfo=timezone.utc)
        self.manager.provider_at = friday_first_hour
        self.manager.change_pct = 0.8
        self.service.collect_us_tech_snapshot(
            now=friday_first_hour,
            session_stage="first_hour",
        )
        self.manager.provider_at = friday_close
        self.manager.change_pct = 1.2
        self.service.collect_us_tech_snapshot(
            now=friday_close,
            session_stage="close",
        )

        signal = self.service.get_us_tech_signal_for_cn_trade(now=monday_cn_open)

        self.assertTrue(signal["available"])
        self.assertEqual(signal["session_date"], "2026-07-24")
        self.assertLess(signal["age_hours"], 96)

    def test_friday_us_signal_is_not_reused_for_tuesday_cn_session(self) -> None:
        friday_first_hour = datetime(2026, 7, 24, 14, 30, tzinfo=timezone.utc)
        friday_close = datetime(2026, 7, 24, 20, 0, tzinfo=timezone.utc)
        tuesday_cn_open = datetime(2026, 7, 28, 1, 35, tzinfo=timezone.utc)
        self.manager.provider_at = friday_first_hour
        self.service.collect_us_tech_snapshot(
            now=friday_first_hour,
            session_stage="first_hour",
        )
        self.manager.provider_at = friday_close
        self.service.collect_us_tech_snapshot(
            now=friday_close,
            session_stage="close",
        )

        signal = self.service.get_us_tech_signal_for_cn_trade(now=tuesday_cn_open)

        self.assertFalse(signal["available"])
        self.assertEqual(signal["reason"], "prior_us_close_signal_unavailable")
        self.assertEqual(signal["required_session_date"], "2026-07-27")

    def test_us_close_without_same_session_first_hour_fails_closed(self) -> None:
        us_close = datetime(2026, 7, 23, 20, 0, tzinfo=timezone.utc)
        self.manager.provider_at = us_close
        self.manager.change_pct = 1.5
        self.service.collect_us_tech_snapshot(now=us_close, session_stage="close")

        signal = self.service.get_us_tech_signal_for_cn_trade(
            now=datetime(2026, 7, 24, 1, 30, tzinfo=timezone.utc)
        )

        self.assertFalse(signal["available"])
        self.assertEqual(signal["reason"], "prior_us_first_hour_signal_unavailable")

    def test_scheduled_us_collection_labels_first_hour_and_close(self) -> None:
        first_hour_at = datetime(2026, 7, 23, 14, 0, tzinfo=timezone.utc)
        self.manager.provider_at = first_hour_at
        phase = SimpleNamespace(
            is_market_open_now=True,
            market_local_time=first_hour_at.astimezone(timezone(timedelta(hours=-4))),
            to_dict=lambda: {"phase": "intraday", "is_market_open_now": True},
        )
        with patch(
            "src.services.cross_market_signal_service.trading_calendar.build_market_phase_context",
            return_value=phase,
        ):
            result = self.service.collect_us_tech_snapshot_if_open(now=first_hour_at)

        self.assertEqual(result["snapshot"]["session_stage"], "first_hour")
        self.assertEqual(len(self.manager.calls), 5)

    def test_scheduled_cpo_collection_captures_and_throttles_first_hour(self) -> None:
        first_hour_at = datetime(2026, 7, 23, 14, 0, tzinfo=timezone.utc)
        self.manager.provider_at = first_hour_at
        self.service.intelligence_service = _IntelligenceService([])
        phase = SimpleNamespace(
            is_market_open_now=True,
            market_local_time=first_hour_at.astimezone(timezone(timedelta(hours=-4))),
            to_dict=lambda: {"phase": "intraday", "is_market_open_now": True},
        )
        with patch(
            "src.services.cross_market_signal_service.trading_calendar.build_market_phase_context",
            return_value=phase,
        ):
            result = self.service.collect_cpo_snapshot_if_open(now=first_hour_at)
            throttled = self.service.collect_cpo_snapshot_if_open(
                now=first_hour_at + timedelta(minutes=1)
            )

        self.assertEqual(result["snapshot"]["session_stage"], "first_hour")
        self.assertEqual(throttled["reason"], "cpo_first_hour_snapshot_throttled")
        self.assertEqual(self.manager.calls, ["COHR", "LITE", "CIEN"])

    def test_scheduled_us_close_uses_real_calendar_across_dst(self) -> None:
        close_times = (
            datetime(2026, 7, 23, 20, 0, 30, tzinfo=timezone.utc),
            datetime(2026, 12, 1, 21, 0, 30, tzinfo=timezone.utc),
            datetime(2026, 11, 27, 18, 0, 30, tzinfo=timezone.utc),
        )
        self.service.intelligence_service = _IntelligenceService([])

        for close_at in close_times:
            with self.subTest(close_at=close_at):
                self.manager.provider_at = close_at - timedelta(seconds=10)
                us_result = self.service.collect_us_tech_snapshot_if_open(now=close_at)
                cpo_result = self.service.collect_cpo_snapshot_if_open(now=close_at)

                self.assertFalse(us_result["skipped"])
                self.assertFalse(cpo_result["skipped"])
                self.assertEqual(us_result["snapshot"]["session_stage"], "close")
                self.assertEqual(cpo_result["snapshot"]["session_stage"], "close")
                self.assertEqual(
                    us_result["snapshot"]["session_date"],
                    close_at.date().isoformat(),
                )

    def test_scheduled_us_close_skips_real_calendar_holiday(self) -> None:
        christmas_close = datetime(2026, 12, 25, 21, 0, 30, tzinfo=timezone.utc)
        self.manager.provider_at = christmas_close - timedelta(seconds=10)

        us_result = self.service.collect_us_tech_snapshot_if_open(now=christmas_close)
        cpo_result = self.service.collect_cpo_snapshot_if_open(now=christmas_close)

        self.assertTrue(us_result["skipped"])
        self.assertTrue(cpo_result["skipped"])
        self.assertEqual(us_result["reason"], "outside_us_signal_capture_window")
        self.assertEqual(cpo_result["reason"], "outside_cpo_signal_capture_window")
        self.assertEqual(self.manager.calls, [])

    def test_scheduled_us_and_cpo_collection_freezes_only_after_close(self) -> None:
        before_close = datetime(2026, 7, 23, 19, 59, tzinfo=timezone.utc)
        after_close = datetime(2026, 7, 23, 20, 0, 30, tzinfo=timezone.utc)
        before_phase = SimpleNamespace(
            phase="closing_auction",
            is_trading_day=True,
            is_market_open_now=True,
            session_date="2026-07-23",
            market_local_time=before_close.astimezone(timezone(timedelta(hours=-4))),
            to_dict=lambda: {"phase": "closing_auction", "is_market_open_now": True},
        )
        after_phase = SimpleNamespace(
            phase="postmarket",
            is_trading_day=True,
            is_market_open_now=False,
            session_date="2026-07-23",
            market_local_time=after_close.astimezone(timezone(timedelta(hours=-4))),
            to_dict=lambda: {"phase": "postmarket", "is_market_open_now": False},
        )
        self.manager.provider_at = after_close - timedelta(seconds=30)
        self.service.intelligence_service = _IntelligenceService([])

        with patch(
            "src.services.cross_market_signal_service.trading_calendar.build_market_phase_context",
            return_value=before_phase,
        ):
            early_us = self.service.collect_us_tech_snapshot_if_open(now=before_close)
            early_cpo = self.service.collect_cpo_snapshot_if_open(now=before_close)
        self.assertTrue(early_us["skipped"])
        self.assertTrue(early_cpo["skipped"])
        self.assertEqual(self.manager.calls, [])

        with patch(
            "src.services.cross_market_signal_service.trading_calendar.build_market_phase_context",
            return_value=after_phase,
        ):
            close_us = self.service.collect_us_tech_snapshot_if_open(now=after_close)
            close_cpo = self.service.collect_cpo_snapshot_if_open(now=after_close)
            duplicate_us = self.service.collect_us_tech_snapshot_if_open(
                now=after_close + timedelta(seconds=30)
            )

        full_close_universe = {
            code
            for codes in US_PREMARKET_THEME_CODES.values()
            for code in codes
        }
        self.assertEqual(close_us["snapshot"]["session_stage"], "close")
        self.assertEqual(close_cpo["snapshot"]["session_stage"], "close")
        self.assertEqual(
            close_us["close_theme_snapshot"]["collection_worker_count"],
            min(US_THEME_COLLECTION_MAX_WORKERS, len(full_close_universe)),
        )
        self.assertEqual(duplicate_us["reason"], "us_close_snapshot_already_collected")
        self.assertEqual(len(self.manager.calls), 5 + len(full_close_universe) + 3)
        runtime = self.service.get_runtime_status(now=after_close + timedelta(minutes=1))
        self.assertFalse(runtime["us_tech"]["collection"]["required_stages_complete"])
        self.assertEqual(
            runtime["cpo"]["collection"]["session_stage_counts"],
            {"close": 1},
        )
        self.assertTrue(runtime["cpo"]["collection"]["required_stages_complete"])

    def test_us_close_theme_retries_independently_until_union_is_complete(self) -> None:
        close_at = datetime(2026, 7, 23, 20, 0, 30, tzinfo=timezone.utc)
        phase = SimpleNamespace(
            phase="postmarket",
            is_trading_day=True,
            is_market_open_now=False,
            session_date="2026-07-23",
            market_local_time=close_at.astimezone(timezone(timedelta(hours=-4))),
            to_dict=lambda: {"phase": "postmarket", "is_market_open_now": False},
        )
        self.manager.provider_at = close_at - timedelta(seconds=10)
        signal_themes = list(dict.fromkeys(US_PREMARKET_A_SHARE_THEME_MAP.values()))
        attempts = 0

        def collect_close_theme(*, now=None):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise ValueError("temporary_close_theme_failure")
            available_theme = None if attempts == 2 else "semiconductor_materials"
            theme_signals = {
                signal_theme: {
                    "available": (
                        signal_theme != "semiconductor_materials"
                        if available_theme is None
                        else signal_theme == available_theme
                    ),
                    "strong": False,
                    "reason": "test_close_theme_signal",
                    "score": 1.0,
                    "sector_change_pct": 0.1,
                }
                for signal_theme in signal_themes
            }
            record = {
                "observed_at": now.isoformat(),
                "session_date": "2026-07-23",
                "session_stage": "close",
                "theme_signals": theme_signals,
            }
            self.service._append_snapshot("us_close_theme_snapshots", record)
            return record

        with patch(
            "src.services.cross_market_signal_service.trading_calendar.build_market_phase_context",
            return_value=phase,
        ), patch.object(
            self.service,
            "collect_us_close_theme_snapshot",
            side_effect=collect_close_theme,
        ) as theme_collection:
            with self.assertRaisesRegex(
                ValueError,
                "temporary_close_theme_failure",
            ):
                self.service.collect_us_tech_snapshot_if_open(now=close_at)
            partial = self.service.collect_us_tech_snapshot_if_open(
                now=close_at + timedelta(minutes=1)
            )
            completed = self.service.collect_us_tech_snapshot_if_open(
                now=close_at + timedelta(minutes=2)
            )
            duplicate = self.service.collect_us_tech_snapshot_if_open(
                now=close_at + timedelta(minutes=2, seconds=20)
            )

        self.assertEqual(theme_collection.call_count, 3)
        self.assertEqual(len(self.service._read_state()["us_tech_snapshots"]), 1)
        self.assertIsNone(partial["snapshot"])
        self.assertEqual(partial["reason"], "us_close_theme_snapshot_collected")
        self.assertIsNone(completed["snapshot"])
        self.assertEqual(completed["reason"], "us_close_theme_snapshot_collected")
        self.assertEqual(duplicate["reason"], "us_close_snapshot_already_collected")
        self.assertEqual(len(self.manager.calls), 5)

    def test_empty_us_close_theme_capture_is_retryable_evidence_skip(self) -> None:
        close_at = datetime(2026, 7, 23, 20, 0, 30, tzinfo=timezone.utc)
        phase = SimpleNamespace(
            phase="postmarket",
            is_trading_day=True,
            is_market_open_now=False,
            session_date="2026-07-23",
            market_local_time=close_at.astimezone(
                timezone(timedelta(hours=-4))
            ),
            to_dict=lambda: {
                "phase": "postmarket",
                "is_market_open_now": False,
            },
        )
        self.manager.provider_at = close_at - timedelta(seconds=10)
        recovered_snapshot = {
            "observed_at": (close_at + timedelta(minutes=1)).isoformat(),
            "session_date": "2026-07-23",
            "session_stage": "close",
            "theme_signals": {},
        }

        with patch(
            "src.services.cross_market_signal_service.trading_calendar.build_market_phase_context",
            return_value=phase,
        ), patch.object(
            self.service,
            "collect_us_close_theme_snapshot",
            side_effect=[
                ValueError("us_close_theme_evidence_unavailable"),
                recovered_snapshot,
            ],
        ) as collect_close_theme:
            first = self.service.collect_us_tech_snapshot_if_open(now=close_at)
            second = self.service.collect_us_tech_snapshot_if_open(
                now=close_at + timedelta(minutes=1)
            )

        self.assertFalse(first["accepted"])
        self.assertTrue(first["skipped"])
        self.assertEqual(first["reason"], "us_close_theme_evidence_unavailable")
        self.assertFalse(first["evidence_ready"])
        self.assertTrue(first["retryable"])
        self.assertEqual(first["retry_after_seconds"], 30)
        self.assertIsNotNone(first["snapshot"])
        self.assertEqual(second["reason"], "us_close_theme_snapshot_collected")
        self.assertIsNone(second["snapshot"])
        self.assertEqual(collect_close_theme.call_count, 2)
        self.assertEqual(len(self.service._read_state()["us_tech_snapshots"]), 1)

    def test_close_theme_reader_keeps_earlier_available_retry_evidence(self) -> None:
        close_at = datetime(2026, 7, 23, 20, 0, 30, tzinfo=timezone.utc)
        signal_themes = list(dict.fromkeys(US_PREMARKET_A_SHARE_THEME_MAP.values()))
        first_signals = {
            signal_theme: {
                "available": signal_theme != "semiconductor_materials",
                "strong": False,
                "reason": "first_close_attempt",
                "score": 1.0,
                "sector_change_pct": 0.1,
            }
            for signal_theme in signal_themes
        }
        second_signals = {
            signal_theme: {
                "available": signal_theme == "semiconductor_materials",
                "strong": False,
                "reason": "second_close_attempt",
                "score": 1.0,
                "sector_change_pct": 0.1,
            }
            for signal_theme in signal_themes
        }
        for observed_at, signals in (
            (close_at, first_signals),
            (close_at + timedelta(minutes=1), second_signals),
        ):
            self.service._append_snapshot("us_close_theme_snapshots", {
                "observed_at": observed_at.isoformat(),
                "session_date": "2026-07-23",
                "session_stage": "close",
                "theme_signals": signals,
            })

        state = self.service._read_state()
        self.assertTrue(self.service._us_close_theme_capture_complete(
            state,
            session_date="2026-07-23",
            current=close_at + timedelta(minutes=2),
        ))
        with patch(
            "src.services.cross_market_signal_service.trading_calendar.get_effective_trading_date",
            return_value=date(2026, 7, 23),
        ):
            semiconductor = self.service.get_us_close_theme_signal_for_cn_trade(
                theme="semiconductor",
                now=datetime(2026, 7, 24, 1, 35, tzinfo=timezone.utc),
            )
            materials = self.service.get_us_close_theme_signal_for_cn_trade(
                theme="materials",
                now=datetime(2026, 7, 24, 1, 35, tzinfo=timezone.utc),
            )

        self.assertTrue(semiconductor["available"])
        self.assertEqual(semiconductor["reason"], "first_close_attempt")
        self.assertEqual(semiconductor["observed_at"], close_at.isoformat())
        self.assertTrue(materials["available"])
        self.assertEqual(materials["reason"], "second_close_attempt")
        self.assertEqual(
            materials["observed_at"],
            (close_at + timedelta(minutes=1)).isoformat(),
        )

    def test_cpo_signal_combines_optional_us_optical_and_news_without_korea(self) -> None:
        us_close = datetime(2026, 7, 23, 20, 0, tzinfo=timezone.utc)
        self.manager.provider_at = us_close
        self.manager.change_pct = 1.2
        self.service.intelligence_service = _IntelligenceService(
            [{
                "title": "1.6T optical module demand expands for CPO systems",
                "published_at": us_close - timedelta(hours=1),
            }]
        )

        snapshot = self.service.collect_cpo_snapshot(now=us_close, session_stage="close")
        signal = self.service.get_cpo_signal_for_cn_trade(
            now=datetime(2026, 7, 24, 1, 30, tzinfo=timezone.utc)
        )

        self.assertGreater(snapshot["score"], 30.0)
        self.assertEqual(snapshot["news"]["positive_count"], 1)
        self.assertTrue(signal["supportive"])
        self.assertEqual(self.manager.calls, ["COHR", "LITE", "CIEN"])

        stale_signal = self.service.get_cpo_signal_for_cn_trade(
            now=datetime(2026, 7, 28, 1, 30, tzinfo=timezone.utc)
        )
        self.assertFalse(stale_signal["available"])
        self.assertEqual(stale_signal["required_session_date"], "2026-07-27")

    def test_cpo_signal_degrades_when_one_optional_us_component_is_missing(self) -> None:
        us_close = datetime(2026, 7, 23, 20, 0, tzinfo=timezone.utc)
        self.manager.provider_at = us_close
        self.manager.change_pct = 1.2
        self.manager.missing_code = "LITE"
        self.service.intelligence_service = _IntelligenceService([])

        snapshot = self.service.collect_cpo_snapshot(now=us_close, session_stage="close")

        self.assertTrue(snapshot["evidence_available"])
        self.assertEqual(set(snapshot["components"]), {"COHR", "CIEN"})
        self.assertIn("LITE", snapshot["component_errors"])

    def test_live_scheduled_cpo_collection_uses_basket_completion_time(self) -> None:
        started_at = datetime(2026, 7, 23, 20, 0, 30, tzinfo=timezone.utc)
        original_get_quote = self.manager.get_realtime_quote

        def progressing_quote(code):
            self.manager.provider_at = started_at + timedelta(
                seconds=len(self.manager.calls) + 1
            )
            return original_get_quote(code)

        self.manager.get_realtime_quote = progressing_quote
        self.service.intelligence_service = _IntelligenceService([])
        phase = SimpleNamespace(
            phase="postmarket",
            is_trading_day=True,
            is_market_open_now=False,
            session_date="2026-07-23",
            market_local_time=started_at.astimezone(timezone(timedelta(hours=-4))),
            to_dict=lambda: {"phase": "postmarket", "is_market_open_now": False},
        )
        with patch(
            "src.services.cross_market_signal_service.trading_calendar.build_market_phase_context",
            return_value=phase,
        ), patch(
            "src.services.cross_market_signal_service._utc_now",
            side_effect=[
                started_at,
                started_at,
                started_at + timedelta(seconds=4),
                started_at + timedelta(seconds=4),
            ],
        ):
            result = self.service.collect_cpo_snapshot_if_open()

        snapshot = result["snapshot"]
        self.assertFalse(result["skipped"])
        self.assertEqual(set(snapshot["components"]), {"COHR", "LITE", "CIEN"})
        self.assertEqual(snapshot["component_errors"], {})
        self.assertEqual(snapshot["collection_started_at"], started_at.isoformat())
        self.assertEqual(snapshot["collection_duration_seconds"], 4.0)
        self.assertEqual(
            snapshot["observed_at"],
            (started_at + timedelta(seconds=4)).isoformat(),
        )

    def test_cpo_collection_rejects_components_from_after_completion(self) -> None:
        self.manager.provider_at = NOW + timedelta(seconds=2)
        self.service.intelligence_service = _IntelligenceService([])

        snapshot = self.service.collect_cpo_snapshot(now=NOW, session_stage="close")

        self.assertEqual(snapshot["components"], {})
        self.assertEqual(
            snapshot["component_errors"],
            {
                "COHR": "evidence_from_future",
                "LITE": "evidence_from_future",
                "CIEN": "evidence_from_future",
            },
        )

    def test_cpo_news_remains_usable_without_optional_us_quotes(self) -> None:
        us_close = datetime(2026, 7, 23, 20, 0, tzinfo=timezone.utc)
        self.service.intelligence_service = _IntelligenceService([{
            "title": "1.6T optical module demand expands for CPO systems",
            "published_at": us_close - timedelta(hours=1),
        }])

        with patch.object(
            self.manager,
            "get_realtime_quote",
            return_value=None,
        ):
            snapshot = self.service.collect_cpo_snapshot(
                now=us_close,
                session_stage="close",
            )

        self.assertTrue(snapshot["evidence_available"])
        self.assertIsNone(snapshot["market_score"])
        self.assertEqual(snapshot["score"], 30.0)

    def test_gold_signal_combines_fresh_gc_trend_and_bounded_news(self) -> None:
        gold_at = datetime(2026, 7, 23, 1, 15, tzinfo=timezone.utc)
        self.manager.provider_at = gold_at
        self.manager.change_pct = 1.0
        self.service.intelligence_service = _IntelligenceService(
            [{
                "title": "Fed officials open door to rate cut",
                "published_at": gold_at - timedelta(hours=2),
            }]
        )

        result = self.service.collect_gold_snapshot_if_window(now=gold_at)
        signal = self.service.get_gold_signal_for_cn_trade(now=gold_at)
        before = self.service.get_gold_signal_for_cn_trade(
            now=gold_at - timedelta(minutes=1)
        )

        self.assertFalse(result["skipped"])
        self.assertTrue(signal["available"])
        self.assertTrue(signal["buy_allowed"])
        self.assertEqual(signal["snapshot"]["news"]["positive_count"], 1)
        collection = signal["snapshot"]["news"]["collection"]
        self.assertEqual(collection["official_source_success_count"], 2)
        self.assertEqual(collection["supplemental_source_success_count"], 2)
        self.assertTrue(collection["window_coverage_complete"])
        self.assertEqual(signal["snapshot"]["quote_source"], "fallback")
        self.assertTrue(signal["snapshot"]["news_window_complete"])
        self.assertTrue(signal["news_window_complete"])
        self.assertFalse(before["available"])

    def test_gold_signal_fails_closed_when_all_live_macro_sources_fail(self) -> None:
        gold_at = datetime(2026, 7, 23, 1, 15, tzinfo=timezone.utc)
        self.manager.provider_at = gold_at
        intelligence = _IntelligenceService([])
        intelligence.failed_templates = {
            "federal-reserve-monetary-policy",
            "federal-reserve-speeches-testimony",
            "newsnow-jin10",
            "newsnow-wallstreetcn-quick",
        }
        self.service.intelligence_service = intelligence

        with self.assertRaisesRegex(ValueError, "gold_news_evidence_unavailable"):
            self.service.collect_gold_snapshot(now=gold_at)

        self.assertEqual(len(intelligence.template_calls), 4)

    def test_gold_signal_fails_closed_when_supplemental_feeds_are_empty(self) -> None:
        gold_at = datetime(2026, 7, 23, 1, 15, tzinfo=timezone.utc)
        self.manager.provider_at = gold_at
        intelligence = _IntelligenceService([], default_published_at=gold_at)
        intelligence.empty_templates = {
            "newsnow-jin10",
            "newsnow-wallstreetcn-quick",
        }
        self.service.intelligence_service = intelligence

        with self.assertRaisesRegex(ValueError, "gold_news_evidence_unavailable"):
            self.service.collect_gold_snapshot(now=gold_at)

    def test_gold_signal_fails_closed_when_official_feed_is_empty(self) -> None:
        gold_at = datetime(2026, 7, 23, 1, 15, tzinfo=timezone.utc)
        self.manager.provider_at = gold_at
        intelligence = _IntelligenceService([], default_published_at=gold_at)
        intelligence.empty_templates = {"federal-reserve-monetary-policy"}
        self.service.intelligence_service = intelligence

        with self.assertRaisesRegex(
            ValueError,
            "gold_news_window_coverage_incomplete",
        ):
            self.service.collect_gold_snapshot(now=gold_at)

    def test_gold_final_snapshot_keeps_rate_news_captured_earlier_in_window(self) -> None:
        preview_at = datetime(2026, 7, 23, 1, 0, tzinfo=timezone.utc)
        final_at = datetime(2026, 7, 23, 1, 15, tzinfo=timezone.utc)
        intelligence = _IntelligenceService([{
            "title": "Fed officials open door to rate cut",
            "published_at": preview_at - timedelta(minutes=5),
        }])
        self.service.intelligence_service = intelligence
        self.manager.provider_at = preview_at

        preview = self.service.collect_gold_snapshot(now=preview_at)
        intelligence.items = []
        intelligence.default_published_at = final_at
        self.manager.provider_at = final_at
        final = self.service.collect_gold_snapshot(now=final_at)

        self.assertEqual(preview["news"]["positive_count"], 1)
        self.assertEqual(final["news"]["positive_count"], 1)
        self.assertEqual(final["news"]["score"], 35.0)
        self.assertEqual(
            final["news"]["collection"]["captured_forward_matched_item_count"],
            1,
        )
        self.assertEqual(
            final["news"]["collection"]["window_item_retention_mode"],
            "same_session_forward_gold_snapshots",
        )

    def test_gold_signal_rejects_pre_cutoff_news_snapshot_after_0915(self) -> None:
        collected_at = datetime(2026, 7, 23, 1, 0, tzinfo=timezone.utc)
        decision_at = datetime(2026, 7, 23, 1, 35, tzinfo=timezone.utc)
        self.manager.provider_at = collected_at
        self.manager.change_pct = 1.0
        self.service.intelligence_service = _IntelligenceService(
            [],
            default_published_at=collected_at,
        )

        snapshot = self.service.collect_gold_snapshot(now=collected_at)
        preview = self.service.get_gold_signal_for_cn_trade(now=collected_at)
        formal = self.service.get_gold_signal_for_cn_trade(now=decision_at)

        self.assertFalse(snapshot["news_window_complete"])
        self.assertTrue(preview["available"])
        self.assertFalse(preview["news_window_complete"])
        self.assertFalse(formal["available"])
        self.assertFalse(formal["news_window_complete"])
        self.assertEqual(formal["reason"], "gold_news_window_incomplete")
        self.assertEqual(
            formal["news_window_cutoff_at"],
            "2026-07-23T01:15:00+00:00",
        )

    def test_live_scheduled_gold_collection_uses_completion_time(self) -> None:
        started_at = datetime(2026, 7, 23, 1, 0, 0, tzinfo=timezone.utc)
        completed_at = started_at + timedelta(seconds=3)
        self.manager.provider_at = started_at + timedelta(seconds=2)
        self.manager.change_pct = 1.0
        self.service.intelligence_service = _IntelligenceService(
            [],
            default_published_at=started_at,
        )

        with patch.object(
            self.service,
            "_cn_collection_phase_gate",
            return_value=None,
        ), patch(
            "src.services.cross_market_signal_service._utc_now",
            side_effect=[
                started_at,
                started_at,
                completed_at,
                completed_at,
            ],
        ):
            result = self.service.collect_gold_snapshot_if_window()

        snapshot = result["snapshot"]
        self.assertFalse(result["skipped"])
        self.assertEqual(snapshot["observed_at"], completed_at.isoformat())
        self.assertEqual(
            snapshot["collection_started_at"],
            started_at.isoformat(),
        )
        self.assertEqual(snapshot["collection_duration_seconds"], 3.0)
        self.assertEqual(
            snapshot["provider_timestamp"],
            (started_at + timedelta(seconds=2)).isoformat(),
        )

    def test_gold_trend_includes_latest_completed_new_york_settlement(self) -> None:
        gold_at = datetime(2026, 7, 23, 1, 15, tzinfo=timezone.utc)
        self.manager.provider_at = gold_at
        dates = pd.date_range(end="2026-07-22", periods=25, freq="D")
        closes = [80.0 + index for index in range(25)]
        frame = pd.DataFrame({"date": dates, "close": closes})
        self.service.intelligence_service = _IntelligenceService(
            [],
            default_published_at=gold_at,
        )

        with patch.object(
            self.manager,
            "get_daily_data",
            return_value=(frame, "unit-test"),
        ):
            snapshot = self.service.collect_gold_snapshot(now=gold_at)

        self.assertEqual(snapshot["trend_through_date"], "2026-07-22")
        self.assertAlmostEqual(snapshot["ma5"], sum(closes[-5:]) / 5.0)
        self.assertAlmostEqual(
            snapshot["five_day_return_pct"],
            (closes[-1] / closes[-6] - 1.0) * 100.0,
            places=6,
        )

    def test_gold_news_window_starts_at_previous_completed_cn_session(self) -> None:
        current = datetime(2026, 7, 2, 1, 10, tzinfo=timezone.utc)

        with patch(
            "src.services.cross_market_signal_service.trading_calendar.get_effective_trading_date",
            return_value=date(2026, 6, 30),
        ):
            start, end = self.service._gold_news_window(current)

        shanghai = timezone(timedelta(hours=8))
        self.assertEqual(start.astimezone(shanghai).isoformat(), "2026-06-30T15:00:00+08:00")
        self.assertEqual(end.astimezone(shanghai).isoformat(), "2026-07-02T09:10:00+08:00")

    def test_gold_news_window_fails_closed_when_calendar_is_unavailable(self) -> None:
        current = datetime(2026, 7, 2, 1, 10, tzinfo=timezone.utc)

        with patch(
            "src.services.cross_market_signal_service.trading_calendar."
            "get_effective_trading_date",
            side_effect=RuntimeError("calendar unavailable"),
        ) as effective_date, self.assertRaisesRegex(
            RuntimeError,
            "calendar unavailable",
        ):
            self.service._gold_news_window(current)

        effective_date.assert_called_once_with(
            "cn",
            current_time=current,
            strict=True,
        )

    def test_gold_news_query_covers_entire_long_holiday_window(self) -> None:
        current = datetime(2026, 10, 9, 1, 10, tzinfo=timezone.utc)
        self.manager.provider_at = current
        intelligence = _IntelligenceService([], default_published_at=current)
        self.service.intelligence_service = intelligence

        with patch(
            "src.services.cross_market_signal_service.trading_calendar.get_effective_trading_date",
            return_value=date(2026, 9, 30),
        ):
            snapshot = self.service.collect_gold_snapshot(now=current)

        self.assertEqual(snapshot["news"]["window_start"], "2026-09-30T07:00:00+00:00")
        self.assertEqual(intelligence.calls[0]["days"], 10)

    def test_gold_collection_skips_outside_preopen_window_without_network(self) -> None:
        result = self.service.collect_gold_snapshot_if_window(now=NOW)

        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "outside_gold_signal_window")
        self.assertEqual(self.manager.calls, [])

    def test_cn_collectors_skip_non_trading_day_without_network(self) -> None:
        non_trading_windows = (
            (
                datetime(2026, 7, 25, 1, 0, tzinfo=timezone.utc),
                datetime(2026, 7, 25, 1, 30, tzinfo=timezone.utc),
            ),
            (
                datetime(2026, 10, 1, 1, 0, tzinfo=timezone.utc),
                datetime(2026, 10, 1, 1, 30, tzinfo=timezone.utc),
            ),
        )

        for gold_window, open_window in non_trading_windows:
            with self.subTest(session_date=gold_window.date()):
                gold = self.service.collect_gold_snapshot_if_window(
                    now=gold_window,
                )
                cn_open = self.service.collect_cn_open_snapshot_if_window(
                    now=open_window,
                )

                self.assertTrue(gold["skipped"])
                self.assertTrue(cn_open["skipped"])
                self.assertEqual(gold["reason"], "non_cn_trading_day")
                self.assertEqual(cn_open["reason"], "non_cn_trading_day")
        self.assertEqual(self.manager.calls, [])

    def test_cn_collectors_fail_closed_when_calendar_is_unavailable(self) -> None:
        gold_window = datetime(2026, 7, 24, 1, 0, tzinfo=timezone.utc)
        open_window = datetime(2026, 7, 24, 1, 30, tzinfo=timezone.utc)

        with patch(
            "src.services.cross_market_signal_service.trading_calendar."
            "build_market_phase_context",
            side_effect=RuntimeError("calendar down"),
        ):
            gold = self.service.collect_gold_snapshot_if_window(now=gold_window)
            cn_open = self.service.collect_cn_open_snapshot_if_window(now=open_window)

        self.assertFalse(gold["accepted"])
        self.assertFalse(cn_open["accepted"])
        self.assertEqual(gold["reason"], "cn_collection_calendar_unavailable")
        self.assertEqual(cn_open["reason"], "cn_collection_calendar_unavailable")
        self.assertEqual(self.manager.calls, [])

    def test_cn_open_collection_skips_call_auction_without_network(self) -> None:
        call_auction = datetime(2026, 7, 24, 1, 25, tzinfo=timezone.utc)

        result = self.service.collect_cn_open_snapshot_if_window(now=call_auction)

        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "outside_cn_open_signal_window")
        self.assertEqual(self.manager.calls, [])

    def test_cn_open_snapshot_rejects_provider_time_before_session_open(self) -> None:
        current = datetime(2026, 7, 24, 1, 30, 30, tzinfo=timezone.utc)
        self.manager.provider_at = datetime(
            2026,
            7,
            24,
            1,
            29,
            59,
            tzinfo=timezone.utc,
        )

        with self.assertRaisesRegex(
            ValueError,
            "cn_open_evidence_before_session_open",
        ):
            self.service.collect_cn_open_snapshot(now=current)

        self.assertEqual(
            self.service._read_state().get("cn_open_snapshots", []),
            [],
        )

    def test_live_scheduled_cn_open_collection_uses_completion_time(self) -> None:
        started_at = datetime(2026, 7, 23, 1, 30, 0, tzinfo=timezone.utc)
        completed_at = started_at + timedelta(seconds=3)
        self.manager.provider_at = started_at + timedelta(seconds=2)
        session_close = datetime(2026, 7, 23, 7, 0, tzinfo=timezone.utc)

        with patch.object(
            self.service,
            "_cn_collection_phase_gate",
            return_value=None,
        ), patch(
            "src.services.cross_market_signal_service.trading_calendar."
            "get_market_session_bounds",
            return_value=(started_at, session_close),
        ), patch(
            "src.services.cross_market_signal_service._utc_now",
            side_effect=[
                started_at,
                started_at,
                completed_at,
                completed_at,
            ],
        ):
            result = self.service.collect_cn_open_snapshot_if_window()

        snapshot = result["snapshot"]
        self.assertFalse(result["skipped"])
        self.assertEqual(snapshot["observed_at"], completed_at.isoformat())
        self.assertEqual(
            snapshot["collection_started_at"],
            started_at.isoformat(),
        )
        self.assertEqual(snapshot["collection_duration_seconds"], 3.0)
        self.assertEqual(
            snapshot["provider_timestamp"],
            (started_at + timedelta(seconds=2)).isoformat(),
        )

    def test_cn_open_snapshot_freezes_high_open_force_sell(self) -> None:
        cn_open = datetime(2026, 7, 23, 1, 30, tzinfo=timezone.utc)
        self.manager.provider_at = cn_open
        self.manager.open_price = 100.6
        self.manager.pre_close = 100.0
        self.manager.price = 100.8
        self.manager.amount = 10080.0
        self.manager.volume = 100.0

        result = self.service.collect_cn_open_snapshot_if_window(now=cn_open)
        signal = self.service.get_cn_open_signal(now=cn_open)
        before = self.service.get_cn_open_signal(now=cn_open - timedelta(seconds=1))

        self.assertFalse(result["skipped"])
        self.assertEqual(signal["regime"], "high_open")
        self.assertTrue(signal["force_sell"])
        self.assertFalse(signal["buy_allowed"])
        self.assertEqual(signal["session_date"], "2026-07-23")
        self.assertEqual(
            signal["classification_thresholds"]["high_open_pct"],
            0.19,
        )
        self.assertEqual(signal["classification_source"], "current_strategy_config")
        self.assertFalse(signal["classification_changed"])
        self.assertIsNone(signal["snapshot"]["vwap"])
        self.assertFalse(signal["snapshot"]["vwap_available"])
        self.assertEqual(
            signal["snapshot"]["vwap_reason"],
            "index_point_vwap_unavailable",
        )
        self.assertFalse(signal["snapshot"]["above_vwap"])
        self.assertEqual(
            signal["snapshot"]["session_open_at"],
            "2026-07-23T01:30:00+00:00",
        )
        self.assertFalse(before["available"])

    def test_cn_open_signal_reclassifies_frozen_gap_with_current_threshold(
        self,
    ) -> None:
        cn_open = datetime(2026, 7, 23, 1, 30, tzinfo=timezone.utc)
        self.service._append_snapshot("cn_open_snapshots", {
            "observed_at": cn_open.isoformat(),
            "cn_session_date": "2026-07-23",
            "gap_pct": 0.3,
            "classification": {
                "regime": "flat_open",
                "buy_allowed": False,
                "force_sell": False,
            },
            "classification_thresholds": {
                "high_open_pct": 0.5,
                "low_open_upper_pct": -0.3,
                "extreme_low_open_pct": -1.5,
            },
        })

        signal = self.service.get_cn_open_signal(now=cn_open)

        self.assertTrue(signal["available"])
        self.assertEqual(signal["gap_pct"], 0.3)
        self.assertEqual(signal["regime"], "high_open")
        self.assertEqual(signal["session_date"], "2026-07-23")
        self.assertFalse(signal["buy_allowed"])
        self.assertTrue(signal["force_sell"])
        self.assertTrue(signal["classification_changed"])
        self.assertEqual(
            signal["recorded_classification"]["regime"],
            "flat_open",
        )
        self.assertEqual(
            signal["classification_thresholds"]["high_open_pct"],
            0.19,
        )


if __name__ == "__main__":
    unittest.main()
