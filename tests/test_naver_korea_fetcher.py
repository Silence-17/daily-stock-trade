# -*- coding: utf-8 -*-
"""Deterministic tests for the credential-free Naver Korean quote adapter."""

from __future__ import annotations

import requests

from data_provider.naver_korea_fetcher import NaverKoreaFetcher
from data_provider.realtime_types import RealtimeSource


class _Response:
    def __init__(self, payload, *, error: bool = False):
        self.payload = payload
        self.error = error

    def raise_for_status(self):
        if self.error:
            raise requests.HTTPError("HTTP 503")

    def json(self):
        return self.payload


class _Session:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.payloads.pop(0)


def _payload(*, stock: bool, timestamp: str = "2026-07-24T10:15:30+09:00"):
    row = {
        "stockName": "삼성전자" if stock else "코스피",
        "closePriceRaw": "71000" if stock else "2850.25",
        "fluctuationsRatioRaw": "2.10" if stock else "1.25",
        "compareToPreviousClosePriceRaw": "1460" if stock else "35.10",
        "openPriceRaw": "70000" if stock else "2820.00",
        "highPriceRaw": "71500" if stock else "2860.00",
        "lowPriceRaw": "69800" if stock else "2810.00",
        "accumulatedTradingVolumeRaw": "987654",
        "accumulatedTradingValueRaw": "70000000000",
        "localTradedAt": timestamp,
    }
    return {"pollingInterval": 70000, "datas": [row]}


def test_fetches_timestamped_kospi_quote() -> None:
    session = _Session([_Response(_payload(stock=False))])
    fetcher = NaverKoreaFetcher(session=session)

    quote = fetcher.get_realtime_quote("KS11")

    assert quote is not None
    assert quote.source is RealtimeSource.NAVER
    assert quote.code == "KS11"
    assert quote.price == 2850.25
    assert quote.change_pct == 1.25
    assert quote.pre_close == 2815.15
    assert quote.provider_timestamp == "2026-07-24T01:15:30+00:00"
    assert quote.volume == 987654
    assert quote.amount == 70000000000
    assert session.calls[0][0].endswith("/index/KOSPI")
    assert session.calls[0][1]["timeout"] == 5.0


def test_fetches_timestamped_samsung_quote() -> None:
    session = _Session([_Response(_payload(stock=True))])

    quote = NaverKoreaFetcher(session=session).get_realtime_quote("005930.KS")

    assert quote is not None
    assert quote.code == "005930.KS"
    assert quote.price == 71000
    assert quote.pre_close == 69540
    assert quote.market == "kr" and quote.currency == "KRW"
    assert session.calls[0][0].endswith("/stock/005930")


def test_fetches_generic_suffix_korean_equity() -> None:
    session = _Session([_Response(_payload(stock=True))])

    quote = NaverKoreaFetcher(session=session).get_realtime_quote("009150.KS")

    assert quote is not None
    assert quote.code == "009150.KS"
    assert session.calls[0][0].endswith("/stock/009150")


def test_rejects_missing_or_naive_provider_timestamp() -> None:
    missing = _Session([_Response(_payload(stock=False, timestamp=""))])
    naive = _Session([_Response(_payload(stock=False, timestamp="2026-07-24T10:15:30"))])

    assert NaverKoreaFetcher(session=missing).get_realtime_quote("KS11") is None
    assert NaverKoreaFetcher(session=naive).get_realtime_quote("KS11") is None


def test_fails_closed_for_transport_error_and_unsupported_symbol() -> None:
    failing = _Session([_Response({}, error=True)])
    fetcher = NaverKoreaFetcher(session=failing)

    assert fetcher.get_realtime_quote("KQ11") is None
    assert fetcher.get_realtime_quote("AAPL") is None


def test_only_advertises_realtime_capability() -> None:
    fetcher = NaverKoreaFetcher(session=_Session([]))

    assert fetcher.is_available_for_request("realtime_quote") is True
    assert fetcher.is_available_for_request("daily_data") is False
