# -*- coding: utf-8 -*-
"""Integration tests for the optional built-in vn.py simulated gateway."""

from __future__ import annotations

import importlib.util
import time
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace


@unittest.skipUnless(importlib.util.find_spec("vnpy"), "optional vn.py runtime is not installed")
class VnpySimulatedGatewayTestCase(unittest.TestCase):
    def test_gateway_accepts_bse_as_a_cn_exchange(self) -> None:
        from vnpy.trader.constant import Exchange

        from src.services.vnpy_simulated_gateway import DsaSimulatedGateway

        self.assertIn(Exchange.BSE, DsaSimulatedGateway.exchanges)

    def test_next_minute_vwap_mode_fills_bse_order(self) -> None:
        from vnpy.event import EventEngine
        from vnpy.trader.constant import Direction, Exchange, Offset, OrderType, Status
        from vnpy.trader.engine import MainEngine
        from vnpy.trader.object import OrderRequest

        from src.services.vnpy_simulated_gateway import DsaSimulatedGateway

        now = datetime.now(timezone.utc)
        quotes = iter([
            SimpleNamespace(
                provider_timestamp=(now - timedelta(seconds=60)).isoformat(),
                source="tencent",
                price=10.0,
                volume=100_000,
                amount=1_000_000,
                pre_close=9.8,
                bid_price=9.99,
                ask_price=10.01,
            ),
            SimpleNamespace(
                provider_timestamp=now.isoformat(),
                source="tencent",
                price=10.1,
                volume=110_000,
                amount=1_100_000,
                pre_close=9.8,
                bid_price=10.04,
                ask_price=10.06,
            ),
        ])
        event_engine = EventEngine()
        main_engine = MainEngine(event_engine)
        main_engine.add_gateway(DsaSimulatedGateway, "DSA_SIM")
        main_engine.connect(
            {
                "matching_mode": "next_minute_vwap",
                "fill_delay_ms": 20,
                "quote_provider": lambda _symbol: next(quotes),
            },
            "DSA_SIM",
        )
        try:
            vt_orderid = main_engine.send_order(
                OrderRequest(
                    symbol="920045",
                    exchange=Exchange.BSE,
                    direction=Direction.LONG,
                    type=OrderType.LIMIT,
                    volume=105,
                    price=10.2,
                    offset=Offset.NONE,
                    reference="dsa:test:bse_next_minute_vwap",
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
            trades = [
                trade
                for trade in main_engine.get_all_trades()
                if trade.vt_orderid == vt_orderid
            ]
            self.assertEqual(order.status, Status.ALLTRADED)
            self.assertEqual(order.exchange, Exchange.BSE)
            self.assertEqual(order.traded, 105.0)
            self.assertEqual(len(trades), 1)
            self.assertEqual(trades[0].volume, 105.0)
        finally:
            main_engine.close()

    def test_next_minute_vwap_mode_uses_quote_deltas_and_dynamic_slippage(self) -> None:
        from vnpy.event import EventEngine
        from vnpy.trader.constant import Direction, Exchange, Offset, OrderType, Status
        from vnpy.trader.engine import MainEngine
        from vnpy.trader.object import OrderRequest

        from src.services.vnpy_simulated_gateway import DsaSimulatedGateway

        now = datetime.now(timezone.utc)
        quotes = iter([
            SimpleNamespace(
                provider_timestamp=(now - timedelta(seconds=60)).isoformat(),
                source="tencent",
                price=10.0,
                volume=100_000,
                amount=1_000_000,
                pre_close=9.8,
                bid_price=9.99,
                ask_price=10.01,
            ),
            SimpleNamespace(
                provider_timestamp=now.isoformat(),
                source="tencent",
                price=10.1,
                volume=110_000,
                amount=1_100_500,
                pre_close=9.8,
                bid_price=10.04,
                ask_price=10.06,
            ),
        ])
        event_engine = EventEngine()
        main_engine = MainEngine(event_engine)
        main_engine.add_gateway(DsaSimulatedGateway, "DSA_SIM")
        main_engine.connect(
            {
                "matching_mode": "next_minute_vwap",
                "fill_delay_ms": 20,
                "quote_provider": lambda _symbol: next(quotes),
            },
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
                    price=10.2,
                    offset=Offset.NONE,
                    reference="dsa:test:next_minute_vwap",
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
            gateway = main_engine.get_gateway("DSA_SIM")
            self.assertEqual(order.status, Status.ALLTRADED)
            self.assertEqual(len(trades), 1)
            self.assertGreater(trades[0].price, 10.07)
            self.assertLessEqual(trades[0].price, 10.1)
            self.assertEqual(trades[0].volume, 100)
            self.assertEqual(trades[0].dsa_execution_mode, "next_minute_vwap")
            self.assertEqual(trades[0].dsa_reference_price, 10.05)
            self.assertGreaterEqual(trades[0].dsa_slippage_bps, 2.0)
            self.assertEqual(trades[0].dsa_provider_span_seconds, 60.0)
            self.assertEqual(trades[0].dsa_minute_volume, 10_000)
            self.assertEqual(trades[0].dsa_participation_rate, 0.01)
            matching = gateway.get_state_snapshot()["matching"]
            self.assertEqual(matching["mode"], "next_minute_vwap")
            self.assertEqual(matching["min_provider_span_seconds"], 45.0)
            self.assertEqual(matching["max_provider_span_seconds"], 90.0)
            self.assertEqual(
                matching["spread_source"],
                "normalized_best_bid_ask_with_4bp_fallback",
            )
        finally:
            main_engine.close()

    def test_next_minute_vwap_mode_applies_five_percent_participation_cap(self) -> None:
        from vnpy.event import EventEngine
        from vnpy.trader.constant import Direction, Exchange, Offset, OrderType, Status
        from vnpy.trader.engine import MainEngine
        from vnpy.trader.object import CancelRequest, OrderRequest

        from src.services.vnpy_simulated_gateway import DsaSimulatedGateway

        now = datetime.now(timezone.utc)
        snapshots = [
            SimpleNamespace(
                provider_timestamp=(now - timedelta(seconds=60)).isoformat(),
                source="tencent",
                price=10.0,
                volume=100_000,
                amount=1_000_000,
                pre_close=9.8,
            ),
            SimpleNamespace(
                provider_timestamp=now.isoformat(),
                source="tencent",
                price=10.0,
                volume=102_000,
                amount=1_020_000,
                pre_close=9.8,
            ),
        ]
        event_engine = EventEngine()
        main_engine = MainEngine(event_engine)
        main_engine.add_gateway(DsaSimulatedGateway, "DSA_SIM")
        main_engine.connect(
            {
                "matching_mode": "next_minute_vwap",
                "fill_delay_ms": 200,
                "quote_provider": lambda _symbol: snapshots.pop(0),
            },
            "DSA_SIM",
        )
        try:
            vt_orderid = main_engine.send_order(
                OrderRequest(
                    symbol="600519",
                    exchange=Exchange.SSE,
                    direction=Direction.LONG,
                    type=OrderType.LIMIT,
                    volume=1_000,
                    price=10.1,
                    offset=Offset.NONE,
                    reference="dsa:test:participation_cap",
                ),
                "DSA_SIM",
            )
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                order = main_engine.get_order(vt_orderid)
                if order is not None and order.status == Status.PARTTRADED:
                    break
                time.sleep(0.02)

            order = main_engine.get_order(vt_orderid)
            self.assertEqual(order.status, Status.PARTTRADED)
            self.assertEqual(order.traded, 100)
            main_engine.cancel_order(
                CancelRequest(orderid=order.orderid, symbol=order.symbol, exchange=order.exchange),
                "DSA_SIM",
            )
        finally:
            main_engine.close()

    def test_next_minute_vwap_mode_completes_star_odd_lot_remainder(self) -> None:
        from vnpy.event import EventEngine
        from vnpy.trader.constant import Direction, Exchange, Offset, OrderType, Status
        from vnpy.trader.engine import MainEngine
        from vnpy.trader.object import OrderRequest

        from src.services.vnpy_simulated_gateway import DsaSimulatedGateway

        now = datetime.now(timezone.utc)
        snapshots = [
            SimpleNamespace(
                provider_timestamp=(now - timedelta(seconds=90)).isoformat(),
                source="tencent",
                price=10.0,
                volume=100_000,
                amount=1_000_000,
                pre_close=9.8,
            ),
            SimpleNamespace(
                provider_timestamp=(now - timedelta(seconds=45)).isoformat(),
                source="tencent",
                price=10.0,
                volume=104_200,
                amount=1_042_000,
                pre_close=9.8,
            ),
            SimpleNamespace(
                provider_timestamp=now.isoformat(),
                source="tencent",
                price=10.0,
                volume=105_200,
                amount=1_052_000,
                pre_close=9.8,
            ),
        ]
        event_engine = EventEngine()
        main_engine = MainEngine(event_engine)
        main_engine.add_gateway(DsaSimulatedGateway, "DSA_SIM")
        main_engine.connect(
            {
                "matching_mode": "next_minute_vwap",
                "fill_delay_ms": 20,
                "quote_provider": lambda _symbol: snapshots.pop(0),
            },
            "DSA_SIM",
        )
        try:
            vt_orderid = main_engine.send_order(
                OrderRequest(
                    symbol="688981",
                    exchange=Exchange.SSE,
                    direction=Direction.LONG,
                    type=OrderType.LIMIT,
                    volume=216,
                    price=10.1,
                    offset=Offset.NONE,
                    reference="dsa:test:star_remainder",
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
            trades = [
                trade
                for trade in main_engine.get_all_trades()
                if trade.vt_orderid == vt_orderid
            ]
            self.assertEqual(order.status, Status.ALLTRADED)
            self.assertEqual(order.traded, 216.0)
            self.assertEqual([trade.volume for trade in trades], [210.0, 6.0])
        finally:
            main_engine.close()

    def test_next_minute_vwap_mode_cancels_on_inconsistent_cumulative_evidence(self) -> None:
        from vnpy.event import EventEngine
        from vnpy.trader.constant import Direction, Exchange, Offset, OrderType, Status
        from vnpy.trader.engine import MainEngine
        from vnpy.trader.object import OrderRequest

        from src.services.vnpy_simulated_gateway import DsaSimulatedGateway

        now = datetime.now(timezone.utc)
        quotes = iter([
            SimpleNamespace(
                provider_timestamp=(now - timedelta(seconds=60)).isoformat(),
                source="tencent",
                price=10.0,
                volume=100_000,
                amount=1_000_000,
                pre_close=9.8,
            ),
            SimpleNamespace(
                provider_timestamp=now.isoformat(),
                source="tencent",
                price=10.1,
                volume=101_000,
                amount=1_000_000,
                pre_close=9.8,
            ),
        ])
        event_engine = EventEngine()
        main_engine = MainEngine(event_engine)
        main_engine.add_gateway(DsaSimulatedGateway, "DSA_SIM")
        main_engine.connect(
            {
                "matching_mode": "next_minute_vwap",
                "fill_delay_ms": 20,
                "quote_provider": lambda _symbol: next(quotes),
            },
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
                    price=10.2,
                    offset=Offset.NONE,
                    reference="dsa:test:inconsistent_quote_delta",
                ),
                "DSA_SIM",
            )
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                order = main_engine.get_order(vt_orderid)
                if order is not None and order.status == Status.CANCELLED:
                    break
                time.sleep(0.02)

            order = main_engine.get_order(vt_orderid)
            self.assertEqual(order.status, Status.CANCELLED)
            self.assertEqual(
                order.rejected_reason,
                "simulated_cumulative_quote_delta_inconsistent",
            )
            self.assertEqual(main_engine.get_all_trades(), [])
        finally:
            main_engine.close()

    def test_next_minute_vwap_mode_requires_one_minute_provider_span(self) -> None:
        from vnpy.event import EventEngine
        from vnpy.trader.constant import Direction, Exchange, Offset, OrderType, Status
        from vnpy.trader.engine import MainEngine
        from vnpy.trader.object import OrderRequest

        from src.services.vnpy_simulated_gateway import DsaSimulatedGateway

        cases = {
            1: "simulated_next_minute_provider_span_too_short",
            100: "simulated_next_minute_provider_span_too_long",
        }
        for provider_span_seconds, expected_reason in cases.items():
            with self.subTest(provider_span_seconds=provider_span_seconds):
                now = datetime.now(timezone.utc)
                quotes = iter([
                    SimpleNamespace(
                        provider_timestamp=(
                            now - timedelta(seconds=provider_span_seconds)
                        ).isoformat(),
                        source="tencent",
                        price=10.0,
                        volume=100_000,
                        amount=1_000_000,
                        pre_close=9.8,
                    ),
                    SimpleNamespace(
                        provider_timestamp=now.isoformat(),
                        source="tencent",
                        price=10.1,
                        volume=101_000,
                        amount=1_010_100,
                        pre_close=9.8,
                    ),
                ])
                event_engine = EventEngine()
                main_engine = MainEngine(event_engine)
                main_engine.add_gateway(DsaSimulatedGateway, "DSA_SIM")
                main_engine.connect(
                    {
                        "matching_mode": "next_minute_vwap",
                        "fill_delay_ms": 20,
                        "quote_provider": lambda _symbol: next(quotes),
                    },
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
                            price=10.2,
                            offset=Offset.NONE,
                            reference="dsa:test:provider_span",
                        ),
                        "DSA_SIM",
                    )
                    deadline = time.monotonic() + 3.0
                    while time.monotonic() < deadline:
                        order = main_engine.get_order(vt_orderid)
                        if order is not None and order.status == Status.CANCELLED:
                            break
                        time.sleep(0.02)

                    order = main_engine.get_order(vt_orderid)
                    self.assertEqual(order.status, Status.CANCELLED)
                    self.assertEqual(order.rejected_reason, expected_reason)
                    self.assertEqual(main_engine.get_all_trades(), [])
                finally:
                    main_engine.close()

    def test_next_minute_vwap_mode_rejects_cn_quote_without_price_limit_evidence(self) -> None:
        from vnpy.event import EventEngine
        from vnpy.trader.constant import Direction, Exchange, Offset, OrderType, Status
        from vnpy.trader.engine import MainEngine
        from vnpy.trader.object import OrderRequest

        from src.services.vnpy_simulated_gateway import DsaSimulatedGateway

        quote = SimpleNamespace(
            provider_timestamp=datetime.now(timezone.utc).isoformat(),
            source="tencent",
            price=10.0,
            volume=100_000,
            amount=1_000_000,
            pre_close=None,
        )
        event_engine = EventEngine()
        main_engine = MainEngine(event_engine)
        main_engine.add_gateway(DsaSimulatedGateway, "DSA_SIM")
        main_engine.connect(
            {
                "matching_mode": "next_minute_vwap",
                "fill_delay_ms": 20,
                "quote_provider": lambda _symbol: quote,
            },
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
                    price=10.2,
                    offset=Offset.NONE,
                    reference="dsa:test:missing_price_limit_evidence",
                ),
                "DSA_SIM",
            )

            deadline = time.monotonic() + 3.0
            order = None
            while time.monotonic() < deadline:
                order = main_engine.get_order(vt_orderid)
                if order is not None:
                    break
                time.sleep(0.02)
            self.assertEqual(order.status, Status.REJECTED)
            self.assertEqual(
                order.rejected_reason,
                "simulated_cn_price_limit_evidence_unavailable",
            )
            self.assertEqual(main_engine.get_all_trades(), [])
        finally:
            main_engine.close()

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
            {
                "initial_balance": 100_000,
                "fill_delay_ms": 20,
                "matching_mode": "fixed_delay_limit",
            },
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
            "matching_mode": "fixed_delay_limit",
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
                "matching_mode": "fixed_delay_limit",
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
            main_engine.connect(
                {"fill_delay_ms": 20, "matching_mode": "fixed_delay_limit"},
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
