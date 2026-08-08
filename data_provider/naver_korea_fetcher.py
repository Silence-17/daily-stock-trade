# -*- coding: utf-8 -*-
"""Credential-free Korean realtime quotes from Naver Finance polling APIs."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import pandas as pd
import requests

from .base import BaseFetcher, DataFetchError
from .realtime_types import RealtimeSource, UnifiedRealtimeQuote

logger = logging.getLogger(__name__)

BASE_URL = "https://polling.finance.naver.com/api/realtime/domestic"
SYMBOLS: Dict[str, tuple[str, str, str]] = {
    "KS11": ("index", "KOSPI", "KOSPI"),
    "KQ11": ("index", "KOSDAQ", "KOSDAQ"),
    "005930.KS": ("stock", "005930", "Samsung Electronics"),
    "000660.KS": ("stock", "000660", "SK Hynix"),
}
KOREA_EQUITY_CODE = re.compile(r"^([0-9]{6})\.(KS|KQ)$")


def _number(value: Any) -> Optional[float]:
    try:
        parsed = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed else None


def _integer(value: Any) -> Optional[int]:
    parsed = _number(value)
    return int(parsed) if parsed is not None else None


class NaverKoreaFetcher(BaseFetcher):
    """Fetch the four Korean signals required by the cross-market strategy."""

    name = "NaverKoreaFetcher"
    priority = 2

    def __init__(
        self,
        *,
        timeout_seconds: float = 5.0,
        session: Optional[requests.Session] = None,
    ) -> None:
        self._timeout_seconds = max(1.0, float(timeout_seconds))
        self._session = session or requests.Session()

    def is_available(self) -> bool:
        return True

    def is_available_for_request(self, capability: str = "") -> bool:
        return capability in {"", "realtime_quote"}

    def _fetch_raw_data(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        raise DataFetchError("[Naver] historical daily data is not supported")

    def _normalize_data(self, df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
        return pd.DataFrame() if df is None else df.copy()

    @staticmethod
    def _provider_timestamp(value: Any) -> Optional[str]:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc).isoformat()

    def get_realtime_quote(self, stock_code: str) -> Optional[UnifiedRealtimeQuote]:
        code = str(stock_code or "").strip().upper()
        symbol = SYMBOLS.get(code)
        if symbol is None:
            match = KOREA_EQUITY_CODE.fullmatch(code)
            if match is None:
                return None
            symbol = ("stock", match.group(1), code)
        kind, provider_code, display_name = symbol
        try:
            response = self._session.get(
                f"{BASE_URL}/{kind}/{provider_code}",
                headers={
                    "Accept": "application/json",
                    "Referer": "https://finance.naver.com/",
                    "User-Agent": (
                        "Mozilla/5.0 (compatible; DSA/1.0; "
                        "+https://github.com/ZhuLinsen/daily_stock_analysis)"
                    ),
                },
                timeout=self._timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning("[Naver] Korean quote request failed for %s: %s", code, exc)
            return None

        rows = payload.get("datas") if isinstance(payload, dict) else None
        row = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else None
        if row is None:
            return None
        price = _number(row.get("closePriceRaw") or row.get("closePrice"))
        change_pct = _number(row.get("fluctuationsRatioRaw") or row.get("fluctuationsRatio"))
        change_amount = _number(
            row.get("compareToPreviousClosePriceRaw")
            or row.get("compareToPreviousClosePrice")
        )
        provider_timestamp = self._provider_timestamp(row.get("localTradedAt"))
        if price is None or price <= 0 or change_pct is None or provider_timestamp is None:
            return None
        pre_close = price - change_amount if change_amount is not None else None
        return UnifiedRealtimeQuote(
            code=code,
            name=str(row.get("stockName") or display_name).strip(),
            source=RealtimeSource.NAVER,
            provider_timestamp=provider_timestamp,
            market="kr",
            currency="KRW",
            data_quality="ok",
            price=price,
            change_pct=change_pct,
            change_amount=change_amount,
            volume=_integer(row.get("accumulatedTradingVolumeRaw")),
            amount=_number(row.get("accumulatedTradingValueRaw")),
            open_price=_number(row.get("openPriceRaw") or row.get("openPrice")),
            high=_number(row.get("highPriceRaw") or row.get("highPrice")),
            low=_number(row.get("lowPriceRaw") or row.get("lowPrice")),
            pre_close=pre_close,
        )
