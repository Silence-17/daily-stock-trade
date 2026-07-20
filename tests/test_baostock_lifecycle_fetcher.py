# -*- coding: utf-8 -*-
"""Tests for BaoStock lifecycle normalization."""

from contextlib import contextmanager

from data_provider.baostock_fetcher import BaostockFetcher


class _Result:
    error_code = "0"
    error_msg = ""
    fields = ["code", "code_name", "ipoDate", "outDate", "type", "status"]

    def __init__(self) -> None:
        self.rows = iter([
            ["sh.600001", "old sh", "1990-12-19", "2001-04-23", "1", "0"],
            ["sz.000001", "active sz", "1991-04-03", "", "1", "1"],
            ["sh.000001", "index", "1991-07-15", "", "2", "1"],
        ])
        self.current = None

    def next(self) -> bool:
        try:
            self.current = next(self.rows)
            return True
        except StopIteration:
            return False

    def get_row_data(self):
        return self.current


class _Api:
    def query_stock_basic(self):
        return _Result()


def test_baostock_lifecycle_keeps_delisted_stocks_and_filters_non_stocks() -> None:
    fetcher = BaostockFetcher()

    @contextmanager
    def session():
        yield _Api()

    fetcher._baostock_session = session
    frame = fetcher.get_stock_lifecycle_list()

    assert frame is not None
    assert frame["code"].tolist() == ["600001", "000001"]
    assert frame["exchange"].tolist() == ["SSE", "SZSE"]
    assert frame["list_status"].tolist() == ["D", "L"]
    assert frame.iloc[0]["delist_date"] == "2001-04-23"
    assert frame.iloc[1]["delist_date"] is None
    assert frame.attrs["lifecycle_methodology"]["coverage_exchanges"] == ["SSE", "SZSE"]
