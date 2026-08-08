# -*- coding: utf-8 -*-
"""Credential-free realtime quotes for Japanese and Taiwan equities."""

from __future__ import annotations

import json
import logging
import re
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
TAIPEI_TZ = ZoneInfo("Asia/Taipei")
YAHOO_JAPAN_QUOTE_URL = "https://finance.yahoo.co.jp/quote/{code}"
TWSE_MIS_QUOTE_URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
NEXT_DATA_PREFIX = "self.__next_f.push([1,"
ESCAPED_PRICE_BOARD_MARKER = (
    '\\"priceBoard\\":{\\"currentKey\\":\\"detail\\",\\"board\\":'
)
PRICE_BOARD_MARKER = '"priceBoard":{"currentKey":"detail","board":'
JP_EQUITY_CODE = re.compile(r"^[0-9]{4,6}\.T$")
TW_EQUITY_CODE = re.compile(r"^([0-9]{4,6})\.(TW|TWO)$")


def _number(value: Any) -> Optional[float]:
    try:
        parsed = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed else None


class AsiaEquityFetcher(BaseFetcher):
    """Fetch strict provider-timestamped JP and TW equity quotes."""

    name = "AsiaEquityFetcher"
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
        if JP_EQUITY_CODE.fullmatch(code):
            return self._get_japan_quote(code)
        if TW_EQUITY_CODE.fullmatch(code):
            return self._get_taiwan_quote(code)
        return None

    def _get_japan_quote(self, code: str) -> Optional[UnifiedRealtimeQuote]:
        if not self._is_current_japan_session_day():
            return None
        try:
            response = self._session.get(
                YAHOO_JAPAN_QUOTE_URL.format(code=code),
                headers=self._headers(referer="https://finance.yahoo.co.jp/"),
                timeout=self._timeout_seconds,
            )
            response.raise_for_status()
            board = self._extract_yahoo_japan_board(response.text)
        except (requests.RequestException, ValueError, TypeError, json.JSONDecodeError) as exc:
            logger.warning("[AsiaEquity] Japan quote request failed for %s: %s", code, exc)
            return None
        if board is None or str(board.get("codeWithMarketExtension") or "").upper() != code:
            return None
        delay_minutes = _number(board.get("delayMinutes"))
        if delay_minutes is None or delay_minutes != 0:
            return None
        price = _number((board.get("price") or {}).get("value"))
        change_amount = _number((board.get("priceChange") or {}).get("value"))
        change_pct = _number((board.get("priceChangeRate") or {}).get("value"))
        provider_at = self._parse_tokyo_update_time(board.get("japanUpdateTime"))
        if price is None or price <= 0 or change_pct is None or provider_at is None:
            return None
        previous_close = price - change_amount if change_amount is not None else None
        return UnifiedRealtimeQuote(
            code=code,
            name=str(board.get("name") or code),
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

    def _get_taiwan_quote(self, code: str) -> Optional[UnifiedRealtimeQuote]:
        match = TW_EQUITY_CODE.fullmatch(code)
        if match is None:
            return None
        base_code, suffix = match.groups()
        exchange = "tse" if suffix == "TW" else "otc"
        try:
            response = self._session.get(
                TWSE_MIS_QUOTE_URL,
                params={
                    "ex_ch": f"{exchange}_{base_code}.tw",
                    "json": "1",
                    "delay": "0",
                },
                headers=self._headers(referer="https://mis.twse.com.tw/"),
                timeout=self._timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError, TypeError, json.JSONDecodeError) as exc:
            logger.warning("[AsiaEquity] Taiwan quote request failed for %s: %s", code, exc)
            return None
        rows = payload.get("msgArray") if isinstance(payload, dict) else None
        row = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else None
        if (
            row is None
            or str(row.get("c") or "") != base_code
            or str(row.get("ex") or "").lower() != exchange
        ):
            return None
        price = _number(row.get("z")) or _number(row.get("pz"))
        previous_close = _number(row.get("y"))
        provider_at = self._parse_taiwan_trade_time(
            row.get("d"),
            row.get("t"),
        )
        if (
            price is None
            or price <= 0
            or previous_close is None
            or previous_close <= 0
            or provider_at is None
        ):
            return None
        change_amount = price - previous_close
        volume_lots = _number(row.get("v"))
        return UnifiedRealtimeQuote(
            code=code,
            name=str(row.get("nf") or row.get("n") or code),
            source=RealtimeSource.TWSE,
            provider_timestamp=provider_at.isoformat(),
            market="tw",
            currency="TWD",
            data_quality="partial",
            missing_fields=["amount", "bid_price", "ask_price"],
            price=price,
            change_pct=round(change_amount / previous_close * 100.0, 6),
            change_amount=round(change_amount, 6),
            volume=int(volume_lots * 1000) if volume_lots is not None else None,
            open_price=_number(row.get("o")),
            high=_number(row.get("h")),
            low=_number(row.get("l")),
            pre_close=previous_close,
        )

    @staticmethod
    def _parse_taiwan_trade_time(
        trade_date: Any,
        trade_time: Any,
    ) -> Optional[datetime]:
        """Parse TWSE's explicit trade date/time instead of its shifted tlong."""

        date_text = str(trade_date or "").strip()
        time_text = str(trade_time or "").strip()
        if not date_text or not time_text:
            return None
        try:
            local_at = datetime.strptime(
                f"{date_text} {time_text}",
                "%Y%m%d %H:%M:%S",
            ).replace(tzinfo=TAIPEI_TZ)
        except ValueError:
            return None
        return local_at.astimezone(timezone.utc)

    @staticmethod
    def _extract_yahoo_japan_board(text: str) -> Optional[Dict[str, Any]]:
        page = str(text or "")
        marker_at = page.find(ESCAPED_PRICE_BOARD_MARKER)
        if marker_at >= 0:
            push_at = page.rfind(NEXT_DATA_PREFIX, 0, marker_at)
            if push_at < 0:
                return None
            encoded_at = push_at + len(NEXT_DATA_PREFIX)
            decoded, _ = json.JSONDecoder().raw_decode(page[encoded_at:])
            board_at = decoded.find(PRICE_BOARD_MARKER)
            if board_at < 0:
                return None
            board, _ = json.JSONDecoder().raw_decode(
                decoded[board_at + len(PRICE_BOARD_MARKER):]
            )
            return board if isinstance(board, dict) else None
        plain_at = page.find(PRICE_BOARD_MARKER)
        if plain_at < 0:
            return None
        board, _ = json.JSONDecoder().raw_decode(
            page[plain_at + len(PRICE_BOARD_MARKER):]
        )
        return board if isinstance(board, dict) else None

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
