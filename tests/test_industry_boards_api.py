# -*- coding: utf-8 -*-
"""API regressions for industry board list endpoint."""

from types import SimpleNamespace
from unittest.mock import patch

from api.v1.endpoints import stocks as stocks_endpoint


def test_get_industry_boards_returns_manager_payload():
    boards = [
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
        }
    ]
    manager = SimpleNamespace(get_industry_boards=lambda: (boards, "AStockDataFetcher"))

    with patch.object(stocks_endpoint, "DataFetcherManager", return_value=manager):
        response = stocks_endpoint.get_industry_boards()

    assert response.total == 1
    assert response.source == "AStockDataFetcher"
    assert response.boards[0].code == "BK1036"
    assert response.boards[0].name == "半导体"
    assert response.boards[0].change_pct == 2.31
    assert response.data_quality == "realtime"
    assert "实时" in response.message


def test_get_industry_boards_marks_directory_fallback_quality():
    boards = [
        {
            "rank": 1,
            "code": "101024",
            "name": "房地产开发",
            "source": "a_stock_data_eastmoney_reportapi_fallback",
            "data_quality": "directory_fallback",
        }
    ]
    manager = SimpleNamespace(get_industry_boards=lambda: (boards, "AStockDataFetcher"))

    with patch.object(stocks_endpoint, "DataFetcherManager", return_value=manager):
        response = stocks_endpoint.get_industry_boards()

    assert response.total == 1
    assert response.data_quality == "directory_fallback"
    assert "reportapi" in response.message
    assert response.boards[0].change_pct is None
