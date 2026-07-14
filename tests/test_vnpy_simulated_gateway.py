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
