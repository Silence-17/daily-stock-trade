# -*- coding: utf-8 -*-
"""Regression coverage for manually submitted vn.py bridge fills."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.config import Config
from src.services.vnpy_paper_trading_service import VnpyPaperTradingService
from src.storage import DatabaseManager


class _PriceFetcher:
    def get_realtime_quote(self, _symbol: str) -> SimpleNamespace:
        return SimpleNamespace(
            price=10.0,
            provider="unit-test",
            provider_timestamp=datetime.now(timezone.utc).isoformat(),
        )


class VnpyManualBridgeFillTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.env_path = root / ".env"
        self.db_path = root / "portfolio.db"
        self.config_path = root / "vnpy-paper.json"
        self.env_path.write_text(
            "\n".join(
                [
                    "STOCK_LIST=600519",
                    "GEMINI_API_KEY=test",
                    "ADMIN_AUTH_ENABLED=false",
                    f"DATABASE_PATH={self.db_path}",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        os.environ["ENV_FILE"] = str(self.env_path)
        os.environ["DATABASE_PATH"] = str(self.db_path)
        Config.reset_instance()
        DatabaseManager.reset_instance()
        self.service = VnpyPaperTradingService(
            data_fetcher_manager=_PriceFetcher(),
            config_path=self.config_path,
            vnpy_main_engine=object(),
        )
        self.service.update_settings({"vnpy_gateway_name": "SIM"})

    def tearDown(self) -> None:
        DatabaseManager.reset_instance()
        Config.reset_instance()
        os.environ.pop("ENV_FILE", None)
        os.environ.pop("DATABASE_PATH", None)
        self.temp_dir.cleanup()

    def _submit_manual_order(self, *, quantity: float = 100) -> dict:
        bridge = MagicMock()
        bridge.send_order.return_value = {
            "accepted": True,
            "status": "submitted",
            "vt_orderid": "SIM.MANUAL.1",
            "gateway_name": "SIM",
            "order_request_payload": {
                "symbol": "600519",
                "market": "cn",
                "side": "buy",
                "volume": quantity,
                "price": 10.0,
            },
        }
        with (
            patch(
                "src.services.vnpy_paper_trading_service.get_vnpy_bridge_status",
                return_value={"available": True, "connection_confirmed": True},
            ),
            patch(
                "src.services.vnpy_paper_trading_service.VnpyMainEngineBridge",
                return_value=bridge,
            ),
        ):
            return self.service.submit_order(
                symbol="600519",
                side="buy",
                market="cn",
                quantity=quantity,
                price=10.0,
                source="manual",
                execution_route="vnpy_bridge",
            )

    def test_manual_order_callbacks_accumulate_and_deduplicate_fills(self) -> None:
        submitted = self._submit_manual_order()
        account_id = int(submitted["account_id"])
        callback_service = VnpyPaperTradingService(
            data_fetcher_manager=_PriceFetcher(),
            config_path=self.config_path,
            vnpy_main_engine=object(),
        )

        order_callback = callback_service.sync_vnpy_order_callback(
            vt_orderid="SIM.MANUAL.1",
            status="nottraded",
            symbol="600519",
            side="buy",
            market="cn",
            volume=100,
            traded=0,
            price=10.0,
        )
        first = callback_service.sync_vnpy_trade_callback(
            vt_orderid="SIM.MANUAL.1",
            vt_tradeid="SIM.MANUAL.T1",
            symbol="600519",
            side="buy",
            market="cn",
            quantity=40,
            price=10.0,
            trade_date=date(2026, 7, 24),
        )
        second = callback_service.sync_vnpy_trade_callback(
            vt_orderid="SIM.MANUAL.1",
            vt_tradeid="SIM.MANUAL.T2",
            symbol="600519",
            side="buy",
            market="cn",
            quantity=60,
            price=10.2,
            trade_date=date(2026, 7, 24),
        )
        duplicate = callback_service.sync_vnpy_trade_callback(
            vt_orderid="SIM.MANUAL.1",
            vt_tradeid="SIM.MANUAL.T2",
            symbol="600519",
            side="buy",
            market="cn",
            quantity=60,
            price=10.2,
            trade_date=date(2026, 7, 24),
        )
        stale_order_callback = callback_service.sync_vnpy_order_callback(
            vt_orderid="SIM.MANUAL.1",
            status="nottraded",
            symbol="600519",
            side="buy",
            market="cn",
            volume=100,
            traded=0,
            price=10.0,
        )

        self.assertTrue(submitted["accepted"])
        self.assertTrue(order_callback["accepted"])
        self.assertEqual(first["status"], "part_filled")
        self.assertEqual(first["raw"]["fill_sync"]["remaining_quantity"], 60.0)
        self.assertEqual(second["status"], "filled")
        self.assertEqual(second["quantity"], 100.0)
        self.assertEqual(second["raw"]["fill_sync"]["trade_count"], 2)
        self.assertTrue(duplicate["raw"]["duplicate_callback"])
        self.assertEqual(stale_order_callback["status"], "filled")
        trades = callback_service.portfolio.list_trade_events(
            account_id=account_id,
            page=1,
            page_size=20,
        )
        self.assertEqual(len(trades["items"]), 2)
        self.assertEqual(sum(item["quantity"] for item in trades["items"]), 100.0)

    def test_manual_trade_callback_rejects_identity_mismatch(self) -> None:
        submitted = self._submit_manual_order()

        result = self.service.sync_vnpy_trade_callback(
            vt_orderid="SIM.MANUAL.1",
            vt_tradeid="SIM.MANUAL.BAD",
            symbol="000001",
            side="buy",
            market="cn",
            quantity=100,
            price=10.0,
        )

        self.assertFalse(result["accepted"])
        self.assertEqual(result["reason"], "vnpy_manual_callback_mismatch")
        trades = self.service.portfolio.list_trade_events(
            account_id=int(submitted["account_id"]),
            page=1,
        )
        self.assertEqual(trades["items"], [])

    def test_unknown_planless_trade_callback_remains_fail_closed(self) -> None:
        result = self.service.sync_vnpy_trade_callback(
            vt_orderid="SIM.UNKNOWN",
            vt_tradeid="SIM.UNKNOWN.T1",
            symbol="600519",
            side="buy",
            market="cn",
            quantity=100,
            price=10.0,
        )

        self.assertFalse(result["accepted"])
        self.assertEqual(result["reason"], "vnpy_trade_plan_not_found")


if __name__ == "__main__":
    unittest.main()
