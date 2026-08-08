# -*- coding: utf-8 -*-
"""Realtime and daily global futures quotes without credential requirements."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from .base import BaseFetcher, DataFetchError, STANDARD_COLUMNS
from .realtime_types import RealtimeSource, UnifiedRealtimeQuote

logger = logging.getLogger(__name__)

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
CONTRACT_SPECS = {
    "GC1": {
        "aliases": {"GC1", "GC=F"},
        "symbol": "GC",
        "name": "COMEX Gold Continuous",
    },
    "NQ00Y": {
        "aliases": {"NQ00Y", "NQ1", "NQ=F"},
        "symbol": "NQ",
        "name": "E-mini Nasdaq-100 Continuous",
    },
}
SUPPORTED_CODES = {
    alias
    for spec in CONTRACT_SPECS.values()
    for alias in spec["aliases"]
}


def _number(value: Any) -> Optional[float]:
    try:
        parsed = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed else None


class GlobalFuturesFetcher(BaseFetcher):
    """Fetch continuous contracts required by the cross-market strategy."""

    name = "GlobalFuturesFetcher"
    priority = 2

    def __init__(
        self,
        *,
        timeout_seconds: float = 8.0,
        session: Optional[requests.Session] = None,
    ) -> None:
        self._timeout_seconds = max(1.0, float(timeout_seconds))
        self._session = session or requests.Session()

    def is_available(self) -> bool:
        return True

    def is_available_for_request(self, capability: str = "") -> bool:
        return capability in {"", "realtime_quote", "daily_data"}

    @staticmethod
    def _normalize_code(stock_code: str) -> Optional[str]:
        code = str(stock_code or "").strip().upper()
        for canonical_code, spec in CONTRACT_SPECS.items():
            if code in spec["aliases"]:
                return canonical_code
        return None

    @staticmethod
    def _contract_spec(stock_code: str) -> Optional[Dict[str, Any]]:
        canonical_code = GlobalFuturesFetcher._normalize_code(stock_code)
        if canonical_code is None:
            return None
        return {"code": canonical_code, **CONTRACT_SPECS[canonical_code]}

    def _request_bytes(self, url: str, *, referer: str) -> Optional[bytes]:
        try:
            response = self._session.get(
                url,
                headers={
                    "Accept": "*/*",
                    "Referer": referer,
                    "User-Agent": (
                        "Mozilla/5.0 (compatible; DSA/1.0; "
                        "+https://github.com/ZhuLinsen/daily_stock_analysis)"
                    ),
                },
                timeout=self._timeout_seconds,
            )
            response.raise_for_status()
            return response.content
        except requests.RequestException as exc:
            logger.warning("[GlobalFutures] request failed for %s: %s", url, exc)
            return None

    @staticmethod
    def _parse_realtime_payload(
        payload: bytes,
        *,
        source: RealtimeSource,
        code: str,
        fallback_name: str,
    ) -> Optional[UnifiedRealtimeQuote]:
        text = payload.decode("gbk", "ignore").strip()
        try:
            fields = text[text.index('"') + 1:text.rindex('"')].split(",")
        except ValueError:
            return None
        if len(fields) < 14:
            return None
        price = _number(fields[0])
        previous_close = _number(fields[7])
        event_date = fields[12].strip()
        event_time = fields[6].strip()
        try:
            provider_at = datetime.strptime(
                f"{event_date} {event_time}",
                "%Y-%m-%d %H:%M:%S",
            ).replace(tzinfo=SHANGHAI_TZ).astimezone(timezone.utc)
        except ValueError:
            return None
        if price is None or price <= 0 or previous_close is None or previous_close <= 0:
            return None
        change_amount = price - previous_close
        change_pct = change_amount / previous_close * 100.0
        return UnifiedRealtimeQuote(
            code=code,
            name=fields[13].strip() or fallback_name,
            source=source,
            provider_timestamp=provider_at.isoformat(),
            market="us",
            currency="USD",
            data_quality="partial",
            missing_fields=["volume", "amount"],
            price=price,
            change_pct=round(change_pct, 6),
            change_amount=round(change_amount, 6),
            open_price=_number(fields[8]),
            high=_number(fields[4]),
            low=_number(fields[5]),
            pre_close=previous_close,
        )

    def get_realtime_quote(self, stock_code: str) -> Optional[UnifiedRealtimeQuote]:
        spec = self._contract_spec(stock_code)
        if spec is None:
            return None
        symbol = str(spec["symbol"])
        routes = (
            (
                f"https://qt.gtimg.cn/q=hf_{symbol}",
                "https://finance.qq.com",
                RealtimeSource.TENCENT,
            ),
            (
                f"https://hq.sinajs.cn/list=hf_{symbol}",
                "https://finance.sina.com.cn",
                RealtimeSource.SINA,
            ),
        )
        for url, referer, source in routes:
            payload = self._request_bytes(url, referer=referer)
            if payload:
                quote = self._parse_realtime_payload(
                    payload,
                    source=source,
                    code=str(spec["code"]),
                    fallback_name=str(spec["name"]),
                )
                if quote is not None:
                    return quote
        return None

    def _fetch_raw_data(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        spec = self._contract_spec(stock_code)
        if spec is None:
            return pd.DataFrame()
        symbol = str(spec["symbol"])
        payload = self._request_bytes(
            (
                "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/"
                f"var%20_{symbol}=/GlobalFuturesService."
                f"getGlobalFuturesDailyKLine?symbol={symbol}"
            ),
            referer="https://finance.sina.com.cn",
        )
        if not payload:
            return pd.DataFrame()
        text = payload.decode("utf-8", "ignore")
        try:
            rows = json.loads(text[text.index("(") + 1:text.rindex(")")])
        except (ValueError, json.JSONDecodeError) as exc:
            raise DataFetchError("[GlobalFutures] invalid Sina daily response") from exc
        frame = pd.DataFrame(rows if isinstance(rows, list) else [])
        if frame.empty or "date" not in frame.columns:
            return pd.DataFrame()
        dates = pd.to_datetime(frame["date"], errors="coerce")
        start = pd.Timestamp(start_date)
        end = pd.Timestamp(end_date)
        return frame.loc[(dates >= start) & (dates <= end)].copy()

    def _normalize_data(self, df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
        normalized = df.copy()
        for column in ("open", "high", "low", "close", "volume"):
            normalized[column] = pd.to_numeric(normalized.get(column), errors="coerce")
        normalized["amount"] = 0.0
        normalized["pct_chg"] = normalized["close"].pct_change(fill_method=None) * 100.0
        return normalized[STANDARD_COLUMNS]
