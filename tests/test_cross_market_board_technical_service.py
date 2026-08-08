"""Tests for point-in-time A-share board support and resistance evidence."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

import pandas as pd

from src.services.cross_market_board_technical_service import (
    CrossMarketBoardTechnicalService,
)


OBSERVED_AT = datetime(2026, 7, 31, 2, 40, tzinfo=timezone.utc)


def _history(*, high: float = 110.0, low: float = 90.0) -> pd.DataFrame:
    dates = pd.date_range(end="2026-07-31", periods=70, freq="D")
    return pd.DataFrame({
        "日期": dates,
        "收盘": [100.0] * len(dates),
        "最高": [high] * len(dates),
        "最低": [low] * len(dates),
    })


class CrossMarketBoardTechnicalServiceTestCase(unittest.TestCase):
    def test_uses_only_completed_bars_and_emits_all_period_support_alerts(self) -> None:
        frame = _history()
        frame.loc[frame.index[-1], ["收盘", "最高", "最低"]] = [300.0, 310.0, 290.0]
        service = CrossMarketBoardTechnicalService(
            history_loader=lambda *_args: frame,
        )

        result = service.analyze_board(
            name="半导体",
            identifier="BK1036",
            board_type="concept",
            live_change_pct=0.0,
            observed_at=OBSERVED_AT,
        )

        self.assertTrue(result["available"])
        self.assertEqual(result["history_through_date"], "2026-07-30")
        self.assertEqual(result["current_level"], 100.0)
        self.assertEqual(result["technical_windows"], [5, 10, 20, 30, 60])
        self.assertEqual(result["ma5"], 100.0)
        self.assertEqual(result["ma10"], 100.0)
        self.assertEqual(result["ma20"], 100.0)
        self.assertEqual(result["ma30"], 100.0)
        self.assertEqual(result["ma60"], 100.0)
        self.assertTrue(result["supportive"])
        self.assertEqual(result["support_score"], 100.0)
        self.assertEqual(result["support_windows"], [5, 10, 20, 30, 60])
        self.assertTrue(result["multi_period_support"])
        self.assertEqual(
            {alert["kind"] for alert in result["alerts"]},
            {
                "near_5d_ma_support",
                "near_10d_ma_support",
                "near_20d_ma_support",
                "near_30d_ma_support",
                "near_60d_ma_support",
            },
        )

    def test_short_period_support_participates_with_lower_weight(self) -> None:
        frame = _history(high=140.0, low=80.0)
        frame["收盘"] = 120.0
        frame.loc[frame.index[-7:-1], "收盘"] = 100.0
        service = CrossMarketBoardTechnicalService(
            history_loader=lambda *_args: frame,
        )

        result = service.analyze_board(
            name="人工智能",
            identifier="BK0800",
            board_type="concept",
            live_change_pct=0.0,
            observed_at=OBSERVED_AT,
        )

        self.assertTrue(result["supportive"])
        self.assertEqual(result["near_ma_support_windows"], [5])
        self.assertEqual(result["support_windows"], [5])
        self.assertEqual(result["support_score"], 65.0)
        self.assertFalse(result["multi_period_support"])

    def test_short_period_resistance_is_included_in_chase_block(self) -> None:
        frame = _history(high=120.0, low=80.0)
        frame.loc[frame.index[-6:-1], "最高"] = 102.0
        service = CrossMarketBoardTechnicalService(
            history_loader=lambda *_args: frame,
        )

        result = service.analyze_board(
            name="算力",
            identifier="BK0735",
            board_type="concept",
            live_change_pct=0.5,
            observed_at=OBSERVED_AT,
        )

        self.assertTrue(result["near_resistance"])
        self.assertEqual(result["nearest_resistance"], 102.0)
        self.assertEqual(result["nearest_resistance_window"], 5)
        self.assertEqual(result["alerts"][-1]["window_days"], 5)

    def test_near_resistance_blocks_chasing_but_confirmed_breakout_clears_it(self) -> None:
        frame = _history(high=102.0)
        intraday = pd.DataFrame({
            "datetime": ["2026-07-31 10:35:00", "2026-07-31 10:40:00"],
            "close": [103.2, 103.5],
        })
        service = CrossMarketBoardTechnicalService(
            history_loader=lambda *_args: frame,
            intraday_loader=lambda *_args: intraday,
        )

        approaching = service.analyze_board(
            name="科技",
            identifier="BK1000",
            board_type="concept",
            live_change_pct=0.5,
            observed_at=OBSERVED_AT,
        )
        breakout = service.analyze_board(
            name="科技",
            identifier="BK1000",
            board_type="concept",
            live_change_pct=4.0,
            live_volume_ratio=1.6,
            observed_at=OBSERVED_AT,
        )

        self.assertTrue(approaching["near_resistance"])
        self.assertFalse(approaching["breakout_confirmed"])
        self.assertIn(
            "near_board_resistance",
            {alert["kind"] for alert in approaching["alerts"]},
        )
        self.assertFalse(breakout["near_resistance"])
        self.assertTrue(breakout["breakout_confirmed"])
        self.assertEqual(
            breakout["breakout_confirmation"]["reason"],
            "breakout_volume_and_5m_closes_confirmed",
        )

    def test_breakout_without_volume_or_two_consecutive_closes_stays_blocked(self) -> None:
        frame = _history(high=102.0)
        nonconsecutive = pd.DataFrame({
            "datetime": ["2026-07-31 10:30:00", "2026-07-31 10:40:00"],
            "close": [103.2, 103.5],
        })
        service = CrossMarketBoardTechnicalService(
            history_loader=lambda *_args: frame,
            intraday_loader=lambda *_args: nonconsecutive,
        )

        weak_volume = service.analyze_board(
            name="科技",
            identifier="BK1000",
            board_type="concept",
            live_change_pct=4.0,
            live_volume_ratio=1.4,
            observed_at=OBSERVED_AT,
        )
        broken_sequence = service.analyze_board(
            name="科技",
            identifier="BK1000",
            board_type="concept",
            live_change_pct=4.0,
            live_volume_ratio=1.6,
            observed_at=OBSERVED_AT,
        )

        self.assertTrue(weak_volume["near_resistance"])
        self.assertFalse(weak_volume["breakout_confirmed"])
        self.assertEqual(
            weak_volume["breakout_confirmation"]["reason"],
            "breakout_volume_ratio_unconfirmed",
        )
        self.assertTrue(broken_sequence["near_resistance"])
        self.assertFalse(broken_sequence["breakout_confirmed"])
        self.assertEqual(
            broken_sequence["breakout_confirmation"]["reason"],
            "breakout_5m_closes_not_consecutive",
        )

    def test_support_must_be_below_price_and_falling_ma_is_not_support(self) -> None:
        below_service = CrossMarketBoardTechnicalService(
            history_loader=lambda *_args: _history(high=140.0, low=80.0),
        )
        below = below_service.analyze_board(
            name="半导体",
            identifier="BK1036",
            board_type="concept",
            live_change_pct=-1.0,
            observed_at=OBSERVED_AT,
        )

        falling_frame = _history(high=140.0, low=80.0)
        falling_frame["收盘"] = 120.0
        falling_frame.loc[falling_frame.index[-7:-1], "收盘"] = [
            106.0,
            105.0,
            104.0,
            103.0,
            102.0,
            101.0,
        ]
        falling_service = CrossMarketBoardTechnicalService(
            history_loader=lambda *_args: falling_frame,
        )
        falling = falling_service.analyze_board(
            name="人工智能",
            identifier="BK0800",
            board_type="concept",
            live_change_pct=2.0,
            observed_at=OBSERVED_AT,
        )

        self.assertFalse(below["supportive"])
        self.assertIn(5, below["below_support_windows"])
        self.assertFalse(falling["supportive"])
        self.assertIn(5, falling["falling_ma_support_windows"])

    def test_rotation_tailwind_identifies_liquor_and_bank_pressure(self) -> None:
        service = CrossMarketBoardTechnicalService(
            history_loader=lambda *_args: _history(),
        )
        references = [
            {"rotation_group": "liquor", "near_resistance": True},
            {"rotation_group": "bank", "near_resistance": True},
        ]

        signal = service.build_rotation_signal(references)

        self.assertTrue(signal["available"])
        self.assertTrue(signal["tailwind"])
        self.assertEqual(signal["pressure_groups"], ["bank", "liquor"])
        self.assertEqual(service.rotation_reference_group("白酒概念"), "liquor")
        self.assertEqual(service.rotation_reference_group("银行"), "bank")

    def test_board_collection_degrades_per_board_without_losing_valid_evidence(self) -> None:
        def loader(identifier, *_args):
            if identifier == "BAD":
                raise RuntimeError("unavailable")
            return _history()

        service = CrossMarketBoardTechnicalService(history_loader=loader)
        result = service.analyze_boards(
            [
                {"name": "半导体", "code": "GOOD", "change_pct": 0.0},
                {"name": "芯片", "code": "BAD", "change_pct": 0.0},
            ],
            observed_at=OBSERVED_AT,
        )

        self.assertTrue(result["available"])
        self.assertEqual(result["primary_board"]["identifier"], "GOOD")
        self.assertEqual(result["errors"], [{"name": "芯片", "reason": "RuntimeError"}])

    def test_any_relevant_board_pressure_blocks_even_when_primary_board_is_supported(self) -> None:
        supported = _history(high=110.0)
        pressured = _history(high=102.0, low=80.0)
        pressured["收盘"] = 90.0
        pressured.loc[pressured.index[-2], "收盘"] = 100.0

        def loader(identifier, *_args):
            return supported if identifier == "SUPPORTED" else pressured

        service = CrossMarketBoardTechnicalService(history_loader=loader)
        result = service.analyze_boards(
            [
                {
                    "name": "半导体",
                    "code": "SUPPORTED",
                    "change_pct": 0.0,
                },
                {
                    "name": "先进封装",
                    "code": "PRESSURED",
                    "change_pct": 0.5,
                },
            ],
            observed_at=OBSERVED_AT,
        )

        self.assertEqual(result["primary_board"]["identifier"], "SUPPORTED")
        self.assertTrue(result["supportive"])
        self.assertTrue(result["near_resistance"])
        self.assertFalse(result["breakout_confirmed"])
        self.assertEqual(result["reason"], "board_near_resistance")
        self.assertEqual(result["pressure_boards"], ["先进封装"])


if __name__ == "__main__":
    unittest.main()
