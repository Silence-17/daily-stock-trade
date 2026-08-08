# -*- coding: utf-8 -*-
"""500-session replay tests for the cross-market paper strategy."""

from __future__ import annotations

import unittest
from datetime import date, datetime, time, timedelta, timezone

from src.services.cross_market_backtest_service import (
    CrossMarketBacktestService,
    ReplayFrame,
    _ReplayLot,
    _ReplayPosition,
)
from src.services.cross_market_paper_strategy import MinuteBar


def _trading_dates(start: date, count: int) -> list[date]:
    values = []
    current = start
    while len(values) < count:
        if current.weekday() < 5:
            values.append(current)
        current += timedelta(days=1)
    return values


def _frames(count: int = 500) -> list[ReplayFrame]:
    frames = []
    for index, session in enumerate(_trading_dates(date(2024, 1, 2), count)):
        signal_at = datetime.combine(session, time(1, 31), tzinfo=timezone.utc)
        cycle = index % 10
        is_exit = cycle in {1, 3, 5, 7, 9}
        gap = 0.6 if is_exit else -0.6
        if cycle == 8:
            gap = 0.0
        signal_price = 10.3 if is_exit else 10.0
        close_price = signal_price if is_exit else 10.1
        bar_price = signal_price
        theme = "memory"
        if cycle in {4, 5}:
            theme = "cpo"
        elif cycle in {6, 7}:
            theme = "gold"
        range_signal = (
            {"regime": "range", "action": "buy"}
            if cycle == 8
            else {}
        )
        korea_buy_allowed = cycle != 2
        volume = 1_000_000.0
        frames.append(
            ReplayFrame(
                session_date=session,
                signal_at=signal_at,
                symbol="688001",
                theme=theme,
                cn_gap_pct=gap,
                reclaimed_open=True,
                above_vwap=True,
                sector_signal_score=75.0,
                expected_gross_edge_pct=4.0,
                signal_price=signal_price,
                next_minute_bar=MinuteBar(
                    timestamp=signal_at + timedelta(minutes=1),
                    open=bar_price,
                    high=bar_price + 0.03,
                    low=bar_price - 0.03,
                    close=bar_price,
                    volume=volume,
                    amount=bar_price * volume,
                    bid_ask_spread_bps=4.0,
                    atr_1m_pct=0.1,
                ),
                close_price=close_price,
                us_tech_score=65.0,
                us_close_theme_signal={
                    "available": True,
                    "strong": True,
                    "score": 72.0,
                    "sector_change_pct": 1.4,
                    "advancing_ratio": 0.8,
                    "leader_change_pct": 2.4,
                },
                nasdaq_futures_signal={
                    "available": True,
                    "confirmed": True,
                    "buy_allowed": True,
                    "sell_fraction": 0.0,
                    "reason": "nasdaq_futures_trend_confirmed",
                },
                asia_market_gate={
                    "available": True,
                    "buy_allowed": korea_buy_allowed,
                    "reason": (
                        "asia_markets_confirmed"
                        if korea_buy_allowed
                        else "korea_direction_unconfirmed"
                    ),
                },
                board_technical_signal={
                    "available": True,
                    "supportive": True,
                    "near_resistance": False,
                    "support_score": 100.0,
                },
                next_day_high_open_exit_signal=(
                    {
                        "eligible": True,
                        "action": "sell",
                        "reason": "next_day_high_open_trailing_exit",
                    }
                    if is_exit
                    else {}
                ),
                korea_gate={
                    "status": "hold",
                    "reason": (
                        "korea_strength_confirmed"
                        if korea_buy_allowed
                        else "korea_direction_unconfirmed"
                    ),
                    "buy_allowed": korea_buy_allowed,
                    "sell_fraction": 0.0,
                    "confirmed": korea_buy_allowed,
                },
                gold_signal={"buy_allowed": True, "score": 70.0},
                cpo_signal={"available": True, "supportive": True, "score": 30.0},
                range_signal=range_signal,
                evidence_timestamps={
                    "us_first_hour": signal_at - timedelta(hours=12),
                    "us_close": signal_at - timedelta(hours=6),
                    "korea": signal_at - timedelta(seconds=30),
                    "cn_open": signal_at - timedelta(minutes=1),
                    "gold": signal_at - timedelta(hours=6),
                    "nasdaq_futures": signal_at - timedelta(seconds=30),
                },
            )
        )
    return frames


