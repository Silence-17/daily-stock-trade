# -*- coding: utf-8 -*-
"""Resolve an A-share universe from listing lifecycle metadata."""

from __future__ import annotations

from datetime import date, datetime
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Optional

import pandas as pd


class StockSelectionHistoricalUniverseService:
    """Build a survivor-bias-aware universe for a requested date."""

    CACHE_TTL_SECONDS = 24 * 60 * 60
    _cache_lock = threading.Lock()
    _cached_frame: Optional[pd.DataFrame] = None
    _cached_at: float = 0.0

    def __init__(self, loader: Optional[Callable[[], Optional[pd.DataFrame]]] = None) -> None:
        self.loader = loader
        self._last_load_error: Optional[str] = None

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
            message = (
                "A-share stock lifecycle metadata is unavailable from both "
                "Tushare stock_basic and the BaoStock+BSE official fallback"
            )
            if self._last_load_error:
                message += f": {self._last_load_error}"
            raise RuntimeError(message)
        required = {"code", "name", "list_date", "delist_date"}
        missing = sorted(required - set(frame.columns))
        if missing:
            raise RuntimeError(f"Stock lifecycle metadata is missing fields: {', '.join(missing)}")

        records = frame.to_dict(orient="records")
        methodology = self._methodology(frame)
        return [
            self._resolve_records(
                records=records,
                snapshot_date=item,
                limit=limit,
                methodology=methodology,
            )
            for item in dates
        ]

    def _resolve_records(
        self,
        *,
        records: List[Dict[str, Any]],
        snapshot_date: date,
        limit: int,
        methodology: Dict[str, Any],
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
                **methodology,
                "membership_rule": "list_date <= snapshot_date <= delist_date_or_open_ended",
            },
        }

    def _load(self) -> Optional[pd.DataFrame]:
        if self.loader is not None:
            return self.loader()
        now = time.monotonic()
        cls = type(self)
        with cls._cache_lock:
            if (
                cls._cached_frame is not None
                and now - cls._cached_at < self.CACHE_TTL_SECONDS
            ):
                return cls._cached_frame.copy()
            frame = self._load_default()
            if frame is not None and not frame.empty:
                cls._cached_frame = frame.copy()
                cls._cached_at = time.monotonic()
                return frame
        return None

    def _load_default(self) -> Optional[pd.DataFrame]:
        errors = []
        try:
            from data_provider.tushare_fetcher import TushareFetcher

            frame = TushareFetcher().get_stock_lifecycle_list()
            if frame is not None and not frame.empty:
                return frame
            errors.append("tushare.stock_basic returned no lifecycle rows")
        except Exception as exc:
            errors.append(f"tushare.stock_basic failed: {exc}")

        try:
            from data_provider.baostock_fetcher import BaostockFetcher
            from data_provider.bse_lifecycle_fetcher import BseLifecycleFetcher

            mainland = BaostockFetcher().get_stock_lifecycle_list()
            if mainland is None or mainland.empty:
                raise RuntimeError("BaoStock returned no Shanghai/Shenzhen lifecycle rows")
            bse = BseLifecycleFetcher().get_stock_lifecycle_list()
            if bse is None or bse.empty:
                raise RuntimeError("BSE official sources returned no lifecycle rows")

            frame = pd.concat([mainland, bse], ignore_index=True)
            frame = frame.drop_duplicates(subset=["code"], keep="last")
            frame.attrs["lifecycle_methodology"] = {
                "source": "baostock+bse.official_lifecycle",
                "sources": [
                    "baostock.query_stock_basic",
                    "bse.nqxxCnzq",
                    "bse.code_mapping",
                    "bse.companyAnnouncement",
                ],
                "coverage_exchanges": ["SSE", "SZSE", "BSE"],
                "includes_delisted_symbols": True,
                "uses_current_universe_fallback": False,
                "point_in_time_name_and_industry": False,
                "permission_requirement": None,
                "delist_date_semantics": (
                    "provider-reported last active date for SSE/SZSE; "
                    "final delist notice publish date for BSE"
                ),
                "source_details": {
                    "mainland_count": len(mainland),
                    "bse_count": len(bse),
                    "bse_coverage_validation": (
                        bse.attrs.get("lifecycle_methodology", {})
                        .get("coverage_validation", {})
                    ),
                },
            }
            return frame
        except Exception as exc:
            errors.append(f"baostock+bse.official_lifecycle failed: {exc}")
        self._last_load_error = "; ".join(errors)
        return None

    def _methodology(self, frame: pd.DataFrame) -> Dict[str, Any]:
        metadata = dict(frame.attrs.get("lifecycle_methodology") or {})
        if not metadata:
            metadata = {
                "source": "custom.lifecycle_loader" if self.loader else "unknown",
                "sources": ["custom.lifecycle_loader"] if self.loader else ["unknown"],
                "coverage_exchanges": None,
                "includes_delisted_symbols": True,
                "uses_current_universe_fallback": False,
                "point_in_time_name_and_industry": False,
                "permission_requirement": None,
            }
        return metadata

    @classmethod
    def clear_cache(cls) -> None:
        with cls._cache_lock:
            cls._cached_frame = None
            cls._cached_at = 0.0

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
