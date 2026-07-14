# -*- coding: utf-8 -*-
"""Direct Eastmoney board helpers adapted from simonlin1212/a-stock-data."""

import logging
import random
import time
from datetime import date, timedelta
from threading import RLock
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests

from .base import BaseFetcher, normalize_stock_code

logger = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

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
        return capability in {"belong_boards", "industry_boards", "sector_rankings"}

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

    @staticmethod
    def _iter_diff(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        diff = (payload.get("data") or {}).get("diff") or []
        if isinstance(diff, dict):
            diff = list(diff.values())
        if not isinstance(diff, list):
            return []
        return [item for item in diff if isinstance(item, dict)]

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
            "fields": "f12,f14,f3,f128",
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
                        "lead_stock": str(item.get("f128") or "").strip(),
                        "source": "a_stock_data_eastmoney",
                    }
                )
            return boards
        except Exception as exc:
            logger.warning("[AStockDataFetcher] 获取 %s 所属板块失败: %s", code, exc)
            return []
