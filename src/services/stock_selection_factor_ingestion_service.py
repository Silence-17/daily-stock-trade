# -*- coding: utf-8 -*-
"""Bounded historical factor ingestion for an explicitly supplied universe."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Callable, Dict, Iterable, List, Optional

import pandas as pd

from data_provider.base import is_bse_code, normalize_stock_code

from src.repositories.stock_selection_factor_snapshot_repo import (
    StockSelectionFactorSnapshotRepository,
)
from src.repositories.stock_selection_corporate_action_repo import (
    StockSelectionCorporateActionRepository,
)


class StockSelectionFactorIngestionService:
    """Build dated factor snapshots without inferring a historical universe."""

    VALUATION_INDICATORS = {
        "pe_ratio": "市盈率(TTM)",
        "pb_ratio": "市净率",
        "total_mv": "总市值",
    }

    def __init__(
        self,
        repository: Optional[StockSelectionFactorSnapshotRepository] = None,
        daily_fetcher: Optional[Callable[..., pd.DataFrame]] = None,
        valuation_fetcher: Optional[Callable[..., pd.DataFrame]] = None,
        corporate_action_repository: Optional[StockSelectionCorporateActionRepository] = None,
        corporate_action_fetcher: Optional[Callable[..., List[Dict[str, Any]]]] = None,
    ) -> None:
        self.repository = repository or StockSelectionFactorSnapshotRepository()
        self.daily_fetcher = daily_fetcher
        self.valuation_fetcher = valuation_fetcher
        self.corporate_action_repository = corporate_action_repository
        self.corporate_action_fetcher = corporate_action_fetcher

    def ingest(
        self,
        *,
        market: str,
        snapshot_dates: Iterable[date],
        universe: Iterable[Dict[str, Any]],
    ) -> Dict[str, Any]:
        market_value = str(market or "").strip().lower()
        if market_value != "cn":
            raise ValueError("historical factor ingestion currently supports market=cn only")
        dates = sorted(set(snapshot_dates))
        symbols = [dict(item or {}) for item in universe]
        if not dates or len(dates) > 50:
            raise ValueError("snapshot_dates must contain between 1 and 50 dates")
        if not symbols or len(symbols) > 50:
            raise ValueError("universe must contain between 1 and 50 symbols")
        if any(not isinstance(item, date) for item in dates):
            raise ValueError("snapshot_dates must contain dates")

        daily_fetcher, valuation_fetcher = self._fetchers()
        corporate_action_fetcher = self._resolve_corporate_action_fetcher()
        rows_by_date: Dict[date, List[Dict[str, Any]]] = {item: [] for item in dates}
        corporate_action_rows: List[Dict[str, Any]] = []
        errors: List[Dict[str, str]] = []
        used_daily_sources = set()
        for item in symbols:
            symbol = str(item.get("symbol") or item.get("code") or "").strip()
            name = str(item.get("name") or "").strip()
            if not symbol or not name:
                raise ValueError("every historical universe item requires symbol and point-in-time name")
            try:
                daily = daily_fetcher(
                    symbol=symbol,
                    period="daily",
                    start_date=(dates[0] - timedelta(days=120)).strftime("%Y%m%d"),
                    end_date=dates[-1].strftime("%Y%m%d"),
                    adjust="",
                )
                normalized_daily = self._normalize_daily(daily)
                daily_source = str(
                    normalized_daily.attrs.get("source_provider")
                    or "custom.daily_fetcher"
                )
                used_daily_sources.add(daily_source)
            except Exception as exc:
                errors.append({"symbol": symbol, "stage": "daily", "error": str(exc)})
                normalized_daily = pd.DataFrame()
                daily_source = "unavailable"

            if corporate_action_fetcher is not None:
                try:
                    corporate_action_rows.extend(corporate_action_fetcher(
                        stock_code=symbol,
                        start_date=dates[0] - timedelta(days=7),
                        end_date=dates[-1] + timedelta(days=400),
                    ))
                except Exception as exc:
                    errors.append({"symbol": symbol, "stage": "corporate_actions", "error": str(exc)})

            valuations = {}
            valuation_dates = {}
            for field, indicator in self.VALUATION_INDICATORS.items():
                try:
                    frame = valuation_fetcher(symbol=symbol, indicator=indicator, period="全部")
                    valuations[field] = self._normalize_valuation(frame)
                except Exception as exc:
                    errors.append({"symbol": symbol, "stage": field, "error": str(exc)})
                    valuations[field] = pd.DataFrame(columns=["date", "value"])

            for snapshot_date in dates:
                row = self._build_row(
                    symbol=symbol,
                    name=name,
                    industry=item.get("industry"),
                    snapshot_date=snapshot_date,
                    daily=normalized_daily,
                    daily_source=daily_source,
                    valuations=valuations,
                )
                rows_by_date[snapshot_date].append(row)
                valuation_dates[snapshot_date] = row.get("source", {}).get("valuation_dates", {})

        inserted = 0
        updated = 0
        persisted_rows = [row for rows in rows_by_date.values() for row in rows]
        complete_row_count = sum(
            1 for row in persisted_rows if row.get("quality_status") == "complete"
        )
        exact_daily_row_count = sum(
            1 for row in persisted_rows if row.get("price") is not None
        )
        valuation_complete_row_count = sum(
            1
            for row in persisted_rows
            if all(row.get(field) is not None for field in self.VALUATION_INDICATORS)
        )
        for snapshot_date, rows in rows_by_date.items():
            result = self.repository.upsert_many(
                market=market_value,
                snapshot_date=snapshot_date,
                rows=rows,
            )
            inserted += result["inserted"]
            updated += result["updated"]
        corporate_action_result = {"inserted": 0, "updated": 0, "total": 0}
        if corporate_action_rows:
            repository = self.corporate_action_repository or StockSelectionCorporateActionRepository()
            corporate_action_result = repository.upsert_many(
                market=market_value,
                rows=corporate_action_rows,
            )
        return {
            "market": market_value,
            "snapshot_dates": [item.isoformat() for item in dates],
            "symbol_count": len(symbols),
            "row_count": sum(len(rows) for rows in rows_by_date.values()),
            "complete_row_count": complete_row_count,
            "partial_row_count": len(persisted_rows) - complete_row_count,
            "exact_daily_row_count": exact_daily_row_count,
            "valuation_complete_row_count": valuation_complete_row_count,
            "inserted": inserted,
            "updated": updated,
            "corporate_action_count": corporate_action_result["total"],
            "corporate_action_inserted": corporate_action_result["inserted"],
            "corporate_action_updated": corporate_action_result["updated"],
            "error_count": len(errors),
            "errors": errors,
            "methodology": {
                "universe_source": "caller_supplied_point_in_time_universe",
                "daily_source": "provider_fallback_route",
                "daily_source_route": [
                    "akshare.stock_zh_a_daily_sina",
                    "akshare.stock_zh_a_hist_eastmoney",
                    "tencent.fqkline",
                    "baostock.query_history_k_data_plus",
                ],
                "daily_sources_used": sorted(used_daily_sources),
                "valuation_source": "akshare.stock_zh_valuation_baidu",
                "corporate_action_source": (
                    "tushare.dividend" if corporate_action_fetcher is not None else "unavailable"
                ),
                "corporate_action_horizon_days": 400,
                "valuation_asof_rule": "latest_value_on_or_before_snapshot_date",
                "technical_feature_rule": "daily_bars_on_or_before_snapshot_date",
                "uses_current_universe_fallback": False,
                "uses_future_values": False,
                "corporate_actions_do_not_feed_selection_factors": True,
                "complete_row_rule": "all required factor fields are non-null",
            },
        }

    def _build_row(
        self,
        *,
        symbol: str,
        name: str,
        industry: Any,
        snapshot_date: date,
        daily: pd.DataFrame,
        daily_source: str,
        valuations: Dict[str, pd.DataFrame],
    ) -> Dict[str, Any]:
        history = daily[daily["date"] <= snapshot_date].copy() if not daily.empty else pd.DataFrame()
        exact = history[history["date"] == snapshot_date] if not history.empty else pd.DataFrame()
        factors: Dict[str, Any] = {}
        if not history.empty:
            try:
                from alphasift.daily import compute_daily_features

                factors = dict(compute_daily_features(history))
            except Exception:
                factors = {}
        latest = exact.iloc[-1] if not exact.empty else None
        values = {}
        valuation_dates = {}
        for field, frame in valuations.items():
            eligible = frame[frame["date"] <= snapshot_date] if not frame.empty else frame
            if eligible is not None and not eligible.empty:
                valuation = eligible.iloc[-1]
                values[field] = self._finite_or_none(valuation["value"])
                valuation_dates[field] = valuation["date"].isoformat()
            else:
                values[field] = None

        row = {
            "symbol": symbol,
            "name": name,
            "industry": industry,
            "price": self._series_value(latest, "close"),
            "change_pct": self._series_value(latest, "change_pct"),
            "amount": self._series_value(latest, "amount"),
            "volume": self._series_value(latest, "volume"),
            "turnover_rate": self._series_value(latest, "turnover_rate"),
            "volume_ratio": self._finite_or_none(factors.get("volume_ratio_20d")),
            **values,
            "factors": factors,
            "source": {
                "daily": daily_source,
                "valuation": "akshare.stock_zh_valuation_baidu",
                "valuation_dates": valuation_dates,
            },
        }
        required = [
            "name", "price", "change_pct", "amount", "turnover_rate", "volume_ratio",
            "pe_ratio", "pb_ratio", "total_mv",
        ]
        missing = [field for field in required if row.get(field) is None or row.get(field) == ""]
        row["missing_fields"] = missing
        row["quality_status"] = "complete" if not missing else "partial"
        return row

    @staticmethod
    def _normalize_daily(frame: Any) -> pd.DataFrame:
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            return pd.DataFrame()
        source_provider = frame.attrs.get("source_provider")
        aliases = {
            "日期": "date",
            "开盘": "open",
            "收盘": "close",
            "最高": "high",
            "最低": "low",
            "成交量": "volume",
            "成交额": "amount",
            "涨跌幅": "change_pct",
            "换手率": "turnover_rate",
        }
        result = frame.rename(columns=aliases).copy()
        required = {"date", "open", "close", "high", "low", "volume"}
        if not required.issubset(result.columns):
            raise ValueError(f"daily history missing columns: {sorted(required - set(result.columns))}")
        result["date"] = pd.to_datetime(result["date"], errors="coerce").dt.date
        for column in set(result.columns) - {"date"}:
            result[column] = pd.to_numeric(result[column], errors="coerce")
        result = result.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
        if source_provider:
            result.attrs["source_provider"] = source_provider
        return result

    @staticmethod
    def _normalize_valuation(frame: Any) -> pd.DataFrame:
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            return pd.DataFrame(columns=["date", "value"])
        if not {"date", "value"}.issubset(frame.columns):
            raise ValueError("valuation history missing date/value columns")
        result = frame[["date", "value"]].copy()
        result["date"] = pd.to_datetime(result["date"], errors="coerce").dt.date
        result["value"] = pd.to_numeric(result["value"], errors="coerce")
        return result.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)

    @staticmethod
    def _series_value(row: Any, field: str) -> Optional[float]:
        return StockSelectionFactorIngestionService._finite_or_none(
            row.get(field) if row is not None else None
        )

    @staticmethod
    def _finite_or_none(value: Any) -> Optional[float]:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if pd.notna(parsed) and parsed not in (float("inf"), float("-inf")) else None

    def _fetchers(self) -> tuple[Callable[..., pd.DataFrame], Callable[..., pd.DataFrame]]:
        if self.daily_fetcher is not None and self.valuation_fetcher is not None:
            return self.daily_fetcher, self.valuation_fetcher
        try:
            import akshare as ak
        except ImportError as exc:
            raise RuntimeError("AKShare is unavailable") from exc
        return (
            self.daily_fetcher or self._build_daily_fetcher(ak),
            self.valuation_fetcher or ak.stock_zh_valuation_baidu,
        )

    @staticmethod
    def _build_daily_fetcher(ak: Any) -> Callable[..., pd.DataFrame]:
        from data_provider.baostock_fetcher import BaostockFetcher
        from data_provider.tencent_fetcher import TencentFetcher

        tencent = TencentFetcher()
        baostock = BaostockFetcher()

        def fetch(
            *,
            symbol: str,
            period: str,
            start_date: str,
            end_date: str,
            adjust: str,
        ) -> pd.DataFrame:
            code = normalize_stock_code(symbol)
            start_iso = StockSelectionFactorIngestionService._iso_date(start_date)
            end_iso = StockSelectionFactorIngestionService._iso_date(end_date)
            errors = []

            try:
                prefix = StockSelectionFactorIngestionService._market_prefix(code)
                frame = ak.stock_zh_a_daily(
                    symbol=f"{prefix}{code}",
                    start_date=start_date,
                    end_date=end_date,
                    adjust=adjust,
                )
                frame = StockSelectionFactorIngestionService._normalize_sina_daily(frame)
                if not frame.empty:
                    frame.attrs["source_provider"] = "akshare.stock_zh_a_daily_sina"
                    return frame
                errors.append("akshare.stock_zh_a_daily_sina returned no rows")
            except Exception as exc:
                errors.append(f"akshare.stock_zh_a_daily_sina failed: {exc}")

            try:
                frame = ak.stock_zh_a_hist(
                    symbol=code,
                    period=period,
                    start_date=start_date,
                    end_date=end_date,
                    adjust=adjust,
                )
                if isinstance(frame, pd.DataFrame) and not frame.empty:
                    frame.attrs["source_provider"] = "akshare.stock_zh_a_hist_eastmoney"
                    return frame
                errors.append("akshare.stock_zh_a_hist_eastmoney returned no rows")
            except Exception as exc:
                errors.append(f"akshare.stock_zh_a_hist_eastmoney failed: {exc}")

            try:
                frame = tencent.get_daily_data(
                    code,
                    start_date=start_iso,
                    end_date=end_iso,
                )
                if isinstance(frame, pd.DataFrame) and not frame.empty:
                    frame = frame.rename(columns={"pct_chg": "change_pct"})
                    frame.attrs["source_provider"] = "tencent.fqkline"
                    return frame
                errors.append("tencent.fqkline returned no rows")
            except Exception as exc:
                errors.append(f"tencent.fqkline failed: {exc}")

            if not is_bse_code(code):
                try:
                    frame = baostock.get_daily_data(
                        code,
                        start_date=start_iso,
                        end_date=end_iso,
                    )
                    if isinstance(frame, pd.DataFrame) and not frame.empty:
                        frame = frame.rename(columns={"pct_chg": "change_pct"})
                        frame.attrs["source_provider"] = (
                            "baostock.query_history_k_data_plus"
                        )
                        return frame
                    errors.append("baostock.query_history_k_data_plus returned no rows")
                except Exception as exc:
                    errors.append(f"baostock.query_history_k_data_plus failed: {exc}")

            raise RuntimeError("; ".join(errors))

        return fetch

    @staticmethod
    def _normalize_sina_daily(frame: Any) -> pd.DataFrame:
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            return pd.DataFrame()
        result = frame.copy()
        result["change_pct"] = pd.to_numeric(
            result.get("close"), errors="coerce"
        ).pct_change(fill_method=None) * 100
        if "turnover" in result.columns:
            result["turnover_rate"] = pd.to_numeric(
                result["turnover"], errors="coerce"
            ) * 100
        return result

    @staticmethod
    def _market_prefix(code: str) -> str:
        if is_bse_code(code):
            return "bj"
        if code.startswith(("5", "6", "9")):
            return "sh"
        return "sz"

    @staticmethod
    def _iso_date(value: str) -> str:
        return pd.to_datetime(value, errors="raise").date().isoformat()

    def _resolve_corporate_action_fetcher(self) -> Optional[Callable[..., List[Dict[str, Any]]]]:
        if self.corporate_action_fetcher is not None:
            return self.corporate_action_fetcher
        if self.daily_fetcher is not None or self.valuation_fetcher is not None:
            return None
        from src.config import get_config

        if not get_config().tushare_token:
            return None
        from data_provider.tushare_fetcher import TushareFetcher

        fetcher = TushareFetcher()
        return fetcher.get_stock_corporate_actions if fetcher.is_available() else None
