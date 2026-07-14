# -*- coding: utf-8 -*-
"""Tests for point-in-time A-share universe resolution."""

from datetime import date

import pandas as pd
import pytest

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
