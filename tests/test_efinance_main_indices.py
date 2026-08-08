import os
import sys
import time
import types
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from data_provider.efinance_fetcher import (
    EfinanceFetcher,
    _extract_efinance_data_date,
    _extract_efinance_provider_timestamp,
    _normalize_efinance_data_date,
    _normalize_efinance_provider_timestamp,
)


def _available_circuit_breaker():
    return types.SimpleNamespace(
        is_available=lambda _key: True,
        record_success=lambda _key: None,
        record_failure=lambda _key, _error: None,
    )


class TestEfinancePriority(unittest.TestCase):
    def test_explicit_priority_is_read_after_config_load(self):
        config = types.SimpleNamespace(enable_eastmoney_patch=False)

        with (
            patch.dict(os.environ, {"EFINANCE_PRIORITY": "1"}),
            patch("data_provider.efinance_fetcher.get_config", return_value=config),
        ):
            fetcher = EfinanceFetcher()

        self.assertEqual(fetcher.priority, 1)

    def test_invalid_priority_uses_default(self):
        config = types.SimpleNamespace(enable_eastmoney_patch=False)

        with (
            patch.dict(os.environ, {"EFINANCE_PRIORITY": "invalid"}),
            patch("data_provider.efinance_fetcher.get_config", return_value=config),
        ):
            fetcher = EfinanceFetcher()

        self.assertEqual(fetcher.priority, 0)


