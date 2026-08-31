# -*- coding: utf-8 -*-
"""Built-in vn.py paper gateway for deterministic local simulation.

This module is imported only when the optional vn.py runtime is enabled. It
uses vn.py's real MainEngine/EventEngine contracts while keeping all matching
local and credential-free.
"""

from __future__ import annotations

import math
import os
from copy import copy
from datetime import datetime, timezone
from threading import RLock, Timer
from typing import Any, Callable, Optional
from uuid import uuid4

from vnpy.trader.constant import Direction, Exchange, Status
from vnpy.trader.gateway import BaseGateway
from vnpy.trader.object import (
    AccountData,
    CancelRequest,
    OrderData,
    OrderRequest,
    PositionData,
    SubscribeRequest,
    TradeData,
)

from src.services.cross_market_paper_strategy import (
    MinuteBar,
    PaperOrder,
    RealisticMinuteExecutionModel,
)


FIXED_DELAY_MATCHING_MODE = "fixed_delay_limit"
NEXT_MINUTE_MATCHING_MODE = "next_minute_vwap"
SUPPORTED_MATCHING_MODES = {FIXED_DELAY_MATCHING_MODE, NEXT_MINUTE_MATCHING_MODE}
NEXT_MINUTE_MIN_PROVIDER_SPAN_SECONDS = 45.0
NEXT_MINUTE_MAX_PROVIDER_SPAN_SECONDS = 90.0


