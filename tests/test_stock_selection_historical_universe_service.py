# -*- coding: utf-8 -*-
"""Tests for point-in-time A-share universe resolution."""

from datetime import date

import pandas as pd
import pytest
from unittest.mock import patch

from src.services.stock_selection_historical_universe_service import (
    StockSelectionHistoricalUniverseService,
)


def test_resolve_includes_delisted_symbols_that_were_active_on_snapshot_date() -> None:
    frame = pd.DataFrame([
        {"code": "600001", "name": "active", "list_date": "19910101", "delist_date": None, "list_status": "L"},
        {"code": "600002", "name": "delisted", "list_date": "20000101", "delist_date": "20240131", "list_status": "D"},
        {"code": "600003", "name": "future", "list_date": "20240201", "delist_date": None, "list_status": "L"},
        {"code": "600004", "name": "old", "list_date": "20000101", "delist_date": "20230101", "list_status": "D"},
    ])

    result = StockSelectionHistoricalUniverseService(loader=lambda: frame).resolve(
        market="cn", snapshot_date=date(2024, 1, 5)
    )

    assert [item["symbol"] for item in result["items"]] == ["600001", "600002"]
    assert result["methodology"]["includes_delisted_symbols"] is True
    assert result["methodology"]["uses_current_universe_fallback"] is False


def test_resolve_reports_truncation_and_rejects_unavailable_source() -> None:
    frame = pd.DataFrame([
        {"code": "600001", "name": "one", "list_date": "2020-01-01", "delist_date": None},
        {"code": "600002", "name": "two", "list_date": "20200101", "delist_date": "NaT"},
    ])
    result = StockSelectionHistoricalUniverseService(loader=lambda: frame).resolve(
        market="cn", snapshot_date=date(2024, 1, 5), limit=1
    )
    assert result["total_count"] == 2
    assert result["returned_count"] == 1
    assert result["truncated"] is True

    with pytest.raises(RuntimeError, match="stock lifecycle metadata is unavailable"):
        StockSelectionHistoricalUniverseService(loader=lambda: None).resolve(
            market="cn", snapshot_date=date(2024, 1, 5)
        )


def test_resolve_many_loads_lifecycle_once_and_keeps_each_date_isolated() -> None:
    calls = []
    frame = pd.DataFrame([
        {"code": "600001", "name": "old", "list_date": "20200101", "delist_date": "20240131"},
        {"code": "600002", "name": "new", "list_date": "20240201", "delist_date": None},
    ])
    service = StockSelectionHistoricalUniverseService(loader=lambda: calls.append(True) or frame)

    results = service.resolve_many(
        market="cn",
        snapshot_dates=[date(2024, 1, 5), date(2024, 2, 5)],
    )

    assert calls == [True]
    assert [item["symbol"] for item in results[0]["items"]] == ["600001"]
    assert [item["symbol"] for item in results[1]["items"]] == ["600002"]


def test_default_loader_prefers_tushare_and_preserves_source_metadata() -> None:
    frame = pd.DataFrame([
        {"code": "600001", "name": "one", "list_date": "20200101", "delist_date": None},
    ])
    frame.attrs["lifecycle_methodology"] = {
        "source": "tushare.stock_basic",
        "sources": ["tushare.stock_basic"],
        "coverage_exchanges": ["SSE", "SZSE", "BSE"],
        "includes_delisted_symbols": True,
        "uses_current_universe_fallback": False,
        "point_in_time_name_and_industry": False,
        "permission_requirement": "Tushare stock_basic permission",
    }
    StockSelectionHistoricalUniverseService.clear_cache()
    with patch(
        "data_provider.tushare_fetcher.TushareFetcher.get_stock_lifecycle_list",
        return_value=frame,
    ), patch(
        "data_provider.bse_lifecycle_fetcher.BseLifecycleFetcher.get_stock_lifecycle_list"
    ) as bse_loader:
        result = StockSelectionHistoricalUniverseService().resolve(
            market="cn", snapshot_date=date(2024, 1, 5)
        )

    assert result["methodology"]["source"] == "tushare.stock_basic"
    bse_loader.assert_not_called()
    StockSelectionHistoricalUniverseService.clear_cache()


def test_default_loader_uses_complete_baostock_bse_fallback() -> None:
    mainland = pd.DataFrame([
        {
            "code": "600001", "name": "sh", "industry": None, "market": None,
            "exchange": "SSE", "list_status": "L", "list_date": "1990-12-19",
            "delist_date": None,
        },
    ])
    bse = pd.DataFrame([
        {
            "code": "920680", "name": "bj old", "industry": None, "market": "BSE",
            "exchange": "BSE", "list_status": "D", "list_date": "2021-11-15",
            "delist_date": "2025-12-31",
        },
    ])
    bse.attrs["lifecycle_methodology"] = {
        "coverage_validation": {"unexplained_missing_count": 0},
    }
    StockSelectionHistoricalUniverseService.clear_cache()
    with patch(
        "data_provider.tushare_fetcher.TushareFetcher.get_stock_lifecycle_list",
        return_value=None,
    ), patch(
        "data_provider.baostock_fetcher.BaostockFetcher.get_stock_lifecycle_list",
        return_value=mainland,
    ), patch(
        "data_provider.bse_lifecycle_fetcher.BseLifecycleFetcher.get_stock_lifecycle_list",
        return_value=bse,
    ):
        result = StockSelectionHistoricalUniverseService().resolve(
            market="cn", snapshot_date=date(2024, 1, 5)
        )

    assert [item["symbol"] for item in result["items"]] == ["600001", "920680"]
    assert result["methodology"]["source"] == "baostock+bse.official_lifecycle"
    assert result["methodology"]["coverage_exchanges"] == ["SSE", "SZSE", "BSE"]
    assert result["methodology"]["permission_requirement"] is None
    StockSelectionHistoricalUniverseService.clear_cache()


def test_default_loader_fails_closed_when_bse_fallback_is_incomplete() -> None:
    mainland = pd.DataFrame([{"code": "600001"}])
    StockSelectionHistoricalUniverseService.clear_cache()
    with patch(
        "data_provider.tushare_fetcher.TushareFetcher.get_stock_lifecycle_list",
        return_value=None,
    ), patch(
        "data_provider.baostock_fetcher.BaostockFetcher.get_stock_lifecycle_list",
        return_value=mainland,
    ), patch(
        "data_provider.bse_lifecycle_fetcher.BseLifecycleFetcher.get_stock_lifecycle_list",
        side_effect=RuntimeError("coverage gap"),
    ):
        with pytest.raises(RuntimeError, match="coverage gap"):
            StockSelectionHistoricalUniverseService().resolve(
                market="cn", snapshot_date=date(2024, 1, 5)
            )
    StockSelectionHistoricalUniverseService.clear_cache()
