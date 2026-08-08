# -*- coding: utf-8 -*-
"""Korea Investment & Securities (KIS) realtime quote adapter."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Any, Callable, Dict, Iterable, Optional
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from .base import BaseFetcher, DataFetchError
from .realtime_types import RealtimeSource, UnifiedRealtimeQuote

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://openapi.koreainvestment.com:9443"
SEOUL_TZ = ZoneInfo("Asia/Seoul")
INDEX_CODES = {
    "KS11": "0001",
    "KQ11": "1001",
}
STOCK_CODES = {
    "005930.KS": "005930",
    "000660.KS": "000660",
}
DISPLAY_NAMES = {
    "KS11": "KOSPI",
    "KQ11": "KOSDAQ",
    "005930.KS": "Samsung Electronics",
    "000660.KS": "SK Hynix",
}


def _number(value: Any) -> Optional[float]:
    try:
        parsed = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return parsed


def _integer(value: Any) -> Optional[int]:
    parsed = _number(value)
    return int(parsed) if parsed is not None else None


class KoreaInvestmentFetcher(BaseFetcher):
    """Fetch timestamped Korean index and stock quotes from the official KIS API."""

    name = "KoreaInvestmentFetcher"
    priority = 1

    def __init__(
        self,
        *,
        app_key: Optional[str] = None,
        app_secret: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        max_event_age_seconds: int = 120,
        session: Optional[requests.Session] = None,
        now_provider: Optional[Callable[[], datetime]] = None,
    ) -> None:
        from src.config import get_config

        config = get_config()
        self._app_key = str(app_key or getattr(config, "kis_app_key", "") or "").strip()
        self._app_secret = str(app_secret or getattr(config, "kis_app_secret", "") or "").strip()
        configured_url = base_url or getattr(config, "kis_base_url", None) or DEFAULT_BASE_URL
        self._base_url = str(configured_url).strip().rstrip("/")
        configured_timeout = timeout_seconds
        if configured_timeout is None:
            configured_timeout = getattr(config, "kis_timeout_seconds", 10.0)
        self._timeout_seconds = max(1.0, float(configured_timeout))
        self._max_event_age_seconds = max(1, int(max_event_age_seconds))
        self._session = session or requests.Session()
        self._now_provider = now_provider or (lambda: datetime.now(timezone.utc))
        self._token_lock = RLock()
        self._access_token: Optional[str] = None
        self._token_expires_at = datetime.min.replace(tzinfo=timezone.utc)

    def is_available(self) -> bool:
        return bool(self._app_key and self._app_secret and self._base_url)

    def is_available_for_request(self, capability: str = "") -> bool:
        return self.is_available() and capability in {"", "realtime_quote"}

    def _fetch_raw_data(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        raise DataFetchError("[KIS] historical daily data is not implemented by this realtime adapter")

    def _normalize_data(self, df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
        return pd.DataFrame() if df is None else df.copy()

    def _now(self) -> datetime:
        current = self._now_provider()
        if current.tzinfo is None:
            raise DataFetchError("[KIS] now_provider must return a timezone-aware datetime")
        return current.astimezone(timezone.utc)

    def _get_access_token(self) -> str:
        now = self._now()
        with self._token_lock:
            if self._access_token and now < self._token_expires_at - timedelta(minutes=5):
                return self._access_token
            response = self._session.post(
                f"{self._base_url}/oauth2/tokenP",
                json={
                    "grant_type": "client_credentials",
                    "appkey": self._app_key,
                    "appsecret": self._app_secret,
                },
                timeout=self._timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
            token = str(payload.get("access_token") or "").strip()
            if not token:
                raise DataFetchError("[KIS] access token missing from response")
            try:
                expires_in = max(600, int(payload.get("expires_in") or 86400))
            except (TypeError, ValueError):
                expires_in = 86400
            self._access_token = token
            self._token_expires_at = now + timedelta(seconds=expires_in)
            return token

    def _request(self, path: str, *, tr_id: str, params: Dict[str, str]) -> Optional[Dict[str, Any]]:
        if not self.is_available():
            return None
        try:
            response = self._session.get(
                f"{self._base_url}{path}",
                headers={
                    "authorization": f"Bearer {self._get_access_token()}",
                    "appkey": self._app_key,
                    "appsecret": self._app_secret,
                    "tr_id": tr_id,
                    "custtype": "P",
                },
                params=params,
                timeout=self._timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError, DataFetchError) as exc:
            logger.warning("[KIS] quote request failed: %s", exc)
            return None
        if str(payload.get("rt_cd") or "") != "0":
            logger.warning(
                "[KIS] quote rejected: code=%s message=%s",
                payload.get("msg_cd"),
                payload.get("msg1"),
            )
            return None
        return payload

    @staticmethod
    def _latest_row(rows: Iterable[Dict[str, Any]], time_field: str) -> Optional[Dict[str, Any]]:
        valid = [
            row
            for row in rows
            if isinstance(row, dict)
            and str(row.get(time_field) or "").strip().isdigit()
            and len(str(row.get(time_field) or "").strip()) == 6
        ]
        if not valid:
            return None
        return max(valid, key=lambda row: str(row[time_field]).strip())

    def _event_timestamp(self, hhmmss: Any) -> Optional[datetime]:
        text = str(hhmmss or "").strip()
        if len(text) != 6 or not text.isdigit():
            return None
        now = self._now()
        local_now = now.astimezone(SEOUL_TZ)
        try:
            event_local = datetime.combine(
                local_now.date(),
                datetime.strptime(text, "%H%M%S").time(),
                tzinfo=SEOUL_TZ,
            )
        except ValueError:
            return None
        event_at = event_local.astimezone(timezone.utc)
        age_seconds = (now - event_at).total_seconds()
        if age_seconds < -1 or age_seconds > self._max_event_age_seconds:
            return None
        return event_at

    def _index_quote(self, code: str) -> Optional[UnifiedRealtimeQuote]:
        payload = self._request(
            "/uapi/domestic-stock/v1/quotations/inquire-index-timeprice",
            tr_id="FHPUP02110200",
            params={
                "FID_INPUT_HOUR_1": "60",
                "FID_INPUT_ISCD": INDEX_CODES[code],
                "FID_COND_MRKT_DIV_CODE": "U",
            },
        )
        rows = payload.get("output") if payload else None
        if isinstance(rows, dict):
            rows = [rows]
        row = self._latest_row(rows or [], "bsop_hour")
        event_at = self._event_timestamp(row.get("bsop_hour")) if row else None
        price = _number(row.get("bstp_nmix_prpr")) if row else None
        change_pct = _number(row.get("bstp_nmix_prdy_ctrt")) if row else None
        if row is None or event_at is None or price is None or price <= 0 or change_pct is None:
            return None
        return UnifiedRealtimeQuote(
            code=code,
            name=DISPLAY_NAMES[code],
            source=RealtimeSource.KOREA_INVESTMENT,
            provider_timestamp=event_at.isoformat(),
            market="kr",
            currency="KRW",
            data_quality="ok",
            price=price,
            change_pct=change_pct,
            change_amount=_number(row.get("bstp_nmix_prdy_vrss")),
            volume=_integer(row.get("acml_vol")),
            amount=_number(row.get("acml_tr_pbmn")),
        )

    def _stock_quote(self, code: str) -> Optional[UnifiedRealtimeQuote]:
        local_now = self._now().astimezone(SEOUL_TZ)
        payload = self._request(
            "/uapi/domestic-stock/v1/quotations/inquire-time-itemconclusion",
            tr_id="FHPST01060000",
            params={
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": STOCK_CODES[code],
                "FID_INPUT_HOUR_1": local_now.strftime("%H%M%S"),
            },
        )
        rows = payload.get("output2") if payload else None
        if isinstance(rows, dict):
            rows = [rows]
        row = self._latest_row(rows or [], "stck_cntg_hour")
        event_at = self._event_timestamp(row.get("stck_cntg_hour")) if row else None
        price = _number(row.get("stck_prpr")) if row else None
        change_pct = _number(row.get("prdy_ctrt")) if row else None
        if row is None or event_at is None or price is None or price <= 0 or change_pct is None:
            return None
        pre_close = price / (1 + change_pct / 100) if change_pct > -100 else None
        return UnifiedRealtimeQuote(
            code=code,
            name=DISPLAY_NAMES[code],
            source=RealtimeSource.KOREA_INVESTMENT,
            provider_timestamp=event_at.isoformat(),
            market="kr",
            currency="KRW",
            data_quality="ok",
            price=price,
            change_pct=change_pct,
            change_amount=_number(row.get("prdy_vrss")),
            volume=_integer(row.get("acml_vol")),
            pre_close=pre_close,
        )

    def get_realtime_quote(self, stock_code: str) -> Optional[UnifiedRealtimeQuote]:
        code = str(stock_code or "").strip().upper()
        if code in INDEX_CODES:
            return self._index_quote(code)
        if code in STOCK_CODES:
            return self._stock_quote(code)
        return None
