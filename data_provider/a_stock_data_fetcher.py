# -*- coding: utf-8 -*-
"""Direct Eastmoney board helpers adapted from simonlin1212/a-stock-data."""

import logging
import math
import random
import time
from datetime import date, datetime, timedelta, timezone
from threading import RLock
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests

from .base import (
    BaseFetcher,
    is_bse_code,
    is_kc_cy_stock,
    is_st_stock,
    normalize_stock_code,
)

logger = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

_EASTMONEY_REALTIME_FIELDS = "f2,f3,f4,f5,f6,f12,f14,f15,f16,f17,f18,f124,f297"
_CN_INDEX_MAP = {
    "000001": ("上证指数", "sh000001"),
    "399001": ("深证成指", "sz399001"),
    "399006": ("创业板指", "sz399006"),
    "000688": ("科创50", "sh000688"),
    "000016": ("上证50", "sh000016"),
    "000300": ("沪深300", "sh000300"),
}
_CN_STOCK_FS = "m:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23,m:0 t:81 s:2048"
_CN_INDEX_FS = "m:1 s:2,m:0 t:5"

_OFFLINE_INDUSTRY_BOARD_NAMES = (
    "农林牧渔",
    "基础化工",
    "钢铁",
    "有色金属",
    "电子",
    "家用电器",
    "食品饮料",
    "纺织服饰",
    "轻工制造",
    "医药生物",
    "公用事业",
    "交通运输",
    "房地产",
    "商贸零售",
    "社会服务",
    "综合",
    "建筑材料",
    "建筑装饰",
    "电力设备",
    "国防军工",
    "计算机",
    "传媒",
    "通信",
    "银行",
    "非银金融",
    "汽车",
    "机械设备",
    "煤炭",
    "石油石化",
    "环保",
    "美容护理",
)


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value in (None, "", "-"):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> Optional[int]:
    try:
        if value in (None, "", "-"):
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _eastmoney_provider_timestamp(value: Any) -> Optional[str]:
    timestamp = _safe_float(value)
    if timestamp is None or timestamp <= 0:
        return None
    try:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _eastmoney_data_date(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    if not text or text in {"-", "--", "0"}:
        return None
    try:
        if len(text) == 8 and text.isdigit():
            return datetime.strptime(text, "%Y%m%d").date().isoformat()
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return None


class AStockDataFetcher(BaseFetcher):
    """a-stock-data-inspired Eastmoney direct source for A-share board data."""

    name = "AStockDataFetcher"
    priority = 2

    def __init__(
        self,
        session: Optional[requests.Session] = None,
        min_interval_seconds: float = 1.0,
    ) -> None:
        self._session = session or requests.Session()
        if hasattr(self._session, "headers"):
            self._session.headers.update({"User-Agent": _UA})
        self._min_interval_seconds = max(0.0, float(min_interval_seconds))
        self._last_call = 0.0
        self._rate_lock = RLock()

    def is_available_for_request(self, capability: str = "") -> bool:
        return capability in {
            "belong_boards",
            "industry_boards",
            "sector_rankings",
            "main_indices",
            "market_stats",
        }

    def _fetch_raw_data(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        return pd.DataFrame()

    def _normalize_data(self, df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
        return pd.DataFrame()

    def _eastmoney_get(
        self,
        url: str,
        params: Dict[str, Any],
        timeout: int = 15,
        referer: str = "https://quote.eastmoney.com/",
    ):
        with self._rate_lock:
            wait = self._min_interval_seconds - (time.time() - self._last_call)
            if wait > 0:
                time.sleep(wait + random.uniform(0.1, 0.5))
            try:
                response = self._session.get(
                    url,
                    params=params,
                    headers={"User-Agent": _UA, "Referer": referer},
                    timeout=timeout,
                )
                response.raise_for_status()
                return response
            finally:
                self._last_call = time.time()

    def _get_realtime_rows(
        self,
        *,
        fs: str,
        page_size: int,
    ) -> List[Dict[str, Any]]:
        params = {
            "pn": "1",
            "pz": str(page_size),
            "po": "1",
            "np": "1",
            "fltt": "2",
            "invt": "2",
            "fid": "f12",
            "fs": fs,
            "fields": _EASTMONEY_REALTIME_FIELDS,
        }
        response = self._eastmoney_get(
            "https://push2.eastmoney.com/api/qt/clist/get",
            params=params,
            timeout=15,
        )
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        rows = self._iter_diff(payload if isinstance(payload, dict) else {})
        total = _safe_int(data.get("total")) if isinstance(data, dict) else None
        if total is None or total < 0:
            raise ValueError("EastMoney realtime response has no valid total")
        if len(rows) != total:
            raise ValueError(
                "EastMoney realtime response is incomplete: "
                f"received={len(rows)} total={total}"
            )
        return rows

    @staticmethod
    def _iter_diff(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        diff = (payload.get("data") or {}).get("diff") or []
        if isinstance(diff, dict):
            diff = list(diff.values())
        if not isinstance(diff, list):
            return []
        return [item for item in diff if isinstance(item, dict)]

    def get_main_indices(self, region: str = "cn") -> Optional[List[Dict[str, Any]]]:
        """Fetch A-share indices with EastMoney's provider quote time."""
        if region != "cn":
            return None
        try:
            rows = self._get_realtime_rows(fs=_CN_INDEX_FS, page_size=1000)
            rows_by_code = {
                str(row.get("f12") or "").strip().zfill(6): row
                for row in rows
            }
            indices: List[Dict[str, Any]] = []
            for code, (name, symbol) in _CN_INDEX_MAP.items():
                row = rows_by_code.get(code)
                if row is None:
                    continue
                current = _safe_float(row.get("f2"))
                change = _safe_float(row.get("f4"))
                indices.append(
                    {
                        "code": symbol,
                        "name": name,
                        "current": current,
                        "change": change,
                        "change_pct": _safe_float(row.get("f3")),
                        "open": _safe_float(row.get("f17")),
                        "high": _safe_float(row.get("f15")),
                        "low": _safe_float(row.get("f16")),
                        "prev_close": _safe_float(row.get("f18")),
                        "volume": _safe_float(row.get("f5")),
                        "amount": _safe_float(row.get("f6")),
                        "amplitude": None,
                        "provider_timestamp": _eastmoney_provider_timestamp(row.get("f124")),
                        "data_date": _eastmoney_data_date(row.get("f297")),
                        "data_granularity": "realtime",
                    }
                )
            return indices or None
        except Exception as exc:
            logger.warning("[AStockDataFetcher] 获取实时指数失败: %s", exc)
            return None

    def get_market_stats(self) -> Optional[Dict[str, Any]]:
        """Calculate A-share breadth only from a complete EastMoney response."""
        try:
            rows = self._get_realtime_rows(fs=_CN_STOCK_FS, page_size=10000)
        except Exception as exc:
            logger.warning("[AStockDataFetcher] 获取实时市场宽度失败: %s", exc)
            return None

        stats = {
            "up_count": 0,
            "down_count": 0,
            "flat_count": 0,
            "limit_up_count": 0,
            "limit_down_count": 0,
            "total_amount": 0.0,
        }
        valid_rows = 0
        provider_timestamps: List[str] = []
        data_dates: List[str] = []

        for row in rows:
            code = normalize_stock_code(str(row.get("f12") or ""))
            name = str(row.get("f14") or "")
            current = _safe_float(row.get("f2"))
            prev_close = _safe_float(row.get("f18"))
            amount = _safe_float(row.get("f6"))
            if not code or current is None or prev_close is None or amount is None:
                continue
            if current <= 0 or prev_close <= 0 or amount <= 0:
                continue

            if is_bse_code(code):
                ratio = 0.30
            elif is_kc_cy_stock(code):
                ratio = 0.20
            elif is_st_stock(name):
                ratio = 0.05
            else:
                ratio = 0.10

            limit_up = math.floor(prev_close * (1 + ratio) * 100 + 0.5) / 100.0
            limit_down = math.floor(prev_close * (1 - ratio) * 100 + 0.5) / 100.0
            limit_up_tolerance = round(abs(prev_close * (1 + ratio) - limit_up), 10)
            limit_down_tolerance = round(
                abs(prev_close * (1 - ratio) - limit_down),
                10,
            )
            valid_rows += 1
            stats["total_amount"] += amount / 1e8
            provider_timestamp = _eastmoney_provider_timestamp(row.get("f124"))
            if provider_timestamp:
                provider_timestamps.append(provider_timestamp)
            data_date = _eastmoney_data_date(row.get("f297"))
            if data_date:
                data_dates.append(data_date)

            if abs(current - limit_up) <= limit_up_tolerance:
                stats["limit_up_count"] += 1
            if abs(current - limit_down) <= limit_down_tolerance:
                stats["limit_down_count"] += 1
            if current > prev_close:
                stats["up_count"] += 1
            elif current < prev_close:
                stats["down_count"] += 1
            else:
                stats["flat_count"] += 1

        if valid_rows == 0:
            logger.warning("[AStockDataFetcher] 实时市场宽度没有有效 A 股行")
            return None

        stats["total_amount"] = round(stats["total_amount"], 6)
        stats["provider_timestamp"] = (
            min(provider_timestamps)
            if len(provider_timestamps) == valid_rows
            else None
        )
        stats["provider_timestamp_coverage_pct"] = round(
            len(provider_timestamps) / valid_rows * 100,
            6,
        )
        stats["data_date"] = (
            data_dates[0]
            if len(data_dates) == valid_rows and len(set(data_dates)) == 1
            else None
        )
        stats["data_granularity"] = "realtime"
        return stats

    def get_industry_boards(self) -> List[Dict[str, Any]]:
        """Return all Eastmoney industry boards with ranking fields."""
        boards = self._get_realtime_industry_boards()
        if boards:
            return boards
        boards = self._get_reportapi_industry_boards()
        if boards:
            return boards
        return self._get_offline_industry_boards()

    def _get_realtime_industry_boards(self) -> List[Dict[str, Any]]:
        """Return Eastmoney realtime industry boards when push2 is reachable."""
        params = {
            "pn": "1",
            "pz": "100",
            "po": "1",
            "np": "1",
            "fltt": "2",
            "invt": "2",
            "fs": "m:90+t:2",
            "fields": "f2,f3,f4,f12,f13,f14,f104,f105,f128,f136,f140,f141,f207",
        }
        try:
            response = self._eastmoney_get(
                "https://push2.eastmoney.com/api/qt/clist/get",
                params=params,
                timeout=15,
            )
            rows = []
            for index, item in enumerate(self._iter_diff(response.json()), start=1):
                name = str(item.get("f14") or "").strip()
                if not name:
                    continue
                rows.append(
                    {
                        "rank": index,
                        "code": str(item.get("f12") or "").strip(),
                        "name": name,
                        "change_pct": _safe_float(item.get("f3")),
                        "up_count": _safe_int(item.get("f104")),
                        "down_count": _safe_int(item.get("f105")),
                        "leader": str(item.get("f140") or "").strip(),
                        "leader_change": _safe_float(item.get("f136")),
                        "source": "a_stock_data_eastmoney",
                        "data_quality": "realtime",
                    }
                )
            if not rows:
                logger.warning("[AStockDataFetcher] EastMoney 实时行业板块返回空列表，尝试备用目录")
            return rows
        except Exception as exc:
            logger.warning("[AStockDataFetcher] 获取行业板块列表失败: %s", exc)
            return []

    def _get_reportapi_industry_boards(self) -> List[Dict[str, Any]]:
        """Use Eastmoney report industry metadata as an online directory fallback."""
        today = date.today()
        params = {
            "qType": "1",
            "pageNo": "1",
            "pageSize": "100",
            "beginTime": (today - timedelta(days=365)).isoformat(),
            "endTime": today.isoformat(),
        }
        try:
            response = self._eastmoney_get(
                "https://reportapi.eastmoney.com/report/list",
                params=params,
                timeout=15,
                referer="https://data.eastmoney.com/",
            )
            payload = response.json()
            data = payload.get("data") or []
            if not isinstance(data, list):
                return []
            rows: List[Dict[str, Any]] = []
            seen = set()
            for item in data:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("industryName") or item.get("indvInduName") or "").strip()
                if not name or name in seen:
                    continue
                seen.add(name)
                code = str(
                    item.get("emIndustryCode")
                    or item.get("industryCode")
                    or item.get("indvInduCode")
                    or ""
                ).strip()
                rows.append(
                    {
                        "rank": len(rows) + 1,
                        "code": code,
                        "name": name,
                        "source": "a_stock_data_eastmoney_reportapi_fallback",
                        "data_quality": "directory_fallback",
                    }
                )
            if rows:
                logger.info(
                    "[AStockDataFetcher] 使用 EastMoney reportapi 行业目录兜底: count=%s",
                    len(rows),
                )
            return rows
        except Exception as exc:
            logger.warning("[AStockDataFetcher] 获取 reportapi 行业目录失败: %s", exc)
            return []

    def _get_offline_industry_boards(self) -> List[Dict[str, Any]]:
        """Return a stable A-share industry directory when all online sources fail."""
        return [
            {
                "rank": index,
                "code": "",
                "name": name,
                "source": "a_stock_data_offline_industry_seed",
                "data_quality": "offline_seed",
            }
            for index, name in enumerate(_OFFLINE_INDUSTRY_BOARD_NAMES, start=1)
        ]

    def get_sector_rankings(self, n: int = 5) -> Optional[Tuple[List[Dict], List[Dict]]]:
        boards = self.get_industry_boards()
        if not boards:
            return None
        ranked = [
            row
            for row in boards
            if isinstance(row.get("change_pct"), (int, float))
        ]
        if not ranked:
            return None
        top = sorted(ranked, key=lambda row: row["change_pct"], reverse=True)[:n]
        bottom = sorted(ranked, key=lambda row: row["change_pct"])[:n]
        return top, bottom

    def get_belong_board(self, stock_code: str) -> List[Dict[str, Any]]:
        """Return mixed industry/concept/region boards for one A-share stock."""
        code = normalize_stock_code(stock_code)
        if not (code.isdigit() and len(code) == 6):
            return []
        market_code = 1 if code.startswith("6") else 0
        params = {
            "fltt": "2",
            "invt": "2",
            "secid": f"{market_code}.{code}",
            "spt": "3",
            "pi": "0",
            "pz": "200",
            "po": "1",
            "fields": "f12,f14,f3,f10,f128",
        }
        try:
            response = self._eastmoney_get(
                "https://push2.eastmoney.com/api/qt/slist/get",
                params=params,
                timeout=15,
            )
            boards = []
            for item in self._iter_diff(response.json()):
                name = str(item.get("f14") or "").strip()
                if not name:
                    continue
                boards.append(
                    {
                        "name": name,
                        "code": str(item.get("f12") or "").strip(),
                        "change_pct": _safe_float(item.get("f3")),
                        "volume_ratio": _safe_float(item.get("f10")),
                        "lead_stock": str(item.get("f128") or "").strip(),
                        "source": "a_stock_data_eastmoney",
                    }
                )
            return boards
        except Exception as exc:
            logger.warning("[AStockDataFetcher] 获取 %s 所属板块失败: %s", code, exc)
            return []
