# -*- coding: utf-8 -*-
"""Integration tests for the optional built-in vn.py simulated gateway."""

from __future__ import annotations

import importlib.util
import time
import unittest


@unittest.skipUnless(importlib.util.find_spec("vnpy"), "optional vn.py runtime is not installed")
class VnpySimulatedGatewayTestCase(unittest.TestCase):
    def test_real_main_engine_receives_order_trade_account_and_position_events(self) -> None:
        from vnpy.event import EventEngine
        from vnpy.trader.constant import Direction, Exchange, Offset, OrderType, Status
        from vnpy.trader.engine import MainEngine
        from vnpy.trader.object import OrderRequest

        from src.services.vnpy_simulated_gateway import DsaSimulatedGateway

        event_engine = EventEngine()
        main_engine = MainEngine(event_engine)
        main_engine.add_gateway(DsaSimulatedGateway, "DSA_SIM")
        main_engine.connect(
            {"initial_balance": 100_000, "fill_delay_ms": 20},
            "DSA_SIM",
        )
        try:
            vt_orderid = main_engine.send_order(
                OrderRequest(
                    symbol="600519",
                    exchange=Exchange.SSE,
                    direction=Direction.LONG,
                    type=OrderType.LIMIT,
                    volume=100,
                    price=10,
                    offset=Offset.NONE,
                    reference="dsa:test:simulated_gateway",
                ),
                "DSA_SIM",
            )

            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                order = main_engine.get_order(vt_orderid)
                if order is not None and order.status == Status.ALLTRADED:
                    break
                time.sleep(0.02)

            order = main_engine.get_order(vt_orderid)
            trades = [trade for trade in main_engine.get_all_trades() if trade.vt_orderid == vt_orderid]
            accounts = main_engine.get_all_accounts()
            positions = main_engine.get_all_positions()

            self.assertIsNotNone(order)
            self.assertEqual(order.status, Status.ALLTRADED)
            self.assertEqual(order.traded, 100)
            self.assertEqual(len(trades), 1)
            self.assertEqual(trades[0].price, 10)
            self.assertTrue(accounts)
            self.assertTrue(any(position.symbol == "600519" for position in positions))
        finally:
            main_engine.close()

    def test_reconnect_preserves_pending_order_and_fills_exactly_once(self) -> None:
        from vnpy.event import EventEngine
        from vnpy.trader.constant import Direction, Exchange, Offset, OrderType, Status
        from vnpy.trader.engine import MainEngine
        from vnpy.trader.object import OrderRequest

        from src.services.vnpy_simulated_gateway import DsaSimulatedGateway

        event_engine = EventEngine()
        main_engine = MainEngine(event_engine)
        main_engine.add_gateway(DsaSimulatedGateway, "DSA_SIM")
        gateway = main_engine.get_gateway("DSA_SIM")
        self.assertIsNotNone(gateway)
        assert gateway is not None
        setting = {
            "initial_balance": 100_000,
            "fill_delay_ms": 200,
            "preserve_state_on_reconnect": True,
        }
        main_engine.connect(setting, "DSA_SIM")
        try:
            vt_orderid = main_engine.send_order(
                OrderRequest(
                    symbol="600519",
                    exchange=Exchange.SSE,
                    direction=Direction.LONG,
                    type=OrderType.LIMIT,
                    volume=100,
                    price=10,
                    offset=Offset.NONE,
                    reference="dsa:test:reconnect",
                ),
                "DSA_SIM",
            )
            time.sleep(0.03)
            gateway.close()
            disconnected = gateway.get_state_snapshot()
            self.assertFalse(disconnected["connected"])
            self.assertEqual(disconnected["active_order_ids"], [vt_orderid.split(".", 1)[1]])

            main_engine.connect(setting, "DSA_SIM")
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                order = main_engine.get_order(vt_orderid)
                if order is not None and order.status == Status.ALLTRADED:
                    break
                time.sleep(0.02)

            order = main_engine.get_order(vt_orderid)
            trades = [
                trade
                for trade in main_engine.get_all_trades()
                if trade.vt_orderid == vt_orderid
            ]
            state = gateway.get_state_snapshot()
            self.assertIsNotNone(order)
            self.assertEqual(order.status, Status.ALLTRADED)
            self.assertEqual(len(trades), 1)
            self.assertEqual(state["connect_count"], 2)
            self.assertEqual(state["trade_count"], 1)
            self.assertEqual(state["balance"], 99_000.0)
            self.assertEqual(state["active_order_ids"], [])
            self.assertEqual(state["position_count"], 1)
        finally:
            main_engine.close()

    def test_fault_injection_rejects_order_and_duplicates_trade_callback(self) -> None:
        from vnpy.event import EventEngine
        from vnpy.trader.constant import Direction, Exchange, Offset, OrderType, Status
        from vnpy.trader.engine import MainEngine
        from vnpy.trader.event import EVENT_TRADE
        from vnpy.trader.object import OrderRequest

        from src.services.vnpy_simulated_gateway import DsaSimulatedGateway

        event_engine = EventEngine()
        main_engine = MainEngine(event_engine)
        main_engine.add_gateway(DsaSimulatedGateway, "DSA_SIM")
        observed_trade_ids = []

        def capture_trade(event) -> None:
            observed_trade_ids.append(event.data.vt_tradeid)

        event_engine.register(EVENT_TRADE, capture_trade)
        main_engine.connect(
            {
                "fill_delay_ms": 20,
                "reject_every_nth_order": 2,
                "duplicate_trade_event_count": 2,
            },
            "DSA_SIM",
        )
        request = OrderRequest(
            symbol="600519",
            exchange=Exchange.SSE,
            direction=Direction.LONG,
            type=OrderType.LIMIT,
            volume=100,
            price=10,
            offset=Offset.NONE,
            reference="dsa:test:fault_matrix",
        )
        try:
            filled_orderid = main_engine.send_order(request, "DSA_SIM")
            rejected_orderid = main_engine.send_order(request, "DSA_SIM")

            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                filled = main_engine.get_order(filled_orderid)
                if filled is not None and filled.status == Status.ALLTRADED and len(observed_trade_ids) >= 2:
                    break
                time.sleep(0.02)

            filled = main_engine.get_order(filled_orderid)
            rejected = main_engine.get_order(rejected_orderid)
            trades = [
                trade
                for trade in main_engine.get_all_trades()
                if trade.vt_orderid == filled_orderid
            ]
            gateway = main_engine.get_gateway("DSA_SIM")
            self.assertIsNotNone(gateway)
            assert gateway is not None
            state = gateway.get_state_snapshot()

            self.assertEqual(filled.status, Status.ALLTRADED)
            self.assertEqual(rejected.status, Status.REJECTED)
            self.assertEqual(rejected.rejected_reason, "simulated_configured_rejection")
            self.assertEqual(len(observed_trade_ids), 2)
            self.assertEqual(len(set(observed_trade_ids)), 1)
            self.assertEqual(len(trades), 1)
            self.assertEqual(
                state["fault_injection"],
                {
                    "reject_every_nth_order": 2,
                    "duplicate_trade_event_count": 2,
                },
            )
        finally:
            event_engine.unregister(EVENT_TRADE, capture_trade)
            main_engine.close()

    def test_fresh_gateway_instances_do_not_reuse_order_or_trade_ids(self) -> None:
        from vnpy.event import EventEngine
        from vnpy.trader.constant import Direction, Exchange, Offset, OrderType, Status
        from vnpy.trader.engine import MainEngine
        from vnpy.trader.object import OrderRequest

        from src.services.vnpy_simulated_gateway import DsaSimulatedGateway

        observed_ids = []
        for _ in range(2):
            event_engine = EventEngine()
            main_engine = MainEngine(event_engine)
            main_engine.add_gateway(DsaSimulatedGateway, "DSA_SIM")
            main_engine.connect({"fill_delay_ms": 20}, "DSA_SIM")
            try:
                vt_orderid = main_engine.send_order(
                    OrderRequest(
                        symbol="600519",
                        exchange=Exchange.SSE,
                        direction=Direction.LONG,
                        type=OrderType.LIMIT,
                        volume=100,
                        price=10,
                        offset=Offset.NONE,
                        reference="dsa:test:fresh_gateway_ids",
                    ),
                    "DSA_SIM",
                )
                deadline = time.monotonic() + 3.0
                while time.monotonic() < deadline:
                    order = main_engine.get_order(vt_orderid)
                    if order is not None and order.status == Status.ALLTRADED:
                        break
                    time.sleep(0.02)
                trades = [
                    trade
                    for trade in main_engine.get_all_trades()
                    if trade.vt_orderid == vt_orderid
                ]
                self.assertEqual(len(trades), 1)
                observed_ids.append((vt_orderid, trades[0].vt_tradeid))
            finally:
                main_engine.close()

        self.assertNotEqual(observed_ids[0][0], observed_ids[1][0])
        self.assertNotEqual(observed_ids[0][1], observed_ids[1][1])