class DsaSimulatedGateway(BaseGateway):
    """Credential-free vn.py gateway that fills valid orders after a short delay."""

    default_name = "DSA_SIM"
    default_setting = {
        "initial_balance": 1_000_000.0,
        "fill_delay_ms": 500,
        "matching_mode": FIXED_DELAY_MATCHING_MODE,
        "preserve_state_on_reconnect": True,
        "reject_every_nth_order": 0,
        "duplicate_trade_event_count": 1,
    }
    exchanges = [
        Exchange.SSE,
        Exchange.SZSE,
        Exchange.BSE,
        Exchange.SEHK,
        Exchange.SMART,
    ]
    connect_without_settings = True

    def __init__(self, event_engine: Any, gateway_name: str) -> None:
        super().__init__(event_engine, gateway_name)
        self._lock = RLock()
        self._connected = False
        self._closed = False
        self._ever_connected = False
        self._connect_count = 0
        self._session_id = uuid4().hex
        self._order_count = 0
        self._trade_count = 0
        self._balance = 1_000_000.0
        self._fill_delay_seconds = 0.5
        self._matching_mode = FIXED_DELAY_MATCHING_MODE
        self._max_evidence_age_seconds = 120.0
        self._reject_every_nth_order = 0
        self._duplicate_trade_event_count = 1
        self._orders: dict[str, OrderData] = {}
        self._timers: dict[str, Timer] = {}
        self._positions: dict[str, dict[str, Any]] = {}
        self._quote_provider: Optional[Callable[[str], Any]] = None
        self._quote_manager: Any = None
        self._order_quote_baselines: dict[str, dict[str, Any]] = {}
        self._execution_model = RealisticMinuteExecutionModel()

    def connect(self, setting: dict) -> None:
        """Start the local simulator and publish its initial account snapshot."""

        payload = setting if isinstance(setting, dict) else {}
        timers_to_cancel: list[Timer] = []
        with self._lock:
            preserve_state = _as_bool(payload.get("preserve_state_on_reconnect"), True)
            reset_state = self._ever_connected and not preserve_state
            if not self._ever_connected or reset_state:
                timers_to_cancel = list(self._timers.values())
                self._timers.clear()
                self._balance = max(
                    0.0,
                    _as_float(payload.get("initial_balance"), 1_000_000.0),
                )
                if reset_state:
                    self._session_id = uuid4().hex
                self._order_count = 0
                self._trade_count = 0
                self._orders.clear()
                self._positions.clear()
                for item in _initial_positions(payload.get("initial_positions")):
                    self._positions[item["vt_symbol"]] = {
                        "symbol": item["symbol"],
                        "exchange": item["exchange"],
                        "volume": item["volume"],
                        "price": item["price"],
                    }
                self._order_quote_baselines.clear()
            matching_mode = str(
                payload.get("matching_mode")
                or os.getenv("DSA_SIM_MATCHING_MODE")
                or FIXED_DELAY_MATCHING_MODE
            ).strip().lower()
            self._matching_mode = (
                matching_mode
                if matching_mode in SUPPORTED_MATCHING_MODES
                else FIXED_DELAY_MATCHING_MODE
            )
            default_delay_ms = (
                65_000.0
                if self._matching_mode == NEXT_MINUTE_MATCHING_MODE
                else 500.0
            )
            configured_delay_ms = payload.get("fill_delay_ms")
            if configured_delay_ms is None and self._matching_mode == NEXT_MINUTE_MATCHING_MODE:
                configured_delay_ms = os.getenv("DSA_SIM_NEXT_MINUTE_DELAY_MS")
            maximum_delay_ms = (
                120_000.0
                if self._matching_mode == NEXT_MINUTE_MATCHING_MODE
                else 10_000.0
            )
            delay_ms = min(
                maximum_delay_ms,
                max(10.0, _as_float(configured_delay_ms, default_delay_ms)),
            )
            self._fill_delay_seconds = delay_ms / 1000.0
            self._max_evidence_age_seconds = min(
                300.0,
                max(30.0, _as_float(payload.get("max_evidence_age_seconds"), 120.0)),
            )
            quote_provider = payload.get("quote_provider")
            self._quote_provider = quote_provider if callable(quote_provider) else None
            self._reject_every_nth_order = min(
                10_000,
                max(0, _as_int(payload.get("reject_every_nth_order"), 0)),
            )
            self._duplicate_trade_event_count = min(
                5,
                max(1, _as_int(payload.get("duplicate_trade_event_count"), 1)),
            )
            self._connected = True
            self._closed = False
            self._ever_connected = True
            self._connect_count += 1
            pending_orders = [
                copy(order) for order in self._orders.values() if order.is_active()
            ]
        for timer in timers_to_cancel:
            timer.cancel()
        self.write_log("DSA built-in simulated gateway connected")
        for order in pending_orders:
            self.on_order(order)
            self._schedule_fill(order.orderid)
        self._publish_account()
        self.query_position()

    def close(self) -> None:
        """Stop pending fills and close the simulator."""

        with self._lock:
            self._connected = False
            self._closed = True
            timers = list(self._timers.values())
            self._timers.clear()
        for timer in timers:
            timer.cancel()
        self.write_log("DSA built-in simulated gateway disconnected; state retained")

    def subscribe(self, req: SubscribeRequest) -> None:
        """Accept subscriptions; prices are supplied by DSA order requests."""

        return None

    def send_order(self, req: OrderRequest) -> str:
        """Create an order and schedule the configured simulated matcher."""

        with self._lock:
            self._order_count += 1
            orderid = f"{self._session_id}-{self._order_count}"
            order = req.create_order_data(orderid, self.gateway_name)
            order.datetime = datetime.now()
            self._orders[orderid] = order

            rejection = self._order_rejection_reason(req)
            if (
                rejection is None
                and self._reject_every_nth_order > 0
                and self._order_count % self._reject_every_nth_order == 0
            ):
                rejection = "simulated_configured_rejection"
            if rejection:
                order.status = Status.REJECTED
                order.rejected_reason = rejection
                snapshot = copy(order)
                vt_orderid = order.vt_orderid
            else:
                snapshot = None
                vt_orderid = order.vt_orderid

        if snapshot is not None:
            self.on_order(snapshot)
            return vt_orderid

        if self._matching_mode == NEXT_MINUTE_MATCHING_MODE:
            baseline, reason = self._capture_quote_snapshot(order.symbol)
            if baseline is None:
                with self._lock:
                    current = self._orders.get(orderid)
                    if current is None:  # pragma: no cover - defensive only.
                        return vt_orderid
                    current.status = Status.REJECTED
                    current.rejected_reason = reason or "simulated_baseline_evidence_unavailable"
                    current.datetime = datetime.now()
                    snapshot = copy(current)
                self.on_order(snapshot)
                return vt_orderid

            with self._lock:
                current = self._orders[orderid]
                current.status = Status.NOTTRADED
                self._order_quote_baselines[orderid] = baseline
                snapshot = copy(current)
                self._schedule_fill_locked(orderid)
            self.on_order(snapshot)
            return vt_orderid

        with self._lock:
            current = self._orders[orderid]
            current.status = Status.NOTTRADED
            snapshot = copy(current)
            self._schedule_fill_locked(orderid)
        self.on_order(snapshot)
        return vt_orderid

    def cancel_order(self, req: CancelRequest) -> None:
        """Cancel an order that has not reached the delayed fill callback."""

        with self._lock:
            order = self._orders.get(str(req.orderid))
            if order is None or not order.is_active():
                return
            timer = self._timers.pop(order.orderid, None)
            if timer is not None:
                timer.cancel()
            self._order_quote_baselines.pop(order.orderid, None)
            order.status = Status.CANCELLED
            order.datetime = datetime.now()
            snapshot = copy(order)
        self.on_order(snapshot)

    def query_account(self) -> None:
        self._publish_account()

    def query_position(self) -> None:
        with self._lock:
            snapshots = [self._position_data(item) for item in self._positions.values()]
        for position in snapshots:
            self.on_position(position)

    def get_state_snapshot(self) -> dict[str, Any]:
        """Return a read-only state summary for diagnostics and reconnect soak tests."""

        with self._lock:
            return {
                "connected": self._connected and not self._closed,
                "connect_count": self._connect_count,
                "session_id": self._session_id,
                "balance": self._balance,
                "order_count": len(self._orders),
                "trade_count": self._trade_count,
                "active_order_ids": sorted(
                    orderid for orderid, order in self._orders.items() if order.is_active()
                ),
                "position_count": sum(
                    1 for item in self._positions.values() if float(item["volume"]) != 0
                ),
                "fault_injection": {
                    "reject_every_nth_order": self._reject_every_nth_order,
                    "duplicate_trade_event_count": self._duplicate_trade_event_count,
                },
                "matching": {
                    "mode": self._matching_mode,
                    "fill_delay_seconds": self._fill_delay_seconds,
                    "min_provider_span_seconds": NEXT_MINUTE_MIN_PROVIDER_SPAN_SECONDS,
                    "max_provider_span_seconds": NEXT_MINUTE_MAX_PROVIDER_SPAN_SECONDS,
                    "spread_source": "normalized_best_bid_ask_with_4bp_fallback",
                    "pending_baseline_count": len(self._order_quote_baselines),
                },
            }

    def _schedule_fill(self, orderid: str) -> None:
        with self._lock:
            if not self._connected or self._closed:
                return
            order = self._orders.get(orderid)
            if order is None or not order.is_active() or orderid in self._timers:
                return
            self._schedule_fill_locked(orderid)

    def _schedule_fill_locked(self, orderid: str) -> None:
        timer = Timer(self._fill_delay_seconds, self._fill_order, args=(orderid,))
        timer.daemon = True
        self._timers[orderid] = timer
        timer.start()

    def _order_rejection_reason(self, req: OrderRequest) -> str | None:
        if not self._connected or self._closed:
            return "simulated_gateway_not_connected"
        if req.exchange not in self.exchanges:
            return "unsupported_exchange"
        if _as_float(req.volume, 0.0) <= 0:
            return "invalid_volume"
        if _as_float(req.price, 0.0) <= 0:
            return "positive_limit_price_required"
        return None

    def _fill_order(self, orderid: str) -> None:
        if self._matching_mode == NEXT_MINUTE_MATCHING_MODE:
            self._match_next_minute_order(orderid)
            return

        with self._lock:
            self._timers.pop(orderid, None)
            order = self._orders.get(orderid)
            if order is None or not order.is_active() or self._closed:
                return
            order.traded = order.volume
            order.status = Status.ALLTRADED
            order.datetime = datetime.now()
            self._trade_count += 1
            trade = TradeData(
                gateway_name=self.gateway_name,
                symbol=order.symbol,
                exchange=order.exchange,
                orderid=order.orderid,
                tradeid=f"{self._session_id}-{self._trade_count}",
                direction=order.direction,
                offset=order.offset,
                price=order.price,
                volume=order.volume,
                datetime=order.datetime,
            )
            self._apply_fill(trade)
            order_snapshot = copy(order)
            duplicate_trade_event_count = self._duplicate_trade_event_count
        self.on_order(order_snapshot)
        for _ in range(duplicate_trade_event_count):
            self.on_trade(copy(trade))
        self._publish_account()
        self.query_position()

    def _match_next_minute_order(self, orderid: str) -> None:
        with self._lock:
            self._timers.pop(orderid, None)
            order = self._orders.get(orderid)
            baseline = self._order_quote_baselines.get(orderid)
            if order is None or baseline is None or not order.is_active() or self._closed:
                return
            order_snapshot = copy(order)

        current, evidence_reason = self._capture_quote_snapshot(order_snapshot.symbol)
        if current is None:
            self._cancel_for_matching_evidence(orderid, evidence_reason)
            return
        if current["provider_at"] <= baseline["provider_at"]:
            self._cancel_for_matching_evidence(
                orderid,
                "simulated_next_minute_timestamp_not_advanced",
            )
            return
        provider_span_seconds = (
            current["provider_at"] - baseline["provider_at"]
        ).total_seconds()
        if provider_span_seconds < NEXT_MINUTE_MIN_PROVIDER_SPAN_SECONDS:
            self._cancel_for_matching_evidence(
                orderid,
                "simulated_next_minute_provider_span_too_short",
            )
            return
        if provider_span_seconds > NEXT_MINUTE_MAX_PROVIDER_SPAN_SECONDS:
            self._cancel_for_matching_evidence(
                orderid,
                "simulated_next_minute_provider_span_too_long",
            )
            return
        if current["provider"] != baseline["provider"]:
            self._cancel_for_matching_evidence(
                orderid,
                "simulated_quote_provider_changed",
            )
            return
        volume_delta = float(current["volume"]) - float(baseline["volume"])
        amount_delta = float(current["amount"]) - float(baseline["amount"])
        if volume_delta < 0 or amount_delta < 0:
            self._cancel_for_matching_evidence(orderid, "simulated_cumulative_quote_regressed")
            return
        if (volume_delta > 0) != (amount_delta > 0):
            self._cancel_for_matching_evidence(
                orderid,
                "simulated_cumulative_quote_delta_inconsistent",
            )
            return

        start_price = float(baseline["price"])
        end_price = float(current["price"])
        vwap = amount_delta / volume_delta if volume_delta > 0 and amount_delta > 0 else end_price
        high = max(start_price, end_price, vwap)
        low = min(start_price, end_price, vwap)
        bar = MinuteBar(
            timestamp=current["provider_at"],
            open=start_price,
            high=high,
            low=low,
            close=end_price,
            volume=volume_delta,
            amount=amount_delta if amount_delta > 0 else None,
            bid_ask_spread_bps=_quote_spread_bps(current),
            atr_1m_pct=abs(end_price - start_price) / start_price * 100.0,
            suspended=volume_delta <= 0,
            limit_up_price=current.get("limit_up_price") or baseline.get("limit_up_price"),
            limit_down_price=current.get("limit_down_price") or baseline.get("limit_down_price"),
        )
        side = "buy" if order_snapshot.direction == Direction.LONG else "sell"
        requested = max(0.0, float(order_snapshot.volume) - float(order_snapshot.traded))
        fill = self._execution_model.execute(
            PaperOrder(
                symbol=order_snapshot.symbol,
                side=side,
                quantity=requested,
                signal_at=baseline["provider_at"],
                declared_quantity=float(order_snapshot.volume),
                limit_price=float(order_snapshot.price),
                instrument_type=_instrument_type(order_snapshot.symbol),
                market=(
                    "cn"
                    if order_snapshot.exchange
                    in {Exchange.SSE, Exchange.SZSE, Exchange.BSE}
                    else "other"
                ),
                sellable_quantity=requested if side == "sell" else None,
                previous_close=current.get("pre_close"),
            ),
            bar,
        )
        if fill.filled_quantity <= 0 or fill.fill_price is None:
            with self._lock:
                order = self._orders.get(orderid)
                if order is None or not order.is_active() or self._closed:
                    return
                order.rejected_reason = fill.reason or "simulated_next_minute_unfilled"
                order.datetime = datetime.now()
                self._order_quote_baselines[orderid] = current
                snapshot = copy(order)
                self._schedule_fill_locked(orderid)
            self.on_order(snapshot)
            return

        with self._lock:
            order = self._orders.get(orderid)
            if order is None or not order.is_active() or self._closed:
                return
            order.traded = min(
                float(order.volume),
                float(order.traded) + float(fill.filled_quantity),
            )
            order.status = (
                Status.ALLTRADED
                if float(order.volume) - float(order.traded) <= 1e-9
                else Status.PARTTRADED
            )
            order.datetime = datetime.now()
            self._trade_count += 1
            trade = TradeData(
                gateway_name=self.gateway_name,
                symbol=order.symbol,
                exchange=order.exchange,
                orderid=order.orderid,
                tradeid=f"{self._session_id}-{self._trade_count}",
                direction=order.direction,
                offset=order.offset,
                price=float(fill.fill_price),
                volume=float(fill.filled_quantity),
                datetime=order.datetime,
            )
            trade.dsa_execution_mode = NEXT_MINUTE_MATCHING_MODE
            trade.dsa_reference_price = fill.reference_price
            trade.dsa_slippage_bps = fill.slippage_bps
            trade.dsa_provider_span_seconds = provider_span_seconds
            trade.dsa_minute_volume = volume_delta
            trade.dsa_participation_rate = (
                float(fill.filled_quantity) / volume_delta
                if volume_delta > 0
                else None
            )
            trade.dsa_bid_ask_spread_bps = bar.bid_ask_spread_bps
            trade.dsa_atr_1m_pct = bar.atr_1m_pct
            self._apply_fill(trade)
            if order.status == Status.ALLTRADED:
                self._order_quote_baselines.pop(orderid, None)
            else:
                self._order_quote_baselines[orderid] = current
                self._schedule_fill_locked(orderid)
            order_snapshot = copy(order)
            duplicate_trade_event_count = self._duplicate_trade_event_count
        self.on_order(order_snapshot)
        for _ in range(duplicate_trade_event_count):
            self.on_trade(copy(trade))
        self._publish_account()
        self.query_position()

    def _cancel_for_matching_evidence(self, orderid: str, reason: Optional[str]) -> None:
        with self._lock:
            order = self._orders.get(orderid)
            if order is None or not order.is_active():
                return
            order.status = Status.CANCELLED
            order.rejected_reason = reason or "simulated_next_minute_evidence_unavailable"
            order.datetime = datetime.now()
            self._order_quote_baselines.pop(orderid, None)
            snapshot = copy(order)
        self.on_order(snapshot)

    def _capture_quote_snapshot(self, symbol: str) -> tuple[Optional[dict[str, Any]], Optional[str]]:
        try:
            provider = self._quote_provider or self._default_quote_provider
            quote = provider(symbol)
        except Exception as exc:  # noqa: BLE001 - simulated execution must fail closed.
            return None, f"simulated_quote_error:{type(exc).__name__}"
        if quote is None:
            return None, "simulated_quote_unavailable"
        provider_at = _as_utc_datetime(getattr(quote, "provider_timestamp", None))
        if provider_at is None:
            return None, "simulated_quote_timestamp_unavailable"
        age_seconds = (datetime.now(timezone.utc) - provider_at).total_seconds()
        if age_seconds < -1 or age_seconds > self._max_evidence_age_seconds:
            return None, "simulated_quote_timestamp_stale"
        price = _finite_positive(getattr(quote, "price", None))
        volume = _finite_nonnegative(getattr(quote, "volume", None))
        amount = _finite_nonnegative(getattr(quote, "amount", None))
        provider = _provider_identity(quote)
        if price is None or volume is None or amount is None or provider is None:
            return None, "simulated_quote_cumulative_fields_unavailable"
        pre_close = _finite_positive(getattr(quote, "pre_close", None))
        limit_up_price = _finite_positive(getattr(quote, "limit_up_price", None))
        limit_down_price = _finite_positive(getattr(quote, "limit_down_price", None))
        bid_price = _finite_positive(getattr(quote, "bid_price", None))
        ask_price = _finite_positive(getattr(quote, "ask_price", None))
        if bid_price is None or ask_price is None or ask_price < bid_price:
            bid_price = None
            ask_price = None
        if _is_cn_symbol(symbol) and pre_close is None and (
            limit_up_price is None or limit_down_price is None
        ):
            return None, "simulated_cn_price_limit_evidence_unavailable"
        return {
            "provider_at": provider_at,
            "provider": provider,
            "price": price,
            "volume": volume,
            "amount": amount,
            "pre_close": pre_close,
            "limit_up_price": limit_up_price,
            "limit_down_price": limit_down_price,
            "bid_price": bid_price,
            "ask_price": ask_price,
        }, None

    def _default_quote_provider(self, symbol: str) -> Any:
        if self._quote_manager is None:
            from data_provider.base import DataFetcherManager

            self._quote_manager = DataFetcherManager()
        return self._quote_manager.get_realtime_quote_with_provider_timestamp(symbol)

    def _apply_fill(self, trade: TradeData) -> None:
        notional = float(trade.price) * float(trade.volume)
        sign = 1.0 if trade.direction == Direction.LONG else -1.0
        self._balance -= sign * notional
        key = trade.vt_symbol
        current = self._positions.setdefault(
            key,
            {
                "symbol": trade.symbol,
                "exchange": trade.exchange,
                "volume": 0.0,
                "price": 0.0,
            },
        )
        old_volume = float(current["volume"])
        new_volume = old_volume + sign * float(trade.volume)
        if sign > 0 and new_volume > 0:
            current["price"] = ((old_volume * float(current["price"])) + notional) / new_volume
        elif new_volume <= 0:
            current["price"] = 0.0
        current["volume"] = new_volume

    def _publish_account(self) -> None:
        with self._lock:
            account = AccountData(
                gateway_name=self.gateway_name,
                accountid="paper",
                balance=self._balance,
                frozen=0.0,
            )
        self.on_account(account)

    def _position_data(self, item: dict[str, Any]) -> PositionData:
        return PositionData(
            gateway_name=self.gateway_name,
            symbol=str(item["symbol"]),
            exchange=item["exchange"],
            direction=Direction.NET,
            volume=float(item["volume"]),
            price=float(item["price"]),
        )


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_utc_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _finite_positive(value: Any) -> Optional[float]:
    parsed = _as_float(value, float("nan"))
    return parsed if math.isfinite(parsed) and parsed > 0 else None


