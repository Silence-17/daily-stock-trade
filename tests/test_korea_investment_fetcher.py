# -*- coding: utf-8 -*-
"""Deterministic tests for the KIS Korean realtime quote adapter."""

from datetime import datetime, timezone

import requests

from data_provider.korea_investment_fetcher import KoreaInvestmentFetcher
from data_provider.realtime_types import RealtimeSource


NOW = datetime(2026, 7, 24, 1, 15, 45, tzinfo=timezone.utc)


class _Response:
    def __init__(self, payload, status_code: int = 200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self.payload


class _Session:
    def __init__(self, *, stale: bool = False, api_error: bool = False):
        self.stale = stale
        self.api_error = api_error
        self.post_calls = []
        self.get_calls = []

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        return _Response({"access_token": "token", "expires_in": 3600})

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        if self.api_error:
            return _Response({"rt_cd": "1", "msg_cd": "ERROR", "msg1": "rejected"})
        if url.endswith("inquire-index-timeprice"):
            event_time = "101300" if self.stale else "101500"
            return _Response(
                {
                    "rt_cd": "0",
                    "output": [
                        {
                            "bsop_hour": event_time,
                            "bstp_nmix_prpr": "2850.25",
                            "bstp_nmix_prdy_ctrt": "1.25",
                            "bstp_nmix_prdy_vrss": "35.10",
                            "acml_vol": "123456",
                            "acml_tr_pbmn": "789000000",
                        }
                    ],
                }
            )
        return _Response(
            {
                "rt_cd": "0",
                "output2": [
                    {
                        "stck_cntg_hour": "101530",
                        "stck_prpr": "71000",
                        "prdy_ctrt": "2.10",
                        "prdy_vrss": "1460",
                        "acml_vol": "987654",
                    }
                ],
            }
        )


def _fetcher(session: _Session) -> KoreaInvestmentFetcher:
    return KoreaInvestmentFetcher(
        app_key="app-key",
        app_secret="app-secret",
        base_url="https://kis.example.test",
        session=session,
        now_provider=lambda: NOW,
    )


def test_fetches_timestamped_kospi_and_reuses_access_token() -> None:
    session = _Session()
    fetcher = _fetcher(session)

    kospi = fetcher.get_realtime_quote("KS11")
    kosdaq = fetcher.get_realtime_quote("KQ11")

    assert kospi is not None and kosdaq is not None
    assert kospi.source is RealtimeSource.KOREA_INVESTMENT
    assert kospi.price == 2850.25
    assert kospi.change_pct == 1.25
    assert kospi.provider_timestamp == "2026-07-24T01:15:00+00:00"
    assert kospi.market == "kr" and kospi.currency == "KRW"
    assert len(session.post_calls) == 1
    assert session.get_calls[0][1]["params"]["FID_INPUT_ISCD"] == "0001"
    assert session.get_calls[1][1]["params"]["FID_INPUT_ISCD"] == "1001"


def test_fetches_timestamped_samsung_and_hynix_quotes() -> None:
    session = _Session()
    fetcher = _fetcher(session)

    samsung = fetcher.get_realtime_quote("005930.KS")
    hynix = fetcher.get_realtime_quote("000660.KS")

    assert samsung is not None and hynix is not None
    assert samsung.price == 71000
    assert samsung.change_pct == 2.1
    assert samsung.provider_timestamp == "2026-07-24T01:15:30+00:00"
    assert samsung.pre_close is not None
    assert session.get_calls[0][1]["params"]["FID_INPUT_ISCD"] == "005930"
    assert session.get_calls[1][1]["params"]["FID_INPUT_ISCD"] == "000660"


def test_rejects_stale_timestamp_instead_of_using_request_time() -> None:
    session = _Session(stale=True)

    assert _fetcher(session).get_realtime_quote("KS11") is None


def test_fails_closed_for_api_rejection_and_unsupported_symbol() -> None:
    fetcher = _fetcher(_Session(api_error=True))

    assert fetcher.get_realtime_quote("KS11") is None
    assert fetcher.get_realtime_quote("035720.KQ") is None


def test_requires_credentials_and_only_advertises_realtime_capability() -> None:
    fetcher = KoreaInvestmentFetcher(
        app_key="",
        app_secret="",
        base_url="https://kis.example.test",
        session=_Session(),
        now_provider=lambda: NOW,
    )

    assert fetcher.is_available() is False
    assert fetcher.is_available_for_request("realtime_quote") is False
    assert fetcher.is_available_for_request("daily_data") is False
