# -*- coding: utf-8 -*-
"""Regression tests for TickFlow market-review manager fallback."""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from data_provider.base import DataFetcherManager


class _DummyFetcher:
    def __init__(self, name, indices=None, stats=None):
        self.name = name
        self.priority = 1
        self.indices = indices
        self.stats = stats
        self.index_calls = 0
        self.stats_calls = 0

    def get_main_indices(self, region="cn"):
        self.index_calls += 1
        return self.indices

    def get_market_stats(self):
        self.stats_calls += 1
        return self.stats


class _DummyTickFlowFetcher:
    def __init__(self, indices=None, stats=None, error=None):
        self.indices = indices
        self.stats = stats
        self.error = error
        self.closed = False

    def get_main_indices(self, region="cn"):
        if self.error is not None:
            raise self.error
        return self.indices

    def get_market_stats(self):
        if self.error is not None:
            raise self.error
        return self.stats

    def close(self):
        self.closed = True


class TestTickFlowMarketReviewFallback(unittest.TestCase):
    def test_manager_prefers_tickflow_indices_when_available(self):
        manager = DataFetcherManager.__new__(DataFetcherManager)
        fallback = _DummyFetcher("AkshareFetcher", indices=[{"code": "fallback"}])
        manager._fetchers = [fallback]
        manager._get_tickflow_fetcher = lambda: _DummyTickFlowFetcher(
            indices=[{"code": "000001"}]
        )

        data = DataFetcherManager.get_main_indices(manager, region="cn")

        self.assertEqual(data[0]["code"], "000001")
        self.assertEqual(data[0]["provider"], "tickflow")
        self.assertIsNotNone(data[0]["fetched_at"])
        self.assertEqual(fallback.index_calls, 0)

    def test_manager_falls_back_when_tickflow_indices_fail(self):
        manager = DataFetcherManager.__new__(DataFetcherManager)
        fallback = _DummyFetcher("AkshareFetcher", indices=[{"code": "fallback"}])
        manager._fetchers = [fallback]
        manager._get_tickflow_fetcher = lambda: _DummyTickFlowFetcher(
            error=RuntimeError("tickflow down")
        )

        data = DataFetcherManager.get_main_indices(manager, region="cn")

        self.assertEqual(data[0]["code"], "fallback")
        self.assertEqual(data[0]["provider"], "akshare")
        self.assertIsNotNone(data[0]["fetched_at"])
        self.assertEqual(fallback.index_calls, 1)

    def test_manager_prefers_direct_eastmoney_indices_before_akshare(self):
        manager = DataFetcherManager.__new__(DataFetcherManager)
        efinance = _DummyFetcher("EfinanceFetcher", indices=None)
        direct = _DummyFetcher(
            "AStockDataFetcher",
            indices=[
                {
                    "code": "sh000001",
                    "provider_timestamp": "2026-07-23T06:30:00+00:00",
                    "data_granularity": "realtime",
                }
            ],
        )
        akshare = _DummyFetcher("AkshareFetcher", indices=[{"code": "akshare"}])
        manager._fetchers = [efinance, direct, akshare]
        manager._get_tickflow_fetcher = lambda: None

        data = DataFetcherManager.get_main_indices(manager, region="cn")

        self.assertEqual(data[0]["code"], "sh000001")
        self.assertEqual(data[0]["provider"], "astockdata")
        self.assertEqual(data[0]["provider_timestamp"], "2026-07-23T06:30:00+00:00")
        self.assertIsNotNone(data[0]["fetched_at"])
        self.assertEqual(efinance.index_calls, 1)
        self.assertEqual(direct.index_calls, 1)
        self.assertEqual(akshare.index_calls, 0)

    def test_manager_falls_back_when_tickflow_indices_missing(self):
        manager = DataFetcherManager.__new__(DataFetcherManager)
        fallback = _DummyFetcher("AkshareFetcher", indices=[{"code": "fallback"}])
        manager._fetchers = [fallback]
        manager._get_tickflow_fetcher = lambda: _DummyTickFlowFetcher(
            indices=None
        )

        data = DataFetcherManager.get_main_indices(manager, region="cn")

        self.assertEqual(data[0]["code"], "fallback")
        self.assertEqual(data[0]["provider"], "akshare")
        self.assertEqual(fallback.index_calls, 1)

    def test_manager_skips_tickflow_for_non_cn_indices(self):
        manager = DataFetcherManager.__new__(DataFetcherManager)
        fallback = _DummyFetcher("YfinanceFetcher", indices=[{"code": "^GSPC"}])
        manager._fetchers = [fallback]
        manager._get_tickflow_fetcher = lambda: self.fail(
            "TickFlow should not be called for non-CN indices"
        )

        data = DataFetcherManager.get_main_indices(manager, region="us")

        self.assertEqual(data[0]["code"], "^GSPC")
        self.assertEqual(data[0]["provider"], "yfinance")
        self.assertEqual(fallback.index_calls, 1)

    def test_manager_falls_back_when_tickflow_market_stats_fails(self):
        manager = DataFetcherManager.__new__(DataFetcherManager)
        fallback = _DummyFetcher(
            "AkshareFetcher",
            stats={"up_count": 1, "down_count": 2, "flat_count": 3},
        )
        manager._fetchers = [fallback]
        manager._get_tickflow_fetcher = lambda: _DummyTickFlowFetcher(
            error=RuntimeError("tickflow down")
        )

        data = DataFetcherManager.get_market_stats(manager, purpose="market_review:cn")

        self.assertEqual(data["up_count"], 1)
        self.assertEqual(data["provider"], "akshare")
        self.assertIsNotNone(data["fetched_at"])
        self.assertEqual(fallback.stats_calls, 1)

    def test_manager_prefers_direct_eastmoney_breadth_before_akshare(self):
        manager = DataFetcherManager.__new__(DataFetcherManager)
        efinance = _DummyFetcher("EfinanceFetcher", stats=None)
        direct = _DummyFetcher(
            "AStockDataFetcher",
            stats={
                "up_count": 2,
                "down_count": 1,
                "flat_count": 0,
                "provider_timestamp": "2026-07-23T06:30:00+00:00",
                "provider_timestamp_coverage_pct": 100.0,
                "data_granularity": "realtime",
            },
        )
        akshare = _DummyFetcher(
            "AkshareFetcher",
            stats={"up_count": 99, "down_count": 0, "flat_count": 0},
        )
        manager._fetchers = [efinance, akshare, direct]
        manager._get_tickflow_fetcher = lambda: None

        data = DataFetcherManager.get_market_stats(manager, purpose="vnpy_paper_intraday_risk")

        self.assertEqual(data["provider"], "astockdata")
        self.assertEqual(data["provider_timestamp_coverage_pct"], 100.0)
        self.assertIsNotNone(data["fetched_at"])
        self.assertEqual(efinance.stats_calls, 0)
        self.assertEqual(direct.stats_calls, 1)
        self.assertEqual(akshare.stats_calls, 0)

    @patch("src.config.get_config")
    def test_manager_skips_tickflow_without_api_key(self, mock_get_config):
        mock_get_config.return_value = SimpleNamespace(tickflow_api_key=None)

        manager = DataFetcherManager.__new__(DataFetcherManager)
        fallback = _DummyFetcher(
            "AkshareFetcher",
            stats={"up_count": 2, "down_count": 1, "flat_count": 0},
        )
        manager._fetchers = [fallback]

        data = DataFetcherManager.get_market_stats(manager)

        self.assertEqual(data["up_count"], 2)
        self.assertEqual(fallback.stats_calls, 1)

    def test_manager_close_releases_tickflow_fetcher(self):
        manager = DataFetcherManager.__new__(DataFetcherManager)
        tickflow_fetcher = _DummyTickFlowFetcher(indices=[{"code": "000001"}])
        manager._tickflow_fetcher = tickflow_fetcher
        manager._tickflow_api_key = "tf-secret"
        manager._tickflow_lock = None

        DataFetcherManager.close(manager)

        self.assertTrue(tickflow_fetcher.closed)
        self.assertIsNone(manager._tickflow_fetcher)
        self.assertIsNone(manager._tickflow_api_key)


if __name__ == "__main__":
    unittest.main()
