# -*- coding: utf-8 -*-
"""Tests for strict provider-timestamp routing of main index quotes."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from data_provider.base import DataFetcherManager
from data_provider.realtime_types import RealtimeSource
from src.services.cross_market_signal_service import CrossMarketSignalService


class _IndexFetcher:
    def __init__(self, name: str, row: dict) -> None:
        self.name = name
        self.priority = 0
        self.row = row
        self.calls = 0

    def get_main_indices(self, region: str = "cn"):
        self.calls += 1
        return [self.row] if region == "cn" else None


def _index_row(*, provider_timestamp=None) -> dict:
    return {
        "code": "sh000300",
        "name": "CSI 300",
        "current": 101.0,
        "change": 2.0,
        "change_pct": 2.02,
        "open": 100.0,
        "high": 102.0,
        "low": 99.0,
        "prev_close": 99.0,
        "volume": 1000,
        "amount": 100500.0,
        "provider_timestamp": provider_timestamp,
    }


def test_main_index_quote_skips_provider_without_timestamp(monkeypatch) -> None:
    stale_shape = _IndexFetcher("TushareFetcher", _index_row())
    timestamped = _IndexFetcher(
        "EfinanceFetcher",
        _index_row(provider_timestamp="2026-07-24T01:30:00+00:00"),
    )
    manager = DataFetcherManager(fetchers=[stale_shape, timestamped])
    monkeypatch.setattr(manager, "_get_tickflow_fetcher", lambda: None)

    quote = manager.get_main_index_quote_with_provider_timestamp("000300")

    assert quote is not None
    assert quote.source is RealtimeSource.EFINANCE
    assert quote.provider_timestamp == "2026-07-24T01:30:00+00:00"
    assert quote.open_price == 100.0
    assert quote.pre_close == 99.0
    assert stale_shape.calls == 1
    assert timestamped.calls == 1


def test_cn_open_collection_uses_strict_main_index_route(tmp_path: Path, monkeypatch) -> None:
    now = datetime(2026, 7, 24, 1, 30, 30, tzinfo=timezone.utc)
    timestamped = _IndexFetcher(
        "EfinanceFetcher",
        _index_row(provider_timestamp="2026-07-24T01:30:00+00:00"),
    )
    manager = DataFetcherManager(fetchers=[timestamped])
    monkeypatch.setattr(manager, "_get_tickflow_fetcher", lambda: None)
    service = CrossMarketSignalService(
        data_fetcher_manager=manager,
        state_path=tmp_path / "cross-market-state.json",
    )

    snapshot = service.collect_cn_open_snapshot(now=now)

    assert snapshot["index_code"] == "000300"
    assert snapshot["provider_timestamp"] == "2026-07-24T01:30:00+00:00"
    assert snapshot["quote_source"] == "efinance"
    assert snapshot["gap_pct"] == 1.010101
    assert snapshot["reclaimed_open"] is True
