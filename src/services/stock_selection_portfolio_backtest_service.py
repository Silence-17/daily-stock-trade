# -*- coding: utf-8 -*-
"""Portfolio backtest for point-in-time AlphaSift replay candidates."""

from __future__ import annotations

import math
import statistics
from datetime import date
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import select

from src.repositories.stock_selection_factor_snapshot_repo import (
    StockSelectionFactorSnapshotRepository,
)
from src.services.stock_selection_strategy_replay_service import (
    StockSelectionStrategyReplayService,
)
from src.storage import DatabaseManager, StockDaily


class StockSelectionPortfolioBacktestService:
    """Run sequential equal-weight periods from dated strategy replays."""

    def __init__(
        self,
        db_manager: Optional[DatabaseManager] = None,
        repository: Optional[StockSelectionFactorSnapshotRepository] = None,
        replay_service: Optional[StockSelectionStrategyReplayService] = None,
    ) -> None:
        self.db = db_manager or DatabaseManager.get_instance()
        self.repository = repository or StockSelectionFactorSnapshotRepository(self.db)
        self.replay_service = replay_service or StockSelectionStrategyReplayService(
            db_manager=self.db,
            repository=self.repository,
        )

    def run(
        self,
        *,
        strategy: str,
        market: str,
        date_from: date,
        date_to: date,
        top_k: int = 5,
        final_holding_bars: int = 20,
        initial_capital: float = 100_000.0,
        commission_bps: float = 3.0,
        minimum_commission: float = 0.0,
        sell_tax_bps: float = 0.0,
        slippage_bps: float = 5.0,
        benchmark_symbol: Optional[str] = None,
        enforce_tradeability: bool = True,
        accounting_mode: str = "equal_weight_approximation",
        target_weights: Optional[Dict[str, float]] = None,
        corporate_actions: Optional[List[Dict[str, Any]]] = None,
        min_hard_coverage: float = 0.95,
        min_score_coverage: float = 0.80,
    ) -> Dict[str, Any]:
        self._validate(
            date_from=date_from,
            date_to=date_to,
            top_k=top_k,
            final_holding_bars=final_holding_bars,
            initial_capital=initial_capital,
            commission_bps=commission_bps,
            minimum_commission=minimum_commission,
            sell_tax_bps=sell_tax_bps,
            slippage_bps=slippage_bps,
        )
        dates = self.repository.list_dates(market=market, date_from=date_from, date_to=date_to)
        if not dates:
            raise ValueError("no point-in-time factor snapshots exist in the requested range")

        normalized_target_weights = self._normalize_target_weights(target_weights)
        normalized_corporate_actions = self._normalize_corporate_actions(corporate_actions)
        if normalized_target_weights and accounting_mode != "cash_ledger":
            raise ValueError("target_weights are supported only in cash_ledger mode")
        if normalized_corporate_actions and accounting_mode != "cash_ledger":
            raise ValueError("corporate_actions are supported only in cash_ledger mode")
        if (minimum_commission > 0 or sell_tax_bps > 0) and accounting_mode != "cash_ledger":
            raise ValueError("minimum_commission and sell_tax_bps are supported only in cash_ledger mode")
        if accounting_mode == "cash_ledger":
            return self._run_cash_ledger(
                strategy=strategy,
                market=market,
                dates=dates,
                top_k=top_k,
                final_holding_bars=final_holding_bars,
                initial_capital=initial_capital,
                commission_bps=commission_bps,
                minimum_commission=minimum_commission,
                sell_tax_bps=sell_tax_bps,
                slippage_bps=slippage_bps,
                benchmark_symbol=benchmark_symbol,
                enforce_tradeability=enforce_tradeability,
                min_hard_coverage=min_hard_coverage,
                min_score_coverage=min_score_coverage,
                date_from=date_from,
                date_to=date_to,
                target_weights=normalized_target_weights,
                corporate_actions=normalized_corporate_actions,
            )
        if accounting_mode != "equal_weight_approximation":
            raise ValueError("accounting_mode must be cash_ledger or equal_weight_approximation")

        equity = float(initial_capital)
        benchmark_equity = float(initial_capital)
        periods: List[Dict[str, Any]] = []
        period_returns: List[float] = []
        benchmark_returns: List[float] = []
        first_entry_date: Optional[date] = None
        last_exit_date: Optional[date] = None
        total_selected = 0
        total_evaluated = 0
        total_turnover_pct = 0.0

        replay_periods = []
        for signal_date in dates:
            replay = self.replay_service.replay(
                strategy=strategy,
                market=market,
                snapshot_date=signal_date,
                max_results=top_k,
                min_hard_coverage=min_hard_coverage,
                min_score_coverage=min_score_coverage,
            )
            replay_periods.append({
                "signal_date": signal_date,
                "replay": replay,
                "candidates": list(replay.get("candidates") or [])[:top_k],
            })

        active_positions: Dict[str, Dict[str, Any]] = {}
        for index, replay_period in enumerate(replay_periods):
            signal_date = replay_period["signal_date"]
            next_signal_date = dates[index + 1] if index + 1 < len(dates) else None
            replay = replay_period["replay"]
            candidates = replay_period["candidates"]
            next_candidates = (
                replay_periods[index + 1]["candidates"]
                if index + 1 < len(replay_periods)
                else []
            )
            next_symbols = {
                str(candidate.get("symbol") or "")
                for candidate in next_candidates
                if candidate.get("symbol")
            }
            total_selected += len(candidates)
            candidate_by_symbol = {
                str(candidate.get("symbol") or ""): candidate
                for candidate in candidates
                if candidate.get("symbol")
            }
            portfolio_candidates = list(candidates)
            portfolio_candidates.extend(
                position
                for symbol, position in active_positions.items()
                if symbol not in candidate_by_symbol
            )
            holdings = []
            for candidate in portfolio_candidates:
                symbol = str(candidate.get("symbol") or "")
                selected = symbol in candidate_by_symbol
                entry_trade = symbol not in active_positions
                exit_trade = symbol not in next_symbols
                holding = self._evaluate_holding(
                    symbol=symbol,
                    name=str(candidate.get("name") or ""),
                    market=market,
                    signal_date=signal_date,
                    next_signal_date=next_signal_date,
                    final_holding_bars=final_holding_bars,
                    commission_bps=commission_bps,
                    slippage_bps=slippage_bps,
                    entry_trade=entry_trade,
                    exit_trade=exit_trade,
                    enforce_tradeability=enforce_tradeability,
                )
                holding.update({
                    "name": candidate.get("name"),
                    "screen_score": candidate.get("screen_score"),
                    "selected": selected,
                })
                holdings.append(holding)
            evaluated = [
                item for item in holdings
                if item["status"] in {"completed", "forced_retained"}
            ]
            selected_evaluated = [item for item in evaluated if item["selected"]]
            total_evaluated += len(selected_evaluated)
            active_positions = {
                item["symbol"]: {
                    "symbol": item["symbol"],
                    "name": item.get("name"),
                    "screen_score": item.get("screen_score"),
                }
                for item in evaluated
                if not item["exit_trade"]
            }
            gross_return = self._mean([item["gross_return_pct"] for item in evaluated])
            net_return = self._mean([item["net_return_pct"] for item in evaluated])
            period_turnover_pct = (
                sum(int(item["entry_trade"]) + int(item["exit_trade"]) for item in evaluated)
                / len(evaluated)
                * 100
                if evaluated
                else 0.0
            )
            total_turnover_pct += period_turnover_pct
            if net_return is not None:
                equity *= 1 + net_return / 100
                period_returns.append(net_return)

            benchmark = self._evaluate_benchmark(
                symbol=benchmark_symbol,
                signal_date=signal_date,
                next_signal_date=next_signal_date,
                final_holding_bars=final_holding_bars,
            )
            benchmark_return = benchmark.get("return_pct")
            if benchmark_return is not None and net_return is not None:
                benchmark_equity *= 1 + benchmark_return / 100
                benchmark_returns.append(float(benchmark_return))

            completed_dates = [item["entry_date"] for item in evaluated]
            exit_dates = [item["exit_date"] for item in evaluated]
            if completed_dates:
                period_first = min(completed_dates)
                first_entry_date = min(first_entry_date, period_first) if first_entry_date else period_first
            if exit_dates:
                period_last = max(exit_dates)
                last_exit_date = max(last_exit_date, period_last) if last_exit_date else period_last
            periods.append({
                "signal_date": signal_date.isoformat(),
                "next_signal_date": next_signal_date.isoformat() if next_signal_date else None,
                "selected_count": len(candidates),
                "holding_count": len(evaluated),
                "evaluated_count": len(selected_evaluated),
                "coverage_pct": round(len(selected_evaluated) / len(candidates) * 100, 4) if candidates else 0.0,
                "gross_return_pct": self._round(gross_return),
                "net_return_pct": self._round(net_return),
                "turnover_pct": round(period_turnover_pct, 4),
                "retained_count": sum(not item["entry_trade"] for item in evaluated),
                "forced_retained_count": sum(item["status"] == "forced_retained" for item in evaluated),
                "entry_trade_count": sum(item["entry_trade"] for item in evaluated),
                "exit_trade_count": sum(item["exit_trade"] for item in evaluated),
                "equity": round(equity, 4),
                "benchmark": benchmark,
                "benchmark_equity": round(benchmark_equity, 4) if benchmark_return is not None else None,
                "holdings": holdings,
                "compatibility": replay.get("compatibility") or {},
            })

        metrics = self._metrics(
            initial_capital=initial_capital,
            final_equity=equity,
            benchmark_equity=benchmark_equity,
            period_returns=period_returns,
            first_entry_date=first_entry_date,
            last_exit_date=last_exit_date,
            equity_points=[initial_capital] + [period["equity"] for period in periods],
            benchmark_available=bool(benchmark_returns),
        )
        metrics["benchmark_period_count"] = len(benchmark_returns)
        metrics["total_turnover_pct"] = round(total_turnover_pct, 4)
        metrics["average_turnover_pct"] = round(total_turnover_pct / len(periods), 4) if periods else 0.0
        metrics["ending_open_position_count"] = len(active_positions)
        return {
            "strategy": strategy,
            "market": str(market).lower(),
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
            "snapshot_count": len(dates),
            "selected_count": total_selected,
            "evaluated_count": total_evaluated,
            "coverage_pct": round(total_evaluated / total_selected * 100, 4) if total_selected else 0.0,
            "initial_capital": initial_capital,
            "final_equity": round(equity, 4),
            "benchmark_symbol": benchmark_symbol,
            "metrics": metrics,
            "periods": periods,
            "methodology": {
                "point_in_time": True,
                "lookahead_protection": True,
                "entry": "first_daily_open_strictly_after_signal_date",
                "rebalance_exit": "first_daily_open_strictly_after_next_signal_date",
                "final_exit": f"close_of_forward_bar_{final_holding_bars}",
                "weighting": "equal_weight_completed_holdings",
                "rebalance": "retain_overlapping_symbols_equal_weight_approximation",
                "blocked_exit": "mark_to_market_and_carry_until_next_rebalance_attempt",
                "commission_bps_per_side": commission_bps,
                "slippage_bps_per_side": slippage_bps,
                "tradeability_gate_enabled": enforce_tradeability,
                "tradeability_gate": "suspension_and_cn_price_limit_by_symbol_board_or_st_name",
                "uses_current_data_fallback": False,
                "places_orders": False,
            },
        }

    def _run_cash_ledger(
        self,
        *,
        strategy: str,
        market: str,
        dates: List[date],
        top_k: int,
        final_holding_bars: int,
        initial_capital: float,
        commission_bps: float,
        minimum_commission: float,
        sell_tax_bps: float,
        slippage_bps: float,
        benchmark_symbol: Optional[str],
        enforce_tradeability: bool,
        min_hard_coverage: float,
        min_score_coverage: float,
        date_from: date,
        date_to: date,
        target_weights: Dict[str, float],
        corporate_actions: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        cash = float(initial_capital)
        equity = float(initial_capital)
        benchmark_equity = float(initial_capital)
        positions: Dict[str, Dict[str, Any]] = {}
        periods: List[Dict[str, Any]] = []
        period_returns: List[float] = []
        benchmark_returns: List[float] = []
        equity_points = [float(initial_capital)]
        total_selected = 0
        total_evaluated = 0
        total_turnover_pct = 0.0
        total_fees = 0.0
        total_taxes = 0.0
        total_slippage_cost = 0.0
        total_dividend_cash = 0.0
        applied_corporate_actions: set[int] = set()
        first_entry_date: Optional[date] = None
        last_exit_date: Optional[date] = None
        commission = commission_bps / 10_000
        sell_tax = sell_tax_bps / 10_000
        slippage = slippage_bps / 10_000
        lot_size = 100 if str(market).lower() == "cn" else 1

        for index, signal_date in enumerate(dates):
            next_signal_date = dates[index + 1] if index + 1 < len(dates) else None
            replay = self.replay_service.replay(
                strategy=strategy,
                market=market,
                snapshot_date=signal_date,
                max_results=top_k,
                min_hard_coverage=min_hard_coverage,
                min_score_coverage=min_score_coverage,
            )
            candidates = list(replay.get("candidates") or [])[:top_k]
            replay_targets = {
                str(item.get("symbol") or "").strip().upper(): item
                for item in candidates
                if item.get("symbol")
            }
            if target_weights:
                targets = {
                    symbol: candidate
                    for symbol, candidate in replay_targets.items()
                    if symbol in target_weights
                }
                target_weight_pcts = {
                    symbol: float(target_weights[symbol])
                    for symbol in targets
                }
            else:
                targets = replay_targets
                target_weight_pcts = {
                    symbol: 100.0 / len(targets)
                    for symbol in targets
                } if targets else {}
            total_selected += len(targets)
            symbols = list(dict.fromkeys([*positions.keys(), *targets.keys()]))
            trade_bars: Dict[str, Optional[StockDaily]] = {}
            trade_prices: Dict[str, Optional[float]] = {}
            for symbol in symbols:
                bars = self._future_bars(symbol=symbol, after=signal_date)
                bar = bars[0] if bars else None
                trade_bars[symbol] = bar
                trade_prices[symbol] = self._positive(getattr(bar, "open", None)) if bar else None

            cash, period_corporate_actions = self._apply_corporate_actions(
                cash=cash,
                positions=positions,
                corporate_actions=corporate_actions,
                applied_indexes=applied_corporate_actions,
                through_dates={
                    symbol: bar.date
                    for symbol, bar in trade_bars.items()
                    if bar is not None
                },
            )

            pretrade_market_value = sum(
                float(position["quantity"])
                * float(trade_prices.get(symbol) or position.get("last_price") or 0)
                for symbol, position in positions.items()
            )
            pretrade_equity = cash + pretrade_market_value
            desired_quantities: Dict[str, int] = {}
            evaluated_symbols = 0
            for symbol, candidate in targets.items():
                price = trade_prices.get(symbol)
                if price is None:
                    desired_quantities[symbol] = int(positions.get(symbol, {}).get("quantity") or 0)
                    continue
                evaluated_symbols += 1
                target_value = pretrade_equity * target_weight_pcts[symbol] / 100.0
                desired_quantities[symbol] = self._max_affordable_lot(
                    budget=target_value,
                    execution_price=price * (1 + slippage),
                    commission_rate=commission,
                    minimum_commission=minimum_commission,
                    lot_size=lot_size,
                )
            total_evaluated += evaluated_symbols

            trades: List[Dict[str, Any]] = []
            blocked: List[Dict[str, Any]] = []
            traded_notional = 0.0
            period_fees = 0.0
            period_taxes = 0.0
            period_slippage_cost = 0.0
            for symbol in list(positions):
                position = positions[symbol]
                current_quantity = int(position["quantity"])
                desired = desired_quantities.get(symbol, 0)
                sell_quantity = max(0, current_quantity - desired)
                if sell_quantity <= 0:
                    continue
                bar = trade_bars.get(symbol)
                price = trade_prices.get(symbol)
                if bar is None or price is None:
                    blocked.append({"symbol": symbol, "side": "sell", "reason": "missing_rebalance_bar"})
                    continue
                reason = (
                    self._tradeability_reason(
                        bar=bar,
                        symbol=symbol,
                        name=str(position.get("name") or ""),
                        market=market,
                        side="sell",
                    )
                    if enforce_tradeability
                    else None
                )
                if reason:
                    blocked.append({"symbol": symbol, "side": "sell", "reason": reason})
                    continue
                execution_price = price * (1 - slippage)
                execution_notional = sell_quantity * execution_price
                fee = self._commission_fee(
                    notional=execution_notional,
                    commission_rate=commission,
                    minimum_commission=minimum_commission,
                )
                tax = execution_notional * sell_tax
                cash_effect = execution_notional - fee - tax
                cash += cash_effect
                position["quantity"] = current_quantity - sell_quantity
                traded_notional += sell_quantity * price
                period_fees += fee
                period_taxes += tax
                period_slippage_cost += sell_quantity * (price - execution_price)
                trades.append(self._trade_record(
                    symbol=symbol,
                    side="sell",
                    quantity=sell_quantity,
                    price=price,
                    execution_price=execution_price,
                    fee=fee,
                    tax=tax,
                    cash_effect=cash_effect,
                    trade_date=bar.date,
                ))
                if position["quantity"] <= 0:
                    del positions[symbol]

            for symbol, candidate in targets.items():
                desired = desired_quantities.get(symbol, 0)
                current_quantity = int(positions.get(symbol, {}).get("quantity") or 0)
                buy_quantity = max(0, desired - current_quantity)
                if desired <= 0 and current_quantity <= 0:
                    blocked.append({"symbol": symbol, "side": "buy", "reason": "target_below_lot_size"})
                    continue
                if buy_quantity <= 0:
                    continue
                bar = trade_bars.get(symbol)
                price = trade_prices.get(symbol)
                if bar is None or price is None:
                    blocked.append({"symbol": symbol, "side": "buy", "reason": "missing_rebalance_bar"})
                    continue
                reason = (
                    self._tradeability_reason(
                        bar=bar,
                        symbol=symbol,
                        name=str(candidate.get("name") or ""),
                        market=market,
                        side="buy",
                    )
                    if enforce_tradeability
                    else None
                )
                if reason:
                    blocked.append({"symbol": symbol, "side": "buy", "reason": reason})
                    continue
                execution_price = price * (1 + slippage)
                affordable = self._max_affordable_lot(
                    budget=cash,
                    execution_price=execution_price,
                    commission_rate=commission,
                    minimum_commission=minimum_commission,
                    lot_size=lot_size,
                )
                executed = min(buy_quantity, affordable)
                if executed <= 0:
                    blocked.append({"symbol": symbol, "side": "buy", "reason": "insufficient_cash_for_lot"})
                    continue
                execution_notional = executed * execution_price
                fee = self._commission_fee(
                    notional=execution_notional,
                    commission_rate=commission,
                    minimum_commission=minimum_commission,
                )
                cash_effect = -(execution_notional + fee)
                cash += cash_effect
                traded_notional += executed * price
                period_fees += fee
                period_slippage_cost += executed * (execution_price - price)
                existing = positions.get(symbol)
                if existing is None:
                    positions[symbol] = {
                        "symbol": symbol,
                        "name": candidate.get("name"),
                        "quantity": executed,
                        "last_price": price,
                    }
                else:
                    existing["quantity"] = current_quantity + executed
                    existing["name"] = candidate.get("name") or existing.get("name")
                trades.append(self._trade_record(
                    symbol=symbol,
                    side="buy",
                    quantity=executed,
                    price=price,
                    execution_price=execution_price,
                    fee=fee,
                    tax=0.0,
                    cash_effect=cash_effect,
                    trade_date=bar.date,
                ))
                first_entry_date = min(first_entry_date, bar.date) if first_entry_date else bar.date

            marks: List[Dict[str, Any]] = []
            for symbol, position in list(positions.items()):
                bars = self._future_bars(symbol=symbol, after=signal_date)
                if next_signal_date is not None:
                    mark_bar = next((bar for bar in bars if bar.date > next_signal_date), None)
                    mark_price = self._positive(getattr(mark_bar, "open", None)) if mark_bar else None
                    mark_mode = "next_rebalance_open"
                else:
                    mark_bar = bars[final_holding_bars - 1] if len(bars) >= final_holding_bars else None
                    mark_price = self._positive(getattr(mark_bar, "close", None)) if mark_bar else None
                    mark_mode = "final_horizon_close"
                if mark_price is None or mark_bar is None:
                    mark_price = self._positive(position.get("last_price")) or 0.0
                    mark_date = signal_date
                    mark_mode = "last_available_price"
                else:
                    mark_date = mark_bar.date
                cash, mark_actions = self._apply_corporate_actions(
                    cash=cash,
                    positions=positions,
                    corporate_actions=corporate_actions,
                    applied_indexes=applied_corporate_actions,
                    through_dates={symbol: mark_date},
                )
                period_corporate_actions.extend(mark_actions)
                position["last_price"] = mark_price
                marks.append({
                    "symbol": symbol,
                    "name": position.get("name"),
                    "quantity": int(position["quantity"]),
                    "mark_date": mark_date,
                    "mark_price": mark_price,
                    "market_value": round(int(position["quantity"]) * mark_price, 4),
                    "mark_mode": mark_mode,
                    "target_weight_pct": round(target_weight_pcts.get(symbol, 0.0), 4),
                })
                last_exit_date = max(last_exit_date, mark_date) if last_exit_date else mark_date

            if next_signal_date is None:
                for item in list(marks):
                    symbol = item["symbol"]
                    position = positions[symbol]
                    bar = next(
                        (bar for bar in self._future_bars(symbol=symbol, after=signal_date) if bar.date == item["mark_date"]),
                        None,
                    )
                    if bar is None:
                        blocked.append({"symbol": symbol, "side": "sell", "reason": "missing_final_exit_bar"})
                        continue
                    reason = (
                        self._tradeability_reason(
                            bar=bar,
                            symbol=symbol,
                            name=str(position.get("name") or ""),
                            market=market,
                            side="sell",
                        )
                        if enforce_tradeability
                        else None
                    )
                    if reason:
                        blocked.append({"symbol": symbol, "side": "sell", "reason": reason})
                        continue
                    quantity = int(position["quantity"])
                    price = float(item["mark_price"])
                    execution_price = price * (1 - slippage)
                    execution_notional = quantity * execution_price
                    fee = self._commission_fee(
                        notional=execution_notional,
                        commission_rate=commission,
                        minimum_commission=minimum_commission,
                    )
                    tax = execution_notional * sell_tax
                    cash_effect = execution_notional - fee - tax
                    cash += cash_effect
                    traded_notional += quantity * price
                    period_fees += fee
                    period_taxes += tax
                    period_slippage_cost += quantity * (price - execution_price)
                    trades.append(self._trade_record(
                        symbol=symbol,
                        side="sell",
                        quantity=quantity,
                        price=price,
                        execution_price=execution_price,
                        fee=fee,
                        tax=tax,
                        cash_effect=cash_effect,
                        trade_date=item["mark_date"],
                    ))
                    del positions[symbol]

            market_value = sum(
                int(position["quantity"]) * float(position.get("last_price") or 0)
                for position in positions.values()
            )
            equity = cash + market_value
            period_return = (equity / equity_points[-1] - 1) * 100 if equity_points[-1] > 0 else 0.0
            period_returns.append(period_return)
            equity_points.append(equity)
            turnover_pct = traded_notional / pretrade_equity * 100 if pretrade_equity > 0 else 0.0
            total_turnover_pct += turnover_pct
            total_fees += period_fees
            total_taxes += period_taxes
            total_slippage_cost += period_slippage_cost
            total_dividend_cash += sum(
                float(item.get("cash_effect") or 0.0)
                for item in period_corporate_actions
                if item.get("status") == "applied" and item.get("action_type") == "cash_dividend"
            )

            benchmark = self._evaluate_benchmark(
                symbol=benchmark_symbol,
                signal_date=signal_date,
                next_signal_date=next_signal_date,
                final_holding_bars=final_holding_bars,
            )
            benchmark_return = benchmark.get("return_pct")
            if benchmark_return is not None:
                benchmark_equity *= 1 + float(benchmark_return) / 100
                benchmark_returns.append(float(benchmark_return))
            periods.append({
                "signal_date": signal_date.isoformat(),
                "next_signal_date": next_signal_date.isoformat() if next_signal_date else None,
                "selected_count": len(targets),
                "replay_candidate_count": len(replay_targets),
                "target_weights": {
                    symbol: round(weight, 6)
                    for symbol, weight in target_weight_pcts.items()
                },
                "configured_targets_not_selected": sorted(
                    set(target_weights) - set(replay_targets)
                ) if target_weights else [],
                "evaluated_count": evaluated_symbols,
                "coverage_pct": round(evaluated_symbols / len(targets) * 100, 4) if targets else 0.0,
                "cash": round(cash, 4),
                "market_value": round(market_value, 4),
                "equity": round(equity, 4),
                "net_return_pct": round(period_return, 6),
                "turnover_pct": round(turnover_pct, 4),
                "fees": round(period_fees, 4),
                "taxes": round(period_taxes, 4),
                "slippage_cost": round(period_slippage_cost, 4),
                "corporate_actions": period_corporate_actions,
                "processed_corporate_action_count": len(period_corporate_actions),
                "applied_corporate_action_count": sum(
                    item.get("status") == "applied" for item in period_corporate_actions
                ),
                "position_count": len(positions),
                "trade_count": len(trades),
                "blocked_trade_count": len(blocked),
                "trades": trades,
                "blocked_trades": blocked,
                "holdings": marks,
                "benchmark": benchmark,
                "benchmark_equity": round(benchmark_equity, 4) if benchmark_return is not None else None,
                "compatibility": replay.get("compatibility") or {},
            })

        metrics = self._metrics(
            initial_capital=initial_capital,
            final_equity=equity,
            benchmark_equity=benchmark_equity,
            period_returns=period_returns,
            first_entry_date=first_entry_date,
            last_exit_date=last_exit_date,
            equity_points=equity_points,
            benchmark_available=bool(benchmark_returns),
        )
        metrics.update({
            "benchmark_period_count": len(benchmark_returns),
            "total_turnover_pct": round(total_turnover_pct, 4),
            "average_turnover_pct": round(total_turnover_pct / len(periods), 4) if periods else 0.0,
            "total_fees": round(total_fees, 4),
            "total_taxes": round(total_taxes, 4),
            "total_slippage_cost": round(total_slippage_cost, 4),
            "processed_corporate_action_count": sum(
                1
                for item in corporate_actions
                if int(item["event_index"]) in applied_corporate_actions
            ),
            "applied_corporate_action_count": sum(
                1
                for period in periods
                for item in period.get("corporate_actions", [])
                if item.get("status") == "applied"
            ),
            "cash_dividends_received": round(total_dividend_cash, 4),
            "ending_open_position_count": len(positions),
            "ending_cash": round(cash, 4),
            "ending_market_value": round(
                sum(int(item["quantity"]) * float(item.get("last_price") or 0) for item in positions.values()),
                4,
            ),
        })
        return {
            "strategy": strategy,
            "market": str(market).lower(),
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
            "snapshot_count": len(dates),
            "selected_count": total_selected,
            "evaluated_count": total_evaluated,
            "coverage_pct": round(total_evaluated / total_selected * 100, 4) if total_selected else 0.0,
            "initial_capital": initial_capital,
            "final_equity": round(equity, 4),
            "benchmark_symbol": benchmark_symbol,
            "metrics": metrics,
            "periods": periods,
            "methodology": {
                "point_in_time": True,
                "lookahead_protection": True,
                "accounting_mode": "cash_ledger",
                "position_sizing": (
                    "explicit_target_weight_value_rounded_down_to_lot"
                    if target_weights
                    else "equal_target_value_rounded_down_to_lot"
                ),
                "target_weight_mode": "explicit_symbol_weights" if target_weights else "equal_weight",
                "configured_target_weights": dict(target_weights),
                "corporate_action_source": "explicit_request_unadjusted_price_ledger",
                "configured_corporate_action_count": len(corporate_actions),
                "unprocessed_corporate_actions": [
                    self._public_corporate_action(item)
                    for item in corporate_actions
                    if int(item["event_index"]) not in applied_corporate_actions
                ],
                "same_day_event_order": "corporate_action_before_trade",
                "lot_size": lot_size,
                "cash_constraint": "buys_capped_by_available_cash",
                "commission_bps_per_side": commission_bps,
                "minimum_commission_per_trade": minimum_commission,
                "sell_tax_bps": sell_tax_bps,
                "slippage_bps_per_side": slippage_bps,
                "tradeability_gate_enabled": enforce_tradeability,
                "places_orders": False,
                "uses_current_data_fallback": False,
            },
        }

    @staticmethod
    def _normalize_target_weights(raw: Optional[Dict[str, float]]) -> Dict[str, float]:
        normalized: Dict[str, float] = {}
        for raw_symbol, raw_weight in dict(raw or {}).items():
            symbol = str(raw_symbol or "").strip().upper()
            if not symbol:
                raise ValueError("target_weights contains an empty symbol")
            try:
                weight = float(raw_weight)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"target weight for {symbol} must be numeric") from exc
            if not math.isfinite(weight) or weight <= 0 or weight > 100:
                raise ValueError(f"target weight for {symbol} must be greater than 0 and at most 100")
            normalized[symbol] = weight
        total = sum(normalized.values())
        if total > 100 + 1e-9:
            raise ValueError("target_weights total must not exceed 100 percent")
        return normalized

    @classmethod
    def _normalize_corporate_actions(
        cls,
        raw_actions: Optional[List[Dict[str, Any]]],
    ) -> List[Dict[str, Any]]:
        normalized: List[Dict[str, Any]] = []
        seen = set()
        for raw in list(raw_actions or []):
            if not isinstance(raw, dict):
                raise ValueError("each corporate action must be an object")
            symbol = str(raw.get("symbol") or "").strip().upper()
            if not symbol:
                raise ValueError("corporate action symbol is required")
            raw_date = raw.get("effective_date")
            if isinstance(raw_date, date):
                effective_date = raw_date
            else:
                try:
                    effective_date = date.fromisoformat(str(raw_date or ""))
                except ValueError as exc:
                    raise ValueError(f"invalid corporate action effective_date for {symbol}") from exc
            action_type = str(raw.get("action_type") or "").strip().lower()
            if action_type not in {"cash_dividend", "split_adjustment"}:
                raise ValueError(f"unsupported corporate action type for {symbol}")
            key = (symbol, effective_date, action_type)
            if key in seen:
                raise ValueError(f"duplicate corporate action for {symbol} on {effective_date.isoformat()}")
            seen.add(key)
            item: Dict[str, Any] = {
                "event_index": len(normalized),
                "symbol": symbol,
                "effective_date": effective_date,
                "action_type": action_type,
            }
            if action_type == "cash_dividend":
                if raw.get("split_ratio") is not None:
                    raise ValueError(f"split_ratio is not allowed for cash_dividend {symbol}")
                value = cls._positive(raw.get("cash_dividend_per_share"))
                if value is None:
                    raise ValueError(f"cash_dividend_per_share must be positive for {symbol}")
                item["cash_dividend_per_share"] = value
            else:
                if raw.get("cash_dividend_per_share") is not None:
                    raise ValueError(f"cash_dividend_per_share is not allowed for split_adjustment {symbol}")
                value = cls._positive(raw.get("split_ratio"))
                if value is None:
                    raise ValueError(f"split_ratio must be positive for {symbol}")
                item["split_ratio"] = value
            normalized.append(item)
        normalized.sort(key=lambda item: (item["effective_date"], item["event_index"]))
        return normalized

    @classmethod
    def _apply_corporate_actions(
        cls,
        *,
        cash: float,
        positions: Dict[str, Dict[str, Any]],
        corporate_actions: List[Dict[str, Any]],
        applied_indexes: set[int],
        through_dates: Dict[str, date],
    ) -> tuple[float, List[Dict[str, Any]]]:
        records: List[Dict[str, Any]] = []
        for action in corporate_actions:
            event_index = int(action["event_index"])
            symbol = str(action["symbol"])
            through_date = through_dates.get(symbol)
            if event_index in applied_indexes or through_date is None:
                continue
            effective_date = action["effective_date"]
            if effective_date > through_date:
                continue
            applied_indexes.add(event_index)
            position = positions.get(symbol)
            quantity_before = int(position.get("quantity") or 0) if position else 0
            record = {
                **cls._public_corporate_action(action),
                "status": "not_held" if quantity_before <= 0 else "applied",
                "quantity_before": quantity_before,
                "quantity_after": quantity_before,
                "cash_effect": 0.0,
            }
            if quantity_before <= 0:
                records.append(record)
                continue
            if action["action_type"] == "cash_dividend":
                cash_effect = quantity_before * float(action["cash_dividend_per_share"])
                cash += cash_effect
                record["cash_effect"] = round(cash_effect, 4)
            else:
                adjusted_quantity = quantity_before * float(action["split_ratio"])
                rounded_quantity = round(adjusted_quantity)
                if abs(adjusted_quantity - rounded_quantity) > 1e-9:
                    raise ValueError(
                        f"split adjustment for {symbol} produces fractional shares; cash-in-lieu is unsupported"
                    )
                position["quantity"] = int(rounded_quantity)
                record["quantity_after"] = int(rounded_quantity)
            records.append(record)
        return cash, records

    @staticmethod
    def _public_corporate_action(action: Dict[str, Any]) -> Dict[str, Any]:
        return {
            key: (value.isoformat() if isinstance(value, date) else value)
            for key, value in action.items()
            if key != "event_index"
        }

    @staticmethod
    def _floor_lot(quantity: float, lot_size: int) -> int:
        if not math.isfinite(quantity) or quantity <= 0:
            return 0
        return int(quantity // lot_size) * lot_size

    @staticmethod
    def _commission_fee(
        *,
        notional: float,
        commission_rate: float,
        minimum_commission: float,
    ) -> float:
        if notional <= 0:
            return 0.0
        return max(notional * commission_rate, minimum_commission)

    @classmethod
    def _max_affordable_lot(
        cls,
        *,
        budget: float,
        execution_price: float,
        commission_rate: float,
        minimum_commission: float,
        lot_size: int,
    ) -> int:
        if budget <= 0 or execution_price <= 0 or lot_size <= 0:
            return 0
        high = int(budget // execution_price // lot_size)
        low = 0
        while low < high:
            midpoint = (low + high + 1) // 2
            quantity = midpoint * lot_size
            notional = quantity * execution_price
            total_cost = notional + cls._commission_fee(
                notional=notional,
                commission_rate=commission_rate,
                minimum_commission=minimum_commission,
            )
            if total_cost <= budget + 1e-9:
                low = midpoint
            else:
                high = midpoint - 1
        return low * lot_size

    @staticmethod
    def _trade_record(
        *,
        symbol: str,
        side: str,
        quantity: int,
        price: float,
        execution_price: float,
        fee: float,
        tax: float,
        cash_effect: float,
        trade_date: date,
    ) -> Dict[str, Any]:
        effective_price = abs(cash_effect) / quantity if quantity > 0 else 0.0
        return {
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "price": round(price, 6),
            "execution_price": round(execution_price, 6),
            "effective_price": round(effective_price, 6),
            "gross_notional": round(quantity * price, 4),
            "execution_notional": round(quantity * execution_price, 4),
            "fee": round(fee, 4),
            "tax": round(tax, 4),
            "slippage_cost": round(quantity * abs(execution_price - price), 4),
            "cash_effect": round(cash_effect, 4),
            "trade_date": trade_date,
        }

    def _evaluate_holding(
        self,
        *,
        symbol: str,
        name: str,
        market: str,
        signal_date: date,
        next_signal_date: Optional[date],
        final_holding_bars: int,
        commission_bps: float,
        slippage_bps: float,
        entry_trade: bool,
        exit_trade: bool,
        enforce_tradeability: bool,
    ) -> Dict[str, Any]:
        if not symbol:
            return {"symbol": symbol, "status": "unable", "reason": "missing_symbol"}
        bars = self._future_bars(symbol=symbol, after=signal_date)
        if not bars:
            return {"symbol": symbol, "status": "unable", "reason": "missing_entry_bar"}
        entry = bars[0]
        if next_signal_date is not None:
            exit_bar = next((bar for bar in bars if bar.date > next_signal_date), None)
            exit_price = self._positive(getattr(exit_bar, "open", None)) if exit_bar else None
            exit_mode = "next_rebalance_open"
        else:
            exit_bar = bars[final_holding_bars - 1] if len(bars) >= final_holding_bars else None
            exit_price = self._positive(getattr(exit_bar, "close", None)) if exit_bar else None
            exit_mode = "final_horizon_close"
        entry_price = self._positive(entry.open)
        if entry_price is None:
            return {"symbol": symbol, "status": "unable", "reason": "invalid_entry_price"}
        if exit_bar is None:
            return {"symbol": symbol, "status": "unable", "reason": "missing_exit_bar"}
        if exit_price is None:
            return {"symbol": symbol, "status": "unable", "reason": "invalid_exit_price"}
        if enforce_tradeability and entry_trade:
            entry_reason = self._tradeability_reason(
                bar=entry,
                symbol=symbol,
                name=name,
                market=market,
                side="buy",
            )
            if entry_reason:
                return {"symbol": symbol, "status": "unable", "reason": entry_reason}
        if enforce_tradeability and exit_trade:
            exit_reason = self._tradeability_reason(
                bar=exit_bar,
                symbol=symbol,
                name=name,
                market=market,
                side="sell",
            )
            if exit_reason:
                gross_multiplier = exit_price / entry_price
                commission = commission_bps / 10_000
                slippage = slippage_bps / 10_000
                effective_entry = (
                    entry_price * (1 + slippage) * (1 + commission)
                    if entry_trade
                    else entry_price
                )
                return {
                    "symbol": symbol,
                    "status": "forced_retained",
                    "reason": exit_reason,
                    "entry_date": entry.date,
                    "entry_price": entry_price,
                    "exit_date": exit_bar.date,
                    "exit_price": exit_price,
                    "exit_mode": "blocked_exit_mark_to_market",
                    "entry_trade": entry_trade,
                    "exit_trade": False,
                    "exit_trade_requested": True,
                    "retained": not entry_trade,
                    "forced_retained": True,
                    "gross_return_pct": round((gross_multiplier - 1) * 100, 6),
                    "net_return_pct": round((exit_price / effective_entry - 1) * 100, 6),
                }

        gross_multiplier = exit_price / entry_price
        commission = commission_bps / 10_000
        slippage = slippage_bps / 10_000
        effective_entry = (
            entry_price * (1 + slippage) * (1 + commission)
            if entry_trade
            else entry_price
        )
        effective_exit = (
            exit_price * (1 - slippage) * (1 - commission)
            if exit_trade
            else exit_price
        )
        return {
            "symbol": symbol,
            "status": "completed",
            "reason": None,
            "entry_date": entry.date,
            "entry_price": entry_price,
            "exit_date": exit_bar.date,
            "exit_price": exit_price,
            "exit_mode": exit_mode,
            "entry_trade": entry_trade,
            "exit_trade": exit_trade,
            "exit_trade_requested": exit_trade,
            "retained": not entry_trade,
            "forced_retained": False,
            "gross_return_pct": round((gross_multiplier - 1) * 100, 6),
            "net_return_pct": round((effective_exit / effective_entry - 1) * 100, 6),
        }

    @classmethod
    def _tradeability_reason(
        cls,
        *,
        bar: StockDaily,
        symbol: str,
        name: str,
        market: str,
        side: str,
    ) -> Optional[str]:
        if str(market).lower() != "cn":
            return None
        volume = cls._finite(getattr(bar, "volume", None))
        if volume is not None and volume <= 0:
            return "entry_suspended" if side == "buy" else "exit_suspended"
        pct_chg = cls._finite(getattr(bar, "pct_chg", None))
        if pct_chg is None:
            return None
        limit_pct = cls._cn_price_limit_pct(symbol=symbol, name=name)
        if side == "buy" and pct_chg >= limit_pct - 0.2:
            return "entry_price_limit_up"
        if side == "sell" and pct_chg <= -limit_pct + 0.2:
            return "exit_price_limit_down"
        return None

    @staticmethod
    def _cn_price_limit_pct(*, symbol: str, name: str) -> float:
        if "ST" in name.upper() or "退" in name:
            return 5.0
        if symbol.startswith(("300", "301", "688", "689")):
            return 20.0
        if symbol.startswith(("4", "8", "920")):
            return 30.0
        return 10.0

    def _evaluate_benchmark(
        self,
        *,
        symbol: Optional[str],
        signal_date: date,
        next_signal_date: Optional[date],
        final_holding_bars: int,
    ) -> Dict[str, Any]:
        if not symbol:
            return {"status": "not_configured", "return_pct": None}
        bars = self._future_bars(symbol=symbol, after=signal_date)
        if not bars or self._positive(bars[0].open) is None:
            return {"status": "unable", "reason": "missing_entry_bar", "return_pct": None}
        if next_signal_date is not None:
            exit_bar = next((bar for bar in bars if bar.date > next_signal_date), None)
            exit_price = self._positive(getattr(exit_bar, "open", None)) if exit_bar else None
        else:
            exit_bar = bars[final_holding_bars - 1] if len(bars) >= final_holding_bars else None
            exit_price = self._positive(getattr(exit_bar, "close", None)) if exit_bar else None
        if exit_bar is None or exit_price is None:
            return {"status": "unable", "reason": "missing_exit_bar", "return_pct": None}
        entry_price = float(bars[0].open)
        return {
            "status": "completed",
            "entry_date": bars[0].date,
            "exit_date": exit_bar.date,
            "return_pct": round((exit_price / entry_price - 1) * 100, 6),
        }

    def _future_bars(self, *, symbol: str, after: date) -> Sequence[StockDaily]:
        with self.db.get_session() as session:
            return list(session.execute(
                select(StockDaily)
                .where(StockDaily.code == symbol, StockDaily.date > after)
                .order_by(StockDaily.date.asc())
            ).scalars())

    @classmethod
    def _metrics(
        cls,
        *,
        initial_capital: float,
        final_equity: float,
        benchmark_equity: float,
        period_returns: List[float],
        first_entry_date: Optional[date],
        last_exit_date: Optional[date],
        equity_points: List[float],
        benchmark_available: bool,
    ) -> Dict[str, Any]:
        total_return = (final_equity / initial_capital - 1) * 100
        benchmark_return = (
            (benchmark_equity / initial_capital - 1) * 100
            if benchmark_available
            else None
        )
        elapsed_days = (
            max(1, (last_exit_date - first_entry_date).days)
            if first_entry_date is not None and last_exit_date is not None
            else None
        )
        annualized = (
            ((final_equity / initial_capital) ** (365 / elapsed_days) - 1) * 100
            if elapsed_days and final_equity > 0
            else None
        )
        max_drawdown = cls._max_drawdown(equity_points)
        win_count = sum(value > 0 for value in period_returns)
        std = statistics.stdev(period_returns) if len(period_returns) > 1 else None
        sharpe = (
            statistics.mean(period_returns) / std * math.sqrt(len(period_returns))
            if std and std > 0
            else None
        )
        return {
            "total_return_pct": round(total_return, 6),
            "annualized_return_pct": cls._round(annualized),
            "benchmark_return_pct": cls._round(benchmark_return),
            "excess_return_pct": cls._round(
                total_return - benchmark_return if benchmark_return is not None else None
            ),
            "max_drawdown_pct": round(max_drawdown, 6),
            "period_count": len(period_returns),
            "winning_period_count": win_count,
            "period_win_rate_pct": round(win_count / len(period_returns) * 100, 4) if period_returns else None,
            "average_period_return_pct": cls._round(cls._mean(period_returns)),
            "period_sharpe_ratio": cls._round(sharpe),
            "elapsed_calendar_days": elapsed_days,
        }

    @staticmethod
    def _max_drawdown(points: List[float]) -> float:
        peak = 0.0
        drawdown = 0.0
        for point in points:
            peak = max(peak, point)
            if peak > 0:
                drawdown = min(drawdown, (point / peak - 1) * 100)
        return drawdown

    @staticmethod
    def _positive(value: Any) -> Optional[float]:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if math.isfinite(parsed) and parsed > 0 else None

    @staticmethod
    def _finite(value: Any) -> Optional[float]:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if math.isfinite(parsed) else None

    @staticmethod
    def _mean(values: List[float]) -> Optional[float]:
        return statistics.mean(values) if values else None

    @staticmethod
    def _round(value: Optional[float]) -> Optional[float]:
        return round(float(value), 6) if value is not None else None

    @staticmethod
    def _validate(
        *,
        date_from: date,
        date_to: date,
        top_k: int,
        final_holding_bars: int,
        initial_capital: float,
        commission_bps: float,
        minimum_commission: float,
        sell_tax_bps: float,
        slippage_bps: float,
    ) -> None:
        if date_from > date_to:
            raise ValueError("date_from must not be later than date_to")
        if not 1 <= top_k <= 100:
            raise ValueError("top_k must be between 1 and 100")
        if not 1 <= final_holding_bars <= 250:
            raise ValueError("final_holding_bars must be between 1 and 250")
        if initial_capital <= 0:
            raise ValueError("initial_capital must be positive")
        if not 0 <= commission_bps <= 1000 or not 0 <= slippage_bps <= 1000:
            raise ValueError("commission_bps and slippage_bps must be between 0 and 1000")
        if not math.isfinite(minimum_commission) or not 0 <= minimum_commission <= initial_capital:
            raise ValueError("minimum_commission must be between 0 and initial_capital")
        if not math.isfinite(sell_tax_bps) or not 0 <= sell_tax_bps <= 1000:
            raise ValueError("sell_tax_bps must be between 0 and 1000")
