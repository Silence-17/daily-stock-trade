# -*- coding: utf-8 -*-
"""Tests for optional vn.py adapter mapping."""

from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import patch

from src.services.vnpy_adapter import (
    VnpyAdapterError,
    VnpyEventSubscriptionBridge,
    VnpyMainEngineBridge,
    build_vnpy_cancel_request_payload,
    build_vnpy_order_request_payload,
    create_vnpy_cancel_request,
    create_vnpy_order_request,
    get_vnpy_adapter_status,
    get_vnpy_bridge_status,
    get_vnpy_event_bridge_status,
    normalize_vnpy_symbol,
)


class VnpyAdapterTestCase(unittest.TestCase):
    def test_builds_a_share_limit_order_payload_with_lot_rounding(self) -> None:
        payload = build_vnpy_order_request_payload(
            symbol="SH600519",
            side="buy",
            market="cn",
            cash_amount=1050,
            price=10,
            source="alphasift_auto",
            plan_uid="plan-1",
        )

        self.assertEqual(payload["symbol"], "600519")
        self.assertEqual(payload["exchange"], "SSE")
        self.assertEqual(payload["vt_symbol"], "600519.SSE")
        self.assertEqual(payload["direction"], "LONG")
        self.assertEqual(payload["offset"], "NONE")
        self.assertEqual(payload["type"], "LIMIT")
        self.assertEqual(payload["volume"], 100.0)
        self.assertEqual(payload["price"], 10.0)
        self.assertEqual(payload["reference"], "dsa:alphasift_auto:plan-1")

    def test_builds_cross_market_payloads(self) -> None:
        sz_symbol, sz_exchange = normalize_vnpy_symbol("000001.SZ", market="cn")
        hk_payload = build_vnpy_order_request_payload(
            symbol="700",
            side="buy",
            market="hk",
            quantity=200,
            price=350,
        )
        us_payload = build_vnpy_order_request_payload(
            symbol="AAPL",
            side="sell",
            market="us",
            quantity=1.5,
            price=200,
        )

        self.assertEqual((sz_symbol, sz_exchange), ("000001", "SZSE"))
        self.assertEqual(hk_payload["symbol"], "00700")
        self.assertEqual(hk_payload["exchange"], "SEHK")
        self.assertEqual(hk_payload["volume"], 200.0)
        self.assertEqual(us_payload["exchange"], "SMART")
        self.assertEqual(us_payload["direction"], "SHORT")
        self.assertEqual(us_payload["volume"], 1.5)

    def test_rejects_volume_that_rounds_below_cn_lot(self) -> None:
        with self.assertRaisesRegex(VnpyAdapterError, "resolved volume is zero"):
            build_vnpy_order_request_payload(
                symbol="600519",
                side="buy",
                market="cn",
                cash_amount=500,
                price=10,
            )

    def test_status_reports_local_fallback_when_vnpy_import_is_missing(self) -> None:
        def _missing_import(module_name: str):
            raise ModuleNotFoundError(f"No module named {module_name!r}", name="vnpy")

        with patch("src.services.vnpy_adapter.importlib.import_module", side_effect=_missing_import):
            status = get_vnpy_adapter_status()

        self.assertFalse(status["available"])
        self.assertEqual(status["mode"], "local_paper_fallback")
        self.assertEqual(status["reason"], "missing_module")
        self.assertFalse(status["order_request_supported"])

    def test_instantiates_order_request_when_fake_vnpy_modules_exist(self) -> None:
        installed = _install_fake_vnpy_modules()
        try:
            payload = build_vnpy_order_request_payload(
                symbol="600519",
                side="buy",
                market="cn",
                quantity=100,
                price=10,
                reference="dsa:test",
            )
            status = get_vnpy_adapter_status()
            order_request = create_vnpy_order_request(payload)
        finally:
            _restore_modules(installed)

        self.assertTrue(status["available"])
        self.assertEqual(order_request.symbol, "600519")
        self.assertEqual(order_request.exchange, "SSE")
        self.assertEqual(order_request.direction, "LONG")
        self.assertEqual(order_request.offset, "NONE")
        self.assertEqual(order_request.type, "LIMIT")
        self.assertEqual(order_request.volume, 100.0)
        self.assertEqual(order_request.price, 10.0)
        self.assertEqual(order_request.reference, "dsa:test")

    def test_bridge_status_requires_main_engine_and_gateway_name(self) -> None:
        installed = _install_fake_vnpy_modules()
        try:
            missing_engine = get_vnpy_bridge_status(gateway_name="SIM")
            missing_gateway = get_vnpy_bridge_status(main_engine=_FakeMainEngine())
            ready = get_vnpy_bridge_status(main_engine=_FakeMainEngine(), gateway_name="SIM")
        finally:
            _restore_modules(installed)

        self.assertFalse(missing_engine["available"])
        self.assertEqual(missing_engine["reason"], "main_engine_not_configured")
        self.assertFalse(missing_gateway["available"])
        self.assertEqual(missing_gateway["reason"], "gateway_name_not_configured")
        self.assertTrue(ready["available"])
        self.assertEqual(ready["mode"], "vnpy_main_engine")
        self.assertIsNone(ready["connection_confirmed"])
        self.assertEqual(ready["connection_status"], "unknown")

    def test_bridge_status_reads_gateway_connection_hook(self) -> None:
        installed = _install_fake_vnpy_modules()
        main_engine = _FakeMainEngine()
        main_engine.gateway = types.SimpleNamespace(
            get_state_snapshot=lambda: {"connected": False}
        )
        try:
            status = get_vnpy_bridge_status(
                main_engine=main_engine,
                gateway_name="SIM",
            )
            main_engine.gateway.get_state_snapshot = lambda: {"connected": True}
            recovered = get_vnpy_bridge_status(
                main_engine=main_engine,
                gateway_name="SIM",
            )
        finally:
            _restore_modules(installed)

        self.assertTrue(status["available"])
        self.assertFalse(status["connection_confirmed"])
        self.assertEqual(status["connection_status"], "disconnected")
        self.assertEqual(
            status["connection_confirmation_source"],
            "get_state_snapshot",
        )
        self.assertTrue(recovered["connection_confirmed"])
        self.assertEqual(recovered["connection_status"], "connected")

    def test_main_engine_bridge_submits_order_request(self) -> None:
        installed = _install_fake_vnpy_modules()
        main_engine = _FakeMainEngine()
        try:
            payload = build_vnpy_order_request_payload(
                symbol="600519",
                side="buy",
                market="cn",
                quantity=100,
                price=10,
                reference="dsa:test",
            )
            result = VnpyMainEngineBridge(
                main_engine=main_engine,
                gateway_name="SIM",
            ).send_order(payload)
        finally:
            _restore_modules(installed)

        self.assertTrue(result["accepted"])
        self.assertEqual(result["status"], "submitted")
        self.assertEqual(result["vt_orderid"], "SIM.1")
        self.assertEqual(main_engine.calls[0][1], "SIM")
        self.assertEqual(main_engine.calls[0][0].symbol, "600519")

    def test_main_engine_bridge_requests_cancel(self) -> None:
        installed = _install_fake_vnpy_modules()
        main_engine = _FakeMainEngine()
        try:
            payload = build_vnpy_cancel_request_payload(
                vt_orderid="SIM.1",
                symbol="600519",
                market="cn",
            )
            cancel_request = create_vnpy_cancel_request(payload)
            result = VnpyMainEngineBridge(
                main_engine=main_engine,
                gateway_name="SIM",
            ).cancel_order(payload)
        finally:
            _restore_modules(installed)

        self.assertEqual(payload["orderid"], "1")
        self.assertEqual(cancel_request.symbol, "600519")
        self.assertEqual(cancel_request.exchange, "SSE")
        self.assertTrue(result["accepted"])
        self.assertEqual(result["status"], "cancel_requested")
        self.assertEqual(main_engine.cancel_calls[0][1], "SIM")
        self.assertEqual(main_engine.cancel_calls[0][0].orderid, "1")

    def test_main_engine_bridge_snapshots_order_and_filters_trades(self) -> None:
        main_engine = _FakeMainEngine()
        matching_trade = {"vt_orderid": "SIM.1", "vt_tradeid": "SIM.T1"}
        main_engine.orders["SIM.1"] = {"vt_orderid": "SIM.1", "status": "alltraded"}
        main_engine.trades = [matching_trade, {"vt_orderid": "SIM.2", "vt_tradeid": "SIM.T2"}]

        snapshot = VnpyMainEngineBridge(
            main_engine=main_engine,
            gateway_name="SIM",
        ).snapshot_order("SIM.1")

        self.assertTrue(snapshot["supported"])
        self.assertTrue(snapshot["order_found"])
        self.assertEqual(snapshot["trade_count"], 1)
        self.assertEqual(snapshot["trades"], [matching_trade])

    def test_event_subscription_bridge_registers_and_unregisters_handlers(self) -> None:
        installed = _install_fake_vnpy_modules()
        event_engine = _FakeEventEngine()
        calls = []
        try:
            status = get_vnpy_event_bridge_status(event_engine=event_engine)
            bridge = VnpyEventSubscriptionBridge(
                event_engine=event_engine,
                order_handler=lambda event: calls.append(("order", event)),
                trade_handler=lambda event: calls.append(("trade", event)),
                account_handler=lambda event: calls.append(("account", event)),
                position_handler=lambda event: calls.append(("position", event)),
            )
            registered = bridge.register()
            event_engine.emit("eOrder.", {"vt_orderid": "SIM.1"})
            unregistered = bridge.unregister()
            event_engine.emit("eOrder.", {"vt_orderid": "SIM.2"})
        finally:
            _restore_modules(installed)

        self.assertTrue(status["available"])
        self.assertEqual(registered["registered_count"], 4)
        self.assertEqual(calls, [("order", {"vt_orderid": "SIM.1"})])
        self.assertFalse(unregistered["registered"])


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

    constant_module.Exchange = Exchange
    constant_module.Direction = Direction
    constant_module.Offset = Offset
    constant_module.OrderType = OrderType
    trader_event_module.EVENT_ORDER = "eOrder."
    trader_event_module.EVENT_TRADE = "eTrade."
    trader_event_module.EVENT_ACCOUNT = "eAccount."
    trader_event_module.EVENT_POSITION = "ePosition."
    object_module.OrderRequest = OrderRequest
    object_module.CancelRequest = CancelRequest
    event_module.EventEngine = EventEngine
    engine_module.MainEngine = MainEngine
    vnpy_module.trader = trader_module
    vnpy_module.event = event_module
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


class _FakeMainEngine:
    def __init__(self) -> None:
        self.calls = []
        self.cancel_calls = []
        self.orders = {}
        self.trades = []
        self.gateway = None

    def get_gateway(self, gateway_name):
        return self.gateway

    def send_order(self, request, gateway_name):
        self.calls.append((request, gateway_name))
        return f"{gateway_name}.1"

    def cancel_order(self, request, gateway_name):
        self.cancel_calls.append((request, gateway_name))
        return True

    def get_order(self, vt_orderid):
        return self.orders.get(vt_orderid)

    def get_all_trades(self):
        return list(self.trades)


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


def _restore_modules(previous: dict[str, object]) -> None:
    for name, module in previous.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


if __name__ == "__main__":
    unittest.main()
