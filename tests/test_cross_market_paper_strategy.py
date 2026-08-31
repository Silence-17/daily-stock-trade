# -*- coding: utf-8 -*-
"""Deterministic tests for the global sector-rotation paper strategy."""

from __future__ import annotations

import unittest
from dataclasses import fields
from datetime import datetime, timedelta, timezone

from src.services.cross_market_paper_strategy import (
    AccountRiskState,
    CrossMarketSignalEngine,
    KoreaSignalSnapshot,
    MinuteBar,
    PaperOrder,
    RealisticMinuteExecutionModel,
    StrategyDecisionInput,
    TimedMarketObservation,
    TradeFeeSchedule,
    StrategyConfig,
    strategy_config_from_overrides,
    strategy_factor_catalog,
)


NOW = datetime(2026, 7, 23, 1, 40, tzinfo=timezone.utc)


class CrossMarketFactorConfigTestCase(unittest.TestCase):
    def test_catalog_lists_every_numeric_runtime_factor(self) -> None:
        catalog = strategy_factor_catalog()
        expected_keys = {
            item.name
            for item in fields(StrategyConfig)
            if item.name not in {
                "strategy_id",
                "opening_sector_score_without_support",
                "flat_open_min_sector_score",
            }
        }

        self.assertEqual({item["key"] for item in catalog}, expected_keys)
        self.assertTrue(all(item["editable"] for item in catalog))
        self.assertTrue(all(item["group_label"] for item in catalog))
        self.assertTrue(all(item["label"] != item["key"] for item in catalog))

    def test_overrides_are_typed_and_validate_related_thresholds(self) -> None:
        config = strategy_config_from_overrides(
            {"entry_score_a": 80, "max_positions": 4}
        )

        self.assertEqual(config.entry_score_a, 80.0)
        self.assertEqual(config.max_positions, 4)
        with self.assertRaisesRegex(ValueError, "entry_score_order"):
            strategy_config_from_overrides({"entry_score_a": 60})
        with self.assertRaisesRegex(ValueError, "unsupported_cross_market_factor"):
            strategy_config_from_overrides({"unknown_factor": 1})
        with self.assertRaisesRegex(ValueError, "entry_weights_invalid"):
            strategy_config_from_overrides({
                "entry_cross_market_weight_pct": 0,
                "entry_sector_weight_pct": 0,
                "entry_stock_weight_pct": 0,
                "entry_intraday_weight_pct": 0,
                "entry_technical_weight_pct": 0,
            })


class CrossMarketSignalEngineTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = CrossMarketSignalEngine()

    @staticmethod
    def _kr_observations(at: datetime, change_pct: float) -> list[TimedMarketObservation]:
        return [
            TimedMarketObservation(code=code, change_pct=change_pct, observed_at=at, provider_timestamp=at)
            for code in ("KS11", "KQ11", "005930.KS", "000660.KS")
        ]

    @staticmethod
    def _strong_close() -> dict[str, object]:
        return {
            "available": True,
            "strong": True,
            "score": 72.0,
            "sector_change_pct": 1.4,
            "advancing_ratio": 0.8,
            "leader_change_pct": 2.4,
        }

    @staticmethod
    def _strong_premarket() -> dict[str, object]:
        return {
            "available": True,
            "strong": True,
            "score": 82.0,
            "sector_change_pct": 2.8,
            "advancing_ratio": 0.8,
            "leader_change_pct": 4.0,
        }

    @staticmethod
    def _asia_ready() -> dict[str, object]:
        return {
            "available": True,
            "buy_allowed": True,
            "score": 80.0,
            "reason": "asia_confirmed",
        }

    @staticmethod
    def _board_support() -> dict[str, object]:
        return {
            "available": True,
            "supportive": True,
            "near_resistance": False,
            "support_score": 100.0,
            "primary_board": {
                "name": "test-board",
                "supportive": True,
                "pressure_windows": [],
                "breakout_confirmed": False,
            },
        }

    @staticmethod
    def _pullback_ready() -> dict[str, object]:
        return {
            "available": True,
            "confirmed": True,
            "pullback_from_high_pct": 1.6,
            "rebound_from_low_pct": 0.5,
        }

    @staticmethod
    def _nasdaq_ready(
        *,
        adjustment: float = 0.0,
    ) -> dict[str, object]:
        return {
            "available": True,
            "confirmed": True,
            "buy_allowed": True,
            "sell_fraction": 0.0,
            "reason": "nasdaq_futures_trend_confirmed",
            "score": 80.0,
            "sector_score_adjustment": adjustment,
        }

    @staticmethod
    def _us_coverage_shortfall(*, stage: str, theme: str = "mlcc") -> dict[str, object]:
        reason = (
            "premarket_theme_coverage_insufficient"
            if stage == "premarket"
            else "us_close_theme_coverage_insufficient"
        )
        observed_at = "2026-07-22T20:00:00+00:00"
        stored_signal = {
            "available": False,
            "strong": False,
            "reason": reason,
        }
        return {
            **stored_signal,
            "theme": theme,
            "signal_theme": theme,
            "session_date": "2026-07-22",
            "observed_at": observed_at,
            "asia_supplement_eligible": True,
            "premarket_reversal_invalidated": False,
            "snapshot": {
                "session_date": "2026-07-22",
                "session_stage": stage,
                "observed_at": observed_at,
                "theme_signals": {theme: stored_signal},
            },
        }

    def test_korea_snapshot_fails_closed_for_missing_stale_and_future_data(self) -> None:
        with self.assertRaisesRegex(ValueError, "korea_evidence_missing"):
            self.engine.build_korea_snapshot(self._kr_observations(NOW, 1.0)[:-1], now=NOW)
        with self.assertRaisesRegex(ValueError, "korea_evidence_stale"):
            self.engine.build_korea_snapshot(
                self._kr_observations(NOW - timedelta(seconds=121), 1.0),
                now=NOW,
            )
        with self.assertRaisesRegex(ValueError, "korea_evidence_from_future"):
            self.engine.build_korea_snapshot(
                self._kr_observations(NOW + timedelta(seconds=2), 1.0),
                now=NOW,
            )

    def test_korea_gate_requires_five_full_minutes_and_cpo_bypasses_it(self) -> None:
        snapshots = []
        for offset in range(6):
            sample_at = NOW - timedelta(minutes=5 - offset)
            snapshots.append(
                self.engine.build_korea_snapshot(
                    self._kr_observations(sample_at, 1.0),
                    now=sample_at,
                )
            )

        gate = self.engine.evaluate_korea_gate(snapshots, theme="memory", now=NOW)
        cpo_gate = self.engine.evaluate_korea_gate([], theme="cpo", now=NOW)

        self.assertEqual(gate["status"], "hold")
        self.assertTrue(gate["buy_allowed"])
        self.assertEqual(gate["sell_fraction"], 0.0)
        self.assertEqual(gate["confirmation_sample_count"], 6)
        self.assertEqual(gate["confirmation_span_seconds"], 300.0)
        self.assertEqual(gate["confirmation_duration_seconds"], 300)
        self.assertEqual(cpo_gate["status"], "bypassed")
        self.assertTrue(cpo_gate["buy_allowed"])

    def test_korea_gate_rejects_five_samples_covering_only_four_minutes(self) -> None:
        snapshots = [
            KoreaSignalSnapshot(
                observed_at=NOW - timedelta(minutes=4 - offset),
                score=50.0,
                component_changes_pct={},
            )
            for offset in range(5)
        ]

        gate = self.engine.evaluate_korea_gate(
            snapshots,
            theme="memory",
            now=NOW,
        )

        self.assertEqual(gate["status"], "unavailable")
        self.assertEqual(
            gate["reason"],
            "korea_confirmation_duration_insufficient",
        )
        self.assertFalse(gate["buy_allowed"])

    def test_korea_gate_rejects_burst_samples_as_five_minute_confirmation(self) -> None:
        snapshots = [
            KoreaSignalSnapshot(
                observed_at=NOW - timedelta(seconds=40 - offset * 10),
                score=50.0,
                component_changes_pct={},
            )
            for offset in range(5)
        ]

        gate = self.engine.evaluate_korea_gate(snapshots, theme="memory", now=NOW)

        self.assertEqual(gate["status"], "unavailable")
        self.assertEqual(gate["reason"], "korea_confirmation_not_continuous")
        self.assertFalse(gate["buy_allowed"])

    def test_korea_gate_ignores_dense_refreshes_after_valid_confirmation(self) -> None:
        snapshots = [
            KoreaSignalSnapshot(
                observed_at=NOW - timedelta(seconds=348 - offset * 60),
                score=-50.0,
                component_changes_pct={},
            )
            for offset in range(6)
        ]
        snapshots.extend(
            KoreaSignalSnapshot(
                observed_at=NOW - timedelta(seconds=offset),
                score=-50.0,
                component_changes_pct={},
            )
            for offset in (25, 14, 8)
        )

        gate = self.engine.evaluate_korea_gate(
            snapshots,
            theme="memory",
            now=NOW,
        )

        self.assertEqual(gate["status"], "exit")
        self.assertTrue(gate["confirmed"])
        self.assertEqual(gate["confirmation_sample_count"], 6)
        self.assertGreaterEqual(gate["confirmation_span_seconds"], 300.0)

    def test_korea_decline_reduces_or_exits_only_after_confirmation(self) -> None:
        moderate = [
            KoreaSignalSnapshot(
                observed_at=NOW - timedelta(minutes=5 - offset),
                score=-30.0,
                component_changes_pct={},
            )
            for offset in range(6)
        ]
        severe = [
            KoreaSignalSnapshot(
                observed_at=NOW - timedelta(minutes=5 - offset),
                score=-50.0,
                component_changes_pct={},
            )
            for offset in range(6)
        ]
        interrupted = list(moderate)
        interrupted[-3] = KoreaSignalSnapshot(
            observed_at=interrupted[-3].observed_at,
            score=5.0,
            component_changes_pct={},
        )

        self.assertEqual(self.engine.evaluate_korea_gate(moderate, theme="semiconductor", now=NOW)["sell_fraction"], 0.5)
        self.assertEqual(self.engine.evaluate_korea_gate(severe, theme="materials", now=NOW)["sell_fraction"], 1.0)
        self.assertEqual(self.engine.evaluate_korea_gate(interrupted, theme="equipment", now=NOW)["sell_fraction"], 0.0)

    def test_open_gold_range_and_net_edge_rules_are_deterministic(self) -> None:
        self.assertEqual(self.engine.classify_cn_open(0.19)["regime"], "high_open")
        self.assertEqual(self.engine.classify_cn_open(0.189999)["regime"], "flat_open")
        self.assertEqual(self.engine.classify_cn_open(-0.8)["regime"], "low_open")
        self.assertEqual(self.engine.classify_cn_open(-1.6)["regime"], "extreme_low_open")
        self.assertEqual(self.engine.classify_cn_open(0.0)["regime"], "flat_open")

        gold = self.engine.calculate_gold_score(
            overnight_return_pct=1.0,
            five_day_return_pct=2.0,
            above_ma20=True,
            rate_cut_news_score=80.0,
        )
        range_buy = self.engine.evaluate_range_action(
            adx14=16,
            ma20_slope_pct_per_day=0.05,
            price=10,
            range_low=9,
            rsi14=32,
            bollinger_position=0.1,
            above_vwap=True,
        )
        self.assertTrue(gold["buy_allowed"])
        self.assertEqual(gold["target_fraction"], 1.0)
        self.assertEqual(range_buy["action"], "buy")
        self.assertTrue(self.engine.has_sufficient_net_edge(expected_gross_edge_pct=1.5, estimated_round_trip_cost_pct=0.4))
        self.assertTrue(self.engine.has_sufficient_net_edge(expected_gross_edge_pct=1.0, estimated_round_trip_cost_pct=0.4))
        self.assertFalse(self.engine.has_sufficient_net_edge(expected_gross_edge_pct=0.89, estimated_round_trip_cost_pct=0.4))

    def test_gold_signal_caps_price_only_entry_but_allows_full_confirmed_slot(self) -> None:
        base = {
            "theme": "gold",
            "cn_gap_pct": -0.5,
            "reclaimed_open": True,
            "sector_signal_score": 45.0,
            "expected_gross_edge_pct": 2.0,
            "estimated_round_trip_cost_pct": 0.2,
        }

        price_only = self.engine.decide(
            StrategyDecisionInput(
                **base,
                gold_signal={"buy_allowed": True, "target_fraction": 0.5},
            )
        )
        price_and_news = self.engine.decide(
            StrategyDecisionInput(
                **base,
                gold_signal={"buy_allowed": True, "target_fraction": 1.0},
            )
        )

        self.assertEqual(price_only.action, "buy")
        self.assertEqual(price_only.target_position_pct, 50.0)
        self.assertEqual(price_and_news.action, "buy")
        self.assertEqual(price_and_news.target_position_pct, 50.0)

    def test_low_position_signal_adjusts_borderline_sector_entry(self) -> None:
        base = {
            "theme": "gold",
            "cn_gap_pct": -0.5,
            "reclaimed_open": True,
            "sector_signal_score": 37.0,
            "gold_signal": {"buy_allowed": True, "target_fraction": 1.0},
            "expected_gross_edge_pct": 2.0,
            "estimated_round_trip_cost_pct": 0.2,
        }

        supported = self.engine.decide(
            StrategyDecisionInput(
                **base,
                low_position_signal={"available": True, "confirmed": True},
            )
        )
        extended = self.engine.decide(
            StrategyDecisionInput(
                **base,
                low_position_signal={
                    "available": True,
                    "confirmed": False,
                    "range_percentile": 80.0,
                },
            )
        )

        self.assertEqual(supported.action, "buy")
        self.assertEqual(extended.action, "buy")

    def test_decision_priority_applies_stop_high_open_korea_and_t_plus_one(self) -> None:
        stopped = self.engine.decide(
            StrategyDecisionInput(
                theme="memory",
                cn_gap_pct=0.8,
                has_position=True,
                sellable_fraction=1.0,
                position_return_pct=-5.1,
                korea_gate={"sell_fraction": 0.5, "reason": "korea_decline_confirmed"},
            )
        )
        high_open = self.engine.decide(
            StrategyDecisionInput(
                theme="memory",
                cn_gap_pct=0.8,
                has_position=True,
                sellable_fraction=1.0,
                position_return_pct=1.0,
                korea_gate={"sell_fraction": 0.5, "reason": "korea_decline_confirmed"},
                next_day_high_open_exit_signal={
                    "eligible": True,
                    "action": "sell",
                    "reason": "next_day_high_open_trailing_exit",
                },
            )
        )
        deferred = self.engine.decide(
            StrategyDecisionInput(
                theme="memory",
                cn_gap_pct=0.8,
                has_position=True,
                sellable_fraction=0.0,
                position_return_pct=1.0,
                next_day_high_open_exit_signal={
                    "eligible": True,
                    "action": "sell",
                    "reason": "next_day_high_open_trailing_exit",
                },
            )
        )

        self.assertEqual(stopped.reason, "hard_stop_loss")
        self.assertEqual(stopped.action, "exit")
        self.assertEqual(high_open.reason, "korea_decline_confirmed")
        self.assertEqual(high_open.action, "reduce")
        self.assertEqual(deferred.action, "hold")
        self.assertEqual(deferred.deferred_action, "exit")

    def test_next_day_high_open_exit_respects_eligible_lot_fraction(self) -> None:
        decision = self.engine.decide(
            StrategyDecisionInput(
                theme="memory",
                cn_gap_pct=0.8,
                has_position=True,
                sellable_fraction=1.0,
                position_return_pct=1.0,
                next_day_high_open_exit_signal={
                    "eligible": True,
                    "action": "sell",
                    "reason": "next_day_high_open_trailing_exit",
                    "sell_fraction": 0.4,
                },
            )
        )

        self.assertEqual(decision.action, "reduce")
        self.assertEqual(decision.sell_fraction, 0.4)
        self.assertEqual(decision.reason, "next_day_high_open_trailing_exit")

    def test_linked_tech_opening_requires_us_close_asia_and_low_open_reclaim(self) -> None:
        base = StrategyDecisionInput(
            theme="memory",
            cn_gap_pct=-0.8,
            reclaimed_open=True,
            above_vwap=True,
            sector_signal_score=70.0,
            us_tech_score=65.0,
            us_close_theme_signal=self._strong_close(),
            nasdaq_futures_signal=self._nasdaq_ready(),
            asia_market_gate=self._asia_ready(),
            board_technical_signal=self._board_support(),
            expected_gross_edge_pct=2.0,
            estimated_round_trip_cost_pct=0.4,
        )
        accepted = self.engine.decide(base)
        no_asia = self.engine.decide(
            StrategyDecisionInput(
                **{
                    **base.__dict__,
                    "asia_market_gate": {
                        "buy_allowed": False,
                        "reason": "asia_market_direction_unconfirmed",
                    },
                }
            )
        )
        missing_us_tech = self.engine.decide(
            StrategyDecisionInput(
                **{
                    **base.__dict__,
                    "us_tech_score": None,
                }
            )
        )
        weak_us_tech = self.engine.decide(
            StrategyDecisionInput(
                **{
                    **base.__dict__,
                    "us_tech_score": 39.9,
                }
            )
        )

        self.assertEqual(accepted.action, "buy")
        self.assertEqual(accepted.target_position_pct, 50.0)
        self.assertEqual(no_asia.action, "blocked")
        self.assertEqual(no_asia.reason, "asia_market_direction_unconfirmed")
        self.assertEqual(missing_us_tech.action, "buy")
        self.assertEqual(weak_us_tech.action, "buy")

    def test_non_technology_rotation_does_not_require_us_or_asia_direction(self) -> None:
        decision = self.engine.decide(
            StrategyDecisionInput(
                theme="pharma",
                cn_gap_pct=-0.8,
                reclaimed_open=True,
                above_vwap=True,
                sector_signal_score=70.0,
                us_close_theme_signal={
                    "available": True,
                    "strong": False,
                    "score": -80.0,
                },
                asia_market_gate={
                    "buy_allowed": False,
                    "reason": "asia_market_severe_downside",
                },
                board_technical_signal=self._board_support(),
                expected_gross_edge_pct=2.0,
                estimated_round_trip_cost_pct=0.4,
            )
        )

        self.assertEqual(decision.action, "buy")
        self.assertEqual(
            decision.reason,
            "pharma_domestic_rotation_entry_confirmed",
        )

    def test_unconfirmed_flat_open_entry_is_capped_without_blocking_rotation(self) -> None:
        decision = self.engine.decide(
            StrategyDecisionInput(
                theme="consumer",
                cn_gap_pct=0.0,
                reclaimed_open=False,
                above_vwap=False,
                sector_signal_score=70.0,
                board_technical_signal=self._board_support(),
                expected_gross_edge_pct=2.0,
                estimated_round_trip_cost_pct=0.4,
                entry_score=80.0,
            )
        )

        self.assertEqual(decision.action, "buy")
        self.assertEqual(decision.target_position_pct, 25.0)
        self.assertEqual(decision.entry_grade, "A")

    def test_low_open_accepts_reclaim_of_open_or_vwap(self) -> None:
        base = {
            "theme": "memory",
            "cn_gap_pct": -0.8,
            "sector_signal_score": 70.0,
            "us_tech_score": 65.0,
            "us_close_theme_signal": self._strong_close(),
            "nasdaq_futures_signal": self._nasdaq_ready(),
            "asia_market_gate": self._asia_ready(),
            "board_technical_signal": self._board_support(),
            "expected_gross_edge_pct": 2.0,
            "estimated_round_trip_cost_pct": 0.4,
        }

        reclaimed_open = self.engine.decide(
            StrategyDecisionInput(**base, reclaimed_open=True, above_vwap=False)
        )
        reclaimed_vwap = self.engine.decide(
            StrategyDecisionInput(**base, reclaimed_open=False, above_vwap=True)
        )
        neither = self.engine.decide(
            StrategyDecisionInput(**base, reclaimed_open=False, above_vwap=False)
        )

        self.assertEqual(reclaimed_open.action, "buy")
        self.assertEqual(reclaimed_vwap.action, "buy")
        self.assertEqual(neither.action, "buy")

    def test_nq00y_confirms_entries_and_severe_downtrend_reduces_positions(self) -> None:
        base = {
            "theme": "memory",
            "cn_gap_pct": -0.8,
            "reclaimed_open": True,
            "above_vwap": True,
            "sector_signal_score": 70.0,
            "us_tech_score": 65.0,
            "us_close_theme_signal": self._strong_close(),
            "asia_market_gate": self._asia_ready(),
            "board_technical_signal": self._board_support(),
            "expected_gross_edge_pct": 2.0,
            "estimated_round_trip_cost_pct": 0.4,
        }
        unavailable = self.engine.decide(StrategyDecisionInput(**base))
        bearish = self.engine.decide(StrategyDecisionInput(
            **base,
            nasdaq_futures_signal={
                "available": True,
                "confirmed": True,
                "buy_allowed": False,
                "reason": "nasdaq_futures_downtrend_blocks_entry",
            },
        ))
        positive = self.engine.decide(StrategyDecisionInput(
            **{**base, "sector_signal_score": 36.0},
            rotation_signal={"tailwind": True},
            nasdaq_futures_signal=self._nasdaq_ready(adjustment=5.0),
        ))
        severe = self.engine.decide(StrategyDecisionInput(
            theme="memory",
            cn_gap_pct=-0.5,
            has_position=True,
            sellable_fraction=1.0,
            nasdaq_futures_signal={
                "available": True,
                "confirmed": True,
                "buy_allowed": False,
                "sell_fraction": 0.5,
                "reason": "nasdaq_futures_severe_downtrend",
            },
        ))

        self.assertEqual(unavailable.reason, "nasdaq_futures_signal_unavailable")
        self.assertEqual(bearish.reason, "nasdaq_futures_downtrend_blocks_entry")
        self.assertEqual(positive.action, "buy")
        self.assertEqual(severe.action, "reduce")
        self.assertEqual(severe.sell_fraction, 0.5)

    def test_flat_open_strong_cross_market_entry_starts_at_twenty_five_percent(self) -> None:
        base = {
            "theme": "memory",
            "cn_gap_pct": 0.0,
            "reclaimed_open": True,
            "above_vwap": True,
            "sector_signal_score": 70.0,
            "us_tech_score": 65.0,
            "us_close_theme_signal": self._strong_close(),
            "nasdaq_futures_signal": self._nasdaq_ready(),
            "asia_market_gate": self._asia_ready(),
            "board_technical_signal": self._board_support(),
            "expected_gross_edge_pct": 2.0,
            "estimated_round_trip_cost_pct": 0.4,
        }

        accepted = self.engine.decide(StrategyDecisionInput(**base))
        partial_support = self.engine.decide(StrategyDecisionInput(
            **{
                **base,
                "board_technical_signal": {
                    "available": True,
                    "supportive": True,
                    "near_resistance": False,
                    "support_score": 80.0,
                },
            }
        ))
        without_support = self.engine.decide(StrategyDecisionInput(
            **{
                **base,
                "board_technical_signal": {
                    "available": True,
                    "supportive": False,
                    "near_resistance": False,
                    "support_score": 0.0,
                },
            }
        ))
        missing_technical = self.engine.decide(StrategyDecisionInput(
            **{**base, "board_technical_signal": {}},
        ))
        weak_sector = self.engine.decide(StrategyDecisionInput(
            **{**base, "sector_signal_score": 54.0}
        ))

        self.assertEqual(accepted.action, "buy")
        self.assertEqual(accepted.target_position_pct, 50.0)
        self.assertEqual(
            accepted.reason,
            "memory_flat_open_staged_entry_confirmed",
        )
        self.assertEqual(partial_support.action, "buy")
        self.assertEqual(without_support.action, "buy")
        self.assertEqual(
            missing_technical.reason,
            "flat_open_board_technical_required",
        )
        self.assertEqual(weak_sector.action, "buy")

    def test_flat_open_staged_entry_adds_only_after_intraday_confirmation(self) -> None:
        base = {
            "theme": "memory",
            "entry_phase": "intraday_dip",
            "cn_gap_pct": 0.0,
            "has_position": True,
            "flat_open_staged_entry": True,
            "strategy_cost_basis_pct": 25.0,
            "reclaimed_open": True,
            "above_vwap": True,
            "sector_signal_score": 72.0,
            "us_tech_score": 65.0,
            "us_close_theme_signal": self._strong_close(),
            "us_premarket_signal": self._strong_premarket(),
            "nasdaq_futures_signal": self._nasdaq_ready(),
            "intraday_pullback_signal": self._pullback_ready(),
            "asia_market_gate": self._asia_ready(),
            "board_technical_signal": self._board_support(),
            "expected_gross_edge_pct": 2.0,
            "estimated_round_trip_cost_pct": 0.4,
            "risk": AccountRiskState(
                total_exposure_pct=25.0,
                theme_exposure_pct=25.0,
                symbol_exposure_pct=25.0,
                position_count=1,
            ),
        }

        confirmed = self.engine.decide(StrategyDecisionInput(**base))
        without_support = self.engine.decide(StrategyDecisionInput(
            **{
                **base,
                "board_technical_signal": {
                    "available": True,
                    "supportive": False,
                    "near_resistance": False,
                    "support_score": 0.0,
                },
            }
        ))
        range_overlap = self.engine.decide(StrategyDecisionInput(
            **{
                **base,
                "current_tranche_count": 1,
                "range_signal": {"regime": "range", "action": "buy"},
            },
        ))

        self.assertEqual(confirmed.action, "buy")
        self.assertEqual(without_support.action, "buy")
        missing_dip = self.engine.decide(StrategyDecisionInput(
            **{**base, "intraday_pullback_signal": {}},
        ))
        not_staged = self.engine.decide(StrategyDecisionInput(
            **{**base, "flat_open_staged_entry": False},
        ))
        already_filled = self.engine.decide(StrategyDecisionInput(
            **{**base, "strategy_cost_basis_pct": 55.0},
        ))

        self.assertEqual(confirmed.action, "buy")
        # The symbol already owns 25% of equity, so this order fills only the
        # remaining gap to the unified 50% target.
        self.assertEqual(confirmed.target_position_pct, 25.0)
        self.assertEqual(
            confirmed.reason,
            "memory_flat_open_staged_add_confirmed",
        )
        self.assertEqual(range_overlap.action, "buy")
        self.assertEqual(range_overlap.reason, "range_add_tranche")
        self.assertEqual(
            missing_dip.reason,
            "memory_premarket_close_dip_unconfirmed",
        )
        self.assertEqual(not_staged.action, "buy")
        self.assertEqual(already_filled.reason, "no_sell_signal")

    def test_flat_open_intraday_core_leader_can_outrun_a_flat_board(self) -> None:
        base = {
            "theme": "mlcc",
            "entry_phase": "intraday_dip",
            "cn_gap_pct": -0.071251,
            "reclaimed_open": False,
            "above_vwap": True,
            "sector_signal_score": 6.760618,
            "core_leader_signal": {
                "available": True,
                "confirmed": True,
                "stock_change_pct": 4.18251,
                "sector_change_pct": 0.14,
                "relative_strength_pct": 4.04251,
            },
            "us_close_theme_signal": self._strong_close(),
            "us_premarket_signal": self._strong_premarket(),
            "nasdaq_futures_signal": self._nasdaq_ready(),
            "asia_supply_chain_signal": {
                "available": True,
                "strong": True,
                "score": 100.0,
                "sector_change_pct": 2.7175,
                "advancing_ratio": 1.0,
            },
            "intraday_pullback_signal": {
                "available": True,
                "confirmed": True,
                "pullback_from_high_pct": 4.024497,
                "rebound_from_low_pct": 4.277567,
            },
            "asia_market_gate": self._asia_ready(),
            "board_technical_signal": {
                "available": True,
                "supportive": False,
                "near_resistance": False,
                "support_score": 0.0,
            },
            "expected_gross_edge_pct": 4.19,
            "estimated_round_trip_cost_pct": 0.4,
        }

        accepted = self.engine.decide(StrategyDecisionInput(**base))
        opening = self.engine.decide(
            StrategyDecisionInput(**{**base, "entry_phase": "opening"})
        )
        low_open = self.engine.decide(
            StrategyDecisionInput(**{**base, "cn_gap_pct": -0.6})
        )
        ordinary_stock = self.engine.decide(
            StrategyDecisionInput(
                **{
                    **base,
                    "core_leader_signal": {
                        "available": True,
                        "confirmed": False,
                    },
                }
            )
        )
        near_resistance = self.engine.decide(
            StrategyDecisionInput(
                **{
                    **base,
                    "board_technical_signal": {
                        "available": True,
                        "supportive": False,
                        "near_resistance": True,
                        "breakout_confirmed": False,
                    },
                }
            )
        )

        self.assertEqual(accepted.action, "buy")
        self.assertEqual(accepted.target_position_pct, 50.0)
        self.assertEqual(
            accepted.reason,
            "mlcc_core_leader_divergence_flat_open_staged_entry_confirmed",
        )
        self.assertEqual(opening.action, "buy")
        self.assertEqual(low_open.action, "buy")
        self.assertEqual(ordinary_stock.action, "buy")
        self.assertEqual(near_resistance.action, "buy")

    def test_core_leader_override_cannot_add_when_board_stays_weak(self) -> None:
        decision = self.engine.decide(
            StrategyDecisionInput(
                theme="mlcc",
                entry_phase="intraday_dip",
                cn_gap_pct=0.0,
                has_position=True,
                flat_open_staged_entry=True,
                strategy_cost_basis_pct=25.0,
                reclaimed_open=True,
                above_vwap=True,
                sector_signal_score=8.0,
                core_leader_signal={"available": True, "confirmed": True},
                us_close_theme_signal=self._strong_close(),
                us_premarket_signal=self._strong_premarket(),
                nasdaq_futures_signal=self._nasdaq_ready(),
                intraday_pullback_signal=self._pullback_ready(),
                asia_market_gate=self._asia_ready(),
                board_technical_signal={
                    "available": True,
                    "supportive": False,
                    "near_resistance": False,
                },
                expected_gross_edge_pct=3.0,
                estimated_round_trip_cost_pct=0.4,
                risk=AccountRiskState(
                    total_exposure_pct=25.0,
                    theme_exposure_pct=25.0,
                    symbol_exposure_pct=25.0,
                    position_count=1,
                ),
            )
        )

        self.assertEqual(decision.action, "buy")

    def test_core_leader_can_absorb_only_short_cycle_resistance_intraday(self) -> None:
        base = {
            "theme": "memory",
            "entry_phase": "intraday_dip",
            "cn_gap_pct": 0.0,
            "reclaimed_open": False,
            "above_vwap": True,
            "sector_signal_score": 64.0,
            "core_leader_signal": {"available": True, "confirmed": True},
            "us_tech_score": 65.0,
            "us_close_theme_signal": self._strong_close(),
            "us_premarket_signal": self._strong_premarket(),
            "nasdaq_futures_signal": self._nasdaq_ready(),
            "intraday_pullback_signal": self._pullback_ready(),
            "asia_market_gate": self._asia_ready(),
            "expected_gross_edge_pct": 2.0,
            "estimated_round_trip_cost_pct": 0.4,
        }
        short_pressure = {
            "available": True,
            "supportive": False,
            "near_resistance": True,
            "breakout_confirmed": False,
            "short_resistance_only": True,
            "short_pressure_boards": ["存储芯片"],
            "medium_long_pressure_boards": [],
            "pressure_windows": [5],
        }

        accepted = self.engine.decide(StrategyDecisionInput(
            **base,
            board_technical_signal=short_pressure,
        ))
        medium_pressure = self.engine.decide(StrategyDecisionInput(
            **base,
            board_technical_signal={
                **short_pressure,
                "short_resistance_only": False,
                "medium_long_pressure_boards": ["存储芯片"],
                "pressure_windows": [20],
            },
        ))
        opening = self.engine.decide(StrategyDecisionInput(
            **{**base, "entry_phase": "opening"},
            board_technical_signal=short_pressure,
        ))
        ordinary = self.engine.decide(StrategyDecisionInput(
            **{
                **base,
                "core_leader_signal": {"available": True, "confirmed": False},
            },
            board_technical_signal=short_pressure,
        ))

        self.assertEqual(accepted.action, "buy")
        self.assertEqual(accepted.target_position_pct, 50.0)
        self.assertEqual(
            accepted.reason,
            "memory_core_leader_divergence_flat_open_staged_entry_confirmed",
        )
        self.assertEqual(medium_pressure.action, "buy")
        self.assertEqual(opening.action, "buy")
        self.assertEqual(ordinary.action, "buy")

    def test_ai_intraday_entry_requires_premarket_close_pullback_and_support(self) -> None:
        base = {
            "theme": "artificial_intelligence",
            "entry_phase": "intraday_dip",
            "cn_gap_pct": -0.5,
            "reclaimed_open": True,
            "above_vwap": True,
            "sector_signal_score": 72.0,
            "us_close_theme_signal": self._strong_close(),
            "nasdaq_futures_signal": self._nasdaq_ready(),
            "intraday_pullback_signal": self._pullback_ready(),
            "asia_market_gate": self._asia_ready(),
            "board_technical_signal": self._board_support(),
            "expected_gross_edge_pct": 2.0,
            "estimated_round_trip_cost_pct": 0.4,
        }
        strong = self.engine.decide(StrategyDecisionInput(
            **base,
            us_premarket_signal=self._strong_premarket(),
        ))
        missing = self.engine.decide(StrategyDecisionInput(**base))

        self.assertEqual(strong.action, "buy")
        self.assertEqual(
            strong.reason,
            "artificial_intelligence_premarket_close_intraday_dip_confirmed",
        )
        self.assertEqual(
            missing.reason,
            "artificial_intelligence_premarket_close_dip_unconfirmed",
        )

    def test_opening_cannot_substitute_premarket_for_completed_us_close(self) -> None:
        decision = self.engine.decide(StrategyDecisionInput(
            theme="memory",
            cn_gap_pct=-0.6,
            reclaimed_open=True,
            above_vwap=True,
            sector_signal_score=75.0,
            us_tech_score=None,
            us_premarket_signal=self._strong_premarket(),
            nasdaq_futures_signal=self._nasdaq_ready(),
            asia_market_gate=self._asia_ready(),
            board_technical_signal=self._board_support(),
            expected_gross_edge_pct=2.0,
            estimated_round_trip_cost_pct=0.4,
        ))

        self.assertEqual(decision.action, "blocked")
        self.assertEqual(decision.reason, "memory_us_close_theme_unconfirmed")

    def test_mlcc_intraday_can_use_strong_asia_supply_chain_when_us_otc_is_missing(self) -> None:
        decision = self.engine.decide(StrategyDecisionInput(
            theme="mlcc",
            entry_phase="intraday_dip",
            cn_gap_pct=-0.4,
            reclaimed_open=True,
            above_vwap=True,
            sector_signal_score=70.0,
            us_close_theme_signal=self._strong_close(),
            us_premarket_signal=self._us_coverage_shortfall(
                stage="premarket"
            ),
            nasdaq_futures_signal=self._nasdaq_ready(),
            asia_supply_chain_signal={
                "available": True,
                "strong": True,
                "score": 55.0,
                "sector_change_pct": 0.7,
                "advancing_ratio": 0.8,
            },
            intraday_pullback_signal=self._pullback_ready(),
            asia_market_gate=self._asia_ready(),
            board_technical_signal=self._board_support(),
            expected_gross_edge_pct=2.0,
            estimated_round_trip_cost_pct=0.4,
        ))

        self.assertEqual(decision.action, "buy")
        self.assertEqual(
            decision.reason,
            "mlcc_premarket_close_intraday_dip_confirmed",
        )

    def test_mlcc_asia_signal_strictly_supplements_us_otc_coverage_shortfalls(self) -> None:
        asia_signal = {
            "available": True,
            "strong": True,
            "score": 55.0,
            "sector_change_pct": 0.7,
            "advancing_ratio": 0.8,
        }
        base = {
            "theme": "mlcc",
            "cn_gap_pct": -0.4,
            "reclaimed_open": True,
            "above_vwap": True,
            "sector_signal_score": 70.0,
            "us_close_theme_signal": self._us_coverage_shortfall(stage="close"),
            "nasdaq_futures_signal": self._nasdaq_ready(),
            "asia_supply_chain_signal": asia_signal,
            "asia_market_gate": self._asia_ready(),
            "board_technical_signal": self._board_support(),
            "expected_gross_edge_pct": 2.0,
            "estimated_round_trip_cost_pct": 0.4,
        }

        opening = self.engine.decide(StrategyDecisionInput(**base))
        intraday = self.engine.decide(StrategyDecisionInput(
            **{
                **base,
                "entry_phase": "intraday_dip",
                "us_premarket_signal": self._us_coverage_shortfall(
                    stage="premarket"
                ),
                "intraday_pullback_signal": self._pullback_ready(),
            }
        ))
        unverified_close = self._us_coverage_shortfall(stage="close")
        unverified_close.pop("asia_supplement_eligible")
        rejected = self.engine.decide(StrategyDecisionInput(
            **{**base, "us_close_theme_signal": unverified_close}
        ))

        self.assertEqual(opening.action, "buy")
        self.assertEqual(intraday.action, "buy")
        self.assertEqual(rejected.action, "blocked")
        self.assertEqual(rejected.reason, "mlcc_us_close_theme_unconfirmed")

    def test_sector_support_and_defensive_resistance_rotation_work_together(self) -> None:
        base = {
            "theme": "memory",
            "cn_gap_pct": -0.4,
            "reclaimed_open": True,
            "above_vwap": True,
            "sector_signal_score": 36.0,
            "us_tech_score": 65.0,
            "us_close_theme_signal": self._strong_close(),
            "nasdaq_futures_signal": self._nasdaq_ready(),
            "asia_market_gate": self._asia_ready(),
            "board_technical_signal": self._board_support(),
            "expected_gross_edge_pct": 2.0,
            "estimated_round_trip_cost_pct": 0.4,
        }
        without_rotation = self.engine.decide(StrategyDecisionInput(**base))
        with_rotation = self.engine.decide(StrategyDecisionInput(
            **base,
            rotation_signal={
                "available": True,
                "tailwind": True,
                "pressure_groups": ["bank", "liquor"],
            },
        ))
        near_resistance = self.engine.decide(StrategyDecisionInput(
            **{
                **base,
                "sector_signal_score": 80.0,
                "board_technical_signal": {
                    "available": True,
                    "supportive": False,
                    "near_resistance": True,
                    "breakout_confirmed": False,
                },
            }
        ))

        self.assertEqual(without_rotation.action, "buy")
        self.assertEqual(with_rotation.action, "buy")
        self.assertEqual(near_resistance.action, "buy")

    def test_range_position_adds_second_tranche_but_not_third(self) -> None:
        base = {
            "theme": "memory",
            "entry_phase": "intraday_dip",
            "cn_gap_pct": 0.0,
            "has_position": True,
            "strategy_cost_basis_pct": 25.0,
            "range_signal": {"regime": "range", "action": "buy"},
            "us_tech_score": 65.0,
            "us_close_theme_signal": self._strong_close(),
            "us_premarket_signal": self._strong_premarket(),
            "nasdaq_futures_signal": self._nasdaq_ready(),
            "intraday_pullback_signal": self._pullback_ready(),
            "asia_market_gate": self._asia_ready(),
            "board_technical_signal": self._board_support(),
            "expected_gross_edge_pct": 2.0,
            "estimated_round_trip_cost_pct": 0.4,
        }

        second = self.engine.decide(StrategyDecisionInput(**base, current_tranche_count=1))
        third = self.engine.decide(StrategyDecisionInput(**base, current_tranche_count=2))
        no_asia = self.engine.decide(StrategyDecisionInput(
            **{
                **base,
                "current_tranche_count": 1,
                "asia_market_gate": {
                    "buy_allowed": False,
                    "reason": "asia_market_direction_unconfirmed",
                },
            }
        ))

        self.assertEqual(second.action, "buy")
        self.assertEqual(second.reason, "range_add_tranche")
        self.assertEqual(third.action, "hold")
        self.assertEqual(third.reason, "no_sell_signal")
        self.assertEqual(no_asia.action, "blocked")
        self.assertEqual(no_asia.reason, "asia_market_direction_unconfirmed")

    def test_range_high_sell_reduces_exactly_one_active_tranche(self) -> None:
        base = {
            "theme": "memory",
            "cn_gap_pct": 0.0,
            "has_position": True,
            "range_signal": {"regime": "range", "action": "sell"},
            "sellable_fraction": 1.0,
        }

        one = self.engine.decide(StrategyDecisionInput(**base, current_tranche_count=1))
        two = self.engine.decide(StrategyDecisionInput(**base, current_tranche_count=2))
        three = self.engine.decide(StrategyDecisionInput(**base, current_tranche_count=3))

        self.assertEqual(one.action, "exit")
        self.assertEqual(one.sell_fraction, 1.0)
        self.assertEqual(two.sell_fraction, 0.5)
        self.assertAlmostEqual(three.sell_fraction, 1.0 / 3.0, places=6)

    def test_range_buy_applies_theme_relevant_cross_market_gate(self) -> None:
        themes = (
            "semiconductor",
            "memory",
            "equipment",
            "materials",
            "cpo",
            "artificial_intelligence",
            "compute_services",
            "gaming",
            "pharma",
            "mlcc",
            "ccl",
            "gold",
        )

        for theme in themes:
            with self.subTest(theme=theme, action="buy"):
                buy = self.engine.decide(StrategyDecisionInput(
                    theme=theme,
                    cn_gap_pct=0.0,
                    range_signal={"regime": "range", "action": "buy"},
                    nasdaq_futures_signal=self._nasdaq_ready(),
                    asia_market_gate=self._asia_ready(),
                    board_technical_signal=self._board_support(),
                    expected_gross_edge_pct=2.0,
                    estimated_round_trip_cost_pct=0.4,
                ))
                self.assertEqual(
                    buy.action,
                    "buy" if theme == "pharma" else "blocked",
                )
            with self.subTest(theme=theme, action="sell"):
                sell = self.engine.decide(StrategyDecisionInput(
                    theme=theme,
                    cn_gap_pct=0.0,
                    has_position=True,
                    current_tranche_count=2,
                    sellable_fraction=1.0,
                    range_signal={"regime": "range", "action": "sell"},
                ))
                self.assertEqual(sell.action, "reduce")
                self.assertEqual(sell.reason, "range_exit_signal")
                self.assertEqual(sell.sell_fraction, 0.5)

    def test_cpo_requires_technology_markets_and_a_share_cost_gates(self) -> None:
        accepted = self.engine.decide(
            StrategyDecisionInput(
                theme="cpo",
                cn_gap_pct=-0.6,
                reclaimed_open=True,
                above_vwap=True,
                sector_signal_score=60.0,
                us_close_theme_signal=self._strong_close(),
                board_technical_signal=self._board_support(),
                us_tech_score=None,
                nasdaq_futures_signal=self._nasdaq_ready(),
                asia_market_gate=self._asia_ready(),
                korea_gate=None,
                cpo_signal={"available": True, "supportive": True, "score": 70.0},
                expected_gross_edge_pct=2.0,
                estimated_round_trip_cost_pct=0.4,
            )
        )
        high_open = self.engine.decide(
            StrategyDecisionInput(
                theme="cpo",
                cn_gap_pct=0.6,
                sector_signal_score=80.0,
                expected_gross_edge_pct=2.0,
                estimated_round_trip_cost_pct=0.4,
            )
        )
        expensive = self.engine.decide(
            StrategyDecisionInput(
                theme="cpo",
                cn_gap_pct=-0.6,
                reclaimed_open=True,
                above_vwap=True,
                sector_signal_score=60.0,
                us_close_theme_signal=self._strong_close(),
                board_technical_signal=self._board_support(),
                nasdaq_futures_signal=self._nasdaq_ready(),
                asia_market_gate=self._asia_ready(),
                cpo_signal={"available": True, "supportive": True, "score": 70.0},
                expected_gross_edge_pct=0.89,
                estimated_round_trip_cost_pct=0.4,
            )
        )

        self.assertEqual(accepted.action, "buy")
        self.assertEqual(high_open.reason, "cn_high_open_buy_blocked")
        self.assertEqual(expensive.reason, "insufficient_net_edge")

    def test_high_open_waits_at_open_but_allows_confirmed_intraday_pullback(self) -> None:
        base = {
            "theme": "memory",
            "cn_gap_pct": 0.6,
            "reclaimed_open": True,
            "above_vwap": True,
            "sector_signal_score": 72.0,
            "us_tech_score": 65.0,
            "us_close_theme_signal": self._strong_close(),
            "us_premarket_signal": self._strong_premarket(),
            "nasdaq_futures_signal": self._nasdaq_ready(),
            "asia_market_gate": self._asia_ready(),
            "board_technical_signal": self._board_support(),
            "expected_gross_edge_pct": 2.0,
            "estimated_round_trip_cost_pct": 0.4,
            "entry_score": 72.0,
        }

        opening = self.engine.decide(StrategyDecisionInput(
            **base,
            entry_phase="opening",
        ))
        unconfirmed = self.engine.decide(StrategyDecisionInput(
            **base,
            entry_phase="intraday_dip",
            analysis_slot="10:40",
            intraday_pullback_signal={
                "available": True,
                "confirmed": False,
                "pullback_from_high_pct": 0.6,
                "rebound_from_low_pct": 0.4,
            },
        ))
        confirmed = self.engine.decide(StrategyDecisionInput(
            **base,
            entry_phase="intraday_dip",
            analysis_slot="10:40",
            intraday_pullback_signal=self._pullback_ready(),
        ))

        self.assertEqual(opening.reason, "cn_high_open_buy_blocked")
        self.assertEqual(unconfirmed.reason, "cn_high_open_pullback_unconfirmed")
        self.assertEqual(confirmed.action, "buy")
        self.assertEqual(confirmed.target_position_pct, 50.0)
        self.assertEqual(
            confirmed.reason,
            "memory_premarket_close_intraday_dip_confirmed",
        )

    def test_cpo_optional_external_score_adjusts_but_does_not_replace_sector_strength(self) -> None:
        base = {
            "theme": "cpo",
            "cn_gap_pct": -0.6,
            "reclaimed_open": True,
            "above_vwap": True,
            "sector_signal_score": 38.0,
            "us_close_theme_signal": self._strong_close(),
            "nasdaq_futures_signal": self._nasdaq_ready(),
            "asia_market_gate": self._asia_ready(),
            "board_technical_signal": self._board_support(),
            "expected_gross_edge_pct": 2.0,
            "estimated_round_trip_cost_pct": 0.4,
        }

        supportive = self.engine.decide(StrategyDecisionInput(
            **base,
            cpo_signal={"available": True, "supportive": True, "score": 30.0},
        ))
        unavailable = self.engine.decide(StrategyDecisionInput(**base))
        negative = self.engine.decide(StrategyDecisionInput(
            **base,
            cpo_signal={"available": True, "supportive": False, "score": -30.0},
        ))

        self.assertEqual(supportive.action, "buy")
        self.assertEqual(unavailable.reason, "cpo_us_close_signal_unconfirmed")
        self.assertEqual(negative.reason, "cpo_us_close_signal_unconfirmed")

    def test_v14_entry_score_tiers_control_slot_and_tranche_size(self) -> None:
        base = {
            "theme": "gold",
            "cn_gap_pct": -0.5,
            "sector_signal_score": 60.0,
            "gold_signal": {
                "buy_allowed": True,
                "target_fraction": 1.0,
                "score": 80.0,
            },
            "expected_gross_edge_pct": 2.0,
            "estimated_round_trip_cost_pct": 0.4,
        }

        grade_a = self.engine.decide(StrategyDecisionInput(
            **base,
            entry_score=75.0,
            analysis_slot="09:35",
        ))
        grade_b = self.engine.decide(StrategyDecisionInput(
            **base,
            entry_score=68.0,
            analysis_slot="10:40",
        ))
        recovery_c = self.engine.decide(StrategyDecisionInput(
            **base,
            entry_score=65.0,
            analysis_slot="10:40",
        ))
        early_c = self.engine.decide(StrategyDecisionInput(
            **base,
            entry_score=65.0,
            analysis_slot="13:30",
        ))
        late_c = self.engine.decide(StrategyDecisionInput(
            **base,
            entry_score=65.0,
            analysis_slot="14:30",
        ))
        rejected = self.engine.decide(StrategyDecisionInput(
            **base,
            entry_score=64.99,
            analysis_slot="14:30",
        ))

        self.assertEqual((grade_a.entry_grade, grade_a.target_position_pct), ("A", 50.0))
        self.assertEqual((grade_b.entry_grade, grade_b.target_position_pct), ("B", 50.0))
        self.assertEqual((recovery_c.entry_grade, recovery_c.target_position_pct), ("C", 50.0))
        self.assertEqual(early_c.reason, "entry_score_late_probe_only")
        self.assertEqual((late_c.entry_grade, late_c.target_position_pct), ("C", 50.0))
        self.assertEqual(rejected.reason, "entry_score_below_threshold")

    def test_long_pressure_reduces_size_and_composite_failure_blocks(self) -> None:
        base = {
            "theme": "gold",
            "cn_gap_pct": -0.5,
            "sector_signal_score": 70.0,
            "gold_signal": {
                "buy_allowed": True,
                "target_fraction": 1.0,
                "score": 85.0,
            },
            "board_technical_signal": {
                "available": True,
                "primary_board": {
                    "name": "gold",
                    "pressure_windows": [20, 60],
                    "breakout_confirmed": False,
                },
            },
            "entry_score": 76.0,
            "analysis_slot": "10:40",
            "expected_gross_edge_pct": 2.0,
            "estimated_round_trip_cost_pct": 0.4,
        }

        reduced = self.engine.decide(StrategyDecisionInput(
            **base,
            failed_breakout_signal={"confirmed": False},
        ))
        blocked = self.engine.decide(StrategyDecisionInput(
            **base,
            failed_breakout_signal={
                "confirmed": True,
                "reason": "long_pressure_failed_breakout_below_vwap",
            },
        ))

        self.assertEqual(reduced.action, "buy")
        self.assertEqual(reduced.target_position_pct, 50.0)
        self.assertEqual(blocked.action, "blocked")
        self.assertEqual(blocked.reason, "long_pressure_failed_breakout_below_vwap")

    def test_high_open_wait_does_not_mask_take_profit_or_range_exit(self) -> None:
        waiting = {
            "eligible": True,
            "action": "hold",
            "reason": "next_day_high_open_waiting_for_intraday_high",
        }
        take_profit = self.engine.decide(StrategyDecisionInput(
            theme="gold",
            cn_gap_pct=0.8,
            has_position=True,
            sellable_fraction=1.0,
            position_return_pct=8.1,
            next_day_high_open_exit_signal=waiting,
        ))
        range_exit = self.engine.decide(StrategyDecisionInput(
            theme="gold",
            cn_gap_pct=0.8,
            has_position=True,
            current_tranche_count=2,
            sellable_fraction=1.0,
            position_return_pct=1.0,
            pullback_from_peak_pct=5.0,
            next_day_high_open_exit_signal=waiting,
            range_signal={"regime": "range", "action": "sell"},
        ))

        self.assertEqual(take_profit.reason, "take_profit")
        self.assertEqual(range_exit.reason, "range_exit_signal")

    def test_account_risk_fuses_and_position_capacity_are_enforced(self) -> None:
        fused = self.engine.decide(
            StrategyDecisionInput(
                theme="gold",
                cn_gap_pct=-0.5,
                has_position=True,
                sellable_fraction=0.5,
                risk=AccountRiskState(account_drawdown_pct=8.0),
            )
        )
        fused_hard_stop = self.engine.decide(
            StrategyDecisionInput(
                theme="gold",
                cn_gap_pct=-0.5,
                has_position=True,
                sellable_fraction=1.0,
                position_return_pct=-5.0,
                risk=AccountRiskState(account_drawdown_pct=8.0),
            )
        )
        blocked = self.engine.decide(
            StrategyDecisionInput(
                theme="cpo",
                cn_gap_pct=-0.5,
                reclaimed_open=True,
                above_vwap=True,
                sector_signal_score=70.0,
                expected_gross_edge_pct=3.0,
                risk=AccountRiskState(position_count=3),
            )
        )
        daily_loss_fused = self.engine.decide(
            StrategyDecisionInput(
                theme="cpo",
                cn_gap_pct=-0.5,
                reclaimed_open=True,
                above_vwap=True,
                sector_signal_score=70.0,
                expected_gross_edge_pct=3.0,
                risk=AccountRiskState(daily_pnl_pct=-1.5),
            )
        )

        self.assertEqual(fused.reason, "account_drawdown_fuse")
        self.assertEqual(fused.action, "hold")
        self.assertEqual(fused.sell_fraction, 0.0)
        self.assertEqual(fused_hard_stop.action, "exit")
        self.assertEqual(fused_hard_stop.reason, "hard_stop_loss")
        self.assertEqual(blocked.reason, "position_count_limit")
        self.assertEqual(daily_loss_fused.action, "blocked")
        self.assertEqual(daily_loss_fused.reason, "daily_loss_fuse")

    def test_exposure_and_consecutive_loss_limits_block_new_entries(self) -> None:
        risk_cases = (
            (AccountRiskState(total_exposure_pct=100.0), "total_exposure_limit"),
            (AccountRiskState(theme_exposure_pct=100.0), "theme_exposure_limit"),
            (AccountRiskState(symbol_exposure_pct=50.0), "symbol_exposure_limit"),
            (AccountRiskState(consecutive_losses=3), "loss_streak_cooldown"),
            (AccountRiskState(in_loss_streak_cooldown=True), "loss_streak_cooldown"),
        )

        for risk, expected_reason in risk_cases:
            with self.subTest(expected_reason=expected_reason, risk=risk):
                decision = self.engine.decide(StrategyDecisionInput(
                    theme="cpo",
                    cn_gap_pct=-0.5,
                    reclaimed_open=True,
                    sector_signal_score=70.0,
                    expected_gross_edge_pct=3.0,
                    risk=risk,
                ))
                self.assertEqual(decision.action, "blocked")
                self.assertEqual(decision.reason, expected_reason)

    def test_gold_requires_five_twenty_day_trend_and_time_bounded_rate_cut_news(self) -> None:
        news = self.engine.calculate_rate_cut_news_score(
            [
                {
                    "title": "Fed turns dovish as markets expect rate cut",
                    "published_at": NOW - timedelta(hours=2),
                },
                {
                    "title": "Old rate cut story",
                    "published_at": NOW - timedelta(days=3),
                },
                {
                    "title": "Fed says no rate cut",
                    "published_at": NOW - timedelta(hours=1),
                },
            ],
            window_start=NOW - timedelta(hours=12),
            window_end=NOW,
        )
        rejected_trend = self.engine.calculate_gold_score(
            overnight_return_pct=1.3,
            five_day_return_pct=2.0,
            above_ma20=True,
            ma5_above_ma20=False,
            rate_cut_news_score=80.0,
        )

        self.assertEqual(news["positive_count"], 1)
        self.assertEqual(news["negative_count"], 1)
        self.assertEqual(news["score"], -10.0)
        self.assertFalse(rejected_trend["buy_allowed"])

    def test_gold_news_treats_delayed_cuts_and_rising_real_yields_as_negative(self) -> None:
        news = self.engine.calculate_rate_cut_news_score(
            [{
                "title": "Fed may delay rate cuts as real yields rise",
                "published_at": NOW - timedelta(hours=1),
            }],
            window_start=NOW - timedelta(hours=12),
            window_end=NOW,
        )

        self.assertEqual(news["positive_count"], 0)
        self.assertEqual(news["negative_count"], 1)
        self.assertEqual(news["score"], -45.0)

    def test_range_indicators_drive_low_buy(self) -> None:
        closes = [10.0] * 27 + [9.8]
        highs = [value + 0.1 for value in closes]
        lows = [value - 0.1 for value in closes]

        signal = self.engine.calculate_range_indicators(
            highs=highs,
            lows=lows,
            closes=closes,
            intraday_vwap=9.7,
        )

        self.assertLess(signal["adx14"], 20.0)
        self.assertLessEqual(signal["rsi14"], 35.0)
        self.assertEqual(signal["regime"], "range")
        self.assertEqual(signal["action"], "buy")
        self.assertEqual(signal["tranche_delta"], 1)

    def test_theme_classifier_prioritizes_cpo_over_semiconductor(self) -> None:
        self.assertEqual(
            self.engine.classify_theme({"industry": "半导体", "concept": "CPO 光模块"}),
            "cpo",
        )
        self.assertEqual(self.engine.classify_theme("HBM 存储芯片"), "memory")
        self.assertEqual(
            self.engine.classify_theme("人工智能大模型应用"),
            "artificial_intelligence",
        )
        self.assertEqual(self.engine.classify_theme("AI算力租赁"), "compute_services")
        self.assertEqual(self.engine.classify_theme("网络游戏"), "gaming")
        self.assertEqual(self.engine.classify_theme("光纤光通信"), "cpo")
        self.assertEqual(self.engine.classify_theme("创新药 CRO"), "pharma")
        self.assertEqual(self.engine.classify_theme("ADC药物"), "pharma")
        self.assertEqual(self.engine.classify_theme("AIDC"), "compute_services")
        self.assertEqual(self.engine.classify_theme("MicroLED"), "semiconductor")
        self.assertEqual(self.engine.classify_theme("microcontroller"), "semiconductor")
        self.assertEqual(self.engine.classify_theme("macro economy"), "other")
        self.assertEqual(self.engine.classify_theme("MLCC 被动元件"), "mlcc")
        self.assertEqual(self.engine.classify_theme("覆铜板 CCL"), "ccl")
        self.assertEqual(self.engine.classify_theme("黄金概念"), "gold")
        self.assertEqual(self.engine.classify_theme("食品饮料"), "consumer")
        self.assertEqual(self.engine.classify_theme("银行"), "bank")
        self.assertEqual(self.engine.classify_theme("公用事业"), "utilities")
        self.assertEqual(self.engine.classify_theme("煤炭"), "energy")
        self.assertEqual(self.engine.classify_theme("机械设备"), "industrials")
        self.assertEqual(self.engine.classify_theme("消费电子"), "other")
        self.assertEqual(self.engine.classify_theme("新能源"), "other")
        self.assertEqual(self.engine.classify_theme("电力设备"), "other")
        self.assertEqual(self.engine.classify_theme("工业金属"), "other")
        self.assertEqual(self.engine.classify_theme("铜 铝 有色金属"), "other")

    def test_theme_classifier_disambiguates_semiconductor_materials(self) -> None:
        self.assertEqual(self.engine.classify_theme("\u5149\u523b\u80f6"), "materials")
        self.assertEqual(self.engine.classify_theme("\u534a\u5bfc\u4f53\u7845\u7247"), "materials")
        self.assertEqual(self.engine.classify_theme("\u7845\u6599\u7845\u7247"), "other")

    def test_theme_classifier_prefers_tradeable_bank_over_gold_concept(self) -> None:
        self.assertEqual(
            self.engine.classify_theme(
                {"industry": "\u94f6\u884c", "concept": "\u9ec4\u91d1\u6982\u5ff5"},
            ),
            "bank",
        )
        self.assertEqual(
            self.engine.classify_theme(
                {
                    "_cross_market_source_theme": "memory",
                    "industry": "bank",
                    "concept": "gold",
                }
            ),
            "memory",
        )


class RealisticMinuteExecutionModelTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.model = RealisticMinuteExecutionModel()
        self.bar = MinuteBar(
            timestamp=NOW + timedelta(minutes=1),
            open=10.00,
            high=10.10,
            low=9.95,
            close=10.05,
            volume=10_000,
            amount=100_200,
            bid_ask_spread_bps=4,
            atr_1m_pct=0.2,
        )

    def test_stock_fees_include_minimum_commission_stamp_tax_and_transfer(self) -> None:
        schedule = TradeFeeSchedule()

        buy = schedule.calculate(side="buy", notional=10_000)
        sell = schedule.calculate(side="sell", notional=10_000)

        self.assertEqual(buy, {"commission": 5.0, "stamp_tax": 0.0, "transfer_fee": 0.1, "total": 5.1})
        self.assertEqual(sell, {"commission": 5.0, "stamp_tax": 5.0, "transfer_fee": 0.1, "total": 10.1})
        self.assertAlmostEqual(
            schedule.estimate_round_trip_cost_pct(
                notional=10_000,
                buy_slippage_bps=8,
                sell_slippage_bps=8,
            ),
            0.312,
        )

        large_buy = schedule.calculate(side="buy", notional=100_000)
        large_sell = schedule.calculate(side="sell", notional=100_000)
        self.assertEqual(
            large_buy,
            {
                "commission": 8.0,
                "stamp_tax": 0.0,
                "transfer_fee": 1.0,
                "total": 9.0,
            },
        )
        self.assertEqual(
            large_sell,
            {
                "commission": 8.0,
                "stamp_tax": 50.0,
                "transfer_fee": 1.0,
                "total": 59.0,
            },
        )

    def test_buy_notional_reserves_commission_and_transfer_fee(self) -> None:
        schedule = TradeFeeSchedule()

        stock_notional = schedule.max_buy_notional_for_cash_budget(
            cash_budget=50_000,
            instrument_type="stock",
        )
        stock_fees = schedule.calculate(
            side="buy",
            notional=stock_notional,
            instrument_type="stock",
        )
        etf_notional = schedule.max_buy_notional_for_cash_budget(
            cash_budget=50_000,
            instrument_type="etf",
        )
        etf_fees = schedule.calculate(
            side="buy",
            notional=etf_notional,
            instrument_type="etf",
        )

        self.assertLessEqual(stock_notional + stock_fees["total"], 50_000)
        self.assertGreater(stock_notional, 49_994)
        self.assertLessEqual(etf_notional + etf_fees["total"], 50_000)
        self.assertEqual(etf_notional, 49_995.0)
        next_cent = stock_notional + 0.01
        self.assertGreater(
            next_cent
            + schedule.calculate(
                side="buy",
                notional=next_cent,
                instrument_type="stock",
            )["total"],
            50_000,
        )

    def test_etf_fees_exclude_stock_stamp_tax_and_transfer_fee(self) -> None:
        schedule = TradeFeeSchedule()

        buy = schedule.calculate(side="buy", notional=10_000, instrument_type="etf")
        sell = schedule.calculate(side="sell", notional=10_000, instrument_type="etf")

        expected = {
            "commission": 5.0,
            "stamp_tax": 0.0,
            "transfer_fee": 0.0,
            "total": 5.0,
        }
        self.assertEqual(buy, expected)
        self.assertEqual(sell, expected)

    def test_dynamic_slippage_is_clamped_to_two_and_fifty_bps(self) -> None:
        quiet_bar = MinuteBar(
            timestamp=NOW + timedelta(minutes=1),
            open=10.0,
            high=10.0,
            low=10.0,
            close=10.0,
            volume=1_000_000,
            amount=10_000_000,
            bid_ask_spread_bps=0.0,
            atr_1m_pct=0.0,
        )
        stressed_bar = MinuteBar(
            timestamp=NOW + timedelta(minutes=1),
            open=10.0,
            high=11.0,
            low=9.0,
            close=10.0,
            volume=1_000,
            amount=10_000,
            bid_ask_spread_bps=200.0,
            atr_1m_pct=10.0,
        )

        self.assertEqual(
            self.model._slippage_bps(bar=quiet_bar, participation=0.0),
            2.0,
        )
        self.assertEqual(
            self.model._slippage_bps(bar=stressed_bar, participation=1.0),
            50.0,
        )

    def test_dynamic_slippage_preserves_observed_zero_spread(self) -> None:
        bar = MinuteBar(
            timestamp=NOW + timedelta(minutes=1),
            open=10.0,
            high=10.0,
            low=10.0,
            close=10.0,
            volume=10_000,
            amount=100_000,
            bid_ask_spread_bps=0.0,
            atr_1m_pct=0.0,
        )

        self.assertEqual(
            self.model._slippage_bps(bar=bar, participation=0.01),
            2.0,
        )

    def test_execution_uses_next_bar_dynamic_slippage_and_partially_fills(self) -> None:
        order = PaperOrder(
            symbol="600000",
            side="buy",
            quantity=1_000,
            signal_at=NOW,
            limit_price=10.10,
        )

        fill = self.model.execute(order, self.bar)

        self.assertEqual(fill.status, "part_filled")
        self.assertEqual(fill.filled_quantity, 500)
        self.assertEqual(fill.unfilled_quantity, 500)
        self.assertGreater(fill.fill_price or 0, fill.reference_price or 0)
        self.assertGreater(fill.fees["total"], 0)

    def test_execution_allows_integer_share_partial_fill_after_valid_cn_declaration(self) -> None:
        bar = MinuteBar(
            timestamp=NOW + timedelta(minutes=1),
            open=10.0,
            high=10.0,
            low=10.0,
            close=10.0,
            volume=1_000,
            amount=10_000,
        )

        fill = self.model.execute(
            PaperOrder(
                symbol="600000",
                side="buy",
                quantity=100,
                declared_quantity=100,
                signal_at=NOW,
            ),
            bar,
        )

        self.assertEqual(fill.status, "part_filled")
        self.assertEqual(fill.filled_quantity, 50.0)
        self.assertEqual(fill.unfilled_quantity, 50.0)

    def test_execution_rejects_invalid_cn_buy_declaration_quantity(self) -> None:
        regular = self.model.execute(
            PaperOrder(
                symbol="600000",
                side="buy",
                quantity=150,
                signal_at=NOW,
            ),
            self.bar,
        )
        star = self.model.execute(
            PaperOrder(
                symbol="688981",
                side="buy",
                quantity=199,
                signal_at=NOW,
            ),
            self.bar,
        )

        self.assertEqual(regular.reason, "below_cn_board_lot")
        self.assertEqual(star.reason, "below_cn_board_lot")

    def test_execution_applies_bse_declaration_and_price_limit_rules(self) -> None:
        valid = self.model.execute(
            PaperOrder(
                symbol="920045",
                side="buy",
                quantity=105,
                declared_quantity=105,
                signal_at=NOW,
                previous_close=10.0,
            ),
            self.bar,
        )
        invalid = self.model.execute(
            PaperOrder(
                symbol="920045",
                side="buy",
                quantity=99,
                declared_quantity=99,
                signal_at=NOW,
            ),
            self.bar,
        )

        self.assertNotEqual(valid.reason, "below_cn_board_lot")
        self.assertEqual(invalid.reason, "below_cn_board_lot")
        self.assertEqual(self.model._cn_price_limit_pct("920045"), 30.0)
        self.assertEqual(self.model._cn_price_limit_pct("900901"), 10.0)

    def test_execution_enforces_t_plus_one_limits_and_signal_time(self) -> None:
        sell = PaperOrder(
            symbol="600000",
            side="sell",
            quantity=500,
            signal_at=NOW,
            sellable_quantity=0,
        )
        same_bar = PaperOrder(symbol="600000", side="buy", quantity=100, signal_at=self.bar.timestamp)

        self.assertEqual(self.model.execute(sell, self.bar).reason, "t_plus_one_no_sellable_quantity")
        self.assertEqual(self.model.execute(same_bar, self.bar).reason, "bar_not_after_signal")

    def test_execution_rejects_unreached_limit_and_one_price_limit(self) -> None:
        limit_order = PaperOrder(
            symbol="600000",
            side="buy",
            quantity=100,
            signal_at=NOW,
            limit_price=9.90,
        )
        one_price_bar = MinuteBar(
            timestamp=NOW + timedelta(minutes=1),
            open=11,
            high=11,
            low=11,
            close=11,
            volume=100_000,
            limit_up_price=11,
        )

        self.assertEqual(self.model.execute(limit_order, self.bar).reason, "limit_not_touched")
        self.assertEqual(
            self.model.execute(
                PaperOrder(symbol="600000", side="buy", quantity=100, signal_at=NOW),
                one_price_bar,
            ).reason,
            "one_price_limit_no_liquidity",
        )

    def test_execution_supports_cancel_and_board_specific_price_limits(self) -> None:
        cancelled = self.model.execute(
            PaperOrder(
                symbol="600000",
                side="buy",
                quantity=100,
                signal_at=NOW,
                cancel_requested=True,
            ),
            self.bar,
        )
        star_limit_bar = MinuteBar(
            timestamp=NOW + timedelta(minutes=1),
            open=12.0,
            high=12.0,
            low=12.0,
            close=12.0,
            volume=100_000,
        )
        star_limit = self.model.execute(
            PaperOrder(
                symbol="688001",
                side="buy",
                quantity=200,
                signal_at=NOW,
                previous_close=10.0,
            ),
            star_limit_bar,
        )
        main_board_same_price = self.model.execute(
            PaperOrder(
                symbol="600000",
                side="buy",
                quantity=100,
                signal_at=NOW,
                previous_close=10.0,
            ),
            star_limit_bar,
        )
        explicit_five_percent_limit = self.model.execute(
            PaperOrder(
                symbol="600000",
                side="buy",
                quantity=100,
                signal_at=NOW,
                previous_close=10.0,
                price_limit_pct=5.0,
            ),
            MinuteBar(
                timestamp=NOW + timedelta(minutes=1),
                open=10.5,
                high=10.5,
                low=10.5,
                close=10.5,
                volume=100_000,
            ),
        )

        self.assertEqual(cancelled.status, "cancelled")
        self.assertEqual(cancelled.reason, "order_cancelled_before_execution")
        self.assertEqual(star_limit.reason, "one_price_limit_no_liquidity")
        self.assertEqual(main_board_same_price.reason, "minute_bar_outside_price_limit")
        self.assertEqual(explicit_five_percent_limit.reason, "one_price_limit_no_liquidity")


if __name__ == "__main__":
    unittest.main()
