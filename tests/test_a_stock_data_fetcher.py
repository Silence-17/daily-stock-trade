# -*- coding: utf-8 -*-
"""Regression tests for a-stock-data Eastmoney board helpers."""

from data_provider.a_stock_data_fetcher import AStockDataFetcher


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, payload):
        self.payloads = list(payload) if isinstance(payload, list) else [payload]
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None, **kwargs):
        self.calls.append(
            {
                "url": url,
                "params": params,
                "headers": headers,
                "timeout": timeout,
                "kwargs": kwargs,
            }
        )
        payload = self.payloads.pop(0)
        if isinstance(payload, Exception):
            raise payload
        return _FakeResponse(payload)


def test_get_industry_boards_parses_eastmoney_clist_rows():
    session = _FakeSession(
        {
            "data": {
                "diff": [
                    {
                        "f12": "BK1036",
                        "f14": "半导体",
                        "f3": 2.31,
                        "f104": 86,
                        "f105": 12,
                        "f140": "中芯国际",
                        "f136": 8.2,
                    },
                    {
                        "f12": "BK0475",
                        "f14": "白酒",
                        "f3": -1.2,
                        "f104": 5,
                        "f105": 28,
                        "f140": "贵州茅台",
                        "f136": -0.8,
                    },
                ]
            }
        }
    )
    fetcher = AStockDataFetcher(session=session, min_interval_seconds=0)

    boards = fetcher.get_industry_boards()

    assert boards == [
        {
            "rank": 1,
            "code": "BK1036",
            "name": "半导体",
            "change_pct": 2.31,
            "up_count": 86,
            "down_count": 12,
            "leader": "中芯国际",
            "leader_change": 8.2,
            "source": "a_stock_data_eastmoney",
            "data_quality": "realtime",
        },
        {
            "rank": 2,
            "code": "BK0475",
            "name": "白酒",
            "change_pct": -1.2,
            "up_count": 5,
            "down_count": 28,
            "leader": "贵州茅台",
            "leader_change": -0.8,
            "source": "a_stock_data_eastmoney",
            "data_quality": "realtime",
        },
    ]
    assert session.calls[0]["params"]["fs"] == "m:90+t:2"


def test_get_industry_boards_falls_back_to_reportapi_directory():
    session = _FakeSession(
        [
            {"data": {"diff": []}},
            {
                "data": [
                    {
                        "industryCode": "451",
                        "industryName": "房地产开发",
                        "emIndustryCode": "101024",
                    },
                    {
                        "industryCode": "473",
                        "industryName": "证券Ⅱ",
                        "emIndustryCode": "101023002002",
                    },
                    {
                        "industryCode": "451",
                        "industryName": "房地产开发",
                        "emIndustryCode": "101024",
                    },
                ]
            },
        ]
    )
    fetcher = AStockDataFetcher(session=session, min_interval_seconds=0)

    boards = fetcher.get_industry_boards()

    assert boards == [
        {
            "rank": 1,
            "code": "101024",
            "name": "房地产开发",
            "source": "a_stock_data_eastmoney_reportapi_fallback",
            "data_quality": "directory_fallback",
        },
        {
            "rank": 2,
            "code": "101023002002",
            "name": "证券Ⅱ",
            "source": "a_stock_data_eastmoney_reportapi_fallback",
            "data_quality": "directory_fallback",
        },
    ]
    assert session.calls[0]["url"].endswith("/api/qt/clist/get")
    assert session.calls[1]["url"].endswith("/report/list")
    assert session.calls[1]["params"]["qType"] == "1"


def test_get_industry_boards_returns_offline_seed_when_online_sources_fail():
    session = _FakeSession([ConnectionError("push2 closed"), ConnectionError("reportapi closed")])
    fetcher = AStockDataFetcher(session=session, min_interval_seconds=0)

    boards = fetcher.get_industry_boards()

    assert len(boards) >= 20
    assert boards[0] == {
        "rank": 1,
        "code": "",
        "name": "农林牧渔",
        "source": "a_stock_data_offline_industry_seed",
        "data_quality": "offline_seed",
    }
    assert any(board["name"] == "银行" for board in boards)


def test_get_belong_board_parses_slist_and_preserves_extended_fields():
    session = _FakeSession(
        {
            "data": {
                "diff": {
                    "0": {"f12": "BK0438", "f14": "食品饮料", "f3": 0.66, "f128": "贵州茅台"},
                    "1": {"f12": "BK0896", "f14": "酿酒概念", "f3": 1.23, "f128": "五粮液"},
                }
            }
        }
    )
    fetcher = AStockDataFetcher(session=session, min_interval_seconds=0)

    boards = fetcher.get_belong_board("600519")

    assert boards == [
        {
            "name": "食品饮料",
            "code": "BK0438",
            "change_pct": 0.66,
            "lead_stock": "贵州茅台",
            "source": "a_stock_data_eastmoney",
        },
        {
            "name": "酿酒概念",
            "code": "BK0896",
            "change_pct": 1.23,
            "lead_stock": "五粮液",
            "source": "a_stock_data_eastmoney",
        },
    ]
    assert session.calls[0]["params"]["secid"] == "1.600519"
    assert session.calls[0]["params"]["spt"] == "3"
