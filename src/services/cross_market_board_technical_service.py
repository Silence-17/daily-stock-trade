"""Point-in-time A-share board support, resistance, and rotation evidence."""

from __future__ import annotations

import math
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

import pandas as pd

from src.services.cross_market_paper_strategy import ROTATION_REFERENCE_KEYWORDS


HistoryLoader = Callable[[str, str, str, str], Any]
IntradayLoader = Callable[[str, str], Any]

TECHNICAL_WINDOWS: Tuple[int, ...] = (5, 10, 20, 30, 60)
MA_SUPPORT_SCORES = {
    5: 65.0,
    10: 75.0,
    20: 85.0,
    30: 92.0,
    60: 100.0,
}
SWING_SUPPORT_SCORES = {
    5: 50.0,
    10: 60.0,
    20: 70.0,
    30: 75.0,
    60: 80.0,
}


def _number(value: Any) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


class CrossMarketBoardTechnicalService:
    """Calculate board levels from completed daily bars and a live change rate."""

    def __init__(
        self,
        *,
        history_loader: Optional[HistoryLoader] = None,
        intraday_loader: Optional[IntradayLoader] = None,
        cache_seconds: int = 6 * 60 * 60,
        intraday_cache_seconds: int = 30,
        support_tolerance_pct: float = 1.5,
        resistance_warning_pct: float = 2.0,
        breakout_confirmation_pct: float = 1.0,
        breakout_min_volume_ratio: float = 1.5,
        breakout_required_5m_closes: int = 2,
    ) -> None:
        self.history_loader = history_loader or self._default_history_loader
        self.intraday_loader = intraday_loader or self._default_intraday_loader
        self.cache_seconds = max(60, int(cache_seconds))
        self.intraday_cache_seconds = max(1, int(intraday_cache_seconds))
        self.support_tolerance_pct = max(0.1, float(support_tolerance_pct))
        self.resistance_warning_pct = max(0.1, float(resistance_warning_pct))
        self.breakout_confirmation_pct = max(
            0.1,
            float(breakout_confirmation_pct),
        )
        self.breakout_min_volume_ratio = max(
            0.1,
            float(breakout_min_volume_ratio),
        )
        self.breakout_required_5m_closes = max(
            2,
            int(breakout_required_5m_closes),
        )
        self._cache: Dict[Tuple[str, str, str], Tuple[float, pd.DataFrame]] = {}
        self._intraday_cache: Dict[
            Tuple[str, str], Tuple[float, pd.DataFrame]
        ] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _default_history_loader(
        identifier: str,
        board_type: str,
        start_date: str,
        end_date: str,
    ) -> Any:
        import akshare as ak

        normalized_type = str(board_type or "").strip().lower()
        errors = []
        loaders = (
            (
                "industry",
                lambda: ak.stock_board_industry_hist_em(
                    symbol=identifier,
                    start_date=start_date,
                    end_date=end_date,
                    period="\u65e5k",
                    adjust="",
                ),
            ),
            (
                "concept",
                lambda: ak.stock_board_concept_hist_em(
                    symbol=identifier,
                    period="daily",
                    start_date=start_date,
                    end_date=end_date,
                    adjust="",
                ),
            ),
        )
        ordered = sorted(
            loaders,
            key=lambda item: 0 if item[0] == normalized_type else 1,
        )
        for _kind, loader in ordered:
            try:
                frame = loader()
                if frame is not None and not pd.DataFrame(frame).empty:
                    return frame
            except Exception as exc:  # noqa: BLE001 - alternate board route is valid.
                errors.append(exc)
        if errors:
            raise errors[-1]
        return pd.DataFrame()

    @staticmethod
    def _default_intraday_loader(identifier: str, board_type: str) -> Any:
        import akshare as ak

        normalized_type = str(board_type or "").strip().lower()
        loaders = (
            (
                "industry",
                lambda: ak.stock_board_industry_hist_min_em(
                    symbol=identifier,
                    period="5",
                ),
            ),
            (
                "concept",
                lambda: ak.stock_board_concept_hist_min_em(
                    symbol=identifier,
                    period="5",
                ),
            ),
        )
        ordered = sorted(
            loaders,
            key=lambda item: 0 if item[0] == normalized_type else 1,
        )
        errors = []
        for _kind, loader in ordered:
            try:
                frame = loader()
                if frame is not None and not pd.DataFrame(frame).empty:
                    return frame
            except Exception as exc:  # noqa: BLE001 - alternate board route is valid.
                errors.append(exc)
        if errors:
            raise errors[-1]
        return pd.DataFrame()

    def analyze_boards(
        self,
        boards: Iterable[Dict[str, Any]],
        *,
        observed_at: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        current = (observed_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
        results = []
        errors = []
        seen = set()
        for board in boards:
            if not isinstance(board, dict):
                continue
            name = str(board.get("name") or "").strip()
            identifier = str(board.get("code") or name).strip()
            if not name or not identifier or identifier in seen:
                continue
            seen.add(identifier)
            change_pct = _number(board.get("change_pct"))
            if change_pct is None:
                errors.append({"name": name, "reason": "live_change_unavailable"})
                continue
            try:
                result = self.analyze_board(
                    name=name,
                    identifier=identifier,
                    board_type=str(board.get("type") or "concept"),
                    live_change_pct=change_pct,
                    live_volume_ratio=_number(board.get("volume_ratio")),
                    observed_at=current,
                )
            except Exception as exc:  # noqa: BLE001 - other matched boards remain usable.
                errors.append({"name": name, "reason": type(exc).__name__})
                continue
            if result.get("available") is True:
                results.append(result)
            else:
                errors.append({"name": name, "reason": result.get("reason")})
        if not results:
            return {
                "available": False,
                "supportive": False,
                "near_resistance": False,
                "reason": "board_technical_evidence_unavailable",
                "observed_at": current.isoformat(),
                "boards": [],
                "errors": errors,
            }
        primary = max(
            results,
            key=lambda item: (
                int(item.get("supportive") is True),
                float(item.get("support_score") or 0.0),
                float(item.get("live_change_pct") or 0.0),
            ),
        )
        alerts = [
            {"board": item.get("name"), **alert}
            for item in results
            for alert in list(item.get("alerts") or [])
        ]
        pressure_boards = [
            str(item.get("name") or "").strip()
            for item in results
            if item.get("near_resistance") is True
            and str(item.get("name") or "").strip()
        ]
        near_resistance = bool(pressure_boards)
        return {
            "available": True,
            "supportive": bool(primary.get("supportive")),
            "near_resistance": near_resistance,
            "breakout_confirmed": bool(
                primary.get("breakout_confirmed")
                and not near_resistance
            ),
            "support_score": primary.get("support_score"),
            "reason": (
                "board_near_resistance"
                if near_resistance
                else primary.get("reason")
            ),
            "observed_at": current.isoformat(),
            "primary_board": primary,
            "pressure_boards": pressure_boards,
            "boards": results,
            "alerts": alerts,
            "errors": errors,
        }

    def analyze_board(
        self,
        *,
        name: str,
        identifier: str,
        board_type: str,
        live_change_pct: float,
        live_volume_ratio: Optional[float] = None,
        observed_at: datetime,
    ) -> Dict[str, Any]:
        shanghai = ZoneInfo("Asia/Shanghai")
        local_day = observed_at.astimezone(shanghai).date()
        completed_through = local_day - timedelta(days=1)
        start_date = (completed_through - timedelta(days=180)).strftime("%Y%m%d")
        end_date = completed_through.strftime("%Y%m%d")
        frame = self._history(
            identifier=identifier,
            board_type=board_type,
            start_date=start_date,
            end_date=end_date,
        )
        normalized = self._normalize_history(frame, completed_through=completed_through)
        if len(normalized) < 60:
            return {
                "available": False,
                "reason": "board_history_60_sessions_required",
                "name": name,
                "identifier": identifier,
                "history_count": len(normalized),
            }
        recent60 = normalized.tail(max(TECHNICAL_WINDOWS))
        previous_close = float(recent60.iloc[-1]["close"])
        current_level = previous_close * (1.0 + float(live_change_pct) / 100.0)
        moving_averages = {
            window: float(recent60.tail(window)["close"].mean())
            for window in TECHNICAL_WINDOWS
        }
        moving_average_slopes = {}
        for window in TECHNICAL_WINDOWS:
            rolling = normalized["close"].rolling(window).mean().dropna()
            moving_average_slopes[window] = (
                (float(rolling.iloc[-1]) / float(rolling.iloc[-2]) - 1.0) * 100.0
                if len(rolling) >= 2 and float(rolling.iloc[-2]) > 0
                else None
            )
        support_levels = {
            window: float(recent60.tail(window)["low"].min())
            for window in TECHNICAL_WINDOWS
        }
        resistance_levels = {
            window: float(recent60.tail(window)["high"].max())
            for window in TECHNICAL_WINDOWS
        }

        def distance_pct(level: float) -> float:
            return (current_level / level - 1.0) * 100.0 if level > 0 else 999.0

        distances = {
            **{
                f"ma{window}": distance_pct(moving_averages[window])
                for window in TECHNICAL_WINDOWS
            },
            **{
                f"low{window}": distance_pct(support_levels[window])
                for window in TECHNICAL_WINDOWS
            },
            **{
                f"high{window}": distance_pct(resistance_levels[window])
                for window in TECHNICAL_WINDOWS
            },
        }
        near_ma_support = {
            window: (
                0.0 <= distances[f"ma{window}"] <= self.support_tolerance_pct
                and moving_average_slopes[window] is not None
                and float(moving_average_slopes[window]) >= -1e-9
            )
            for window in TECHNICAL_WINDOWS
        }
        near_swing_support = {
            window: 0.0 <= distances[f"low{window}"] <= self.support_tolerance_pct
            for window in TECHNICAL_WINDOWS
        }
        falling_ma_support_windows = [
            window
            for window in TECHNICAL_WINDOWS
            if 0.0 <= distances[f"ma{window}"] <= self.support_tolerance_pct
            and moving_average_slopes[window] is not None
            and float(moving_average_slopes[window]) < -1e-9
        ]
        below_support_windows = sorted({
            window
            for window in TECHNICAL_WINDOWS
            if (
                -self.support_tolerance_pct
                <= distances[f"ma{window}"]
                < 0.0
            )
            or (
                -self.support_tolerance_pct
                <= distances[f"low{window}"]
                < 0.0
            )
        })
        near_ma_support_windows = [
            window for window in TECHNICAL_WINDOWS if near_ma_support[window]
        ]
        near_swing_support_windows = [
            window for window in TECHNICAL_WINDOWS if near_swing_support[window]
        ]
        support_windows = sorted(
            set(near_ma_support_windows) | set(near_swing_support_windows)
        )
        supportive = bool(support_windows)
        resistance_candidates = [
            (window, resistance_levels[window])
            for window in TECHNICAL_WINDOWS
        ]
        upcoming = [
            item for item in resistance_candidates if item[1] >= current_level
        ]
        nearest_resistance_window, nearest_resistance = (
            min(upcoming, key=lambda item: (item[1], item[0]))
            if upcoming
            else max(resistance_candidates, key=lambda item: (item[1], item[0]))
        )
        resistance_distance_pct = (
            (nearest_resistance / current_level - 1.0) * 100.0
            if current_level > 0
            else 999.0
        )
        breakout_reference = max(resistance_levels.values())
        breakout_pct = (
            (current_level / breakout_reference - 1.0) * 100.0
            if breakout_reference > 0
            else 0.0
        )
        price_breakout_confirmed = bool(
            breakout_pct >= self.breakout_confirmation_pct
            and float(live_change_pct) > 0
        )
        breakout_evidence = self._confirm_breakout(
            identifier=identifier,
            board_type=board_type,
            breakout_level=(
                breakout_reference
                * (1.0 + self.breakout_confirmation_pct / 100.0)
            ),
            price_breakout_confirmed=price_breakout_confirmed,
            live_volume_ratio=live_volume_ratio,
            observed_at=observed_at,
        )
        breakout_confirmed = bool(breakout_evidence["confirmed"])
        unconfirmed_price_breakout = bool(
            price_breakout_confirmed and not breakout_confirmed
        )
        near_resistance = bool(
            (
                -self.breakout_confirmation_pct
                <= resistance_distance_pct
                <= self.resistance_warning_pct
            )
            or unconfirmed_price_breakout
        ) and not breakout_confirmed
        support_candidates = [
            MA_SUPPORT_SCORES[window]
            for window in TECHNICAL_WINDOWS
            if near_ma_support[window]
        ] + [
            SWING_SUPPORT_SCORES[window]
            for window in TECHNICAL_WINDOWS
            if near_swing_support[window]
        ]
        resonance_bonus = min(8.0, max(0, len(support_windows) - 1) * 2.0)
        support_score = min(
            100.0,
            (max(support_candidates) if support_candidates else 0.0)
            + resonance_bonus,
        )
        alerts: List[Dict[str, Any]] = []
        for window in TECHNICAL_WINDOWS:
            if near_ma_support[window]:
                alerts.append({
                    "kind": f"near_{window}d_ma_support",
                    "window_days": window,
                    "level": round(moving_averages[window], 6),
                    "distance_pct": round(distances[f"ma{window}"], 6),
                })
            if near_swing_support[window]:
                alerts.append({
                    "kind": f"near_{window}d_swing_support",
                    "window_days": window,
                    "level": round(support_levels[window], 6),
                    "distance_pct": round(distances[f"low{window}"], 6),
                })
        if near_resistance:
            alerts.append({
                "kind": (
                    "unconfirmed_board_breakout"
                    if unconfirmed_price_breakout
                    else "near_board_resistance"
                ),
                "window_days": nearest_resistance_window,
                "level": round(nearest_resistance, 6),
                "distance_pct": round(resistance_distance_pct, 6),
                "breakout_reason": breakout_evidence.get("reason"),
            })
        reason = (
            "board_near_resistance"
            if near_resistance
            else "board_support_zone_confirmed"
            if supportive
            else "board_between_support_and_resistance"
        )
        return {
            "available": True,
            "reason": reason,
            "name": name,
            "identifier": identifier,
            "board_type": board_type,
            "observed_at": observed_at.astimezone(timezone.utc).isoformat(),
            "history_through_date": completed_through.isoformat(),
            "history_count": len(normalized),
            "live_change_pct": round(float(live_change_pct), 6),
            "previous_close": round(previous_close, 6),
            "current_level": round(current_level, 6),
            "technical_windows": list(TECHNICAL_WINDOWS),
            "moving_averages": {
                str(window): round(moving_averages[window], 6)
                for window in TECHNICAL_WINDOWS
            },
            "moving_average_slopes_pct": {
                str(window): (
                    round(float(moving_average_slopes[window]), 6)
                    if moving_average_slopes[window] is not None
                    else None
                )
                for window in TECHNICAL_WINDOWS
            },
            "support_levels": {
                str(window): round(support_levels[window], 6)
                for window in TECHNICAL_WINDOWS
            },
            "resistance_levels": {
                str(window): round(resistance_levels[window], 6)
                for window in TECHNICAL_WINDOWS
            },
            **{
                f"ma{window}": round(moving_averages[window], 6)
                for window in TECHNICAL_WINDOWS
            },
            **{
                f"support_{window}d": round(support_levels[window], 6)
                for window in TECHNICAL_WINDOWS
            },
            **{
                f"resistance_{window}d": round(resistance_levels[window], 6)
                for window in TECHNICAL_WINDOWS
            },
            "nearest_resistance": round(nearest_resistance, 6),
            "nearest_resistance_window": nearest_resistance_window,
            "distance_pct": {
                key: round(value, 6) for key, value in distances.items()
            },
            "resistance_distance_pct": round(resistance_distance_pct, 6),
            "supportive": supportive,
            "support_score": support_score,
            "support_windows": support_windows,
            "support_resonance_count": len(support_windows),
            "multi_period_support": len(support_windows) >= 2,
            "near_ma_support_windows": near_ma_support_windows,
            "near_swing_support_windows": near_swing_support_windows,
            "falling_ma_support_windows": falling_ma_support_windows,
            "below_support_windows": below_support_windows,
            "support_side_rule": "current_level_at_or_above_support",
            **{
                f"near_ma{window}_support": near_ma_support[window]
                for window in TECHNICAL_WINDOWS
            },
            **{
                f"near_{window}d_swing_support": near_swing_support[window]
                for window in TECHNICAL_WINDOWS
            },
            "near_resistance": near_resistance,
            "price_breakout_confirmed": price_breakout_confirmed,
            "breakout_confirmed": breakout_confirmed,
            "breakout_confirmation": breakout_evidence,
            "live_volume_ratio": (
                round(float(live_volume_ratio), 6)
                if live_volume_ratio is not None
                else None
            ),
            "alerts": alerts,
        }

    def _confirm_breakout(
        self,
        *,
        identifier: str,
        board_type: str,
        breakout_level: float,
        price_breakout_confirmed: bool,
        live_volume_ratio: Optional[float],
        observed_at: datetime,
    ) -> Dict[str, Any]:
        required_closes = self.breakout_required_5m_closes
        base = {
            "confirmed": False,
            "required_volume_ratio": self.breakout_min_volume_ratio,
            "observed_volume_ratio": (
                round(float(live_volume_ratio), 6)
                if live_volume_ratio is not None
                else None
            ),
            "required_5m_closes": required_closes,
            "breakout_level": round(float(breakout_level), 6),
            "completed_5m_closes": [],
        }
        if not price_breakout_confirmed:
            return {**base, "reason": "breakout_price_threshold_unconfirmed"}
        if (
            live_volume_ratio is None
            or live_volume_ratio < self.breakout_min_volume_ratio
        ):
            return {**base, "reason": "breakout_volume_ratio_unconfirmed"}
        try:
            raw = self._intraday_history(
                identifier=identifier,
                board_type=board_type,
            )
        except Exception as exc:  # noqa: BLE001 - an unproven breakout must stay blocked.
            return {
                **base,
                "reason": "breakout_5m_evidence_unavailable",
                "error_type": type(exc).__name__,
            }
        normalized = self._normalize_intraday_history(
            raw,
            observed_at=observed_at,
        )
        if len(normalized) < required_closes:
            return {
                **base,
                "reason": "breakout_5m_closes_insufficient",
                "available_5m_closes": len(normalized),
            }
        selected = normalized.tail(required_closes)
        timestamps = list(selected["timestamp"])
        consecutive = all(
            abs((timestamps[index] - timestamps[index - 1]).total_seconds() - 300.0)
            <= 1.0
            for index in range(1, len(timestamps))
        )
        closes = [
            {
                "timestamp": value["timestamp"].isoformat(),
                "close": round(float(value["close"]), 6),
            }
            for value in selected.to_dict("records")
        ]
        above_breakout = all(
            float(value["close"]) >= breakout_level
            for value in selected.to_dict("records")
        )
        if not consecutive:
            return {
                **base,
                "reason": "breakout_5m_closes_not_consecutive",
                "completed_5m_closes": closes,
            }
        if not above_breakout:
            return {
                **base,
                "reason": "breakout_5m_close_below_threshold",
                "completed_5m_closes": closes,
            }
        return {
            **base,
            "confirmed": True,
            "reason": "breakout_volume_and_5m_closes_confirmed",
            "completed_5m_closes": closes,
        }

    def _intraday_history(
        self,
        *,
        identifier: str,
        board_type: str,
    ) -> pd.DataFrame:
        cache_key = (identifier, str(board_type or "").lower())
        now = time.monotonic()
        with self._lock:
            cached = self._intraday_cache.get(cache_key)
            if (
                cached is not None
                and now - cached[0] <= self.intraday_cache_seconds
            ):
                return cached[1].copy()
        raw = self.intraday_loader(identifier, board_type)
        frame = pd.DataFrame(raw).copy()
        with self._lock:
            self._intraday_cache[cache_key] = (now, frame.copy())
        return frame

    @staticmethod
    def _normalize_intraday_history(
        frame: Any,
        *,
        observed_at: datetime,
    ) -> pd.DataFrame:
        source = pd.DataFrame(frame).copy()
        if source.empty:
            return pd.DataFrame(columns=["timestamp", "close"])
        timestamp_aliases = (
            "timestamp",
            "datetime",
            "date",
            "\u65e5\u671f\u65f6\u95f4",
            "\u65f6\u95f4",
        )
        close_aliases = ("close", "Close", "\u6536\u76d8")
        timestamp_column = next(
            (name for name in timestamp_aliases if name in source.columns),
            None,
        )
        close_column = next(
            (name for name in close_aliases if name in source.columns),
            None,
        )
        if timestamp_column is None or close_column is None:
            return pd.DataFrame(columns=["timestamp", "close"])

        shanghai = ZoneInfo("Asia/Shanghai")
        observed_local = observed_at.astimezone(shanghai)
        cutoff = observed_local.replace(
            minute=(observed_local.minute // 5) * 5,
            second=0,
            microsecond=0,
        )
        rows = []
        for raw_timestamp, raw_close in zip(
            source[timestamp_column],
            source[close_column],
        ):
            parsed_timestamp = pd.to_datetime(raw_timestamp, errors="coerce")
            close = _number(raw_close)
            if pd.isna(parsed_timestamp) or close is None or close <= 0:
                continue
            value = parsed_timestamp.to_pydatetime()
            if value.tzinfo is None:
                value = value.replace(tzinfo=shanghai)
            else:
                value = value.astimezone(shanghai)
            if value.date() != observed_local.date() or value > cutoff:
                continue
            rows.append({"timestamp": value, "close": close})
        if not rows:
            return pd.DataFrame(columns=["timestamp", "close"])
        return (
            pd.DataFrame(rows)
            .sort_values("timestamp")
            .drop_duplicates("timestamp", keep="last")
        )

    @staticmethod
    def rotation_reference_group(name: str) -> Optional[str]:
        normalized = str(name or "").strip().lower()
        for group, keywords in ROTATION_REFERENCE_KEYWORDS.items():
            if any(keyword in normalized for keyword in keywords):
                return group
        return None

    @staticmethod
    def build_rotation_signal(reference_results: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
        rows = [item for item in reference_results if isinstance(item, dict)]
        pressure_groups = sorted({
            str(item.get("rotation_group") or "")
            for item in rows
            if item.get("near_resistance") is True
            and str(item.get("rotation_group") or "")
        })
        return {
            "available": bool(rows),
            "tailwind": bool(pressure_groups),
            "reason": (
                "defensive_rotation_boards_near_resistance"
                if pressure_groups
                else "rotation_pressure_not_confirmed"
            ),
            "pressure_groups": pressure_groups,
            "references": rows,
        }

    def _history(
        self,
        *,
        identifier: str,
        board_type: str,
        start_date: str,
        end_date: str,
    ) -> pd.DataFrame:
        cache_key = (identifier, str(board_type or "").lower(), end_date)
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(cache_key)
            if cached is not None and now - cached[0] <= self.cache_seconds:
                return cached[1].copy()
        raw = self.history_loader(identifier, board_type, start_date, end_date)
        frame = pd.DataFrame(raw).copy()
        with self._lock:
            self._cache[cache_key] = (now, frame.copy())
        return frame

    @staticmethod
    def _normalize_history(frame: Any, *, completed_through: Any) -> pd.DataFrame:
        source = pd.DataFrame(frame).copy()
        if source.empty:
            return pd.DataFrame(columns=["date", "close", "high", "low"])
        aliases = {
            "date": ("date", "\u65e5\u671f"),
            "close": ("close", "Close", "\u6536\u76d8"),
            "high": ("high", "High", "\u6700\u9ad8"),
            "low": ("low", "Low", "\u6700\u4f4e"),
        }
        selected: Dict[str, Any] = {}
        for target, names in aliases.items():
            column = next((name for name in names if name in source.columns), None)
            if column is None:
                return pd.DataFrame(columns=["date", "close", "high", "low"])
            selected[target] = source[column]
        normalized = pd.DataFrame(selected)
        normalized["date"] = pd.to_datetime(normalized["date"], errors="coerce")
        for column in ("close", "high", "low"):
            normalized[column] = pd.to_numeric(normalized[column], errors="coerce")
        cutoff = pd.Timestamp(completed_through)
        normalized = normalized[
            (normalized["date"].notna())
            & (normalized["date"] <= cutoff)
            & (normalized[["close", "high", "low"]] > 0).all(axis=1)
        ]
        return normalized.sort_values("date").drop_duplicates("date", keep="last")
