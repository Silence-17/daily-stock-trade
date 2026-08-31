# -*- coding: utf-8 -*-
"""Runtime evidence collection for the aggressive cross-market strategy."""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from data_provider.base import DataFetcherManager
from src.core import trading_calendar
from src.services.cross_market_paper_strategy import (
    CrossMarketSignalEngine,
    KR_LINKED_THEMES,
    KoreaSignalSnapshot,
    STRATEGY_ID,
    TECHNOLOGY_WEIGHTED_THEMES,
    TimedMarketObservation,
)

logger = logging.getLogger(__name__)

DEFAULT_STATE_PATH = Path("data") / "cross_market_strategy_state.json"
KOREA_EVIDENCE_CODES = ("KS11", "KQ11", "005930.KS", "000660.KS")
KOREA_LIVE_PROVIDER_CLOCK_SKEW_MAX_SECONDS = 3.0
KOREA_LIVE_PROVIDER_CLOCK_SKEW_MARGIN_SECONDS = 0.05
JAPAN_EVIDENCE_CODES = ("N225", "TOPX")
US_TECH_EVIDENCE_CODES = ("SMH", "SOXX", "MU", "WDC", "IXIC")
US_TECH_COMPONENT_GROUPS = {
    "semiconductor": ("SMH", "SOXX"),
    "memory": ("MU", "WDC"),
}
NASDAQ_FUTURES_CODE = "NQ00Y"
NASDAQ_FUTURES_STATE_KEY = "nasdaq_futures_snapshots"
NASDAQ_FUTURES_CAPTURE_THROTTLE_SECONDS = 55
US_PREMARKET_CAPTURE_ATTEMPTS_STATE_KEY = "us_premarket_capture_attempts"
US_CLOSE_THEME_CAPTURE_ATTEMPTS_STATE_KEY = "us_close_theme_capture_attempts"
CPO_US_EVIDENCE_CODES = ("COHR", "LITE", "CIEN")
# Each ticker has one primary basket so cross-listed themes cannot silently give a
# company extra weight. Theme scores are normalized independently below.
US_PREMARKET_THEME_CODES = {
    "benchmark": ("QQQ", "AAPL"),
    "semiconductor": (
        "SMH", "SOXX", "NVDA", "INTC", "AMD", "AVGO", "MRVL", "QCOM",
        "TSM", "ARM", "SNPS", "CDNS", "ALAB",
    ),
    "semiconductor_equipment": ("ASML", "AMAT", "LRCX", "KLAC", "TER"),
    "semiconductor_materials": ("ENTG", "MKSI", "UCTT", "ICHR", "FORM"),
    "storage": ("MU", "SNDK", "WDC", "STX", "SIMO", "NTAP", "P"),
    "cpo": (
        "COHR", "LITE", "CIEN", "AAOI", "FN", "GLW", "ANET", "CSCO",
        "CRDO", "APH", "CLS",
    ),
    "artificial_intelligence": (
        "MSFT", "GOOGL", "AMZN", "META", "ORCL", "IBM", "PLTR", "SNOW",
        "NOW", "CRM", "NET",
    ),
    "compute_services": (
        "CRWV", "NBIS", "APLD", "IREN", "SMCI", "DELL", "HPE", "VRT",
        "EQIX", "DLR",
    ),
    "gaming": ("TTWO", "EA", "RBLX", "NTES", "U"),
    "pharma": (
        "XLV", "XBI", "IBB", "XPH", "LLY", "JNJ", "MRK", "ABBV",
        "PFE", "AMGN", "GILD", "REGN", "VRTX", "BMY",
    ),
    "ccl": ("TTMI", "ROG", "DD", "JBL", "SANM", "FLEX"),
    "mlcc": ("TTDKY", "MRAAY"),
}
US_PREMARKET_A_SHARE_THEME_MAP = {
    "semiconductor": "semiconductor",
    "equipment": "semiconductor_equipment",
    "materials": "semiconductor_materials",
    "memory": "storage",
    "cpo": "cpo",
    "artificial_intelligence": "artificial_intelligence",
    "compute_services": "compute_services",
    "gaming": "gaming",
    "pharma": "pharma",
    "ccl": "ccl",
    "mlcc": "mlcc",
}
ASIA_THEME_EVIDENCE_CODES = {
    "mlcc": ("6981.T", "6762.T", "6976.T", "009150.KS", "2327.TW"),
    "ccl": ("2383.TW", "6274.TWO", "6213.TW", "1303.TW", "3037.TW"),
}
US_PREMARKET_MIN_COVERAGE_RATIO = 0.60
US_PREMARKET_MIN_COMPONENTS = 3
US_THEME_COLLECTION_MAX_WORKERS = 16
ASIA_CLOSE_CARRY_TOLERANCE_SECONDS = 5 * 60
ASIA_CLOSE_PROVIDER_GRACE_SECONDS = 60
JAPAN_LUNCH_CARRY_MAX_AGE_SECONDS = 45 * 60
CPO_NEWS_POSITIVE_TERMS = (
    "optical transceiver",
    "optical module",
    "silicon photonics",
    "co-packaged optics",
    "cpo",
    "800g",
    "1.6t",
    "光模块",
    "光通信",
    "硅光",
    "共封装光学",
)
CPO_NEWS_NEGATIVE_TERMS = (
    "order cut",
    "demand slowdown",
    "export restriction",
    "订单下调",
    "需求放缓",
    "出口限制",
)
GOLD_NEWS_OFFICIAL_TEMPLATE_IDS = (
    "federal-reserve-monetary-policy",
    "federal-reserve-speeches-testimony",
)
GOLD_NEWS_SUPPLEMENTAL_TEMPLATE_IDS = (
    "newsnow-jin10",
    "newsnow-wallstreetcn-quick",
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _normalized_enum_text(value: Any) -> str:
    return str(getattr(value, "value", value) or "").strip().lower()


def _is_us_close_capture_window(current: datetime) -> bool:
    try:
        _session_open, session_close = trading_calendar.get_market_session_bounds(
            "us",
            current_time=current,
            strict=True,
        )
    except Exception as exc:  # noqa: BLE001 - execution evidence must fail closed.
        logger.warning("Failed to resolve the current US session close: %s", exc)
        return False
    if session_close is None:
        return False
    elapsed_seconds = (
        current.astimezone(timezone.utc)
        - session_close.astimezone(timezone.utc)
    ).total_seconds()
    return 0 <= elapsed_seconds <= 180


class CrossMarketSignalService:
    """Collect, persist and evaluate point-in-time cross-market evidence."""

    _lock = threading.RLock()

    def __init__(
        self,
        *,
        data_fetcher_manager: Optional[DataFetcherManager] = None,
        engine: Optional[CrossMarketSignalEngine] = None,
        state_path: Optional[Path] = None,
        max_runtime_snapshots: int = 600,
        intelligence_service: Optional[Any] = None,
    ) -> None:
        self.data_fetcher_manager = data_fetcher_manager or DataFetcherManager()
        self.engine = engine or CrossMarketSignalEngine()
        self.state_path = state_path or DEFAULT_STATE_PATH
        self.max_runtime_snapshots = max(5, int(max_runtime_snapshots))
        self.intelligence_service = intelligence_service

    @staticmethod
    def _asia_evidence_market(code: str) -> Optional[str]:
        normalized = str(code or "").strip().upper()
        if normalized in JAPAN_EVIDENCE_CODES or normalized.endswith(".T"):
            return "jp"
        if normalized in KOREA_EVIDENCE_CODES or normalized.endswith((".KS", ".KQ")):
            return "kr"
        if normalized.endswith((".TW", ".TWO")):
            return "tw"
        return None

    def _classify_asia_evidence_time(
        self,
        *,
        code: str,
        provider_at: datetime,
        now: datetime,
    ) -> Dict[str, Any]:
        current = now.astimezone(timezone.utc)
        provider_time = provider_at.astimezone(timezone.utc)
        age_seconds = (current - provider_time).total_seconds()
        result: Dict[str, Any] = {
            "accepted": False,
            "reason": "evidence_not_fresh",
            "stage": "unavailable",
            "age_seconds": round(age_seconds, 3),
        }
        if age_seconds < -1:
            return {**result, "reason": "evidence_from_future"}

        market = self._asia_evidence_market(code)
        if market is None:
            return {**result, "reason": "evidence_market_unknown"}
        result["market"] = market
        try:
            phase = trading_calendar.infer_market_phase(
                market,
                current_time=current,
            )
            market_open, market_close = trading_calendar.get_market_session_bounds(
                market,
                current_time=current,
                strict=True,
            )
        except Exception as exc:  # noqa: BLE001 - trading evidence fails closed.
            return {
                **result,
                "reason": "evidence_market_calendar_unavailable",
                "error_type": type(exc).__name__,
            }
        result["market_phase"] = _normalized_enum_text(phase)
        if market_open is None or market_close is None:
            return {**result, "reason": "evidence_non_trading_session"}

        market_open_utc = market_open.astimezone(timezone.utc)
        market_close_utc = market_close.astimezone(timezone.utc)
        if not (market_open_utc <= provider_time <= current + timedelta(seconds=1)):
            return {**result, "reason": "evidence_outside_current_session"}

        if phase in {
            trading_calendar.MarketPhase.INTRADAY,
            trading_calendar.MarketPhase.CLOSING_AUCTION,
        }:
            if age_seconds <= self.engine.config.evidence_max_age_seconds:
                return {
                    **result,
                    "accepted": True,
                    "reason": "live_evidence",
                    "stage": "live",
                    "max_age_seconds": self.engine.config.evidence_max_age_seconds,
                }
            return result

        if (
            market == "jp"
            and phase == trading_calendar.MarketPhase.LUNCH_BREAK
            and age_seconds <= JAPAN_LUNCH_CARRY_MAX_AGE_SECONDS
        ):
            try:
                break_start, break_end = (
                    trading_calendar.get_market_session_break_bounds(
                        market,
                        current_time=current,
                        strict=True,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - break carry fails closed.
                return {
                    **result,
                    "reason": "evidence_market_break_calendar_unavailable",
                    "error_type": type(exc).__name__,
                }
            if break_start is None or break_end is None:
                return {**result, "reason": "evidence_market_break_unavailable"}
            break_start_utc = break_start.astimezone(timezone.utc)
            seconds_before_break = (break_start_utc - provider_time).total_seconds()
            if not (
                -ASIA_CLOSE_PROVIDER_GRACE_SECONDS
                <= seconds_before_break
                <= JAPAN_LUNCH_CARRY_MAX_AGE_SECONDS
            ):
                return {
                    **result,
                    "reason": "provider_time_not_near_session_break",
                }
            return {
                **result,
                "accepted": True,
                "reason": "japan_session_break_close_evidence",
                "stage": "session_break_carry",
                "max_age_seconds": JAPAN_LUNCH_CARRY_MAX_AGE_SECONDS,
                "session_break_at": break_start_utc.isoformat(),
                "valid_until": break_end.astimezone(timezone.utc).isoformat(),
                "seconds_before_session_break": round(seconds_before_break, 3),
            }

        if phase != trading_calendar.MarketPhase.POSTMARKET:
            return {**result, "reason": "evidence_outside_supported_market_phase"}

        try:
            cn_open, cn_close = trading_calendar.get_market_session_bounds(
                "cn",
                current_time=current,
                strict=True,
            )
        except Exception as exc:  # noqa: BLE001 - close carry must fail closed.
            return {
                **result,
                "reason": "cn_market_calendar_unavailable",
                "error_type": type(exc).__name__,
            }
        if cn_open is None or cn_close is None:
            return {**result, "reason": "cn_non_trading_session"}
        cn_open_utc = cn_open.astimezone(timezone.utc)
        cn_close_utc = cn_close.astimezone(timezone.utc)
        if not (cn_open_utc <= current < cn_close_utc):
            return {**result, "reason": "same_session_close_carry_expired"}
        if current < market_close_utc:
            return result

        seconds_before_close = (market_close_utc - provider_time).total_seconds()
        if not (
            -ASIA_CLOSE_PROVIDER_GRACE_SECONDS
            <= seconds_before_close
            <= ASIA_CLOSE_CARRY_TOLERANCE_SECONDS
        ):
            return {**result, "reason": "provider_time_not_near_session_close"}
        return {
            **result,
            "accepted": True,
            "reason": "same_session_official_close_evidence",
            "stage": "same_session_close",
            "session_close_at": market_close_utc.isoformat(),
            "valid_until": cn_close_utc.isoformat(),
            "seconds_before_session_close": round(seconds_before_close, 3),
        }

    @staticmethod
    def _asia_theme_signal_from_components(
        *,
        codes: tuple[str, ...],
        components: Dict[str, Dict[str, Any]],
    ) -> Dict[str, Any]:
        available_codes = [code for code in codes if code in components]
        required_count = max(3, math.ceil(len(codes) * 0.60))
        stage_counts: Dict[str, int] = {}
        for code in available_codes:
            stage = str(components[code].get("evidence_stage") or "unknown")
            stage_counts[stage] = stage_counts.get(stage, 0) + 1
        if len(available_codes) < required_count:
            return {
                "available": False,
                "strong": False,
                "reason": "asia_theme_coverage_insufficient",
                "available_codes": available_codes,
                "missing_codes": [code for code in codes if code not in components],
                "required_count": required_count,
                "evidence_stage_counts": stage_counts,
            }
        changes = [float(components[code]["change_pct"]) for code in available_codes]
        mean_change = sum(changes) / len(changes)
        advancing_ratio = sum(1 for value in changes if value > 0) / len(changes)
        score = max(
            -100.0,
            min(100.0, mean_change / 1.5 * 70.0 + advancing_ratio * 30.0),
        )
        strong = bool(
            mean_change >= 0.3
            and advancing_ratio >= 0.6
            and score >= 40.0
        )
        return {
            "available": True,
            "strong": strong,
            "reason": "asia_theme_strong" if strong else "asia_theme_not_strong",
            "score": round(score, 6),
            "sector_change_pct": round(mean_change, 6),
            "advancing_ratio": round(advancing_ratio, 6),
            "leader_change_pct": round(max(changes), 6),
            "available_codes": available_codes,
            "missing_codes": [code for code in codes if code not in components],
            "required_count": required_count,
            "evidence_stage_counts": stage_counts,
        }

    def collect_korea_snapshot(self, *, now: Optional[datetime] = None) -> Dict[str, Any]:
        live_collection = now is None
        collection_started_at = (now or _utc_now()).astimezone(timezone.utc)
        observations: List[TimedMarketObservation] = []
        component_quotes: Dict[str, tuple[Any, datetime, float]] = {}
        for code in KOREA_EVIDENCE_CODES:
            quote = self._get_timestamped_quote(code)
            if quote is None:
                raise ValueError(f"korea_quote_unavailable:{code}")
            provider_at = _parse_datetime(getattr(quote, "provider_timestamp", None))
            if provider_at is None:
                raise ValueError(f"korea_provider_timestamp_required:{code}")
            change_pct = getattr(quote, "change_pct", None)
            try:
                normalized_change_pct = float(change_pct)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"korea_evidence_invalid:{code}") from exc
            component_quotes[code] = (quote, provider_at, normalized_change_pct)

        # Live providers timestamp each sequential response near request completion.
        # Validate the whole basket against collection completion, not its start.
        collected_at = (
            collection_started_at
            if now is not None
            else _utc_now().astimezone(timezone.utc)
        )
        if live_collection:
            latest_provider_at = max(item[1] for item in component_quotes.values())
            provider_clock_skew_seconds = (
                latest_provider_at - collected_at
            ).total_seconds()
            if (
                1.0 < provider_clock_skew_seconds
                <= KOREA_LIVE_PROVIDER_CLOCK_SKEW_MAX_SECONDS
            ):
                time.sleep(
                    provider_clock_skew_seconds
                    - 1.0
                    + KOREA_LIVE_PROVIDER_CLOCK_SKEW_MARGIN_SECONDS
                )
                collected_at = _utc_now().astimezone(timezone.utc)
        raw_components: Dict[str, Dict[str, Any]] = {}
        for code in KOREA_EVIDENCE_CODES:
            quote, provider_at, change_pct = component_quotes[code]
            observation = TimedMarketObservation(
                code=code,
                change_pct=change_pct,
                observed_at=collected_at,
                provider_timestamp=provider_at,
            )
            observations.append(observation)
            source = getattr(quote, "source", None)
            raw_components[code] = {
                "change_pct": change_pct,
                "price": getattr(quote, "price", None),
                "provider_timestamp": provider_at.isoformat(),
                "fetched_at": getattr(quote, "fetched_at", None),
                "source": getattr(source, "value", source),
                "data_quality": getattr(quote, "data_quality", None),
            }

        snapshot = self.engine.build_korea_snapshot(observations, now=collected_at)
        record = {
            "observed_at": snapshot.observed_at.astimezone(timezone.utc).isoformat(),
            "collection_started_at": collection_started_at.isoformat(),
            "collection_duration_seconds": round(
                max(0.0, (collected_at - collection_started_at).total_seconds()),
                6,
            ),
            "session_date": collected_at.astimezone(ZoneInfo("Asia/Seoul")).date().isoformat(),
            "score": snapshot.score,
            "components": raw_components,
        }
        self._append_korea_snapshot(record)
        return record

    def collect_korea_snapshot_if_open(
        self,
        *,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Collect one snapshot only while the Korean cash market is open."""

        live_collection = now is None
        current = (now or _utc_now()).astimezone(timezone.utc)
        try:
            phase = trading_calendar.build_market_phase_context(
                market="kr",
                current_time=current,
                trigger_source="cross_market_korea_signal",
                analysis_intent="auto",
            )
        except Exception as exc:  # noqa: BLE001 - automatic evidence must fail closed.
            logger.warning("Failed to resolve Korean market phase: %s", exc)
            return {
                "accepted": False,
                "skipped": True,
                "reason": "korea_market_phase_unknown",
                "strategy_id": STRATEGY_ID,
                "error_type": type(exc).__name__,
            }
        if phase.is_market_open_now is not True:
            return {
                "accepted": True,
                "skipped": True,
                "reason": "outside_korea_trading_session",
                "strategy_id": STRATEGY_ID,
                "market_phase": phase.to_dict(),
            }
        snapshot = self.collect_korea_snapshot(
            now=None if live_collection else current,
        )
        return {
            "accepted": True,
            "skipped": False,
            "reason": "korea_snapshot_collected",
            "strategy_id": STRATEGY_ID,
            "market_phase": phase.to_dict(),
            "snapshot": snapshot,
        }

    def collect_japan_snapshot(self, *, now: Optional[datetime] = None) -> Dict[str, Any]:
        """Collect fresh Nikkei 225 and TOPIX direction for the CN session."""

        collection_started_at = (now or _utc_now()).astimezone(timezone.utc)
        quote_rows: Dict[str, tuple[Any, datetime, float]] = {}
        for code in JAPAN_EVIDENCE_CODES:
            quote = self._get_timestamped_quote(code)
            if quote is None:
                raise ValueError(f"japan_quote_unavailable:{code}")
            provider_at = _parse_datetime(getattr(quote, "provider_timestamp", None))
            if provider_at is None:
                raise ValueError(f"japan_provider_timestamp_required:{code}")
            try:
                change_pct = float(getattr(quote, "change_pct", None))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"japan_evidence_invalid:{code}") from exc
            quote_rows[code] = (quote, provider_at, change_pct)
        collected_at = (
            collection_started_at
            if now is not None
            else _utc_now().astimezone(timezone.utc)
        )
        components: Dict[str, Dict[str, Any]] = {}
        changes = []
        evidence_stages = set()
        for code in JAPAN_EVIDENCE_CODES:
            quote, provider_at, change_pct = quote_rows[code]
            timing = self._classify_asia_evidence_time(
                code=code,
                provider_at=provider_at,
                now=collected_at,
            )
            if timing.get("reason") == "evidence_from_future":
                raise ValueError(f"japan_evidence_from_future:{code}")
            if timing.get("accepted") is not True:
                raise ValueError(f"japan_evidence_stale:{code}")
            source = getattr(quote, "source", None)
            changes.append(change_pct)
            evidence_stages.add(str(timing.get("stage") or "unknown"))
            components[code] = {
                "name": getattr(quote, "name", None),
                "change_pct": round(change_pct, 6),
                "price": getattr(quote, "price", None),
                "provider_timestamp": provider_at.isoformat(),
                "source": getattr(source, "value", source),
                "data_quality": getattr(quote, "data_quality", None),
                "proxy_instrument": getattr(quote, "proxy_instrument", None),
                "evidence_stage": timing.get("stage"),
                "evidence_age_seconds": timing.get("age_seconds"),
                "session_break_at": timing.get("session_break_at"),
                "session_close_at": timing.get("session_close_at"),
                "valid_until": timing.get("valid_until"),
                "seconds_before_session_break": timing.get(
                    "seconds_before_session_break"
                ),
                "seconds_before_session_close": timing.get(
                    "seconds_before_session_close"
                ),
            }
        mean_change = sum(changes) / len(changes)
        advancing_ratio = sum(1 for value in changes if value > 0) / len(changes)
        record = {
            "observed_at": collected_at.isoformat(),
            "collection_started_at": collection_started_at.isoformat(),
            "collection_duration_seconds": round(
                max(0.0, (collected_at - collection_started_at).total_seconds()),
                6,
            ),
            "session_date": collected_at.astimezone(
                ZoneInfo("Asia/Tokyo")
            ).date().isoformat(),
            "mean_change_pct": round(mean_change, 6),
            "advancing_ratio": round(advancing_ratio, 6),
            "buy_allowed": all(value > 0 for value in changes),
            "evidence_stage": (
                next(iter(evidence_stages))
                if len(evidence_stages) == 1
                else "mixed"
            ),
            "components": components,
        }
        self._append_snapshot("japan_snapshots", record)
        return record

    def collect_japan_snapshot_if_open(
        self,
        *,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        live_collection = now is None
        current = (now or _utc_now()).astimezone(timezone.utc)
        try:
            phase = trading_calendar.build_market_phase_context(
                market="jp",
                current_time=current,
                trigger_source="cross_market_japan_signal",
                analysis_intent="auto",
            )
        except Exception as exc:  # noqa: BLE001 - automatic evidence fails closed.
            return {
                "accepted": False,
                "skipped": True,
                "reason": "japan_market_phase_unknown",
                "strategy_id": STRATEGY_ID,
                "error_type": type(exc).__name__,
            }
        if phase.is_market_open_now is not True:
            return {
                "accepted": True,
                "skipped": True,
                "reason": "outside_japan_trading_session",
                "strategy_id": STRATEGY_ID,
                "market_phase": phase.to_dict(),
            }
        snapshot = self.collect_japan_snapshot(
            now=None if live_collection else current,
        )
        return {
            "accepted": True,
            "skipped": False,
            "reason": "japan_snapshot_collected",
            "strategy_id": STRATEGY_ID,
            "market_phase": phase.to_dict(),
            "snapshot": snapshot,
        }

    def collect_asia_theme_snapshot(
        self,
        *,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Collect live Asian MLCC and CCL leaders during the A-share day."""

        collection_started_at = (now or _utc_now()).astimezone(timezone.utc)
        unique_codes = sorted({
            code
            for codes in ASIA_THEME_EVIDENCE_CODES.values()
            for code in codes
        })
        quotes: Dict[str, Any] = {}
        errors: Dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=min(8, len(unique_codes))) as executor:
            futures = {
                executor.submit(self._get_timestamped_quote, code): code
                for code in unique_codes
            }
            for future in as_completed(futures):
                code = futures[future]
                try:
                    quotes[code] = future.result()
                except Exception as exc:  # noqa: BLE001 - components fail independently.
                    errors[code] = str(exc) or type(exc).__name__
        collected_at = (
            collection_started_at
            if now is not None
            else _utc_now().astimezone(timezone.utc)
        )
        components: Dict[str, Dict[str, Any]] = {}
        for code, quote in quotes.items():
            try:
                if quote is None:
                    raise ValueError("quote_unavailable")
                provider_at = _parse_datetime(
                    getattr(quote, "provider_timestamp", None)
                )
                if provider_at is None:
                    raise ValueError("provider_timestamp_required")
                timing = self._classify_asia_evidence_time(
                    code=code,
                    provider_at=provider_at,
                    now=collected_at,
                )
                if timing.get("accepted") is not True:
                    raise ValueError(str(timing.get("reason") or "evidence_not_fresh"))
                change_pct = float(getattr(quote, "change_pct", None))
                if not math.isfinite(change_pct):
                    raise ValueError("change_pct_invalid")
                source = getattr(quote, "source", None)
                components[code] = {
                    "change_pct": round(change_pct, 6),
                    "provider_timestamp": provider_at.isoformat(),
                    "source": getattr(source, "value", source),
                    "evidence_stage": timing.get("stage"),
                    "evidence_age_seconds": timing.get("age_seconds"),
                    "session_break_at": timing.get("session_break_at"),
                    "session_close_at": timing.get("session_close_at"),
                    "valid_until": timing.get("valid_until"),
                    "seconds_before_session_break": timing.get(
                        "seconds_before_session_break"
                    ),
                    "seconds_before_session_close": timing.get(
                        "seconds_before_session_close"
                    ),
                }
            except Exception as exc:  # noqa: BLE001 - components fail independently.
                errors[code] = str(exc) or type(exc).__name__
        themes: Dict[str, Dict[str, Any]] = {}
        for theme, codes in ASIA_THEME_EVIDENCE_CODES.items():
            themes[theme] = self._asia_theme_signal_from_components(
                codes=codes,
                components=components,
            )
        record = {
            "observed_at": collected_at.isoformat(),
            "collection_started_at": collection_started_at.isoformat(),
            "collection_duration_seconds": round(
                max(0.0, (collected_at - collection_started_at).total_seconds()),
                6,
            ),
            "session_date": collected_at.astimezone(
                ZoneInfo("Asia/Shanghai")
            ).date().isoformat(),
            "themes": themes,
            "components": components,
            "component_errors": errors,
        }
        self._append_snapshot("asia_theme_snapshots", record)
        return record

    def collect_asia_theme_snapshot_if_cn_open(
        self,
        *,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Collect supply-chain evidence throughout the A-share cash session."""

        live_collection = now is None
        current = (now or _utc_now()).astimezone(timezone.utc)
        try:
            phase = trading_calendar.build_market_phase_context(
                market="cn",
                current_time=current,
                trigger_source="cross_market_asia_theme_signal",
                analysis_intent="auto",
            )
        except Exception as exc:  # noqa: BLE001 - automatic evidence fails closed.
            return {
                "accepted": False,
                "skipped": True,
                "reason": "cn_market_phase_unknown",
                "strategy_id": STRATEGY_ID,
                "error_type": type(exc).__name__,
            }
        if phase.is_market_open_now is not True:
            return {
                "accepted": True,
                "skipped": True,
                "reason": "outside_cn_trading_session",
                "strategy_id": STRATEGY_ID,
                "market_phase": phase.to_dict(),
            }
        snapshot = self.collect_asia_theme_snapshot(
            now=None if live_collection else current,
        )
        return {
            "accepted": True,
            "skipped": False,
            "reason": "asia_theme_snapshot_collected",
            "strategy_id": STRATEGY_ID,
            "market_phase": phase.to_dict(),
            "snapshot": snapshot,
        }

    def collect_us_tech_snapshot(
        self,
        *,
        now: Optional[datetime] = None,
        session_stage: str = "intraday",
    ) -> Dict[str, Any]:
        collection_started_at = (now or _utc_now()).astimezone(timezone.utc)
        changes: Dict[str, float] = {}
        component_quotes: Dict[str, tuple[Any, datetime]] = {}
        raw_components: Dict[str, Dict[str, Any]] = {}
        for code in US_TECH_EVIDENCE_CODES:
            quote = self._get_timestamped_us_quote(code)
            if quote is None:
                raise ValueError(f"us_tech_quote_unavailable:{code}")
            provider_at = _parse_datetime(getattr(quote, "provider_timestamp", None))
            if provider_at is None:
                raise ValueError(f"us_tech_provider_timestamp_required:{code}")
            component_quotes[code] = (quote, provider_at)

        # Quotes arrive sequentially and may carry timestamps near each response.
        # Live freshness therefore uses basket completion rather than request start.
        collected_at = (
            collection_started_at
            if now is not None
            else _utc_now().astimezone(timezone.utc)
        )
        for code in US_TECH_EVIDENCE_CODES:
            quote, provider_at = component_quotes[code]
            age_seconds = (collected_at - provider_at).total_seconds()
            if age_seconds < -1:
                raise ValueError(f"us_tech_evidence_from_future:{code}")
            if age_seconds > self.engine.config.evidence_max_age_seconds:
                raise ValueError(f"us_tech_evidence_stale:{code}")
            change_pct = getattr(quote, "change_pct", None)
            try:
                changes[code] = float(change_pct)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"us_tech_evidence_invalid:{code}") from exc
            source = getattr(quote, "source", None)
            raw_components[code] = {
                "change_pct": changes[code],
                "price": getattr(quote, "price", None),
                "provider_timestamp": provider_at.isoformat(),
                "fetched_at": getattr(quote, "fetched_at", None),
                "source": getattr(source, "value", source),
                "data_quality": getattr(quote, "data_quality", None),
            }

        semiconductor = sum(changes[code] for code in US_TECH_COMPONENT_GROUPS["semiconductor"]) / 2.0
        memory = sum(changes[code] for code in US_TECH_COMPONENT_GROUPS["memory"]) / 2.0
        technology_components = [changes[code] for code in US_TECH_EVIDENCE_CODES if code != "IXIC"]
        first_hour = sum(technology_components) / len(technology_components)
        advancing_ratio = sum(1 for value in technology_components if value > 0) / len(technology_components)
        score = self.engine.calculate_us_tech_score(
            first_hour_change_pct=first_hour,
            semiconductor_change_pct=semiconductor,
            memory_basket_change_pct=memory,
            nasdaq_change_pct=changes["IXIC"],
            advancing_ratio=advancing_ratio,
        )
        stage = str(session_stage or "intraday").strip().lower()
        if stage not in {"first_hour", "intraday", "close"}:
            raise ValueError("invalid_us_session_stage")
        record = {
            "observed_at": collected_at.isoformat(),
            "collection_started_at": collection_started_at.isoformat(),
            "collection_duration_seconds": round(
                max(0.0, (collected_at - collection_started_at).total_seconds()),
                6,
            ),
            "session_date": collected_at.astimezone(ZoneInfo("America/New_York")).date().isoformat(),
            "session_stage": stage,
            "score": score,
            "semiconductor_change_pct": round(semiconductor, 6),
            "memory_basket_change_pct": round(memory, 6),
            "nasdaq_change_pct": round(changes["IXIC"], 6),
            "advancing_ratio": round(advancing_ratio, 6),
            "components": raw_components,
        }
        self._append_snapshot("us_tech_snapshots", record)
        return record

    def collect_nasdaq_futures_snapshot(
        self,
        *,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Freeze one fresh NQ00Y quote for A-share technology decisions."""

        collection_started_at = (now or _utc_now()).astimezone(timezone.utc)
        quote = self._get_timestamped_quote(NASDAQ_FUTURES_CODE)
        if quote is None:
            raise ValueError("nasdaq_futures_quote_unavailable")
        provider_at = _parse_datetime(getattr(quote, "provider_timestamp", None))
        if provider_at is None:
            raise ValueError("nasdaq_futures_provider_timestamp_required")
        collected_at = (
            collection_started_at
            if now is not None
            else _utc_now().astimezone(timezone.utc)
        )
        age_seconds = (collected_at - provider_at).total_seconds()
        if age_seconds < -1:
            raise ValueError("nasdaq_futures_evidence_from_future")
        if age_seconds > self.engine.config.evidence_max_age_seconds:
            raise ValueError("nasdaq_futures_evidence_stale")
        try:
            price = float(getattr(quote, "price", None))
            change_pct = float(getattr(quote, "change_pct", None))
        except (TypeError, ValueError) as exc:
            raise ValueError("nasdaq_futures_evidence_invalid") from exc
        if not math.isfinite(price) or price <= 0 or not math.isfinite(change_pct):
            raise ValueError("nasdaq_futures_evidence_invalid")
        source = getattr(quote, "source", None)
        record = {
            "code": NASDAQ_FUTURES_CODE,
            "name": str(
                getattr(quote, "name", None)
                or "E-mini Nasdaq-100 Continuous"
            ),
            "observed_at": collected_at.isoformat(),
            "provider_timestamp": provider_at.isoformat(),
            "collection_started_at": collection_started_at.isoformat(),
            "collection_duration_seconds": round(
                max(0.0, (collected_at - collection_started_at).total_seconds()),
                6,
            ),
            "session_date": collected_at.astimezone(
                ZoneInfo("Asia/Shanghai")
            ).date().isoformat(),
            "session_stage": "cn_intraday",
            "price": round(price, 6),
            "change_pct": round(change_pct, 6),
            "open_price": getattr(quote, "open_price", None),
            "high": getattr(quote, "high", None),
            "low": getattr(quote, "low", None),
            "pre_close": getattr(quote, "pre_close", None),
            "source": getattr(source, "value", source),
            "data_quality": getattr(quote, "data_quality", None),
        }
        self._append_snapshot(NASDAQ_FUTURES_STATE_KEY, record)
        return record

    def get_nasdaq_futures_signal_for_cn_trade(
        self,
        *,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        current = (now or _utc_now()).astimezone(timezone.utc)
        session_date = current.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
        snapshots_by_provider: Dict[datetime, Dict[str, Any]] = {}
        for item in self._read_state().get(NASDAQ_FUTURES_STATE_KEY, []):
            if not isinstance(item, dict):
                continue
            if str(item.get("session_date") or "") != session_date:
                continue
            observed_at = _parse_datetime(item.get("observed_at"))
            provider_at = _parse_datetime(item.get("provider_timestamp"))
            price = item.get("price")
            change_pct = item.get("change_pct")
            if (
                observed_at is None
                or provider_at is None
                or observed_at > current
                or provider_at > current + timedelta(seconds=1)
            ):
                continue
            try:
                normalized_price = float(price)
                normalized_change = float(change_pct)
            except (TypeError, ValueError):
                continue
            if (
                not math.isfinite(normalized_price)
                or normalized_price <= 0
                or not math.isfinite(normalized_change)
            ):
                continue
            snapshots_by_provider[provider_at] = {
                **item,
                "_observed_at": observed_at,
                "_provider_at": provider_at,
                "_price": normalized_price,
                "_change_pct": normalized_change,
            }
        snapshots = sorted(
            snapshots_by_provider.values(),
            key=lambda item: item["_provider_at"],
        )
        if not snapshots:
            return {
                "available": False,
                "confirmed": False,
                "buy_allowed": False,
                "sell_fraction": 0.0,
                "reason": "nasdaq_futures_signal_unavailable",
                "strategy_id": STRATEGY_ID,
                "code": NASDAQ_FUTURES_CODE,
                "session_date": session_date,
            }
        latest = snapshots[-1]
        latest_provider_at = latest["_provider_at"]
        latest_age_seconds = (current - latest_provider_at).total_seconds()
        if latest_age_seconds > self.engine.config.evidence_max_age_seconds:
            return {
                "available": False,
                "confirmed": False,
                "buy_allowed": False,
                "sell_fraction": 0.0,
                "reason": "nasdaq_futures_signal_stale",
                "strategy_id": STRATEGY_ID,
                "code": NASDAQ_FUTURES_CODE,
                "session_date": session_date,
                "latest_provider_timestamp": latest_provider_at.isoformat(),
                "latest_age_seconds": round(latest_age_seconds, 3),
            }
        window_start = latest_provider_at - timedelta(
            minutes=self.engine.config.nasdaq_futures_trend_window_minutes
        )
        trend_samples = [
            item
            for item in snapshots
            if item["_provider_at"] >= window_start
        ]
        earliest = trend_samples[0]
        span_seconds = (
            latest_provider_at - earliest["_provider_at"]
        ).total_seconds()
        confirmed = bool(
            len(trend_samples)
            >= self.engine.config.nasdaq_futures_confirmation_samples
            and span_seconds
            >= self.engine.config.nasdaq_futures_confirmation_duration_seconds
        )
        trend_change_pct = (
            (latest["_price"] / earliest["_price"] - 1.0) * 100.0
            if confirmed
            else None
        )
        session_change_pct = float(latest["_change_pct"])
        strong_down = bool(
            confirmed
            and (
                session_change_pct
                <= self.engine.config.nasdaq_futures_buy_block_change_pct
                or float(trend_change_pct or 0.0)
                <= self.engine.config.nasdaq_futures_buy_block_trend_pct
            )
        )
        severe_down = bool(
            confirmed
            and (
                session_change_pct
                <= self.engine.config.nasdaq_futures_reduce_change_pct
                or float(trend_change_pct or 0.0)
                <= self.engine.config.nasdaq_futures_reduce_trend_pct
            )
        )
        session_score = max(
            -100.0,
            min(100.0, session_change_pct / 1.5 * 100.0),
        )
        trend_score = (
            max(-100.0, min(100.0, float(trend_change_pct) / 0.5 * 100.0))
            if trend_change_pct is not None
            else 0.0
        )
        sector_score_adjustment = (
            max(-5.0, min(5.0, (0.7 * session_score + 0.3 * trend_score) * 0.05))
            if confirmed
            else 0.0
        )
        directional_score = 0.7 * session_score + 0.3 * trend_score
        strength_score = max(0.0, min(100.0, (directional_score + 100.0) / 2.0))
        if not confirmed:
            reason = "nasdaq_futures_trend_unconfirmed"
        elif severe_down:
            reason = "nasdaq_futures_severe_downtrend"
        elif strong_down:
            reason = "nasdaq_futures_downtrend_blocks_entry"
        else:
            reason = "nasdaq_futures_trend_confirmed"
        return {
            "available": True,
            "confirmed": confirmed,
            "buy_allowed": bool(confirmed and not strong_down),
            "sell_fraction": (
                self.engine.config.nasdaq_futures_reduce_fraction
                if severe_down
                else 0.0
            ),
            "reason": reason,
            "strategy_id": STRATEGY_ID,
            "code": NASDAQ_FUTURES_CODE,
            "name": latest.get("name"),
            "session_date": session_date,
            "observed_at": latest["_observed_at"].isoformat(),
            "provider_timestamp": latest_provider_at.isoformat(),
            "latest_age_seconds": round(latest_age_seconds, 3),
            "price": round(float(latest["_price"]), 6),
            "session_change_pct": round(session_change_pct, 6),
            "trend_change_pct": (
                round(float(trend_change_pct), 6)
                if trend_change_pct is not None
                else None
            ),
            "directional_score": round(directional_score, 6),
            "score": round(strength_score, 6),
            "trend_window_minutes": (
                self.engine.config.nasdaq_futures_trend_window_minutes
            ),
            "confirmation_sample_count": len(trend_samples),
            "confirmation_span_seconds": round(span_seconds, 3),
            "required_confirmation_samples": (
                self.engine.config.nasdaq_futures_confirmation_samples
            ),
            "required_confirmation_duration_seconds": (
                self.engine.config.nasdaq_futures_confirmation_duration_seconds
            ),
            "sector_score_adjustment": round(sector_score_adjustment, 6),
            "thresholds": {
                "buy_block_change_pct": (
                    self.engine.config.nasdaq_futures_buy_block_change_pct
                ),
                "buy_block_trend_pct": (
                    self.engine.config.nasdaq_futures_buy_block_trend_pct
                ),
                "reduce_change_pct": (
                    self.engine.config.nasdaq_futures_reduce_change_pct
                ),
                "reduce_trend_pct": (
                    self.engine.config.nasdaq_futures_reduce_trend_pct
                ),
            },
            "snapshot": {
                key: value
                for key, value in latest.items()
                if not str(key).startswith("_")
            },
        }

    def collect_us_premarket_snapshot(
        self,
        *,
        now: Optional[datetime] = None,
        session_open_at: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Freeze sector baskets shortly before the US regular-session open."""

        collection_started_at = (now or _utc_now()).astimezone(timezone.utc)
        collected_at = collection_started_at
        regular_session_open_at = _parse_datetime(session_open_at)
        components: Dict[str, Dict[str, Any]] = {}
        errors: Dict[str, str] = {}
        unique_codes = sorted({
            code
            for codes in US_PREMARKET_THEME_CODES.values()
            for code in codes
        })

        quote_results: Dict[str, Any] = {}
        raw_stream_results: Dict[str, Any] = {}
        stream_rejections: Dict[str, str] = {}
        bulk_getter = getattr(
            self.data_fetcher_manager,
            "get_cross_market_us_premarket_quotes_with_provider_timestamps",
            None,
        )
        if callable(bulk_getter):
            try:
                bulk_results = bulk_getter(unique_codes)
                if isinstance(bulk_results, dict):
                    raw_stream_results.update(bulk_results)
            except Exception as exc:  # noqa: BLE001 - the per-symbol route still retries gaps.
                errors["_bulk_stream"] = str(exc) or type(exc).__name__
        stream_validation_at = (
            _utc_now().astimezone(timezone.utc)
            if now is None
            else collected_at
        )
        for code, quote in raw_stream_results.items():
            if code not in unique_codes:
                continue
            try:
                if quote is None:
                    raise ValueError("quote_unavailable")
                provider_at = _parse_datetime(
                    getattr(quote, "provider_timestamp", None)
                )
                if provider_at is None:
                    raise ValueError("provider_timestamp_required")
                if (
                    regular_session_open_at is not None
                    and provider_at >= regular_session_open_at
                ):
                    raise ValueError("provider_timestamp_not_premarket")
                age_seconds = (stream_validation_at - provider_at).total_seconds()
                if age_seconds < -1:
                    raise ValueError("evidence_from_future")
                if age_seconds > self.engine.config.evidence_max_age_seconds:
                    raise ValueError("evidence_stale")
                change_pct = float(getattr(quote, "change_pct", None))
                if not math.isfinite(change_pct):
                    raise ValueError("change_pct_invalid")
                quote_results[code] = quote
            except Exception as exc:  # noqa: BLE001 - rejected stream codes enter gap fill.
                stream_rejections[code] = str(exc) or type(exc).__name__
        streamed_quote_count = len(raw_stream_results)
        streamed_component_count = len(quote_results)
        gap_fill_codes = [code for code in unique_codes if code not in quote_results]
        worker_count = min(
            US_THEME_COLLECTION_MAX_WORKERS,
            max(1, len(gap_fill_codes)),
        )
        if gap_fill_codes:
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                futures = {
                    executor.submit(self._get_timestamped_us_premarket_quote, code): code
                    for code in gap_fill_codes
                }
                for future in as_completed(futures):
                    code = futures[future]
                    try:
                        quote_results[code] = future.result()
                    except Exception as exc:  # noqa: BLE001 - components fail independently.
                        errors[code] = str(exc) or type(exc).__name__

        if now is None:
            collected_at = _utc_now().astimezone(timezone.utc)
        for code in unique_codes:
            if code in errors:
                continue
            try:
                quote = quote_results.get(code)
                if quote is None:
                    raise ValueError("quote_unavailable")
                provider_at = _parse_datetime(
                    getattr(quote, "provider_timestamp", None)
                )
                if provider_at is None:
                    raise ValueError("provider_timestamp_required")
                if (
                    regular_session_open_at is not None
                    and provider_at >= regular_session_open_at
                ):
                    raise ValueError("provider_timestamp_not_premarket")
                age_seconds = (collected_at - provider_at).total_seconds()
                if age_seconds < -1:
                    raise ValueError("evidence_from_future")
                if age_seconds > self.engine.config.evidence_max_age_seconds:
                    raise ValueError("evidence_stale")
                change_pct = float(getattr(quote, "change_pct", None))
                if not math.isfinite(change_pct):
                    raise ValueError("change_pct_invalid")
                source = getattr(quote, "source", None)
                components[code] = {
                    "change_pct": round(change_pct, 6),
                    "price": getattr(quote, "price", None),
                    "provider_timestamp": provider_at.isoformat(),
                    "source": getattr(source, "value", source),
                    "data_quality": getattr(quote, "data_quality", None),
                }
            except Exception as exc:  # noqa: BLE001 - components fail independently.
                errors[code] = str(exc) or type(exc).__name__

        session_date = collected_at.astimezone(
            ZoneInfo("America/New_York")
        ).date().isoformat()
        reused_component_count = self._reuse_fresh_us_theme_components(
            payload=self._read_state(),
            state_keys=(
                US_PREMARKET_CAPTURE_ATTEMPTS_STATE_KEY,
                "us_premarket_snapshots",
            ),
            session_date=session_date,
            session_stage="premarket",
            collected_at=collected_at,
            components=components,
            component_errors=errors,
            regular_session_open_at=regular_session_open_at,
            expected_component_count=len(unique_codes),
        )

        theme_signals: Dict[str, Dict[str, Any]] = {}
        for theme, codes in US_PREMARKET_THEME_CODES.items():
            available_components = [
                components[code]
                for code in codes
                if code in components
            ]
            required_count = min(
                len(codes),
                max(
                    US_PREMARKET_MIN_COMPONENTS,
                    math.ceil(len(codes) * US_PREMARKET_MIN_COVERAGE_RATIO),
                ),
            )
            available_codes = [code for code in codes if code in components]
            missing_codes = [code for code in codes if code not in components]
            coverage_ratio = len(available_components) / len(codes) if codes else 0.0
            if len(available_components) < required_count:
                theme_signals[theme] = {
                    "available": False,
                    "strong": False,
                    "reason": "premarket_theme_coverage_insufficient",
                    "required_codes": list(codes),
                    "required_count": required_count,
                    "available_codes": available_codes,
                    "missing_codes": missing_codes,
                    "coverage_ratio": round(coverage_ratio, 6),
                }
                continue
            changes = [float(item["change_pct"]) for item in available_components]
            sector_change = sum(changes) / len(changes)
            advancing_ratio = sum(1 for value in changes if value > 0) / len(changes)
            leader_change = max(changes)
            direction_score = 100.0 * math.tanh(sector_change / 2.0)
            score = max(
                -100.0,
                min(
                    100.0,
                    0.7 * direction_score
                    + 0.3 * (advancing_ratio * 100.0),
                ),
            )
            strong = bool(
                sector_change
                >= self.engine.config.us_premarket_min_sector_change_pct
                and advancing_ratio
                >= self.engine.config.us_premarket_min_advancing_ratio
                and leader_change
                >= self.engine.config.us_premarket_min_leader_change_pct
                and score >= self.engine.config.us_premarket_strong_score
            )
            theme_signals[theme] = {
                "available": True,
                "strong": strong,
                "reason": (
                    "us_premarket_theme_strong"
                    if strong
                    else "us_premarket_theme_not_strong"
                ),
                "score": round(score, 6),
                "sector_change_pct": round(sector_change, 6),
                "advancing_ratio": round(advancing_ratio, 6),
                "leader_change_pct": round(leader_change, 6),
                "required_count": required_count,
                "coverage_ratio": round(coverage_ratio, 6),
                "codes": available_codes,
                "missing_codes": missing_codes,
            }

        available_theme_count = sum(
            1 for item in theme_signals.values() if item.get("available") is True
        )
        record = {
            "observed_at": collected_at.isoformat(),
            "collection_started_at": collection_started_at.isoformat(),
            "collection_duration_seconds": round(
                max(0.0, (collected_at - collection_started_at).total_seconds()),
                6,
            ),
            "session_date": session_date,
            "session_stage": "premarket",
            "regular_session_open_at": (
                regular_session_open_at.isoformat()
                if regular_session_open_at is not None
                else None
            ),
            "collection_worker_count": worker_count,
            "universe_size": len(unique_codes),
            "streamed_quote_count": streamed_quote_count,
            "streamed_component_count": streamed_component_count,
            "stream_rejected_component_count": len(stream_rejections),
            "stream_rejections": stream_rejections,
            "gap_fill_request_count": len(gap_fill_codes),
            "collected_component_count": len(components),
            "reused_fresh_component_count": reused_component_count,
            "available_theme_count": available_theme_count,
            "snapshot_persisted": available_theme_count > 0,
            "theme_signals": theme_signals,
            "components": components,
            "component_errors": errors,
        }
        self._record_us_theme_capture(
            record,
            attempts_state_key=US_PREMARKET_CAPTURE_ATTEMPTS_STATE_KEY,
            snapshots_state_key="us_premarket_snapshots",
            persist_snapshot=available_theme_count > 0,
        )
        if available_theme_count <= 0:
            raise ValueError("us_premarket_evidence_unavailable")
        return record

    def warm_us_premarket_stream(
        self,
        *,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Start the retained Yahoo stream before the evidence window opens."""

        current = (now or _utc_now()).astimezone(timezone.utc)
        unique_codes = sorted({
            code
            for codes in US_PREMARKET_THEME_CODES.values()
            for code in codes
        })
        bulk_getter = getattr(
            self.data_fetcher_manager,
            "get_cross_market_us_premarket_quotes_with_provider_timestamps",
            None,
        )
        if not callable(bulk_getter):
            return {
                "accepted": True,
                "skipped": True,
                "reason": "us_premarket_stream_warmup_unavailable",
                "strategy_id": STRATEGY_ID,
                "observed_at": current.isoformat(),
                "universe_size": len(unique_codes),
                "streamed_quote_count": 0,
            }
        try:
            quotes = bulk_getter(unique_codes)
        except Exception as exc:  # noqa: BLE001 - warm-up failure is observable, not evidence.
            return {
                "accepted": False,
                "skipped": True,
                "reason": "us_premarket_stream_warmup_failed",
                "strategy_id": STRATEGY_ID,
                "observed_at": current.isoformat(),
                "universe_size": len(unique_codes),
                "streamed_quote_count": 0,
                "error_type": type(exc).__name__,
            }
        return {
            "accepted": True,
            "skipped": True,
            "reason": "us_premarket_stream_warmed",
            "strategy_id": STRATEGY_ID,
            "observed_at": current.isoformat(),
            "universe_size": len(unique_codes),
            "streamed_quote_count": len(quotes) if isinstance(quotes, dict) else 0,
        }

    def collect_us_close_theme_snapshot(
        self,
        *,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Freeze the full confirmed watch universe at the US regular close."""

        collection_started_at = (now or _utc_now()).astimezone(timezone.utc)
        collected_at = collection_started_at
        unique_codes = sorted({
            code
            for codes in US_PREMARKET_THEME_CODES.values()
            for code in codes
        })
        quote_results: Dict[str, Any] = {}
        errors: Dict[str, str] = {}
        worker_count = min(
            US_THEME_COLLECTION_MAX_WORKERS,
            max(1, len(unique_codes)),
        )
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {
                executor.submit(self._get_timestamped_us_quote, code): code
                for code in unique_codes
            }
            for future in as_completed(futures):
                code = futures[future]
                try:
                    quote_results[code] = future.result()
                except Exception as exc:  # noqa: BLE001 - components fail independently.
                    errors[code] = str(exc) or type(exc).__name__

        if now is None:
            collected_at = _utc_now().astimezone(timezone.utc)
        components: Dict[str, Dict[str, Any]] = {}
        for code in unique_codes:
            if code in errors:
                continue
            try:
                quote = quote_results.get(code)
                if quote is None:
                    raise ValueError("quote_unavailable")
                provider_at = _parse_datetime(
                    getattr(quote, "provider_timestamp", None)
                )
                if provider_at is None:
                    raise ValueError("provider_timestamp_required")
                age_seconds = (collected_at - provider_at).total_seconds()
                if age_seconds < -1:
                    raise ValueError("evidence_from_future")
                if age_seconds > self.engine.config.evidence_max_age_seconds:
                    raise ValueError("evidence_stale")
                change_pct = float(getattr(quote, "change_pct", None))
                if not math.isfinite(change_pct):
                    raise ValueError("change_pct_invalid")
                source = getattr(quote, "source", None)
                components[code] = {
                    "change_pct": round(change_pct, 6),
                    "price": getattr(quote, "price", None),
                    "provider_timestamp": provider_at.isoformat(),
                    "source": getattr(source, "value", source),
                    "data_quality": getattr(quote, "data_quality", None),
                }
            except Exception as exc:  # noqa: BLE001 - components fail independently.
                errors[code] = str(exc) or type(exc).__name__

        session_date = collected_at.astimezone(
            ZoneInfo("America/New_York")
        ).date().isoformat()
        reused_component_count = self._reuse_fresh_us_theme_components(
            payload=self._read_state(),
            state_keys=(
                US_CLOSE_THEME_CAPTURE_ATTEMPTS_STATE_KEY,
                "us_close_theme_snapshots",
            ),
            session_date=session_date,
            session_stage="close",
            collected_at=collected_at,
            components=components,
            component_errors=errors,
            expected_component_count=len(unique_codes),
        )

        theme_signals: Dict[str, Dict[str, Any]] = {}
        for theme, codes in US_PREMARKET_THEME_CODES.items():
            available_codes = [code for code in codes if code in components]
            missing_codes = [code for code in codes if code not in components]
            required_count = min(
                len(codes),
                max(
                    US_PREMARKET_MIN_COMPONENTS,
                    math.ceil(len(codes) * US_PREMARKET_MIN_COVERAGE_RATIO),
                ),
            )
            coverage_ratio = len(available_codes) / len(codes) if codes else 0.0
            if len(available_codes) < required_count:
                theme_signals[theme] = {
                    "available": False,
                    "strong": False,
                    "reason": "us_close_theme_coverage_insufficient",
                    "required_codes": list(codes),
                    "required_count": required_count,
                    "available_codes": available_codes,
                    "missing_codes": missing_codes,
                    "coverage_ratio": round(coverage_ratio, 6),
                }
                continue
            changes = [float(components[code]["change_pct"]) for code in available_codes]
            sector_change = sum(changes) / len(changes)
            advancing_ratio = sum(1 for value in changes if value > 0) / len(changes)
            leader_change = max(changes)
            direction_score = 100.0 * math.tanh(sector_change / 2.0)
            score = max(
                -100.0,
                min(
                    100.0,
                    0.7 * direction_score
                    + 0.3 * (advancing_ratio * 100.0),
                ),
            )
            strong = bool(
                sector_change >= self.engine.config.us_close_min_sector_change_pct
                and advancing_ratio >= self.engine.config.us_close_min_advancing_ratio
                and leader_change >= self.engine.config.us_close_min_leader_change_pct
                and score >= self.engine.config.us_close_strong_score
            )
            theme_signals[theme] = {
                "available": True,
                "strong": strong,
                "reason": (
                    "us_close_theme_strong"
                    if strong
                    else "us_close_theme_not_strong"
                ),
                "score": round(score, 6),
                "sector_change_pct": round(sector_change, 6),
                "advancing_ratio": round(advancing_ratio, 6),
                "leader_change_pct": round(leader_change, 6),
                "required_count": required_count,
                "coverage_ratio": round(coverage_ratio, 6),
                "codes": available_codes,
                "missing_codes": missing_codes,
            }

        available_theme_count = sum(
            1 for item in theme_signals.values() if item.get("available") is True
        )
        record = {
            "observed_at": collected_at.isoformat(),
            "collection_started_at": collection_started_at.isoformat(),
            "collection_duration_seconds": round(
                max(0.0, (collected_at - collection_started_at).total_seconds()),
                6,
            ),
            "session_date": session_date,
            "session_stage": "close",
            "collection_worker_count": worker_count,
            "universe_size": len(unique_codes),
            "collected_component_count": len(components),
            "reused_fresh_component_count": reused_component_count,
            "available_theme_count": available_theme_count,
            "snapshot_persisted": available_theme_count > 0,
            "theme_signals": theme_signals,
            "components": components,
            "component_errors": errors,
        }
        self._record_us_theme_capture(
            record,
            attempts_state_key=US_CLOSE_THEME_CAPTURE_ATTEMPTS_STATE_KEY,
            snapshots_state_key="us_close_theme_snapshots",
            persist_snapshot=available_theme_count > 0,
        )
        if available_theme_count <= 0:
            raise ValueError("us_close_theme_evidence_unavailable")
        return record

    def collect_us_tech_snapshot_if_open(
        self,
        *,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        live_collection = now is None
        current = (now or _utc_now()).astimezone(timezone.utc)
        nasdaq_futures_result = self._collect_nasdaq_futures_if_cn_window(
            current=current,
            live_collection=live_collection,
        )
        try:
            phase = trading_calendar.build_market_phase_context(
                market="us",
                current_time=current,
                trigger_source="cross_market_us_tech_signal",
                analysis_intent="auto",
            )
        except Exception as exc:  # noqa: BLE001 - automatic evidence must fail closed.
            logger.warning("Failed to resolve US market phase: %s", exc)
            if nasdaq_futures_result is not None:
                return {
                    **nasdaq_futures_result,
                    "us_market_phase_error": type(exc).__name__,
                }
            return {
                "accepted": False,
                "skipped": True,
                "reason": "us_market_phase_unknown",
                "strategy_id": STRATEGY_ID,
                "error_type": type(exc).__name__,
            }
        local_time = phase.market_local_time.time()
        session_date = str(
            getattr(phase, "session_date", None)
            or phase.market_local_time.date().isoformat()
        )
        collect_tech_snapshot = True
        collect_close_theme_snapshot = False
        phase_name = _normalized_enum_text(getattr(phase, "phase", ""))
        is_trading_day = getattr(phase, "is_trading_day", True) is True
        if (
            is_trading_day
            and phase_name == "premarket"
            and datetime_time(9, 14) <= local_time < datetime_time(9, 15)
        ):
            warmup = self.warm_us_premarket_stream(
                now=None if live_collection else current,
            )
            return {
                **warmup,
                "market_phase": phase.to_dict(),
            }
        if (
            is_trading_day
            and phase_name == "premarket"
            and datetime_time(9, 15) <= local_time < datetime_time(9, 30)
        ):
            if not self._session_stage_capture_due(
                state_key="us_premarket_snapshots",
                session_date=session_date,
                session_stage="premarket",
                now=current,
                minimum_interval_seconds=30,
            ):
                return {
                    "accepted": True,
                    "skipped": True,
                    "reason": "us_premarket_snapshot_throttled",
                    "strategy_id": STRATEGY_ID,
                    "market_phase": phase.to_dict(),
                }
            try:
                session_open_at, _session_close_at = (
                    trading_calendar.get_market_session_bounds(
                        "us",
                        current_time=current,
                        strict=True,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - evidence must fail closed.
                logger.warning("Failed to resolve the current US session open: %s", exc)
                return {
                    "accepted": False,
                    "skipped": True,
                    "reason": "us_session_bounds_unavailable",
                    "strategy_id": STRATEGY_ID,
                    "error_type": type(exc).__name__,
                    "market_phase": phase.to_dict(),
                }
            if session_open_at is None:
                return {
                    "accepted": False,
                    "skipped": True,
                    "reason": "us_session_bounds_unavailable",
                    "strategy_id": STRATEGY_ID,
                    "market_phase": phase.to_dict(),
                }
            try:
                snapshot = self.collect_us_premarket_snapshot(
                    now=None if live_collection else current,
                    session_open_at=session_open_at,
                )
            except ValueError as exc:
                if str(exc) != "us_premarket_evidence_unavailable":
                    raise
                return {
                    "accepted": False,
                    "skipped": True,
                    "reason": "us_premarket_evidence_unavailable",
                    "strategy_id": STRATEGY_ID,
                    "market_phase": phase.to_dict(),
                    "evidence_ready": False,
                    "retryable": True,
                    "retry_after_seconds": 30,
                }
            return {
                "accepted": True,
                "skipped": False,
                "reason": "us_premarket_snapshot_collected",
                "strategy_id": STRATEGY_ID,
                "market_phase": phase.to_dict(),
                "snapshot": snapshot,
            }
        if (
            phase.is_market_open_now is True
            and local_time <= datetime_time(10, 30)
        ):
            stage = "first_hour"
            if not self._session_stage_capture_due(
                state_key="us_tech_snapshots",
                session_date=session_date,
                session_stage=stage,
                now=current,
                minimum_interval_seconds=300,
            ):
                return {
                    "accepted": True,
                    "skipped": True,
                    "reason": "us_first_hour_snapshot_throttled",
                    "strategy_id": STRATEGY_ID,
                    "market_phase": phase.to_dict(),
                }
        elif (
            is_trading_day
            and phase_name == "postmarket"
            and _is_us_close_capture_window(current)
        ):
            stage = "close"
            collect_tech_snapshot = self._session_stage_capture_due(
                state_key="us_tech_snapshots",
                session_date=session_date,
                session_stage=stage,
                now=current,
                once_per_session=True,
            )
            collect_close_theme_snapshot = not self._us_close_theme_capture_complete(
                self._read_state(),
                session_date=session_date,
                current=current,
            )
            if not collect_tech_snapshot and not collect_close_theme_snapshot:
                return {
                    "accepted": True,
                    "skipped": True,
                    "reason": "us_close_snapshot_already_collected",
                    "strategy_id": STRATEGY_ID,
                    "market_phase": phase.to_dict(),
                }
        else:
            if nasdaq_futures_result is not None:
                return {
                    **nasdaq_futures_result,
                    "market_phase": phase.to_dict(),
                }
            return {
                "accepted": True,
                "skipped": True,
                "reason": "outside_us_signal_capture_window",
                "strategy_id": STRATEGY_ID,
                "market_phase": phase.to_dict(),
            }
        snapshot = None
        close_theme_snapshot = None
        collection_errors = []
        close_theme_evidence_pending = False
        if collect_tech_snapshot:
            try:
                snapshot = self.collect_us_tech_snapshot(
                    now=None if live_collection else current,
                    session_stage=stage,
                )
            except Exception as exc:  # noqa: BLE001 - the theme close still runs.
                collection_errors.append(exc)
        if stage == "close" and collect_close_theme_snapshot:
            try:
                close_theme_snapshot = self.collect_us_close_theme_snapshot(
                    now=None if live_collection else current,
                )
            except ValueError as exc:
                if str(exc) != "us_close_theme_evidence_unavailable":
                    collection_errors.append(exc)
                else:
                    close_theme_evidence_pending = True
            except Exception as exc:  # noqa: BLE001 - the tech close may still persist.
                collection_errors.append(exc)
        if collection_errors:
            raise collection_errors[0]
        if close_theme_evidence_pending:
            return {
                "accepted": False,
                "skipped": True,
                "reason": "us_close_theme_evidence_unavailable",
                "strategy_id": STRATEGY_ID,
                "market_phase": phase.to_dict(),
                "evidence_ready": False,
                "retryable": True,
                "retry_after_seconds": 30,
                "snapshot": snapshot,
                "close_theme_snapshot": None,
            }
        reason = "us_tech_snapshot_collected"
        if stage == "close" and close_theme_snapshot is not None:
            reason = (
                "us_close_snapshots_collected"
                if snapshot is not None
                else "us_close_theme_snapshot_collected"
            )
        return {
            "accepted": True,
            "skipped": False,
            "reason": reason,
            "strategy_id": STRATEGY_ID,
            "market_phase": phase.to_dict(),
            "snapshot": snapshot,
            "close_theme_snapshot": close_theme_snapshot,
        }

    def _collect_nasdaq_futures_if_cn_window(
        self,
        *,
        current: datetime,
        live_collection: bool,
    ) -> Optional[Dict[str, Any]]:
        try:
            cn_phase = trading_calendar.build_market_phase_context(
                market="cn",
                current_time=current,
                trigger_source="cross_market_nasdaq_futures_signal",
                analysis_intent="auto",
            )
        except Exception as exc:  # noqa: BLE001 - the normal US task may still run.
            logger.warning("Failed to resolve CN phase for NQ00Y capture: %s", exc)
            return None
        if cn_phase.market_local_time.utcoffset() != timedelta(hours=8):
            return None
        local_time = cn_phase.market_local_time.time()
        if not (
            getattr(cn_phase, "is_trading_day", True) is True
            and datetime_time(9, 25) <= local_time <= datetime_time(15, 0)
        ):
            return None
        session_date = current.astimezone(
            ZoneInfo("Asia/Shanghai")
        ).date().isoformat()
        if not self._session_stage_capture_due(
            state_key=NASDAQ_FUTURES_STATE_KEY,
            session_date=session_date,
            session_stage="cn_intraday",
            now=current,
            minimum_interval_seconds=NASDAQ_FUTURES_CAPTURE_THROTTLE_SECONDS,
        ):
            return {
                "accepted": True,
                "skipped": True,
                "reason": "nasdaq_futures_snapshot_throttled",
                "strategy_id": STRATEGY_ID,
                "cn_market_phase": cn_phase.to_dict(),
            }
        try:
            snapshot = self.collect_nasdaq_futures_snapshot(
                now=None if live_collection else current,
            )
        except Exception as exc:  # noqa: BLE001 - evidence failure is explicit.
            logger.warning("Failed to collect NQ00Y evidence: %s", exc)
            return {
                "accepted": False,
                "skipped": True,
                "reason": str(exc) or "nasdaq_futures_snapshot_failed",
                "strategy_id": STRATEGY_ID,
                "error_type": type(exc).__name__,
                "cn_market_phase": cn_phase.to_dict(),
            }
        return {
            "accepted": True,
            "skipped": False,
            "reason": "nasdaq_futures_snapshot_collected",
            "strategy_id": STRATEGY_ID,
            "snapshot": snapshot,
            "cn_market_phase": cn_phase.to_dict(),
        }

    def get_us_tech_signal_for_cn_trade(
        self,
        *,
        now: Optional[datetime] = None,
        max_age_hours: int = 96,
    ) -> Dict[str, Any]:
        current = (now or _utc_now()).astimezone(timezone.utc)
        try:
            required_session_date = trading_calendar.get_effective_trading_date(
                "us",
                current_time=current,
                strict=True,
            ).isoformat()
        except Exception as exc:  # noqa: BLE001 - session alignment must fail closed.
            return {
                "available": False,
                "buy_allowed": False,
                "reason": "us_session_calendar_unavailable",
                "strategy_id": STRATEGY_ID,
                "error_type": type(exc).__name__,
            }
        payload = self._read_state()
        close_candidates = []
        first_hour_by_session: Dict[str, List[tuple[datetime, Dict[str, Any]]]] = {}
        for item in payload.get("us_tech_snapshots", []):
            if not isinstance(item, dict):
                continue
            observed_at = _parse_datetime(item.get("observed_at"))
            if observed_at is None or observed_at > current:
                continue
            age_hours = (current - observed_at).total_seconds() / 3600.0
            if age_hours > max(1, int(max_age_hours)):
                continue
            stage = str(item.get("session_stage") or "").strip().lower()
            session_date = str(item.get("session_date") or "").strip()
            if session_date != required_session_date:
                continue
            if stage == "close":
                close_candidates.append((observed_at, item, age_hours))
            elif stage == "first_hour":
                first_hour_by_session.setdefault(session_date, []).append((observed_at, item))
        if not close_candidates:
            return {
                "available": False,
                "buy_allowed": False,
                "reason": "prior_us_close_signal_unavailable",
                "strategy_id": STRATEGY_ID,
                "required_session_date": required_session_date,
            }
        close_at, close_item, age_hours = max(
            close_candidates,
            key=lambda candidate: candidate[0],
        )
        session_date = str(close_item.get("session_date") or "")
        first_hour_candidates = [
            candidate
            for candidate in first_hour_by_session.get(session_date, [])
            if candidate[0] <= close_at
        ]
        if not first_hour_candidates:
            return {
                "available": False,
                "buy_allowed": False,
                "reason": "prior_us_first_hour_signal_unavailable",
                "strategy_id": STRATEGY_ID,
                "session_date": session_date,
                "close_observed_at": close_at.isoformat(),
            }
        first_hour_at, first_hour_item = max(
            first_hour_candidates,
            key=lambda candidate: candidate[0],
        )
        first_hour_score = float(first_hour_item.get("score"))
        close_score = float(close_item.get("score"))
        score = 0.4 * first_hour_score + 0.6 * close_score
        return {
            "available": True,
            "buy_allowed": score >= self.engine.config.us_buy_score,
            "reason": "prior_us_first_hour_and_close_signals_ready",
            "strategy_id": STRATEGY_ID,
            "score": round(score, 6),
            "first_hour_score": first_hour_score,
            "close_score": close_score,
            "first_hour_observed_at": first_hour_at.isoformat(),
            "close_observed_at": close_at.isoformat(),
            "observed_at": close_at.isoformat(),
            "session_date": session_date,
            "age_hours": round(age_hours, 6),
            "weighting": {"first_hour": 0.4, "close": 0.6},
            "snapshots": {
                "first_hour": first_hour_item,
                "close": close_item,
            },
        }

    def get_us_premarket_signal_for_cn_trade(
        self,
        *,
        theme: str,
        now: Optional[datetime] = None,
        max_age_hours: int = 96,
    ) -> Dict[str, Any]:
        current = (now or _utc_now()).astimezone(timezone.utc)
        normalized_theme = str(theme or "").strip().lower()
        signal_theme = US_PREMARKET_A_SHARE_THEME_MAP.get(normalized_theme, "")
        if not signal_theme:
            return {
                "available": False,
                "strong": False,
                "reason": "premarket_theme_not_supported",
                "strategy_id": STRATEGY_ID,
                "theme": normalized_theme,
            }
        try:
            required_session_date = trading_calendar.get_effective_trading_date(
                "us",
                current_time=current,
                strict=True,
            ).isoformat()
        except Exception as exc:  # noqa: BLE001 - session mismatch fails closed.
            return {
                "available": False,
                "strong": False,
                "reason": "us_session_calendar_unavailable",
                "strategy_id": STRATEGY_ID,
                "error_type": type(exc).__name__,
            }
        candidates = []
        for item in self._read_state().get("us_premarket_snapshots", []):
            if not isinstance(item, dict):
                continue
            if str(item.get("session_date") or "") != required_session_date:
                continue
            observed_at = _parse_datetime(item.get("observed_at"))
            if observed_at is None or observed_at > current:
                continue
            age_hours = (current - observed_at).total_seconds() / 3600.0
            if age_hours > max(1, int(max_age_hours)):
                continue
            theme_signals = (
                item.get("theme_signals")
                if isinstance(item.get("theme_signals"), dict)
                else {}
            )
            signal = theme_signals.get(signal_theme)
            if isinstance(signal, dict):
                candidates.append((observed_at, age_hours, item, signal))
        if not candidates:
            return {
                "available": False,
                "strong": False,
                "reason": "prior_us_premarket_signal_unavailable",
                "strategy_id": STRATEGY_ID,
                "theme": normalized_theme,
                "signal_theme": signal_theme,
                "required_session_date": required_session_date,
            }
        observed_at, age_hours, snapshot, signal = max(
            candidates,
            key=lambda candidate: (
                candidate[3].get("available") is True,
                candidate[0],
            ),
        )
        result = {
            **signal,
            "strategy_id": STRATEGY_ID,
            "theme": normalized_theme,
            "signal_theme": signal_theme,
            "session_date": required_session_date,
            "observed_at": observed_at.isoformat(),
            "age_hours": round(age_hours, 6),
            "snapshot": snapshot,
        }
        result["asia_supplement_eligible"] = (
            self._us_theme_allows_asia_supplement(
                theme=normalized_theme,
                signal=result,
                session_stage="premarket",
            )
        )
        return result

    def get_us_close_theme_signal_for_cn_trade(
        self,
        *,
        theme: str,
        now: Optional[datetime] = None,
        max_age_hours: int = 96,
    ) -> Dict[str, Any]:
        """Return the completed US close basket that leads the next CN open."""

        current = (now or _utc_now()).astimezone(timezone.utc)
        normalized_theme = str(theme or "").strip().lower()
        signal_theme = US_PREMARKET_A_SHARE_THEME_MAP.get(normalized_theme, "")
        if not signal_theme:
            return {
                "available": False,
                "strong": False,
                "reason": "us_close_theme_not_supported",
                "strategy_id": STRATEGY_ID,
                "theme": normalized_theme,
            }
        try:
            required_session_date = trading_calendar.get_effective_trading_date(
                "us",
                current_time=current,
                strict=True,
            ).isoformat()
        except Exception as exc:  # noqa: BLE001 - session mismatch fails closed.
            return {
                "available": False,
                "strong": False,
                "reason": "us_session_calendar_unavailable",
                "strategy_id": STRATEGY_ID,
                "error_type": type(exc).__name__,
            }
        candidates = []
        for item in self._read_state().get("us_close_theme_snapshots", []):
            if not isinstance(item, dict):
                continue
            if str(item.get("session_date") or "") != required_session_date:
                continue
            observed_at = _parse_datetime(item.get("observed_at"))
            if observed_at is None or observed_at > current:
                continue
            age_hours = (current - observed_at).total_seconds() / 3600.0
            if age_hours > max(1, int(max_age_hours)):
                continue
            theme_signals = (
                item.get("theme_signals")
                if isinstance(item.get("theme_signals"), dict)
                else {}
            )
            signal = theme_signals.get(signal_theme)
            if isinstance(signal, dict):
                candidates.append((observed_at, age_hours, item, signal))
        if not candidates:
            return {
                "available": False,
                "strong": False,
                "reason": "prior_us_close_theme_signal_unavailable",
                "strategy_id": STRATEGY_ID,
                "theme": normalized_theme,
                "signal_theme": signal_theme,
                "required_session_date": required_session_date,
            }
        observed_at, age_hours, snapshot, signal = max(
            candidates,
            key=lambda candidate: (
                candidate[3].get("available") is True,
                candidate[0],
            ),
        )
        premarket = self.get_us_premarket_signal_for_cn_trade(
            theme=normalized_theme,
            now=current,
            max_age_hours=max_age_hours,
        )
        premarket_reversal_invalidated = bool(
            premarket.get("available") is True
            and premarket.get("strong") is True
            and (
                float(signal.get("sector_change_pct") or 0.0) < 0.0
                or float(signal.get("score") or 0.0) < 0.0
            )
        )
        result = {
            **signal,
            "strategy_id": STRATEGY_ID,
            "theme": normalized_theme,
            "signal_theme": signal_theme,
            "session_date": required_session_date,
            "observed_at": observed_at.isoformat(),
            "age_hours": round(age_hours, 6),
            "premarket_reversal_invalidated": premarket_reversal_invalidated,
            "paired_premarket": premarket,
            "snapshot": snapshot,
        }
        result["asia_supplement_eligible"] = (
            self._us_theme_allows_asia_supplement(
                theme=normalized_theme,
                signal=result,
                session_stage="close",
            )
        )
        return result

    def collect_cpo_snapshot(
        self,
        *,
        now: Optional[datetime] = None,
        session_stage: str = "intraday",
    ) -> Dict[str, Any]:
        collection_started_at = (now or _utc_now()).astimezone(timezone.utc)
        components: Dict[str, Dict[str, Any]] = {}
        component_errors: Dict[str, str] = {}
        component_quotes: Dict[str, tuple[Any, datetime]] = {}
        normalized_scores: List[float] = []
        advancing = 0
        for code in CPO_US_EVIDENCE_CODES:
            try:
                quote = self._get_timestamped_us_quote(code)
                if quote is None:
                    raise ValueError("quote_unavailable")
                provider_at = _parse_datetime(getattr(quote, "provider_timestamp", None))
                if provider_at is None:
                    raise ValueError("provider_timestamp_required")
                component_quotes[code] = (quote, provider_at)
            except Exception as exc:  # noqa: BLE001 - US optical evidence is optional for CPO.
                component_errors[code] = str(exc) or type(exc).__name__

        collected_at = (
            collection_started_at
            if now is not None
            else _utc_now().astimezone(timezone.utc)
        )
        for code in CPO_US_EVIDENCE_CODES:
            if code not in component_quotes:
                continue
            quote, provider_at = component_quotes[code]
            try:
                age_seconds = (collected_at - provider_at).total_seconds()
                if age_seconds < -1:
                    raise ValueError("evidence_from_future")
                if age_seconds > self.engine.config.evidence_max_age_seconds:
                    raise ValueError("evidence_stale")
                change_pct = float(getattr(quote, "change_pct", None))
                if not math.isfinite(change_pct):
                    raise ValueError("change_invalid")
            except Exception as exc:  # noqa: BLE001 - US optical evidence is optional for CPO.
                component_errors[code] = str(exc) or type(exc).__name__
                continue
            normalized_scores.append(max(-100.0, min(100.0, change_pct / 2.0 * 100.0)))
            advancing += int(change_pct > 0)
            source = getattr(quote, "source", None)
            components[code] = {
                "change_pct": change_pct,
                "price": getattr(quote, "price", None),
                "provider_timestamp": provider_at.isoformat(),
                "source": getattr(source, "value", source),
            }

        try:
            news_items = self._list_recent_intelligence_items(days=2)
            news = self._calculate_cpo_news_score(news_items, now=collected_at)
        except Exception as exc:  # noqa: BLE001 - news is optional CPO evidence.
            news = {
                "score": 0.0,
                "positive_count": 0,
                "negative_count": 0,
                "matched_items": [],
                "window_start": (collected_at - timedelta(hours=24)).isoformat(),
                "window_end": collected_at.isoformat(),
                "status": "unavailable",
                "error_type": type(exc).__name__,
            }
        market_score = (
            sum(normalized_scores) / len(normalized_scores)
            if normalized_scores
            else None
        )
        breadth_score = (
            (advancing / len(normalized_scores) - 0.5) * 200.0
            if normalized_scores
            else None
        )
        news_available = news.get("status") != "unavailable"
        weighted_components = []
        if market_score is not None and breadth_score is not None:
            weighted_components.extend(((0.60, market_score), (0.15, breadth_score)))
        if news_available:
            weighted_components.append((0.25, float(news["score"])))
        total_weight = sum(weight for weight, _value in weighted_components)
        score = (
            sum(weight * value for weight, value in weighted_components) / total_weight
            if total_weight > 0
            else 0.0
        )
        record = {
            "observed_at": collected_at.isoformat(),
            "collection_started_at": collection_started_at.isoformat(),
            "collection_duration_seconds": round(
                max(0.0, (collected_at - collection_started_at).total_seconds()),
                6,
            ),
            "session_date": collected_at.astimezone(ZoneInfo("America/New_York")).date().isoformat(),
            "session_stage": str(session_stage or "intraday").strip().lower(),
            "score": round(max(-100.0, min(100.0, score)), 6),
            "evidence_available": total_weight > 0,
            "market_score": round(market_score, 6) if market_score is not None else None,
            "advancing_ratio": (
                round(advancing / len(normalized_scores), 6)
                if normalized_scores
                else None
            ),
            "components": components,
            "component_errors": component_errors,
            "news": news,
        }
        self._append_snapshot("cpo_snapshots", record)
        return record

    def collect_cpo_snapshot_if_open(
        self,
        *,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        live_collection = now is None
        current = (now or _utc_now()).astimezone(timezone.utc)
        try:
            phase = trading_calendar.build_market_phase_context(
                market="us",
                current_time=current,
                trigger_source="cross_market_cpo_signal",
                analysis_intent="auto",
            )
        except Exception as exc:  # noqa: BLE001 - optional CPO evidence must stay observable.
            return {
                "accepted": False,
                "skipped": True,
                "reason": "cpo_us_market_phase_unknown",
                "strategy_id": STRATEGY_ID,
                "error_type": type(exc).__name__,
            }
        local_time = phase.market_local_time.time()
        session_date = str(
            getattr(phase, "session_date", None)
            or phase.market_local_time.date().isoformat()
        )
        phase_name = _normalized_enum_text(getattr(phase, "phase", ""))
        is_trading_day = getattr(phase, "is_trading_day", True) is True
        if (
            phase.is_market_open_now is True
            and local_time <= datetime_time(10, 30)
        ):
            stage = "first_hour"
            if not self._session_stage_capture_due(
                state_key="cpo_snapshots",
                session_date=session_date,
                session_stage=stage,
                now=current,
                minimum_interval_seconds=300,
            ):
                return {
                    "accepted": True,
                    "skipped": True,
                    "reason": "cpo_first_hour_snapshot_throttled",
                    "strategy_id": STRATEGY_ID,
                    "market_phase": phase.to_dict(),
                }
        elif (
            is_trading_day
            and phase_name == "postmarket"
            and _is_us_close_capture_window(current)
        ):
            stage = "close"
            if not self._session_stage_capture_due(
                state_key="cpo_snapshots",
                session_date=session_date,
                session_stage=stage,
                now=current,
                once_per_session=True,
            ):
                return {
                    "accepted": True,
                    "skipped": True,
                    "reason": "cpo_close_snapshot_already_collected",
                    "strategy_id": STRATEGY_ID,
                    "market_phase": phase.to_dict(),
                }
        else:
            return {
                "accepted": True,
                "skipped": True,
                "reason": "outside_cpo_signal_capture_window",
                "strategy_id": STRATEGY_ID,
                "market_phase": phase.to_dict(),
            }
        snapshot = self.collect_cpo_snapshot(
            now=None if live_collection else current,
            session_stage=stage,
        )
        return {
            "accepted": True,
            "skipped": False,
            "reason": "cpo_snapshot_collected",
            "strategy_id": STRATEGY_ID,
            "market_phase": phase.to_dict(),
            "snapshot": snapshot,
        }

    def get_cpo_signal_for_cn_trade(
        self,
        *,
        now: Optional[datetime] = None,
        max_age_hours: int = 96,
    ) -> Dict[str, Any]:
        current = (now or _utc_now()).astimezone(timezone.utc)
        try:
            required_session_date = trading_calendar.get_effective_trading_date(
                "us",
                current_time=current,
                strict=True,
            ).isoformat()
        except Exception as exc:  # noqa: BLE001 - session alignment must fail closed.
            return {
                "available": False,
                "supportive": False,
                "reason": "cpo_session_calendar_unavailable",
                "strategy_id": STRATEGY_ID,
                "error_type": type(exc).__name__,
            }
        candidates = []
        for item in self._read_state().get("cpo_snapshots", []):
            if (
                not isinstance(item, dict)
                or item.get("session_stage") != "close"
                or item.get("evidence_available") is False
                or str(item.get("session_date") or "").strip()
                != required_session_date
            ):
                continue
            observed_at = _parse_datetime(item.get("observed_at"))
            if observed_at is None or observed_at > current:
                continue
            age_hours = (current - observed_at).total_seconds() / 3600.0
            if age_hours <= max(1, int(max_age_hours)):
                candidates.append((observed_at, item, age_hours))
        if not candidates:
            return {
                "available": False,
                "supportive": False,
                "reason": "prior_cpo_close_signal_unavailable",
                "strategy_id": STRATEGY_ID,
                "required_session_date": required_session_date,
            }
        observed_at, item, age_hours = max(candidates, key=lambda candidate: candidate[0])
        score = float(item.get("score") or 0.0)
        return {
            "available": True,
            "supportive": score >= 30.0,
            "reason": "prior_cpo_close_signal_ready",
            "strategy_id": STRATEGY_ID,
            "score": score,
            "observed_at": observed_at.isoformat(),
            "age_hours": round(age_hours, 6),
            "snapshot": item,
        }

    @staticmethod
    def _calculate_cpo_news_score(
        items: List[Dict[str, Any]],
        *,
        now: datetime,
    ) -> Dict[str, Any]:
        start = now - timedelta(hours=24)
        positive_count = 0
        negative_count = 0
        matched_items = []
        for item in items:
            observed_at = _parse_datetime(item.get("published_at") or item.get("fetched_at"))
            if observed_at is None or observed_at < start or observed_at > now:
                continue
            text = f"{item.get('title') or ''} {item.get('summary') or ''}".lower()
            negative = any(term in text for term in CPO_NEWS_NEGATIVE_TERMS)
            positive = not negative and any(term in text for term in CPO_NEWS_POSITIVE_TERMS)
            if not positive and not negative:
                continue
            positive_count += int(positive)
            negative_count += int(negative)
            matched_items.append({
                "title": str(item.get("title") or "")[:200],
                "observed_at": observed_at.isoformat(),
                "direction": "positive" if positive else "negative",
            })
        score = max(-100.0, min(100.0, positive_count * 30.0 - negative_count * 40.0))
        return {
            "score": score,
            "positive_count": positive_count,
            "negative_count": negative_count,
            "matched_items": matched_items,
            "window_start": start.isoformat(),
            "window_end": now.isoformat(),
            "status": "available",
        }

    def collect_gold_snapshot(self, *, now: Optional[datetime] = None) -> Dict[str, Any]:
        live_collection = now is None
        collection_started_at = (now or _utc_now()).astimezone(timezone.utc)
        quote = self._get_timestamped_quote("GC1")
        if quote is None:
            raise ValueError("gold_quote_unavailable:GC1")
        provider_at = _parse_datetime(getattr(quote, "provider_timestamp", None))
        if provider_at is None:
            raise ValueError("gold_provider_timestamp_required:GC1")

        frame, source = self.data_fetcher_manager.get_daily_data("GC1", days=35)
        if frame is None or frame.empty or "close" not in frame.columns or "date" not in frame.columns:
            raise ValueError("gold_daily_history_unavailable")
        current = (
            _utc_now().astimezone(timezone.utc)
            if live_collection
            else collection_started_at
        )
        age_seconds = (current - provider_at).total_seconds()
        if age_seconds < -1:
            raise ValueError("gold_evidence_from_future:GC1")
        if age_seconds > self.engine.config.evidence_max_age_seconds:
            raise ValueError("gold_evidence_stale:GC1")
        ny_date = current.astimezone(ZoneInfo("America/New_York")).date()
        completed = frame.copy()
        completed["date"] = completed["date"].apply(
            lambda value: value.date() if hasattr(value, "date") else value
        )
        # The 08:55-09:25 Shanghai collection window is 20:55-21:25 on the
        # preceding New York date, after that date's COMEX settlement.
        completed = completed[completed["date"] <= ny_date].sort_values("date")
        closes = [float(value) for value in completed["close"].tolist() if value is not None]
        if len(closes) < 20:
            raise ValueError("gold_daily_history_insufficient")
        trend_through_date = completed.iloc[-1]["date"]
        ma5 = sum(closes[-5:]) / 5.0
        ma20 = sum(closes[-20:]) / 20.0
        five_day_return_pct = (closes[-1] / closes[-6] - 1.0) * 100.0
        price = float(getattr(quote, "price", 0.0) or 0.0)
        overnight_return_pct = float(getattr(quote, "change_pct", 0.0) or 0.0)

        window_start, window_end = self._gold_news_window(current)
        news_window_cutoff = self._gold_news_cutoff(current)
        news_window_complete = window_end >= news_window_cutoff
        news_lookback_days = max(
            1,
            min(
                30,
                math.ceil((window_end - window_start).total_seconds() / 86400.0) + 1,
            ),
        )
        news_items, news_collection = self._collect_gold_intelligence_items(
            days=news_lookback_days,
            window_start=window_start,
            window_end=window_end,
        )
        captured_news_items = self._gold_captured_rate_news_items(
            session_date=current.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat(),
            window_start=window_start,
            window_end=window_end,
            captured_before=current,
        )
        scoring_items: List[Dict[str, Any]] = []
        seen_scoring_items = set()
        for item in news_items + captured_news_items:
            key = (
                str(item.get("title") or "").strip(),
                str(item.get("published_at") or item.get("fetched_at") or "").strip(),
            )
            if key in seen_scoring_items:
                continue
            seen_scoring_items.add(key)
            scoring_items.append(item)
        news = self.engine.calculate_rate_cut_news_score(
            scoring_items,
            window_start=window_start,
            window_end=window_end,
        )
        news_collection["captured_forward_matched_item_count"] = len(
            captured_news_items
        )
        news_collection["scoring_item_count"] = len(scoring_items)
        news_collection["window_item_retention_mode"] = (
            "same_session_forward_gold_snapshots"
        )
        news["collection"] = news_collection
        signal = self.engine.calculate_gold_score(
            overnight_return_pct=overnight_return_pct,
            five_day_return_pct=five_day_return_pct,
            above_ma20=price > ma20,
            ma5_above_ma20=ma5 > ma20,
            rate_cut_news_score=float(news["score"]),
        )
        record = {
            "observed_at": current.isoformat(),
            "collection_started_at": collection_started_at.isoformat(),
            "collection_duration_seconds": round(
                max(0.0, (current - collection_started_at).total_seconds()),
                6,
            ),
            "cn_session_date": current.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat(),
            "provider_timestamp": provider_at.isoformat(),
            "quote_source": getattr(
                getattr(quote, "source", None),
                "value",
                getattr(quote, "source", None),
            ),
            "price": price,
            "overnight_return_pct": overnight_return_pct,
            "five_day_return_pct": round(five_day_return_pct, 6),
            "ma5": round(ma5, 6),
            "ma20": round(ma20, 6),
            "trend_through_date": trend_through_date.isoformat(),
            "daily_source": source,
            "news_window_cutoff_at": news_window_cutoff.isoformat(),
            "news_window_complete": news_window_complete,
            "news": news,
            "signal": signal,
        }
        self._append_snapshot("gold_snapshots", record)
        return record

    def collect_cn_open_snapshot(self, *, now: Optional[datetime] = None) -> Dict[str, Any]:
        live_collection = now is None
        collection_started_at = (now or _utc_now()).astimezone(timezone.utc)
        try:
            session_open, session_close = trading_calendar.get_market_session_bounds(
                "cn",
                current_time=collection_started_at,
                strict=True,
            )
        except Exception as exc:  # noqa: BLE001 - opening evidence must fail closed.
            raise ValueError("cn_open_calendar_unavailable") from exc
        if session_open is None or session_close is None:
            raise ValueError("cn_open_non_trading_session")
        session_open_utc = session_open.astimezone(timezone.utc)
        if collection_started_at < session_open_utc:
            raise ValueError("cn_open_before_session_open")
        index_getter = getattr(
            self.data_fetcher_manager,
            "get_main_index_quote_with_provider_timestamp",
            None,
        )
        quote = (
            index_getter("000300", region="cn")
            if callable(index_getter)
            else self._get_timestamped_quote("000300")
        )
        if quote is None:
            raise ValueError("cn_open_quote_unavailable:000300")
        provider_at = _parse_datetime(getattr(quote, "provider_timestamp", None))
        if provider_at is None:
            raise ValueError("cn_open_provider_timestamp_required:000300")
        current = (
            _utc_now().astimezone(timezone.utc)
            if live_collection
            else collection_started_at
        )
        age_seconds = (current - provider_at).total_seconds()
        if age_seconds < -1:
            raise ValueError("cn_open_evidence_from_future:000300")
        if age_seconds > self.engine.config.evidence_max_age_seconds:
            raise ValueError("cn_open_evidence_stale:000300")
        if provider_at < session_open_utc:
            raise ValueError("cn_open_evidence_before_session_open:000300")
        open_price = getattr(quote, "open_price", None)
        previous_close = getattr(quote, "pre_close", None)
        current_price = getattr(quote, "price", None)
        try:
            open_value = float(open_price)
            previous_value = float(previous_close)
            price_value = float(current_price)
        except (TypeError, ValueError) as exc:
            raise ValueError("cn_open_prices_required:000300") from exc
        if open_value <= 0 or previous_value <= 0 or price_value <= 0:
            raise ValueError("cn_open_prices_invalid:000300")
        gap_pct = (open_value / previous_value - 1.0) * 100.0
        classification = self.engine.classify_cn_open(gap_pct)
        # Index turnover aggregates constituent cash and share volume. Their
        # ratio is an average constituent price, not an index-point VWAP.
        source = getattr(quote, "source", None)
        record = {
            "observed_at": current.isoformat(),
            "collection_started_at": collection_started_at.isoformat(),
            "collection_duration_seconds": round(
                max(0.0, (current - collection_started_at).total_seconds()),
                6,
            ),
            "cn_session_date": session_open.astimezone(
                ZoneInfo("Asia/Shanghai")
            ).date().isoformat(),
            "session_open_at": session_open_utc.isoformat(),
            "provider_timestamp": provider_at.isoformat(),
            "quote_source": getattr(source, "value", source),
            "index_code": "000300",
            "open_price": open_value,
            "previous_close": previous_value,
            "current_price": price_value,
            "gap_pct": round(gap_pct, 6),
            "reclaimed_open": price_value >= open_value,
            "vwap": None,
            "vwap_available": False,
            "vwap_reason": "index_point_vwap_unavailable",
            "above_vwap": False,
            "classification": classification,
            "classification_thresholds": self._cn_open_classification_thresholds(),
        }
        self._append_snapshot("cn_open_snapshots", record)
        return record

    def collect_cn_open_snapshot_if_window(
        self,
        *,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        live_collection = now is None
        current = (now or _utc_now()).astimezone(timezone.utc)
        local = current.astimezone(ZoneInfo("Asia/Shanghai"))
        if not datetime_time(9, 30) <= local.time() <= datetime_time(9, 45):
            return {
                "accepted": True,
                "skipped": True,
                "reason": "outside_cn_open_signal_window",
                "strategy_id": STRATEGY_ID,
            }
        phase_gate = self._cn_collection_phase_gate(
            current=current,
            trigger_source="cross_market_cn_open_signal",
        )
        if phase_gate is not None:
            return phase_gate
        snapshot = self.collect_cn_open_snapshot(
            now=None if live_collection else current
        )
        return {
            "accepted": True,
            "skipped": False,
            "reason": "cn_open_snapshot_collected",
            "strategy_id": STRATEGY_ID,
            "snapshot": snapshot,
        }

    def get_cn_open_signal(self, *, now: Optional[datetime] = None) -> Dict[str, Any]:
        current = (now or _utc_now()).astimezone(timezone.utc)
        session_date = current.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
        records = []
        for item in self._read_state().get("cn_open_snapshots", []):
            if not isinstance(item, dict) or item.get("cn_session_date") != session_date:
                continue
            observed_at = _parse_datetime(item.get("observed_at"))
            if observed_at is not None and observed_at <= current:
                records.append((observed_at, item))
        if not records:
            return {
                "available": False,
                "buy_allowed": False,
                "reason": "cn_open_signal_unavailable",
                "session_date": session_date,
                "strategy_id": STRATEGY_ID,
            }
        observed_at, item = max(records, key=lambda candidate: candidate[0])
        recorded_classification = dict(item.get("classification") or {})
        try:
            gap_pct = float(item.get("gap_pct"))
        except (TypeError, ValueError):
            return {
                "available": False,
                "buy_allowed": False,
                "reason": "cn_open_gap_invalid",
                "session_date": session_date,
                "strategy_id": STRATEGY_ID,
                "classification_thresholds": (
                    self._cn_open_classification_thresholds()
                ),
            }
        classification = self.engine.classify_cn_open(gap_pct)
        return {
            "available": True,
            "buy_allowed": bool(classification.get("buy_allowed")),
            "force_sell": bool(classification.get("force_sell")),
            "regime": classification.get("regime"),
            "gap_pct": gap_pct,
            "session_date": session_date,
            "observed_at": observed_at.isoformat(),
            "snapshot": item,
            "classification_source": "current_strategy_config",
            "classification_thresholds": self._cn_open_classification_thresholds(),
            "recorded_classification": recorded_classification,
            "classification_changed": recorded_classification != classification,
            "strategy_id": STRATEGY_ID,
        }

    def _cn_open_classification_thresholds(self) -> Dict[str, float]:
        config = self.engine.config
        return {
            "high_open_pct": float(config.cn_high_open_pct),
            "low_open_upper_pct": float(config.cn_low_open_upper_pct),
            "extreme_low_open_pct": float(config.cn_extreme_low_open_pct),
        }

    def collect_gold_snapshot_if_window(
        self,
        *,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        live_collection = now is None
        current = (now or _utc_now()).astimezone(timezone.utc)
        local = current.astimezone(ZoneInfo("Asia/Shanghai"))
        if not datetime_time(8, 55) <= local.time() <= datetime_time(9, 25):
            return {
                "accepted": True,
                "skipped": True,
                "reason": "outside_gold_signal_window",
                "strategy_id": STRATEGY_ID,
            }
        phase_gate = self._cn_collection_phase_gate(
            current=current,
            trigger_source="cross_market_gold_signal",
        )
        if phase_gate is not None:
            return phase_gate
        snapshot = self.collect_gold_snapshot(
            now=None if live_collection else current
        )
        return {
            "accepted": True,
            "skipped": False,
            "reason": "gold_snapshot_collected",
            "strategy_id": STRATEGY_ID,
            "snapshot": snapshot,
        }

    @staticmethod
    def _cn_collection_phase_gate(
        *,
        current: datetime,
        trigger_source: str,
    ) -> Optional[Dict[str, Any]]:
        try:
            phase = trading_calendar.build_market_phase_context(
                market="cn",
                current_time=current,
                trigger_source=trigger_source,
                analysis_intent="auto",
            )
        except Exception as exc:  # noqa: BLE001 - collection must fail closed.
            return {
                "accepted": False,
                "skipped": True,
                "reason": "cn_collection_calendar_unavailable",
                "strategy_id": STRATEGY_ID,
                "error_type": type(exc).__name__,
            }
        warnings = {
            str(item or "").strip().lower()
            for item in getattr(phase, "warnings", [])
        }
        if warnings.intersection({"calendar_unavailable", "calendar_error"}):
            return {
                "accepted": False,
                "skipped": True,
                "reason": "cn_collection_calendar_unavailable",
                "strategy_id": STRATEGY_ID,
                "market_phase": phase.to_dict(),
            }
        if getattr(phase, "is_trading_day", False) is not True:
            return {
                "accepted": True,
                "skipped": True,
                "reason": "non_cn_trading_day",
                "strategy_id": STRATEGY_ID,
                "market_phase": phase.to_dict(),
            }
        return None

    def get_gold_signal_for_cn_trade(self, *, now: Optional[datetime] = None) -> Dict[str, Any]:
        current = (now or _utc_now()).astimezone(timezone.utc)
        session_date = current.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
        news_window_cutoff = self._gold_news_cutoff(current)
        require_complete_news_window = current >= news_window_cutoff
        records = []
        provisional_records = []
        for item in self._read_state().get("gold_snapshots", []):
            if not isinstance(item, dict) or item.get("cn_session_date") != session_date:
                continue
            observed_at = _parse_datetime(item.get("observed_at"))
            if observed_at is None or observed_at > current:
                continue
            news = item.get("news") if isinstance(item.get("news"), dict) else {}
            news_window_end = _parse_datetime(news.get("window_end"))
            news_window_complete = bool(
                news_window_end is not None
                and news_window_end >= news_window_cutoff
            )
            if require_complete_news_window and not news_window_complete:
                provisional_records.append((observed_at, item))
                continue
            records.append((observed_at, item))
        if not records:
            return {
                "available": False,
                "buy_allowed": False,
                "reason": (
                    "gold_news_window_incomplete"
                    if provisional_records and require_complete_news_window
                    else "gold_signal_unavailable"
                ),
                "news_window_cutoff_at": news_window_cutoff.isoformat(),
                "news_window_complete": False,
                "strategy_id": STRATEGY_ID,
            }
        observed_at, item = max(records, key=lambda candidate: candidate[0])
        signal = dict(item.get("signal") or {})
        selected_news = item.get("news") if isinstance(item.get("news"), dict) else {}
        selected_news_window_end = _parse_datetime(selected_news.get("window_end"))
        selected_news_window_complete = bool(
            selected_news_window_end is not None
            and selected_news_window_end >= news_window_cutoff
        )
        return {
            "available": True,
            "buy_allowed": bool(signal.get("buy_allowed")),
            "reason": signal.get("reason") or "gold_signal_unconfirmed",
            "strategy_id": STRATEGY_ID,
            "observed_at": observed_at.isoformat(),
            "news_window_cutoff_at": news_window_cutoff.isoformat(),
            "news_window_complete": selected_news_window_complete,
            "signal": signal,
            "snapshot": item,
        }

    @staticmethod
    def _gold_news_window(current: datetime) -> tuple[datetime, datetime]:
        shanghai = ZoneInfo("Asia/Shanghai")
        local = current.astimezone(shanghai)
        previous_date = trading_calendar.get_effective_trading_date(
            "cn",
            current_time=current,
            strict=True,
        )
        start = datetime.combine(previous_date, datetime_time(15, 0), tzinfo=shanghai)
        scheduled_end = datetime.combine(local.date(), datetime_time(9, 15), tzinfo=shanghai)
        end = min(local, scheduled_end)
        return start.astimezone(timezone.utc), end.astimezone(timezone.utc)

    @staticmethod
    def _gold_news_cutoff(current: datetime) -> datetime:
        shanghai = ZoneInfo("Asia/Shanghai")
        local = current.astimezone(shanghai)
        return datetime.combine(
            local.date(),
            datetime_time(9, 15),
            tzinfo=shanghai,
        ).astimezone(timezone.utc)

    def _list_recent_intelligence_items(
        self,
        *,
        days: int = 5,
        max_items: int = 500,
    ) -> List[Dict[str, Any]]:
        service = self.intelligence_service
        if service is None:
            from src.services.intelligence_service import IntelligenceService

            service = IntelligenceService()
        safe_days = max(1, min(30, int(days or 1)))
        safe_limit = max(1, min(1000, int(max_items or 1)))
        items: List[Dict[str, Any]] = []
        page = 1
        while len(items) < safe_limit:
            payload = service.list_items(
                days=safe_days,
                page=page,
                page_size=min(100, safe_limit - len(items)),
            )
            page_items = [
                item for item in payload.get("items", []) if isinstance(item, dict)
            ]
            items.extend(page_items)
            total = int(payload.get("total") or len(items))
            if not page_items or len(items) >= total:
                break
            page += 1
        return items[:safe_limit]

    def _collect_gold_intelligence_items(
        self,
        *,
        days: int,
        window_start: datetime,
        window_end: datetime,
        max_items: int = 500,
    ) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """Collect audited live macro news plus any already persisted context."""

        persisted_items = self._list_recent_intelligence_items(
            days=days,
            max_items=max_items,
        )
        service = self.intelligence_service
        if service is None:
            from src.services.intelligence_service import IntelligenceService

            service = IntelligenceService()
            self.intelligence_service = service
        template_fetcher = getattr(service, "fetch_template_items", None)
        if not callable(template_fetcher):
            return persisted_items, {
                "status": "available",
                "mode": "injected_repository_only",
                "window_coverage_complete": True,
                "persisted_item_count": len(persisted_items),
                "live_source_success_count": 0,
                "live_source_failure_count": 0,
                "source_results": [],
            }

        template_ids = (
            *GOLD_NEWS_OFFICIAL_TEMPLATE_IDS,
            *GOLD_NEWS_SUPPLEMENTAL_TEMPLATE_IDS,
        )
        per_source_limit = max(10, min(250, int(max_items or 1) // len(template_ids)))
        live_items: List[Dict[str, Any]] = []
        source_results: List[Dict[str, Any]] = []
        official_fetch_successes = 0
        supplemental_fetch_successes = 0
        official_successes = 0
        supplemental_successes = 0
        official_coverage_complete = True
        normalized_window_start = window_start.astimezone(timezone.utc)
        normalized_window_end = window_end.astimezone(timezone.utc)
        for template_id in template_ids:
            source_kind = (
                "official"
                if template_id in GOLD_NEWS_OFFICIAL_TEMPLATE_IDS
                else "supplemental"
            )
            try:
                payload = template_fetcher(template_id, limit=per_source_limit)
                fetched_items = [
                    dict(item)
                    for item in list(payload.get("items") or [])
                    if isinstance(item, dict)
                ]
                live_items.extend(fetched_items)
                published_times = [
                    parsed
                    for parsed in (
                        _parse_datetime(item.get("published_at"))
                        for item in fetched_items
                    )
                    if parsed is not None
                ]
                nonfuture_published_times = [
                    published_at
                    for published_at in published_times
                    if published_at <= normalized_window_end
                ]
                window_published_times = [
                    published_at
                    for published_at in nonfuture_published_times
                    if published_at >= normalized_window_start
                ]
                exhausted = bool(payload.get("exhausted"))
                covers_window_start = bool(
                    nonfuture_published_times
                    and (
                        exhausted
                        or min(nonfuture_published_times) <= normalized_window_start
                    )
                )
                if source_kind == "official":
                    official_fetch_successes += 1
                    source_usable = covers_window_start
                    official_successes += int(source_usable)
                    official_coverage_complete = bool(
                        official_coverage_complete and source_usable
                    )
                else:
                    supplemental_fetch_successes += 1
                    source_usable = bool(window_published_times)
                    supplemental_successes += int(source_usable)
                if source_usable:
                    evidence_reason = None
                elif not published_times:
                    evidence_reason = "no_parseable_published_items"
                elif source_kind == "official":
                    evidence_reason = "official_window_coverage_incomplete"
                else:
                    evidence_reason = "no_items_in_news_window"
                source_results.append({
                    "template_id": template_id,
                    "source_kind": source_kind,
                    "status": "available" if source_usable else "insufficient_evidence",
                    "reason": evidence_reason,
                    "fetched_count": len(fetched_items),
                    "parseable_published_count": len(published_times),
                    "window_item_count": len(window_published_times),
                    "requested_limit": per_source_limit,
                    "exhausted": exhausted,
                    "covers_window_start": covers_window_start,
                    "oldest_published_at": (
                        min(published_times).isoformat()
                        if published_times
                        else None
                    ),
                    "latest_published_at": (
                        max(published_times).isoformat()
                        if published_times
                        else None
                    ),
                })
            except Exception as exc:  # noqa: BLE001 - every source is audited.
                if source_kind == "official":
                    official_coverage_complete = False
                source_results.append({
                    "template_id": template_id,
                    "source_kind": source_kind,
                    "status": "unavailable",
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:200],
                })

        if (
            official_successes != len(GOLD_NEWS_OFFICIAL_TEMPLATE_IDS)
            or not official_coverage_complete
            or supplemental_successes < 1
        ):
            reason = (
                "gold_news_window_coverage_incomplete"
                if official_fetch_successes == len(GOLD_NEWS_OFFICIAL_TEMPLATE_IDS)
                and official_successes != len(GOLD_NEWS_OFFICIAL_TEMPLATE_IDS)
                else "gold_news_evidence_unavailable"
            )
            error = ValueError(reason)
            setattr(error, "details", {"source_results": source_results})
            raise error

        combined = persisted_items + live_items
        deduplicated: List[Dict[str, Any]] = []
        seen = set()
        for item in combined:
            key = (
                str(item.get("url") or "").strip(),
                str(item.get("title") or "").strip(),
                str(item.get("published_at") or item.get("fetched_at") or ""),
            )
            if key in seen:
                continue
            seen.add(key)
            deduplicated.append(item)
            if len(deduplicated) >= max_items:
                break
        return deduplicated, {
            "status": "available",
            "mode": "live_templates_with_repository_context",
            "window_coverage_complete": True,
            "window_start": normalized_window_start.isoformat(),
            "persisted_item_count": len(persisted_items),
            "live_item_count": len(live_items),
            "deduplicated_item_count": len(deduplicated),
            "live_source_success_count": official_successes + supplemental_successes,
            "live_source_failure_count": len(template_ids) - official_successes - supplemental_successes,
            "official_source_fetch_count": official_fetch_successes,
            "supplemental_source_fetch_count": supplemental_fetch_successes,
            "official_source_success_count": official_successes,
            "supplemental_source_success_count": supplemental_successes,
            "source_results": source_results,
        }

    def _gold_captured_rate_news_items(
        self,
        *,
        session_date: str,
        window_start: datetime,
        window_end: datetime,
        captured_before: datetime,
    ) -> List[Dict[str, Any]]:
        """Reuse rate-news matches captured earlier in the same forward window."""

        start = window_start.astimezone(timezone.utc)
        end = window_end.astimezone(timezone.utc)
        capture_limit = captured_before.astimezone(timezone.utc)
        captured: List[Dict[str, Any]] = []
        seen = set()
        for snapshot in self._read_state().get("gold_snapshots", []):
            if (
                not isinstance(snapshot, dict)
                or snapshot.get("cn_session_date") != session_date
            ):
                continue
            snapshot_at = _parse_datetime(snapshot.get("observed_at"))
            if snapshot_at is None or snapshot_at > capture_limit:
                continue
            news = snapshot.get("news") if isinstance(snapshot.get("news"), dict) else {}
            for item in news.get("matched_items", []):
                if not isinstance(item, dict):
                    continue
                published_at = _parse_datetime(
                    item.get("published_at") or item.get("observed_at")
                )
                if published_at is None or not start <= published_at <= end:
                    continue
                key = (str(item.get("title") or "").strip(), published_at.isoformat())
                if key in seen:
                    continue
                seen.add(key)
                captured.append({
                    "title": str(item.get("title") or "")[:200],
                    "summary": str(item.get("summary") or "")[:500],
                    "source": str(item.get("source") or "")[:100],
                    "published_at": published_at.isoformat(),
                })
        return captured

    def _session_stage_capture_due(
        self,
        *,
        state_key: str,
        session_date: str,
        session_stage: str,
        now: datetime,
        minimum_interval_seconds: int = 0,
        once_per_session: bool = False,
    ) -> bool:
        latest: Optional[datetime] = None
        for item in self._read_state().get(state_key, []):
            if (
                not isinstance(item, dict)
                or str(item.get("session_date") or "") != session_date
                or str(item.get("session_stage") or "") != session_stage
            ):
                continue
            observed_at = _parse_datetime(item.get("observed_at"))
            if observed_at is not None and observed_at <= now:
                latest = observed_at if latest is None else max(latest, observed_at)
        if latest is None:
            return True
        if once_per_session:
            return False
        return (now - latest).total_seconds() >= max(0, minimum_interval_seconds)

    @staticmethod
    def _us_close_theme_capture_complete(
        payload: Dict[str, Any],
        *,
        session_date: str,
        current: datetime,
    ) -> bool:
        required_signal_themes = set(US_PREMARKET_A_SHARE_THEME_MAP.values())
        available_signal_themes = set()
        for item in payload.get("us_close_theme_snapshots", []):
            if (
                not isinstance(item, dict)
                or str(item.get("session_date") or "") != session_date
                or str(item.get("session_stage") or "") != "close"
            ):
                continue
            observed_at = _parse_datetime(item.get("observed_at"))
            if observed_at is None or observed_at > current:
                continue
            theme_signals = (
                item.get("theme_signals")
                if isinstance(item.get("theme_signals"), dict)
                else {}
            )
            for signal_theme in required_signal_themes:
                signal = theme_signals.get(signal_theme)
                if isinstance(signal, dict) and signal.get("available") is True:
                    available_signal_themes.add(signal_theme)
        return required_signal_themes.issubset(available_signal_themes)

    def evaluate_korea_gate(
        self,
        *,
        theme: str,
        now: Optional[datetime] = None,
        refresh: bool = True,
    ) -> Dict[str, Any]:
        live_collection = now is None
        current = (now or _utc_now()).astimezone(timezone.utc)
        if str(theme or "").strip().lower() == "cpo":
            return {
                **self.engine.evaluate_korea_gate([], theme="cpo", now=current),
                "evaluated_at": current.isoformat(),
            }
        if refresh:
            collection = self.collect_korea_snapshot_if_open(
                now=None if live_collection else current
            )
            if collection.get("accepted") is False:
                logger.warning(
                    "Korea refresh skipped because its market phase is unknown: %s",
                    collection.get("reason"),
                )
            if live_collection:
                current = _utc_now().astimezone(timezone.utc)
        snapshots = self.load_korea_snapshots(now=current)
        timing: Optional[Dict[str, Any]] = None
        evaluation_time = current
        if snapshots:
            latest_at = snapshots[-1].observed_at.astimezone(timezone.utc)
            timing = self._classify_asia_evidence_time(
                code="KS11",
                provider_at=latest_at,
                now=current,
            )
            if (
                timing.get("accepted") is True
                and timing.get("stage") == "same_session_close"
            ):
                evaluation_time = latest_at
        gate = self.engine.evaluate_korea_gate(
            snapshots,
            theme=theme,
            now=evaluation_time,
        )
        if (
            gate.get("status") != "unavailable"
            and timing is not None
            and timing.get("accepted") is not True
        ):
            return {
                "status": "unavailable",
                "reason": "korea_evidence_not_fresh",
                "buy_allowed": False,
                "sell_fraction": 0.0,
                "confirmed": False,
                "evidence_time": timing,
                "evaluated_at": current.isoformat(),
            }
        if timing is not None:
            gate = {
                **gate,
                "evidence_stage": timing.get("stage"),
                "evidence_age_seconds": timing.get("age_seconds"),
                "evidence_time": timing,
            }
        return {
            **gate,
            "evaluated_at": current.isoformat(),
        }

    def get_japan_market_gate(
        self,
        *,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        current = (now or _utc_now()).astimezone(timezone.utc)
        try:
            phase = trading_calendar.build_market_phase_context(
                market="jp",
                current_time=current,
                trigger_source="cross_market_japan_gate",
                analysis_intent="auto",
            )
        except Exception as exc:  # noqa: BLE001 - unknown calendars fail closed.
            return {
                "available": False,
                "buy_allowed": False,
                "reason": "japan_market_phase_unknown",
                "error_type": type(exc).__name__,
            }
        session_date = str(phase.session_date)
        if phase.is_trading_day is False:
            return {
                "available": True,
                "buy_allowed": True,
                "reason": "japan_scheduled_market_closure_neutral",
                "neutral": True,
                "session_date": session_date,
                "market_phase": phase.to_dict(),
            }
        candidates = []
        for item in self._read_state().get("japan_snapshots", []):
            if not isinstance(item, dict) or item.get("session_date") != session_date:
                continue
            observed_at = _parse_datetime(item.get("observed_at"))
            if observed_at is not None and observed_at <= current:
                candidates.append((observed_at, item))
        if not candidates:
            return {
                "available": False,
                "buy_allowed": False,
                "reason": "japan_market_snapshot_unavailable",
            }
        observed_at, snapshot = max(candidates, key=lambda candidate: candidate[0])
        age_seconds = (current - observed_at).total_seconds()
        components = (
            snapshot.get("components")
            if isinstance(snapshot.get("components"), dict)
            else {}
        )
        changes = []
        component_evidence: Dict[str, Dict[str, Any]] = {}
        component_errors: Dict[str, str] = {}
        for code in JAPAN_EVIDENCE_CODES:
            component = components.get(code)
            if not isinstance(component, dict):
                component_errors[code] = "component_missing"
                continue
            try:
                provider_at = _parse_datetime(component.get("provider_timestamp"))
                change_pct = float(component.get("change_pct"))
            except (TypeError, ValueError):
                component_errors[code] = "component_invalid"
                continue
            if provider_at is None:
                component_errors[code] = "provider_timestamp_required"
                continue
            timing = self._classify_asia_evidence_time(
                code=code,
                provider_at=provider_at,
                now=current,
            )
            if timing.get("accepted") is not True:
                component_errors[code] = str(
                    timing.get("reason") or "evidence_not_fresh"
                )
                continue
            changes.append(change_pct)
            component_evidence[code] = timing
        if age_seconds < -1 or len(changes) != 2:
            return {
                "available": False,
                "buy_allowed": False,
                "reason": "japan_market_snapshot_not_fresh",
                "observed_at": observed_at.isoformat(),
                "age_seconds": round(age_seconds, 3),
                "component_errors": component_errors,
                "component_evidence": component_evidence,
            }
        buy_allowed = all(value > 0 for value in changes)
        evidence_stages = {
            str(item.get("stage") or "unknown")
            for item in component_evidence.values()
        }
        return {
            "available": True,
            "buy_allowed": buy_allowed,
            "reason": (
                "nikkei_topix_both_rising"
                if buy_allowed
                else "nikkei_topix_not_both_rising"
            ),
            "observed_at": observed_at.isoformat(),
            "age_seconds": round(age_seconds, 3),
            "evidence_stage": (
                next(iter(evidence_stages))
                if len(evidence_stages) == 1
                else "mixed"
            ),
            "component_evidence": component_evidence,
            "mean_change_pct": round(sum(changes) / len(changes), 6),
            "components": components,
        }

    def get_korea_broad_market_gate(
        self,
        *,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        current = (now or _utc_now()).astimezone(timezone.utc)
        snapshots = self.load_korea_snapshots(now=current)
        if not snapshots:
            return {
                "available": False,
                "buy_allowed": False,
                "reason": "korea_broad_market_snapshot_unavailable",
            }
        latest = snapshots[-1]
        age_seconds = (current - latest.observed_at).total_seconds()
        timing = self._classify_asia_evidence_time(
            code="KS11",
            provider_at=latest.observed_at,
            now=current,
        )
        changes = [
            latest.component_changes_pct.get("KS11"),
            latest.component_changes_pct.get("KQ11"),
        ]
        if (
            timing.get("accepted") is not True
            or any(value is None for value in changes)
        ):
            return {
                "available": False,
                "buy_allowed": False,
                "reason": "korea_broad_market_snapshot_not_fresh",
                "age_seconds": round(age_seconds, 3),
                "evidence_time": timing,
            }
        normalized = [float(value) for value in changes if value is not None]
        buy_allowed = all(value > 0 for value in normalized)
        return {
            "available": True,
            "buy_allowed": buy_allowed,
            "reason": (
                "kospi_kosdaq_both_rising"
                if buy_allowed
                else "kospi_kosdaq_not_both_rising"
            ),
            "observed_at": latest.observed_at.isoformat(),
            "age_seconds": round(age_seconds, 3),
            "evidence_stage": timing.get("stage"),
            "evidence_time": timing,
            "mean_change_pct": round(sum(normalized) / len(normalized), 6),
            "components": {
                "KS11": normalized[0],
                "KQ11": normalized[1],
            },
        }

    def get_asia_theme_signal_for_cn_trade(
        self,
        *,
        theme: str,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        current = (now or _utc_now()).astimezone(timezone.utc)
        normalized_theme = str(theme or "").strip().lower()
        session_date = current.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
        candidates = []
        for item in self._read_state().get("asia_theme_snapshots", []):
            if not isinstance(item, dict) or item.get("session_date") != session_date:
                continue
            observed_at = _parse_datetime(item.get("observed_at"))
            themes = item.get("themes") if isinstance(item.get("themes"), dict) else {}
            signal = themes.get(normalized_theme)
            if observed_at is not None and observed_at <= current and isinstance(signal, dict):
                candidates.append((observed_at, item, signal))
        if not candidates:
            return {
                "available": False,
                "strong": False,
                "reason": "asia_theme_signal_unavailable",
                "theme": normalized_theme,
            }
        candidates.sort(key=lambda candidate: candidate[0], reverse=True)
        latest_observed_at, latest_snapshot, latest_signal = candidates[0]
        latest_age_seconds = (current - latest_observed_at).total_seconds()
        if latest_age_seconds < -1 or latest_age_seconds > 2 * 60 * 60:
            return {
                "available": False,
                "strong": False,
                "reason": "asia_theme_signal_stale",
                "theme": normalized_theme,
                "observed_at": latest_observed_at.isoformat(),
                "age_seconds": round(latest_age_seconds, 3),
            }
        configured_codes = ASIA_THEME_EVIDENCE_CODES.get(normalized_theme)
        if not configured_codes:
            return {
                **latest_signal,
                "theme": normalized_theme,
                "observed_at": latest_observed_at.isoformat(),
                "age_seconds": round(latest_age_seconds, 3),
                "validated_components": {},
                "component_errors": {},
                "snapshot": latest_snapshot,
            }

        latest_failure: Optional[Dict[str, Any]] = None
        for candidate_index, (observed_at, snapshot, _stored_signal) in enumerate(
            candidates
        ):
            age_seconds = (current - observed_at).total_seconds()
            if age_seconds < -1:
                continue
            if age_seconds > 2 * 60 * 60:
                break
            components = (
                snapshot.get("components")
                if isinstance(snapshot.get("components"), dict)
                else {}
            )
            validated_components: Dict[str, Dict[str, Any]] = {}
            component_errors: Dict[str, str] = {}
            for code in configured_codes:
                component = components.get(code)
                if not isinstance(component, dict):
                    component_errors[code] = "component_missing"
                    continue
                provider_at = _parse_datetime(component.get("provider_timestamp"))
                try:
                    change_pct = float(component.get("change_pct"))
                except (TypeError, ValueError):
                    component_errors[code] = "change_pct_invalid"
                    continue
                if provider_at is None or not math.isfinite(change_pct):
                    component_errors[code] = "component_invalid"
                    continue
                timing = self._classify_asia_evidence_time(
                    code=code,
                    provider_at=provider_at,
                    now=current,
                )
                if timing.get("accepted") is not True:
                    component_errors[code] = str(
                        timing.get("reason") or "evidence_not_fresh"
                    )
                    continue
                validated_components[code] = {
                    **component,
                    "change_pct": change_pct,
                    "evidence_stage": timing.get("stage"),
                    "evidence_age_seconds": timing.get("age_seconds"),
                    "session_break_at": timing.get("session_break_at"),
                    "session_close_at": timing.get("session_close_at"),
                    "valid_until": timing.get("valid_until"),
                    "seconds_before_session_break": timing.get(
                        "seconds_before_session_break"
                    ),
                    "seconds_before_session_close": timing.get(
                        "seconds_before_session_close"
                    ),
                }
            refreshed_signal = self._asia_theme_signal_from_components(
                codes=configured_codes,
                components=validated_components,
            )
            result = {
                **refreshed_signal,
                "theme": normalized_theme,
                "observed_at": observed_at.isoformat(),
                "age_seconds": round(age_seconds, 3),
                "validated_components": validated_components,
                "component_errors": component_errors,
                "snapshot": snapshot,
            }
            if refreshed_signal.get("available") is True:
                if candidate_index:
                    result.update({
                        "latest_observed_at": latest_observed_at.isoformat(),
                        "skipped_newer_snapshot_count": candidate_index,
                        "selection_reason": (
                            "latest_currently_valid_asia_theme_snapshot"
                        ),
                    })
                return result
            if latest_failure is None:
                latest_failure = result

        if latest_failure is not None:
            return latest_failure
        return {
            "available": False,
            "strong": False,
            "reason": "asia_theme_signal_stale",
            "theme": normalized_theme,
            "observed_at": latest_observed_at.isoformat(),
            "age_seconds": round(latest_age_seconds, 3),
        }

    def evaluate_asia_market_gate(
        self,
        *,
        theme: str,
        now: Optional[datetime] = None,
        refresh: bool = True,
        reuse_fresh_supply_chain: bool = False,
    ) -> Dict[str, Any]:
        live_collection = now is None
        current = (now or _utc_now()).astimezone(timezone.utc)
        normalized_theme = str(theme or "").strip().lower()
        if normalized_theme not in TECHNOLOGY_WEIGHTED_THEMES:
            return {
                "available": True,
                "buy_allowed": True,
                "score": 50.0,
                "reason": "asia_gate_not_applicable_non_technology_theme",
                "bypassed": True,
                "theme": normalized_theme,
                "policy_scope": "technology_weighted_themes",
            }
        supply_chain_refresh = {
            "applicable": normalized_theme in ASIA_THEME_EVIDENCE_CODES,
            "requested": bool(
                refresh and normalized_theme in ASIA_THEME_EVIDENCE_CODES
            ),
            "reuse_fresh_requested": bool(reuse_fresh_supply_chain),
            "reused_fresh": False,
            "performed": False,
        }
        if refresh:
            collection_now = None if live_collection else current
            korea_collection = self.collect_korea_snapshot_if_open(
                now=collection_now
            )
            if korea_collection.get("skipped") is not False:
                logger.debug(
                    "Korea refresh skipped: %s",
                    korea_collection.get("reason"),
                )
            japan_collection = self.collect_japan_snapshot_if_open(
                now=collection_now
            )
            if japan_collection.get("skipped") is not False:
                logger.debug("Japan refresh skipped: %s", japan_collection.get("reason"))
            if normalized_theme in ASIA_THEME_EVIDENCE_CODES:
                cached_supply_chain: Dict[str, Any] = {}
                if reuse_fresh_supply_chain:
                    try:
                        cached_supply_chain = (
                            self.get_asia_theme_signal_for_cn_trade(
                                theme=normalized_theme,
                                now=current,
                            )
                        )
                    except Exception as exc:  # noqa: BLE001 - collection remains the fallback.
                        logger.debug(
                            "Fresh Asia supply-chain reuse check failed for %s: %s",
                            normalized_theme,
                            exc,
                        )
                if cached_supply_chain.get("available") is True:
                    supply_chain_refresh.update({
                        "reused_fresh": True,
                        "cached_observed_at": cached_supply_chain.get(
                            "observed_at"
                        ),
                        "cached_age_seconds": cached_supply_chain.get(
                            "age_seconds"
                        ),
                        "cached_evidence_stage": cached_supply_chain.get(
                            "evidence_stage"
                        ),
                    })
                else:
                    self.collect_asia_theme_snapshot(now=collection_now)
                    supply_chain_refresh["performed"] = True
            if live_collection:
                current = _utc_now().astimezone(timezone.utc)
        korea_gate = (
            self.evaluate_korea_gate(
                theme=normalized_theme,
                now=current,
                refresh=False,
            )
            if normalized_theme in KR_LINKED_THEMES
            else self.get_korea_broad_market_gate(now=current)
        )
        japan_gate = self.get_japan_market_gate(now=current)
        supply_chain = (
            self.get_asia_theme_signal_for_cn_trade(
                theme=normalized_theme,
                now=current,
            )
            if normalized_theme in ASIA_THEME_EVIDENCE_CODES
            else {"available": True, "strong": True, "reason": "not_required"}
        )
        block_threshold = float(
            self.engine.config.asia_market_block_mean_change_pct
        )

        def mean_change_pct(payload: Dict[str, Any]) -> Optional[float]:
            direct = payload.get("mean_change_pct")
            try:
                return float(direct) if direct is not None else None
            except (TypeError, ValueError):
                pass
            changes = payload.get("latest_component_changes_pct")
            if not isinstance(changes, dict):
                return None
            values = []
            for raw_value in changes.values():
                try:
                    values.append(float(raw_value))
                except (TypeError, ValueError):
                    continue
            return sum(values) / len(values) if values else None

        korea_mean_change_pct = mean_change_pct(korea_gate)
        japan_mean_change_pct = mean_change_pct(japan_gate)
        korea_available = bool(
            korea_gate.get("status") != "unavailable"
            and korea_gate.get("available") is not False
        )
        japan_available = japan_gate.get("available") is True
        supply_chain_required = normalized_theme in ASIA_THEME_EVIDENCE_CODES
        supply_chain_ready = bool(
            not supply_chain_required
            or (
                supply_chain.get("available") is True
                and supply_chain.get("strong") is True
            )
        )
        severe_downside_markets = []
        if (
            korea_mean_change_pct is not None
            and korea_mean_change_pct <= block_threshold
        ):
            severe_downside_markets.append("korea")
        if (
            japan_mean_change_pct is not None
            and japan_mean_change_pct <= block_threshold
        ):
            severe_downside_markets.append("japan")
        market_evidence_ready = bool(korea_available and japan_available)
        buy_allowed = bool(
            market_evidence_ready
            and not severe_downside_markets
            and supply_chain_ready
        )
        positive_confirmation = bool(
            korea_gate.get("buy_allowed") is True
            and japan_gate.get("buy_allowed") is True
            and supply_chain_ready
        )

        def market_strength(payload: Dict[str, Any]) -> Optional[float]:
            raw_score = payload.get("latest_score")
            if raw_score is None:
                raw_score = payload.get("score")
            if raw_score is None:
                mean_change = payload.get("mean_change_pct")
                try:
                    raw_score = max(
                        -100.0,
                        min(100.0, float(mean_change) / 1.5 * 100.0),
                    )
                except (TypeError, ValueError):
                    return None
            try:
                return max(0.0, min(100.0, (float(raw_score) + 100.0) / 2.0))
            except (TypeError, ValueError):
                return None

        component_strengths = [
            value
            for value in (
                market_strength(korea_gate),
                market_strength(japan_gate),
                market_strength(supply_chain),
            )
            if value is not None
        ]
        strength_score = (
            sum(component_strengths) / len(component_strengths)
            if component_strengths
            else (75.0 if buy_allowed else 0.0)
        )
        if not market_evidence_ready:
            reason = "asia_market_evidence_unavailable"
        elif severe_downside_markets:
            reason = "asia_market_severe_downside"
        elif not supply_chain_ready:
            reason = "asia_supply_chain_unconfirmed"
        elif positive_confirmation:
            reason = "asia_markets_and_supply_chain_confirmed"
        else:
            reason = "asia_markets_not_severely_weak"
        return {
            "available": bool(
                market_evidence_ready
                and (
                    not supply_chain_required
                    or supply_chain.get("available") is True
                )
            ),
            "buy_allowed": buy_allowed,
            "score": round(strength_score, 6),
            "reason": reason,
            "positive_confirmation": positive_confirmation,
            "market_block_threshold_pct": block_threshold,
            "severe_downside_markets": severe_downside_markets,
            "korea_mean_change_pct": (
                round(korea_mean_change_pct, 6)
                if korea_mean_change_pct is not None
                else None
            ),
            "japan_mean_change_pct": (
                round(japan_mean_change_pct, 6)
                if japan_mean_change_pct is not None
                else None
            ),
            "korea": korea_gate,
            "japan": japan_gate,
            "supply_chain": supply_chain,
            "supply_chain_refresh": supply_chain_refresh,
        }

    def load_korea_snapshots(self, *, now: Optional[datetime] = None) -> List[KoreaSignalSnapshot]:
        current = (now or _utc_now()).astimezone(timezone.utc)
        session_date = current.astimezone(ZoneInfo("Asia/Seoul")).date().isoformat()
        payload = self._read_state()
        snapshots: List[KoreaSignalSnapshot] = []
        for item in payload.get("korea_snapshots", []):
            if not isinstance(item, dict) or item.get("session_date") != session_date:
                continue
            observed_at = _parse_datetime(item.get("observed_at"))
            if observed_at is None or observed_at > current:
                continue
            components = item.get("components") if isinstance(item.get("components"), dict) else {}
            changes: Dict[str, float] = {}
            for code, component in components.items():
                if not isinstance(component, dict):
                    continue
                try:
                    changes[str(code)] = float(component.get("change_pct"))
                except (TypeError, ValueError):
                    continue
            try:
                score = float(item.get("score"))
            except (TypeError, ValueError):
                continue
            snapshots.append(
                KoreaSignalSnapshot(
                    observed_at=observed_at,
                    score=score,
                    component_changes_pct=changes,
                )
            )
        return sorted(snapshots, key=lambda item: item.observed_at)

    def get_status(self, *, now: Optional[datetime] = None) -> Dict[str, Any]:
        current = (now or _utc_now()).astimezone(timezone.utc)
        snapshots = self.load_korea_snapshots(now=current)
        latest = snapshots[-1] if snapshots else None
        age_seconds = int((current - latest.observed_at).total_seconds()) if latest else None
        available_fetchers = {
            str(item)
            for item in getattr(self.data_fetcher_manager, "available_fetchers", [])
        }
        return {
            "strategy_id": STRATEGY_ID,
            "quote_route": {
                "preferred": "korea_investment",
                "fallback": "naver",
                "secondary_fallback": "yfinance",
                "kis_available": "KoreaInvestmentFetcher" in available_fetchers,
                "naver_available": "NaverKoreaFetcher" in available_fetchers,
                "yfinance_available": "YfinanceFetcher" in available_fetchers,
            },
            "korea_snapshot_count": len(snapshots),
            "latest_observed_at": latest.observed_at.isoformat() if latest else None,
            "latest_score": latest.score if latest else None,
            "latest_age_seconds": age_seconds,
            "fresh": bool(
                latest is not None
                and age_seconds is not None
                and 0 <= age_seconds <= self.engine.config.evidence_max_age_seconds
            ),
        }

    @staticmethod
    def _us_theme_allows_asia_supplement(
        *,
        theme: str,
        signal: Dict[str, Any],
        session_stage: str,
    ) -> bool:
        """Only supplement an explicit same-session US coverage shortfall."""

        normalized_theme = str(theme or "").strip().lower()
        normalized_stage = str(session_stage or "").strip().lower()
        expected_reason = {
            "premarket": "premarket_theme_coverage_insufficient",
            "close": "us_close_theme_coverage_insufficient",
        }.get(normalized_stage)
        signal_theme = US_PREMARKET_A_SHARE_THEME_MAP.get(normalized_theme)
        snapshot = signal.get("snapshot")
        if (
            normalized_theme not in ASIA_THEME_EVIDENCE_CODES
            or expected_reason is None
            or not signal_theme
            or signal.get("available") is not False
            or signal.get("reason") != expected_reason
            or not isinstance(snapshot, dict)
        ):
            return False
        session_date = str(signal.get("session_date") or "").strip()
        if (
            not session_date
            or str(snapshot.get("session_date") or "").strip() != session_date
            or str(snapshot.get("session_stage") or "").strip().lower()
            != normalized_stage
        ):
            return False
        signal_observed_at = _parse_datetime(signal.get("observed_at"))
        snapshot_observed_at = _parse_datetime(snapshot.get("observed_at"))
        if (
            signal_observed_at is None
            or snapshot_observed_at is None
            or signal_observed_at != snapshot_observed_at
        ):
            return False
        theme_signals = (
            snapshot.get("theme_signals")
            if isinstance(snapshot.get("theme_signals"), dict)
            else {}
        )
        stored_signal = theme_signals.get(signal_theme)
        return bool(
            isinstance(stored_signal, dict)
            and stored_signal.get("available") is False
            and stored_signal.get("reason") == expected_reason
        )

    def get_runtime_status(self, *, now: Optional[datetime] = None) -> Dict[str, Any]:
        current = (now or _utc_now()).astimezone(timezone.utc)
        state = self._read_state()
        korea = self.get_status(now=current)
        korea_gate = self.engine.evaluate_korea_gate(
            self.load_korea_snapshots(now=current),
            theme="semiconductor",
            now=current,
        )
        korea["linked_technology_gate"] = korea_gate
        us_tech = self.get_us_tech_signal_for_cn_trade(now=current)
        nasdaq_futures = self.get_nasdaq_futures_signal_for_cn_trade(
            now=current,
        )
        cpo = self.get_cpo_signal_for_cn_trade(now=current)
        mapped_themes = (
            "semiconductor",
            "memory",
            "equipment",
            "materials",
            "cpo",
            "artificial_intelligence",
            "compute_services",
            "gaming",
            "pharma",
            "mlcc",
            "ccl",
        )
        premarket_themes = {
            theme: self.get_us_premarket_signal_for_cn_trade(
                theme=theme,
                now=current,
            )
            for theme in mapped_themes
        }
        close_themes = {
            theme: self.get_us_close_theme_signal_for_cn_trade(
                theme=theme,
                now=current,
            )
            for theme in mapped_themes
        }
        us_tech["collection"] = self._snapshot_collection_status(
            state,
            state_key="us_tech_snapshots",
            current=current,
            required_stages={"first_hour", "close"},
        )
        cpo["collection"] = self._snapshot_collection_status(
            state,
            state_key="cpo_snapshots",
            current=current,
            required_stages={"close"},
        )
        cn_open = self.get_cn_open_signal(now=current)
        gold = self.get_gold_signal_for_cn_trade(now=current)
        japan = self.get_japan_market_gate(now=current)
        asia_supply_chain = {
            theme: self.get_asia_theme_signal_for_cn_trade(
                theme=theme,
                now=current,
            )
            for theme in ASIA_THEME_EVIDENCE_CODES
        }
        required_themes = list(mapped_themes)
        premarket_available_themes = {
            theme
            for theme, signal in premarket_themes.items()
            if signal.get("available") is True
        }
        premarket_supplemented_themes = {
            theme
            for theme, signal in asia_supply_chain.items()
            if theme not in premarket_available_themes
            and signal.get("available") is True
            and self._us_theme_allows_asia_supplement(
                theme=theme,
                signal=premarket_themes.get(theme, {}),
                session_stage="premarket",
            )
        }
        premarket_qualified_themes = (
            premarket_available_themes | premarket_supplemented_themes
        )
        premarket_missing_themes = [
            theme
            for theme in required_themes
            if theme not in premarket_qualified_themes
        ]
        close_available_themes = {
            theme
            for theme, signal in close_themes.items()
            if signal.get("available") is True
        }
        close_supplemented_themes = {
            theme
            for theme, signal in asia_supply_chain.items()
            if theme not in close_available_themes
            and signal.get("available") is True
            and self._us_theme_allows_asia_supplement(
                theme=theme,
                signal=close_themes.get(theme, {}),
                session_stage="close",
            )
        }
        close_qualified_themes = (
            close_available_themes | close_supplemented_themes
        )
        close_missing_themes = [
            theme for theme in required_themes if theme not in close_qualified_themes
        ]
        premarket_collection = self._snapshot_collection_status(
            state,
            state_key="us_premarket_snapshots",
            current=current,
            required_stages={"premarket"},
        )
        latest_premarket_capture = self._latest_premarket_capture_summary(
            state,
            current=current,
            required_themes=required_themes,
        )
        close_collection = self._snapshot_collection_status(
            state,
            state_key="us_close_theme_snapshots",
            current=current,
            required_stages={"close"},
        )
        premarket_full_strategy_coverage = bool(
            not premarket_missing_themes
            and premarket_collection.get("required_stages_complete") is True
        )
        close_full_strategy_coverage = bool(
            not close_missing_themes
            and close_collection.get("required_stages_complete") is True
        )
        return {
            "strategy_id": STRATEGY_ID,
            "generated_at": current.isoformat(),
            "state_path": str(self.state_path),
            "korea": korea,
            "japan": japan,
            "asia_supply_chain": asia_supply_chain,
            "us_tech": us_tech,
            "nasdaq_futures": nasdaq_futures,
            "us_premarket": {
                "available": bool(premarket_available_themes),
                "fully_covered": premarket_full_strategy_coverage,
                "full_strategy_coverage": premarket_full_strategy_coverage,
                "required_themes": required_themes,
                "available_themes": sorted(premarket_available_themes),
                "supplemented_themes": sorted(premarket_supplemented_themes),
                "qualified_themes": sorted(premarket_qualified_themes),
                "missing_themes": premarket_missing_themes,
                "available_theme_count": len(premarket_available_themes),
                "qualified_theme_count": len(premarket_qualified_themes),
                "theme_count": len(premarket_themes),
                "themes": premarket_themes,
                "collection": premarket_collection,
                "latest_capture": latest_premarket_capture,
            },
            "us_close_themes": {
                "available": bool(close_available_themes),
                "fully_covered": close_full_strategy_coverage,
                "full_strategy_coverage": close_full_strategy_coverage,
                "required_themes": required_themes,
                "available_themes": sorted(close_available_themes),
                "supplemented_themes": sorted(close_supplemented_themes),
                "qualified_themes": sorted(close_qualified_themes),
                "missing_themes": close_missing_themes,
                "available_theme_count": len(close_available_themes),
                "qualified_theme_count": len(close_qualified_themes),
                "theme_count": len(close_themes),
                "themes": close_themes,
                "collection": close_collection,
            },
            "cpo": cpo,
            "cn_open": cn_open,
            "gold": gold,
            "buy_paths": {
                "linked_technology_ready": bool(
                    cn_open.get("available")
                    and us_tech.get("available")
                    and korea_gate.get("buy_allowed")
                    and nasdaq_futures.get("buy_allowed")
                ),
                "cpo_ready": bool(
                    cn_open.get("available")
                    and nasdaq_futures.get("buy_allowed")
                ),
                "cpo_optional_external_support": bool(cpo.get("supportive")),
                "gold_ready": bool(cn_open.get("available") and gold.get("available")),
            },
        }

    @staticmethod
    def _latest_premarket_capture_summary(
        payload: Dict[str, Any],
        *,
        current: datetime,
        required_themes: List[str],
    ) -> Dict[str, Any]:
        configured_universe_size = len({
            code
            for codes in US_PREMARKET_THEME_CODES.values()
            for code in codes
        })
        base = {
            "available": False,
            "session_date": None,
            "observed_at": None,
            "age_seconds": None,
            "session_stage": None,
            "universe_size": configured_universe_size,
            "streamed_quote_count": 0,
            "streamed_component_count": 0,
            "stream_rejected_component_count": 0,
            "gap_fill_request_count": 0,
            "collected_component_count": 0,
            "reused_fresh_component_count": 0,
            "required_themes": list(required_themes),
            "available_themes": [],
            "missing_themes": list(required_themes),
            "available_theme_count": 0,
            "theme_count": len(required_themes),
            "component_error_count": 0,
            "snapshot_persisted": False,
            "full_snapshot_coverage": False,
        }
        candidates = []
        seen_captures = set()
        for state_key in (
            US_PREMARKET_CAPTURE_ATTEMPTS_STATE_KEY,
            "us_premarket_snapshots",
        ):
            for item in payload.get(state_key, []):
                if not isinstance(item, dict):
                    continue
                observed_at = _parse_datetime(item.get("observed_at"))
                session_date = str(item.get("session_date") or "").strip()
                session_stage = str(item.get("session_stage") or "").strip().lower()
                capture_key = (session_date, observed_at)
                if (
                    observed_at is None
                    or observed_at > current
                    or not session_date
                    or session_stage != "premarket"
                    or capture_key in seen_captures
                ):
                    continue
                seen_captures.add(capture_key)
                candidates.append((observed_at, item))
        if not candidates:
            return base

        observed_at, latest = max(candidates, key=lambda candidate: candidate[0])
        theme_signals = (
            latest.get("theme_signals")
            if isinstance(latest.get("theme_signals"), dict)
            else {}
        )
        available_themes = []
        for theme in required_themes:
            signal_theme = US_PREMARKET_A_SHARE_THEME_MAP.get(theme)
            signal = theme_signals.get(signal_theme)
            if isinstance(signal, dict) and signal.get("available") is True:
                available_themes.append(theme)
        missing_themes = [
            theme for theme in required_themes if theme not in available_themes
        ]
        components = (
            latest.get("components")
            if isinstance(latest.get("components"), dict)
            else {}
        )
        component_errors = (
            latest.get("component_errors")
            if isinstance(latest.get("component_errors"), dict)
            else {}
        )

        def count_or_default(field: str, default: Optional[int]) -> Optional[int]:
            value = latest.get(field)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return default
            if not math.isfinite(float(value)) or value < 0:
                return default
            return int(value)

        return {
            **base,
            "available": True,
            "session_date": str(latest.get("session_date")),
            "observed_at": observed_at.isoformat(),
            "age_seconds": max(0, int((current - observed_at).total_seconds())),
            "session_stage": "premarket",
            "universe_size": count_or_default(
                "universe_size",
                configured_universe_size,
            ),
            "streamed_quote_count": count_or_default(
                "streamed_quote_count",
                None,
            ),
            "streamed_component_count": count_or_default(
                "streamed_component_count",
                None,
            ),
            "stream_rejected_component_count": count_or_default(
                "stream_rejected_component_count",
                None,
            ),
            "gap_fill_request_count": count_or_default(
                "gap_fill_request_count",
                None,
            ),
            "collected_component_count": count_or_default(
                "collected_component_count",
                len(components),
            ),
            "reused_fresh_component_count": count_or_default(
                "reused_fresh_component_count",
                None,
            ),
            "available_themes": available_themes,
            "missing_themes": missing_themes,
            "available_theme_count": len(available_themes),
            "component_error_count": len(component_errors),
            "snapshot_persisted": bool(latest.get("snapshot_persisted", True)),
            "full_snapshot_coverage": not missing_themes,
        }

    @staticmethod
    def _snapshot_collection_status(
        payload: Dict[str, Any],
        *,
        state_key: str,
        current: datetime,
        required_stages: set[str],
    ) -> Dict[str, Any]:
        records = []
        for item in payload.get(state_key, []):
            if not isinstance(item, dict):
                continue
            observed_at = _parse_datetime(item.get("observed_at"))
            session_date = str(item.get("session_date") or "").strip()
            stage = str(item.get("session_stage") or "").strip().lower()
            if (
                observed_at is None
                or observed_at > current
                or not session_date
                or not stage
            ):
                continue
            records.append((observed_at, session_date, stage))
        if not records:
            return {
                "latest_session_date": None,
                "latest_observed_at": None,
                "latest_age_seconds": None,
                "session_stage_counts": {},
                "required_stages": sorted(required_stages),
                "required_stages_complete": False,
            }
        latest_observed_at, latest_session_date, _latest_stage = max(
            records,
            key=lambda record: record[0],
        )
        stage_counts: Dict[str, int] = {}
        for _observed_at, session_date, stage in records:
            if session_date != latest_session_date:
                continue
            stage_counts[stage] = stage_counts.get(stage, 0) + 1
        return {
            "latest_session_date": latest_session_date,
            "latest_observed_at": latest_observed_at.isoformat(),
            "latest_age_seconds": max(
                0,
                int((current - latest_observed_at).total_seconds()),
            ),
            "session_stage_counts": stage_counts,
            "required_stages": sorted(required_stages),
            "required_stages_complete": required_stages.issubset(stage_counts),
        }

    def _append_korea_snapshot(self, record: Dict[str, Any]) -> None:
        self._append_snapshot("korea_snapshots", record)

    def _reuse_fresh_us_theme_components(
        self,
        *,
        payload: Dict[str, Any],
        state_keys: tuple[str, ...],
        session_date: str,
        session_stage: str,
        collected_at: datetime,
        components: Dict[str, Dict[str, Any]],
        component_errors: Dict[str, str],
        expected_component_count: int,
        regular_session_open_at: Optional[datetime] = None,
    ) -> int:
        previous_captures = []
        seen_captures = set()
        for state_key in state_keys:
            for previous in payload.get(state_key, []):
                if not isinstance(previous, dict):
                    continue
                capture_key = (
                    str(previous.get("session_date") or ""),
                    str(previous.get("observed_at") or ""),
                )
                if capture_key in seen_captures:
                    continue
                seen_captures.add(capture_key)
                previous_captures.append(previous)
        previous_captures.sort(
            key=lambda item: _parse_datetime(item.get("observed_at"))
            or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )

        reused_component_count = 0
        for previous in previous_captures:
            if str(previous.get("session_date") or "") != session_date:
                continue
            if (
                str(previous.get("session_stage") or "").strip().lower()
                != session_stage
            ):
                continue
            previous_observed_at = _parse_datetime(previous.get("observed_at"))
            if previous_observed_at is None or previous_observed_at > collected_at:
                continue
            previous_components = previous.get("components")
            if not isinstance(previous_components, dict):
                continue
            for code, item in previous_components.items():
                if code in components or not isinstance(item, dict):
                    continue
                provider_at = _parse_datetime(item.get("provider_timestamp"))
                if provider_at is None:
                    continue
                if (
                    regular_session_open_at is not None
                    and provider_at >= regular_session_open_at
                ):
                    continue
                age_seconds = (collected_at - provider_at).total_seconds()
                if not (
                    -1
                    <= age_seconds
                    <= self.engine.config.evidence_max_age_seconds
                ):
                    continue
                components[code] = dict(item)
                component_errors.pop(code, None)
                reused_component_count += 1
            if len(components) >= expected_component_count:
                break
        return reused_component_count

    def _record_us_theme_capture(
        self,
        record: Dict[str, Any],
        *,
        attempts_state_key: str,
        snapshots_state_key: str,
        persist_snapshot: bool,
    ) -> None:
        with self._lock:
            payload = self._read_state_unlocked()
            attempts = [
                item
                for item in payload.get(attempts_state_key, [])
                if isinstance(item, dict)
            ]
            attempts.append(record)
            payload[attempts_state_key] = attempts[
                -self.max_runtime_snapshots :
            ]
            if persist_snapshot:
                snapshots = [
                    item
                    for item in payload.get(snapshots_state_key, [])
                    if isinstance(item, dict)
                ]
                snapshots.append(record)
                payload[snapshots_state_key] = snapshots[
                    -self.max_runtime_snapshots :
                ]
            payload.update(
                {
                    "schema_version": 1,
                    "strategy_id": STRATEGY_ID,
                    "updated_at": _utc_now().isoformat(),
                }
            )
            self._write_state_unlocked(payload)

    def _append_snapshot(self, key: str, record: Dict[str, Any]) -> None:
        with self._lock:
            payload = self._read_state_unlocked()
            snapshots = [item for item in payload.get(key, []) if isinstance(item, dict)]
            snapshots.append(record)
            payload.update(
                {
                    "schema_version": 1,
                    "strategy_id": STRATEGY_ID,
                    "updated_at": _utc_now().isoformat(),
                    key: snapshots[-self.max_runtime_snapshots :],
                }
            )
            self._write_state_unlocked(payload)

    def _read_state(self) -> Dict[str, Any]:
        with self._lock:
            return self._read_state_unlocked()

    def _read_state_unlocked(self) -> Dict[str, Any]:
        if not self.state_path.exists():
            return {"schema_version": 1, "strategy_id": STRATEGY_ID, "korea_snapshots": []}
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Failed to read cross-market strategy state: %s", exc)
            return {"schema_version": 1, "strategy_id": STRATEGY_ID, "korea_snapshots": []}
        return payload if isinstance(payload, dict) else {}

    def _write_state_unlocked(self, payload: Dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        temporary.replace(self.state_path)

    def _get_timestamped_quote(self, code: str):
        strict_getter = getattr(
            self.data_fetcher_manager,
            "get_realtime_quote_with_provider_timestamp",
            None,
        )
        if callable(strict_getter):
            return strict_getter(code)
        return self.data_fetcher_manager.get_realtime_quote(code)

    def _get_timestamped_us_quote(self, code: str):
        strict_us_getter = getattr(
            self.data_fetcher_manager,
            "get_cross_market_us_quote_with_provider_timestamp",
            None,
        )
        if callable(strict_us_getter):
            return strict_us_getter(code)
        return self._get_timestamped_quote(code)

    def _get_timestamped_us_premarket_quote(self, code: str):
        strict_premarket_getter = getattr(
            self.data_fetcher_manager,
            "get_cross_market_us_premarket_quote_with_provider_timestamp",
            None,
        )
        if callable(strict_premarket_getter):
            return strict_premarket_getter(code)
        return self._get_timestamped_us_quote(code)
