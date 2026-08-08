# -*- coding: utf-8 -*-
"""Fresh Japanese index quotes for strict cross-market trading gates."""

from __future__ import annotations

import json
import logging
from datetime import datetime, time as datetime_time, timedelta, timezone
from typing import Any, Callable, Dict, Optional
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from .base import BaseFetcher, STANDARD_COLUMNS
from .realtime_types import RealtimeSource, UnifiedRealtimeQuote
from src.core import trading_calendar

logger = logging.getLogger(__name__)

TOKYO_TZ = ZoneInfo("Asia/Tokyo")
EASTMONEY_N225_URL = "https://push2.eastmoney.com/api/qt/clist/get"
SINA_NIKKEI_FUTURES_URL = "https://hq.sinajs.cn/list=hf_NK"
YAHOO_JAPAN_TOPIX_URL = "https://finance.yahoo.co.jp/quote/998405.T"
YAHOO_JAPAN_INDEX_MARKER = '"mainDomesticIndexPriceBoard":{"indexPrices":'


def _number(value: Any) -> Optional[float]:
    try:
        parsed = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed else None


class JapanIndexFetcher(BaseFetcher):
    """Fetch Nikkei 225 and TOPIX with provider-side timestamps."""

    name = "JapanIndexFetcher"
    priority = 2

    def __init__(
        self,
        *,
        timeout_seconds: float = 8.0,
        session: Optional[requests.Session] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._timeout_seconds = max(1.0, float(timeout_seconds))
        self._session = session or requests.Session()
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def is_available(self) -> bool:
        return True

    def is_available_for_request(self, capability: str = "") -> bool:
        return capability in {"", "realtime_quote"}

    def get_realtime_quote(self, stock_code: str) -> Optional[UnifiedRealtimeQuote]:
        code = str(stock_code or "").strip().upper()
        if code == "N225":
            return self._get_nikkei_quote() or self._get_nikkei_futures_proxy()
        if code == "TOPX":
            return self._get_topix_quote()
        return None

    def _get_nikkei_quote(self) -> Optional[UnifiedRealtimeQuote]:
        try:
            response = self._session.get(
                EASTMONEY_N225_URL,
                params={
                    "pn": "1",
                    "pz": "100",
                    "po": "1",
                    "np": "1",
                    "fltt": "2",
                    "invt": "2",
                    "fid": "f3",
                    "fs": "m:100",
                    "fields": "f2,f3,f4,f12,f13,f14,f15,f16,f17,f18,f124",
                },
                headers=self._headers(referer="https://quote.eastmoney.com/"),
                timeout=min(self._timeout_seconds, 3.0),
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError, TypeError, json.JSONDecodeError) as exc:
            logger.warning("[JapanIndex] Nikkei quote request failed: %s", exc)
            return None

        data = payload.get("data") if isinstance(payload, dict) else None
        rows = data.get("diff") if isinstance(data, dict) else None
        row = next(
            (
                item
                for item in rows
                if isinstance(item, dict)
                and str(item.get("f12") or "").upper() == "N225"
            ),
            None,
        ) if isinstance(rows, list) else None
        if not isinstance(row, dict):
            return None
        price = _number(row.get("f2"))
        previous_close = _number(row.get("f18"))
        timestamp = _number(row.get("f124"))
        if price is None or price <= 0 or timestamp is None or timestamp <= 0:
            return None
        provider_at = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        change_amount = _number(row.get("f4"))
        if change_amount is None and previous_close is not None:
            change_amount = price - previous_close
        change_pct = _number(row.get("f3"))
        if change_pct is None and change_amount is not None and previous_close and previous_close > 0:
            change_pct = change_amount / previous_close * 100.0
        if change_pct is None:
            return None
        return UnifiedRealtimeQuote(
            code="N225",
            name=str(row.get("f14") or "Nikkei 225"),
            source=RealtimeSource.EASTMONEY_GLOBAL,
            provider_timestamp=provider_at.isoformat(),
            market="jp",
            currency="JPY",
            data_quality="partial",
            missing_fields=["volume", "amount", "bid_price", "ask_price"],
            price=price,
            change_pct=round(change_pct, 6),
            change_amount=round(change_amount, 6) if change_amount is not None else None,
            open_price=_number(row.get("f17")),
            high=_number(row.get("f15")),
            low=_number(row.get("f16")),
            pre_close=previous_close,
        )

    def _get_nikkei_futures_proxy(self) -> Optional[UnifiedRealtimeQuote]:
        try:
            response = self._session.get(
                SINA_NIKKEI_FUTURES_URL,
                headers=self._headers(referer="https://finance.sina.com.cn/"),
                timeout=self._timeout_seconds,
            )
            response.raise_for_status()
            text = response.content.decode("gbk", "ignore").strip()
            fields = text[text.index('"') + 1:text.rindex('"')].split(",")
        except (requests.RequestException, ValueError, TypeError) as exc:
            logger.warning("[JapanIndex] Nikkei futures proxy request failed: %s", exc)
            return None
        if len(fields) < 14:
            return None
        price = _number(fields[0])
        previous_close = _number(fields[7])
        try:
            provider_at = datetime.strptime(
                f"{fields[12].strip()} {fields[6].strip()}",
                "%Y-%m-%d %H:%M:%S",
            ).replace(tzinfo=ZoneInfo("Asia/Shanghai")).astimezone(timezone.utc)
        except ValueError:
            return None
        if price is None or price <= 0 or previous_close is None or previous_close <= 0:
            return None
        change_amount = price - previous_close
        quote = UnifiedRealtimeQuote(
            code="N225",
            name="Nikkei 225 Futures Proxy",
            source=RealtimeSource.SINA,
            provider_timestamp=provider_at.isoformat(),
            fallback_from="eastmoney_global",
            market="jp",
            currency="JPY",
            data_quality="partial",
            missing_fields=["cash_index_price", "amount", "bid_price", "ask_price"],
            price=price,
            change_pct=round(change_amount / previous_close * 100.0, 6),
            change_amount=round(change_amount, 6),
            open_price=_number(fields[8]),
            high=_number(fields[4]),
            low=_number(fields[5]),
            pre_close=previous_close,
        )
        setattr(quote, "proxy_instrument", "nikkei_225_futures")
        return quote

    def _get_topix_quote(self) -> Optional[UnifiedRealtimeQuote]:
        if not self._is_current_japan_session_day():
            return None
        try:
            response = self._session.get(
                YAHOO_JAPAN_TOPIX_URL,
                headers=self._headers(referer="https://finance.yahoo.co.jp/"),
                timeout=self._timeout_seconds,
            )
            response.raise_for_status()
            payload = self._extract_yahoo_japan_index_payload(response.text)
        except (requests.RequestException, ValueError, TypeError, json.JSONDecodeError) as exc:
            logger.warning("[JapanIndex] TOPIX quote request failed: %s", exc)
            return None
        if payload is None or str(payload.get("code") or "").upper() != "998405.T":
            return None
        delay_minutes = _number(payload.get("delayMinutes"))
        if delay_minutes is None or delay_minutes != 0 or payload.get("isDelayed") is True:
            return None
        price = _number(payload.get("price"))
        change_amount = _number(payload.get("changePrice"))
        change_pct = _number(payload.get("changePriceRate"))
        provider_at = self._parse_tokyo_update_time(payload.get("japanUpdateTime"))
        if price is None or price <= 0 or change_pct is None or provider_at is None:
            return None
        previous_close = price - change_amount if change_amount is not None else None
        return UnifiedRealtimeQuote(
            code="TOPX",
            name=str(payload.get("name") or "TOPIX"),
            source=RealtimeSource.YAHOO_JAPAN,
            provider_timestamp=provider_at.astimezone(timezone.utc).isoformat(),
            market="jp",
            currency="JPY",
            data_quality="partial",
            missing_fields=[
                "volume",
                "amount",
                "open_price",
                "high",
                "low",
                "bid_price",
                "ask_price",
            ],
            price=price,
            change_pct=round(change_pct, 6),
            change_amount=round(change_amount, 6) if change_amount is not None else None,
            pre_close=round(previous_close, 6) if previous_close is not None else None,
        )

    @staticmethod
    def _extract_yahoo_japan_index_payload(text: str) -> Optional[Dict[str, Any]]:
        marker_at = str(text or "").find(YAHOO_JAPAN_INDEX_MARKER)
        if marker_at < 0:
            return None
        object_at = marker_at + len(YAHOO_JAPAN_INDEX_MARKER)
        payload, _ = json.JSONDecoder().raw_decode(text[object_at:])
        return payload if isinstance(payload, dict) else None

    def _parse_tokyo_update_time(self, value: Any) -> Optional[datetime]:
        text = str(value or "").strip()
        parsed_time: Optional[datetime_time] = None
        for pattern in ("%H:%M:%S", "%H:%M"):
            try:
                parsed_time = datetime.strptime(text, pattern).time()
                break
            except ValueError:
                continue
        if parsed_time is None:
            return None
        now = self._clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        tokyo_now = now.astimezone(TOKYO_TZ)
        provider_at = datetime.combine(tokyo_now.date(), parsed_time, tzinfo=TOKYO_TZ)
        if provider_at > tokyo_now + timedelta(seconds=60):
            provider_at -= timedelta(days=1)
        if provider_at > tokyo_now + timedelta(seconds=1):
            return None
        return provider_at

    def _is_current_japan_session_day(self) -> bool:
        now = self._clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return trading_calendar.is_market_open(
            "jp",
            now.astimezone(TOKYO_TZ).date(),
        )

    @staticmethod
    def _headers(*, referer: str) -> Dict[str, str]:
        return {
            "Accept": "application/json,text/html,*/*",
            "Referer": referer,
            "User-Agent": (
                "Mozilla/5.0 (compatible; DSA/1.0; "
                "+https://github.com/ZhuLinsen/daily_stock_analysis)"
            ),
        }

    def _fetch_raw_data(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        return pd.DataFrame(columns=STANDARD_COLUMNS)

    def _normalize_data(self, df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
        return df.reindex(columns=STANDARD_COLUMNS)
