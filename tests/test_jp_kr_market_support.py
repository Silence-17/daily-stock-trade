# -*- coding: utf-8 -*-
"""Regression tests for Issue #1718 JP/KR suffix-only market support."""

from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
from data_provider.base import BaseFetcher, DataFetchError, DataFetcherManager, normalize_stock_code
from data_provider.yfinance_fetcher import YfinanceFetcher
from data_provider.realtime_types import UnifiedRealtimeQuote
from src.core.trading_calendar import MARKET_EXCHANGE, MARKET_TIMEZONE, get_market_for_stock
from src.market_context import detect_market, get_market_guidelines
from src.services.stock_code_utils import is_code_like, normalize_code


class _FakeFetcher(BaseFetcher):
    def __init__(self, name: str, should_fail: bool = False):
        self.name = name
        self.priority = 0 if name != "YfinanceFetcher" else 4
        self.calls = []
        self.should_fail = should_fail

    def _fetch_raw_data(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        raise NotImplementedError

    def _normalize_data(self, df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
        raise NotImplementedError

    def get_daily_data(self, stock_code, start_date=None, end_date=None, days=30):
        self.calls.append(stock_code)
        if self.should_fail:
            raise DataFetchError(f"{self.name} should not be called for {stock_code}")
        return pd.DataFrame(
            {
                "date": [pd.Timestamp("2026-06-18")],
                "open": [1.0],
                "high": [1.0],
                "low": [1.0],
                "close": [1.0],
                "volume": [100],
                "amount": [100.0],
                "pct_chg": [0.0],
            }
        )


def test_normalize_and_detect_jp_kr_suffix_codes() -> None:
    assert normalize_stock_code("7203.t") == "7203.T"
    assert normalize_stock_code("005930.ks") == "005930.KS"
    assert normalize_stock_code("035720.kq") == "035720.KQ"

    assert detect_market("7203.T") == "jp"
    assert detect_market("6758.T") == "jp"
    assert detect_market("005930.KS") == "kr"
    assert detect_market("035720.KQ") == "kr"
    assert detect_market("005930") == "cn"

    assert get_market_for_stock("7203.T") == "jp"
    assert get_market_for_stock("005930.KS") == "kr"
    assert get_market_for_stock("005930") == "cn"

    assert is_code_like("7203.T") is True
    assert is_code_like("005930.KS") is True
    assert normalize_code("035720.KQ") == "035720.KQ"


def test_market_guidelines_for_jp_kr_exclude_a_share_specific_context() -> None:
    jp_guidelines = get_market_guidelines("7203.T")
    kr_guidelines = get_market_guidelines("005930.KS")

    assert "日股" in jp_guidelines
    assert "韩股" in kr_guidelines
    for text in (jp_guidelines, kr_guidelines):
        assert "不要套用 A 股" in text
        assert "北向资金" in text
        assert "龙虎榜" in text


def test_yfinance_keeps_jp_kr_suffix_codes_and_indices() -> None:
    fetcher = YfinanceFetcher()

    assert fetcher._convert_stock_code("7203.T") == "7203.T"
    assert fetcher._convert_stock_code("005930.KS") == "005930.KS"
    assert fetcher._convert_stock_code("035720.KQ") == "035720.KQ"
    assert fetcher._convert_stock_code("GC1") == "GC=F"

    captured = []

    def fake_fetch(_yf, yf_code, name, return_code):
        captured.append((yf_code, name, return_code))
        return {"code": return_code, "name": name, "current": 1.0}

    fetcher._fetch_yf_ticker_data = fake_fetch  # type: ignore[method-assign]

    jp_indices = fetcher.get_main_indices("jp") or []
    kr_indices = fetcher.get_main_indices("kr") or []

    assert {item["code"] for item in jp_indices} == {"N225", "TOPX"}
    assert {item["code"] for item in kr_indices} == {"KS11", "KQ11"}
    assert ("^N225", "日经225", "N225") in captured
    assert ("^TOPX", "东证指数", "TOPX") in captured
    assert ("^KS11", "KOSPI", "KS11") in captured
    assert ("^KQ11", "KOSDAQ", "KQ11") in captured


def test_data_fetcher_manager_routes_jp_kr_daily_only_to_yfinance() -> None:
    efinance = _FakeFetcher("EfinanceFetcher", should_fail=True)
    akshare = _FakeFetcher("AkshareFetcher", should_fail=True)
    yfinance = _FakeFetcher("YfinanceFetcher")
    manager = DataFetcherManager(fetchers=[efinance, akshare, yfinance])

    with patch("data_provider.base.record_provider_run_started"), patch("data_provider.base.record_provider_run"):
        jp_df, jp_source = manager.get_daily_data("7203.T")
        kr_df, kr_source = manager.get_daily_data("005930.KS")

    assert jp_source == "YfinanceFetcher"
    assert kr_source == "YfinanceFetcher"
    assert not jp_df.empty and not kr_df.empty
    assert efinance.calls == []
    assert akshare.calls == []
    assert yfinance.calls == ["7203.T", "005930.KS"]


def test_realtime_quote_serializes_jp_kr_data_quality_metadata() -> None:
    quote = UnifiedRealtimeQuote(
        code="005930.KS",
        market="kr",
        currency="KRW",
        price=70000.0,
        data_quality="partial",
        missing_fields=["amount", "pe_ratio"],
    )

    payload = quote.to_dict()

    assert payload["market"] == "kr"
    assert payload["currency"] == "KRW"
    assert payload["data_quality"] == "partial"
    assert payload["missing_fields"] == ["amount", "pe_ratio"]


def test_data_fetcher_manager_routes_korea_indices_to_yfinance_realtime() -> None:
    provider_timestamp = "2026-07-23T01:35:00+00:00"

    class _RealtimeFetcher(_FakeFetcher):
        def get_realtime_quote(self, stock_code):
            self.calls.append(stock_code)
            return UnifiedRealtimeQuote(
                code=stock_code,
                market="kr",
                currency="KRW",
                price=2800.0,
                change_pct=1.0,
                provider_timestamp=provider_timestamp,
            )

    yfinance = _RealtimeFetcher("YfinanceFetcher")
    manager = DataFetcherManager(fetchers=[yfinance])
    config = SimpleNamespace(enable_realtime_quote=True, realtime_cache_ttl=600)

    with patch("src.config.get_config", return_value=config):
        quote = manager.get_realtime_quote("KS11")

    assert quote is not None
    assert quote.market == "kr"
    assert quote.provider_timestamp == provider_timestamp
    assert yfinance.calls == ["KS11"]


def test_data_fetcher_manager_prefers_fresh_japan_index_route() -> None:
    provider_timestamp = "2026-08-03T04:52:00+00:00"

    class _RealtimeFetcher(_FakeFetcher):
        def get_realtime_quote(self, stock_code):
            self.calls.append(stock_code)
            return UnifiedRealtimeQuote(
                code=stock_code,
                market="jp",
                currency="JPY",
                price=3960.83,
                change_pct=-1.06,
                provider_timestamp=provider_timestamp,
            )

    japan_index = _RealtimeFetcher("JapanIndexFetcher")
    yfinance = _RealtimeFetcher("YfinanceFetcher")
    manager = DataFetcherManager(fetchers=[japan_index, yfinance])
    config = SimpleNamespace(enable_realtime_quote=True, realtime_cache_ttl=120)
    DataFetcherManager.reset_realtime_source_health()

    with patch("src.config.get_config", return_value=config):
        quote = manager.get_realtime_quote(
            "TOPX",
            require_provider_timestamp=True,
        )

    assert quote is not None
    assert quote.provider_timestamp == provider_timestamp
    assert japan_index.calls == ["TOPX"]
    assert yfinance.calls == []


def test_data_fetcher_manager_prefers_asia_equity_for_japan_stock() -> None:
    provider_timestamp = "2026-08-03T05:15:00+00:00"

    class _RealtimeFetcher(_FakeFetcher):
        def get_realtime_quote(self, stock_code):
            self.calls.append(stock_code)
            return UnifiedRealtimeQuote(
                code=stock_code,
                market="jp",
                currency="JPY",
                price=7215.0,
                change_pct=-2.71,
                provider_timestamp=provider_timestamp,
            )

    asia_equity = _RealtimeFetcher("AsiaEquityFetcher")
    yfinance = _RealtimeFetcher("YfinanceFetcher")
    manager = DataFetcherManager(fetchers=[asia_equity, yfinance])
    config = SimpleNamespace(enable_realtime_quote=True, realtime_cache_ttl=120)
    DataFetcherManager.reset_realtime_source_health()

    with patch("src.config.get_config", return_value=config):
        quote = manager.get_realtime_quote(
            "6981.T",
            require_provider_timestamp=True,
        )

    assert quote is not None
    assert quote.provider_timestamp == provider_timestamp
    assert asia_equity.calls == ["6981.T"]
    assert yfinance.calls == []


def test_data_fetcher_manager_prefers_kis_for_korean_realtime_quotes() -> None:
    provider_timestamp = "2026-07-23T01:35:00+00:00"

    class _RealtimeFetcher(_FakeFetcher):
        def get_realtime_quote(self, stock_code):
            self.calls.append(stock_code)
            return UnifiedRealtimeQuote(
                code=stock_code,
                market="kr",
                currency="KRW",
                price=2800.0,
                change_pct=1.0,
                provider_timestamp=provider_timestamp,
            )

    kis = _RealtimeFetcher("KoreaInvestmentFetcher")
    yfinance = _RealtimeFetcher("YfinanceFetcher")
    manager = DataFetcherManager(fetchers=[kis, yfinance])
    config = SimpleNamespace(enable_realtime_quote=True, realtime_cache_ttl=600)
    DataFetcherManager.reset_realtime_source_health()

    with patch("src.config.get_config", return_value=config):
        quote = manager.get_realtime_quote("KS11")

    assert quote is not None
    assert kis.calls == ["KS11"]
    assert yfinance.calls == []


def test_data_fetcher_manager_falls_back_to_yfinance_when_kis_is_empty() -> None:
    class _RealtimeFetcher(_FakeFetcher):
        def get_realtime_quote(self, stock_code):
            self.calls.append(stock_code)
            if self.name == "KoreaInvestmentFetcher":
                return None
            return UnifiedRealtimeQuote(
                code=stock_code,
                market="kr",
                currency="KRW",
                price=2800.0,
                change_pct=1.0,
                provider_timestamp="2026-07-23T01:35:00+00:00",
            )

    kis = _RealtimeFetcher("KoreaInvestmentFetcher")
    yfinance = _RealtimeFetcher("YfinanceFetcher")
    manager = DataFetcherManager(fetchers=[kis, yfinance])
    config = SimpleNamespace(enable_realtime_quote=True, realtime_cache_ttl=600)
    DataFetcherManager.reset_realtime_source_health()

    with patch("src.config.get_config", return_value=config):
        quote = manager.get_realtime_quote("KS11")

    assert quote is not None
    assert kis.calls == ["KS11"]
    assert yfinance.calls == ["KS11"]


def test_data_fetcher_manager_prefers_naver_over_yfinance_without_kis() -> None:
    provider_timestamp = "2026-07-23T01:35:00+00:00"

    class _RealtimeFetcher(_FakeFetcher):
        def get_realtime_quote(self, stock_code):
            self.calls.append(stock_code)
            return UnifiedRealtimeQuote(
                code=stock_code,
                market="kr",
                currency="KRW",
                price=2800.0,
                change_pct=1.0,
                provider_timestamp=provider_timestamp,
            )

    naver = _RealtimeFetcher("NaverKoreaFetcher")
    yfinance = _RealtimeFetcher("YfinanceFetcher")
    manager = DataFetcherManager(fetchers=[naver, yfinance])
    config = SimpleNamespace(enable_realtime_quote=True, realtime_cache_ttl=600)
    DataFetcherManager.reset_realtime_source_health()

    with patch("src.config.get_config", return_value=config):
        quote = manager.get_realtime_quote("KS11")

    assert quote is not None
    assert naver.calls == ["KS11"]
    assert yfinance.calls == []


def test_data_fetcher_manager_falls_back_to_yfinance_when_naver_is_empty() -> None:
    class _RealtimeFetcher(_FakeFetcher):
        def get_realtime_quote(self, stock_code):
            self.calls.append(stock_code)
            if self.name == "NaverKoreaFetcher":
                return None
            return UnifiedRealtimeQuote(
                code=stock_code,
                market="kr",
                currency="KRW",
                price=2800.0,
                change_pct=1.0,
                provider_timestamp="2026-07-23T01:35:00+00:00",
            )

    naver = _RealtimeFetcher("NaverKoreaFetcher")
    yfinance = _RealtimeFetcher("YfinanceFetcher")
    manager = DataFetcherManager(fetchers=[naver, yfinance])
    config = SimpleNamespace(enable_realtime_quote=True, realtime_cache_ttl=600)
    DataFetcherManager.reset_realtime_source_health()

    with patch("src.config.get_config", return_value=config):
        quote = manager.get_realtime_quote("KS11")

    assert quote is not None
    assert naver.calls == ["KS11"]
    assert yfinance.calls == ["KS11"]


def test_trading_calendar_registers_jp_kr_exchanges_and_timezones() -> None:
    assert MARKET_EXCHANGE["jp"] == "XTKS"
    assert MARKET_EXCHANGE["kr"] == "XKRX"
    assert MARKET_TIMEZONE["jp"] == "Asia/Tokyo"
    assert MARKET_TIMEZONE["kr"] == "Asia/Seoul"