class CrossMarketBacktestServiceTestCase(unittest.TestCase):
    def test_replay_position_enforces_t_plus_one_per_fill_lot(self) -> None:
        session = date(2026, 7, 24)
        position = _ReplayPosition(
            symbol="688001",
            theme="memory",
            last_price=11.0,
            peak_price=11.0,
            instrument_type="stock",
            lots=[
                _ReplayLot(opened_on=session - timedelta(days=1), quantity=100.0, unit_cost=10.0),
                _ReplayLot(opened_on=session, quantity=100.0, unit_cost=11.0),
            ],
            tranche_count=2,
        )

        self.assertEqual(position.sellable_quantity(session), 100.0)
        allocated_cost = position.consume_sellable_fifo(session=session, quantity=100.0)

        self.assertEqual(allocated_cost, 1000.0)
        self.assertEqual(position.quantity, 100.0)
        self.assertEqual(position.total_cost, 1100.0)
        self.assertEqual(position.lots[0].opened_on, session)

    def test_runs_500_sessions_and_all_required_ablations_without_lookahead(self) -> None:
        result = CrossMarketBacktestService().run_ablation_suite(_frames())

        self.assertEqual(result["session_count"], 500)
        self.assertEqual(
            set(result["variants"]),
            {"full", "no_kr", "no_gap_sell", "no_range", "zero_cost"},
        )
        self.assertGreaterEqual(result["variants"]["full"]["metrics"]["trade_count"], 100)
        self.assertGreaterEqual(result["variants"]["full"]["metrics"]["profit_factor"], 1.2)
        self.assertLessEqual(result["variants"]["full"]["metrics"]["max_drawdown_pct"], 10.0)
        self.assertEqual(result["variants"]["full"]["metrics"]["lookahead_violation_count"], 0)
        self.assertTrue(result["validation"]["passed"])
        self.assertTrue(result["validation"]["theme_coverage_passed"])
        self.assertTrue(result["validation"]["range_coverage_passed"])
        self.assertTrue(result["validation"]["ablation_effects_passed"])
        self.assertTrue(all(result["validation"]["ablation_effects"].values()))
        self.assertGreater(
            result["variants"]["zero_cost"]["metrics"]["final_equity"],
            result["variants"]["full"]["metrics"]["final_equity"],
        )

    def test_validation_rejects_missing_scope_and_ineffective_ablations(self) -> None:
        frames = [
            ReplayFrame(
                **{
                    **frame.__dict__,
                    "theme": "memory",
                    "range_signal": {},
                }
            )
            for frame in _frames(10)
        ]

        result = CrossMarketBacktestService().run_ablation_suite(
            frames,
            minimum_sessions=10,
        )

        self.assertFalse(result["validation"]["theme_coverage_passed"])
        self.assertFalse(result["validation"]["range_coverage_passed"])
        self.assertFalse(result["validation"]["ablation_effects_passed"])
        self.assertFalse(result["validation"]["passed"])

    def test_rejects_future_evidence_and_non_next_execution_bar(self) -> None:
        frame = _frames(1)[0]
        future_evidence = ReplayFrame(
            **{
                **frame.__dict__,
                "evidence_timestamps": {"future": frame.signal_at + timedelta(seconds=1)},
            }
        )
        with self.assertRaisesRegex(ValueError, "lookahead_evidence:future"):
            CrossMarketBacktestService().run([future_evidence])

        bad_bar = ReplayFrame(
            **{
                **frame.__dict__,
                "next_minute_bar": MinuteBar(
                    **{
                        **frame.next_minute_bar.__dict__,
                        "timestamp": frame.signal_at,
                    }
                ),
            }
        )
        with self.assertRaisesRegex(ValueError, "lookahead_execution_bar_not_after_signal"):
            CrossMarketBacktestService().run([bad_bar])

    def test_rejects_missing_or_stale_required_cross_market_evidence(self) -> None:
        frame = _frames(1)[0]
        missing_korea = ReplayFrame(
            **{
                **frame.__dict__,
                "evidence_timestamps": {
                    "us_first_hour": frame.signal_at - timedelta(hours=12),
                    "us_close": frame.signal_at - timedelta(hours=6),
                    "cn_open": frame.signal_at - timedelta(minutes=1),
                },
            }
        )
        with self.assertRaisesRegex(ValueError, "required_evidence_missing:korea"):
            CrossMarketBacktestService().run([missing_korea])

        stale_us = ReplayFrame(
            **{
                **frame.__dict__,
                "evidence_timestamps": {
                    **frame.evidence_timestamps,
                    "us_first_hour": frame.signal_at - timedelta(hours=102),
                    "us_close": frame.signal_at - timedelta(hours=97),
                },
            }
        )
        with self.assertRaisesRegex(ValueError, "required_evidence_stale:us_close"):
            CrossMarketBacktestService().run([stale_us])

        missing_first_hour = ReplayFrame(
            **{
                **frame.__dict__,
                "evidence_timestamps": {
                    name: observed_at
                    for name, observed_at in frame.evidence_timestamps.items()
                    if name != "us_first_hour"
                },
            }
        )
        with self.assertRaisesRegex(ValueError, "required_evidence_missing:us_first_hour"):
            CrossMarketBacktestService().run([missing_first_hour])

    def test_rejects_execution_bar_later_than_the_next_minute(self) -> None:
        frame = _frames(1)[0]
        delayed_bar = ReplayFrame(
            **{
                **frame.__dict__,
                "next_minute_bar": MinuteBar(
                    **{
                        **frame.next_minute_bar.__dict__,
                        "timestamp": frame.signal_at + timedelta(minutes=2),
                    }
                ),
            }
        )
        with self.assertRaisesRegex(ValueError, "execution_bar_not_next_minute"):
            CrossMarketBacktestService().run([delayed_bar])


if __name__ == "__main__":
    unittest.main()
