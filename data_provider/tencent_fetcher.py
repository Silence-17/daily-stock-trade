# -*- coding: utf-8 -*-
"""Tencent direct daily K-line fetcher for A-share fallback routing."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

import pandas as pd
import requests

try:
    import exchange_calendars as xcals
except ImportError:  # pragma: no cover - dependency is present in supported installs
    xcals = None

from .base import BaseFetcher, DataFetchError, STANDARD_COLUMNS, normalize_stock_code, is_bse_code
from .realtime_types import RealtimeSource, UnifiedRealtimeQuote

logger = logging.getLogger(__name__)

_MAX_KLINE_BARS = 800


class TencentFetcher(BaseFetcher):
    """Fetch qfq daily K-line data from Tencent's direct quote endpoint."""

    name = "TencentFetcher"
    priority = 0
    allow_empty_daily_data = True

    _KLINE_ENDPOINT = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    _QUOTE_ENDPOINT = "https://qt.gtimg.cn/q=r_hk{code}"
    _HTTP_TIMEOUT_SECONDS = 8

    def _fetch_raw_data(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        code = normalize_stock_code(stock_code)
        symbol = _to_tencent_symbol(code)
        if not symbol:
            raise DataFetchError(f"TencentFetcher unsupported stock code: {stock_code}")

        lookback = _estimate_lookback_days(start_date=start_date, end_date=end_date)
        explicit_start = _format_tencent_date(start_date)
        explicit_end = _format_tencent_date(end_date)
        explicit_window = (
            f"{explicit_start},{explicit_end}"
            if explicit_start and explicit_end
            else ","
        )
        response = requests.get(
            self._KLINE_ENDPOINT,
            params={"param": f"{symbol},day,{explicit_window},{lookback},qfq"},
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json,text/plain,*/*"},
            timeout=self._HTTP_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
        rows = _extract_kline_rows(payload, symbol=symbol)
        if not rows:
            logger.info("TencentFetcher empty daily history for %s", stock_code)
            return _empty_daily_frame()

        df = pd.DataFrame(rows)
        first_returned_date = _first_returned_date(df)
        if first_returned_date and _is_capped_history_incomplete(
            first_returned_date=first_returned_date,
            start_date=start_date,
            lookback=lookback,
            returned_rows=len(rows),
        ):
            logger.info(
                "TencentFetcher incomplete capped daily history for %s: first_date=%s requested_start=%s",
                stock_code,
                first_returned_date,
                start_date,
            )
            return _empty_daily_frame()

        df = df[(df["date"] >= start_date) & (df["date"] <= end_date)]
        if df.empty:
            logger.info(
                "TencentFetcher daily history outside requested range for %s: %s~%s",
                stock_code,
                start_date,
                end_date,
            )
            return _empty_daily_frame()
        return df

    def _normalize_data(self, df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
        normalized = df.copy()
        for column in ("open", "high", "low", "close", "volume", "amount"):
            if column in normalized.columns:
                normalized[column] = pd.to_numeric(normalized[column], errors="coerce")
        if "pct_chg" not in normalized.columns:
            normalized["pct_chg"] = normalized["close"].pct_change().fillna(0.0) * 100
        normalized = normalized[["date", "open", "high", "low", "close", "volume", "amount", "pct_chg"]]
        return normalized

    def get_realtime_quote(self, stock_code: str) -> Optional[UnifiedRealtimeQuote]:
        """Fetch one HK quote directly without downloading the full market table."""
        code = normalize_stock_code(stock_code)
        match = re.fullmatch(r"HK(\d{5})", code)
        if not match:
            return None
        hk_code = match.group(1)
        try:
            response = requests.get(
                self._QUOTE_ENDPOINT.format(code=hk_code),
                headers={
                    "Referer": "https://finance.qq.com",
                    "User-Agent": "Mozilla/5.0 (compatible; DSA/1.0)",
                    "Accept": "text/plain,*/*",
                },
                timeout=self._HTTP_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            payload = response.content.decode("gb18030", "ignore").strip()
            if not payload or '=""' in payload:
                return None
            fields = payload[payload.index('"') + 1:payload.rindex('"')].split("~")
            if len(fields) < 60:
                raise ValueError(f"field_count={len(fields)}")

            def number(index: int) -> Optional[float]:
                value = fields[index].strip() if len(fields) > index else ""
                if not value:
                    return None
                parsed = float(value)
                return parsed if parsed == parsed else None

            price = number(3)
            volume = number(36) or number(6)
            if price is None or price <= 0 or volume is None or volume <= 0:
                raise ValueError("missing positive price or volume")
            provider_timestamp = None
            timestamp_text = fields[30].strip()
            if timestamp_text:
                provider_timestamp = (
                    datetime.strptime(timestamp_text, "%Y/%m/%d %H:%M:%S")
                    .replace(tzinfo=ZoneInfo("Asia/Hong_Kong"))
                    .astimezone(timezone.utc)
                    .isoformat()
                )
            total_mv = number(44)
            circ_mv = number(45)
            missing_fields = ["volume_ratio"]
            quote = UnifiedRealtimeQuote(
                code=f"HK{hk_code}",
                name=fields[1].strip() or f"HK{hk_code}",
                source=RealtimeSource.TENCENT,
                provider_timestamp=provider_timestamp,
                market="hk",
                currency=(fields[75].strip().upper() if len(fields) > 75 else "") or "HKD",
                data_quality="partial",
                missing_fields=missing_fields,
                price=price,
                change_pct=number(32),
                change_amount=number(31),
                volume=int(volume),
                amount=number(37) or price * volume,
                turnover_rate=number(38),
                amplitude=number(43),
                open_price=number(5),
                high=number(33),
                low=number(34),
                pre_close=number(4),
                pe_ratio=number(39),
                pb_ratio=number(58),
                total_mv=(total_mv * 100000000 if total_mv is not None else None),
                circ_mv=(circ_mv * 100000000 if circ_mv is not None else None),
                high_52w=number(48),
                low_52w=number(49),
            )
            logger.info("TencentFetcher HK realtime quote succeeded for %s", stock_code)
            return quote
        except Exception as exc:  # noqa: BLE001 - realtime routing falls back to other providers.
            logger.warning("TencentFetcher HK realtime quote failed for %s: %s", stock_code, exc)
            return None


def _to_tencent_symbol(stock_code: str) -> str:
    code = normalize_stock_code(stock_code)
    if not code or not code.isdigit() or len(code) != 6:
        return ""
    if is_bse_code(code):
        return f"bj{code}"
    if code.startswith(("6", "5", "9")):
        return f"sh{code}"
    return f"sz{code}"


def _estimate_lookback_days(*, start_date: str, end_date: str) -> int:
    try:
        start = datetime.strptime(start_date, "%Y-%m-%d")
        end = datetime.strptime(end_date, "%Y-%m-%d")
        calendar_days = max(1, (end - start).days + 1)
    except ValueError:
        calendar_days = 90
    # Trading days are sparse over calendar days; add margin for holidays/suspensions.
    return max(30, min(_MAX_KLINE_BARS, int(calendar_days * 1.8) + 20))


def _empty_daily_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=STANDARD_COLUMNS)


