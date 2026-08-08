# -*- coding: utf-8 -*-
"""Tests for crumb-free Yahoo Chart realtime fallback."""

from __future__ import annotations

import json
import threading
from urllib.error import URLError
from unittest.mock import MagicMock, patch

from data_provider.base import DataFetcherManager
from data_provider.realtime_types import RealtimeSource
from data_provider.yfinance_fetcher import (
    CROSS_MARKET_US_HTTP_TIMEOUT_SECONDS,
    YfinanceFetcher,
)


def _chart_response() -> MagicMock:
    payload = {
        "chart": {
            "result": [{
                "meta": {
                    "currency": "KRW",
                    "chartPreviousClose": 100.0,
                    "shortName": "KOSPI",
                },
                "timestamp": [1784851200, 1784851260, 1784851320],
                "indicators": {
                    "quote": [{
                        "open": [100.0, 101.0, None],
                        "high": [101.0, 102.0, None],
                        "low": [99.0, 100.5, None],
                        "close": [100.5, 101.5, None],
                        "volume": [1000, 1500, None],
                    }]
                },
            }],
            "error": None,
        }
    }
    response = MagicMock()
    response.read.return_value = json.dumps(payload).encode("utf-8")
    response.__enter__.return_value = response
    return response


@patch("data_provider.yfinance_fetcher.urlopen")
def test_yahoo_chart_quote_preserves_latest_real_minute_timestamp(mock_urlopen) -> None:
    mock_urlopen.return_value = _chart_response()

    quote = YfinanceFetcher()._get_yahoo_chart_realtime_quote(
        user_code="KS11",
        yf_symbol="^KS11",
        name="KOSPI",
        market="kr",
        currency="KRW",
    )

    assert quote is not None
    assert quote.source is RealtimeSource.YAHOO_CHART
    assert quote.provider_timestamp == "2026-07-24T00:01:00+00:00"
    assert quote.price == 101.5
    assert quote.pre_close == 100.0
    assert quote.change_pct == 1.5
    assert quote.open_price == 100.0
    assert quote.high == 102.0
    assert quote.low == 99.0
    assert quote.volume == 2500
    assert "%5EKS11" in mock_urlopen.call_args.args[0].full_url
    assert mock_urlopen.call_args.kwargs["timeout"] == 15.0


@patch("data_provider.yfinance_fetcher.urlopen")
@patch("yfinance.Ticker")
def test_korea_index_prefers_chart_without_yfinance_crumb_state(
    mock_ticker,
    mock_urlopen,
) -> None:
    mock_urlopen.return_value = _chart_response()

    quote = YfinanceFetcher().get_realtime_quote("KS11")

    assert quote is not None
    assert quote.code == "KS11"
    assert quote.market == "kr"
    assert quote.source is RealtimeSource.YAHOO_CHART
    mock_ticker.assert_not_called()


@patch("data_provider.yfinance_fetcher.urlopen")
def test_yahoo_chart_switches_to_second_host_after_transport_failure(mock_urlopen) -> None:
    mock_urlopen.side_effect = [URLError("connection closed"), _chart_response()]

    quote = YfinanceFetcher()._get_yahoo_chart_realtime_quote(
        user_code="GC1",
        yf_symbol="GC=F",
        name="Gold Futures",
        market="us",
        currency="USD",
    )

    assert quote is not None
    assert quote.source is RealtimeSource.YAHOO_CHART
    assert mock_urlopen.call_count == 2
    assert "query1.finance.yahoo.com" in mock_urlopen.call_args_list[0].args[0].full_url
    assert "query2.finance.yahoo.com" in mock_urlopen.call_args_list[1].args[0].full_url


def _timestamped_us_quote(source: RealtimeSource) -> MagicMock:
    quote = MagicMock()
    quote.source = source
    quote.provider_timestamp = "2026-07-24T13:35:00+00:00"
    quote.has_basic_data.return_value = True
    return quote


def test_cross_market_us_quote_prefers_timestamped_tencent() -> None:
    fetcher = YfinanceFetcher()
    tencent_quote = _timestamped_us_quote(RealtimeSource.TENCENT)

    with patch.object(
        fetcher,
        "_get_us_stock_quote_from_tencent",
        return_value=tencent_quote,
    ) as tencent, patch.object(
        fetcher,
        "_get_yahoo_chart_realtime_quote",
    ) as yahoo:
        quote = fetcher.get_cross_market_us_realtime_quote("SMH")

    assert quote is tencent_quote
    tencent.assert_called_once_with(
        "SMH",
        timeout_seconds=CROSS_MARKET_US_HTTP_TIMEOUT_SECONDS,
    )
    yahoo.assert_not_called()


