# -*- coding: utf-8 -*-
"""Free implemented A-share corporate actions with audited source fallback."""

from __future__ import annotations

import threading
import time
from datetime import date
from typing import Any, Dict, List, Optional

import pandas as pd

from data_provider.base import is_bse_code, normalize_stock_code


class AkshareCorporateActionFetcher:
    """Normalize implemented Shanghai/Shenzhen dividends and share distributions."""

    CACHE_TTL_SECONDS = 24 * 60 * 60
    _cache_lock = threading.Lock()
    _cache: Dict[str, tuple[float, List[Dict[str, Any]]]] = {}

    def __init__(self, *, client: Optional[Any] = None) -> None:
        if client is None:
            import akshare as client
        self.client = client

    def get_stock_corporate_actions(
        self,
        stock_code: str,
        *,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
    ) -> List[Dict[str, Any]]:
        code = normalize_stock_code(stock_code)
        if is_bse_code(code):
            return []
        events = self._load(code)
        return [
            dict(item)
            for item in events
            if (start_date is None or item["effective_date"] >= start_date)
            and (end_date is None or item["effective_date"] <= end_date)
        ]

    def _load(self, code: str) -> List[Dict[str, Any]]:
        now = time.monotonic()
        with self._cache_lock:
            cached = self._cache.get(code)
            if cached and now - cached[0] < self.CACHE_TTL_SECONDS:
                return [dict(item) for item in cached[1]]

        errors = []
        for source, loader in (
            ("akshare.stock_fhps_detail_em", self._load_eastmoney),
            ("akshare.stock_dividend_cninfo", self._load_cninfo),
        ):
            try:
                events = loader(code)
                with self._cache_lock:
                    self._cache[code] = (time.monotonic(), [dict(item) for item in events])
                return events
            except Exception as exc:
                errors.append(f"{source} failed: {exc}")
        raise RuntimeError("; ".join(errors))

    def _load_eastmoney(self, code: str) -> List[Dict[str, Any]]:
        frame = self.client.stock_fhps_detail_em(symbol=code)
        required = {
            "除权除息日",
            "方案进度",
            "现金分红-现金分红比例",
            "送转股份-送转总比例",
            "送转股份-送股比例",
            "送转股份-转股比例",
        }
        work = self._validated_frame(frame, required, "EastMoney dividend detail")
        work = work[
            work["方案进度"].astype(str).str.contains("实施", na=False)
        ].copy()
        work["effective_date"] = pd.to_datetime(
            work["除权除息日"], errors="coerce"
        )
        work = work.dropna(subset=["effective_date"])
        if "最新公告日期" in work.columns:
            work["_order"] = pd.to_datetime(work["最新公告日期"], errors="coerce")
            work = work.sort_values("_order")
        work = work.drop_duplicates(subset=["effective_date"], keep="last")
        return self._events_from_rows(
            code=code,
            work=work,
            source="akshare.stock_fhps_detail_em",
            cash_field="现金分红-现金分红比例",
            stock_total_field="送转股份-送转总比例",
            stock_fields=("送转股份-送股比例", "送转股份-转股比例"),
        )

    def _load_cninfo(self, code: str) -> List[Dict[str, Any]]:
        frame = self.client.stock_dividend_cninfo(symbol=code)
        required = {"除权日", "派息比例", "送股比例", "转增比例"}
        work = self._validated_frame(frame, required, "CNInfo dividend detail")
        work["effective_date"] = pd.to_datetime(work["除权日"], errors="coerce")
        work = work.dropna(subset=["effective_date"])
        if "实施方案公告日期" in work.columns:
            work["_order"] = pd.to_datetime(work["实施方案公告日期"], errors="coerce")
            work = work.sort_values("_order")
        work = work.drop_duplicates(subset=["effective_date"], keep="last")
        return self._events_from_rows(
            code=code,
            work=work,
            source="akshare.stock_dividend_cninfo",
            cash_field="派息比例",
            stock_fields=("送股比例", "转增比例"),
        )

    @classmethod
    def _events_from_rows(
        cls,
        *,
        code: str,
        work: pd.DataFrame,
        source: str,
        cash_field: str,
        stock_fields: tuple[str, ...],
        stock_total_field: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        events: List[Dict[str, Any]] = []
        for _, row in work.sort_values("effective_date").iterrows():
            effective_date = row["effective_date"].date()
            cash_per_ten = cls._positive(row.get(cash_field))
            if cash_per_ten > 0:
                events.append({
                    "symbol": code,
                    "effective_date": effective_date,
                    "action_type": "cash_dividend",
                    "cash_dividend_per_share": round(cash_per_ten / 10.0, 10),
                    "source": source,
                    "source_record_key": (
                        f"{source}|{code}|{effective_date.isoformat()}|cash_dividend"
                    ),
                })
            stock_per_ten = (
                cls._positive(row.get(stock_total_field))
                if stock_total_field
                else 0.0
            )
            if stock_per_ten <= 0:
                stock_per_ten = sum(cls._positive(row.get(field)) for field in stock_fields)
            if stock_per_ten > 0:
                events.append({
                    "symbol": code,
                    "effective_date": effective_date,
                    "action_type": "split_adjustment",
                    "split_ratio": round(1.0 + stock_per_ten / 10.0, 10),
                    "source": source,
                    "source_record_key": (
                        f"{source}|{code}|{effective_date.isoformat()}|split_adjustment"
                    ),
                })
        return events

    @staticmethod
    def _validated_frame(frame: Any, required: set[str], label: str) -> pd.DataFrame:
        if not isinstance(frame, pd.DataFrame):
            raise RuntimeError(f"{label} returned a non-tabular payload")
        missing = sorted(required - set(frame.columns))
        if missing:
            raise RuntimeError(f"{label} is missing fields: {', '.join(missing)}")
        return frame.copy()

    @staticmethod
    def _positive(value: Any) -> float:
        parsed = pd.to_numeric(value, errors="coerce")
        return float(parsed) if pd.notna(parsed) and float(parsed) > 0 else 0.0

    @classmethod
    def clear_cache(cls) -> None:
        with cls._cache_lock:
            cls._cache.clear()
