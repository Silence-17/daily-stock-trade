# -*- coding: utf-8 -*-
"""Built-in vn.py paper gateway for deterministic local simulation.

This module is imported only when the optional vn.py runtime is enabled. It
uses vn.py's real MainEngine/EventEngine contracts while keeping all matching
local and credential-free.
"""

from __future__ import annotations

from copy import copy
from datetime import datetime
from threading import RLock, Timer
from typing import Any
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


class DsaSimulatedGateway(BaseGateway):
    """Credential-free vn.py gateway that fills valid orders after a short delay."""

    default_name = "DSA_SIM"
    default_setting = {
        "initial_balance": 1_000_000.0,
        "fill_delay_ms": 500,
        "preserve_state_on_reconnect": True,
        "reject_every_nth_order": 0,
        "duplicate_trade_event_count": 1,
    }
    exchanges = [Exchange.SSE, Exchange.SZSE, Exchange.SEHK, Exchange.SMART]
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
        self._reject_every_nth_order = 0
        self._duplicate_trade_event_count = 1
        self._orders: dict[str, OrderData] = {}
        self._timers: dict[str, Timer] = {}
        self._positions: dict[str, dict[str, Any]] = {}

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
            delay_ms = min(10_000.0, max(10.0, _as_float(payload.get("fill_delay_ms"), 500.0)))
            self._fill_delay_seconds = delay_ms / 1000.0
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
        """Create a vn.py order and schedule a deterministic full fill."""

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
                self.on_order(copy(order))
                return order.vt_orderid

            order.status = Status.NOTTRADED
            self.on_order(copy(order))
            self._schedule_fill_locked(orderid)
            return order.vt_orderid

    def cancel_order(self, req: CancelRequest) -> None:
        """Cancel an order that has not reached the delayed fill callback."""

        with self._lock:
            order = self._orders.get(str(req.orderid))
            if order is None or not order.is_active():
                return
            timer = self._timers.pop(order.orderid, None)
            if timer is not None:
                timer.cancel()
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