def test_cross_market_us_quote_falls_back_to_timestamped_yahoo() -> None:
    fetcher = YfinanceFetcher()
    yahoo_quote = _timestamped_us_quote(RealtimeSource.YAHOO_CHART)

    with patch.object(
        fetcher,
        "_get_us_stock_quote_from_tencent",
        return_value=None,
    ), patch.object(
        fetcher,
        "_get_yahoo_chart_realtime_quote",
        return_value=yahoo_quote,
    ) as yahoo:
        quote = fetcher.get_cross_market_us_realtime_quote("IXIC")

    assert quote is yahoo_quote
    assert yahoo.call_args.kwargs["user_code"] == "IXIC"
    assert yahoo.call_args.kwargs["yf_symbol"] == "^IXIC"
    assert (
        yahoo.call_args.kwargs["request_timeout_seconds"]
        == CROSS_MARKET_US_HTTP_TIMEOUT_SECONDS
    )


@patch("data_provider.yfinance_fetcher.urlopen")
def test_cross_market_us_premarket_enables_extended_hours_minutes(mock_urlopen) -> None:
    mock_urlopen.return_value = _chart_response()

    quote = YfinanceFetcher().get_cross_market_us_premarket_quote("MSFT")

    assert quote is not None
    assert "includePrePost=true" in mock_urlopen.call_args.args[0].full_url
    assert mock_urlopen.call_args.kwargs["timeout"] == CROSS_MARKET_US_HTTP_TIMEOUT_SECONDS


def test_cross_market_us_premarket_batch_uses_streamer_trade_timestamps() -> None:
    class _FakeWebSocket:
        def __init__(self, *, verbose):
            assert verbose is False
            self.symbols = []
            self.closed = False
            self.close_event = threading.Event()
            self.logger = MagicMock()
            self.logger.disabled = False

        def subscribe(self, symbols):
            self.symbols = list(symbols)

        def listen(self, handler):
            for index, symbol in enumerate(self.symbols):
                handler({
                    "id": symbol,
                    "price": 100.0 + index,
                    "time": str(1785763367000 + index * 1000),
                    "change_percent": 1.5,
                    "change": 1.25,
                })
            self.close_event.wait(2)

        def close(self):
            self.closed = True
            self.close_event.set()

    websocket = _FakeWebSocket(verbose=False)
    fetcher = YfinanceFetcher()
    with patch("yfinance.WebSocket", return_value=websocket):
        quotes = fetcher.get_cross_market_us_premarket_quotes(
            ["MSFT", "NVDA"]
        )

    assert set(quotes) == {"MSFT", "NVDA"}
    assert quotes["MSFT"].provider_timestamp == "2026-08-03T13:22:47+00:00"
    assert quotes["MSFT"].source is RealtimeSource.YAHOO_STREAMER
    assert quotes["MSFT"].change_pct == 1.5
    assert quotes["MSFT"].pre_close == 98.75
    assert websocket.closed is False
    fetcher.close()
    assert websocket.closed is True
    assert websocket.logger.disabled is False


def test_cross_market_us_premarket_stream_captures_between_scheduler_polls() -> None:
    class _PersistentFakeWebSocket:
        def __init__(self, *, verbose):
            assert verbose is False
            self.symbols = []
            self.handler = None
            self.close_event = threading.Event()
            self.logger = MagicMock()
            self.logger.disabled = False

        def subscribe(self, symbols):
            self.symbols = list(symbols)

        def listen(self, handler):
            self.handler = handler
            self.emit("MSFT", 1785763367000)
            self.close_event.wait(3)

        def emit(self, symbol, timestamp):
            if self.handler is None:
                return
            self.handler({
                "id": symbol,
                "price": 100.0,
                "time": str(timestamp),
                "change_percent": 1.0,
                "change": 1.0,
            })

        def close(self):
            self.close_event.set()

    websocket = _PersistentFakeWebSocket(verbose=False)
    fetcher = YfinanceFetcher()
    with patch("yfinance.WebSocket", return_value=websocket) as websocket_factory:
        first = fetcher.get_cross_market_us_premarket_quotes(
            ["MSFT", "NVDA"],
            timeout_seconds=1,
        )
        websocket.emit("NVDA", 1785763410000)
        second = fetcher.get_cross_market_us_premarket_quotes(
            ["MSFT", "NVDA"],
            timeout_seconds=1,
        )

    assert set(first) == {"MSFT"}
    assert set(second) == {"MSFT", "NVDA"}
    assert second["NVDA"].provider_timestamp == "2026-08-03T13:23:30+00:00"
    websocket_factory.assert_called_once_with(verbose=False)
    fetcher.close()


