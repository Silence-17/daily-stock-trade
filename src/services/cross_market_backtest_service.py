# -*- coding: utf-8 -*-
"""Point-in-time replay and ablation suite for the cross-market strategy."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from typing import Dict, List, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

from src.services.cross_market_paper_strategy import (
    AccountRiskState,
    CrossMarketSignalEngine,
    MinuteBar,
    PaperOrder,
    RealisticMinuteExecutionModel,
    StrategyDecisionInput,
    TradeFeeSchedule,
)


@dataclass(frozen=True)
class ReplayFrame:
    session_date: date
    signal_at: datetime
    symbol: str
    theme: str
    cn_gap_pct: float
    reclaimed_open: bool
    above_vwap: bool
    sector_signal_score: float
    expected_gross_edge_pct: float
    signal_price: float
    next_minute_bar: MinuteBar
    close_price: float
    entry_phase: str = "opening"
    us_tech_score: Optional[float] = None
    us_close_theme_signal: Mapping[str, object] = field(default_factory=dict)
    us_premarket_signal: Mapping[str, object] = field(default_factory=dict)
    nasdaq_futures_signal: Mapping[str, object] = field(default_factory=dict)
    asia_supply_chain_signal: Mapping[str, object] = field(default_factory=dict)
    low_position_signal: Mapping[str, object] = field(default_factory=dict)
    intraday_pullback_signal: Mapping[str, object] = field(default_factory=dict)
    asia_market_gate: Mapping[str, object] = field(default_factory=dict)
    board_technical_signal: Mapping[str, object] = field(default_factory=dict)
    rotation_signal: Mapping[str, object] = field(default_factory=dict)
    next_day_high_open_exit_signal: Mapping[str, object] = field(default_factory=dict)
    korea_gate: Mapping[str, object] = field(default_factory=dict)
    cpo_signal: Mapping[str, object] = field(default_factory=dict)
    gold_signal: Mapping[str, object] = field(default_factory=dict)
    range_signal: Mapping[str, object] = field(default_factory=dict)
    evidence_timestamps: Mapping[str, datetime] = field(default_factory=dict)
    instrument_type: str = "stock"
    split_ratio: Optional[float] = None
    cash_dividend_per_share: float = 0.0
    order_cancel_requested: bool = False
    previous_close: Optional[float] = None
    price_limit_pct: Optional[float] = None


@dataclass
class _ReplayLot:
    opened_on: date
    quantity: float
    unit_cost: float


@dataclass
class _ReplayPosition:
    symbol: str
    theme: str
    last_price: float
    peak_price: float
    instrument_type: str
    lots: List[_ReplayLot] = field(default_factory=list)
    tranche_count: int = 0

    @property
    def quantity(self) -> float:
        return sum(lot.quantity for lot in self.lots)

    @property
    def total_cost(self) -> float:
        return sum(lot.quantity * lot.unit_cost for lot in self.lots)

    @property
    def average_cost(self) -> float:
        return self.total_cost / self.quantity if self.quantity > 0 else 0.0

    def sellable_quantity(self, session: date) -> float:
        return sum(lot.quantity for lot in self.lots if lot.opened_on < session)

    def add_fill(self, *, session: date, quantity: float, net_cost: float) -> None:
        if quantity <= 0:
            return
        self.lots.append(
            _ReplayLot(
                opened_on=session,
                quantity=quantity,
                unit_cost=net_cost / quantity,
            )
        )

    def consume_sellable_fifo(self, *, session: date, quantity: float) -> float:
        remaining = quantity
        allocated_cost = 0.0
        for lot in self.lots:
            if remaining <= 1e-8:
                break
            if lot.opened_on >= session or lot.quantity <= 1e-8:
                continue
            consumed = min(lot.quantity, remaining)
            lot.quantity -= consumed
            remaining -= consumed
            allocated_cost += consumed * lot.unit_cost
        self.lots = [lot for lot in self.lots if lot.quantity > 1e-8]
        return allocated_cost


class CrossMarketBacktestService:
    """Replay frozen daily decisions through the realistic minute execution model."""

    ABLATIONS = ("full", "no_kr", "no_gap_sell", "no_range", "zero_cost")

    def __init__(
        self,
        *,
        signal_engine: Optional[CrossMarketSignalEngine] = None,
        initial_cash: float = 100000.0,
    ) -> None:
        self.signal_engine = signal_engine or CrossMarketSignalEngine()
        self.initial_cash = float(initial_cash)
        if self.initial_cash <= 0:
            raise ValueError("initial_cash must be positive")

    def run_ablation_suite(
        self,
        frames: Sequence[ReplayFrame],
        *,
        minimum_sessions: int = 500,
    ) -> Dict[str, object]:
        sessions = sorted({frame.session_date for frame in frames})
        if len(sessions) < minimum_sessions:
            raise ValueError(f"backtest_requires_{minimum_sessions}_sessions")
        results = {
            variant: self.run(frames, variant=variant)
            for variant in self.ABLATIONS
        }
        full_metrics = results["full"]["metrics"]
        theme_session_counts = {
            theme: len({frame.session_date for frame in frames if frame.theme == theme})
            for theme in sorted({frame.theme for frame in frames})
        }
        required_theme_groups = {
            "linked_technology": sum(
                count
                for theme, count in theme_session_counts.items()
                if theme in {"semiconductor", "memory", "equipment", "materials"}
            ),
            "cpo": theme_session_counts.get("cpo", 0),
            "gold": theme_session_counts.get("gold", 0),
        }
        range_frame_count = sum(
            1
            for frame in frames
            if frame.range_signal.get("regime") == "range"
            and frame.range_signal.get("action") in {"buy", "sell", "exit"}
        )
        full_signature = self._ablation_signature(results["full"])
        ablation_effects = {
            variant: self._ablation_signature(results[variant]) != full_signature
            for variant in self.ABLATIONS
            if variant != "full"
        }
        validation = {
            "minimum_sessions": minimum_sessions,
            "minimum_trades": 100,
            "minimum_profit_factor": 1.2,
            "maximum_drawdown_pct": 10.0,
            "sessions_passed": int(full_metrics["session_count"]) >= minimum_sessions,
            "trades_passed": int(full_metrics["trade_count"]) >= 100,
            "profit_factor_passed": float(full_metrics["profit_factor"]) >= 1.2,
            "drawdown_passed": float(full_metrics["max_drawdown_pct"]) <= 10.0,
            "lookahead_passed": int(full_metrics["lookahead_violation_count"]) == 0,
            "theme_session_counts": theme_session_counts,
            "required_theme_groups": required_theme_groups,
            "theme_coverage_passed": all(count > 0 for count in required_theme_groups.values()),
            "range_frame_count": range_frame_count,
            "range_coverage_passed": range_frame_count > 0,
            "ablation_effects": ablation_effects,
            "ablation_effects_passed": all(ablation_effects.values()),
        }
        validation["passed"] = all(
            bool(value)
            for key, value in validation.items()
            if key.endswith("_passed")
        )
        return {
            "strategy_id": self.signal_engine.config.strategy_id,
            "session_count": len(sessions),
            "variants": results,
            "validation": validation,
        }

    @staticmethod
    def _ablation_signature(result: Mapping[str, object]) -> tuple:
        metrics = result.get("metrics") if isinstance(result.get("metrics"), Mapping) else {}
        orders = result.get("orders") if isinstance(result.get("orders"), list) else []
        decisions = []
        fills = []
        for order in orders:
            if not isinstance(order, Mapping):
                continue
            decision = order.get("decision") if isinstance(order.get("decision"), Mapping) else {}
            fill = order.get("fill") if isinstance(order.get("fill"), Mapping) else {}
            decisions.append((decision.get("action"), decision.get("reason")))
            if fill:
                fees = fill.get("fees") if isinstance(fill.get("fees"), Mapping) else {}
                fills.append((
                    fill.get("status"),
                    fill.get("filled_quantity"),
                    fill.get("fill_price"),
                    fees.get("total"),
                ))
        return (
            metrics.get("trade_count"),
            metrics.get("final_equity"),
            tuple(decisions),
            tuple(fills),
        )

    def run(self, frames: Sequence[ReplayFrame], *, variant: str = "full") -> Dict[str, object]:
        if variant not in self.ABLATIONS:
            raise ValueError(f"unsupported_ablation:{variant}")
        ordered = sorted(frames, key=lambda item: (item.session_date, item.signal_at, item.symbol))
        if not ordered:
            raise ValueError("backtest_frames_required")
        self._validate_point_in_time(ordered)

        if variant == "zero_cost":
            fee_schedule = TradeFeeSchedule(
                commission_rate=0.0,
                minimum_commission=0.0,
                stock_sell_stamp_tax_rate=0.0,
                stock_transfer_fee_rate=0.0,
            )
            execution = RealisticMinuteExecutionModel(
                fee_schedule=fee_schedule,
                minimum_slippage_bps=0.0,
                maximum_slippage_bps=0.0,
            )
        else:
            execution = RealisticMinuteExecutionModel()

        cash = self.initial_cash
        positions: Dict[str, _ReplayPosition] = {}
        orders: List[Dict[str, object]] = []
        closed_trades: List[Dict[str, object]] = []
        equity_curve: List[Dict[str, object]] = []
        peak_equity = self.initial_cash
        consecutive_losses = 0
        cooldown_until_index = -1
        sessions = sorted({frame.session_date for frame in ordered})
        frames_by_session = {
            session: [frame for frame in ordered if frame.session_date == session]
            for session in sessions
        }

        for session_index, session in enumerate(sessions):
            day_start_equity = self._equity(cash, positions)
            for frame in frames_by_session[session]:
                position = positions.get(frame.symbol)
                if position is not None:
                    self._apply_corporate_action(frame, position)
                    if frame.cash_dividend_per_share:
                        cash += position.quantity * float(frame.cash_dividend_per_share)

                equity_before = self._equity(cash, positions)
                peak_equity = max(peak_equity, equity_before)
                account_drawdown = (
                    (peak_equity - equity_before) / peak_equity * 100.0
                    if peak_equity > 0
                    else 0.0
                )
                daily_pnl_pct = (
                    (equity_before / day_start_equity - 1.0) * 100.0
                    if day_start_equity > 0
                    else 0.0
                )
                total_exposure = sum(item.quantity * item.last_price for item in positions.values())
                theme_exposure = sum(
                    item.quantity * item.last_price
                    for item in positions.values()
                    if item.theme == frame.theme
                )
                symbol_exposure = position.quantity * position.last_price if position else 0.0
                risk = AccountRiskState(
                    total_exposure_pct=total_exposure / equity_before * 100.0 if equity_before > 0 else 100.0,
                    theme_exposure_pct=theme_exposure / equity_before * 100.0 if equity_before > 0 else 100.0,
                    symbol_exposure_pct=symbol_exposure / equity_before * 100.0 if equity_before > 0 else 100.0,
                    position_count=len(positions),
                    daily_pnl_pct=daily_pnl_pct,
                    account_drawdown_pct=account_drawdown,
                    consecutive_losses=consecutive_losses,
                    in_loss_streak_cooldown=session_index < cooldown_until_index,
                )
                request = self._decision_input(frame, position=position, risk=risk, variant=variant)
                decision = self.signal_engine.decide(request)
                audit: Dict[str, object] = {
                    "session_date": session.isoformat(),
                    "symbol": frame.symbol,
                    "theme": frame.theme,
                    "variant": variant,
                    "decision": decision.to_dict(),
                    "signal_at": frame.signal_at.isoformat(),
                    "execution_bar_at": frame.next_minute_bar.timestamp.isoformat(),
                }

                if decision.action == "buy":
                    target_cash = equity_before * decision.target_position_pct / 100.0
                    raw_quantity = target_cash / frame.next_minute_bar.vwap
                    if str(frame.symbol or "").startswith(("688", "689")):
                        quantity = float(math.floor(raw_quantity))
                        if quantity < 200:
                            quantity = 0.0
                    else:
                        quantity = math.floor(raw_quantity / 100.0) * 100.0
                    if quantity > 0 and cash > 0:
                        fill = execution.execute(
                            PaperOrder(
                                symbol=frame.symbol,
                                side="buy",
                                quantity=quantity,
                                signal_at=frame.signal_at,
                                market="cn",
                                instrument_type=frame.instrument_type,
                                cancel_requested=frame.order_cancel_requested,
                                previous_close=frame.previous_close,
                                price_limit_pct=frame.price_limit_pct,
                            ),
                            frame.next_minute_bar,
                        )
                        audit["fill"] = fill.to_dict()
                        if fill.filled_quantity > 0 and -fill.net_cash_change <= cash:
                            cash += fill.net_cash_change
                            if position is None:
                                position = _ReplayPosition(
                                    symbol=frame.symbol,
                                    theme=frame.theme,
                                    last_price=float(fill.fill_price or frame.close_price),
                                    peak_price=float(fill.fill_price or frame.close_price),
                                    instrument_type=frame.instrument_type,
                                )
                                positions[frame.symbol] = position
                            position.add_fill(
                                session=session,
                                quantity=fill.filled_quantity,
                                net_cost=-fill.net_cash_change,
                            )
                            position.tranche_count += max(1, decision.target_tranche_delta)

                elif decision.action in {"reduce", "exit"} and position is not None:
                    requested_quantity = position.quantity * decision.sell_fraction
                    sellable = position.sellable_quantity(session)
                    fill = execution.execute(
                        PaperOrder(
                            symbol=frame.symbol,
                            side="sell",
                            quantity=requested_quantity,
                            signal_at=frame.signal_at,
                            market="cn",
                            instrument_type=position.instrument_type,
                            sellable_quantity=sellable,
                            cancel_requested=frame.order_cancel_requested,
                            previous_close=frame.previous_close,
                            price_limit_pct=frame.price_limit_pct,
                        ),
                        frame.next_minute_bar,
                    )
                    audit["fill"] = fill.to_dict()
                    if fill.filled_quantity > 0:
                        allocated_cost = position.consume_sellable_fifo(
                            session=session,
                            quantity=fill.filled_quantity,
                        )
                        realized_pnl = fill.net_cash_change - allocated_cost
                        cash += fill.net_cash_change
                        position.tranche_count = max(
                            0,
                            position.tranche_count - max(1, abs(decision.target_tranche_delta)),
                        )
                        closed_trades.append({
                            "session_date": session.isoformat(),
                            "symbol": frame.symbol,
                            "quantity": fill.filled_quantity,
                            "realized_pnl": round(realized_pnl, 6),
                            "fees": fill.fees,
                        })
                        if realized_pnl < 0:
                            consecutive_losses += 1
                            if consecutive_losses >= self.signal_engine.config.loss_streak_limit:
                                cooldown_until_index = session_index + self.signal_engine.config.loss_streak_cooldown_days + 1
                        else:
                            consecutive_losses = 0
                        if position.quantity <= 1e-8:
                            del positions[frame.symbol]

                if frame.symbol in positions:
                    current_position = positions[frame.symbol]
                    current_position.last_price = float(frame.close_price)
                    current_position.peak_price = max(current_position.peak_price, float(frame.close_price))
                orders.append(audit)

            equity = self._equity(cash, positions)
            peak_equity = max(peak_equity, equity)
            equity_curve.append({
                "session_date": session.isoformat(),
                "cash": round(cash, 6),
                "equity": round(equity, 6),
                "position_count": len(positions),
            })

        final_equity = self._equity(cash, positions)
        metrics = self._metrics(
            sessions=sessions,
            equity_curve=equity_curve,
            closed_trades=closed_trades,
            final_equity=final_equity,
        )
        return {
            "variant": variant,
            "metrics": metrics,
            "equity_curve": equity_curve,
            "closed_trades": closed_trades,
            "orders": orders,
            "open_positions": [asdict(position) for position in positions.values()],
        }

    def _decision_input(
        self,
        frame: ReplayFrame,
        *,
        position: Optional[_ReplayPosition],
        risk: AccountRiskState,
        variant: str,
    ) -> StrategyDecisionInput:
        korea_gate = dict(frame.korea_gate)
        asia_market_gate = dict(frame.asia_market_gate)
        if variant == "no_kr":
            korea_gate = {
                "status": "ablated",
                "reason": "korea_gate_disabled",
                "buy_allowed": True,
                "sell_fraction": 0.0,
                "confirmed": True,
            }
            asia_market_gate = {
                **asia_market_gate,
                "available": True,
                "buy_allowed": True,
                "reason": "korea_gate_disabled_for_ablation",
            }
        gap = 0.0 if variant == "no_gap_sell" and position is not None else frame.cn_gap_pct
        range_signal = {} if variant == "no_range" else dict(frame.range_signal)
        position_return = None
        pullback = 0.0
        if position is not None and position.average_cost > 0:
            position_return = (frame.signal_price / position.average_cost - 1.0) * 100.0
            pullback = (
                (position.peak_price - frame.signal_price) / position.peak_price * 100.0
                if position.peak_price > 0
                else 0.0
            )
        estimated_cost = 0.0 if variant == "zero_cost" else TradeFeeSchedule().estimate_round_trip_cost_pct(
            notional=max(1000.0, self.initial_cash * 0.50),
            instrument_type=frame.instrument_type,
            buy_slippage_bps=10.0,
            sell_slippage_bps=10.0,
        )
        return StrategyDecisionInput(
            theme=frame.theme,
            cn_gap_pct=gap,
            entry_phase=frame.entry_phase,
            has_position=position is not None,
            current_tranche_count=position.tranche_count if position is not None else 0,
            sellable_fraction=(
                position.sellable_quantity(frame.session_date) / position.quantity
                if position is not None and position.quantity > 0
                else 0.0
            ),
            reclaimed_open=frame.reclaimed_open,
            above_vwap=frame.above_vwap,
            sector_signal_score=frame.sector_signal_score,
            us_tech_score=frame.us_tech_score,
            us_close_theme_signal=frame.us_close_theme_signal,
            us_premarket_signal=frame.us_premarket_signal,
            nasdaq_futures_signal=frame.nasdaq_futures_signal,
            asia_supply_chain_signal=frame.asia_supply_chain_signal,
            low_position_signal=frame.low_position_signal,
            intraday_pullback_signal=frame.intraday_pullback_signal,
            asia_market_gate=asia_market_gate,
            board_technical_signal=frame.board_technical_signal,
            rotation_signal=frame.rotation_signal,
            next_day_high_open_exit_signal=frame.next_day_high_open_exit_signal,
            korea_gate=korea_gate,
            cpo_signal=frame.cpo_signal,
            gold_signal=frame.gold_signal,
            range_signal=range_signal,
            position_return_pct=position_return,
            pullback_from_peak_pct=pullback,
            expected_gross_edge_pct=frame.expected_gross_edge_pct,
            estimated_round_trip_cost_pct=estimated_cost,
            risk=risk,
        )

    @staticmethod
    def _apply_corporate_action(frame: ReplayFrame, position: _ReplayPosition) -> None:
        if frame.split_ratio is None:
            return
        ratio = float(frame.split_ratio)
        if ratio <= 0:
            raise ValueError("split_ratio_must_be_positive")
        for lot in position.lots:
            lot.quantity *= ratio
            lot.unit_cost /= ratio
        position.peak_price /= ratio
        position.last_price /= ratio

    @staticmethod
    def _equity(cash: float, positions: Mapping[str, _ReplayPosition]) -> float:
        return float(cash) + sum(item.quantity * item.last_price for item in positions.values())

    def _metrics(
        self,
        *,
        sessions: Sequence[date],
        equity_curve: Sequence[Mapping[str, object]],
        closed_trades: Sequence[Mapping[str, object]],
        final_equity: float,
    ) -> Dict[str, object]:
        gains = sum(max(0.0, float(item["realized_pnl"])) for item in closed_trades)
        losses = sum(min(0.0, float(item["realized_pnl"])) for item in closed_trades)
        profit_factor = gains / abs(losses) if losses < 0 else 999.0 if gains > 0 else 0.0
        peak = self.initial_cash
        max_drawdown = 0.0
        for item in equity_curve:
            equity = float(item["equity"])
            peak = max(peak, equity)
            if peak > 0:
                max_drawdown = max(max_drawdown, (peak - equity) / peak * 100.0)
        wins = sum(1 for item in closed_trades if float(item["realized_pnl"]) > 0)
        return {
            "session_count": len(sessions),
            "trade_count": len(closed_trades),
            "win_rate_pct": round(wins / len(closed_trades) * 100.0, 6) if closed_trades else 0.0,
            "profit_factor": round(profit_factor, 6),
            "max_drawdown_pct": round(max_drawdown, 6),
            "initial_cash": round(self.initial_cash, 6),
            "final_equity": round(final_equity, 6),
            "net_pnl": round(final_equity - self.initial_cash, 6),
            "return_pct": round((final_equity / self.initial_cash - 1.0) * 100.0, 6),
            "lookahead_violation_count": 0,
        }

    @staticmethod
    def _validate_point_in_time(frames: Sequence[ReplayFrame]) -> None:
        for frame in frames:
            if frame.signal_at.tzinfo is None or frame.next_minute_bar.timestamp.tzinfo is None:
                raise ValueError("replay_timestamps_require_timezone")
            if frame.next_minute_bar.timestamp <= frame.signal_at:
                raise ValueError("lookahead_execution_bar_not_after_signal")
            if (frame.next_minute_bar.timestamp - frame.signal_at).total_seconds() > 90:
                raise ValueError("execution_bar_not_next_minute")
            if frame.next_minute_bar.timestamp.date() != frame.signal_at.date():
                raise ValueError("execution_bar_must_be_same_session_date")
            for name, observed_at in frame.evidence_timestamps.items():
                if observed_at.tzinfo is None:
                    raise ValueError(f"evidence_timestamp_requires_timezone:{name}")
                if observed_at > frame.signal_at:
                    raise ValueError(f"lookahead_evidence:{name}")
            CrossMarketBacktestService._validate_required_evidence(frame)

    @staticmethod
    def _validate_required_evidence(frame: ReplayFrame) -> None:
        timestamps = {
            str(name).strip().lower(): value.astimezone(timezone.utc)
            for name, value in frame.evidence_timestamps.items()
        }

        def latest_matching(token: str) -> Optional[datetime]:
            values = [value for name, value in timestamps.items() if token in name]
            return max(values) if values else None

        cn_at = latest_matching("cn")
        if cn_at is None:
            raise ValueError("required_evidence_missing:cn")
        if cn_at.date() != frame.signal_at.astimezone(timezone.utc).date():
            raise ValueError("required_evidence_wrong_session:cn")

        if frame.theme in {"semiconductor", "memory", "equipment", "materials"}:
            us_first_hour_at = timestamps.get("us_first_hour")
            us_close_at = timestamps.get("us_close")
            korea_at = latest_matching("korea")
            if us_first_hour_at is None:
                raise ValueError("required_evidence_missing:us_first_hour")
            if us_close_at is None:
                raise ValueError("required_evidence_missing:us_close")
            if korea_at is None:
                raise ValueError("required_evidence_missing:korea")
            if us_first_hour_at > us_close_at:
                raise ValueError("required_evidence_order:us_first_hour_after_close")
            ny_tz = ZoneInfo("America/New_York")
            if us_first_hour_at.astimezone(ny_tz).date() != us_close_at.astimezone(ny_tz).date():
                raise ValueError("required_evidence_wrong_session:us")
            if (frame.signal_at - us_close_at).total_seconds() > 96 * 3600:
                raise ValueError("required_evidence_stale:us_close")
            korea_age = (frame.signal_at - korea_at).total_seconds()
            if korea_age < 0 or korea_age > 120:
                raise ValueError("required_evidence_stale:korea")
        elif frame.theme == "gold":
            gold_at = latest_matching("gold")
            if gold_at is None:
                raise ValueError("required_evidence_missing:gold")
            if (frame.signal_at - gold_at).total_seconds() > 24 * 3600:
                raise ValueError("required_evidence_stale:gold")