def _first_returned_date(df: pd.DataFrame) -> Optional[str]:
    if "date" not in df.columns or df.empty:
        return None
    dates = pd.to_datetime(df["date"], errors="coerce").dropna()
    if dates.empty:
        return None
    return dates.min().strftime("%Y-%m-%d")


def _is_capped_history_incomplete(
    *,
    first_returned_date: str,
    start_date: str,
    lookback: int,
    returned_rows: int,
) -> bool:
    hit_cap = lookback >= _MAX_KLINE_BARS and returned_rows >= _MAX_KLINE_BARS
    if not hit_cap:
        return False
    try:
        first = datetime.strptime(first_returned_date, "%Y-%m-%d")
        requested_start = datetime.strptime(start_date, "%Y-%m-%d")
    except ValueError:
        return False
    return first > _first_trading_date_on_or_after(requested_start)


def _first_trading_date_on_or_after(start_date: datetime) -> datetime:
    if xcals is not None:
        try:
            cal = xcals.get_calendar("XSHG")
            session = cal.date_to_session(start_date.date(), direction="next")
            return datetime.combine(session.date(), datetime.min.time())
        except Exception:
            pass

    current = start_date
    while current.weekday() >= 5:
        current += timedelta(days=1)
    return current


def _format_tencent_date(date_text: str) -> Optional[str]:
    try:
        return datetime.strptime(date_text, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        return None


def _lots_to_shares(volume: Any) -> Any:
    try:
        return float(volume) * 100
    except (TypeError, ValueError):
        return volume


def _extract_kline_rows(payload: dict[str, Any], *, symbol: str) -> list[dict[str, Any]]:
    data = payload.get("data") if isinstance(payload, dict) else None
    item = data.get(symbol) if isinstance(data, dict) else None
    if not isinstance(item, dict):
        return []
    rows = item.get("qfqday") or item.get("day") or []
    result: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 6:
            continue
        amount: Optional[Any] = row[6] if len(row) > 6 else None
        result.append(
            {
                "date": str(row[0]),
                "open": row[1],
                "close": row[2],
                "high": row[3],
                "low": row[4],
                "volume": _lots_to_shares(row[5]),
                "amount": amount,
            }
        )
    return result
