# -*- coding: utf-8 -*-
"""Tests for point-in-time portfolio strategy backtesting."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date
from unittest.mock import MagicMock

from src.config import Config
from src.services.stock_selection_portfolio_backtest_service import (
    StockSelectionPortfolioBacktestService,
)
from src.storage import DatabaseManager, StockDaily


class StockSelectionPortfolioBacktestServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "portfolio_replay.db")
        self.env_path = os.path.join(self.temp_dir.name, ".env")
        with open(self.env_path, "w", encoding="utf-8") as env_file:
            env_file.write("STOCK_LIST=600519,000001\n")
        self.original_env = {key: os.environ.get(key) for key in ("ENV_FILE", "DATABASE_PATH")}
        os.environ["ENV_FILE"] = self.env_path
        os.environ["DATABASE_PATH"] = self.db_path
        Config.reset_instance()
        DatabaseManager.reset_instance()
        self.db = DatabaseManager.get_instance()
        self.repository = MagicMock()
        self.replay = MagicMock()
        self.service = StockSelectionPortfolioBacktestService(
            db_manager=self.db,
            repository=self.repository,
            replay_service=self.replay,
        )

    def tearDown(self) -> None:
        DatabaseManager.reset_instance()
        Config.reset_instance()
        for key, value in self.original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temp_dir.cleanup()

    def _seed_bars(self) -> None:
        with self.db.get_session() as session:
            session.add_all([
                StockDaily(code="600519", date=date(2024, 1, 2), open=100, close=102),
                StockDaily(code="600519", date=date(2024, 1, 4), open=110, close=111),
                StockDaily(code="000001", date=date(2024, 1, 4), open=50, close=51),
                StockDaily(code="000001", date=date(2024, 1, 5), open=52, close=55),
                StockDaily(code="000300", date=date(2024, 1, 2), open=4000, close=4010),
                StockDaily(code="000300", date=date(2024, 1, 4), open=4040, close=4050),
                StockDaily(code="000300", date=date(2024, 1, 5), open=4060, close=4080),
            ])
            session.commit()

    def test_run_compounds_periods_with_costs_and_benchmark(self) -> None:
        self._seed_bars()
        self.repository.list_dates.return_value = [date(2024, 1, 1), date(2024, 1, 3)]
        self.replay.replay.side_effect = [
            {
                "candidates": [{"symbol": "600519", "name": "贵州茅台", "screen_score": 90}],
                "compatibility": {"hard_coverage_ratio": 1, "score_coverage_ratio": 1},
            },
            {
                "candidates": [{"symbol": "000001", "name": "平安银行", "screen_score": 80}],
                "compatibility": {"hard_coverage_ratio": 1, "score_coverage_ratio": 1},
            },
        ]

        result = self.service.run(
            strategy="dual_low",
            market="cn",
            date_from=date(2024, 1, 1),
            date_to=date(2024, 1, 3),
            top_k=1,
            final_holding_bars=2,
            initial_capital=100_000,
            commission_bps=10,
            slippage_bps=10,
            benchmark_symbol="000300",
        )

        self.assertEqual(result["snapshot_count"], 2)
        self.assertEqual(result["coverage_pct"], 100)
        self.assertGreater(result["final_equity"], 120_000)
        self.assertLess(result["final_equity"], 121_000)
        self.assertGreater(result["metrics"]["benchmark_return_pct"], 0)
        self.assertGreater(result["metrics"]["excess_return_pct"], 0)
        self.assertEqual(result["periods"][0]["holdings"][0]["entry_date"], date(2024, 1, 2))
        self.assertEqual(result["periods"][0]["holdings"][0]["exit_date"], date(2024, 1, 4))
        self.assertEqual(result["periods"][1]["holdings"][0]["exit_mode"], "final_horizon_close")
        self.assertTrue(result["methodology"]["lookahead_protection"])
        self.assertFalse(result["methodology"]["places_orders"])

    def test_missing_bars_reduce_coverage_without_current_price_fallback(self) -> None:
        self.repository.list_dates.return_value = [date(2024, 1, 1)]
        self.replay.replay.return_value = {
            "candidates": [{"symbol": "999999", "screen_score": 80}],
            "compatibility": {},
        }

        result = self.service.run(
            strategy="dual_low",
            market="cn",
            date_from=date(2024, 1, 1),
            date_to=date(2024, 1, 1),
            final_holding_bars=1,
        )

        self.assertEqual(result["coverage_pct"], 0)
        self.assertEqual(result["periods"][0]["holdings"][0]["reason"], "missing_entry_bar")
        self.assertIsNone(result["metrics"]["benchmark_return_pct"])
        self.assertIsNone(result["metrics"]["excess_return_pct"])

    def test_overlapping_symbols_are_retained_without_repeated_costs(self) -> None:
        with self.db.get_session() as session:
            session.add_all([
                StockDaily(code="600519", date=date(2024, 1, 2), open=100, close=101, volume=1000, pct_chg=1),
                StockDaily(code="600519", date=date(2024, 1, 4), open=110, close=111, volume=1000, pct_chg=10),
                StockDaily(code="600519", date=date(2024, 1, 6), open=120, close=121, volume=1000, pct_chg=1),
                StockDaily(code="600519", date=date(2024, 1, 7), open=125, close=130, volume=1000, pct_chg=1),
            ])
            session.commit()
        self.repository.list_dates.return_value = [
            date(2024, 1, 1),
            date(2024, 1, 3),
            date(2024, 1, 5),
        ]
        replay = {
            "candidates": [{"symbol": "600519", "name": "贵州茅台", "screen_score": 90}],
            "compatibility": {},
        }
        self.replay.replay.side_effect = [replay, replay, replay]

        result = self.service.run(
            strategy="dual_low",
            market="cn",
            date_from=date(2024, 1, 1),
            date_to=date(2024, 1, 5),
            top_k=1,
            final_holding_bars=2,
            commission_bps=10,
            slippage_bps=10,
        )

        self.assertEqual([item["turnover_pct"] for item in result["periods"]], [100, 0, 100])
        self.assertTrue(result["periods"][0]["holdings"][0]["entry_trade"])
        self.assertFalse(result["periods"][0]["holdings"][0]["exit_trade"])
        self.assertTrue(result["periods"][1]["holdings"][0]["retained"])
        self.assertFalse(result["periods"][1]["holdings"][0]["exit_trade"])
        self.assertTrue(result["periods"][2]["holdings"][0]["exit_trade"])
        self.assertEqual(result["metrics"]["total_turnover_pct"], 200)
        self.assertEqual(
            result["methodology"]["rebalance"],
            "retain_overlapping_symbols_equal_weight_approximation",
        )

    def test_tradeability_gate_blocks_suspension_and_price_limits(self) -> None:
        with self.db.get_session() as session:
            session.add_all([
                StockDaily(code="600001", date=date(2024, 1, 2), open=10, close=10, volume=1000, pct_chg=10),
                StockDaily(code="600001", date=date(2024, 1, 3), open=10, close=10, volume=1000, pct_chg=0),
                StockDaily(code="600002", date=date(2024, 1, 2), open=10, close=10, volume=0, pct_chg=0),
                StockDaily(code="600002", date=date(2024, 1, 3), open=10, close=10, volume=1000, pct_chg=0),
                StockDaily(code="600003", date=date(2024, 1, 2), open=10, close=10, volume=1000, pct_chg=0),
                StockDaily(code="600003", date=date(2024, 1, 3), open=9, close=9, volume=1000, pct_chg=-10),
            ])
            session.commit()
        self.repository.list_dates.return_value = [date(2024, 1, 1)]
        self.replay.replay.return_value = {
            "candidates": [
                {"symbol": "600001", "name": "涨停股", "screen_score": 90},
                {"symbol": "600002", "name": "停牌股", "screen_score": 80},
                {"symbol": "600003", "name": "跌停股", "screen_score": 70},
            ],
            "compatibility": {},
        }

        result = self.service.run(
            strategy="dual_low",
            market="cn",
            date_from=date(2024, 1, 1),
            date_to=date(2024, 1, 1),
            top_k=3,
            final_holding_bars=2,
        )

        reasons = {item["symbol"]: item["reason"] for item in result["periods"][0]["holdings"]}
        self.assertEqual(reasons["600001"], "entry_price_limit_up")
        self.assertEqual(reasons["600002"], "entry_suspended")
        self.assertEqual(reasons["600003"], "exit_price_limit_down")
        self.assertAlmostEqual(result["coverage_pct"], 100 / 3, places=4)
        forced = result["periods"][0]["holdings"][2]
        self.assertEqual(forced["status"], "forced_retained")
        self.assertFalse(forced["exit_trade"])
        self.assertTrue(forced["exit_trade_requested"])
        self.assertEqual(result["periods"][0]["forced_retained_count"], 1)
        self.assertEqual(result["metrics"]["ending_open_position_count"], 1)

    def test_blocked_exit_is_carried_to_the_next_rebalance(self) -> None:
        with self.db.get_session() as session:
            session.add_all([
                StockDaily(code="600003", date=date(2024, 1, 2), open=10, close=10, volume=1000, pct_chg=0),
                StockDaily(code="600003", date=date(2024, 1, 4), open=9, close=9, volume=1000, pct_chg=-10),
                StockDaily(code="600003", date=date(2024, 1, 6), open=8, close=8, volume=1000, pct_chg=-1),
            ])
            session.commit()
        self.repository.list_dates.return_value = [
            date(2024, 1, 1),
            date(2024, 1, 3),
            date(2024, 1, 5),
        ]
        self.replay.replay.side_effect = [
            {"candidates": [{"symbol": "600003", "name": "blocked", "screen_score": 70}], "compatibility": {}},
            {"candidates": [], "compatibility": {}},
            {"candidates": [], "compatibility": {}},
        ]

        result = self.service.run(
            strategy="dual_low",
            market="cn",
            date_from=date(2024, 1, 1),
            date_to=date(2024, 1, 5),
            top_k=1,
            final_holding_bars=1,
        )

        first, second, third = result["periods"]
        self.assertEqual(first["holdings"][0]["status"], "forced_retained")
        self.assertEqual(first["forced_retained_count"], 1)
        self.assertEqual(second["selected_count"], 0)
        self.assertEqual(second["holding_count"], 1)
        self.assertTrue(second["holdings"][0]["exit_trade"])
        self.assertEqual(second["exit_trade_count"], 1)
        self.assertEqual(third["holding_count"], 0)
        self.assertEqual(result["metrics"]["ending_open_position_count"], 0)

    def test_rejects_empty_ranges_and_invalid_parameters(self) -> None:
        self.repository.list_dates.return_value = []
        with self.assertRaisesRegex(ValueError, "no point-in-time factor snapshots"):
            self.service.run(
                strategy="dual_low",
                market="cn",
                date_from=date(2024, 1, 1),
                date_to=date(2024, 1, 2),
            )
        with self.assertRaisesRegex(ValueError, "date_from"):
            self.service.run(
                strategy="dual_low",
                market="cn",
                date_from=date(2024, 1, 2),
                date_to=date(2024, 1, 1),
            )

    def test_cash_ledger_uses_whole_lots_and_available_cash(self) -> None:
        with self.db.get_session() as session:
            session.add_all([
                StockDaily(code="600001", date=date(2024, 1, 2), open=10, close=10, volume=1000, pct_chg=0),
                StockDaily(code="600001", date=date(2024, 1, 3), open=11, close=11, volume=1000, pct_chg=1),
            ])
            session.commit()
        self.repository.list_dates.return_value = [date(2024, 1, 1)]
        self.replay.replay.return_value = {
            "candidates": [{"symbol": "600001", "name": "sample", "screen_score": 90}],
            "compatibility": {},
        }

        result = self.service.run(
            strategy="dual_low",
            market="cn",
            date_from=date(2024, 1, 1),
            date_to=date(2024, 1, 1),
            final_holding_bars=2,
            commission_bps=10,
            slippage_bps=10,
            accounting_mode="cash_ledger",
        )

        buys = [item for item in result["periods"][0]["trades"] if item["side"] == "buy"]
        self.assertEqual(len(buys), 1)
        self.assertEqual(buys[0]["quantity"] % 100, 0)
        self.assertGreaterEqual(result["metrics"]["ending_cash"], 0)
        self.assertEqual(result["metrics"]["ending_open_position_count"], 0)
        self.assertEqual(result["methodology"]["accounting_mode"], "cash_ledger")
        self.assertEqual(result["final_equity"], result["metrics"]["ending_cash"])

    def test_cash_ledger_keeps_limit_down_position_open_at_final_horizon(self) -> None:
        with self.db.get_session() as session:
            session.add_all([
                StockDaily(code="600003", date=date(2024, 1, 2), open=10, close=10, volume=1000, pct_chg=0),
                StockDaily(code="600003", date=date(2024, 1, 3), open=9, close=9, volume=1000, pct_chg=-10),
            ])
            session.commit()
        self.repository.list_dates.return_value = [date(2024, 1, 1)]
        self.replay.replay.return_value = {
            "candidates": [{"symbol": "600003", "name": "blocked", "screen_score": 90}],
            "compatibility": {},
        }

        result = self.service.run(
            strategy="dual_low",
            market="cn",
            date_from=date(2024, 1, 1),
            date_to=date(2024, 1, 1),
            final_holding_bars=2,
            accounting_mode="cash_ledger",
        )

        self.assertEqual(result["metrics"]["ending_open_position_count"], 1)
        self.assertGreater(result["metrics"]["ending_market_value"], 0)
        self.assertIn(
            {"symbol": "600003", "side": "sell", "reason": "exit_price_limit_down"},
            result["periods"][0]["blocked_trades"],
        )

    def test_cash_ledger_rebalances_drifted_positions_to_equal_weight_lots(self) -> None:
        with self.db.get_session() as session:
            session.add_all([
                StockDaily(code="600001", date=date(2024, 1, 2), open=10, close=10, volume=1000, pct_chg=0),
                StockDaily(code="600001", date=date(2024, 1, 4), open=20, close=20, volume=1000, pct_chg=1),
                StockDaily(code="600001", date=date(2024, 1, 5), open=20, close=20, volume=1000, pct_chg=0),
                StockDaily(code="600002", date=date(2024, 1, 2), open=10, close=10, volume=1000, pct_chg=0),
                StockDaily(code="600002", date=date(2024, 1, 4), open=5, close=5, volume=1000, pct_chg=-1),
                StockDaily(code="600002", date=date(2024, 1, 5), open=5, close=5, volume=1000, pct_chg=0),
            ])
            session.commit()
        self.repository.list_dates.return_value = [date(2024, 1, 1), date(2024, 1, 3)]
        replay = {
            "candidates": [
                {"symbol": "600001", "name": "winner", "screen_score": 90},
                {"symbol": "600002", "name": "laggard", "screen_score": 80},
            ],
            "compatibility": {},
        }
        self.replay.replay.side_effect = [replay, replay]

        result = self.service.run(
            strategy="dual_low",
            market="cn",
            date_from=date(2024, 1, 1),
            date_to=date(2024, 1, 3),
            top_k=2,
            final_holding_bars=2,
            accounting_mode="cash_ledger",
        )

        rebalance_trades = result["periods"][1]["trades"]
        self.assertTrue(any(item["symbol"] == "600001" and item["side"] == "sell" for item in rebalance_trades))
        self.assertTrue(any(item["symbol"] == "600002" and item["side"] == "buy" for item in rebalance_trades))
        self.assertTrue(all(item["quantity"] % 100 == 0 for item in rebalance_trades))
        holdings = {item["symbol"]: item for item in result["periods"][1]["holdings"]}
        self.assertLessEqual(abs(holdings["600001"]["market_value"] - holdings["600002"]["market_value"]), 2000)

    def test_cash_ledger_applies_explicit_target_weights_and_keeps_residual_cash(self) -> None:
        with self.db.get_session() as session:
            session.add_all([
                StockDaily(code="600001", date=date(2024, 1, 2), open=10, close=10, volume=1000, pct_chg=0),
                StockDaily(code="600001", date=date(2024, 1, 3), open=10, close=10, volume=1000, pct_chg=0),
                StockDaily(code="600002", date=date(2024, 1, 2), open=20, close=20, volume=1000, pct_chg=0),
                StockDaily(code="600002", date=date(2024, 1, 3), open=20, close=20, volume=1000, pct_chg=0),
            ])
            session.commit()
        self.repository.list_dates.return_value = [date(2024, 1, 1)]
        self.replay.replay.return_value = {
            "candidates": [
                {"symbol": "600001", "name": "primary", "screen_score": 90},
                {"symbol": "600002", "name": "secondary", "screen_score": 80},
                {"symbol": "600003", "name": "unconfigured", "screen_score": 70},
            ],
            "compatibility": {},
        }

        result = self.service.run(
            strategy="dual_low",
            market="cn",
            date_from=date(2024, 1, 1),
            date_to=date(2024, 1, 1),
            top_k=3,
            final_holding_bars=2,
            initial_capital=100_000,
            commission_bps=0,
            slippage_bps=0,
            accounting_mode="cash_ledger",
            target_weights={"600001": 60, "600002": 20, "600004": 10},
        )

        period = result["periods"][0]
        buys = {item["symbol"]: item for item in period["trades"] if item["side"] == "buy"}
        holdings = {item["symbol"]: item for item in period["holdings"]}
        self.assertEqual(buys["600001"]["quantity"], 6000)
        self.assertEqual(buys["600002"]["quantity"], 1000)
        self.assertNotIn("600003", buys)
        self.assertEqual(holdings["600001"]["target_weight_pct"], 60)
        self.assertEqual(holdings["600002"]["target_weight_pct"], 20)
        self.assertEqual(period["target_weights"], {"600001": 60.0, "600002": 20.0})
        self.assertEqual(period["configured_targets_not_selected"], ["600004"])
        self.assertEqual(result["methodology"]["target_weight_mode"], "explicit_symbol_weights")
        self.assertEqual(result["methodology"]["configured_target_weights"]["600004"], 10.0)
        self.assertEqual(result["final_equity"], 100_000)

    def test_cash_ledger_applies_minimum_commission_and_sell_tax(self) -> None:
        with self.db.get_session() as session:
            session.add_all([
                StockDaily(code="600001", date=date(2024, 1, 2), open=1, close=1, volume=1000, pct_chg=0),
                StockDaily(code="600001", date=date(2024, 1, 3), open=1, close=1, volume=1000, pct_chg=0),
            ])
            session.commit()
        self.repository.list_dates.return_value = [date(2024, 1, 1)]
        self.replay.replay.return_value = {
            "candidates": [{"symbol": "600001", "name": "fees", "screen_score": 90}],
            "compatibility": {},
        }

        result = self.service.run(
            strategy="dual_low",
            market="cn",
            date_from=date(2024, 1, 1),
            date_to=date(2024, 1, 1),
            final_holding_bars=2,
            initial_capital=1000,
            commission_bps=0,
            minimum_commission=5,
            sell_tax_bps=100,
            slippage_bps=0,
            accounting_mode="cash_ledger",
        )

        trades = result["periods"][0]["trades"]
        buy = next(item for item in trades if item["side"] == "buy")
        sell = next(item for item in trades if item["side"] == "sell")
        self.assertEqual(buy["quantity"], 900)
        self.assertEqual(buy["fee"], 5)
        self.assertEqual(buy["tax"], 0)
        self.assertEqual(buy["cash_effect"], -905)
        self.assertEqual(sell["fee"], 5)
        self.assertEqual(sell["tax"], 9)
        self.assertEqual(sell["cash_effect"], 886)
        self.assertEqual(result["metrics"]["total_fees"], 10)
        self.assertEqual(result["metrics"]["total_taxes"], 9)
        self.assertEqual(result["final_equity"], 981)
        self.assertEqual(result["methodology"]["minimum_commission_per_trade"], 5)
        self.assertEqual(result["methodology"]["sell_tax_bps"], 100)

    def test_rejects_invalid_target_weight_contracts(self) -> None:
        self.repository.list_dates.return_value = [date(2024, 1, 1)]

        with self.assertRaisesRegex(ValueError, "total must not exceed 100"):
            self.service.run(
                strategy="dual_low",
                market="cn",
                date_from=date(2024, 1, 1),
                date_to=date(2024, 1, 1),
                accounting_mode="cash_ledger",
                target_weights={"600001": 60, "600002": 50},
            )
        with self.assertRaisesRegex(ValueError, "only in cash_ledger"):
            self.service.run(
                strategy="dual_low",
                market="cn",
                date_from=date(2024, 1, 1),
                date_to=date(2024, 1, 1),
                accounting_mode="equal_weight_approximation",
                target_weights={"600001": 50},
            )
        with self.assertRaisesRegex(ValueError, "minimum_commission and sell_tax_bps"):
            self.service.run(
                strategy="dual_low",
                market="cn",
                date_from=date(2024, 1, 1),
                date_to=date(2024, 1, 1),
                accounting_mode="equal_weight_approximation",
                minimum_commission=5,
                sell_tax_bps=5,
            )


if __name__ == "__main__":
    unittest.main()
