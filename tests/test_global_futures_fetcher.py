# -*- coding: utf-8 -*-
"""Deterministic routing and parsing tests for global futures data."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
import requests

from data_provider.base import DataFetcherManager
from data_provider.global_futures_fetcher import GlobalFuturesFetcher
from data_provider.realtime_types import RealtimeSource, UnifiedRealtimeQuote


class _Response:
    def __init__(self, content: bytes, *, error: bool = False):
        self.content = content
        self.error = error

    def raise_for_status(self):
        if self.error:
            raise requests.HTTPError("HTTP 503")


class _Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def _quote_payload(prefix: str = "v_hf_GC") -> bytes:
    text = (
        f'{prefix}="4063.80,0.34,4062.90,4063.30,4073.00,4024.00,'
        '22:15:06,4050.20,4053.40,0,2,3,2026-07-24,纽约黄金";'
    )
    return text.encode("gbk")


def _daily_payload() -> bytes:
    return (
        'var _GC=([{"date":"2026-07-22","open":"4084.7","high":"4171.4",'
        '"low":"4081.0","close":"4134.7","volume":"0"},'
        '{"date":"2026-07-23","open":"4134.7","high":"4144.0",'
        '"low":"4042.5","close":"4052.1","volume":"0"},'
        '{"date":"2026-07-24","open":"4052.1","high":"4072.1",'
        '"low":"4024.0","close":"4070.0","volume":"0"}]);'
    ).encode("utf-8")


def _nq_quote_payload() -> bytes:
    return (
        'var hq_str_hf_NQ="28453.410,,28466.500,28467.750,28640.750,'
        '28300.000,20:58:25,28237.750,28317.000,0,1,2,2026-07-31,'
        'E-mini Nasdaq-100";'
    ).encode("gbk")


def test_realtime_quote_preserves_provider_second_and_price_fields() -> None:
    session = _Session([_Response(_quote_payload())])

    quote = GlobalFuturesFetcher(session=session).get_realtime_quote("GC1")

    assert quote is not None
    assert quote.source is RealtimeSource.TENCENT
    assert quote.provider_timestamp == "2026-07-24T14:15:06+00:00"
    assert quote.price == 4063.8
    assert quote.pre_close == 4050.2
    assert quote.open_price == 4053.4
    assert quote.high == 4073.0 and quote.low == 4024.0
    assert round(quote.change_pct, 2) == 0.34
    assert session.calls[0][0].endswith("q=hf_GC")


def test_realtime_quote_falls_back_to_sina() -> None:
    session = _Session([
        _Response(b"", error=True),
        _Response(_quote_payload("var hq_str_hf_GC")),
    ])

    quote = GlobalFuturesFetcher(session=session).get_realtime_quote("GC=F")

    assert quote is not None
    assert quote.source is RealtimeSource.SINA
    assert len(session.calls) == 2
    assert session.calls[1][0].endswith("list=hf_GC")


def test_nq00y_uses_sina_nq_route_and_preserves_canonical_code() -> None:
    session = _Session([
        _Response(b'v_pv_none_match="1";'),
        _Response(_nq_quote_payload()),
    ])

    quote = GlobalFuturesFetcher(session=session).get_realtime_quote("NQ00Y")

    assert quote is not None
    assert quote.code == "NQ00Y"
    assert quote.name == "E-mini Nasdaq-100"
    assert quote.source is RealtimeSource.SINA
    assert session.calls[0][0].endswith("q=hf_NQ")
    assert session.calls[1][0].endswith("list=hf_NQ")
    assert GlobalFuturesFetcher._normalize_code("NQ1") == "NQ00Y"
    assert GlobalFuturesFetcher._normalize_code("NQ=F") == "NQ00Y"


def test_realtime_quote_rejects_missing_provider_time_and_unknown_code() -> None:
    invalid = _quote_payload().replace(b"22:15:06", b"invalid!")
    fetcher = GlobalFuturesFetcher(
        session=_Session([_Response(invalid), _Response(invalid)])
    )

    assert fetcher.get_realtime_quote("GC1") is None
    assert fetcher.get_realtime_quote("SI1") is None


def test_daily_history_filters_dates_and_normalizes_columns() -> None:
    session = _Session([_Response(_daily_payload())])
    fetcher = GlobalFuturesFetcher(session=session)

    frame = fetcher.get_daily_data(
        "GC1",
        start_date="2026-07-23",
        end_date="2026-07-24",
    )

    assert len(frame) == 2
    assert list(frame["date"].dt.strftime("%Y-%m-%d")) == ["2026-07-23", "2026-07-24"]
    assert frame.iloc[-1]["close"] == 4070.0
    assert set(("date", "open", "high", "low", "close", "volume", "amount", "pct_chg")) <= set(frame.columns)


class _RouteFetcher:
    def __init__(self, name: str, *, quote=None, frame=None, priority: int = 2):
        self.name = name
        self.priority = priority
        self.quote = quote
        self.frame = frame
        self.calls = []

    def is_available_for_request(self, capability=""):
        return True

    def get_realtime_quote(self, code):
        self.calls.append(("quote", code))
        return self.quote

    def get_daily_data(self, **kwargs):
        self.calls.append(("daily", kwargs["stock_code"]))
        return self.frame


def test_manager_prefers_global_futures_for_gold_realtime_and_daily() -> None:
    quote = UnifiedRealtimeQuote(
        code="GC1",
        source=RealtimeSource.TENCENT,
        provider_timestamp="2026-07-24T14:15:06+00:00",
        price=4063.8,
        change_pct=0.34,
    )
    frame = pd.DataFrame({
        "date": pd.to_datetime(["2026-07-23", "2026-07-24"]),
        "open": [1.0, 2.0],
        "high": [2.0, 3.0],
        "low": [0.5, 1.5],
        "close": [1.5, 2.5],
        "volume": [0, 0],
    })
    futures = _RouteFetcher("GlobalFuturesFetcher", quote=quote, frame=frame)
    yahoo = _RouteFetcher("YfinanceFetcher", priority=4)
    manager = DataFetcherManager(fetchers=[futures, yahoo])
    config = SimpleNamespace(enable_realtime_quote=True, realtime_cache_ttl=120)
    DataFetcherManager.reset_realtime_source_health()

    with patch("src.config.get_config", return_value=config):
        routed_quote = manager.get_realtime_quote_with_provider_timestamp("GC1")
        routed_frame, source = manager.get_daily_data(
            "GC1",
            start_date="2026-07-23",
            end_date="2026-07-24",
        )

    assert routed_quote is quote
    assert source == "GlobalFuturesFetcher"
    assert len(routed_frame) == 2
    assert futures.calls == [("quote", "GC1"), ("daily", "GC1")]
    assert yahoo.calls == []


def test_manager_prefers_global_futures_for_nq00y_realtime() -> None:
    quote = UnifiedRealtimeQuote(
        code="NQ00Y",
        source=RealtimeSource.SINA,
        provider_timestamp="2026-07-31T12:58:25+00:00",
        price=28453.41,
        change_pct=0.76,
    )
    futures = _RouteFetcher("GlobalFuturesFetcher", quote=quote)
    yahoo = _RouteFetcher("YfinanceFetcher", priority=4)
    manager = DataFetcherManager(fetchers=[futures, yahoo])
    config = SimpleNamespace(enable_realtime_quote=True, realtime_cache_ttl=120)
    DataFetcherManager.reset_realtime_source_health()

    with patch("src.config.get_config", return_value=config):
        routed_quote = manager.get_realtime_quote_with_provider_timestamp("NQ00Y")

    assert routed_quote is quote
    assert futures.calls == [("quote", "NQ00Y")]
    assert yahoo.calls == []
