import json
from datetime import datetime, timezone
from unittest.mock import Mock, patch

import requests

from data_provider.japan_index_fetcher import JapanIndexFetcher
from data_provider.realtime_types import RealtimeSource


def _response(*, payload=None, text=""):
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = payload
    response.text = text
    return response


def test_nikkei_quote_uses_eastmoney_provider_timestamp() -> None:
    timestamp = 1_785_732_701
    session = Mock()
    session.get.return_value = _response(
        payload={
            "data": {
                "diff": [{
                    "f2": 63733.22,
                    "f3": -0.98,
                    "f4": -628.8,
                    "f12": "N225",
                    "f14": "Nikkei 225",
                    "f15": 63905.12,
                    "f16": 62703.47,
                    "f17": 63834.95,
                    "f18": 64362.02,
                    "f124": timestamp,
                }]
            }
        }
    )

    quote = JapanIndexFetcher(session=session).get_realtime_quote("N225")

    assert quote is not None
    assert quote.source == RealtimeSource.EASTMONEY_GLOBAL
    assert quote.provider_timestamp == datetime.fromtimestamp(
        timestamp,
        tz=timezone.utc,
    ).isoformat()
    assert quote.price == 63733.22
    assert quote.change_pct == -0.98


def test_topix_quote_accepts_only_zero_delay_yahoo_japan_payload() -> None:
    session = Mock()
    payload = {
        "code": "998405.T",
        "name": "TOPIX",
        "japanUpdateTime": "13:52",
        "price": "3,960.83",
        "changePrice": "-42.47",
        "changePriceRate": "-1.06",
        "delayMinutes": 0,
        "isDelayed": False,
    }
    session.get.return_value = _response(
        text=(
            "<script>window.__PRELOADED_STATE__={"
            '"mainDomesticIndexPriceBoard":{"indexPrices":'
            + json.dumps(payload)
            + ',"currentTabNavigationKey":"detail"}};</script>'
        )
    )
    fetcher = JapanIndexFetcher(
        session=session,
        clock=lambda: datetime(2026, 8, 3, 4, 52, 30, tzinfo=timezone.utc),
    )

    quote = fetcher.get_realtime_quote("TOPX")

    assert quote is not None
    assert quote.source == RealtimeSource.YAHOO_JAPAN
    assert quote.provider_timestamp == "2026-08-03T04:52:00+00:00"
    assert quote.price == 3960.83
    assert quote.change_pct == -1.06
    assert quote.pre_close == 4003.3


def test_nikkei_quote_uses_timestamped_futures_proxy_after_cash_route_failure() -> None:
    session = Mock()
    proxy_response = _response()
    proxy_response.content = (
        'var hq_str_hf_NK="63726.700,,63665.000,63675.000,64300.000,'
        '62655.000,13:08:27,63750.000,63820.000,53293,3,2,2026-08-03,'
        'Nikkei 225 Futures,21915";'
    ).encode("gbk")
    session.get.side_effect = [requests.ConnectionError("cash route unavailable"), proxy_response]

    quote = JapanIndexFetcher(session=session).get_realtime_quote("N225")

    assert quote is not None
    assert quote.source == RealtimeSource.SINA
    assert quote.name == "Nikkei 225 Futures Proxy"
    assert quote.provider_timestamp == "2026-08-03T05:08:27+00:00"
    assert quote.fallback_from == "eastmoney_global"
    assert quote.proxy_instrument == "nikkei_225_futures"


def test_topix_quote_rejects_provider_marked_delay() -> None:
    session = Mock()
    payload = {
        "code": "998405.T",
        "name": "TOPIX",
        "japanUpdateTime": "13:37",
        "price": "3,960.83",
        "changePrice": "-42.47",
        "changePriceRate": "-1.06",
        "delayMinutes": 15,
        "isDelayed": True,
    }
    session.get.return_value = _response(
        text=(
            '"mainDomesticIndexPriceBoard":{"indexPrices":'
            + json.dumps(payload)
            + ',"currentTabNavigationKey":"detail"'
        )
    )
    fetcher = JapanIndexFetcher(
        session=session,
        clock=lambda: datetime(2026, 8, 3, 4, 52, 30, tzinfo=timezone.utc),
    )

    assert fetcher.get_realtime_quote("TOPX") is None


def test_unknown_japan_index_is_not_guessed() -> None:
    session = Mock()
    assert JapanIndexFetcher(session=session).get_realtime_quote("JP_UNKNOWN") is None
    session.get.assert_not_called()


def test_topix_rejects_non_session_day_without_network_call() -> None:
    session = Mock()
    fetcher = JapanIndexFetcher(
        session=session,
        clock=lambda: datetime(2026, 8, 11, 5, 15, tzinfo=timezone.utc),
    )

    with patch(
        "data_provider.japan_index_fetcher.trading_calendar.is_market_open",
        return_value=False,
    ):
        quote = fetcher.get_realtime_quote("TOPX")

    assert quote is None
    session.get.assert_not_called()