def _finite_nonnegative(value: Any) -> Optional[float]:
    parsed = _as_float(value, float("nan"))
    return parsed if math.isfinite(parsed) and parsed >= 0 else None


def _initial_positions(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("initial_positions_must_be_list")

    positions: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("initial_position_must_be_object")
        raw_symbol = str(item.get("symbol") or "").strip().upper()
        symbol, _, symbol_exchange = raw_symbol.partition(".")
        volume = _finite_positive(item.get("quantity", item.get("volume")))
        price = _finite_nonnegative(item.get("avg_cost", item.get("price")))
        if not symbol or volume is None or price is None:
            raise ValueError("initial_position_fields_invalid")
        exchange = _initial_position_exchange(
            symbol=symbol,
            market=item.get("market"),
            exchange=item.get("exchange") or symbol_exchange,
        )
        positions.append(
            {
                "vt_symbol": f"{symbol}.{exchange.value}",
                "symbol": symbol,
                "exchange": exchange,
                "volume": volume,
                "price": price,
            }
        )
    return positions


def _initial_position_exchange(*, symbol: str, market: Any, exchange: Any) -> Exchange:
    exchange_text = str(exchange or "").strip().upper()
    exchange_aliases = {
        "XSHG": Exchange.SSE,
        "XSHE": Exchange.SZSE,
        "XBSE": Exchange.BSE,
        "BJ": Exchange.BSE,
        "HKEX": Exchange.SEHK,
    }
    if exchange_text in exchange_aliases:
        return exchange_aliases[exchange_text]
    if exchange_text:
        try:
            return Exchange(exchange_text)
        except ValueError:
            try:
                return Exchange[exchange_text]
            except KeyError as exc:
                raise ValueError("initial_position_exchange_invalid") from exc

    market_text = str(market or "").strip().lower()
    if market_text in {"hk", "hong_kong"}:
        return Exchange.SEHK
    if market_text in {"us", "usa"}:
        return Exchange.SMART
    if market_text not in {"", "cn", "china", "a_share"}:
        raise ValueError("initial_position_market_invalid")
    if symbol.startswith(("43", "83", "87", "88", "92")):
        return Exchange.BSE
    if symbol.startswith(("5", "6", "9")):
        return Exchange.SSE
    return Exchange.SZSE


def _quote_spread_bps(snapshot: dict[str, Any]) -> Optional[float]:
    bid = _finite_positive(snapshot.get("bid_price"))
    ask = _finite_positive(snapshot.get("ask_price"))
    if bid is None or ask is None or ask < bid:
        return None
    midpoint = (bid + ask) / 2.0
    return (ask - bid) / midpoint * 10000.0 if midpoint > 0 else None


def _instrument_type(symbol: str) -> str:
    code = str(symbol or "").strip().upper().split(".", 1)[0]
    return "etf" if code.startswith(("15", "16", "50", "51", "52", "56", "58")) else "stock"


def _is_cn_symbol(symbol: str) -> bool:
    code = str(symbol or "").strip().upper().split(".", 1)[0]
    return len(code) == 6 and code.isdigit()


def _provider_identity(quote: Any) -> Optional[str]:
    value = getattr(quote, "source", None) or getattr(quote, "provider", None)
    value = getattr(value, "value", value)
    text = str(value or "").strip().lower()
    return text or None
