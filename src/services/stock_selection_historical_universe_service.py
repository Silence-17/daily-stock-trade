# -*- coding: utf-8 -*-
"""Resolve an A-share universe from listing lifecycle metadata."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Callable, Dict, Iterable, List, Optional

import pandas as pd


class StockSelectionHistoricalUniverseService:
    """Build a survivor-bias-aware universe for a requested date."""

    def __init__(self, loader: Optional[Callable[[], Optional[pd.DataFrame]]] = None) -> None:
        self.loader = loader

    def resolve(self, *, market: str, snapshot_date: date, limit: int = 6000) -> Dict[str, Any]:
        return self.resolve_many(
            market=market,
            snapshot_dates=[snapshot_date],
            limit=limit,
        )[0]

    def resolve_many(
        self,
        *,
        market: str,
        snapshot_dates: Iterable[date],
        limit: int = 6000,
    ) -> List[Dict[str, Any]]:
        if str(market or "").strip().lower() != "cn":
            raise ValueError("historical universe resolution currently supports market=cn only")
        if not 1 <= limit <= 6000:
            raise ValueError("limit must be between 1 and 6000")
        dates = list(snapshot_dates)
        if not dates:
            raise ValueError("snapshot_dates must not be empty")

        frame = self._load()
        if frame is None or frame.empty:
            raise RuntimeError(
                "Tushare stock lifecycle metadata is unavailable; configure a token with stock_basic permission"
            )
        required = {"code", "name", "list_date", "delist_date"}
        missing = sorted(required - set(frame.columns))
        if missing:
            raise RuntimeError(f"Tushare lifecycle metadata is missing fields: {', '.join(missing)}")

        records = frame.to_dict(orient="records")
        return [
            self._resolve_records(records=records, snapshot_date=item, limit=limit)
            for item in dates
        ]

    def _resolve_records(
        self,
        *,
        records: List[Dict[str, Any]],
        snapshot_date: date,
        limit: int,
    ) -> Dict[str, Any]:
        rows = []
        for raw in records:
            list_date = self._parse_date(raw.get("list_date"))
            delist_date = self._parse_date(raw.get("delist_date"))
            if list_date is None or list_date > snapshot_date:
                continue
            if delist_date is not None and snapshot_date > delist_date:
                continue
            code = str(raw.get("code") or "").strip()
            name = str(raw.get("name") or "").strip()
            if not code or not name:
                continue
            rows.append({
                "symbol": code,
                "name": name,
                "industry": self._text_or_none(raw.get("industry")),
                "exchange": self._text_or_none(raw.get("exchange")),
                "board": self._text_or_none(raw.get("market")),
                "list_status": self._text_or_none(raw.get("list_status")),
                "list_date": list_date.isoformat(),
                "delist_date": delist_date.isoformat() if delist_date else None,
            })
        deduplicated = {}
        for item in rows:
            previous = deduplicated.get(item["symbol"])
            if previous is None or item["list_date"] > previous["list_date"]:
                deduplicated[item["symbol"]] = item
        rows = sorted(deduplicated.values(), key=lambda item: item["symbol"])
        total = len(rows)
        return {
            "market": "cn",
            "snapshot_date": snapshot_date.isoformat(),
            "total_count": total,
            "returned_count": min(total, limit),
            "truncated": total > limit,
            "items": rows[:limit],
            "methodology": {
                "source": "tushare.stock_basic",
                "membership_rule": "list_date <= snapshot_date <= delist_date_or_open_ended",
                "includes_delisted_symbols": True,
                "uses_current_universe_fallback": False,
                "point_in_time_name_and_industry": False,
                "permission_requirement": "Tushare stock_basic permission",
            },
        }

    def _load(self) -> Optional[pd.DataFrame]:
        if self.loader is not None:
            return self.loader()
        from data_provider.tushare_fetcher import TushareFetcher

        return TushareFetcher().get_stock_lifecycle_list()

    @staticmethod
    def _parse_date(value: Any) -> Optional[date]:
        if value is None or pd.isna(value):
            return None
        text = str(value).strip()
        if not text or text.upper() in {"NAT", "NONE", "NAN"}:
            return None
        for pattern in ("%Y%m%d", "%Y-%m-%d"):
            try:
                return datetime.strptime(text, pattern).date()
            except ValueError:
                continue
        return None

    @staticmethod
    def _text_or_none(value: Any) -> Optional[str]:
        if value is None or pd.isna(value):
            return None
        text = str(value).strip()
        return text or None