class TestEfinanceMainIndices(unittest.TestCase):
    def test_get_main_indices_prefers_jinkai_column_for_open_price(self):
        fetcher = EfinanceFetcher()
        fake_df = pd.DataFrame(
            {
                "股票代码": ["000001"],
                "最新价": [3200.0],
                "涨跌幅": [0.63],
                "涨跌额": [20.0],
                "今开": [3188.0],
                "开盘": [0.0],
                "最高": [3215.0],
                "最低": [3170.0],
                "成交量": [123456789],
                "成交额": [9876543210.0],
                "振幅": [1.2],
                "更新时间": ["2026-07-23 14:30:00"],
                "最新交易日": ["2026-07-23"],
            }
        )
        fake_efinance = types.SimpleNamespace(
            stock=types.SimpleNamespace(get_realtime_quotes=lambda *args, **kwargs: fake_df)
        )

        with patch.dict(sys.modules, {"efinance": fake_efinance}):
            with patch.object(fetcher, "_set_random_user_agent", return_value=None), patch.object(
                fetcher, "_enforce_rate_limit", return_value=None
            ):
                data = fetcher.get_main_indices(region="cn")

        self.assertIsNotNone(data)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["code"], "sh000001")
        self.assertEqual(data[0]["name"], "上证指数")
        self.assertAlmostEqual(data[0]["open"], 3188.0)
        self.assertAlmostEqual(data[0]["current"], 3200.0)
        self.assertEqual(data[0]["data_granularity"], "realtime")
        expected_timestamp = (
            datetime(2026, 7, 23, 14, 30)
            .replace(tzinfo=datetime.now().astimezone().tzinfo)
            .astimezone(timezone.utc)
            .isoformat()
        )
        self.assertEqual(data[0]["provider_timestamp"], expected_timestamp)
        self.assertEqual(data[0]["data_date"], "2026-07-23")

    def test_get_main_indices_falls_back_to_kaipan_when_jinkai_is_missing(self):
        fetcher = EfinanceFetcher()
        fake_df = pd.DataFrame(
            {
                "股票代码": ["000001"],
                "最新价": [3200.0],
                "涨跌幅": [0.63],
                "涨跌额": [20.0],
                "今开": [""],
                "开盘": [3186.0],
                "最高": [3215.0],
                "最低": [3170.0],
                "成交量": [123456789],
                "成交额": [9876543210.0],
                "振幅": [1.2],
            }
        )
        fake_efinance = types.SimpleNamespace(
            stock=types.SimpleNamespace(get_realtime_quotes=lambda *args, **kwargs: fake_df)
        )

        with patch.dict(sys.modules, {"efinance": fake_efinance}):
            with patch.object(fetcher, "_set_random_user_agent", return_value=None), patch.object(
                fetcher, "_enforce_rate_limit", return_value=None
            ):
                data = fetcher.get_main_indices(region="cn")

        self.assertIsNotNone(data)
        self.assertEqual(len(data), 1)
        self.assertAlmostEqual(data[0]["open"], 3186.0)

    def test_provider_timestamp_accepts_epoch_and_rejects_invalid_values(self):
        self.assertEqual(
            _normalize_efinance_provider_timestamp(1784788200),
            "2026-07-23T06:30:00+00:00",
        )
        self.assertEqual(
            _normalize_efinance_provider_timestamp("1784788200000"),
            "2026-07-23T06:30:00+00:00",
        )
        self.assertIsNone(_normalize_efinance_provider_timestamp("not-a-time"))
        self.assertIsNone(_normalize_efinance_provider_timestamp(None))
        self.assertEqual(_normalize_efinance_data_date("20260723"), "2026-07-23")
        self.assertEqual(_normalize_efinance_data_date("2026-07-23"), "2026-07-23")
        self.assertIsNone(_normalize_efinance_data_date("not-a-date"))
        self.assertEqual(
            _extract_efinance_provider_timestamp(
                {"更新时间戳": 1784788200, "更新时间": "not-a-time"}
            ),
            "2026-07-23T06:30:00+00:00",
        )
        self.assertEqual(
            _extract_efinance_data_date({"最新交易日": "20260723"}),
            "2026-07-23",
        )

    def test_stock_and_etf_quotes_preserve_provider_timestamp_from_cache(self):
        timestamp = "2026-07-23T06:30:00+00:00"
        stock_df = pd.DataFrame(
            {
                "股票代码": ["600519"],
                "股票名称": ["贵州茅台"],
                "最新价": [1500.0],
                "更新时间": [timestamp],
            }
        )
        etf_df = pd.DataFrame(
            {
                "股票代码": ["510300"],
                "股票名称": ["沪深300ETF"],
                "最新价": [4.1],
                "更新时间": [timestamp],
            }
        )
        fake_efinance = types.SimpleNamespace(stock=types.SimpleNamespace())
        cache_timestamp = time.time()
        fetcher = EfinanceFetcher()

        with patch.dict(sys.modules, {"efinance": fake_efinance}), patch(
            "data_provider.efinance_fetcher.get_realtime_circuit_breaker",
            return_value=_available_circuit_breaker(),
        ), patch(
            "data_provider.efinance_fetcher._realtime_cache",
            {"data": stock_df, "timestamp": cache_timestamp, "ttl": 1200},
        ), patch(
            "data_provider.efinance_fetcher._etf_realtime_cache",
            {"data": etf_df, "timestamp": cache_timestamp, "ttl": 1200},
        ):
            stock_quote = fetcher.get_realtime_quote("600519")
            etf_quote = fetcher.get_realtime_quote("510300")

        self.assertIsNotNone(stock_quote)
        self.assertIsNotNone(etf_quote)
        self.assertEqual(stock_quote.provider_timestamp, timestamp)
        self.assertEqual(etf_quote.provider_timestamp, timestamp)

    def test_market_stats_only_reports_as_of_with_complete_valid_row_coverage(self):
        fetcher = EfinanceFetcher()
        complete_df = pd.DataFrame(
            {
                "股票代码": ["600519", "000001"],
                "股票名称": ["贵州茅台", "平安银行"],
                "最新价": [1500.0, 10.1],
                "昨收": [1490.0, 10.0],
                "成交额": [100000000.0, 200000000.0],
                "更新时间": [
                    "2026-07-23T06:30:02+00:00",
                    "2026-07-23T06:30:00+00:00",
                ],
                "最新交易日": ["20260723", "20260723"],
            }
        )

        complete = fetcher._calc_market_stats(complete_df)

        self.assertEqual(complete["provider_timestamp"], "2026-07-23T06:30:00+00:00")
        self.assertEqual(complete["provider_timestamp_coverage_pct"], 100.0)
        self.assertEqual(complete["data_date"], "2026-07-23")
        self.assertEqual(complete["data_granularity"], "realtime")

        partial_df = complete_df.copy()
        partial_df.loc[1, "更新时间"] = None
        partial = fetcher._calc_market_stats(partial_df)

        self.assertIsNone(partial["provider_timestamp"])
        self.assertEqual(partial["provider_timestamp_coverage_pct"], 50.0)


if __name__ == "__main__":
    unittest.main()