def test_stale_premarket_idle_timer_cannot_close_refreshed_stream() -> None:
    class _IdleFakeWebSocket:
        def __init__(self, *, verbose):
            assert verbose is False
            self.symbols = []
            self.closed = False
            self.close_event = threading.Event()
            self.logger = MagicMock()
            self.logger.disabled = False

        def subscribe(self, symbols):
            self.symbols = list(symbols)

        def listen(self, handler):
            handler({
                "id": "MSFT",
                "price": 100.0,
                "time": "1785763367000",
                "change_percent": 1.0,
                "change": 1.0,
            })
            self.close_event.wait(2)

        def close(self):
            self.closed = True
            self.close_event.set()

    websocket = _IdleFakeWebSocket(verbose=False)
    fetcher = YfinanceFetcher()
    with patch("yfinance.WebSocket", return_value=websocket):
        fetcher.get_cross_market_us_premarket_quotes(
            ["MSFT"],
            timeout_seconds=1,
        )
        stale_generation = fetcher._premarket_stream_idle_generation
        fetcher._refresh_cross_market_us_premarket_stream_idle_timer()
        fetcher._expire_cross_market_us_premarket_stream(stale_generation)

    assert websocket.closed is False
    assert fetcher._premarket_stream_thread is not None
    fetcher.close()
    assert websocket.closed is True


@patch("src.config.get_config")
def test_manager_exposes_dedicated_us_premarket_route(mock_get_config) -> None:
    mock_get_config.return_value.enable_realtime_quote = True
    quote = _timestamped_us_quote(RealtimeSource.YAHOO_CHART)
    fetcher = MagicMock()
    fetcher.name = "YfinanceFetcher"
    fetcher.priority = 4
    fetcher.is_available_for_request.return_value = True
    fetcher.get_cross_market_us_premarket_quote.return_value = quote
    manager = DataFetcherManager(fetchers=[fetcher])

    result = manager.get_cross_market_us_premarket_quote_with_provider_timestamp(
        "MSFT"
    )

    assert result is quote
    fetcher.get_cross_market_us_premarket_quote.assert_called_once_with("MSFT")


@patch("src.config.get_config")
def test_manager_exposes_batch_us_premarket_stream_route(mock_get_config) -> None:
    mock_get_config.return_value.enable_realtime_quote = True
    quote = _timestamped_us_quote(RealtimeSource.YAHOO_CHART)
    fetcher = MagicMock()
    fetcher.name = "YfinanceFetcher"
    fetcher.priority = 4
    fetcher.is_available_for_request.return_value = True
    fetcher.get_cross_market_us_premarket_quotes.return_value = {"MSFT": quote}
    manager = DataFetcherManager(fetchers=[fetcher])

    result = manager.get_cross_market_us_premarket_quotes_with_provider_timestamps(
        ["MSFT", "NVDA"]
    )

    assert result == {"MSFT": quote}
    fetcher.get_cross_market_us_premarket_quotes.assert_called_once_with(
        ["MSFT", "NVDA"]
    )


@patch("src.config.get_config")
def test_manager_exposes_dedicated_cross_market_us_route(mock_get_config) -> None:
    mock_get_config.return_value.enable_realtime_quote = True
    mock_get_config.return_value.realtime_cache_ttl = 120
    quote = _timestamped_us_quote(RealtimeSource.TENCENT)
    fetcher = MagicMock()
    fetcher.name = "YfinanceFetcher"
    fetcher.priority = 4
    fetcher.is_available_for_request.return_value = True
    fetcher.get_cross_market_us_realtime_quote.return_value = quote
    manager = DataFetcherManager(fetchers=[fetcher])

    result = manager.get_cross_market_us_quote_with_provider_timestamp("IXIC")

    assert result is quote
    assert result.provider_timestamp == "2026-07-24T13:35:00+00:00"
    assert result.fetched_at is not None
    fetcher.get_cross_market_us_realtime_quote.assert_called_once_with("IXIC")


def test_manager_bypasses_lock_only_for_declared_cross_market_yahoo_methods() -> None:
    fetcher = YfinanceFetcher()
    manager = DataFetcherManager(fetchers=[fetcher])
    quote = _timestamped_us_quote(RealtimeSource.YAHOO_CHART)

    with patch.object(
        fetcher,
        "get_cross_market_us_premarket_quote",
        return_value=quote,
    ) as premarket, patch.object(
        manager,
        "_get_fetcher_call_lock",
        side_effect=AssertionError("declared concurrent-safe method acquired lock"),
    ):
        result = manager._call_fetcher_method(
            fetcher,
            "get_cross_market_us_premarket_quote",
            "MSFT",
        )

    assert result is quote
    premarket.assert_called_once_with("MSFT")


def test_manager_keeps_lock_for_ordinary_yahoo_methods() -> None:
    fetcher = YfinanceFetcher()
    manager = DataFetcherManager(fetchers=[fetcher])
    lock = MagicMock()
    lock.__enter__.return_value = lock
    lock.__exit__.return_value = False

    with patch.object(
        fetcher,
        "get_realtime_quote",
        return_value=None,
    ) as realtime, patch.object(
        manager,
        "_get_fetcher_call_lock",
        return_value=lock,
    ) as get_lock:
        result = manager._call_fetcher_method(
            fetcher,
            "get_realtime_quote",
            "MSFT",
        )

    assert result is None
    get_lock.assert_called_once_with(fetcher)
    lock.__enter__.assert_called_once_with()
    lock.__exit__.assert_called_once()
    realtime.assert_called_once_with("MSFT")
