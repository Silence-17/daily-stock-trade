# -*- coding: utf-8 -*-
"""Tests for fail-closed BSE lifecycle composition."""

from datetime import date

import pytest

from data_provider.bse_lifecycle_fetcher import BseLifecycleFetcher


class _StubFetcher(BseLifecycleFetcher):
    def __init__(self, *, current, mapping, delisted) -> None:
        super().__init__(today=date(2026, 7, 20))
        self.current = current
        self.mapping = mapping
        self.delisted = delisted

    def _fetch_current_rows(self):
        return self.current

    def _fetch_code_mapping_rows(self):
        return self.mapping

    def _fetch_final_delist_rows(self):
        return self.delisted


def _row(code: str, *, listed: str, name: str = "stock"):
    return {
        "code": code,
        "name": name,
        "industry": None,
        "market": "BSE",
        "exchange": "BSE",
        "list_status": "L",
        "list_date": listed,
        "delist_date": None,
    }


def test_bse_lifecycle_includes_mapped_delisted_symbol() -> None:
    fetcher = _StubFetcher(
        current=[_row("920748", listed="2023-08-16")],
        mapping=[
            _row("920748", listed="2023-08-16"),
            _row("920680", listed="2021-11-15", name="old name"),
        ],
        delisted=[{"code": "920680", "name": "final name", "delist_date": "2025-12-31"}],
    )

    frame = fetcher.get_stock_lifecycle_list()

    assert frame["code"].tolist() == ["920680", "920748"]
    row = frame.loc[frame["code"] == "920680"].iloc[0]
    assert row["name"] == "final name"
    assert row["list_status"] == "D"
    assert row["delist_date"] == "2025-12-31"
    validation = frame.attrs["lifecycle_methodology"]["coverage_validation"]
    assert validation["unexplained_missing_count"] == 0


def test_bse_lifecycle_fails_closed_for_unexplained_missing_mapping_symbol() -> None:
    fetcher = _StubFetcher(
        current=[],
        mapping=[_row("920680", listed="2021-11-15")],
        delisted=[],
    )

    with pytest.raises(RuntimeError, match="without a final delisting notice"):
        fetcher.get_stock_lifecycle_list()


def test_bse_lifecycle_fails_closed_for_delisted_symbol_without_listing_date() -> None:
    fetcher = _StubFetcher(
        current=[],
        mapping=[],
        delisted=[{"code": "920999", "name": "unknown", "delist_date": "2026-01-01"}],
    )

    with pytest.raises(RuntimeError, match="lack an authoritative listing-date row"):
        fetcher.get_stock_lifecycle_list()


def test_bse_selected_layer_date_is_clamped_to_exchange_opening() -> None:
    assert BseLifecycleFetcher._normalize_list_date("2020/07/27") == "2021-11-15"


def test_bse_official_mapping_exposes_legacy_code_without_changing_lifecycle_schema() -> None:
    fetcher = _StubFetcher(
        current=[_row("920748", listed="2023-08-16")],
        mapping=[{**_row("920748", listed="2023-08-16"), "legacy_code": "833748"}],
        delisted=[],
    )

    assert fetcher.get_legacy_code_map() == {"920748": "833748"}
    assert "legacy_code" not in fetcher.get_stock_lifecycle_list().columns
