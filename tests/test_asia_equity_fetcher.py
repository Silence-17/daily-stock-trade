# -*- coding: utf-8 -*-
"""Deterministic tests for strict Japanese and Taiwan equity quotes."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import patch

from data_provider.asia_equity_fetcher import AsiaEquityFetcher
from data_provider.realtime_types import RealtimeSource


class _Response:
    def __init__(self, *, text: str = "", payload=None):
        self.text = text
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class _Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def _yahoo_japan_page(*, delay_minutes: int = 0) -> str:
    board = {
        "stockType": "STOCK",
        "name": "Murata Manufacturing",
        "code": "6981",
        "codeWithMarketExtension": "6981.T",
        "price": {"value": "7,215"},
        "priceChange": {"value": "-201"},
        "priceChangeRate": {"value": "-2.71"},
        "japanUpdateTime": "14:14",
        "delayMinutes": delay_minutes,
    }
    decoded = (
        '6:["$",{"preloadedStore":{"priceBoard":'
        '{"currentKey":"detail","board":'
        f"{json.dumps(board, ensure_ascii=False)}"
        "}}}]"
    )
    return f"<script>self.__next_f.push([1,{json.dumps(decoded)}])</script>"


def _twse_payload(*, exchange: str = "tse", code: str = "2383"):
    shifted_epoch = datetime(2026, 8, 3, 6, 29, 45, tzinfo=timezone.utc)
    return {
        "msgArray": [{
            "c": code,
            "ex": exchange,
            "nf": "Elite Material Co Ltd",
            "z": "4,980.0000",
            "y": "4,745.0000",
            "o": "5,135.0000",
            "h": "5,195.0000",
            "l": "4,930.0000",
            "v": "3,989",
            "d": "20260803",
            "t": "13:29:45",
            "tlong": str(int(shifted_epoch.timestamp() * 1000)),
        }]
    }


def test_parses_zero_delay_yahoo_japan_equity_quote() -> None:
    session = _Session([_Response(text=_yahoo_japan_page())])
    fetcher = AsiaEquityFetcher(
        session=session,
        clock=lambda: datetime(2026, 8, 3, 5, 15, tzinfo=timezone.utc),
    )

    quote = fetcher.get_realtime_quote("6981.T")

    assert quote is not None
    assert quote.source is RealtimeSource.YAHOO_JAPAN
    assert quote.price == 7215.0
    assert quote.change_pct == -2.71
    assert quote.pre_close == 7416.0
    assert quote.provider_timestamp == "2026-08-03T05:14:00+00:00"
    assert session.calls[0][0].endswith("/6981.T")


def test_rejects_delayed_yahoo_japan_equity_quote() -> None:
    session = _Session([_Response(text=_yahoo_japan_page(delay_minutes=20))])
    fetcher = AsiaEquityFetcher(
        session=session,
        clock=lambda: datetime(2026, 8, 3, 5, 15, tzinfo=timezone.utc),
    )

    assert fetcher.get_realtime_quote("6981.T") is None


def test_parses_twse_quote_with_explicit_trade_date_and_time() -> None:
    session = _Session([_Response(payload=_twse_payload())])

    quote = AsiaEquityFetcher(session=session).get_realtime_quote("2383.TW")

    assert quote is not None
    assert quote.source is RealtimeSource.TWSE
    assert quote.price == 4980.0
    assert quote.change_pct == 4.952582
    assert quote.provider_timestamp == "2026-08-03T05:29:45+00:00"
    assert quote.volume == 3989000
    assert session.calls[0][1]["params"]["ex_ch"] == "tse_2383.tw"


def test_rejects_twse_quote_without_explicit_trade_date_or_time() -> None:
    payload = _twse_payload()
    del payload["msgArray"][0]["t"]
    session = _Session([_Response(payload=payload)])

    assert AsiaEquityFetcher(session=session).get_realtime_quote("2383.TW") is None


def test_routes_tpex_suffix_to_otc_channel() -> None:
    session = _Session([
        _Response(payload=_twse_payload(exchange="otc", code="6274"))
    ])

    quote = AsiaEquityFetcher(session=session).get_realtime_quote("6274.TWO")

    assert quote is not None
    assert quote.market == "tw"
    assert session.calls[0][1]["params"]["ex_ch"] == "otc_6274.tw"


def test_rejects_unknown_or_mismatched_symbol() -> None:
    session = _Session([_Response(payload=_twse_payload(code="2330"))])
    fetcher = AsiaEquityFetcher(session=session)

    assert fetcher.get_realtime_quote("AAPL") is None
    assert fetcher.get_realtime_quote("2383.TW") is None
    assert len(session.calls) == 1


def test_japan_equity_rejects_non_session_day_without_network_call() -> None:
    session = _Session([])
    fetcher = AsiaEquityFetcher(
        session=session,
        clock=lambda: datetime(2026, 8, 11, 5, 15, tzinfo=timezone.utc),
    )

    with patch(
        "data_provider.asia_equity_fetcher.trading_calendar.is_market_open",
        return_value=False,
    ):
        quote = fetcher.get_realtime_quote("6981.T")

    assert quote is None
    assert session.calls == []
