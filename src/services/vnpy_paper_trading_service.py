# -*- coding: utf-8 -*-
"""vn.py-style paper trading service.

The first integration step keeps execution in DSA's local portfolio ledger while
exposing a vn.py paper-trading shaped surface. If vn.py is installed or hosted
beside DSA, the status endpoint reports that capability without making real
broker/gateway calls from this module.
"""

from __future__ import annotations

import importlib.util
import copy
import json
import logging
import math
import os
import threading
import time
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from data_provider.base import DataFetcherManager
from src.config import get_config
from src.core import trading_calendar
from src.repositories.stock_selection_agent_repo import StockSelectionAgentRepository
from src.services.alphasift_service import AlphaSiftService
from src.services.market_light_service import load_previous_snapshot
from src.services.portfolio_service import (
    PortfolioConflictError,
    PortfolioOversellError,
    PortfolioService,
)
from src.services.vnpy_adapter import (
    VnpyAdapterError,
    VnpyEventSubscriptionBridge,
    VnpyMainEngineBridge,
    build_vnpy_cancel_request_payload,
    build_vnpy_order_request_payload,
    get_vnpy_adapter_status,
    get_vnpy_bridge_status,
    get_vnpy_event_bridge_status,
)

logger = logging.getLogger(__name__)

VNPY_PAPER_BROKER = "vnpy_paper"
VNPY_PAPER_ALERT_TARGET = "vnpy_paper"
VNPY_PAPER_ALERT_SOURCE = "vnpy_paper_auto"
VNPY_PAPER_ACCOUNT_NAME = "vn.py 模拟交易"
VNPY_PAPER_CONFIG_PATH = Path("data") / "vnpy_paper_trading.json"
MARKET_CURRENCIES = {
    "cn": "CNY",
    "hk": "HKD",
    "us": "USD",
    "jp": "JPY",
    "kr": "KRW",
    "tw": "TWD",
}
PAPER_EPS = 1e-8
TRADE_PLAN_RETRY_MAX_ATTEMPTS = 3
TRADE_PLAN_RETRY_COOLDOWN_SECONDS = 60
TRADE_PLAN_AUTO_RETRY_MAX_PLANS = 3
TRADE_PLAN_AUTO_RETRY_SCAN_LIMIT = 50
TRADE_PLAN_AUTO_RETRY_INTERVAL_SECONDS = 300
TRADE_PLAN_ORDER_TIMEOUT_SECONDS = 30 * 60
TRADE_PLAN_RECONCILIATION_GRACE_SECONDS = 60
STATUS_SNAPSHOT_CACHE_TTL_SECONDS = 10
RETRYABLE_TRADE_PLAN_EXECUTION_MODES = {"manual_approval", "paper", "vnpy_paper"}
AUTO_RETRY_TRADE_PLAN_EXECUTION_MODES = {"paper", "vnpy_paper"}
ALPHASIFT_FALLBACK_STRATEGIES = (
    "dual_low",
    "quality_value",
    "balanced_alpha",
    "momentum_quality",
    "capital_heat",
    "oversold_reversal",
    "shrink_pullback",
    "volume_breakout",
)
ALLOWED_AUTO_MARKETS = {"cn", "hk", "us", "jp", "kr", "tw"}
LLM_DYNAMIC_AGENT_PLAN_PROMPT_VERSION = "vnpy_paper_dynamic_agent_plan_v2"
LLM_DYNAMIC_AGENT_PLAN_EVALUATOR_VERSION = "dynamic_plan_guardrails_v1"
LLM_PRE_TRADE_REVIEW_PROMPT_VERSION = "vnpy_paper_pre_trade_review_v1"
LLM_PRE_TRADE_REVIEW_EVALUATOR_VERSION = "pre_trade_fail_closed_v1"
RETRYABLE_TRADE_PLAN_SKIP_REASONS = {
    "cash_below_min_lot",
    "cash_insufficient",
    "oversell",
    "position_exists",
    "price_unavailable",
    "quantity_too_small",
    "unsupported_execution_route",
    "vnpy_bridge_submit_failed",
    "vnpy_bridge_unavailable",
}
NON_RETRYABLE_TRADE_PLAN_FAILURE_REASONS = {
    "vnpy_cancel_timeout",
    "vnpy_order_cancelled",
    "vnpy_order_failed",
    "vnpy_order_rejected",
    "vnpy_order_timeout",
    "vnpy_partial_fill_timeout",
}
ACTIVE_VNPY_TRADE_PLAN_STATUSES = {"submitted", "part_filled", "cancel_requested"}
AUTO_SIGNAL_EXIT_ACTIONS = {"reduce", "sell", "avoid"}


@dataclass(frozen=True)
class VnpyPaperSettings:
    enabled: bool = True
    account_id: Optional[int] = None
    initial_cash: float = 100000.0
    auto_trade_enabled: bool = False
    auto_strategy: str = "dual_low"
    auto_market: str = "cn"
    auto_max_results: int = 3
    auto_cash_per_order: float = 10000.0
    auto_interval_minutes: int = 1440
    auto_min_score: Optional[float] = None
    auto_skip_existing_positions: bool = True
    auto_execution_mode: str = "paper"
    auto_max_positions: int = 10
    auto_max_single_position_value: Optional[float] = None
    auto_max_total_position_value: Optional[float] = None
    auto_max_total_position_pct: Optional[float] = None
    auto_max_industry_position_value: Optional[float] = None
    auto_max_industry_position_pct: Optional[float] = None
    auto_daily_max_orders: Optional[int] = None
    auto_daily_budget: Optional[float] = None
    auto_trade_time_gate_enabled: bool = True
    auto_symbol_blacklist: List[str] = field(default_factory=list)
    auto_exclude_st: bool = True
    auto_exclude_suspended: bool = True
    auto_exclude_price_limit: bool = True
    auto_min_turnover: Optional[float] = None
    auto_min_cash_balance: Optional[float] = None
    auto_max_drawdown_pct: Optional[float] = None
    auto_market_light_gate_enabled: bool = False
    auto_market_light_block_statuses: List[str] = field(default_factory=lambda: ["red"])
    auto_failure_fuse_enabled: bool = False
    auto_failure_fuse_threshold: int = 3
    auto_sell_enabled: bool = False
    auto_stop_loss_pct: Optional[float] = None
    auto_take_profit_pct: Optional[float] = None
    auto_trailing_stop_pct: Optional[float] = None
    auto_max_holding_days: Optional[int] = None
    auto_sell_position_pct: Optional[float] = None
    auto_signal_exit_enabled: bool = False
    auto_no_progress_days: Optional[int] = None
    auto_no_progress_min_return_pct: Optional[float] = None
    auto_rebalance_enabled: bool = False
    auto_target_position_weights: Dict[str, float] = field(default_factory=dict)
    auto_target_industry_weights: Dict[str, float] = field(default_factory=dict)
    auto_llm_plan_enabled: bool = False
    auto_llm_review_enabled: bool = False
    vnpy_gateway_name: Optional[str] = None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _safe_int(value: Any) -> Optional[int]:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number


class VnpyPaperTradingService:
    """Coordinate local paper execution and AlphaSift-driven auto orders."""

    _lock = threading.RLock()
    _status_snapshot_cache: Dict[Tuple[str, int], Dict[str, Any]] = {}

    def __init__(
        self,
        *,
        portfolio_service: Optional[PortfolioService] = None,
        data_fetcher_manager: Optional[DataFetcherManager] = None,
        agent_repo: Optional[StockSelectionAgentRepository] = None,
        config_path: Optional[Path] = None,
        vnpy_main_engine: Optional[Any] = None,
        vnpy_event_engine: Optional[Any] = None,
        decision_signal_service: Optional[Any] = None,
    ) -> None:
        self.portfolio = portfolio_service or PortfolioService()
        self.data_fetcher_manager = data_fetcher_manager or DataFetcherManager()
        self.agent_repo = agent_repo or StockSelectionAgentRepository()
        self.config_path = config_path or VNPY_PAPER_CONFIG_PATH
        self.vnpy_main_engine = vnpy_main_engine
        self.vnpy_event_engine = vnpy_event_engine or getattr(vnpy_main_engine, "event_engine", None)
        self.decision_signal_service = decision_signal_service

    def _record_auto_trade_alert_event(
        self,
        event_type: str,
        *,
        status: str = "triggered",
        reason: Optional[str] = None,
        observed_value: Optional[Any] = None,
        threshold: Optional[Any] = None,
        diagnostics: Optional[Dict[str, Any]] = None,
    ) -> None:
        try:
            from src.services.alert_service import AlertService

            alert_service = AlertService()
            trigger = alert_service.record_system_event(
                target=VNPY_PAPER_ALERT_TARGET,
                event_type=event_type,
                status=status,
                reason=reason or event_type,
                data_source=VNPY_PAPER_ALERT_SOURCE,
                observed_value=observed_value,
                threshold=threshold,
                diagnostics=diagnostics or {},
            )
        except Exception as exc:  # noqa: BLE001 - alert history must not block trading recovery.
            logger.warning("Failed to record vn.py paper alert event %s: %s", event_type, exc)
            return

        self._notify_auto_trade_alert_event(
            alert_service=alert_service,
            trigger_id=_safe_int(trigger.get("id") if isinstance(trigger, dict) else None),
            event_type=event_type,
            status=status,
            reason=reason,
            observed_value=observed_value,
            threshold=threshold,
            diagnostics=diagnostics,
        )

    def _notify_auto_trade_alert_event(
        self,
        *,
        alert_service: Any,
        trigger_id: Optional[int],
        event_type: str,
        status: str,
        reason: Optional[str],
        observed_value: Optional[Any],
        threshold: Optional[Any],
        diagnostics: Optional[Dict[str, Any]],
    ) -> None:
        try:
            dispatch = self._send_auto_trade_alert_notification_safely(
                alert_service=alert_service,
                event_type=event_type,
                status=status,
                reason=reason,
                observed_value=observed_value,
                threshold=threshold,
                diagnostics=diagnostics,
            )
            self._record_auto_trade_notification_attempts_safely(
                alert_service=alert_service,
                trigger_id=trigger_id,
                dispatch=dispatch,
            )
        except Exception as exc:  # noqa: BLE001 - notification diagnostics must not block trading recovery.
            logger.warning("Failed to dispatch vn.py paper alert notification %s: %s", event_type, exc)

    def _send_auto_trade_alert_notification_safely(
        self,
        *,
        alert_service: Any,
        event_type: str,
        status: str,
        reason: Optional[str],
        observed_value: Optional[Any],
        threshold: Optional[Any],
        diagnostics: Optional[Dict[str, Any]],
    ) -> Any:
        try:
            return self._send_auto_trade_alert_notification(
                event_type=event_type,
                status=status,
                reason=reason,
                observed_value=observed_value,
                threshold=threshold,
                diagnostics=diagnostics,
            )
        except Exception as exc:  # noqa: BLE001 - return a recordable dispatch failure.
            from src.notification import ChannelAttemptResult, NotificationDispatchResult

            sanitized = alert_service._sanitize_text(str(exc) or "notification failed")
            logger.warning(
                "Failed to send vn.py paper alert notification %s: %s",
                event_type,
                sanitized,
            )
            return NotificationDispatchResult(
                dispatched=False,
                success=False,
                status="exception",
                channel_results=[
                    ChannelAttemptResult(
                        channel="__dispatch__",
                        success=False,
                        error_code="exception",
                        retryable=True,
                        diagnostics=sanitized,
                    )
                ],
                message=sanitized,
            )

    def _send_auto_trade_alert_notification(
        self,
        *,
        event_type: str,
        status: str,
        reason: Optional[str],
        observed_value: Optional[Any],
        threshold: Optional[Any],
        diagnostics: Optional[Dict[str, Any]],
    ) -> Any:
        from src.notification import NotificationBuilder, NotificationService

        clean_event_type = str(event_type or "auto_trade_alert").strip().lower()
        clean_status = str(status or "triggered").strip().lower()
        clean_reason = str(reason or clean_event_type).strip() or clean_event_type
        title = f"vn.py paper auto trading alert | {clean_event_type}"
        content_lines = [
            f"Status: {clean_status}",
            f"Reason: {clean_reason}",
        ]
        if observed_value is not None:
            content_lines.append(f"Observed: {observed_value}")
        if threshold is not None:
            content_lines.append(f"Threshold: {threshold}")
        context = self._auto_trade_alert_notification_context(diagnostics or {})
        if context:
            content_lines.append(f"Context: {context}")

        alert_text = NotificationBuilder.build_simple_alert(
            title=title,
            content="\n".join(content_lines),
            alert_type="warning",
        )
        severity = "critical" if clean_status in {"failed", "triggered"} else "warning"
        dedup_reason = self._notification_key_fragment(clean_reason)
        return NotificationService().send_with_results(
            alert_text,
            route_type="alert",
            severity=severity,
            dedup_key=f"{VNPY_PAPER_ALERT_SOURCE}:{clean_event_type}:{clean_status}:{dedup_reason}",
            cooldown_key=f"{VNPY_PAPER_ALERT_SOURCE}:{clean_event_type}:{dedup_reason}",
        )

    @staticmethod
    def _auto_trade_alert_notification_context(diagnostics: Dict[str, Any]) -> Optional[str]:
        fields = []
        for key in (
            "agent_run_uid",
            "plan_uid",
            "symbol",
            "strategy",
            "market",
            "execution_mode",
        ):
            value = diagnostics.get(key)
            if value is not None and str(value).strip():
                fields.append(f"{key}={value}")
        return ", ".join(fields) if fields else None

    @staticmethod
    def _notification_key_fragment(value: Any) -> str:
        fragment = str(value or "").strip().lower()
        if not fragment:
            return "unknown"
        normalized = "".join(ch if ch.isalnum() else "_" for ch in fragment)
        normalized = "_".join(part for part in normalized.split("_") if part)
        return (normalized or "unknown")[:80]

    def _record_auto_trade_notification_attempts_safely(
        self,
        *,
        alert_service: Any,
        trigger_id: Optional[int],
        dispatch: Any,
    ) -> int:
        try:
            return self._record_auto_trade_notification_attempts(
                alert_service=alert_service,
                trigger_id=trigger_id,
                dispatch=dispatch,
            )
        except Exception as exc:  # noqa: BLE001 - notification audit must not block trading recovery.
            logger.warning(
                "Failed to record vn.py paper alert notification attempt: %s",
                alert_service._sanitize_text(str(exc) or "notification attempt write failed"),
            )
            return 0

    def _record_auto_trade_notification_attempts(
        self,
        *,
        alert_service: Any,
        trigger_id: Optional[int],
        dispatch: Any,
    ) -> int:
        channel_results = list(getattr(dispatch, "channel_results", None) or [])
        if not channel_results:
            channel_results = [self._synthetic_auto_trade_notification_attempt(dispatch)]

        recorded = 0
        for attempt_index, item in enumerate(channel_results, start=1):
            fields = {
                "trigger_id": trigger_id,
                "channel": str(getattr(item, "channel", None) or "__dispatch__")[:32],
                "attempt": attempt_index,
                "success": bool(getattr(item, "success", False)),
                "error_code": getattr(item, "error_code", None),
                "retryable": bool(getattr(item, "retryable", False)),
                "latency_ms": _safe_int(getattr(item, "latency_ms", None)),
                "diagnostics": alert_service._sanitize_text(
                    getattr(item, "diagnostics", None) or getattr(dispatch, "message", None)
                ),
            }
            alert_service.repo.record_notification_attempt(fields)
            recorded += 1
        return recorded

    @staticmethod
    def _synthetic_auto_trade_notification_attempt(dispatch: Any) -> Any:
        from src.notification import ChannelAttemptResult

        status = str(getattr(dispatch, "status", None) or "unknown")
        channel_by_status = {
            "noise_suppressed": "__noise_suppressed__",
            "no_channel": "__no_channel__",
            "exception": "__dispatch__",
        }
        success = bool(getattr(dispatch, "success", False))
        return ChannelAttemptResult(
            channel=channel_by_status.get(status, "__dispatch__"),
            success=success,
            error_code=None if success else status,
            retryable=status not in {"noise_suppressed", "no_channel"},
            diagnostics=getattr(dispatch, "message", None),
        )

    # ------------------------------------------------------------------
    # Settings and status
    # ------------------------------------------------------------------
    def get_settings(self) -> VnpyPaperSettings:
        payload = self._read_config_payload()
        raw_settings = payload.get("settings") if isinstance(payload, dict) else None
        if not isinstance(raw_settings, dict):
            raw_settings = {}
        return self._normalize_settings(raw_settings)

    def update_settings(
        self,
        updates: Dict[str, Any],
        *,
        include_snapshot: bool = True,
        include_recent_trades: bool = True,
    ) -> Dict[str, Any]:
        with self._lock:
            current_payload = self._read_config_payload()
            current = self.get_settings()
            nullable_fields = {
                "account_id",
                "auto_min_score",
                "auto_daily_max_orders",
                "auto_daily_budget",
                "auto_min_turnover",
                "auto_min_cash_balance",
                "auto_max_drawdown_pct",
                "auto_max_single_position_value",
                "auto_max_total_position_value",
                "auto_max_total_position_pct",
                "auto_max_industry_position_value",
                "auto_max_industry_position_pct",
                "auto_stop_loss_pct",
                "auto_take_profit_pct",
                "auto_trailing_stop_pct",
                "auto_max_holding_days",
                "auto_sell_position_pct",
                "auto_no_progress_days",
                "auto_no_progress_min_return_pct",
                "vnpy_gateway_name",
            }
            update_fields = {
                key: value
                for key, value in updates.items()
                if key in VnpyPaperSettings.__dataclass_fields__
                and (value is not None or key in nullable_fields)
            }
            merged = self._normalize_settings({**asdict(current), **update_fields})
            payload = dict(current_payload) if isinstance(current_payload, dict) else {}
            payload["settings"] = asdict(merged)
            payload["updated_at"] = _utc_now_iso()
            self._write_config_payload(payload)
            return self.get_status(
                include_snapshot=include_snapshot,
                include_recent_trades=include_recent_trades,
            )

    def reset_failure_fuse(self) -> Dict[str, Any]:
        """Reset the consecutive-failure fuse baseline without deleting audit history."""

        with self._lock:
            payload = self._read_config_payload()
            if not isinstance(payload, dict):
                payload = {}
            reset_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            payload["failure_fuse_reset_at"] = reset_at
            payload["updated_at"] = reset_at
            self._write_config_payload(payload)
        return self.get_status(include_snapshot=False, include_recent_trades=False)

    def get_status(
        self,
        *,
        ensure_account: bool = False,
        include_snapshot: bool = True,
        include_recent_trades: bool = True,
    ) -> Dict[str, Any]:
        settings = self.get_settings()
        account = None
        snapshot = None
        recent_trades: List[Dict[str, Any]] = []
        diagnostics: Dict[str, Any] = {
            "detail_level": "full" if include_snapshot or include_recent_trades else "summary",
            "snapshot_requested": bool(include_snapshot),
            "snapshot_loaded": False,
            "snapshot_cache_hit": False,
            "snapshot_cache_ttl_seconds": STATUS_SNAPSHOT_CACHE_TTL_SECONDS,
            "recent_trades_requested": bool(include_recent_trades),
            "recent_trades_loaded": False,
            "vnpy_adapter": get_vnpy_adapter_status(),
            "vnpy_bridge": get_vnpy_bridge_status(
                main_engine=self.vnpy_main_engine,
                gateway_name=settings.vnpy_gateway_name,
            ),
            "vnpy_event_bridge": get_vnpy_event_bridge_status(event_engine=self.vnpy_event_engine),
            "vnpy_sync_state": self._vnpy_sync_state_summary(),
            "trading_window": self._trading_window_diagnostics(settings),
            "failure_fuse": self._failure_fuse_status(settings),
            "account_drawdown": self._account_drawdown_diagnostics(
                settings=settings,
                account=account,
                snapshot=None,
                requested=include_snapshot,
                update_peak=False,
            ),
            "industry_exposure": self._industry_exposure_diagnostics(
                settings=settings,
                snapshot=None,
                evaluated=False,
                requested=include_snapshot,
            ),
        }
        if ensure_account:
            account = self.ensure_account(settings=settings)
            settings = replace(settings, account_id=int(account["id"]))
        elif settings.account_id is not None:
            account = self._find_account(settings.account_id)

        if not include_snapshot:
            diagnostics["account_drawdown"] = self._account_drawdown_diagnostics(
                settings=settings,
                account=account,
                snapshot=None,
                requested=False,
                update_peak=False,
            )

        if account is not None:
            account_id = int(account["id"])
            if include_snapshot:
                try:
                    snapshot, cache_hit = self._get_cached_status_snapshot(account_id)
                    diagnostics["snapshot_loaded"] = snapshot is not None
                    diagnostics["snapshot_cache_hit"] = cache_hit
                    diagnostics["industry_exposure"] = self._industry_exposure_diagnostics(
                        settings=settings,
                        snapshot=snapshot,
                        evaluated=snapshot is not None,
                        requested=True,
                    )
                    diagnostics["account_drawdown"] = self._account_drawdown_diagnostics(
                        settings=settings,
                        account=account,
                        snapshot=snapshot,
                        requested=True,
                        update_peak=True,
                    )
                except Exception as exc:  # noqa: BLE001 - status must remain readable.
                    logger.warning("Failed to build vn.py paper snapshot: %s", exc)
                    diagnostics["snapshot_error"] = str(exc)
                    snapshot = None
            if include_recent_trades:
                try:
                    recent = self.portfolio.list_trade_events(account_id=account_id, page=1, page_size=20)
                    recent_trades = list(recent.get("items") or [])
                    diagnostics["recent_trades_loaded"] = True
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Failed to list vn.py paper trades: %s", exc)
                    diagnostics["recent_trades_error"] = str(exc)

        payload = self._read_config_payload()
        return {
            "available": bool(settings.enabled),
            "enabled": settings.enabled,
            "vnpy_available": self.vnpy_available(),
            "engine": "vnpy_local_paper_ledger",
            "mode": "local_paper",
            "settings": asdict(settings),
            "account": account,
            "snapshot": snapshot,
            "recent_trades": recent_trades,
            "last_auto_run": payload.get("last_auto_run") if isinstance(payload, dict) else None,
            "diagnostics": diagnostics,
        }

    def _get_cached_status_snapshot(self, account_id: int) -> Tuple[Optional[Dict[str, Any]], bool]:
        cache_key = self._status_snapshot_cache_key(account_id)
        now = time.monotonic()
        with self._lock:
            cached = self._status_snapshot_cache.get(cache_key)
            if isinstance(cached, dict):
                cached_at = _safe_float(cached.get("cached_at"))
                if (
                    cached_at is not None
                    and now - cached_at <= STATUS_SNAPSHOT_CACHE_TTL_SECONDS
                    and isinstance(cached.get("snapshot"), dict)
                ):
                    return copy.deepcopy(cached["snapshot"]), True

        snapshot = self.portfolio.get_portfolio_snapshot(account_id=account_id)
        with self._lock:
            self._status_snapshot_cache[cache_key] = {
                "cached_at": now,
                "snapshot": copy.deepcopy(snapshot),
            }
        return snapshot, False

    def _invalidate_status_snapshot_cache(self, account_id: Optional[int] = None) -> None:
        scope = self._status_snapshot_cache_scope()
        with self._lock:
            if account_id is None:
                keys = [key for key in self._status_snapshot_cache if key[0] == scope]
                for key in keys:
                    self._status_snapshot_cache.pop(key, None)
                return
            self._status_snapshot_cache.pop((scope, int(account_id)), None)

    def _status_snapshot_cache_key(self, account_id: int) -> Tuple[str, int]:
        return (self._status_snapshot_cache_scope(), int(account_id))

    def _status_snapshot_cache_scope(self) -> str:
        try:
            return str(self.config_path.resolve())
        except Exception:  # noqa: BLE001 - cache scoping should never block status.
            return str(self.config_path)

    def ensure_account(self, *, settings: Optional[VnpyPaperSettings] = None) -> Dict[str, Any]:
        settings = settings or self.get_settings()
        existing = self._find_account(settings.account_id) if settings.account_id is not None else None
        if existing is None:
            existing = self._find_default_account()
        if existing is None:
            existing = self.portfolio.create_account(
                name=VNPY_PAPER_ACCOUNT_NAME,
                broker=VNPY_PAPER_BROKER,
                market="cn",
                base_currency="CNY",
            )
            self.portfolio.record_cash_ledger(
                account_id=int(existing["id"]),
                event_date=date.today(),
                direction="in",
                amount=float(settings.initial_cash),
                currency="CNY",
                note="vn.py paper initial cash",
            )
            self._invalidate_status_snapshot_cache(int(existing["id"]))

        account_id = int(existing["id"])
        if settings.account_id != account_id:
            self.update_settings({"account_id": account_id})
        return existing

    def reset_account(
        self,
        *,
        include_snapshot: bool = True,
        include_recent_trades: bool = True,
    ) -> Dict[str, Any]:
        """Archive the current local paper account and create a clean one."""

        with self._lock:
            settings = self.get_settings()
            existing = self._find_account(settings.account_id) if settings.account_id is not None else None
            if existing is None:
                existing = self._find_default_account()

            archived_account_id: Optional[int] = None
            if existing is not None:
                broker = str(existing.get("broker") or "").strip()
                if broker != VNPY_PAPER_BROKER:
                    raise ValueError("configured_account_not_vnpy_paper")
                archived_account_id = int(existing["id"])
                self.portfolio.deactivate_account(archived_account_id)

            new_account = self.portfolio.create_account(
                name=VNPY_PAPER_ACCOUNT_NAME,
                broker=VNPY_PAPER_BROKER,
                market="cn",
                base_currency="CNY",
            )
            new_account_id = int(new_account["id"])
            self.portfolio.record_cash_ledger(
                account_id=new_account_id,
                event_date=date.today(),
                direction="in",
                amount=float(settings.initial_cash),
                currency="CNY",
                note="vn.py paper reset initial cash",
            )

            payload = self._read_config_payload()
            if not isinstance(payload, dict):
                payload = {}
            payload["settings"] = asdict(replace(settings, account_id=new_account_id))
            payload["auto_trailing_peaks"] = {}
            payload["last_account_reset"] = {
                "reset_at": _utc_now_iso(),
                "archived_account_id": archived_account_id,
                "new_account_id": new_account_id,
            }
            payload["updated_at"] = _utc_now_iso()
            self._write_config_payload(payload)
            self._invalidate_status_snapshot_cache(archived_account_id)
            self._invalidate_status_snapshot_cache(new_account_id)

        status = self.get_status(
            include_snapshot=include_snapshot,
            include_recent_trades=include_recent_trades,
        )
        status.setdefault("diagnostics", {})["account_reset"] = {
            "archived_account_id": archived_account_id,
            "new_account_id": new_account_id,
        }
        return status

    def list_paper_accounts(
        self,
        *,
        include_inactive: bool = True,
        include_hidden: bool = False,
    ) -> Dict[str, Any]:
        """List local vn.py paper accounts without reactivating archived ledgers."""

        settings = self.get_settings()
        current_account_id = _safe_int(settings.account_id)
        hidden_ids = self._hidden_archived_account_ids()
        items: List[Dict[str, Any]] = []
        for account in self.portfolio.list_accounts(include_inactive=include_inactive):
            if str(account.get("broker") or "").strip() != VNPY_PAPER_BROKER:
                continue
            account_id = _safe_int(account.get("id"))
            if account_id is not None and account_id in hidden_ids and not include_hidden:
                continue
            item = dict(account)
            item["is_current"] = account_id is not None and account_id == current_account_id
            item["archived"] = not bool(account.get("is_active", False))
            item["cleanup_hidden"] = account_id is not None and account_id in hidden_ids
            items.append(item)

        items.sort(
            key=lambda item: (
                bool(item.get("cleanup_hidden")),
                not bool(item.get("is_current")),
                bool(item.get("archived")),
                -int(item.get("id") or 0),
            )
        )
        return {
            "items": items,
            "count": len(items),
            "current_account_id": current_account_id,
            "hidden_count": len(hidden_ids),
        }

    def cleanup_archived_paper_accounts(
        self,
        account_ids: Optional[List[int]] = None,
        *,
        dry_run: bool = False,
        include_hidden: bool = False,
    ) -> Dict[str, Any]:
        """Hide archived local paper accounts from the vn.py paper history view."""

        requested_ids = {
            item
            for item in (_safe_int(value) for value in (account_ids or []))
            if item is not None and item > 0
        }
        with self._lock:
            settings = self.get_settings()
            current_account_id = _safe_int(settings.account_id)
            hidden_ids = self._hidden_archived_account_ids()
            hidden_before = set(hidden_ids)
            cleaned_ids: List[int] = []
            skipped: List[Dict[str, Any]] = []
            candidates: List[int] = []

            for account in self.portfolio.list_accounts(include_inactive=True):
                broker = str(account.get("broker") or "").strip()
                account_id = _safe_int(account.get("id"))
                if broker != VNPY_PAPER_BROKER or account_id is None:
                    continue
                if requested_ids and account_id not in requested_ids:
                    continue
                if account_id == current_account_id:
                    skipped.append({"account_id": account_id, "reason": "current_account"})
                    continue
                if account.get("is_active"):
                    skipped.append({"account_id": account_id, "reason": "account_active"})
                    continue
                candidates.append(account_id)
                if account_id in hidden_ids:
                    skipped.append({"account_id": account_id, "reason": "already_hidden"})
                    continue
                cleaned_ids.append(account_id)

            missing_ids = sorted(requested_ids - set(candidates) - {
                _safe_int(item.get("account_id")) or -1 for item in skipped
            })
            for account_id in missing_ids:
                skipped.append({"account_id": account_id, "reason": "paper_account_not_found"})

            if cleaned_ids and not dry_run:
                hidden_ids.update(cleaned_ids)
                payload = self._read_config_payload()
                if not isinstance(payload, dict):
                    payload = {}
                payload["hidden_archived_account_ids"] = sorted(hidden_ids)
                payload["last_account_cleanup"] = {
                    "cleaned_at": _utc_now_iso(),
                    "account_ids": sorted(cleaned_ids),
                    "dry_run": False,
                }
                payload["updated_at"] = _utc_now_iso()
                self._write_config_payload(payload)

        history = self.list_paper_accounts(include_inactive=True, include_hidden=include_hidden)
        return {
            "cleaned_account_ids": sorted(cleaned_ids),
            "skipped": skipped,
            "dry_run": dry_run,
            "hidden_count_before": len(hidden_before),
            "hidden_count_after": len(self._hidden_archived_account_ids()),
            "remaining_count": int(history.get("count") or 0),
            "accounts": history,
        }

    def restore_paper_account(
        self,
        account_id: int,
        *,
        include_snapshot: bool = True,
        include_recent_trades: bool = True,
    ) -> Dict[str, Any]:
        """Reactivate an archived local paper account and make it current."""

        target_id = _safe_int(account_id)
        if target_id is None or target_id <= 0:
            raise ValueError("account_id is required")

        with self._lock:
            settings = self.get_settings()
            previous_account_id = _safe_int(settings.account_id)
            target: Optional[Dict[str, Any]] = None
            deactivated_account_ids: List[int] = []
            accounts = self.portfolio.list_accounts(include_inactive=True)
            for account in accounts:
                current_id = _safe_int(account.get("id"))
                if current_id == target_id:
                    target = account

            if target is None:
                raise ValueError("paper_account_not_found")
            if str(target.get("broker") or "").strip() != VNPY_PAPER_BROKER:
                raise ValueError("account_not_vnpy_paper")

            for account in accounts:
                broker = str(account.get("broker") or "").strip()
                current_id = _safe_int(account.get("id"))
                if broker != VNPY_PAPER_BROKER or current_id is None or current_id == target_id:
                    continue
                if account.get("is_active"):
                    self.portfolio.deactivate_account(current_id)
                    deactivated_account_ids.append(current_id)

            restored = self.portfolio.update_account(target_id, is_active=True)
            if restored is None:
                raise ValueError("paper_account_not_found")

            payload = self._read_config_payload()
            if not isinstance(payload, dict):
                payload = {}
            hidden_ids = self._account_id_set(payload.get("hidden_archived_account_ids"))
            hidden_ids.discard(target_id)
            payload["hidden_archived_account_ids"] = sorted(hidden_ids)
            payload["settings"] = asdict(replace(settings, account_id=target_id))
            payload["auto_trailing_peaks"] = {}
            payload["last_account_restore"] = {
                "restored_at": _utc_now_iso(),
                "restored_account_id": target_id,
                "previous_account_id": previous_account_id,
                "deactivated_account_ids": deactivated_account_ids,
            }
            payload["updated_at"] = _utc_now_iso()
            self._write_config_payload(payload)
            self._invalidate_status_snapshot_cache(previous_account_id)
            self._invalidate_status_snapshot_cache(target_id)

        status = self.get_status(
            include_snapshot=include_snapshot,
            include_recent_trades=include_recent_trades,
        )
        status.setdefault("diagnostics", {})["account_restore"] = {
            "restored_account_id": target_id,
            "previous_account_id": previous_account_id,
            "deactivated_account_ids": deactivated_account_ids,
        }
        return status

    def get_performance_summary(
        self,
        *,
        run_limit: int = 50,
        created_from: Optional[datetime] = None,
        created_to: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Summarize local paper account performance and recent Agent execution quality."""

        settings = self.get_settings()
        account = self._find_account(settings.account_id) if settings.account_id is not None else None
        if account is None:
            account = self._find_default_account()

        diagnostics: Dict[str, Any] = {
            "source": "portfolio_snapshot_and_agent_audit",
            "run_limit": max(1, min(100, int(run_limit or 50))),
            "created_from": created_from.isoformat() if created_from is not None else None,
            "created_to": created_to.isoformat() if created_to is not None else None,
        }
        account_snapshot: Dict[str, Any] = {}
        if account is not None:
            try:
                snapshot = self.portfolio.get_portfolio_snapshot(account_id=int(account["id"]))
                accounts = snapshot.get("accounts") or []
                if accounts and isinstance(accounts[0], dict):
                    account_snapshot = accounts[0]
                elif isinstance(snapshot, dict):
                    account_snapshot = snapshot
            except Exception as exc:  # noqa: BLE001 - performance should degrade, not fail status pages.
                logger.warning("Failed to build vn.py paper performance snapshot: %s", exc)
                diagnostics["snapshot_error"] = str(exc)
        else:
            diagnostics["account"] = "not_initialized"

        initial_cash = _safe_float(settings.initial_cash) or 0.0
        total_cash = _safe_float(account_snapshot.get("total_cash"))
        total_equity = _safe_float(account_snapshot.get("total_equity"))
        total_market_value = _safe_float(account_snapshot.get("total_market_value"))
        realized_pnl = _safe_float(account_snapshot.get("realized_pnl"))
        unrealized_pnl = _safe_float(account_snapshot.get("unrealized_pnl"))
        total_pnl = (
            round(total_equity - initial_cash, 6)
            if total_equity is not None and initial_cash > 0
            else None
        )
        return_pct = (
            round((total_pnl / initial_cash) * 100.0, 6)
            if total_pnl is not None and initial_cash > 0
            else None
        )
        paper_performance = (
            self._paper_trade_performance(
                int(account["id"]),
                initial_cash=initial_cash,
                current_equity=total_equity,
                current_market_value=total_market_value,
            )
            if account is not None
            else {
                "trade_metrics": self._empty_trade_metrics(),
                "risk_metrics": self._empty_risk_metrics(),
                "equity_curve": [],
                "daily_returns": [],
                "monthly_returns": [],
            }
        )
        trade_metrics = paper_performance["trade_metrics"]
        risk_metrics = paper_performance["risk_metrics"]
        equity_curve = paper_performance["equity_curve"]
        daily_returns = paper_performance["daily_returns"]
        monthly_returns = paper_performance["monthly_returns"]

        runs_payload = self.agent_repo.list_runs(
            limit=int(diagnostics["run_limit"]),
            offset=0,
            created_from=created_from,
            created_to=created_to,
        )
        runs = [item for item in list(runs_payload.get("items") or []) if isinstance(item, dict)]
        status_counts: Counter[str] = Counter()
        execution_mode_counts: Counter[str] = Counter()
        data_quality_counts: Counter[str] = Counter()
        trade_plan_status_counts: Counter[str] = Counter()
        skip_reason_counts: Counter[str] = Counter()
        traded_symbols: Counter[str] = Counter()
        strategy_stats: Dict[str, Dict[str, Any]] = {}
        industry_stats: Dict[str, Dict[str, Any]] = {}
        candidate_count = 0
        planned_count = 0
        submitted_count = 0
        skipped_count = 0
        latest_run_at: Optional[Any] = None

        for run in runs:
            status = str(run.get("status") or "unknown")
            strategy = str(run.get("strategy") or "unknown")
            status_counts[status] += 1
            candidate_count += int(run.get("candidate_count") or 0)
            planned_count += int(run.get("planned_count") or 0)
            submitted_count += int(run.get("submitted_count") or 0)
            skipped_count += int(run.get("skipped_count") or 0)
            strategy_item = strategy_stats.setdefault(
                strategy,
                {
                    "key": strategy,
                    "run_count": 0,
                    "candidate_count": 0,
                    "planned_count": 0,
                    "submitted_count": 0,
                    "skipped_count": 0,
                    "filled_plan_count": 0,
                    "planned_cash_amount": 0.0,
                    "filled_cash_amount": 0.0,
                },
            )
            strategy_item["run_count"] += 1
            strategy_item["candidate_count"] += int(run.get("candidate_count") or 0)
            strategy_item["planned_count"] += int(run.get("planned_count") or 0)
            strategy_item["submitted_count"] += int(run.get("submitted_count") or 0)
            strategy_item["skipped_count"] += int(run.get("skipped_count") or 0)
            latest_run_at = latest_run_at or run.get("started_at") or run.get("created_at")
            diagnostics_payload = run.get("diagnostics") if isinstance(run.get("diagnostics"), dict) else {}
            settings_payload = run.get("settings") if isinstance(run.get("settings"), dict) else {}
            execution_mode = (
                diagnostics_payload.get("execution_mode")
                or settings_payload.get("auto_execution_mode")
                or "unknown"
            )
            execution_mode_counts[str(execution_mode)] += 1
            data_quality = diagnostics_payload.get("data_quality")
            if isinstance(data_quality, dict):
                data_quality_counts[str(data_quality.get("status") or "unknown")] += 1

            run_uid = str(run.get("run_uid") or "")
            if not run_uid:
                continue
            detail = self.agent_repo.get_run_detail(run_uid)
            if not isinstance(detail, dict):
                continue
            trade_plans = [plan for plan in list(detail.get("trade_plans") or []) if isinstance(plan, dict)]
            for plan in trade_plans:
                if not isinstance(plan, dict):
                    continue
                plan_status = str(plan.get("status") or "unknown")
                trade_plan_status_counts[plan_status] += 1
                reason = str(plan.get("skip_reason") or "").strip()
                if reason:
                    skip_reason_counts[reason] += 1
                planned_cash = _safe_float(plan.get("planned_cash_amount")) or 0.0
                strategy_item["planned_cash_amount"] += planned_cash
                if plan_status == "filled":
                    symbol = str(plan.get("symbol") or "").strip()
                    if symbol:
                        traded_symbols[symbol] += 1
                    strategy_item["filled_plan_count"] += 1
                    submitted_quantity = _safe_float(plan.get("submitted_quantity")) or 0.0
                    submitted_price = _safe_float(plan.get("submitted_price")) or 0.0
                    strategy_item["filled_cash_amount"] += submitted_quantity * submitted_price
            if not trade_plans:
                for decision in list(detail.get("decisions") or []):
                    if not isinstance(decision, dict):
                        continue
                    reason = str(decision.get("reason") or "").strip()
                    if reason:
                        skip_reason_counts[reason] += 1
            for decision in list(detail.get("decisions") or []):
                if not isinstance(decision, dict):
                    continue
                industry = self._decision_industry(decision)
                industry_item = industry_stats.setdefault(
                    industry,
                    {
                        "key": industry,
                        "decision_count": 0,
                        "filled_count": 0,
                        "skipped_count": 0,
                        "planned_cash_amount": 0.0,
                        "filled_cash_amount": 0.0,
                    },
                )
                industry_item["decision_count"] += 1
                if str(decision.get("status") or "").strip().lower() == "filled" or decision.get("trade_id"):
                    industry_item["filled_count"] += 1
                    quantity = _safe_float(decision.get("quantity")) or 0.0
                    price = _safe_float(decision.get("price")) or 0.0
                    industry_item["filled_cash_amount"] += quantity * price
                elif str(decision.get("status") or "").strip().lower() == "skipped":
                    industry_item["skipped_count"] += 1
                industry_item["planned_cash_amount"] += _safe_float(decision.get("cash_amount")) or 0.0

        action_total = submitted_count + skipped_count + planned_count
        fill_rate_pct = round((submitted_count / action_total) * 100.0, 6) if action_total > 0 else None
        return {
            "account": account,
            "initial_cash": initial_cash,
            "total_cash": total_cash,
            "total_market_value": total_market_value,
            "total_equity": total_equity,
            "realized_pnl": realized_pnl,
            "unrealized_pnl": unrealized_pnl,
            "total_pnl": total_pnl,
            "return_pct": return_pct,
            "run_window": {
                "limit": int(diagnostics["run_limit"]),
                "run_count": len(runs),
                "total": int(runs_payload.get("total") or len(runs)),
                "latest_run_at": latest_run_at,
                "created_from": diagnostics["created_from"],
                "created_to": diagnostics["created_to"],
            },
            "agent": {
                "candidate_count": candidate_count,
                "planned_count": planned_count,
                "submitted_count": submitted_count,
                "skipped_count": skipped_count,
                "fill_rate_pct": fill_rate_pct,
            },
            "trade_metrics": trade_metrics,
            "risk_metrics": risk_metrics,
            "equity_curve": equity_curve,
            "daily_returns": daily_returns,
            "monthly_returns": monthly_returns,
            "strategy_attribution": self._attribution_payload(
                strategy_stats,
                sort_key="filled_cash_amount",
                fill_rate_keys=("filled_plan_count", "planned_count", "skipped_count"),
            ),
            "industry_attribution": self._attribution_payload(
                industry_stats,
                sort_key="filled_cash_amount",
                fill_rate_keys=("filled_count", "decision_count", "skipped_count"),
            ),
            "status_counts": self._counter_payload(status_counts),
            "execution_mode_counts": self._counter_payload(execution_mode_counts),
            "data_quality_counts": self._counter_payload(data_quality_counts),
            "trade_plan_status_counts": self._counter_payload(trade_plan_status_counts),
            "skip_reason_counts": self._counter_payload(skip_reason_counts),
            "top_skip_reasons": self._top_counter_items(skip_reason_counts),
            "traded_symbols": self._top_counter_items(traded_symbols),
            "diagnostics": diagnostics,
        }

    def get_trade_plan_recovery_summary(
        self,
        *,
        limit: int = 100,
        include_terminal: bool = False,
    ) -> Dict[str, Any]:
        """Summarize trade-plan recovery state without mutating any plan."""

        scan_limit = max(1, min(500, int(limit or 100)))
        statuses = None if include_terminal else [
            "planned",
            "submitted",
            "part_filled",
            "cancel_requested",
            "failed",
            "skipped",
        ]
        payload = self.agent_repo.list_trade_plans(
            limit=scan_limit,
            offset=0,
            statuses=statuses,
        )
        plans = [item for item in list(payload.get("items") or []) if isinstance(item, dict)]
        status_counts: Counter[str] = Counter()
        execution_mode_counts: Counter[str] = Counter()
        side_counts: Counter[str] = Counter()
        recovery_counts: Counter[str] = Counter()
        stale_active_count = 0
        cancellable_count = 0
        items: List[Dict[str, Any]] = []

        for plan in plans:
            status = str(plan.get("status") or "unknown").strip() or "unknown"
            execution_mode = str(plan.get("execution_mode") or "unknown").strip() or "unknown"
            side = str(plan.get("side") or "unknown").strip() or "unknown"
            status_counts[status] += 1
            execution_mode_counts[execution_mode] += 1
            side_counts[side] += 1
            item = self._trade_plan_recovery_item(plan)
            recovery_state = str(item.get("recovery_state") or "unknown")
            recovery_counts[recovery_state] += 1
            if item.get("stale_active"):
                stale_active_count += 1
            if item.get("cancellable"):
                cancellable_count += 1
            items.append(item)

        recovery_priority = {
            "stale_active": 0,
            "retry_due": 1,
            "retry_cooldown": 2,
            "retry_limit_reached": 3,
            "manual_approval_pending": 4,
            "active_waiting": 5,
            "planned": 6,
            "not_retryable": 7,
            "terminal": 8,
        }
        items.sort(
            key=lambda item: (
                recovery_priority.get(str(item.get("recovery_state") or ""), 99),
                -int(item.get("age_seconds") or 0),
                str(item.get("plan_uid") or ""),
            )
        )

        return {
            "generated_at": _utc_now_iso(),
            "limit": int(payload.get("limit") or scan_limit),
            "total": int(payload.get("total") or len(plans)),
            "scanned_count": len(plans),
            "include_terminal": bool(include_terminal),
            "timeout_seconds": TRADE_PLAN_ORDER_TIMEOUT_SECONDS,
            "retry_cooldown_seconds": TRADE_PLAN_RETRY_COOLDOWN_SECONDS,
            "retry_max_attempts": TRADE_PLAN_RETRY_MAX_ATTEMPTS,
            "active_statuses": sorted(ACTIVE_VNPY_TRADE_PLAN_STATUSES),
            "status_counts": self._counter_payload(status_counts),
            "execution_mode_counts": self._counter_payload(execution_mode_counts),
            "side_counts": self._counter_payload(side_counts),
            "recovery_counts": self._counter_payload(recovery_counts),
            "stale_active_count": stale_active_count,
            "cancellable_count": cancellable_count,
            "retry_due_count": int(recovery_counts.get("retry_due", 0)),
            "retry_cooldown_count": int(recovery_counts.get("retry_cooldown", 0)),
            "retry_limit_count": int(recovery_counts.get("retry_limit_reached", 0)),
            "not_retryable_count": int(recovery_counts.get("not_retryable", 0)),
            "items": items,
        }

    @staticmethod
    def vnpy_available() -> bool:
        try:
            return importlib.util.find_spec("vnpy") is not None
        except (ImportError, ValueError):
            return False

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------
    def submit_order(
        self,
        *,
        symbol: str,
        side: str = "buy",
        market: str = "cn",
        quantity: Optional[float] = None,
        cash_amount: Optional[float] = None,
        price: Optional[float] = None,
        note: Optional[str] = None,
        source: str = "manual",
        dedup_key: Optional[str] = None,
        raw: Optional[Dict[str, Any]] = None,
        execution_route: str = "local_paper",
    ) -> Dict[str, Any]:
        settings = self.get_settings()
        if not settings.enabled:
            return self._skipped_order(symbol=symbol, side=side, reason="paper_trading_disabled")

        account = self.ensure_account(settings=settings)
        account_id = int(account["id"])
        symbol_norm = self._normalize_symbol(symbol)
        side_norm = (side or "").strip().lower()
        if side_norm not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")

        fill_price, price_source = self._resolve_order_price(symbol_norm, price)
        if fill_price is None or fill_price <= 0:
            return self._skipped_order(
                symbol=symbol_norm,
                side=side_norm,
                reason="price_unavailable",
                message="Price is unavailable; order was not filled.",
            )

        fill_quantity = self._resolve_order_quantity(
            market=market,
            side=side_norm,
            quantity=quantity,
            cash_amount=cash_amount,
            price=fill_price,
        )
        if fill_quantity <= 0:
            reason = "quantity_too_small"
            message = "Resolved quantity is zero after lot-size rounding."
            cash = _safe_float(cash_amount)
            requested_qty = _safe_float(quantity)
            if (market or "").lower() == "cn" and side_norm == "buy" and requested_qty is None and cash is not None:
                min_cash = fill_price * 100.0
                reason = "cash_below_min_lot"
                message = (
                    "Cash amount is below one A-share lot; "
                    f"need at least {min_cash:.2f} to buy 100 shares at the fill price."
                )
            return self._skipped_order(
                symbol=symbol_norm,
                side=side_norm,
                price=fill_price,
                reason=reason,
                message=message,
            )

        cash_value = round(fill_quantity * fill_price, 6)
        quote_currency = self._currency_for_market(market)
        base_currency = str(account.get("base_currency") or "CNY").strip().upper() or "CNY"
        cash_value_base, fx_stale, fx_source = self.portfolio.convert_amount(
            amount=cash_value,
            from_currency=quote_currency,
            to_currency=base_currency,
            as_of_date=date.today(),
        )
        fx_diagnostics = {
            "base_currency": base_currency,
            "quote_currency": quote_currency,
            "base_cash_amount": round(cash_value_base, 6),
            "quote_cash_amount": cash_value,
            "source": fx_source,
            "stale": bool(fx_stale),
        }
        order_raw = {**(raw or {}), "fx_conversion": fx_diagnostics}
        if side_norm == "buy" and base_currency != quote_currency and fx_stale:
            return self._skipped_order(
                symbol=symbol_norm,
                side=side_norm,
                quantity=fill_quantity,
                price=fill_price,
                cash_amount=cash_value,
                reason="fx_rate_unavailable",
                message=f"A current {quote_currency}/{base_currency} FX rate is required.",
                raw=order_raw,
            )

        if side_norm == "buy":
            available_cash = self._account_cash(account_id)
            if available_cash + 1e-8 < cash_value_base:
                return self._skipped_order(
                    symbol=symbol_norm,
                    side=side_norm,
                    quantity=fill_quantity,
                    price=fill_price,
                    cash_amount=cash_value,
                    reason="cash_insufficient",
                    message="Paper account cash is insufficient in its base currency.",
                    raw=order_raw,
                )

        route = str(execution_route or "local_paper").strip().lower()
        if route == "vnpy_bridge":
            bridge_result = self._submit_vnpy_bridge_order(
                settings=settings,
                account_id=account_id,
                symbol=symbol_norm,
                side=side_norm,
                market=market,
                quantity=fill_quantity,
                price=fill_price,
                cash_amount=cash_value,
                source=source,
                raw=order_raw,
            )
            bridge_result.update(
                {
                    "cash_amount_base": round(cash_value_base, 6),
                    "cash_amount_quote": cash_value,
                    "base_currency": base_currency,
                    "quote_currency": quote_currency,
                }
            )
            return bridge_result
        if route != "local_paper":
            return self._skipped_order(
                symbol=symbol_norm,
                side=side_norm,
                quantity=fill_quantity,
                price=fill_price,
                cash_amount=cash_value,
                reason="unsupported_execution_route",
                message=f"Unsupported execution_route: {execution_route}",
            )

        trade_uid = f"vnpy-paper-{uuid.uuid4().hex}"
        dedup_hash = self._dedup_hash(dedup_key) if dedup_key else None
        trade_note = self._build_trade_note(source=source, note=note, price_source=price_source)
        try:
            created = self.portfolio.record_trade(
                account_id=account_id,
                symbol=symbol_norm,
                trade_date=date.today(),
                side=side_norm,
                quantity=fill_quantity,
                price=fill_price,
                fee=0.0,
                tax=0.0,
                market=market,
                currency=quote_currency,
                trade_uid=trade_uid,
                dedup_hash=dedup_hash,
                note=trade_note,
            )
        except PortfolioConflictError:
            return self._skipped_order(
                symbol=symbol_norm,
                side=side_norm,
                quantity=fill_quantity,
                price=fill_price,
                cash_amount=cash_value,
                reason="duplicate_order",
                message="Duplicate paper order was skipped.",
            )
        except PortfolioOversellError as exc:
            return self._skipped_order(
                symbol=symbol_norm,
                side=side_norm,
                quantity=fill_quantity,
                price=fill_price,
                cash_amount=cash_value,
                reason="oversell",
                message=str(exc),
            )

        self._invalidate_status_snapshot_cache(account_id)
        return {
            "accepted": True,
            "status": "filled",
            "trade_id": int(created["id"]),
            "account_id": account_id,
            "symbol": symbol_norm,
            "side": side_norm,
            "quantity": round(fill_quantity, 8),
            "price": round(fill_price, 8),
            "cash_amount": cash_value,
            "source": "vnpy_local_paper_ledger",
            "message": "Paper order filled.",
            "reason": None,
            "cash_amount_base": round(cash_value_base, 6),
            "cash_amount_quote": cash_value,
            "base_currency": base_currency,
            "quote_currency": quote_currency,
            "raw": order_raw,
        }

    def _submit_vnpy_bridge_order(
        self,
        *,
        settings: VnpyPaperSettings,
        account_id: int,
        symbol: str,
        side: str,
        market: str,
        quantity: float,
        price: float,
        cash_amount: float,
        source: str,
        raw: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        bridge_status = get_vnpy_bridge_status(
            main_engine=self.vnpy_main_engine,
            gateway_name=settings.vnpy_gateway_name,
        )
        if not bridge_status.get("available"):
            return self._skipped_order(
                symbol=symbol,
                side=side,
                quantity=quantity,
                price=price,
                cash_amount=cash_amount,
                reason="vnpy_bridge_unavailable",
                message=f"vn.py bridge unavailable: {bridge_status.get('reason') or 'unknown'}",
                raw={"vnpy_bridge": bridge_status, **(raw or {})},
            )
        try:
            request_payload = build_vnpy_order_request_payload(
                symbol=symbol,
                side=side,
                market=market,
                quantity=quantity,
                price=price,
                source=source,
            )
            bridge = VnpyMainEngineBridge(
                main_engine=self.vnpy_main_engine,
                gateway_name=str(settings.vnpy_gateway_name or ""),
            )
            submitted = bridge.send_order(request_payload)
        except VnpyAdapterError as exc:
            return self._failed_order(
                symbol=symbol,
                side=side,
                quantity=quantity,
                price=price,
                cash_amount=cash_amount,
                reason="vnpy_bridge_submit_failed",
                message=str(exc),
                raw=raw,
            )
        except Exception as exc:  # noqa: BLE001 - gateway submit failures must not abort the Agent run.
            logger.warning("Submit vn.py bridge order failed for %s: %s", symbol, exc)
            return self._failed_order(
                symbol=symbol,
                side=side,
                quantity=quantity,
                price=price,
                cash_amount=cash_amount,
                reason="vnpy_bridge_submit_failed",
                message=str(exc),
                raw={"error_type": type(exc).__name__, **(raw or {})},
            )
        return {
            "accepted": True,
            "status": "submitted",
            "trade_id": None,
            "account_id": account_id,
            "symbol": symbol,
            "side": side,
            "quantity": round(quantity, 8),
            "price": round(price, 8),
            "cash_amount": cash_amount,
            "source": "vnpy_main_engine",
            "message": "vn.py order submitted; waiting for gateway order/trade callbacks.",
            "reason": None,
            "raw": {
                **(raw or {}),
                "vt_orderid": submitted["vt_orderid"],
                "gateway_name": submitted["gateway_name"],
                "order_request_payload": submitted["order_request_payload"],
            },
        }

    def sync_vnpy_trade_callback(
        self,
        *,
        vt_orderid: str,
        vt_tradeid: Optional[str] = None,
        symbol: Optional[str] = None,
        side: Optional[str] = None,
        market: Optional[str] = None,
        quantity: Optional[float] = None,
        price: Optional[float] = None,
        trade_date: Optional[Any] = None,
        fee: float = 0.0,
        tax: float = 0.0,
        currency: Optional[str] = None,
        raw: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Sync a vn.py trade callback into the local audit and paper ledger."""

        order_id = str(vt_orderid or "").strip()
        if not order_id:
            raise ValueError("vt_orderid is required")

        plan = self.agent_repo.find_trade_plan_by_vnpy_order_id(order_id)
        if plan is None:
            return self._failed_order(
                symbol=symbol,
                side=side or "buy",
                quantity=quantity,
                price=price,
                reason="vnpy_trade_plan_not_found",
                message=f"No submitted vn.py trade plan found for {order_id}.",
                raw={"vt_orderid": order_id, **(raw or {})},
            )

        symbol_norm = self._normalize_symbol(symbol or plan.get("symbol") or "")
        side_norm = str(side or plan.get("side") or "buy").strip().lower()
        if side_norm not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")
        market_norm = str(market or plan.get("market") or self.get_settings().auto_market or "cn").strip().lower()
        trade_quantity = _safe_float(quantity) or _safe_float(plan.get("submitted_quantity")) or _safe_float(plan.get("planned_quantity"))
        trade_price = _safe_float(price) or _safe_float(plan.get("submitted_price")) or _safe_float(plan.get("planned_price"))
        if trade_quantity is None or trade_quantity <= 0 or trade_price is None or trade_price <= 0:
            return self._failed_order(
                symbol=symbol_norm,
                side=side_norm,
                quantity=trade_quantity,
                price=trade_price,
                reason="vnpy_trade_payload_invalid",
                message="vn.py trade callback requires positive quantity and price.",
                raw={"vt_orderid": order_id, **(raw or {})},
            )

        settings = self.get_settings()
        account = self.ensure_account(settings=settings)
        account_id = int(account["id"])
        trade_day = self._coerce_trade_date(trade_date)
        trade_ref = str(vt_tradeid or "").strip() or f"{order_id}:{trade_quantity}:{trade_price}:{trade_day.isoformat()}"
        trade_uid = f"vnpy-trade-{trade_ref}"[:128]
        original_order = plan.get("order_result") if isinstance(plan.get("order_result"), dict) else {}
        order_raw = original_order.get("raw") if isinstance(original_order, dict) else {}
        if not isinstance(order_raw, dict):
            order_raw = {}
        fill_sync = order_raw.get("fill_sync") if isinstance(order_raw.get("fill_sync"), dict) else {}
        synced_trades = [
            item for item in list(fill_sync.get("trades") or [])
            if isinstance(item, dict)
        ]
        synced_refs = {
            str(item.get("trade_ref") or item.get("vt_tradeid") or "").strip()
            for item in synced_trades
            if str(item.get("trade_ref") or item.get("vt_tradeid") or "").strip()
        }
        existing_trade_id = _safe_int(plan.get("trade_id"))
        legacy_filled = str(plan.get("status") or "") == "filled" and existing_trade_id is not None and not synced_trades
        if trade_ref in synced_refs or legacy_filled or str(plan.get("status") or "") == "filled":
            return {
                "accepted": True,
                "status": str(plan.get("status") or "filled"),
                "trade_id": existing_trade_id,
                "account_id": self.get_settings().account_id,
                "symbol": plan.get("symbol"),
                "side": plan.get("side"),
                "quantity": _safe_float(plan.get("submitted_quantity")),
                "price": _safe_float(plan.get("submitted_price")),
                "cash_amount": (
                    (_safe_float(plan.get("submitted_quantity")) or 0.0)
                    * (_safe_float(plan.get("submitted_price")) or 0.0)
                ),
                "source": "vnpy_main_engine",
                "message": "vn.py trade callback already synced.",
                "reason": None,
                "raw": {
                    **order_raw,
                    "vt_orderid": order_id,
                    "vt_tradeid": str(vt_tradeid or "").strip() or None,
                    "duplicate_callback": True,
                    **(raw or {}),
                },
            }
        cash_amount = round(float(trade_quantity) * float(trade_price), 6)
        fee_value = _safe_float(fee) or 0.0
        tax_value = _safe_float(tax) or 0.0
        callback_raw = {
            "vt_orderid": order_id,
            "vt_tradeid": str(vt_tradeid or "").strip() or None,
            "callback": raw or {},
        }
        try:
            created = self.portfolio.record_trade(
                account_id=account_id,
                symbol=symbol_norm,
                trade_date=trade_day,
                side=side_norm,
                quantity=float(trade_quantity),
                price=float(trade_price),
                fee=fee_value,
                tax=tax_value,
                market=market_norm,
                currency=currency or self._currency_for_market(market_norm),
                trade_uid=trade_uid,
                dedup_hash=self._dedup_hash(f"vnpy-trade:{trade_ref}"),
                note=f"vn.py callback | vt_orderid={order_id}",
            )
        except PortfolioConflictError:
            return self._failed_order(
                symbol=symbol_norm,
                side=side_norm,
                quantity=trade_quantity,
                price=trade_price,
                cash_amount=cash_amount,
                reason="duplicate_vnpy_trade_callback",
                message="Duplicate vn.py trade callback was ignored.",
                raw=callback_raw,
            )
        except PortfolioOversellError as exc:
            return self._failed_order(
                symbol=symbol_norm,
                side=side_norm,
                quantity=trade_quantity,
                price=trade_price,
                cash_amount=cash_amount,
                reason="vnpy_trade_sync_oversell",
                message=str(exc),
                raw=callback_raw,
            )

        self._invalidate_status_snapshot_cache(account_id)
        previous_quantity = _safe_float(fill_sync.get("cumulative_quantity")) or 0.0
        previous_notional = _safe_float(fill_sync.get("cumulative_notional"))
        if previous_notional is None:
            previous_notional = previous_quantity * (_safe_float(fill_sync.get("average_price")) or 0.0)
        cumulative_quantity = previous_quantity + float(trade_quantity)
        cumulative_notional = previous_notional + cash_amount
        average_price = cumulative_notional / cumulative_quantity
        target_quantity = (
            _safe_float(plan.get("planned_quantity"))
            or _safe_float(
                order_raw.get("order_request_payload", {}).get("volume")
                if isinstance(order_raw.get("order_request_payload"), dict)
                else None
            )
            or _safe_float(plan.get("submitted_quantity"))
            or cumulative_quantity
        )
        next_status = "filled" if cumulative_quantity + 1e-8 >= target_quantity else "part_filled"
        next_reason = None if next_status == "filled" else "vnpy_trade_partially_filled"
        synced_trades.append({
            "trade_ref": trade_ref,
            "vt_tradeid": str(vt_tradeid or "").strip() or None,
            "local_trade_id": int(created["id"]),
            "quantity": round(float(trade_quantity), 8),
            "price": round(float(trade_price), 8),
            "trade_date": trade_day.isoformat(),
        })
        fill_sync_payload = {
            "target_quantity": round(float(target_quantity), 8),
            "cumulative_quantity": round(cumulative_quantity, 8),
            "cumulative_notional": round(cumulative_notional, 6),
            "average_price": round(average_price, 8),
            "remaining_quantity": round(max(0.0, target_quantity - cumulative_quantity), 8),
            "trade_count": len(synced_trades),
            "trades": synced_trades,
            "updated_at": _utc_now_iso(),
        }
        result = {
            "accepted": True,
            "status": next_status,
            "trade_id": int(created["id"]),
            "account_id": account_id,
            "symbol": symbol_norm,
            "side": side_norm,
            "quantity": round(cumulative_quantity, 8),
            "price": round(average_price, 8),
            "cash_amount": round(cumulative_notional, 6),
            "source": "vnpy_main_engine",
            "message": (
                "vn.py trade callback completed the local paper fill."
                if next_status == "filled"
                else "vn.py partial trade callback synced; waiting for remaining fills."
            ),
            "reason": next_reason,
            "raw": {
                **order_raw,
                **callback_raw,
                "fill_sync": fill_sync_payload,
            },
        }
        result = self._preserve_trade_plan_retry_metadata(
            plan=plan,
            order=result,
            status=next_status,
            reason=next_reason,
        )
        self.agent_repo.update_trade_plan_execution(
            plan_uid=str(plan["plan_uid"]),
            status=next_status,
            submitted_quantity=cumulative_quantity,
            submitted_price=average_price,
            trade_id=int(created["id"]),
            skip_reason=next_reason,
            order_result=result,
        )
        decision_id = _safe_int(plan.get("decision_id"))
        if decision_id is not None:
            self.agent_repo.update_decision_execution(
                decision_id=decision_id,
                status=next_status,
                reason=next_reason,
                quantity=cumulative_quantity,
                price=average_price,
                trade_id=int(created["id"]),
                order_result=result,
            )
        run_id = _safe_int(plan.get("run_id"))
        if run_id is not None:
            self.agent_repo.refresh_run_trade_counts(run_id)
        return result

    def sync_vnpy_order_callback(
        self,
        *,
        vt_orderid: str,
        status: str,
        symbol: Optional[str] = None,
        side: Optional[str] = None,
        market: Optional[str] = None,
        volume: Optional[float] = None,
        traded: Optional[float] = None,
        price: Optional[float] = None,
        rejected_reason: Optional[str] = None,
        raw: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Sync a vn.py order status callback into Agent audit records."""

        order_id = str(vt_orderid or "").strip()
        if not order_id:
            raise ValueError("vt_orderid is required")
        order_status = self._normalize_vnpy_status(status)
        if not order_status:
            raise ValueError("status is required")

        plan = self.agent_repo.find_trade_plan_by_vnpy_order_id(order_id)
        state_payload = {
            "vt_orderid": order_id,
            "status": order_status,
            "raw_status": status,
            "symbol": symbol,
            "side": side,
            "market": market,
            "volume": _safe_float(volume),
            "traded": _safe_float(traded),
            "price": _safe_float(price),
            "rejected_reason": rejected_reason,
            "raw": raw or {},
        }
        self._record_vnpy_order_state(order_id, state_payload)

        if plan is None:
            return self._failed_order(
                symbol=symbol,
                side=side or "buy",
                quantity=_safe_float(volume) or _safe_float(traded),
                price=price,
                reason="vnpy_order_plan_not_found",
                message=f"No submitted vn.py trade plan found for {order_id}.",
                raw={"vt_orderid": order_id, "vnpy_order_status": order_status, **(raw or {})},
            )

        existing_trade_id = _safe_int(plan.get("trade_id"))
        if str(plan.get("status") or "") == "filled" and existing_trade_id is not None:
            return {
                "accepted": True,
                "status": "filled",
                "trade_id": existing_trade_id,
                "account_id": self.get_settings().account_id,
                "symbol": plan.get("symbol"),
                "side": plan.get("side"),
                "quantity": _safe_float(plan.get("submitted_quantity")),
                "price": _safe_float(plan.get("submitted_price")),
                "cash_amount": (
                    (_safe_float(plan.get("submitted_quantity")) or 0.0)
                    * (_safe_float(plan.get("submitted_price")) or 0.0)
                ),
                "source": "vnpy_main_engine",
                "message": "vn.py order callback received after the trade was already synced.",
                "reason": None,
                "raw": {"vt_orderid": order_id, "vnpy_order_status": order_status, **(raw or {})},
            }

        symbol_norm = self._normalize_symbol(symbol or plan.get("symbol") or "")
        side_norm = str(side or plan.get("side") or "buy").strip().lower()
        market_norm = str(market or plan.get("market") or self.get_settings().auto_market or "cn").strip().lower()
        order_volume = _safe_float(volume)
        traded_quantity = _safe_float(traded)
        is_part_filled = self._vnpy_order_is_part_filled(order_status)
        failed_reason = self._vnpy_order_failure_reason(order_status)
        previous_order_result = plan.get("order_result") if isinstance(plan.get("order_result"), dict) else {}
        previous_order_raw = (
            previous_order_result.get("raw")
            if isinstance(previous_order_result.get("raw"), dict)
            else {}
        )
        fill_sync = (
            previous_order_raw.get("fill_sync")
            if isinstance(previous_order_raw.get("fill_sync"), dict)
            else {}
        )
        synced_quantity = _safe_float(fill_sync.get("cumulative_quantity"))
        synced_average_price = _safe_float(fill_sync.get("average_price"))
        callback_filled_quantity = max(
            traded_quantity or 0.0,
            synced_quantity or 0.0,
        )
        use_filled_quantity = bool(
            callback_filled_quantity > 0 and (is_part_filled or failed_reason is not None)
        )
        submitted_quantity = (
            callback_filled_quantity
            if use_filled_quantity
            else order_volume
            or _safe_float(plan.get("submitted_quantity"))
            or _safe_float(plan.get("planned_quantity"))
        )
        submitted_price = (
            synced_average_price
            if use_filled_quantity and synced_average_price is not None
            else _safe_float(price)
            or _safe_float(plan.get("submitted_price"))
            or _safe_float(plan.get("planned_price"))
        )
        cash_amount = (
            round(float(submitted_quantity) * float(submitted_price), 6)
            if submitted_quantity is not None and submitted_price is not None
            else None
        )
        order_raw = {
            **previous_order_raw,
            "vt_orderid": order_id,
            "vnpy_order_status": order_status,
            "raw_status": status,
            "order_volume": order_volume,
            "traded": _safe_float(traded),
            "rejected_reason": rejected_reason,
            "callback": raw or {},
        }

        if failed_reason is not None:
            result = self._failed_order(
                symbol=symbol_norm,
                side=side_norm,
                quantity=submitted_quantity,
                price=submitted_price,
                cash_amount=cash_amount,
                reason=failed_reason,
                message=rejected_reason or f"vn.py order {order_status}.",
                raw=order_raw,
            )
            self._update_vnpy_plan_from_order_callback(
                plan=plan,
                status="failed",
                reason=failed_reason,
                quantity=submitted_quantity,
                price=submitted_price,
                result=result,
            )
            return result

        if is_part_filled:
            reason = "vnpy_order_partially_filled_waiting_trade_callback"
            result = {
                "accepted": True,
                "status": "part_filled",
                "trade_id": None,
                "account_id": self.get_settings().account_id,
                "symbol": symbol_norm,
                "side": side_norm,
                "quantity": submitted_quantity,
                "price": submitted_price,
                "cash_amount": cash_amount,
                "source": "vnpy_main_engine",
                "message": "vn.py order is partially filled; waiting for trade callback before local ledger fill.",
                "reason": reason,
                "raw": {
                    **order_raw,
                    "market": market_norm,
                },
            }
            self._update_vnpy_plan_from_order_callback(
                plan=plan,
                status="part_filled",
                reason=reason,
                quantity=submitted_quantity,
                price=submitted_price,
                result=result,
            )
            return result

        reason = "vnpy_order_waiting_trade_callback" if order_status in {"alltraded", "filled"} else None
        result = {
            "accepted": True,
            "status": "submitted",
            "trade_id": None,
            "account_id": self.get_settings().account_id,
            "symbol": symbol_norm,
            "side": side_norm,
            "quantity": submitted_quantity,
            "price": submitted_price,
            "cash_amount": cash_amount,
            "source": "vnpy_main_engine",
            "message": "vn.py order status synced; waiting for trade callback before local fill.",
            "reason": reason,
            "raw": {
                **order_raw,
                "market": market_norm,
            },
        }
        self._update_vnpy_plan_from_order_callback(
            plan=plan,
            status="submitted",
            reason=reason,
            quantity=submitted_quantity,
            price=submitted_price,
            result=result,
        )
        return result

    def sync_vnpy_account_callback(
        self,
        *,
        account_id: str,
        balance: Optional[float] = None,
        available: Optional[float] = None,
        frozen: Optional[float] = None,
        margin: Optional[float] = None,
        close_profit: Optional[float] = None,
        holding_profit: Optional[float] = None,
        currency: Optional[str] = None,
        raw: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Store the latest vn.py account snapshot for diagnostics."""

        account_ref = str(account_id or "").strip()
        if not account_ref:
            raise ValueError("account_id is required")
        snapshot = {
            "account_id": account_ref,
            "balance": _safe_float(balance),
            "available": _safe_float(available),
            "frozen": _safe_float(frozen),
            "margin": _safe_float(margin),
            "close_profit": _safe_float(close_profit),
            "holding_profit": _safe_float(holding_profit),
            "currency": str(currency or "").strip()[:8] or None,
            "raw": raw or {},
            "updated_at": _utc_now_iso(),
        }
        self._record_vnpy_account_state(snapshot)
        return {
            "accepted": True,
            "status": "synced",
            "message": "vn.py account snapshot synced for diagnostics.",
            "reason": None,
            "raw": snapshot,
        }

    def sync_vnpy_positions_callback(
        self,
        *,
        positions: List[Dict[str, Any]],
        raw: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Store the latest vn.py position snapshot for diagnostics."""

        normalized = [item for item in (self._normalize_vnpy_position(item) for item in positions) if item]
        snapshot = {
            "positions": normalized,
            "count": len(normalized),
            "raw": raw or {},
            "updated_at": _utc_now_iso(),
        }
        self._record_vnpy_positions_state(snapshot)
        return {
            "accepted": True,
            "status": "synced",
            "message": "vn.py positions snapshot synced for diagnostics.",
            "reason": None,
            "raw": snapshot,
        }

    def attach_vnpy_event_engine(self, event_engine: Optional[Any] = None) -> VnpyEventSubscriptionBridge:
        """Register this service on a vn.py EventEngine-like object."""

        engine = event_engine or self.vnpy_event_engine
        status = get_vnpy_event_bridge_status(event_engine=engine)
        if not status.get("available"):
            raise VnpyAdapterError(f"vn.py event bridge unavailable: {status.get('reason') or 'unknown'}")
        bridge = VnpyEventSubscriptionBridge(
            event_engine=engine,
            order_handler=self._handle_vnpy_order_event,
            trade_handler=self._handle_vnpy_trade_event,
            account_handler=self._handle_vnpy_account_event,
            position_handler=self._handle_vnpy_position_event,
        )
        bridge.register()
        return bridge

    def run_auto_trade_once(
        self,
        *,
        execution_mode_override: Optional[str] = None,
        ignore_auto_trade_enabled: bool = False,
    ) -> Dict[str, Any]:
        settings = self.get_settings()
        override_mode = str(execution_mode_override or "").strip().lower()
        if override_mode:
            if override_mode not in {"paper", "vnpy_paper", "dry_run", "manual_approval"}:
                raise ValueError("execution_mode must be paper, vnpy_paper, dry_run or manual_approval")
            settings = replace(settings, auto_execution_mode=override_mode)
        if ignore_auto_trade_enabled:
            settings = replace(settings, auto_trade_enabled=True)
        run_uid = f"ss-agent-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
        recent_run_context = self._recent_agent_run_context(settings)
        llm_dynamic_plan = self._generate_llm_dynamic_agent_plan(
            settings=settings,
            run_uid=run_uid,
            execution_mode_override=override_mode or None,
            ignore_auto_trade_enabled=bool(ignore_auto_trade_enabled),
            recent_run_context=recent_run_context,
        )
        settings = self._apply_llm_dynamic_agent_plan(settings, llm_dynamic_plan)
        run_diagnostics = {
            "engine": "vnpy_local_paper_ledger",
            "execution_mode": settings.auto_execution_mode,
            "execution_mode_override": override_mode or None,
            "ignore_auto_trade_enabled": bool(ignore_auto_trade_enabled),
            "agent_plan": self._build_agent_plan(
                settings,
                execution_mode_override=override_mode or None,
                ignore_auto_trade_enabled=bool(ignore_auto_trade_enabled),
                llm_dynamic_plan=llm_dynamic_plan,
                recent_run_context=recent_run_context,
            ),
            "llm_dynamic_plan": llm_dynamic_plan,
        }
        run = self.agent_repo.create_run(
            run_uid=run_uid,
            trigger_source="vnpy_paper_auto",
            strategy=settings.auto_strategy,
            market=settings.auto_market,
            max_results=settings.auto_max_results,
            cash_per_order=settings.auto_cash_per_order,
            min_score=settings.auto_min_score,
            skip_existing_positions=settings.auto_skip_existing_positions,
            settings=asdict(settings),
            diagnostics=run_diagnostics,
        )
        run_id = int(run["id"])
        if not settings.auto_trade_enabled:
            result = {
                "accepted": False,
                "skipped": True,
                "reason": "auto_trade_disabled",
                "agent_run_uid": run_uid,
                "agent_run_id": run_id,
                "strategy": settings.auto_strategy,
                "market": settings.auto_market,
                "candidate_count": 0,
                "planned_count": 0,
                "submitted_count": 0,
                "skipped_count": 0,
                "orders": [],
                "messages": ["Auto paper trading is disabled."],
            }
            self.agent_repo.complete_run(
                run_id=run_id,
                status="skipped",
                candidate_count=0,
                planned_count=0,
                submitted_count=0,
                skipped_count=0,
                message_count=1,
                error="auto_trade_disabled",
                diagnostics={**run_diagnostics, "reason": "auto_trade_disabled"},
            )
            self._record_last_auto_run(result)
            return result

        failure_fuse_reason, failure_fuse_diagnostics = self._failure_fuse_reason(
            settings,
            current_run_id=run_id,
        )
        if failure_fuse_reason:
            result = {
                "accepted": False,
                "skipped": True,
                "reason": failure_fuse_reason,
                "agent_run_uid": run_uid,
                "agent_run_id": run_id,
                "strategy": settings.auto_strategy,
                "market": settings.auto_market,
                "candidate_count": 0,
                "planned_count": 0,
                "submitted_count": 0,
                "skipped_count": 0,
                "orders": [],
                "messages": [failure_fuse_reason],
            }
            self.agent_repo.complete_run(
                run_id=run_id,
                status="skipped",
                candidate_count=0,
                planned_count=0,
                submitted_count=0,
                skipped_count=0,
                message_count=1,
                error=failure_fuse_reason,
                diagnostics={
                    **run_diagnostics,
                    "reason": failure_fuse_reason,
                    "failure_fuse": failure_fuse_diagnostics,
                },
            )
            self._record_auto_trade_alert_event(
                "failure_fuse_open",
                status="triggered",
                reason=failure_fuse_reason,
                observed_value=len(failure_fuse_diagnostics.get("recent_statuses") or []),
                threshold=failure_fuse_diagnostics.get("threshold"),
                diagnostics={
                    "agent_run_uid": run_uid,
                    "agent_run_id": run_id,
                    "strategy": settings.auto_strategy,
                    "market": settings.auto_market,
                    "failure_fuse": failure_fuse_diagnostics,
                },
            )
            self._record_last_auto_run(result)
            return result

        time_gate_reason, time_gate_diagnostics = self._auto_trade_time_gate(settings)
        if time_gate_reason:
            result = {
                "accepted": False,
                "skipped": True,
                "reason": time_gate_reason,
                "agent_run_uid": run_uid,
                "agent_run_id": run_id,
                "strategy": settings.auto_strategy,
                "market": settings.auto_market,
                "candidate_count": 0,
                "planned_count": 0,
                "submitted_count": 0,
                "skipped_count": 0,
                "orders": [],
                "messages": [time_gate_reason],
            }
            self.agent_repo.complete_run(
                run_id=run_id,
                status="skipped",
                candidate_count=0,
                planned_count=0,
                submitted_count=0,
                skipped_count=0,
                message_count=1,
                error=time_gate_reason,
                diagnostics={
                    **run_diagnostics,
                    "reason": time_gate_reason,
                    "market_phase": time_gate_diagnostics,
                },
            )
            self._record_last_auto_run(result)
            return result

        orders: List[Dict[str, Any]] = self._run_auto_sell_checks(settings, run_id=run_id)

        config = get_config()
        try:
            screen = AlphaSiftService(config=config).screen(
                strategy=settings.auto_strategy,
                market=settings.auto_market,
                max_results=settings.auto_max_results,
            )
        except Exception as exc:
            planned_count = sum(1 for item in orders if item.get("status") == "planned")
            submitted_count = sum(1 for item in orders if item.get("accepted"))
            skipped_count = sum(
                1
                for item in orders
                if not item.get("accepted") and item.get("status") != "planned"
            )
            self.agent_repo.complete_run(
                run_id=run_id,
                status="failed",
                candidate_count=0,
                planned_count=planned_count,
                submitted_count=submitted_count,
                skipped_count=skipped_count,
                error=str(exc),
                diagnostics={**run_diagnostics, "stage": "alphasift_screen"},
            )
            self._record_auto_trade_alert_event(
                "alphasift_screen_failed",
                status="failed",
                reason=str(exc) or "alphasift_screen_failed",
                diagnostics={
                    "agent_run_uid": run_uid,
                    "agent_run_id": run_id,
                    "strategy": settings.auto_strategy,
                    "market": settings.auto_market,
                    "stage": "alphasift_screen",
                },
            )
            raise
        candidates = list(screen.get("candidates") or [])
        data_quality = self._screen_data_quality(screen, candidates)
        if data_quality["status"] in {"stale", "unavailable"}:
            reason = f"data_quality_{data_quality['status']}"
            for index, candidate in enumerate(candidates[: settings.auto_max_results], start=1):
                candidate_payload = candidate if isinstance(candidate, dict) else {"candidate": str(candidate)}
                symbol = self._normalize_symbol(
                    candidate_payload.get("code") or candidate_payload.get("symbol") or ""
                )
                order = self._skipped_order(
                    symbol=symbol or None,
                    side="buy",
                    reason=reason,
                    raw=candidate_payload,
                )
                orders.append(order)
                self._record_agent_decision(
                    run_id=run_id,
                    sequence=index,
                    candidate=candidate_payload,
                    symbol=symbol or None,
                    settings=settings,
                    action="skip",
                    order=order,
                    reason=reason,
                    risk_flags=[reason],
                )

            planned_count = sum(1 for item in orders if item.get("status") == "planned")
            submitted_count = sum(1 for item in orders if item.get("accepted"))
            skipped_count = sum(
                1
                for item in orders
                if not item.get("accepted") and item.get("status") != "planned"
            )
            messages = list(screen.get("warnings") or []) + list(screen.get("source_errors") or [])
            result = {
                "accepted": bool(candidates or submitted_count),
                "skipped": not bool(candidates or orders),
                "reason": reason,
                "agent_run_uid": run_uid,
                "agent_run_id": run_id,
                "strategy": settings.auto_strategy,
                "market": settings.auto_market,
                "candidate_count": len(candidates),
                "planned_count": planned_count,
                "submitted_count": submitted_count,
                "skipped_count": skipped_count,
                "orders": orders,
                "messages": messages or [reason],
            }
            self.agent_repo.complete_run(
                run_id=run_id,
                status="completed" if candidates else "skipped",
                candidate_count=len(candidates),
                planned_count=0,
                submitted_count=0,
                skipped_count=skipped_count,
                message_count=len(result["messages"]),
                error=None if candidates else reason,
                diagnostics={
                    **run_diagnostics,
                    "data_quality": data_quality,
                    "warnings": list(screen.get("warnings") or []),
                    "source_errors": list(screen.get("source_errors") or []),
                    "source_health": screen.get("source_health") if isinstance(screen, dict) else None,
                },
            )
            self._record_auto_trade_alert_event(
                reason,
                status="degraded" if data_quality["status"] == "stale" else "failed",
                reason=reason,
                observed_value=len(candidates),
                diagnostics={
                    "agent_run_uid": run_uid,
                    "agent_run_id": run_id,
                    "strategy": settings.auto_strategy,
                    "market": settings.auto_market,
                    "candidate_count": len(candidates),
                    "data_quality": data_quality,
                    "warnings": list(screen.get("warnings") or []),
                    "source_errors": list(screen.get("source_errors") or []),
                    "source_health": screen.get("source_health") if isinstance(screen, dict) else None,
                },
            )
            self._record_last_auto_run(result)
            return result

        currency_budget = self._auto_order_currency_budget(settings)
        run_diagnostics["currency_budget"] = currency_budget
        currency_budget_reason = None if currency_budget.get("available") else "fx_rate_unavailable"
        exposure_state = self._position_exposure_state(settings)
        held_symbols = set(exposure_state.get("held_symbols") or set())
        blacklisted_symbols = set(settings.auto_symbol_blacklist or [])
        daily_usage = self._daily_auto_trade_usage(settings)
        daily_order_count = int(daily_usage["order_count"])
        daily_cash_used = float(daily_usage["cash_amount"])
        daily_usage_fx_unavailable = bool(daily_usage.get("fx_unavailable"))
        account_risk_reason, account_risk_diagnostics = self._account_pre_trade_risk(settings)
        run_diagnostics["account_risk"] = account_risk_diagnostics
        market_risk_reason = self._market_light_pre_trade_risk_reason(settings)
        if account_risk_reason:
            self._record_auto_trade_alert_event(
                account_risk_reason,
                status="failed" if account_risk_reason == "account_risk_unavailable" else "triggered",
                reason=account_risk_reason,
                observed_value=account_risk_diagnostics.get("observed_value"),
                threshold=account_risk_diagnostics.get("threshold"),
                diagnostics={
                    "agent_run_uid": run_uid,
                    "agent_run_id": run_id,
                    "strategy": settings.auto_strategy,
                    "market": settings.auto_market,
                    "candidate_count": len(candidates),
                    "account_risk": account_risk_diagnostics,
                },
            )

        for index, candidate in enumerate(candidates[: settings.auto_max_results], start=1):
            if not isinstance(candidate, dict):
                order = self._skipped_order(
                    symbol=None,
                    side="buy",
                    reason="invalid_candidate",
                    raw={"candidate": str(candidate)},
                )
                orders.append(order)
                self._record_agent_decision(
                    run_id=run_id,
                    sequence=index,
                    candidate={"candidate": str(candidate)},
                    symbol=None,
                    settings=settings,
                    action="skip",
                    order=order,
                    reason="invalid_candidate",
                    risk_flags=["invalid_candidate"],
                )
                continue
            symbol = self._normalize_symbol(candidate.get("code") or candidate.get("symbol") or "")
            if not symbol:
                order = self._skipped_order(
                    symbol=None,
                    side="buy",
                    reason="missing_symbol",
                    raw=candidate,
                )
                orders.append(order)
                self._record_agent_decision(
                    run_id=run_id,
                    sequence=index,
                    candidate=candidate,
                    symbol=None,
                    settings=settings,
                    action="skip",
                    order=order,
                    reason="missing_symbol",
                    risk_flags=["missing_symbol"],
                )
                continue
            if symbol in blacklisted_symbols:
                order = self._skipped_order(symbol=symbol, side="buy", reason="symbol_blacklisted", raw=candidate)
                orders.append(order)
                self._record_agent_decision(
                    run_id=run_id,
                    sequence=index,
                    candidate=candidate,
                    symbol=symbol,
                    settings=settings,
                    action="skip",
                    order=order,
                    reason="symbol_blacklisted",
                    risk_flags=["symbol_blacklisted"],
                )
                continue
            risk_reason = self._candidate_pre_trade_risk_reason(candidate, settings)
            if risk_reason:
                order = self._skipped_order(symbol=symbol, side="buy", reason=risk_reason, raw=candidate)
                orders.append(order)
                self._record_agent_decision(
                    run_id=run_id,
                    sequence=index,
                    candidate=candidate,
                    symbol=symbol,
                    settings=settings,
                    action="skip",
                    order=order,
                    reason=risk_reason,
                    risk_flags=[risk_reason],
                )
                continue
            if currency_budget_reason:
                order = self._skipped_order(
                    symbol=symbol,
                    side="buy",
                    reason=currency_budget_reason,
                    message="A current FX rate is required before cross-currency auto trading.",
                    raw={**candidate, "fx_conversion": currency_budget},
                )
                self._annotate_order_currency_budget(order, currency_budget)
                orders.append(order)
                self._record_agent_decision(
                    run_id=run_id,
                    sequence=index,
                    candidate=candidate,
                    symbol=symbol,
                    settings=settings,
                    action="skip",
                    order=order,
                    reason=currency_budget_reason,
                    risk_flags=[currency_budget_reason],
                )
                continue
            if daily_usage_fx_unavailable:
                order = self._skipped_order(
                    symbol=symbol,
                    side="buy",
                    reason="fx_rate_unavailable",
                    message="Daily cross-currency usage cannot be valued in the account currency.",
                    raw={**candidate, "daily_usage": daily_usage},
                )
                orders.append(order)
                self._record_agent_decision(
                    run_id=run_id,
                    sequence=index,
                    candidate=candidate,
                    symbol=symbol,
                    settings=settings,
                    action="skip",
                    order=order,
                    reason="fx_rate_unavailable",
                    risk_flags=["fx_rate_unavailable"],
                )
                continue
            if account_risk_reason:
                order = self._skipped_order(symbol=symbol, side="buy", reason=account_risk_reason, raw=candidate)
                orders.append(order)
                self._record_agent_decision(
                    run_id=run_id,
                    sequence=index,
                    candidate=candidate,
                    symbol=symbol,
                    settings=settings,
                    action="skip",
                    order=order,
                    reason=account_risk_reason,
                    risk_flags=[account_risk_reason],
                )
                continue
            if market_risk_reason:
                order = self._skipped_order(symbol=symbol, side="buy", reason=market_risk_reason, raw=candidate)
                orders.append(order)
                self._record_agent_decision(
                    run_id=run_id,
                    sequence=index,
                    candidate=candidate,
                    symbol=symbol,
                    settings=settings,
                    action="skip",
                    order=order,
                    reason=market_risk_reason,
                    risk_flags=[market_risk_reason],
                )
                continue
            score = self._candidate_score(candidate)
            if settings.auto_min_score is not None and (score is None or score < settings.auto_min_score):
                order = self._skipped_order(symbol=symbol, side="buy", reason="score_below_threshold", raw=candidate)
                orders.append(order)
                self._record_agent_decision(
                    run_id=run_id,
                    sequence=index,
                    candidate=candidate,
                    symbol=symbol,
                    settings=settings,
                    action="skip",
                    order=order,
                    reason="score_below_threshold",
                    risk_flags=["score_below_threshold"],
                )
                continue
            has_position_target = symbol in dict(settings.auto_target_position_weights or {})
            if settings.auto_skip_existing_positions and symbol in held_symbols and not has_position_target:
                order = self._skipped_order(symbol=symbol, side="buy", reason="position_exists", raw=candidate)
                orders.append(order)
                self._record_agent_decision(
                    run_id=run_id,
                    sequence=index,
                    candidate=candidate,
                    symbol=symbol,
                    settings=settings,
                    action="skip",
                    order=order,
                    reason="position_exists",
                    risk_flags=["position_exists"],
                )
                continue
            active_plan = self._active_vnpy_trade_plan(symbol=symbol, side="buy", settings=settings)
            if active_plan is not None:
                order = self._skipped_order(
                    symbol=symbol,
                    side="buy",
                    reason="active_vnpy_order_exists",
                    raw={**candidate, "active_trade_plan": self._trade_plan_reference(active_plan)},
                )
                orders.append(order)
                self._record_agent_decision(
                    run_id=run_id,
                    sequence=index,
                    candidate=candidate,
                    symbol=symbol,
                    settings=settings,
                    action="skip",
                    order=order,
                    reason="active_vnpy_order_exists",
                    risk_flags=["active_vnpy_order_exists"],
                )
                continue
            if (
                settings.auto_max_positions > 0
                and symbol not in held_symbols
                and len(held_symbols) >= settings.auto_max_positions
            ):
                order = self._skipped_order(symbol=symbol, side="buy", reason="max_positions_reached", raw=candidate)
                orders.append(order)
                self._record_agent_decision(
                    run_id=run_id,
                    sequence=index,
                    candidate=candidate,
                    symbol=symbol,
                    settings=settings,
                    action="skip",
                    order=order,
                    reason="max_positions_reached",
                    risk_flags=["max_positions_reached"],
                )
                continue
            target_budget, target_sizing, target_budget_reason = self._target_weight_buy_budget(
                settings=settings,
                exposure_state=exposure_state,
                symbol=symbol,
                candidate=candidate,
                configured_base_amount=float(settings.auto_cash_per_order),
            )
            if target_budget_reason:
                order = self._skipped_order(
                    symbol=symbol,
                    side="buy",
                    reason=target_budget_reason,
                    raw={**candidate, "target_weight_sizing": target_sizing},
                )
                orders.append(order)
                self._record_agent_decision(
                    run_id=run_id,
                    sequence=index,
                    candidate=candidate,
                    symbol=symbol,
                    settings=settings,
                    action="skip",
                    order=order,
                    reason=target_budget_reason,
                    risk_flags=[target_budget_reason],
                )
                continue
            candidate_currency_budget = self._scaled_currency_budget(
                currency_budget,
                base_cash_amount=target_budget,
            )
            candidate_price = _safe_float(candidate.get("price"))
            target_budget, candidate_currency_budget, target_lot_reason = (
                self._executable_target_weight_budget(
                    settings=settings,
                    budget=candidate_currency_budget,
                    sizing=target_sizing,
                    price=candidate_price,
                )
            )
            if target_lot_reason:
                order = self._skipped_order(
                    symbol=symbol,
                    side="buy",
                    price=candidate_price,
                    reason=target_lot_reason,
                    raw={
                        **candidate,
                        "fx_conversion": candidate_currency_budget,
                        "target_weight_sizing": target_sizing,
                    },
                )
                self._annotate_order_currency_budget(order, candidate_currency_budget)
                orders.append(order)
                self._record_agent_decision(
                    run_id=run_id,
                    sequence=index,
                    candidate=candidate,
                    symbol=symbol,
                    settings=settings,
                    action="skip",
                    order=order,
                    reason=target_lot_reason,
                    risk_flags=[target_lot_reason],
                )
                continue
            exposure_limit_reason = self._position_exposure_limit_reason(
                settings=settings,
                exposure_state=exposure_state,
                symbol=symbol,
                candidate=candidate,
                next_cash_amount=target_budget,
            )
            if exposure_limit_reason:
                order = self._skipped_order(
                    symbol=symbol,
                    side="buy",
                    reason=exposure_limit_reason,
                    raw={**candidate, "target_weight_sizing": target_sizing},
                )
                orders.append(order)
                self._record_agent_decision(
                    run_id=run_id,
                    sequence=index,
                    candidate=candidate,
                    symbol=symbol,
                    settings=settings,
                    action="skip",
                    order=order,
                    reason=exposure_limit_reason,
                    risk_flags=[exposure_limit_reason],
                )
                continue
            daily_limit_reason = self._daily_limit_reason(
                settings=settings,
                order_count=daily_order_count,
                cash_used=daily_cash_used,
                next_cash_amount=target_budget,
            )
            if daily_limit_reason:
                order = self._skipped_order(
                    symbol=symbol,
                    side="buy",
                    reason=daily_limit_reason,
                    raw={**candidate, "target_weight_sizing": target_sizing},
                )
                orders.append(order)
                self._record_agent_decision(
                    run_id=run_id,
                    sequence=index,
                    candidate=candidate,
                    symbol=symbol,
                    settings=settings,
                    action="skip",
                    order=order,
                    reason=daily_limit_reason,
                    risk_flags=[daily_limit_reason],
                )
                continue
            llm_review = self._candidate_llm_pre_trade_review(
                candidate=candidate,
                settings=settings,
                symbol=symbol,
                run_uid=run_uid,
                sequence=index,
            )
            if llm_review is not None and self._llm_review_blocks_trade(llm_review):
                llm_reason = self._llm_review_block_reason(llm_review)
                order = self._skipped_order(symbol=symbol, side="buy", reason=llm_reason, raw=candidate)
                order["llm_review"] = llm_review
                orders.append(order)
                self._record_agent_decision(
                    run_id=run_id,
                    sequence=index,
                    candidate=candidate,
                    symbol=symbol,
                    settings=settings,
                    action="skip",
                    order=order,
                    reason=llm_reason,
                    risk_flags=[llm_reason],
                )
                continue
            dedup_key = f"{date.today().isoformat()}:{settings.auto_strategy}:{settings.auto_market}:{symbol}"
            if settings.auto_execution_mode in {"dry_run", "manual_approval"}:
                plan_reason = "pending_approval" if settings.auto_execution_mode == "manual_approval" else "dry_run"
                order = self._planned_order(
                    symbol=symbol,
                    market=settings.auto_market,
                    cash_amount=float(candidate_currency_budget["quote_cash_amount"]),
                    price=candidate_price,
                    reason=plan_reason,
                    raw={
                        **candidate,
                        "fx_conversion": candidate_currency_budget,
                        "target_weight_sizing": target_sizing,
                    },
                )
            else:
                execution_route = "vnpy_bridge" if settings.auto_execution_mode == "vnpy_paper" else "local_paper"
                order = self.submit_order(
                    symbol=symbol,
                    side="buy",
                    market=settings.auto_market,
                    cash_amount=float(candidate_currency_budget["quote_cash_amount"]),
                    price=candidate_price,
                    source="alphasift_auto",
                    dedup_key=dedup_key,
                    note=f"AlphaSift {settings.auto_strategy}",
                    raw={
                        **candidate,
                        "fx_conversion": candidate_currency_budget,
                        "target_weight_sizing": target_sizing,
                    },
                    execution_route=execution_route,
                )
            self._annotate_order_currency_budget(order, candidate_currency_budget)
            if llm_review is not None:
                order["llm_review"] = llm_review
            orders.append(order)
            order_reason = order.get("reason")
            self._record_agent_decision(
                run_id=run_id,
                sequence=index,
                candidate=candidate,
                symbol=symbol,
                settings=settings,
                action="buy",
                order=order,
                reason=str(order_reason) if order_reason else None,
                risk_flags=[str(order_reason)] if order_reason else [],
            )
            if order.get("accepted") or order.get("status") == "planned":
                held_symbols.add(symbol)
                self._apply_planned_exposure(exposure_state, symbol, order, settings=settings, candidate=candidate)
            if order.get("accepted") or order.get("status") == "planned":
                daily_order_count += 1
                daily_cash_used += self._order_base_cash_amount(order, settings=settings)

        planned_count = sum(1 for item in orders if item.get("status") == "planned")
        submitted_count = sum(1 for item in orders if item.get("accepted"))
        skipped_count = sum(
            1
            for item in orders
            if not item.get("accepted") and item.get("status") != "planned"
        )
        messages = list(screen.get("warnings") or []) + list(screen.get("source_errors") or [])
        result = {
            "accepted": True,
            "skipped": False,
            "reason": None,
            "agent_run_uid": run_uid,
            "agent_run_id": run_id,
            "strategy": settings.auto_strategy,
            "market": settings.auto_market,
            "candidate_count": len(candidates),
            "planned_count": planned_count,
            "submitted_count": submitted_count,
            "skipped_count": skipped_count,
            "orders": orders,
            "messages": messages,
        }
        self.agent_repo.complete_run(
            run_id=run_id,
            status="completed",
            candidate_count=len(candidates),
            planned_count=planned_count,
            submitted_count=submitted_count,
            skipped_count=skipped_count,
            message_count=len(messages),
            diagnostics={
                **run_diagnostics,
                "data_quality": data_quality,
                "warnings": list(screen.get("warnings") or []),
                "source_errors": list(screen.get("source_errors") or []),
                "source_health": screen.get("source_health") if isinstance(screen, dict) else None,
            },
        )
        self._record_last_auto_run(result)
        return result

    def generate_agent_run_llm_recap(
        self,
        run_uid: str,
        *,
        max_output_tokens: int = 800,
    ) -> Dict[str, Any]:
        run_id = str(run_uid or "").strip()
        if not run_id:
            raise ValueError("run_uid is required")
        detail = self.agent_repo.get_run_detail(run_id)
        if detail is None:
            raise ValueError("agent_run_not_found")

        prompt_payload = self._agent_run_recap_prompt_payload(detail)
        system_prompt, prompt = self._agent_run_recap_prompts(prompt_payload)
        generated_at = _utc_now_iso()
        recap: Dict[str, Any] = {
            "schema_version": 1,
            "status": "running",
            "generated_at": generated_at,
            "prompt_version": "vnpy_paper_agent_recap_v1",
            "source": "manual_agent_console",
        }

        try:
            from src.analyzer import GeminiAnalyzer

            analyzer = GeminiAnalyzer()
            if not analyzer.is_available():
                raise RuntimeError("llm_unavailable")
            text, model, usage = analyzer._call_litellm(
                prompt,
                {
                    "temperature": 0.2,
                    "max_output_tokens": max(200, min(2000, int(max_output_tokens or 800))),
                },
                system_prompt=system_prompt,
                audit_context={
                    "call_type": "vnpy_paper_agent_recap",
                    "run_uid": run_id,
                },
            )
            content = str(text or "").strip()
            if not content:
                raise RuntimeError("llm_empty_recap")
            recap.update(
                {
                    "status": "completed",
                    "completed_at": _utc_now_iso(),
                    "model": model,
                    "usage": usage or {},
                    "content": content,
                }
            )
            accepted = True
            reason = None
        except Exception as exc:  # noqa: BLE001 - recap must never affect trading.
            reason = str(exc or "llm_recap_failed")[:500]
            recap.update(
                {
                    "status": "failed",
                    "completed_at": _utc_now_iso(),
                    "error": reason,
                }
            )
            accepted = False

        self.agent_repo.merge_run_diagnostics(run_id, {"llm_recap": recap})
        updated_detail = self.agent_repo.get_run_detail(run_id)
        return {
            "accepted": accepted,
            "status": recap["status"],
            "reason": reason,
            "run_uid": run_id,
            "llm_recap": recap,
            "run_detail": updated_detail,
        }

    def approve_trade_plan(self, plan_uid: str) -> Dict[str, Any]:
        """Submit a previously generated manual-approval plan to the paper ledger."""

        return self._submit_trade_plan(
            plan_uid,
            allowed_statuses={"planned"},
            invalid_status_error="trade_plan_already_processed",
            allowed_execution_modes={"manual_approval"},
            invalid_execution_mode_error="trade_plan_not_manual_approval",
            dedup_prefix="manual-approval",
            note_prefix="approved plan",
        )

    def retry_trade_plan(self, plan_uid: str) -> Dict[str, Any]:
        """Retry a failed/skipped paper plan after the operator fixes state."""

        return self._submit_trade_plan(
            plan_uid,
            allowed_statuses={"failed", "skipped"},
            invalid_status_error="trade_plan_not_retryable",
            allowed_execution_modes=RETRYABLE_TRADE_PLAN_EXECUTION_MODES,
            invalid_execution_mode_error="trade_plan_not_retryable",
            dedup_prefix=f"trade-plan-retry:{uuid.uuid4().hex[:8]}",
            note_prefix="retried plan",
            is_retry=True,
        )

    def cancel_trade_plan(self, plan_uid: str) -> Dict[str, Any]:
        """Request cancellation for a submitted vn.py paper trade plan."""

        plan_key = str(plan_uid or "").strip()
        if not plan_key:
            raise ValueError("plan_uid is required")

        with self._lock:
            plan = self.agent_repo.get_trade_plan(plan_key)
            if plan is None:
                raise ValueError("trade_plan_not_found")
            if str(plan.get("execution_mode") or "") != "vnpy_paper":
                raise ValueError("trade_plan_cancel_not_supported")
            current_status = str(plan.get("status") or "").strip().lower()
            if current_status not in {"submitted", "part_filled"}:
                raise ValueError("trade_plan_not_cancelable")

            order_result = plan.get("order_result") if isinstance(plan.get("order_result"), dict) else {}
            raw = order_result.get("raw") if isinstance(order_result, dict) else {}
            if not isinstance(raw, dict):
                raw = {}
            order_request_payload = raw.get("order_request_payload") if isinstance(raw.get("order_request_payload"), dict) else {}
            vt_orderid = str(raw.get("vt_orderid") or raw.get("vtOrderid") or "").strip()
            if not vt_orderid:
                raise ValueError("vnpy_order_id_missing")

            settings = self.get_settings()
            bridge_status = get_vnpy_bridge_status(
                main_engine=self.vnpy_main_engine,
                gateway_name=settings.vnpy_gateway_name,
            )
            if not bridge_status.get("available") or not bridge_status.get("cancel_order_supported"):
                reason = (
                    "cancel_order_unavailable"
                    if bridge_status.get("available")
                    else str(bridge_status.get("reason") or "vnpy_bridge_unavailable")
                )
                return self._failed_order(
                    symbol=plan.get("symbol"),
                    side=plan.get("side") or "buy",
                    quantity=_safe_float(plan.get("submitted_quantity")) or _safe_float(plan.get("planned_quantity")),
                    price=_safe_float(plan.get("submitted_price")) or _safe_float(plan.get("planned_price")),
                    cash_amount=_safe_float(plan.get("planned_cash_amount")),
                    reason="vnpy_cancel_unavailable",
                    message=f"vn.py cancel unavailable: {reason}",
                    raw={"plan_uid": plan_key, "vnpy_bridge": bridge_status, "vt_orderid": vt_orderid},
                )

            try:
                cancel_payload = build_vnpy_cancel_request_payload(
                    vt_orderid=vt_orderid,
                    symbol=str(order_request_payload.get("symbol") or plan.get("symbol") or ""),
                    market=str(plan.get("market") or settings.auto_market or "cn"),
                    order_id=raw.get("orderid") or raw.get("order_id"),
                    exchange=order_request_payload.get("exchange") or raw.get("exchange"),
                )
                submitted = VnpyMainEngineBridge(
                    main_engine=self.vnpy_main_engine,
                    gateway_name=str(settings.vnpy_gateway_name or ""),
                ).cancel_order(cancel_payload)
            except VnpyAdapterError as exc:
                return self._failed_order(
                    symbol=plan.get("symbol"),
                    side=plan.get("side") or "buy",
                    quantity=_safe_float(plan.get("submitted_quantity")) or _safe_float(plan.get("planned_quantity")),
                    price=_safe_float(plan.get("submitted_price")) or _safe_float(plan.get("planned_price")),
                    cash_amount=_safe_float(plan.get("planned_cash_amount")),
                    reason="vnpy_cancel_failed",
                    message=str(exc),
                    raw={"plan_uid": plan_key, "vt_orderid": vt_orderid},
                )
            except Exception as exc:  # noqa: BLE001 - gateway cancellation failures should remain visible.
                logger.warning("Cancel vn.py bridge order failed for %s: %s", vt_orderid, exc)
                return self._failed_order(
                    symbol=plan.get("symbol"),
                    side=plan.get("side") or "buy",
                    quantity=_safe_float(plan.get("submitted_quantity")) or _safe_float(plan.get("planned_quantity")),
                    price=_safe_float(plan.get("submitted_price")) or _safe_float(plan.get("planned_price")),
                    cash_amount=_safe_float(plan.get("planned_cash_amount")),
                    reason="vnpy_cancel_failed",
                    message=str(exc),
                    raw={"plan_uid": plan_key, "vt_orderid": vt_orderid, "error_type": type(exc).__name__},
                )

            cancel_requested_at = _utc_now_iso()
            quantity = _safe_float(plan.get("submitted_quantity")) or _safe_float(plan.get("planned_quantity"))
            price = _safe_float(plan.get("submitted_price")) or _safe_float(plan.get("planned_price"))
            cash_amount = round(float(quantity) * float(price), 6) if quantity is not None and price is not None else None
            result = {
                "accepted": True,
                "status": "cancel_requested",
                "trade_id": _safe_int(plan.get("trade_id")),
                "account_id": settings.account_id,
                "symbol": plan.get("symbol"),
                "side": plan.get("side") or "buy",
                "quantity": quantity,
                "price": price,
                "cash_amount": cash_amount,
                "source": "vnpy_main_engine",
                "message": "vn.py cancel request submitted; waiting for order callback confirmation.",
                "reason": "vnpy_order_cancel_requested",
                "raw": {
                    **raw,
                    "vt_orderid": vt_orderid,
                    "gateway_name": submitted.get("gateway_name") or settings.vnpy_gateway_name,
                    "cancel": {
                        "requested_at": cancel_requested_at,
                        "cancel_request_payload": submitted.get("cancel_request_payload") or cancel_payload,
                        "raw_result": self._jsonable_event_value(submitted.get("raw_result")),
                    },
                },
            }
            result = self._preserve_trade_plan_retry_metadata(
                plan=plan,
                order=result,
                status="cancel_requested",
                reason="vnpy_order_cancel_requested",
            )
            self.agent_repo.update_trade_plan_execution(
                plan_uid=plan_key,
                status="cancel_requested",
                submitted_quantity=_safe_float(plan.get("submitted_quantity")),
                submitted_price=_safe_float(plan.get("submitted_price")),
                trade_id=_safe_int(plan.get("trade_id")),
                skip_reason=None,
                order_result=result,
            )
            decision_id = _safe_int(plan.get("decision_id"))
            if decision_id is not None:
                self.agent_repo.update_decision_execution(
                    decision_id=decision_id,
                    status="cancel_requested",
                    reason="vnpy_order_cancel_requested",
                    quantity=quantity,
                    price=price,
                    trade_id=_safe_int(plan.get("trade_id")),
                    order_result=result,
                )
            run_id = _safe_int(plan.get("run_id"))
            if run_id is not None:
                self.agent_repo.refresh_run_trade_counts(run_id)
            return result

    def retry_due_trade_plans(
        self,
        *,
        max_plans: int = TRADE_PLAN_AUTO_RETRY_MAX_PLANS,
        scan_limit: int = TRADE_PLAN_AUTO_RETRY_SCAN_LIMIT,
    ) -> Dict[str, Any]:
        """Retry due failed/skipped automatic paper plans under bounded controls."""

        settings = self.get_settings()
        if not settings.enabled:
            return {
                "accepted": True,
                "skipped": True,
                "reason": "auto_trade_disabled",
                "scanned_count": 0,
                "attempted_count": 0,
                "submitted_count": 0,
                "skipped_count": 0,
                "failed_count": 0,
                "orders": [],
                "messages": ["Automatic paper trading is disabled; recovery scan skipped."],
            }

        max_attempts = max(1, min(20, int(max_plans or TRADE_PLAN_AUTO_RETRY_MAX_PLANS)))
        expiration = self.expire_stale_vnpy_trade_plans(
            max_plans=max_attempts,
            scan_limit=scan_limit,
        )
        if not settings.auto_trade_enabled:
            return {
                "accepted": True,
                "skipped": False,
                "reason": "auto_trade_disabled",
                "expired_count": int(expiration.get("expired_count") or 0),
                "reconciled_count": int(expiration.get("reconciled_count") or 0),
                "protected_count": int(expiration.get("protected_count") or 0),
                "reconciliation_failed_count": int(
                    expiration.get("reconciliation_failed_count") or 0
                ),
                "scanned_count": int(expiration.get("scanned_count") or 0),
                "attempted_count": 0,
                "submitted_count": 0,
                "skipped_count": 0,
                "failed_count": int(expiration.get("failed_count") or 0),
                "orders": [],
                "messages": [
                    *list(expiration.get("messages") or []),
                    "Automatic paper trading is paused; active order recovery remains enabled.",
                ],
            }
        candidates = self._auto_retry_candidate_trade_plans(scan_limit=scan_limit)
        result: Dict[str, Any] = {
            "accepted": True,
            "skipped": False,
            "reason": None,
            "expired_count": int(expiration.get("expired_count") or 0),
            "reconciled_count": int(expiration.get("reconciled_count") or 0),
            "protected_count": int(expiration.get("protected_count") or 0),
            "reconciliation_failed_count": int(expiration.get("reconciliation_failed_count") or 0),
            "scanned_count": len(candidates),
            "attempted_count": 0,
            "submitted_count": 0,
            "skipped_count": 0,
            "failed_count": 0,
            "orders": [],
            "messages": list(expiration.get("messages") or []),
        }
        for plan in candidates:
            if int(result["attempted_count"]) >= max_attempts:
                result["messages"].append("auto_retry_attempt_limit_reached")
                break
            due, due_reason = self._auto_retry_due(plan)
            if not due:
                result["skipped_count"] = int(result["skipped_count"]) + 1
                if due_reason:
                    result["messages"].append(due_reason)
                continue

            result["attempted_count"] = int(result["attempted_count"]) + 1
            try:
                order = self.retry_trade_plan(str(plan.get("plan_uid") or ""))
            except ValueError as exc:
                result["skipped_count"] = int(result["skipped_count"]) + 1
                result["messages"].append(str(exc))
                self._record_auto_trade_alert_event(
                    "auto_retry_skipped",
                    status="skipped",
                    reason=str(exc),
                    diagnostics={
                        "plan_uid": plan.get("plan_uid"),
                        "run_id": plan.get("run_id"),
                        "symbol": plan.get("symbol"),
                        "execution_mode": plan.get("execution_mode"),
                    },
                )
                continue
            except Exception as exc:  # noqa: BLE001 - retry scans must not stop scheduler threads.
                logger.warning("Auto retry trade plan failed for %s: %s", plan.get("plan_uid"), exc)
                result["failed_count"] = int(result["failed_count"]) + 1
                result["messages"].append(f"auto_retry_failed:{plan.get('plan_uid')}")
                self._record_auto_trade_alert_event(
                    "auto_retry_failed",
                    status="failed",
                    reason=str(exc) or "auto_retry_failed",
                    diagnostics={
                        "plan_uid": plan.get("plan_uid"),
                        "run_id": plan.get("run_id"),
                        "symbol": plan.get("symbol"),
                        "execution_mode": plan.get("execution_mode"),
                    },
                )
                continue

            result["orders"].append(order)
            order_status = self._trade_plan_status(order)
            if order.get("accepted") or order_status in {"submitted", "part_filled", "filled"}:
                result["submitted_count"] = int(result["submitted_count"]) + 1
            else:
                result["skipped_count"] = int(result["skipped_count"]) + 1
                self._record_auto_trade_alert_event(
                    "auto_retry_not_accepted",
                    status="failed" if order_status == "failed" else "skipped",
                    reason=str(order.get("reason") or order_status or "auto_retry_not_accepted"),
                    diagnostics={
                        "plan_uid": plan.get("plan_uid"),
                        "run_id": plan.get("run_id"),
                        "symbol": plan.get("symbol"),
                        "execution_mode": plan.get("execution_mode"),
                        "order_status": order_status,
                        "order_reason": order.get("reason"),
                    },
                )

        if not result["orders"] and not result["messages"]:
            result["messages"].append("no_due_trade_plans")
        return result

    def expire_stale_vnpy_trade_plans(
        self,
        *,
        max_plans: int = TRADE_PLAN_AUTO_RETRY_MAX_PLANS,
        scan_limit: int = TRADE_PLAN_AUTO_RETRY_SCAN_LIMIT,
        timeout_seconds: int = TRADE_PLAN_ORDER_TIMEOUT_SECONDS,
        reconciliation_grace_seconds: int = TRADE_PLAN_RECONCILIATION_GRACE_SECONDS,
    ) -> Dict[str, Any]:
        """Reconcile active vn.py plans and expire only unsupported stale orders."""

        timeout = max(60, int(timeout_seconds or TRADE_PLAN_ORDER_TIMEOUT_SECONDS))
        grace = max(0, min(timeout, int(reconciliation_grace_seconds or 0)))
        candidates = self._active_vnpy_trade_plans(
            scan_limit=scan_limit,
            minimum_age_seconds=grace,
        )
        limit = max(1, min(20, int(max_plans or TRADE_PLAN_AUTO_RETRY_MAX_PLANS)))
        result: Dict[str, Any] = {
            "accepted": True,
            "scanned_count": len(candidates),
            "expired_count": 0,
            "reconciled_count": 0,
            "protected_count": 0,
            "reconciliation_failed_count": 0,
            "failed_count": 0,
            "messages": [],
        }
        for plan in candidates[:limit]:
            try:
                reconciliation = self._reconcile_vnpy_trade_plan(plan)
            except Exception as exc:  # noqa: BLE001 - gateway query failures must fail closed.
                logger.warning("Reconcile stale vn.py trade plan failed for %s: %s", plan.get("plan_uid"), exc)
                result["reconciliation_failed_count"] = int(result["reconciliation_failed_count"]) + 1
                result["protected_count"] = int(result["protected_count"]) + 1
                result["failed_count"] = int(result["failed_count"]) + 1
                result["messages"].append(f"vnpy_order_reconciliation_failed:{plan.get('plan_uid')}")
                self._record_auto_trade_alert_event(
                    "vnpy_order_reconciliation_failed",
                    status="failed",
                    reason=str(exc) or "vnpy_order_reconciliation_failed",
                    diagnostics={
                        "plan_uid": plan.get("plan_uid"),
                        "run_id": plan.get("run_id"),
                        "symbol": plan.get("symbol"),
                        "execution_mode": plan.get("execution_mode"),
                    },
                )
                continue
            if reconciliation.get("supported") and reconciliation.get("observed"):
                result["reconciled_count"] = int(result["reconciled_count"]) + 1
                result["protected_count"] = int(result["protected_count"]) + 1
                continue
            age_seconds = self._trade_plan_age_seconds(plan)
            if age_seconds is None or age_seconds < timeout:
                continue
            try:
                self._expire_stale_vnpy_trade_plan(plan, timeout_seconds=timeout)
                result["expired_count"] = int(result["expired_count"]) + 1
            except Exception as exc:  # noqa: BLE001 - timeout scans must not kill scheduler threads.
                logger.warning("Expire stale vn.py trade plan failed for %s: %s", plan.get("plan_uid"), exc)
                result["failed_count"] = int(result["failed_count"]) + 1
                result["messages"].append(f"vnpy_order_timeout_failed:{plan.get('plan_uid')}")
                self._record_auto_trade_alert_event(
                    "vnpy_order_timeout_scan_failed",
                    status="failed",
                    reason=str(exc) or "vnpy_order_timeout_scan_failed",
                    diagnostics={
                        "plan_uid": plan.get("plan_uid"),
                        "run_id": plan.get("run_id"),
                        "symbol": plan.get("symbol"),
                        "execution_mode": plan.get("execution_mode"),
                    },
                )
        if int(result["expired_count"]):
            result["messages"].append(f"expired_vnpy_orders:{result['expired_count']}")
        if int(result["reconciled_count"]):
            result["messages"].append(f"reconciled_vnpy_orders:{result['reconciled_count']}")
        if int(result["reconciliation_failed_count"]):
            result["messages"].append(
                f"vnpy_order_reconciliation_failed:{result['reconciliation_failed_count']}"
            )
        return result

    def _reconcile_vnpy_trade_plan(self, plan: Dict[str, Any]) -> Dict[str, Any]:
        order_result = plan.get("order_result") if isinstance(plan.get("order_result"), dict) else {}
        raw = order_result.get("raw") if isinstance(order_result.get("raw"), dict) else {}
        vt_orderid = str(
            raw.get("vt_orderid") or raw.get("vtOrderid") or raw.get("order_id") or ""
        ).strip()
        settings = self.get_settings()
        if not vt_orderid or self.vnpy_main_engine is None or not settings.vnpy_gateway_name:
            return {"supported": False, "observed": False, "vt_orderid": vt_orderid or None}

        bridge_status = get_vnpy_bridge_status(
            main_engine=self.vnpy_main_engine,
            gateway_name=settings.vnpy_gateway_name,
        )
        if not bridge_status.get("order_reconciliation_supported"):
            return {"supported": False, "observed": False, "vt_orderid": vt_orderid}

        snapshot = VnpyMainEngineBridge(
            main_engine=self.vnpy_main_engine,
            gateway_name=settings.vnpy_gateway_name,
        ).snapshot_order(vt_orderid)
        trades = list(snapshot.get("trades") or [])
        for trade in trades:
            synced = self.sync_vnpy_trade_callback(
                vt_orderid=vt_orderid,
                vt_tradeid=self._event_value(trade, "vt_tradeid", "vtTradeid"),
                symbol=self._event_value(trade, "symbol"),
                side=self._side_from_vnpy_direction(self._event_value(trade, "direction")),
                market=self._market_from_vnpy_exchange(self._event_value(trade, "exchange")),
                quantity=_safe_float(self._event_value(trade, "volume")),
                price=_safe_float(self._event_value(trade, "price")),
                trade_date=self._event_value(trade, "datetime", "trade_date", "tradeDate"),
                raw={"reconciled_from_main_engine": True, **self._object_public_dict(trade)},
            )
            if not synced.get("accepted"):
                raise VnpyAdapterError(
                    str(synced.get("reason") or "vn.py trade reconciliation was not accepted")
                )

        order = snapshot.get("order")
        if order is not None:
            synced_order = self.sync_vnpy_order_callback(
                vt_orderid=vt_orderid,
                status=self._event_text(self._event_value(order, "status")),
                symbol=self._event_value(order, "symbol"),
                side=self._side_from_vnpy_direction(self._event_value(order, "direction")),
                market=self._market_from_vnpy_exchange(self._event_value(order, "exchange")),
                volume=_safe_float(self._event_value(order, "volume")),
                traded=_safe_float(self._event_value(order, "traded")),
                price=_safe_float(self._event_value(order, "price")),
                rejected_reason=self._event_value(
                    order,
                    "rejected_reason",
                    "rejectedReason",
                    "status_msg",
                    "statusMsg",
                ),
                raw={"reconciled_from_main_engine": True, **self._object_public_dict(order)},
            )
            if not synced_order.get("accepted") and synced_order.get("reason") == "vnpy_order_plan_not_found":
                raise VnpyAdapterError("vn.py order reconciliation plan disappeared")

        observed = bool(order is not None or trades)
        refreshed = self.agent_repo.get_trade_plan(str(plan.get("plan_uid") or "")) if observed else None
        return {
            "supported": True,
            "observed": observed,
            "vt_orderid": vt_orderid,
            "order_found": order is not None,
            "trade_count": len(trades),
            "status": refreshed.get("status") if isinstance(refreshed, dict) else plan.get("status"),
        }

    def _auto_retry_candidate_trade_plans(self, *, scan_limit: int) -> List[Dict[str, Any]]:
        runs = self.agent_repo.list_recent_runs(
            trigger_source="vnpy_paper_auto",
            limit=max(1, min(200, int(scan_limit or TRADE_PLAN_AUTO_RETRY_SCAN_LIMIT))),
        )
        candidates: List[Dict[str, Any]] = []
        seen_plan_uids: set[str] = set()
        for run in runs:
            run_uid = str(run.get("run_uid") or "")
            if not run_uid:
                continue
            detail = self.agent_repo.get_run_detail(run_uid)
            if not isinstance(detail, dict):
                continue
            for plan in list(detail.get("trade_plans") or []):
                if not isinstance(plan, dict):
                    continue
                plan_uid = str(plan.get("plan_uid") or "")
                if not plan_uid or plan_uid in seen_plan_uids:
                    continue
                if str(plan.get("execution_mode") or "") not in AUTO_RETRY_TRADE_PLAN_EXECUTION_MODES:
                    continue
                if str(plan.get("status") or "").strip().lower() not in {"failed", "skipped"}:
                    continue
                seen_plan_uids.add(plan_uid)
                candidates.append(plan)
        return candidates

    def _active_vnpy_trade_plans(
        self,
        *,
        scan_limit: int,
        minimum_age_seconds: int,
    ) -> List[Dict[str, Any]]:
        runs = self.agent_repo.list_recent_runs(
            trigger_source="vnpy_paper_auto",
            limit=max(1, min(200, int(scan_limit or TRADE_PLAN_AUTO_RETRY_SCAN_LIMIT))),
        )
        now = datetime.now()
        candidates: List[Dict[str, Any]] = []
        seen_plan_uids: set[str] = set()
        for run in runs:
            run_uid = str(run.get("run_uid") or "")
            if not run_uid:
                continue
            detail = self.agent_repo.get_run_detail(run_uid)
            if not isinstance(detail, dict):
                continue
            for plan in list(detail.get("trade_plans") or []):
                if not isinstance(plan, dict):
                    continue
                plan_uid = str(plan.get("plan_uid") or "")
                if not plan_uid or plan_uid in seen_plan_uids:
                    continue
                if str(plan.get("execution_mode") or "") != "vnpy_paper":
                    continue
                if str(plan.get("status") or "").strip().lower() not in {
                    "submitted",
                    "part_filled",
                    "cancel_requested",
                }:
                    continue
                updated_at = self._coerce_local_naive_datetime(plan.get("updated_at") or plan.get("created_at"))
                if updated_at is None or now - updated_at < timedelta(seconds=minimum_age_seconds):
                    continue
                seen_plan_uids.add(plan_uid)
                candidates.append(plan)
        candidates.sort(
            key=lambda plan: self._coerce_local_naive_datetime(
                plan.get("updated_at") or plan.get("created_at")
            )
            or now
        )
        return candidates

    def _trade_plan_age_seconds(self, plan: Dict[str, Any]) -> Optional[float]:
        updated_at = self._coerce_local_naive_datetime(plan.get("updated_at") or plan.get("created_at"))
        if updated_at is None:
            return None
        return max(0.0, (datetime.now() - updated_at).total_seconds())

    def _expire_stale_vnpy_trade_plan(self, plan: Dict[str, Any], *, timeout_seconds: int) -> None:
        plan_uid = str(plan.get("plan_uid") or "").strip()
        if not plan_uid:
            return
        order_result = dict(plan.get("order_result") if isinstance(plan.get("order_result"), dict) else {})
        raw = order_result.get("raw") if isinstance(order_result.get("raw"), dict) else {}
        previous_status = str(plan.get("status") or "").strip().lower()
        if previous_status == "cancel_requested":
            timeout_reason = "vnpy_cancel_timeout"
            timeout_message = "vn.py cancel request timed out before a terminal callback was received."
        elif previous_status == "part_filled":
            timeout_reason = "vnpy_partial_fill_timeout"
            timeout_message = "vn.py partial fill timed out before a trade callback was received."
        else:
            timeout_reason = "vnpy_order_timeout"
            timeout_message = "vn.py order timed out before a terminal callback was received."
        timeout_payload = {
            "reason": timeout_reason,
            "expired_at": _utc_now_iso(),
            "timeout_seconds": int(timeout_seconds),
            "previous_status": previous_status or None,
            "vt_orderid": (
                str(raw.get("vt_orderid") or raw.get("vtOrderid") or raw.get("order_id") or "").strip()
                or None
            ),
        }
        result = {
            **order_result,
            "accepted": False,
            "status": "failed",
            "trade_id": None,
            "symbol": plan.get("symbol"),
            "side": plan.get("side"),
            "quantity": _safe_float(plan.get("submitted_quantity")) or _safe_float(plan.get("planned_quantity")),
            "price": _safe_float(plan.get("submitted_price")) or _safe_float(plan.get("planned_price")),
            "cash_amount": (
                (_safe_float(plan.get("submitted_quantity")) or _safe_float(plan.get("planned_quantity")) or 0.0)
                * (_safe_float(plan.get("submitted_price")) or _safe_float(plan.get("planned_price")) or 0.0)
            ),
            "source": "vnpy_main_engine",
            "message": timeout_message,
            "reason": timeout_reason,
            "raw": {**raw, "timeout": timeout_payload},
        }
        result = self._preserve_trade_plan_retry_metadata(
            plan=plan,
            order=result,
            status="failed",
            reason=timeout_reason,
        )
        self.agent_repo.update_trade_plan_execution(
            plan_uid=plan_uid,
            status="failed",
            submitted_quantity=_safe_float(plan.get("submitted_quantity")),
            submitted_price=_safe_float(plan.get("submitted_price")),
            trade_id=_safe_int(plan.get("trade_id")),
            skip_reason=timeout_reason,
            order_result=result,
        )
        decision_id = _safe_int(plan.get("decision_id"))
        if decision_id is not None:
            self.agent_repo.update_decision_execution(
                decision_id=decision_id,
                status="failed",
                reason=timeout_reason,
                quantity=_safe_float(plan.get("submitted_quantity")) or _safe_float(plan.get("planned_quantity")),
                price=_safe_float(plan.get("submitted_price")) or _safe_float(plan.get("planned_price")),
                trade_id=_safe_int(plan.get("trade_id")),
                order_result=result,
            )
        run_id = _safe_int(plan.get("run_id"))
        if run_id is not None:
            self.agent_repo.refresh_run_trade_counts(run_id)
        self._record_auto_trade_alert_event(
            timeout_reason,
            status="failed",
            reason=timeout_reason,
            threshold=timeout_seconds,
            diagnostics={
                "plan_uid": plan_uid,
                "run_id": run_id,
                "decision_id": decision_id,
                "symbol": plan.get("symbol"),
                "market": plan.get("market"),
                "execution_mode": plan.get("execution_mode"),
                "timeout": timeout_payload,
            },
        )

    @classmethod
    def _auto_retry_due(cls, plan: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        try:
            cls._validate_trade_plan_retry(plan)
        except ValueError as exc:
            return False, str(exc)

        if cls._trade_plan_retry_payload(plan):
            return True, None

        updated_at = cls._parse_db_datetime(plan.get("updated_at") or plan.get("created_at"))
        if updated_at is None:
            return True, None
        now = datetime.now(timezone.utc)
        if now - updated_at < timedelta(seconds=TRADE_PLAN_RETRY_COOLDOWN_SECONDS):
            return False, "trade_plan_initial_retry_cooldown_active"
        return True, None

    def _submit_trade_plan(
        self,
        plan_uid: str,
        *,
        allowed_statuses: set[str],
        invalid_status_error: str,
        allowed_execution_modes: set[str],
        invalid_execution_mode_error: str,
        dedup_prefix: str,
        note_prefix: str,
        is_retry: bool = False,
    ) -> Dict[str, Any]:
        plan_key = str(plan_uid or "").strip()
        if not plan_key:
            raise ValueError("plan_uid is required")

        with self._lock:
            plan = self.agent_repo.get_trade_plan(plan_key)
            if plan is None:
                raise ValueError("trade_plan_not_found")
            execution_mode = str(plan.get("execution_mode") or "").strip()
            if execution_mode not in allowed_execution_modes:
                raise ValueError(invalid_execution_mode_error)
            if str(plan.get("status") or "") not in allowed_statuses:
                raise ValueError(invalid_status_error)
            symbol = self._normalize_symbol(plan.get("symbol") or "")
            if not symbol:
                raise ValueError("trade_plan_missing_symbol")

            settings = self.get_settings()
            retry_context = self._validate_trade_plan_retry(plan) if is_retry else None
            execution_route = "vnpy_bridge" if execution_mode == "vnpy_paper" else "local_paper"
            if settings.auto_skip_existing_positions and symbol in self._held_symbols():
                order = self._skipped_order(
                    symbol=symbol,
                    side=str(plan.get("side") or "buy"),
                    reason="position_exists",
                    message="Position already exists; trade plan was not submitted.",
                    raw={"plan_uid": plan_key, "execution_mode": execution_mode},
                )
            else:
                order = self.submit_order(
                    symbol=symbol,
                    side=str(plan.get("side") or "buy"),
                    market=str(plan.get("market") or settings.auto_market),
                    quantity=_safe_float(plan.get("planned_quantity")),
                    cash_amount=_safe_float(plan.get("planned_cash_amount")),
                    price=_safe_float(plan.get("planned_price")),
                    source="alphasift_auto",
                    dedup_key=f"{dedup_prefix}:{plan_key}",
                    note=f"{note_prefix} {plan_key}",
                    raw={
                        "plan_uid": plan_key,
                        "run_id": plan.get("run_id"),
                        "decision_id": plan.get("decision_id"),
                        "execution_mode": execution_mode,
                    },
                    execution_route=execution_route,
                )
            if retry_context is not None:
                order = self._attach_trade_plan_retry_metadata(
                    plan=plan,
                    order=order,
                    retry_context=retry_context,
                )

            next_status = self._trade_plan_status(order)
            reason = str(order.get("reason")) if order.get("reason") else None
            self.agent_repo.update_trade_plan_execution(
                plan_uid=plan_key,
                status=next_status,
                submitted_quantity=_safe_float(order.get("quantity")) if order.get("accepted") else None,
                submitted_price=_safe_float(order.get("price")) if order.get("accepted") else None,
                trade_id=_safe_int(order.get("trade_id")),
                skip_reason=reason,
                order_result=order,
            )
            decision_id = _safe_int(plan.get("decision_id"))
            if decision_id is not None:
                self.agent_repo.update_decision_execution(
                    decision_id=decision_id,
                    status=next_status,
                    reason=reason,
                    quantity=_safe_float(order.get("quantity")),
                    price=_safe_float(order.get("price")),
                    trade_id=_safe_int(order.get("trade_id")),
                    order_result=order,
                )
            run_id = _safe_int(plan.get("run_id"))
            if run_id is not None:
                self.agent_repo.refresh_run_trade_counts(run_id)

            return order

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _handle_vnpy_order_event(self, event: Any) -> None:
        try:
            data = self._event_data(event)
            self.sync_vnpy_order_callback(
                vt_orderid=str(self._event_value(data, "vt_orderid", "vtOrderid") or ""),
                status=self._event_text(self._event_value(data, "status")),
                symbol=self._event_value(data, "symbol"),
                side=self._side_from_vnpy_direction(self._event_value(data, "direction")),
                market=self._market_from_vnpy_exchange(self._event_value(data, "exchange")),
                volume=_safe_float(self._event_value(data, "volume")),
                traded=_safe_float(self._event_value(data, "traded")),
                price=_safe_float(self._event_value(data, "price")),
                rejected_reason=self._event_value(data, "rejected_reason", "rejectedReason", "status_msg", "statusMsg"),
                raw=self._event_raw(event, data),
            )
        except Exception as exc:  # noqa: BLE001 - event threads must not be interrupted by one bad callback.
            logger.warning("Failed to sync vn.py order event: %s", exc)

    def _handle_vnpy_trade_event(self, event: Any) -> None:
        try:
            data = self._event_data(event)
            self.sync_vnpy_trade_callback(
                vt_orderid=str(self._event_value(data, "vt_orderid", "vtOrderid") or ""),
                vt_tradeid=self._event_value(data, "vt_tradeid", "vtTradeid"),
                symbol=self._event_value(data, "symbol"),
                side=self._side_from_vnpy_direction(self._event_value(data, "direction")),
                market=self._market_from_vnpy_exchange(self._event_value(data, "exchange")),
                quantity=_safe_float(self._event_value(data, "volume")),
                price=_safe_float(self._event_value(data, "price")),
                trade_date=self._event_value(data, "datetime", "trade_date", "tradeDate"),
                raw=self._event_raw(event, data),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to sync vn.py trade event: %s", exc)

    def _handle_vnpy_account_event(self, event: Any) -> None:
        try:
            data = self._event_data(event)
            self.sync_vnpy_account_callback(
                account_id=str(self._event_value(data, "accountid", "account_id", "accountId") or ""),
                balance=_safe_float(self._event_value(data, "balance")),
                available=_safe_float(self._event_value(data, "available")),
                frozen=_safe_float(self._event_value(data, "frozen")),
                margin=_safe_float(self._event_value(data, "margin")),
                close_profit=_safe_float(self._event_value(data, "close_profit", "closeProfit")),
                holding_profit=_safe_float(self._event_value(data, "holding_profit", "holdingProfit")),
                currency=self._event_value(data, "currency"),
                raw=self._event_raw(event, data),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to sync vn.py account event: %s", exc)

    def _handle_vnpy_position_event(self, event: Any) -> None:
        try:
            data = self._event_data(event)
            self.sync_vnpy_positions_callback(
                positions=[self._event_raw(event, data)],
                raw={"event_type": self._event_value(event, "type")},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to sync vn.py position event: %s", exc)

    @staticmethod
    def _event_data(event: Any) -> Any:
        return getattr(event, "data", event)

    @staticmethod
    def _event_value(data: Any, *names: str) -> Any:
        for name in names:
            if isinstance(data, dict) and name in data:
                return data.get(name)
            if hasattr(data, name):
                return getattr(data, name)
        return None

    @classmethod
    def _event_text(cls, value: Any) -> str:
        if value is None:
            return ""
        enum_value = getattr(value, "value", None)
        if enum_value is not None:
            return str(enum_value)
        enum_name = getattr(value, "name", None)
        if enum_name is not None:
            return str(enum_name)
        return str(value)

    @classmethod
    def _side_from_vnpy_direction(cls, value: Any) -> Optional[str]:
        text = cls._event_text(value).strip().lower()
        if text in {"long", "buy", "多"}:
            return "buy"
        if text in {"short", "sell", "空"}:
            return "sell"
        return None

    @classmethod
    def _market_from_vnpy_exchange(cls, value: Any) -> Optional[str]:
        text = cls._event_text(value).strip().upper()
        if text in {"SSE", "SZSE", "XSHG", "XSHE"}:
            return "cn"
        if text in {"SEHK", "HKEX"}:
            return "hk"
        if text in {"SMART", "NASDAQ", "NYSE", "AMEX"}:
            return "us"
        return None

    @classmethod
    def _event_raw(cls, event: Any, data: Any) -> Dict[str, Any]:
        raw = cls._object_public_dict(data)
        event_type = cls._event_value(event, "type")
        if event_type is not None:
            raw["event_type"] = str(event_type)
        return raw

    @classmethod
    def _object_public_dict(cls, value: Any) -> Dict[str, Any]:
        if isinstance(value, dict):
            return {str(key): cls._jsonable_event_value(item) for key, item in value.items()}
        payload: Dict[str, Any] = {}
        source = getattr(value, "__dict__", {})
        if isinstance(source, dict):
            for key, item in source.items():
                if str(key).startswith("_"):
                    continue
                payload[str(key)] = cls._jsonable_event_value(item)
        return payload

    @classmethod
    def _jsonable_event_value(cls, value: Any) -> Any:
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        enum_value = getattr(value, "value", None)
        if enum_value is not None:
            return enum_value
        enum_name = getattr(value, "name", None)
        if enum_name is not None:
            return enum_name
        return str(value)

    @staticmethod
    def _normalize_vnpy_status(status: Any) -> str:
        text = str(status or "").strip().lower()
        return text.replace(" ", "_").replace("-", "_")

    @staticmethod
    def _vnpy_order_failure_reason(order_status: str) -> Optional[str]:
        if order_status in {"rejected", "reject"}:
            return "vnpy_order_rejected"
        if order_status in {"cancelled", "canceled", "cancelled_all", "canceled_all"}:
            return "vnpy_order_cancelled"
        if order_status in {"inactive", "error", "failed"}:
            return "vnpy_order_failed"
        return None

    @staticmethod
    def _vnpy_order_is_part_filled(order_status: str) -> bool:
        return order_status in {
            "parttraded",
            "part_traded",
            "partialtraded",
            "partial_traded",
            "part_filled",
            "partial_filled",
            "partfilled",
            "partialfilled",
            "partially_filled",
        }

    def _update_vnpy_plan_from_order_callback(
        self,
        *,
        plan: Dict[str, Any],
        status: str,
        reason: Optional[str],
        quantity: Optional[float],
        price: Optional[float],
        result: Dict[str, Any],
    ) -> None:
        result = self._preserve_trade_plan_retry_metadata(
            plan=plan,
            order=result,
            status=status,
            reason=reason,
        )
        self.agent_repo.update_trade_plan_execution(
            plan_uid=str(plan["plan_uid"]),
            status=status,
            submitted_quantity=quantity,
            submitted_price=price,
            trade_id=_safe_int(plan.get("trade_id")),
            skip_reason=reason if status == "failed" else None,
            order_result=result,
        )
        decision_id = _safe_int(plan.get("decision_id"))
        if decision_id is not None:
            self.agent_repo.update_decision_execution(
                decision_id=decision_id,
                status=status,
                reason=reason,
                quantity=quantity,
                price=price,
                trade_id=_safe_int(plan.get("trade_id")),
                order_result=result,
            )
        run_id = _safe_int(plan.get("run_id"))
        if run_id is not None:
            self.agent_repo.refresh_run_trade_counts(run_id)

    def _vnpy_sync_state_summary(self) -> Dict[str, Any]:
        state = self._read_vnpy_sync_state()
        orders = state.get("orders") if isinstance(state.get("orders"), dict) else {}
        recent_orders = sorted(
            [item for item in orders.values() if isinstance(item, dict)],
            key=lambda item: str(item.get("updated_at") or ""),
            reverse=True,
        )[:20]
        return {
            "updated_at": state.get("updated_at"),
            "account": state.get("account") if isinstance(state.get("account"), dict) else None,
            "positions": state.get("positions") if isinstance(state.get("positions"), list) else [],
            "position_count": len(state.get("positions") or []) if isinstance(state.get("positions"), list) else 0,
            "recent_orders": recent_orders,
        }

    def _read_vnpy_sync_state(self) -> Dict[str, Any]:
        payload = self._read_config_payload()
        state = payload.get("vnpy_sync_state") if isinstance(payload, dict) else None
        return dict(state) if isinstance(state, dict) else {}

    def _record_vnpy_order_state(self, vt_orderid: str, order_state: Dict[str, Any]) -> None:
        with self._lock:
            payload = self._read_config_payload()
            state = payload.get("vnpy_sync_state") if isinstance(payload.get("vnpy_sync_state"), dict) else {}
            orders = state.get("orders") if isinstance(state.get("orders"), dict) else {}
            now = _utc_now_iso()
            orders[str(vt_orderid)] = {**order_state, "updated_at": now}
            trimmed = sorted(
                orders.items(),
                key=lambda item: str(item[1].get("updated_at") if isinstance(item[1], dict) else ""),
                reverse=True,
            )[:100]
            state["orders"] = {key: value for key, value in trimmed if isinstance(value, dict)}
            state["updated_at"] = now
            payload["vnpy_sync_state"] = state
            self._write_config_payload(payload)

    def _record_vnpy_account_state(self, account_state: Dict[str, Any]) -> None:
        with self._lock:
            payload = self._read_config_payload()
            state = payload.get("vnpy_sync_state") if isinstance(payload.get("vnpy_sync_state"), dict) else {}
            state["account"] = account_state
            state["updated_at"] = _utc_now_iso()
            payload["vnpy_sync_state"] = state
            self._write_config_payload(payload)

    def _record_vnpy_positions_state(self, positions_state: Dict[str, Any]) -> None:
        with self._lock:
            payload = self._read_config_payload()
            state = payload.get("vnpy_sync_state") if isinstance(payload.get("vnpy_sync_state"), dict) else {}
            state["positions"] = positions_state.get("positions") or []
            state["positions_raw"] = positions_state.get("raw") or {}
            state["positions_updated_at"] = positions_state.get("updated_at")
            state["updated_at"] = _utc_now_iso()
            payload["vnpy_sync_state"] = state
            self._write_config_payload(payload)

    def _normalize_vnpy_position(self, position: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(position, dict):
            return {}
        symbol = str(position.get("symbol") or "").strip()
        vt_symbol = str(position.get("vt_symbol") or position.get("vtSymbol") or "").strip()
        if not symbol and vt_symbol:
            symbol = vt_symbol.split(".", 1)[0].strip()
        symbol = self._normalize_symbol(symbol)
        if not symbol:
            return {}
        volume = (
            _safe_float(position.get("volume"))
            or _safe_float(position.get("quantity"))
            or _safe_float(position.get("net_position"))
            or _safe_float(position.get("netPosition"))
        )
        return {
            "symbol": symbol,
            "vt_symbol": vt_symbol or None,
            "market": str(position.get("market") or "").strip()[:16] or None,
            "direction": str(position.get("direction") or "").strip()[:24] or None,
            "volume": volume,
            "yd_volume": _safe_float(position.get("yd_volume") or position.get("ydVolume")),
            "frozen": _safe_float(position.get("frozen")),
            "price": _safe_float(position.get("price") or position.get("avg_price") or position.get("avgPrice")),
            "pnl": _safe_float(position.get("pnl") or position.get("holding_pnl") or position.get("holdingPnl")),
            "raw": position.get("raw") if isinstance(position.get("raw"), dict) else position,
        }

    def _read_config_payload(self) -> Dict[str, Any]:
        try:
            return json.loads(self.config_path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return {}
        except Exception as exc:
            logger.warning("Failed to read vn.py paper config from %s: %s", self.config_path, exc)
            return {}

    def _write_config_payload(self, payload: Dict[str, Any]) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _hidden_archived_account_ids(self) -> set[int]:
        payload = self._read_config_payload()
        raw = payload.get("hidden_archived_account_ids") if isinstance(payload, dict) else None
        return self._account_id_set(raw)

    @staticmethod
    def _account_id_set(value: Any) -> set[int]:
        if isinstance(value, (list, tuple, set)):
            raw_items = value
        elif value is None:
            raw_items = []
        else:
            raw_items = [value]
        ids: set[int] = set()
        for item in raw_items:
            account_id = _safe_int(item)
            if account_id is not None and account_id > 0:
                ids.add(account_id)
        return ids

    def _record_last_auto_run(self, result: Dict[str, Any]) -> None:
        with self._lock:
            payload = self._read_config_payload()
            payload["last_auto_run"] = {
                "ran_at": _utc_now_iso(),
                "accepted": bool(result.get("accepted")),
                "skipped": bool(result.get("skipped")),
                "reason": result.get("reason"),
                "strategy": result.get("strategy"),
                "market": result.get("market"),
                "candidate_count": result.get("candidate_count", 0),
                "planned_count": result.get("planned_count", 0),
                "submitted_count": result.get("submitted_count", 0),
                "skipped_count": result.get("skipped_count", 0),
            }
            self._write_config_payload(payload)

    def _normalize_settings(self, raw: Dict[str, Any]) -> VnpyPaperSettings:
        defaults = VnpyPaperSettings()
        account_id = _safe_int(raw.get("account_id"))
        initial_cash = _safe_float(raw.get("initial_cash"))
        auto_max_results = _safe_int(raw.get("auto_max_results"))
        auto_cash_per_order = _safe_float(raw.get("auto_cash_per_order"))
        auto_interval_minutes = _safe_int(raw.get("auto_interval_minutes"))
        auto_min_score = _safe_float(raw.get("auto_min_score"))
        auto_max_positions = _safe_int(raw.get("auto_max_positions"))
        auto_max_single_position_value = _safe_float(raw.get("auto_max_single_position_value"))
        auto_max_total_position_value = _safe_float(raw.get("auto_max_total_position_value"))
        auto_max_total_position_pct = _safe_float(raw.get("auto_max_total_position_pct"))
        auto_max_industry_position_value = _safe_float(raw.get("auto_max_industry_position_value"))
        auto_max_industry_position_pct = _safe_float(raw.get("auto_max_industry_position_pct"))
        auto_daily_max_orders = _safe_int(raw.get("auto_daily_max_orders"))
        auto_daily_budget = _safe_float(raw.get("auto_daily_budget"))
        auto_min_turnover = _safe_float(raw.get("auto_min_turnover"))
        auto_min_cash_balance = _safe_float(raw.get("auto_min_cash_balance"))
        auto_max_drawdown_pct = _safe_float(raw.get("auto_max_drawdown_pct"))
        auto_failure_fuse_threshold = _safe_int(raw.get("auto_failure_fuse_threshold"))
        auto_stop_loss_pct = _safe_float(raw.get("auto_stop_loss_pct"))
        auto_take_profit_pct = _safe_float(raw.get("auto_take_profit_pct"))
        auto_trailing_stop_pct = _safe_float(raw.get("auto_trailing_stop_pct"))
        auto_max_holding_days = _safe_int(raw.get("auto_max_holding_days"))
        auto_sell_position_pct = _safe_float(raw.get("auto_sell_position_pct"))
        auto_no_progress_days = _safe_int(raw.get("auto_no_progress_days"))
        auto_no_progress_min_return_pct = _safe_float(raw.get("auto_no_progress_min_return_pct"))
        auto_execution_mode = str(raw.get("auto_execution_mode") or defaults.auto_execution_mode).strip().lower()
        if auto_execution_mode not in {"paper", "vnpy_paper", "dry_run", "manual_approval"}:
            auto_execution_mode = defaults.auto_execution_mode
        vnpy_gateway_name = (
            str(raw.get("vnpy_gateway_name") or os.getenv("VNPY_GATEWAY_NAME") or "").strip()[:64]
            or None
        )
        auto_symbol_blacklist = self._normalize_symbol_list(raw.get("auto_symbol_blacklist"))
        auto_target_position_weights = self._normalize_target_weight_map(
            raw.get("auto_target_position_weights"),
            key_type="symbol",
        )
        auto_target_industry_weights = self._normalize_target_weight_map(
            raw.get("auto_target_industry_weights"),
            key_type="industry",
        )
        auto_market_light_block_statuses = self._normalize_market_light_statuses(
            raw.get("auto_market_light_block_statuses"),
            default=defaults.auto_market_light_block_statuses,
        )
        return VnpyPaperSettings(
            enabled=bool(raw.get("enabled", defaults.enabled)),
            account_id=account_id if account_id and account_id > 0 else None,
            initial_cash=initial_cash if initial_cash and initial_cash > 0 else defaults.initial_cash,
            auto_trade_enabled=bool(raw.get("auto_trade_enabled", defaults.auto_trade_enabled)),
            auto_strategy=str(raw.get("auto_strategy") or defaults.auto_strategy).strip()[:64] or defaults.auto_strategy,
            auto_market=str(raw.get("auto_market") or defaults.auto_market).strip()[:16] or defaults.auto_market,
            auto_max_results=max(1, min(50, auto_max_results or defaults.auto_max_results)),
            auto_cash_per_order=(
                auto_cash_per_order
                if auto_cash_per_order is not None and auto_cash_per_order > 0
                else defaults.auto_cash_per_order
            ),
            auto_interval_minutes=max(1, min(10080, auto_interval_minutes or defaults.auto_interval_minutes)),
            auto_min_score=auto_min_score,
            auto_skip_existing_positions=bool(
                raw.get("auto_skip_existing_positions", defaults.auto_skip_existing_positions)
            ),
            auto_execution_mode=auto_execution_mode,
            auto_max_positions=max(1, min(200, auto_max_positions or defaults.auto_max_positions)),
            auto_max_single_position_value=(
                auto_max_single_position_value
                if auto_max_single_position_value is not None and auto_max_single_position_value > 0
                else None
            ),
            auto_max_total_position_value=(
                auto_max_total_position_value
                if auto_max_total_position_value is not None and auto_max_total_position_value > 0
                else None
            ),
            auto_max_total_position_pct=(
                min(100.0, auto_max_total_position_pct)
                if auto_max_total_position_pct is not None and auto_max_total_position_pct > 0
                else None
            ),
            auto_max_industry_position_value=(
                auto_max_industry_position_value
                if auto_max_industry_position_value is not None and auto_max_industry_position_value > 0
                else None
            ),
            auto_max_industry_position_pct=(
                min(100.0, auto_max_industry_position_pct)
                if auto_max_industry_position_pct is not None and auto_max_industry_position_pct > 0
                else None
            ),
            auto_daily_max_orders=(
                max(1, min(200, auto_daily_max_orders))
                if auto_daily_max_orders is not None and auto_daily_max_orders > 0
                else None
            ),
            auto_daily_budget=(
                auto_daily_budget
                if auto_daily_budget is not None and auto_daily_budget > 0
                else None
            ),
            auto_trade_time_gate_enabled=bool(
                raw.get("auto_trade_time_gate_enabled", defaults.auto_trade_time_gate_enabled)
            ),
            auto_symbol_blacklist=auto_symbol_blacklist,
            auto_exclude_st=bool(raw.get("auto_exclude_st", defaults.auto_exclude_st)),
            auto_exclude_suspended=bool(raw.get("auto_exclude_suspended", defaults.auto_exclude_suspended)),
            auto_exclude_price_limit=bool(
                raw.get("auto_exclude_price_limit", defaults.auto_exclude_price_limit)
            ),
            auto_min_turnover=(
                auto_min_turnover
                if auto_min_turnover is not None and auto_min_turnover > 0
                else None
            ),
            auto_min_cash_balance=(
                auto_min_cash_balance
                if auto_min_cash_balance is not None and auto_min_cash_balance > 0
                else None
            ),
            auto_max_drawdown_pct=(
                min(100.0, auto_max_drawdown_pct)
                if auto_max_drawdown_pct is not None and auto_max_drawdown_pct > 0
                else None
            ),
            auto_market_light_gate_enabled=bool(
                raw.get("auto_market_light_gate_enabled", defaults.auto_market_light_gate_enabled)
            ),
            auto_market_light_block_statuses=auto_market_light_block_statuses,
            auto_failure_fuse_enabled=bool(
                raw.get("auto_failure_fuse_enabled", defaults.auto_failure_fuse_enabled)
            ),
            auto_failure_fuse_threshold=max(
                2,
                min(20, auto_failure_fuse_threshold or defaults.auto_failure_fuse_threshold),
            ),
            auto_sell_enabled=bool(raw.get("auto_sell_enabled", defaults.auto_sell_enabled)),
            auto_stop_loss_pct=(
                min(1000.0, auto_stop_loss_pct)
                if auto_stop_loss_pct is not None and auto_stop_loss_pct > 0
                else None
            ),
            auto_take_profit_pct=(
                min(1000.0, auto_take_profit_pct)
                if auto_take_profit_pct is not None and auto_take_profit_pct > 0
                else None
            ),
            auto_trailing_stop_pct=(
                min(100.0, auto_trailing_stop_pct)
                if auto_trailing_stop_pct is not None and auto_trailing_stop_pct > 0
                else None
            ),
            auto_max_holding_days=(
                max(1, min(3650, auto_max_holding_days))
                if auto_max_holding_days is not None and auto_max_holding_days > 0
                else None
            ),
            auto_sell_position_pct=(
                min(100.0, auto_sell_position_pct)
                if auto_sell_position_pct is not None and auto_sell_position_pct > 0
                else None
            ),
            auto_signal_exit_enabled=bool(
                raw.get("auto_signal_exit_enabled", defaults.auto_signal_exit_enabled)
            ),
            auto_no_progress_days=(
                max(1, min(3650, auto_no_progress_days))
                if auto_no_progress_days is not None and auto_no_progress_days > 0
                else None
            ),
            auto_no_progress_min_return_pct=(
                max(-100.0, min(1000.0, auto_no_progress_min_return_pct))
                if auto_no_progress_min_return_pct is not None
                else None
            ),
            auto_rebalance_enabled=bool(
                raw.get("auto_rebalance_enabled", defaults.auto_rebalance_enabled)
            ),
            auto_target_position_weights=auto_target_position_weights,
            auto_target_industry_weights=auto_target_industry_weights,
            auto_llm_plan_enabled=bool(
                raw.get("auto_llm_plan_enabled", defaults.auto_llm_plan_enabled)
            ),
            auto_llm_review_enabled=bool(
                raw.get("auto_llm_review_enabled", defaults.auto_llm_review_enabled)
            ),
            vnpy_gateway_name=vnpy_gateway_name,
        )

    def _normalize_symbol_list(self, value: Any) -> List[str]:
        if value is None:
            return []
        if isinstance(value, str):
            raw_items = value.replace("，", ",").replace(";", ",").replace("；", ",").split(",")
        elif isinstance(value, (list, tuple, set)):
            raw_items = list(value)
        else:
            raw_items = [value]

        normalized: List[str] = []
        seen = set()
        for item in raw_items:
            symbol = self._normalize_symbol(item)
            if not symbol or symbol in seen:
                continue
            normalized.append(symbol)
            seen.add(symbol)
            if len(normalized) >= 200:
                break
        return normalized

    def _normalize_target_weight_map(self, value: Any, *, key_type: str) -> Dict[str, float]:
        if value is None:
            return {}
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return {}
            if text.startswith("{"):
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    return {}
                raw_items = parsed.items() if isinstance(parsed, dict) else []
            else:
                raw_items = []
                for chunk in text.replace("；", ";").replace("，", ",").replace("\n", ",").split(","):
                    item = chunk.strip()
                    if not item:
                        continue
                    if ":" in item:
                        key, raw_weight = item.split(":", 1)
                    elif "=" in item:
                        key, raw_weight = item.split("=", 1)
                    else:
                        continue
                    raw_items.append((key, raw_weight))
        elif isinstance(value, dict):
            raw_items = value.items()
        else:
            raw_items = []

        normalized: Dict[str, float] = {}
        for raw_key, raw_weight in raw_items:
            if key_type == "symbol":
                key = self._normalize_symbol(raw_key)[:32]
            else:
                key = str(raw_key or "").strip()[:64]
            weight = _safe_float(raw_weight)
            if not key or weight is None or weight <= 0:
                continue
            normalized[key] = round(min(100.0, weight), 6)
            if len(normalized) >= 200:
                break
        return normalized

    @staticmethod
    def _normalize_market_light_statuses(value: Any, *, default: List[str]) -> List[str]:
        if value is None:
            raw_items = list(default)
        elif isinstance(value, str):
            raw_items = value.replace("，", ",").replace(";", ",").replace("；", ",").split(",")
        elif isinstance(value, (list, tuple, set)):
            raw_items = list(value)
        else:
            raw_items = [value]

        normalized: List[str] = []
        for item in raw_items:
            status = str(item or "").strip().lower()
            if status not in {"red", "yellow"} or status in normalized:
                continue
            normalized.append(status)
        return normalized or list(default)

    def _find_account(self, account_id: Optional[int]) -> Optional[Dict[str, Any]]:
        if account_id is None:
            return None
        for account in self.portfolio.list_accounts(include_inactive=False):
            if int(account.get("id")) == int(account_id):
                return account
        return None

    def _find_default_account(self) -> Optional[Dict[str, Any]]:
        for account in self.portfolio.list_accounts(include_inactive=False):
            if (account.get("broker") or "").strip() == VNPY_PAPER_BROKER:
                return account
        return None

    def _resolve_order_price(self, symbol: str, price: Optional[float]) -> Tuple[Optional[float], str]:
        configured = _safe_float(price)
        if configured is not None and configured > 0:
            return configured, "request"
        try:
            quote = self.data_fetcher_manager.get_realtime_quote(symbol)
        except Exception as exc:
            logger.warning("vn.py paper quote fetch failed for %s: %s", symbol, exc)
            return None, "unavailable"
        provider = self._quote_provider(quote)
        quote_price = _safe_float(getattr(quote, "price", None))
        if quote_price is not None and quote_price > 0:
            return quote_price, provider
        return None, provider or "unavailable"

    def _resolve_order_quantity(
        self,
        *,
        market: str,
        side: str,
        quantity: Optional[float],
        cash_amount: Optional[float],
        price: float,
    ) -> float:
        resolved = _safe_float(quantity)
        if resolved is None:
            cash = _safe_float(cash_amount)
            resolved = 0.0 if cash is None else cash / price
        if (market or "").lower() == "cn" and side == "buy":
            return math.floor(float(resolved) / 100.0) * 100.0
        return float(resolved)

    def _account_cash(self, account_id: int) -> float:
        try:
            snapshot = self.portfolio.get_portfolio_snapshot(account_id=account_id)
            accounts = snapshot.get("accounts") or []
            if accounts:
                return float(accounts[0].get("total_cash") or 0.0)
        except Exception as exc:
            logger.warning("Failed to resolve vn.py paper cash: %s", exc)
        return 0.0

    def _auto_order_currency_budget(self, settings: VnpyPaperSettings) -> Dict[str, Any]:
        account = self.ensure_account(settings=settings)
        base_currency = str(account.get("base_currency") or "CNY").strip().upper() or "CNY"
        quote_currency = self._currency_for_market(settings.auto_market)
        base_cash_amount = max(0.0, float(settings.auto_cash_per_order or 0.0))
        try:
            quote_cash_amount, stale, source = self.portfolio.convert_amount(
                amount=base_cash_amount,
                from_currency=base_currency,
                to_currency=quote_currency,
                as_of_date=date.today(),
            )
        except Exception as exc:  # noqa: BLE001 - automatic orders must fail closed on FX errors.
            return {
                "available": False,
                "base_currency": base_currency,
                "quote_currency": quote_currency,
                "base_cash_amount": round(base_cash_amount, 6),
                "quote_cash_amount": None,
                "rate": None,
                "source": "conversion_error",
                "stale": True,
                "error": str(exc),
            }
        available = base_currency == quote_currency or not stale
        rate = quote_cash_amount / base_cash_amount if base_cash_amount > 0 else None
        return {
            "available": bool(available),
            "base_currency": base_currency,
            "quote_currency": quote_currency,
            "base_cash_amount": round(base_cash_amount, 6),
            "quote_cash_amount": round(float(quote_cash_amount), 6),
            "rate": round(float(rate), 10) if rate is not None else None,
            "source": source,
            "stale": bool(stale),
        }

    @staticmethod
    def _scaled_currency_budget(
        budget: Dict[str, Any],
        *,
        base_cash_amount: float,
    ) -> Dict[str, Any]:
        scaled = dict(budget)
        base_amount = max(0.0, float(base_cash_amount or 0.0))
        base_currency = str(budget.get("base_currency") or "").strip().upper()
        quote_currency = str(budget.get("quote_currency") or "").strip().upper()
        rate = _safe_float(budget.get("rate"))
        quote_amount = base_amount
        if base_currency != quote_currency:
            quote_amount = base_amount * float(rate or 0.0)
        scaled["base_cash_amount"] = round(base_amount, 6)
        scaled["quote_cash_amount"] = round(quote_amount, 6)
        return scaled

    def _executable_target_weight_budget(
        self,
        *,
        settings: VnpyPaperSettings,
        budget: Dict[str, Any],
        sizing: Dict[str, Any],
        price: Optional[float],
    ) -> Tuple[float, Dict[str, Any], Optional[str]]:
        base_amount = _safe_float(budget.get("base_cash_amount")) or 0.0
        if not sizing.get("adjusted") or price is None or price <= 0:
            return base_amount, budget, None

        quote_amount = _safe_float(budget.get("quote_cash_amount")) or 0.0
        quantity = self._resolve_order_quantity(
            market=settings.auto_market,
            side="buy",
            quantity=None,
            cash_amount=quote_amount,
            price=price,
        )
        if quantity <= 0:
            sizing.update(
                {
                    "executable_quantity": 0.0,
                    "executable_quote_amount": 0.0,
                    "executable_base_amount": 0.0,
                    "lot_adjusted": True,
                }
            )
            return 0.0, budget, "target_weight_below_min_lot"

        executable_quote = quantity * price
        base_currency = str(budget.get("base_currency") or "").strip().upper()
        quote_currency = str(budget.get("quote_currency") or "").strip().upper()
        rate = _safe_float(budget.get("rate"))
        if base_currency == quote_currency:
            executable_base = executable_quote
        elif rate is None or rate <= 0:
            return 0.0, budget, "fx_rate_unavailable"
        else:
            executable_base = executable_quote / rate
        executable_budget = dict(budget)
        executable_budget["base_cash_amount"] = round(executable_base, 6)
        executable_budget["quote_cash_amount"] = round(executable_quote, 6)
        sizing.update(
            {
                "executable_quantity": round(quantity, 6),
                "executable_quote_amount": round(executable_quote, 6),
                "executable_base_amount": round(executable_base, 6),
                "lot_adjusted": executable_base + PAPER_EPS < base_amount,
            }
        )
        return executable_base, executable_budget, None

    @staticmethod
    def _annotate_order_currency_budget(order: Dict[str, Any], budget: Dict[str, Any]) -> None:
        order["cash_amount_base"] = budget.get("base_cash_amount")
        order["cash_amount_quote"] = order.get("cash_amount")
        order["base_currency"] = budget.get("base_currency")
        order["quote_currency"] = budget.get("quote_currency")
        raw = dict(order.get("raw") if isinstance(order.get("raw"), dict) else {})
        raw["fx_conversion"] = dict(budget)
        order["raw"] = raw

    @staticmethod
    def _order_base_cash_amount(order: Dict[str, Any], *, settings: VnpyPaperSettings) -> float:
        value = _safe_float(order.get("cash_amount_base"))
        if value is not None:
            return max(0.0, float(value))
        return max(0.0, float(settings.auto_cash_per_order or 0.0))

    def _auto_trade_time_gate(self, settings: VnpyPaperSettings) -> Tuple[Optional[str], Dict[str, Any]]:
        if (
            not settings.auto_trade_time_gate_enabled
            or settings.auto_execution_mode in {"dry_run", "manual_approval"}
        ):
            return None, {}
        try:
            ctx = trading_calendar.build_market_phase_context(
                market=settings.auto_market,
                trigger_source="vnpy_paper_auto",
                analysis_intent="auto",
            )
            diagnostics = ctx.to_dict()
        except Exception as exc:  # noqa: BLE001 - fail closed for automatic paper execution.
            logger.warning("Failed to resolve vn.py paper market phase: %s", exc)
            return "market_phase_unknown", {"error": str(exc)}

        if ctx.is_market_open_now is True:
            return None, diagnostics
        if ctx.phase == trading_calendar.MarketPhase.NON_TRADING:
            return "non_trading_day", diagnostics
        if ctx.phase == trading_calendar.MarketPhase.UNKNOWN:
            return "market_phase_unknown", diagnostics
        return "outside_trading_session", diagnostics

    def _build_agent_plan(
        self,
        settings: VnpyPaperSettings,
        *,
        execution_mode_override: Optional[str],
        ignore_auto_trade_enabled: bool,
        llm_dynamic_plan: Optional[Dict[str, Any]] = None,
        recent_run_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        configured_layers = self._agent_plan_configured_layers(settings)
        execution_policy = self._agent_plan_execution_policy(settings)
        risk_level = self._agent_plan_risk_level(settings, configured_layers=configured_layers)
        plan_mode = self._agent_plan_mode(settings)
        max_planned_cash = max(0, int(settings.auto_max_results or 0)) * max(
            0.0,
            float(settings.auto_cash_per_order or 0.0),
        )
        if settings.auto_daily_budget is not None:
            max_planned_cash = min(max_planned_cash, max(0.0, float(settings.auto_daily_budget)))
        dynamic_plan_status = str((llm_dynamic_plan or {}).get("status") or "") if llm_dynamic_plan else None
        dynamic_plan_overrides = (
            llm_dynamic_plan.get("applied_overrides")
            if isinstance(llm_dynamic_plan, dict) and isinstance(llm_dynamic_plan.get("applied_overrides"), dict)
            else {}
        )
        return {
            "schema_version": 1,
            "created_at": _utc_now_iso(),
            "objective": "screen_online_candidates_and_generate_simulated_trade_plans",
            "trigger_source": "vnpy_paper_auto",
            "strategy": settings.auto_strategy,
            "market": settings.auto_market,
            "max_results": settings.auto_max_results,
            "cash_per_order": settings.auto_cash_per_order,
            "min_score": settings.auto_min_score,
            "skip_existing_positions": settings.auto_skip_existing_positions,
            "execution_mode": settings.auto_execution_mode,
            "execution_mode_override": execution_mode_override,
            "ignore_auto_trade_enabled": bool(ignore_auto_trade_enabled),
            "plan_profile": {
                "mode": plan_mode,
                "risk_level": risk_level,
                "configured_layer_count": len(configured_layers),
            },
            "execution_policy": execution_policy,
            "position_plan": {
                "side": "buy",
                "cash_per_order": settings.auto_cash_per_order,
                "lot_rule": "cn_buy_round_down_to_100_shares",
                "price_source": "candidate_price_or_realtime_quote",
                "dedup_scope": "trade_date_strategy_market_symbol",
            },
            "sizing_plan": {
                "method": "fixed_cash_per_candidate",
                "cash_per_order": settings.auto_cash_per_order,
                "max_results": settings.auto_max_results,
                "max_planned_cash": round(max_planned_cash, 6),
                "daily_budget": settings.auto_daily_budget,
                "daily_budget_caps_plan": (
                    settings.auto_daily_budget is not None
                    and max_planned_cash < settings.auto_cash_per_order * max(0, int(settings.auto_max_results or 0))
                ),
            },
            "risk_budget": {
                "max_positions": settings.auto_max_positions,
                "max_single_position_value": settings.auto_max_single_position_value,
                "max_total_position_value": settings.auto_max_total_position_value,
                "max_total_position_pct": settings.auto_max_total_position_pct,
                "max_industry_position_value": settings.auto_max_industry_position_value,
                "max_industry_position_pct": settings.auto_max_industry_position_pct,
                "daily_max_orders": settings.auto_daily_max_orders,
                "daily_budget": settings.auto_daily_budget,
                "min_cash_balance": settings.auto_min_cash_balance,
                "max_drawdown_pct": settings.auto_max_drawdown_pct,
            },
            "candidate_filters": {
                "symbol_blacklist_count": len(settings.auto_symbol_blacklist or []),
                "exclude_st": settings.auto_exclude_st,
                "exclude_suspended": settings.auto_exclude_suspended,
                "exclude_price_limit": settings.auto_exclude_price_limit,
                "min_turnover": settings.auto_min_turnover,
            },
            "gates": {
                "auto_trade_enabled": settings.auto_trade_enabled,
                "time_gate_enabled": settings.auto_trade_time_gate_enabled,
                "time_gate_enforced": (
                    bool(settings.auto_trade_time_gate_enabled)
                    and settings.auto_execution_mode not in {"dry_run", "manual_approval"}
                ),
                "market_light_gate_enabled": settings.auto_market_light_gate_enabled,
                "market_light_block_statuses": list(settings.auto_market_light_block_statuses or []),
                "failure_fuse_enabled": settings.auto_failure_fuse_enabled,
                "failure_fuse_threshold": settings.auto_failure_fuse_threshold,
                "llm_dynamic_plan_enabled": settings.auto_llm_plan_enabled,
                "llm_dynamic_plan_status": dynamic_plan_status,
                "llm_dynamic_plan_applied": bool(dynamic_plan_overrides),
                "llm_review_enabled": settings.auto_llm_review_enabled,
                "llm_review_fail_closed": settings.auto_llm_review_enabled,
                "acceptable_data_quality": ["ok", "partial"],
            },
            "llm_dynamic_plan": llm_dynamic_plan,
            "recent_run_context": recent_run_context or self._recent_agent_run_context(settings),
            "adaptive_controls": {
                "risk_level": risk_level,
                "configured_layers": configured_layers,
                "degrade_actions": self._agent_plan_degrade_actions(settings),
            },
            "sell_policy": {
                "enabled": settings.auto_sell_enabled,
                "stop_loss_pct": settings.auto_stop_loss_pct,
                "take_profit_pct": settings.auto_take_profit_pct,
                "trailing_stop_pct": settings.auto_trailing_stop_pct,
                "max_holding_days": settings.auto_max_holding_days,
                "sell_position_pct": settings.auto_sell_position_pct or 100.0,
                "signal_exit_enabled": settings.auto_signal_exit_enabled,
                "no_progress_days": settings.auto_no_progress_days,
                "no_progress_min_return_pct": (
                    settings.auto_no_progress_min_return_pct
                    if settings.auto_no_progress_min_return_pct is not None
                    else 0.0
                ),
                "rebalance_enabled": settings.auto_rebalance_enabled,
                "target_position_weights": dict(settings.auto_target_position_weights or {}),
                "target_industry_weights": dict(settings.auto_target_industry_weights or {}),
                "mode": (
                    "signal_and_partial_position_exit"
                    if settings.auto_sell_enabled
                    and settings.auto_signal_exit_enabled
                    and settings.auto_sell_position_pct is not None
                    and settings.auto_sell_position_pct < 100
                    else "signal_exit"
                    if settings.auto_sell_enabled and settings.auto_signal_exit_enabled
                    else "partial_position_exit"
                    if settings.auto_sell_enabled
                    and settings.auto_sell_position_pct is not None
                    and settings.auto_sell_position_pct < 100
                    else "full_position_exit_only"
                    if settings.auto_sell_enabled
                    else "disabled"
                ),
            },
            "expected_outputs": [
                "stock_selection_agent_decisions",
                "stock_selection_agent_trade_plans",
                "timeline",
                "llm_dynamic_plan",
                "agent_review",
                "llm_review",
            ],
        }

    def _recent_agent_run_context(
        self,
        settings: VnpyPaperSettings,
        *,
        limit: int = 5,
    ) -> Dict[str, Any]:
        recent = self.agent_repo.list_recent_runs(
            trigger_source="vnpy_paper_auto",
            strategy=settings.auto_strategy,
            market=settings.auto_market,
            limit=limit,
        )
        status_counts: Counter[str] = Counter()
        quality_counts: Counter[str] = Counter()
        candidate_count = 0
        planned_count = 0
        submitted_count = 0
        skipped_count = 0
        failure_streak = 0
        compact_runs: List[Dict[str, Any]] = []
        for index, item in enumerate(recent):
            status = str(item.get("status") or "unknown").strip().lower() or "unknown"
            status_counts[status] += 1
            if index == failure_streak and status == "failed":
                failure_streak += 1
            diagnostics = item.get("diagnostics") if isinstance(item.get("diagnostics"), dict) else {}
            data_quality = diagnostics.get("data_quality") if isinstance(diagnostics.get("data_quality"), dict) else {}
            quality = str(data_quality.get("status") or "unknown").strip().lower() or "unknown"
            quality_counts[quality] += 1
            item_candidates = int(item.get("candidate_count") or 0)
            item_planned = int(item.get("planned_count") or 0)
            item_submitted = int(item.get("submitted_count") or 0)
            item_skipped = int(item.get("skipped_count") or 0)
            candidate_count += item_candidates
            planned_count += item_planned
            submitted_count += item_submitted
            skipped_count += item_skipped
            compact_runs.append({
                "run_uid": item.get("run_uid"),
                "created_at": item.get("created_at"),
                "status": status,
                "data_quality": quality,
                "candidate_count": item_candidates,
                "planned_count": item_planned,
                "submitted_count": item_submitted,
                "skipped_count": item_skipped,
                "error": str(item.get("error") or "")[:160] or None,
            })
        return {
            "schema_version": 1,
            "scope": "same_trigger_strategy_market",
            "strategy": settings.auto_strategy,
            "market": settings.auto_market,
            "limit": limit,
            "run_count": len(recent),
            "status_counts": dict(sorted(status_counts.items())),
            "data_quality_counts": dict(sorted(quality_counts.items())),
            "candidate_count": candidate_count,
            "planned_count": planned_count,
            "submitted_count": submitted_count,
            "skipped_count": skipped_count,
            "submission_rate_pct": (
                round(submitted_count / candidate_count * 100.0, 2)
                if candidate_count > 0
                else None
            ),
            "current_failure_streak": failure_streak,
            "latest_run_uid": compact_runs[0]["run_uid"] if compact_runs else None,
            "latest_status": compact_runs[0]["status"] if compact_runs else None,
            "runs": compact_runs,
        }

    @staticmethod
    def _agent_plan_mode(settings: VnpyPaperSettings) -> str:
        if not settings.auto_trade_enabled:
            return "observe_only"
        if settings.auto_execution_mode == "dry_run":
            return "rehearsal"
        if settings.auto_execution_mode == "manual_approval":
            return "approval_required"
        if settings.auto_execution_mode == "vnpy_paper":
            return "vnpy_bridge_execution"
        return "local_paper_execution"

    @staticmethod
    def _agent_plan_execution_policy(settings: VnpyPaperSettings) -> Dict[str, Any]:
        execution_mode = str(settings.auto_execution_mode or "").strip()
        return {
            "execution_mode": execution_mode,
            "order_route": (
                "vnpy_bridge"
                if execution_mode == "vnpy_paper"
                else "plan_only"
                if execution_mode == "dry_run"
                else "manual_approval"
                if execution_mode == "manual_approval"
                else "local_paper"
            ),
            "records_trade_plan": True,
            "submits_orders": bool(settings.auto_trade_enabled and execution_mode in {"paper", "vnpy_paper"}),
            "requires_user_approval": execution_mode == "manual_approval",
            "time_gate_applies": (
                bool(settings.auto_trade_time_gate_enabled)
                and execution_mode not in {"dry_run", "manual_approval"}
            ),
            "failure_mode": "skip_and_audit",
        }

    @classmethod
    def _agent_plan_configured_layers(cls, settings: VnpyPaperSettings) -> List[str]:
        layers: List[str] = []
        if settings.auto_trade_time_gate_enabled:
            layers.append("time_gate")
        if (
            settings.auto_exclude_st
            or settings.auto_exclude_suspended
            or settings.auto_exclude_price_limit
            or settings.auto_min_turnover is not None
            or bool(settings.auto_symbol_blacklist)
        ):
            layers.append("candidate_filters")
        if cls._position_exposure_configured(settings) or settings.auto_max_positions > 0:
            layers.append("portfolio_limits")
        if settings.auto_daily_max_orders is not None or settings.auto_daily_budget is not None:
            layers.append("daily_limits")
        if settings.auto_min_cash_balance is not None or settings.auto_max_drawdown_pct is not None:
            layers.append("account_risk")
        if settings.auto_market_light_gate_enabled:
            layers.append("market_light")
        if settings.auto_failure_fuse_enabled:
            layers.append("failure_fuse")
        if settings.auto_sell_enabled:
            layers.append("sell_policy")
        if settings.auto_rebalance_enabled:
            layers.append("rebalance")
        if settings.auto_llm_plan_enabled:
            layers.append("llm_dynamic_plan")
        if settings.auto_llm_review_enabled:
            layers.append("llm_review")
        return layers

    @staticmethod
    def _agent_plan_risk_level(settings: VnpyPaperSettings, *, configured_layers: List[str]) -> str:
        if not settings.auto_trade_enabled:
            return "observe_only"
        if settings.auto_execution_mode in {"dry_run", "manual_approval"}:
            return "rehearsal"
        if len(configured_layers) >= 6:
            return "strict"
        if len(configured_layers) >= 3:
            return "guarded"
        return "baseline"

    @staticmethod
    def _agent_plan_degrade_actions(settings: VnpyPaperSettings) -> List[Dict[str, Any]]:
        actions = [
            {
                "reason": "data_quality_stale",
                "action": "skip_run_and_audit",
                "alert": True,
            },
            {
                "reason": "data_quality_unavailable",
                "action": "skip_run_and_audit",
                "alert": True,
            },
            {
                "reason": "score_below_threshold",
                "action": "skip_candidate",
                "alert": False,
            },
            {
                "reason": "active_vnpy_order_exists",
                "action": "skip_candidate",
                "alert": False,
            },
        ]
        if settings.auto_trade_time_gate_enabled and settings.auto_execution_mode not in {"dry_run", "manual_approval"}:
            actions.extend(
                [
                    {"reason": "non_trading_day", "action": "skip_execution", "alert": False},
                    {"reason": "outside_trading_session", "action": "skip_execution", "alert": False},
                    {"reason": "market_phase_unknown", "action": "skip_execution", "alert": False},
                ]
            )
        if settings.auto_min_cash_balance is not None or settings.auto_max_drawdown_pct is not None:
            actions.extend(
                [
                    {"reason": "cash_low_watermark", "action": "skip_buy_and_alert", "alert": True},
                    {"reason": "account_drawdown_limit_reached", "action": "skip_buy_and_alert", "alert": True},
                ]
            )
        if settings.auto_market_light_gate_enabled:
            actions.append(
                {
                    "reason": "market_light_blocked",
                    "action": "skip_buy",
                    "statuses": list(settings.auto_market_light_block_statuses or []),
                    "alert": False,
                }
            )
        if settings.auto_failure_fuse_enabled:
            actions.append({"reason": "failure_fuse_open", "action": "skip_run_and_alert", "alert": True})
        return actions

    @classmethod
    def _agent_run_recap_prompt_payload(cls, detail: Dict[str, Any]) -> Dict[str, Any]:
        diagnostics = detail.get("diagnostics") if isinstance(detail.get("diagnostics"), dict) else {}
        agent_summary = diagnostics.get("agent_summary") if isinstance(diagnostics.get("agent_summary"), dict) else {}
        agent_plan = diagnostics.get("agent_plan") if isinstance(diagnostics.get("agent_plan"), dict) else {}
        data_quality = diagnostics.get("data_quality") if isinstance(diagnostics.get("data_quality"), dict) else {}
        decisions = [item for item in list(detail.get("decisions") or []) if isinstance(item, dict)]
        trade_plans = [item for item in list(detail.get("trade_plans") or []) if isinstance(item, dict)]
        return {
            "run": {
                "run_uid": detail.get("run_uid"),
                "status": detail.get("status"),
                "strategy": detail.get("strategy"),
                "market": detail.get("market"),
                "candidate_count": detail.get("candidate_count"),
                "planned_count": detail.get("planned_count"),
                "submitted_count": detail.get("submitted_count"),
                "skipped_count": detail.get("skipped_count"),
                "error": detail.get("error"),
            },
            "agent_plan": {
                "plan_profile": agent_plan.get("plan_profile") or {},
                "execution_policy": agent_plan.get("execution_policy") or {},
                "sizing_plan": agent_plan.get("sizing_plan") or {},
                "adaptive_controls": agent_plan.get("adaptive_controls") or {},
                "llm_dynamic_plan": agent_plan.get("llm_dynamic_plan") or {},
            },
            "agent_summary": agent_summary,
            "data_quality": data_quality,
            "decisions": [cls._recap_decision_payload(item) for item in decisions[:30]],
            "trade_plans": [cls._recap_trade_plan_payload(item) for item in trade_plans[:30]],
        }

    @staticmethod
    def _agent_run_recap_prompts(payload: Dict[str, Any]) -> Tuple[str, str]:
        system_prompt = (
            "你是一个自动选股模拟交易审计复盘助手。只基于用户给定的 JSON 审计数据回答，"
            "不要编造行情、新闻、成交或实盘建议。输出中文 Markdown，包含：本轮结论、"
            "成交/跳过主因、风控与数据质量、下一步改进建议。"
        )
        prompt = (
            "请复盘下面这次自动选股 Agent run。重点说明系统为什么成交、计划或跳过，"
            "哪些数据质量/风控/复核信号值得关注，以及下一轮应如何调参或排障。\n\n"
            "```json\n"
            f"{json.dumps(payload, ensure_ascii=False, default=str, indent=2)}\n"
            "```"
        )
        return system_prompt, prompt

    def _generate_llm_dynamic_agent_plan(
        self,
        *,
        settings: VnpyPaperSettings,
        run_uid: str,
        execution_mode_override: Optional[str],
        ignore_auto_trade_enabled: bool,
        recent_run_context: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        if not settings.auto_llm_plan_enabled or not settings.auto_trade_enabled:
            return None

        plan: Dict[str, Any] = {
            "schema_version": 1,
            "status": "running",
            "planner": "llm_dynamic_planner_v1",
            "plan_source": "litellm",
            "prompt_version": LLM_DYNAMIC_AGENT_PLAN_PROMPT_VERSION,
            "evaluator_version": LLM_DYNAMIC_AGENT_PLAN_EVALUATOR_VERSION,
            "created_at": _utc_now_iso(),
            "run_uid": run_uid,
            "fail_policy": "fallback_to_configured_settings",
            "configured": {
                "strategy": settings.auto_strategy,
                "market": settings.auto_market,
                "max_results": settings.auto_max_results,
                "cash_per_order": settings.auto_cash_per_order,
                "min_score": settings.auto_min_score,
                "execution_mode": settings.auto_execution_mode,
                "execution_mode_override": execution_mode_override,
                "ignore_auto_trade_enabled": bool(ignore_auto_trade_enabled),
            },
            "applied_overrides": {},
        }
        try:
            config = get_config()
            strategy_options, strategy_warnings = self._available_alphasift_strategy_options(config)
            prompt_payload = self._llm_dynamic_agent_plan_prompt_payload(
                settings=settings,
                run_uid=run_uid,
                strategy_options=strategy_options,
                strategy_warnings=strategy_warnings,
                recent_run_context=recent_run_context,
            )
            system_prompt, prompt = self._llm_dynamic_agent_plan_prompts(prompt_payload)

            from src.analyzer import GeminiAnalyzer

            analyzer = GeminiAnalyzer()
            if not analyzer.is_available():
                raise RuntimeError("llm_unavailable")
            text, model, usage = analyzer._call_litellm(
                prompt,
                {"temperature": 0.1, "max_output_tokens": 900},
                system_prompt=system_prompt,
                audit_context={
                    "call_type": "vnpy_paper_dynamic_agent_plan",
                    "run_uid": run_uid,
                    "strategy": settings.auto_strategy,
                    "market": settings.auto_market,
                    "prompt_version": LLM_DYNAMIC_AGENT_PLAN_PROMPT_VERSION,
                    "evaluator_version": LLM_DYNAMIC_AGENT_PLAN_EVALUATOR_VERSION,
                },
            )
            parsed = self._extract_llm_json_object(str(text or ""), error_prefix="llm_plan")
            normalized = self._normalize_llm_dynamic_agent_plan(
                parsed,
                settings=settings,
                strategy_options=strategy_options,
                strategy_warnings=strategy_warnings,
            )
            plan.update(
                {
                    "status": "accepted",
                    "model": model,
                    "usage": usage or {},
                    "completed_at": _utc_now_iso(),
                    "strategy_options_count": len(strategy_options),
                    "recommendation": normalized["recommendation"],
                    "applied_overrides": normalized["applied_overrides"],
                    "warnings": normalized["warnings"],
                    "raw_response_excerpt": str(text or "").strip()[:1000],
                }
            )
        except Exception as exc:  # noqa: BLE001 - dynamic planning must not break the configured path.
            reason = str(exc or "llm_dynamic_plan_failed").strip()[:200] or "llm_dynamic_plan_failed"
            plan.update(
                {
                    "status": "failed",
                    "reason": reason,
                    "summary": f"LLM dynamic plan failed; using configured settings: {reason}",
                    "completed_at": _utc_now_iso(),
                    "applied_overrides": {},
                    "warnings": [reason],
                }
            )
        return plan

    @staticmethod
    def _apply_llm_dynamic_agent_plan(
        settings: VnpyPaperSettings,
        plan: Optional[Dict[str, Any]],
    ) -> VnpyPaperSettings:
        if not isinstance(plan, dict) or str(plan.get("status") or "") != "accepted":
            return settings
        raw_overrides = plan.get("applied_overrides")
        if not isinstance(raw_overrides, dict) or not raw_overrides:
            return settings
        allowed = {
            "auto_strategy",
            "auto_market",
            "auto_max_results",
            "auto_cash_per_order",
            "auto_min_score",
        }
        overrides = {key: value for key, value in raw_overrides.items() if key in allowed}
        if not overrides:
            return settings
        return replace(settings, **overrides)

    def _available_alphasift_strategy_options(self, config: Any) -> Tuple[List[Dict[str, Any]], List[str]]:
        warnings: List[str] = []
        strategies: List[Dict[str, Any]] = []
        try:
            payload = AlphaSiftService(config=config).strategies()
            raw_items = payload.get("strategies") if isinstance(payload, dict) else []
            if isinstance(raw_items, list):
                for item in raw_items:
                    if not isinstance(item, dict):
                        continue
                    strategy_id = str(item.get("id") or "").strip()
                    if not strategy_id:
                        continue
                    market_scope = item.get("market_scope") or item.get("marketScope") or []
                    strategies.append(
                        {
                            "id": strategy_id,
                            "name": str(item.get("name") or item.get("title") or strategy_id),
                            "category": str(item.get("category") or item.get("tag") or ""),
                            "description": str(item.get("description") or "")[:300],
                            "market_scope": market_scope if isinstance(market_scope, list) else [str(market_scope)],
                        }
                    )
        except Exception as exc:  # noqa: BLE001 - strategy list is best-effort for planning.
            warnings.append(f"strategy_list_unavailable:{str(exc)[:120]}")

        seen = {str(item.get("id") or "") for item in strategies}
        for strategy_id in ALPHASIFT_FALLBACK_STRATEGIES:
            if strategy_id not in seen:
                strategies.append({"id": strategy_id, "name": strategy_id, "category": "fallback", "description": ""})
        return strategies, warnings

    def _llm_dynamic_agent_plan_prompt_payload(
        self,
        *,
        settings: VnpyPaperSettings,
        run_uid: str,
        strategy_options: List[Dict[str, Any]],
        strategy_warnings: List[str],
        recent_run_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        context = recent_run_context or self._recent_agent_run_context(settings)
        return {
            "run": {
                "run_uid": run_uid,
                "objective": "choose_this_run_stock_selection_plan",
                "prompt_version": LLM_DYNAMIC_AGENT_PLAN_PROMPT_VERSION,
                "evaluator_version": LLM_DYNAMIC_AGENT_PLAN_EVALUATOR_VERSION,
            },
            "configured_settings": {
                "strategy": settings.auto_strategy,
                "market": settings.auto_market,
                "max_results": settings.auto_max_results,
                "cash_per_order": settings.auto_cash_per_order,
                "min_score": settings.auto_min_score,
                "execution_mode": settings.auto_execution_mode,
                "daily_budget": settings.auto_daily_budget,
                "daily_max_orders": settings.auto_daily_max_orders,
                "max_positions": settings.auto_max_positions,
                "skip_existing_positions": settings.auto_skip_existing_positions,
            },
            "allowed_changes": {
                "strategy": "may choose one id from strategy_options",
                "market": "must keep the configured market for this version",
                "max_results": f"integer 1..{settings.auto_max_results}; do not increase",
                "cash_per_order": f"positive number <= {settings.auto_cash_per_order}; do not increase",
                "min_score": "may keep empty or tighten; do not loosen a configured threshold",
            },
            "strategy_options": strategy_options[:20],
            "strategy_warnings": strategy_warnings,
            "recent_run_context": context,
            "output_contract": {
                "schema_version": 1,
                "prompt_version": LLM_DYNAMIC_AGENT_PLAN_PROMPT_VERSION,
                "evaluator_version": LLM_DYNAMIC_AGENT_PLAN_EVALUATOR_VERSION,
                "required_json_fields": [
                    "strategy",
                    "market",
                    "max_results",
                    "cash_per_order",
                    "min_score",
                    "risk_level",
                    "rationale",
                    "checks",
                ],
                "risk_level_values": ["baseline", "guarded", "strict"],
            },
        }

    @staticmethod
    def _llm_dynamic_agent_plan_prompts(payload: Dict[str, Any]) -> Tuple[str, str]:
        system_prompt = (
            "You are a planner for an automated stock-selection paper-trading Agent. "
            "Use only the JSON payload from the user. Pick a conservative plan for this run. "
            "Return exactly one JSON object with fields: strategy, market, max_results, "
            "cash_per_order, min_score, risk_level, rationale, checks. Only choose strategy ids "
            "from strategy_options. Do not increase max_results or cash_per_order. Keep the "
            "configured market in this version."
        )
        prompt = (
            "Create a dynamic plan for this automated paper-trading run. "
            "Return strict JSON only, no Markdown.\n"
            f"{json.dumps(payload, ensure_ascii=False, default=str)}"
        )
        return system_prompt, prompt

    @staticmethod
    def _normalize_llm_dynamic_agent_plan(
        parsed: Dict[str, Any],
        *,
        settings: VnpyPaperSettings,
        strategy_options: List[Dict[str, Any]],
        strategy_warnings: List[str],
    ) -> Dict[str, Any]:
        warnings = list(strategy_warnings or [])
        strategy_ids = {str(item.get("id") or "").strip() for item in strategy_options if item.get("id")}
        strategy = str(parsed.get("strategy") or parsed.get("selected_strategy") or "").strip()
        if not strategy:
            strategy = settings.auto_strategy
            warnings.append("missing_strategy")
        elif strategy not in strategy_ids:
            warnings.append(f"invalid_strategy:{strategy[:64]}")
            strategy = settings.auto_strategy

        market = str(parsed.get("market") or settings.auto_market or "cn").strip().lower()
        if market not in ALLOWED_AUTO_MARKETS:
            warnings.append(f"invalid_market:{market[:16]}")
            market = settings.auto_market
        if market != settings.auto_market:
            warnings.append("market_override_ignored")
            market = settings.auto_market

        max_results = _safe_int(parsed.get("max_results") or parsed.get("candidate_limit"))
        if max_results is None:
            max_results = settings.auto_max_results
        if max_results > settings.auto_max_results:
            warnings.append("max_results_capped_to_configured")
            max_results = settings.auto_max_results
        max_results = max(1, min(50, max_results))

        cash_per_order = _safe_float(parsed.get("cash_per_order") or parsed.get("cash_amount_per_order"))
        if cash_per_order is None or cash_per_order <= 0:
            cash_per_order = settings.auto_cash_per_order
        if cash_per_order > settings.auto_cash_per_order:
            warnings.append("cash_per_order_capped_to_configured")
            cash_per_order = settings.auto_cash_per_order

        min_score = _safe_float(parsed.get("min_score"))
        if min_score is not None:
            min_score = max(0.0, min(1000.0, min_score))
            if settings.auto_min_score is not None and min_score < settings.auto_min_score:
                warnings.append("min_score_loosen_ignored")
                min_score = settings.auto_min_score
        else:
            min_score = settings.auto_min_score

        risk_level = str(parsed.get("risk_level") or "guarded").strip().lower()
        if risk_level not in {"baseline", "guarded", "strict"}:
            warnings.append(f"invalid_risk_level:{risk_level[:32]}")
            risk_level = "guarded"
        checks = parsed.get("checks") if isinstance(parsed.get("checks"), list) else []
        recommendation = {
            "strategy": strategy,
            "market": market,
            "max_results": max_results,
            "cash_per_order": round(float(cash_per_order), 6),
            "min_score": min_score,
            "risk_level": risk_level,
            "rationale": str(parsed.get("rationale") or parsed.get("summary") or "").strip()[:800],
            "checks": checks[:10],
        }
        applied_overrides: Dict[str, Any] = {}
        if strategy != settings.auto_strategy:
            applied_overrides["auto_strategy"] = strategy
        if market != settings.auto_market:
            applied_overrides["auto_market"] = market
        if int(max_results) != int(settings.auto_max_results):
            applied_overrides["auto_max_results"] = int(max_results)
        if abs(float(cash_per_order) - float(settings.auto_cash_per_order)) > PAPER_EPS:
            applied_overrides["auto_cash_per_order"] = float(cash_per_order)
        if min_score != settings.auto_min_score:
            applied_overrides["auto_min_score"] = min_score
        return {
            "recommendation": recommendation,
            "applied_overrides": applied_overrides,
            "warnings": warnings,
        }

    def _candidate_llm_pre_trade_review(
        self,
        *,
        candidate: Dict[str, Any],
        settings: VnpyPaperSettings,
        symbol: str,
        run_uid: str,
        sequence: int,
    ) -> Optional[Dict[str, Any]]:
        if not settings.auto_llm_review_enabled:
            return None

        review: Dict[str, Any] = {
            "schema_version": 1,
            "status": "running",
            "reviewer": "llm_reviewer_v1",
            "review_source": "litellm",
            "prompt_version": LLM_PRE_TRADE_REVIEW_PROMPT_VERSION,
            "evaluator_version": LLM_PRE_TRADE_REVIEW_EVALUATOR_VERSION,
            "reviewed_at": _utc_now_iso(),
            "symbol": symbol,
            "side": "buy",
            "run_uid": run_uid,
            "sequence": sequence,
            "block_trade": True,
        }
        prompt_payload = self._candidate_llm_review_prompt_payload(
            candidate=candidate,
            settings=settings,
            symbol=symbol,
            run_uid=run_uid,
            sequence=sequence,
        )
        system_prompt, prompt = self._candidate_llm_review_prompts(prompt_payload)
        try:
            from src.analyzer import GeminiAnalyzer

            analyzer = GeminiAnalyzer()
            if not analyzer.is_available():
                raise RuntimeError("llm_unavailable")
            text, model, usage = analyzer._call_litellm(
                prompt,
                {"temperature": 0.1, "max_output_tokens": 700},
                system_prompt=system_prompt,
                audit_context={
                    "call_type": "vnpy_paper_pre_trade_llm_review",
                    "run_uid": run_uid,
                    "symbol": symbol,
                    "sequence": sequence,
                    "prompt_version": LLM_PRE_TRADE_REVIEW_PROMPT_VERSION,
                    "evaluator_version": LLM_PRE_TRADE_REVIEW_EVALUATOR_VERSION,
                },
            )
            parsed = self._extract_llm_json_object(str(text or ""))
            status = str(parsed.get("status") or "warning").strip().lower()
            if status not in {"passed", "warning", "blocked"}:
                status = "warning"
            reason = str(parsed.get("reason") or "").strip()[:160] or None
            checks = parsed.get("checks") if isinstance(parsed.get("checks"), list) else []
            review.update(
                {
                    "status": status,
                    "reason": reason,
                    "summary": str(parsed.get("summary") or status).strip()[:600],
                    "checks": checks[:10],
                    "model": model,
                    "usage": usage or {},
                    "completed_at": _utc_now_iso(),
                    "block_trade": status == "blocked",
                    "raw_response_excerpt": str(text or "").strip()[:1000],
                }
            )
        except Exception as exc:  # noqa: BLE001 - enabled LLM review must fail closed per candidate.
            reason = str(exc or "llm_review_failed").strip()[:160] or "llm_review_failed"
            review.update(
                {
                    "status": "failed",
                    "reason": reason,
                    "summary": f"LLM pre-trade review failed: {reason}",
                    "checks": [
                        {
                            "key": "llm_review_call",
                            "status": "failed",
                            "reason": reason,
                        }
                    ],
                    "completed_at": _utc_now_iso(),
                    "block_trade": True,
                    "error": reason,
                }
            )
        return review

    def _candidate_llm_review_prompt_payload(
        self,
        *,
        candidate: Dict[str, Any],
        settings: VnpyPaperSettings,
        symbol: str,
        run_uid: str,
        sequence: int,
    ) -> Dict[str, Any]:
        return {
            "review_metadata": {
                "schema_version": 1,
                "prompt_version": LLM_PRE_TRADE_REVIEW_PROMPT_VERSION,
                "evaluator_version": LLM_PRE_TRADE_REVIEW_EVALUATOR_VERSION,
            },
            "run": {
                "run_uid": run_uid,
                "sequence": sequence,
                "strategy": settings.auto_strategy,
                "market": settings.auto_market,
                "execution_mode": settings.auto_execution_mode,
            },
            "candidate": {
                "symbol": symbol,
                "name": self._candidate_name(candidate),
                "score": self._candidate_score(candidate),
                "confidence": self._candidate_confidence(candidate),
                "price": _safe_float(candidate.get("price")),
                "rationale": self._candidate_rationale(candidate),
                "data_quality": candidate.get("data_quality") or candidate.get("quality_status"),
                "missing_fields": self._candidate_quality_text_list(candidate.get("missing_fields")),
                "data_sources": self._candidate_quality_text_list(candidate.get("data_sources")),
                "limit_status": candidate.get("limit_status"),
                "is_suspended": candidate.get("is_suspended"),
                "turnover_amount": (
                    _safe_float(candidate.get("turnover_amount"))
                    or _safe_float(candidate.get("amount"))
                    or _safe_float(candidate.get("turnover"))
                ),
                "industry": (
                    candidate.get("industry")
                    or candidate.get("sector")
                    or candidate.get("concept")
                ),
            },
            "planned_order": {
                "side": "buy",
                "cash_amount": settings.auto_cash_per_order,
                "min_score": settings.auto_min_score,
                "skip_existing_positions": settings.auto_skip_existing_positions,
                "time_gate_enabled": settings.auto_trade_time_gate_enabled,
            },
            "review_contract": {
                "allowed_status": ["passed", "warning", "blocked"],
                "blocked_means": "do_not_create_or_submit_trade_plan_for_this_candidate",
                "warning_means": "record_audit_but_allow_existing_rule_path_to_continue",
            },
        }

    @staticmethod
    def _candidate_llm_review_prompts(payload: Dict[str, Any]) -> Tuple[str, str]:
        system_prompt = (
            "You are a pre-trade reviewer for an automated stock-selection Agent. "
            "Use only the JSON audit payload from the user. Do not invent news, quotes, "
            "fills, or live-trading advice. Return exactly one JSON object with fields: "
            "status (passed/warning/blocked), reason, summary, checks. Use blocked only "
            "when the candidate should not continue to a buy plan; use warning for "
            "auditable data gaps that do not require blocking."
        )
        prompt = (
            "Review whether this candidate can continue to a simulated buy plan. "
            "Return strict JSON only, no Markdown.\n"
            f"{json.dumps(payload, ensure_ascii=False, default=str)}"
        )
        return system_prompt, prompt

    @staticmethod
    def _extract_llm_json_object(text: str, *, error_prefix: str = "llm_review") -> Dict[str, Any]:
        cleaned = str(text or "").strip()
        if cleaned.startswith("```"):
            lines = cleaned.splitlines()
            if lines and lines[0].lstrip().startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            cleaned = "\n".join(lines).strip()
        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError:
            start = cleaned.find("{")
            end = cleaned.rfind("}")
            if start < 0 or end <= start:
                raise ValueError(f"{error_prefix}_invalid_json")
            parsed = json.loads(cleaned[start : end + 1])
        if not isinstance(parsed, dict):
            raise ValueError(f"{error_prefix}_json_not_object")
        return parsed

    @staticmethod
    def _llm_review_blocks_trade(review: Dict[str, Any]) -> bool:
        status = str(review.get("status") or "").strip().lower()
        return bool(review.get("block_trade")) or status in {"blocked", "failed"}

    @staticmethod
    def _llm_review_block_reason(review: Dict[str, Any]) -> str:
        reason = str(review.get("reason") or "").strip().lower()
        reason = "".join(ch if ch.isascii() and ch.isalnum() else "_" for ch in reason)
        reason = "_".join(part for part in reason.split("_") if part)
        if reason and not reason.startswith("llm_review"):
            return f"llm_review_{reason[:96]}"
        return reason[:120] or "llm_review_blocked"

    @classmethod
    def _recap_decision_payload(cls, item: Dict[str, Any]) -> Dict[str, Any]:
        order_result = item.get("order_result") if isinstance(item.get("order_result"), dict) else {}
        risk_review = order_result.get("risk_review") if isinstance(order_result.get("risk_review"), dict) else {}
        agent_review = order_result.get("agent_review") if isinstance(order_result.get("agent_review"), dict) else {}
        return {
            "symbol": item.get("symbol"),
            "name": item.get("name"),
            "action": item.get("action"),
            "status": item.get("status"),
            "reason": item.get("reason"),
            "score": item.get("score"),
            "confidence": item.get("confidence"),
            "cash_amount": item.get("cash_amount"),
            "quantity": item.get("quantity"),
            "price": item.get("price"),
            "risk_flags": item.get("risk_flags") or [],
            "rationale": item.get("rationale"),
            "risk_review": {
                "status": risk_review.get("status"),
                "reason": risk_review.get("reason"),
                "risk_flags": risk_review.get("risk_flags") or [],
                "candidate_data_quality": risk_review.get("candidate_data_quality") or {},
            },
            "agent_review": {
                "status": agent_review.get("status"),
                "reason": agent_review.get("reason"),
                "summary": agent_review.get("summary"),
            },
        }

    @staticmethod
    def _recap_trade_plan_payload(item: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "symbol": item.get("symbol"),
            "name": item.get("name"),
            "side": item.get("side"),
            "status": item.get("status"),
            "execution_mode": item.get("execution_mode"),
            "planned_cash_amount": item.get("planned_cash_amount"),
            "planned_quantity": item.get("planned_quantity"),
            "planned_price": item.get("planned_price"),
            "submitted_quantity": item.get("submitted_quantity"),
            "submitted_price": item.get("submitted_price"),
            "trade_id": item.get("trade_id"),
            "skip_reason": item.get("skip_reason"),
            "risk_flags": item.get("risk_flags") or [],
        }

    def _trading_window_diagnostics(self, settings: VnpyPaperSettings) -> Dict[str, Any]:
        enforced = bool(settings.auto_trade_time_gate_enabled) and settings.auto_execution_mode not in {
            "dry_run",
            "manual_approval",
        }
        try:
            diagnostics = trading_calendar.build_next_trading_window_context(
                market=settings.auto_market,
                trigger_source="vnpy_paper_auto",
                analysis_intent="auto",
            )
        except Exception as exc:  # noqa: BLE001 - status diagnostics must stay readable.
            logger.warning("Failed to resolve vn.py paper trading window: %s", exc)
            diagnostics = {
                "available": False,
                "market": settings.auto_market,
                "next_window_status": "unknown",
                "reason": "calendar_error",
                "error": str(exc),
            }

        diagnostics["time_gate_enabled"] = bool(settings.auto_trade_time_gate_enabled)
        diagnostics["time_gate_enforced"] = enforced
        diagnostics["execution_mode"] = settings.auto_execution_mode
        if not settings.auto_trade_time_gate_enabled:
            diagnostics["gate_reason"] = "time_gate_disabled"
        elif not enforced:
            diagnostics["gate_reason"] = "execution_mode_not_gated"
        else:
            diagnostics["gate_reason"] = None
        return diagnostics

    def _failure_fuse_reason(
        self,
        settings: VnpyPaperSettings,
        *,
        current_run_id: int,
    ) -> Tuple[Optional[str], Dict[str, Any]]:
        if not settings.auto_failure_fuse_enabled:
            return None, {}
        threshold = max(2, min(20, int(settings.auto_failure_fuse_threshold or 3)))
        recent = self.agent_repo.list_recent_runs(
            trigger_source="vnpy_paper_auto",
            strategy=settings.auto_strategy,
            market=settings.auto_market,
            limit=max(threshold, 20),
            before_run_id=current_run_id,
        )
        reset_at = self._failure_fuse_reset_at()
        recent = self._filter_runs_after_failure_fuse_reset(recent, reset_at)[:threshold]
        diagnostics = {
            "enabled": True,
            "threshold": threshold,
            "reset_at": self._format_utc_datetime(reset_at) if reset_at is not None else None,
            "recent_run_uids": [item.get("run_uid") for item in recent],
            "recent_statuses": [item.get("status") for item in recent],
            "recent_errors": [item.get("error") for item in recent],
        }
        if len(recent) < threshold:
            return None, diagnostics
        if all(self._run_counts_for_failure_fuse(item) for item in recent[:threshold]):
            return "failure_fuse_open", diagnostics
        return None, diagnostics

    def _failure_fuse_status(self, settings: VnpyPaperSettings) -> Dict[str, Any]:
        if not settings.auto_failure_fuse_enabled:
            return {
                "enabled": False,
                "open": False,
                "threshold": max(2, min(20, int(settings.auto_failure_fuse_threshold or 3))),
                "consecutive_failure_count": 0,
            }
        threshold = max(2, min(20, int(settings.auto_failure_fuse_threshold or 3)))
        recent = self.agent_repo.list_recent_runs(
            trigger_source="vnpy_paper_auto",
            strategy=settings.auto_strategy,
            market=settings.auto_market,
            limit=max(threshold, 20),
        )
        reset_at = self._failure_fuse_reset_at()
        recent = self._filter_runs_after_failure_fuse_reset(recent, reset_at)[:threshold]
        consecutive = 0
        for item in recent:
            if not self._run_counts_for_failure_fuse(item):
                break
            consecutive += 1
        is_open = len(recent) >= threshold and consecutive >= threshold
        return {
            "enabled": True,
            "open": is_open,
            "threshold": threshold,
            "reset_at": self._format_utc_datetime(reset_at) if reset_at is not None else None,
            "consecutive_failure_count": consecutive,
            "recent_run_uids": [item.get("run_uid") for item in recent],
            "recent_statuses": [item.get("status") for item in recent],
            "recent_errors": [item.get("error") for item in recent],
        }

    def _failure_fuse_reset_at(self) -> Optional[datetime]:
        payload = self._read_config_payload()
        if not isinstance(payload, dict):
            return None
        return self._parse_utc_datetime(payload.get("failure_fuse_reset_at"))

    @classmethod
    def _filter_runs_after_failure_fuse_reset(
        cls,
        runs: List[Dict[str, Any]],
        reset_at: Optional[datetime],
    ) -> List[Dict[str, Any]]:
        if reset_at is None:
            return list(runs)
        reset_local = cls._coerce_local_naive_datetime(reset_at)
        if reset_local is None:
            return list(runs)
        filtered: List[Dict[str, Any]] = []
        for item in runs:
            run_at = cls._coerce_local_naive_datetime(
                item.get("started_at")
                or item.get("completed_at")
                or item.get("created_at")
                or item.get("updated_at")
            )
            if run_at is None or run_at >= reset_local:
                filtered.append(item)
        return filtered

    @classmethod
    def _coerce_local_naive_datetime(cls, value: Any) -> Optional[datetime]:
        if isinstance(value, datetime):
            if value.tzinfo is None:
                return value
            return value.astimezone().replace(tzinfo=None)
        parsed = cls._parse_utc_datetime(value)
        if parsed is None:
            return None
        return parsed.astimezone().replace(tzinfo=None)

    @staticmethod
    def _run_counts_for_failure_fuse(run: Dict[str, Any]) -> bool:
        status = str(run.get("status") or "").strip().lower()
        if status == "failed":
            return True
        diagnostics = run.get("diagnostics") if isinstance(run.get("diagnostics"), dict) else {}
        data_quality = diagnostics.get("data_quality") if isinstance(diagnostics, dict) else None
        if isinstance(data_quality, dict):
            quality_status = str(data_quality.get("status") or "").strip().lower()
            if quality_status in {"stale", "unavailable"}:
                return True

        error = str(run.get("error") or "").strip()
        if not error:
            return False
        benign_errors = {
            "auto_trade_disabled",
            "non_trading_day",
            "outside_trading_session",
            "market_phase_unknown",
            "failure_fuse_open",
        }
        return error not in benign_errors

    def _held_symbols(self) -> set[str]:
        settings = self.get_settings()
        account = self.ensure_account(settings=settings)
        try:
            snapshot = self.portfolio.get_portfolio_snapshot(account_id=int(account["id"]))
        except Exception:
            return set()
        accounts = snapshot.get("accounts") or []
        if not accounts:
            return set()
        return {
            self._normalize_symbol(item.get("symbol") or "")
            for item in accounts[0].get("positions", [])
            if _safe_float(item.get("quantity")) and float(item.get("quantity")) > 0
        }

    def _active_vnpy_trade_plan(
        self,
        *,
        symbol: str,
        side: str,
        settings: VnpyPaperSettings,
    ) -> Optional[Dict[str, Any]]:
        if settings.auto_execution_mode != "vnpy_paper":
            return None
        return self.agent_repo.find_active_trade_plan(
            symbol=self._normalize_symbol(symbol),
            side=side,
            execution_mode="vnpy_paper",
            statuses=sorted(ACTIVE_VNPY_TRADE_PLAN_STATUSES),
        )

    @staticmethod
    def _trade_plan_reference(plan: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "plan_uid": plan.get("plan_uid"),
            "run_uid": plan.get("run_uid"),
            "status": plan.get("status"),
            "side": plan.get("side"),
            "execution_mode": plan.get("execution_mode"),
            "submitted_quantity": plan.get("submitted_quantity"),
            "submitted_price": plan.get("submitted_price"),
            "updated_at": plan.get("updated_at"),
        }

    @classmethod
    def _trade_plan_recovery_item(cls, plan: Dict[str, Any]) -> Dict[str, Any]:
        now = datetime.now(timezone.utc)
        status = str(plan.get("status") or "").strip().lower()
        execution_mode = str(plan.get("execution_mode") or "").strip()
        order_result = plan.get("order_result") if isinstance(plan.get("order_result"), dict) else {}
        reason = (
            str(plan.get("skip_reason") or "").strip()
            or str(order_result.get("reason") or "").strip()
            or None
        )
        updated_at = cls._parse_db_datetime(plan.get("updated_at") or plan.get("created_at"))
        age_seconds = (
            max(0, int((now - updated_at).total_seconds()))
            if updated_at is not None
            else None
        )
        is_active = status in ACTIVE_VNPY_TRADE_PLAN_STATUSES
        stale_active = bool(
            is_active
            and age_seconds is not None
            and age_seconds >= TRADE_PLAN_ORDER_TIMEOUT_SECONDS
        )
        stale_reason = None
        if stale_active:
            if status == "cancel_requested":
                stale_reason = "vnpy_cancel_timeout_candidate"
            elif status == "part_filled":
                stale_reason = "vnpy_partial_fill_stale_candidate"
            else:
                stale_reason = "vnpy_order_timeout_candidate"

        retry_payload = cls._trade_plan_retry_payload(plan)
        max_attempts = _safe_int(retry_payload.get("max_attempts")) or TRADE_PLAN_RETRY_MAX_ATTEMPTS
        max_attempts = max(1, min(20, int(max_attempts)))
        attempt_count = _safe_int(retry_payload.get("attempt_count")) or 0
        next_retry_after = cls._parse_utc_datetime(retry_payload.get("next_retry_after"))
        next_retry_after_text = (
            cls._format_utc_datetime(next_retry_after)
            if next_retry_after is not None
            else None
        )
        retry_status_allowed = status in {"failed", "skipped"}
        retry_mode_allowed = execution_mode in RETRYABLE_TRADE_PLAN_EXECUTION_MODES
        reason_retry_allowed = True
        if status == "skipped" and str(reason or "") not in RETRYABLE_TRADE_PLAN_SKIP_REASONS:
            reason_retry_allowed = False
        if status == "failed" and str(reason or "") in NON_RETRYABLE_TRADE_PLAN_FAILURE_REASONS:
            reason_retry_allowed = False

        retryable = bool(retry_status_allowed and retry_mode_allowed and reason_retry_allowed)
        retry_due = False
        retry_block_reason = None
        recovery_state = "not_retryable"
        if stale_active:
            recovery_state = "stale_active"
        elif is_active:
            recovery_state = "active_waiting"
        elif status == "planned":
            recovery_state = "manual_approval_pending" if execution_mode == "manual_approval" else "planned"
        elif status == "filled":
            recovery_state = "terminal"
        elif not retry_status_allowed:
            retry_block_reason = "trade_plan_not_retryable_status"
        elif not retry_mode_allowed:
            retry_block_reason = "trade_plan_not_retryable_execution_mode"
        elif not reason_retry_allowed:
            retry_block_reason = "trade_plan_not_retryable_reason"
        elif attempt_count >= max_attempts:
            retry_block_reason = "trade_plan_retry_limit_reached"
            recovery_state = "retry_limit_reached"
        elif next_retry_after is not None and now < next_retry_after:
            retry_block_reason = "trade_plan_retry_cooldown_active"
            recovery_state = "retry_cooldown"
            retryable = True
        else:
            recovery_state = "retry_due"
            retryable = True
            retry_due = True

        return {
            "plan_uid": plan.get("plan_uid"),
            "run_id": plan.get("run_id"),
            "decision_id": plan.get("decision_id"),
            "symbol": plan.get("symbol"),
            "name": plan.get("name"),
            "market": plan.get("market"),
            "side": plan.get("side"),
            "status": status or plan.get("status"),
            "execution_mode": execution_mode,
            "skip_reason": reason,
            "planned_cash_amount": plan.get("planned_cash_amount"),
            "planned_quantity": plan.get("planned_quantity"),
            "planned_price": plan.get("planned_price"),
            "submitted_quantity": plan.get("submitted_quantity"),
            "submitted_price": plan.get("submitted_price"),
            "trade_id": plan.get("trade_id"),
            "created_at": plan.get("created_at"),
            "updated_at": plan.get("updated_at"),
            "age_seconds": age_seconds,
            "age_minutes": round(age_seconds / 60.0, 2) if age_seconds is not None else None,
            "is_active": is_active,
            "stale_active": stale_active,
            "stale_reason": stale_reason,
            "cancellable": execution_mode == "vnpy_paper" and status in {"submitted", "part_filled"},
            "retryable": retryable,
            "retry_due": retry_due,
            "retry_block_reason": retry_block_reason,
            "recovery_state": recovery_state,
            "retry": {
                "attempt_count": attempt_count,
                "max_attempts": max_attempts,
                "next_retry_after": next_retry_after_text,
                "cooldown_seconds": TRADE_PLAN_RETRY_COOLDOWN_SECONDS,
            },
        }

    @staticmethod
    def _position_exposure_configured(settings: VnpyPaperSettings) -> bool:
        return (
            settings.auto_max_single_position_value is not None
            or settings.auto_max_total_position_value is not None
            or settings.auto_max_total_position_pct is not None
            or settings.auto_max_industry_position_value is not None
            or settings.auto_max_industry_position_pct is not None
            or bool(settings.auto_target_position_weights)
            or bool(settings.auto_target_industry_weights)
        )

    @staticmethod
    def _industry_exposure_configured(settings: VnpyPaperSettings) -> bool:
        return (
            settings.auto_max_industry_position_value is not None
            or settings.auto_max_industry_position_pct is not None
        )

    def _position_exposure_state(self, settings: VnpyPaperSettings) -> Dict[str, Any]:
        industry_configured = (
            self._industry_exposure_configured(settings)
            or bool(settings.auto_target_industry_weights)
        )
        needs_snapshot = (
            settings.auto_skip_existing_positions
            or settings.auto_max_positions > 0
            or self._position_exposure_configured(settings)
        )
        state: Dict[str, Any] = {
            "available": True,
            "held_symbols": set(),
            "position_values": {},
            "industry_values": {},
            "industry_available": True,
            "total_market_value": 0.0,
            "total_equity": None,
            "base_currency": None,
            "fx_stale": False,
        }
        if not needs_snapshot:
            return state

        try:
            account = self.ensure_account(settings=settings)
            snapshot = self.portfolio.get_portfolio_snapshot(account_id=int(account["id"]))
        except Exception as exc:  # noqa: BLE001 - configured exposure guard should fail closed.
            logger.warning("Failed to resolve vn.py paper position exposure snapshot: %s", exc)
            state["available"] = False
            return state

        accounts = snapshot.get("accounts") or []
        account_snapshot = accounts[0] if accounts and isinstance(accounts[0], dict) else snapshot
        fx_stale = bool(account_snapshot.get("fx_stale") or snapshot.get("fx_stale"))
        if fx_stale and self._position_exposure_configured(settings):
            state.update(
                {
                    "available": False,
                    "base_currency": account_snapshot.get("base_currency"),
                    "fx_stale": True,
                }
            )
            return state
        positions = list(account_snapshot.get("positions") or [])
        position_values: Dict[str, float] = {}
        industry_values: Dict[str, float] = {}
        held_symbols: set[str] = set()
        total_from_positions = 0.0
        for position in positions:
            if not isinstance(position, dict):
                continue
            symbol = self._normalize_symbol(position.get("symbol") or "")
            quantity = _safe_float(position.get("quantity"))
            if not symbol or quantity is None or quantity <= 0:
                continue
            held_symbols.add(symbol)
            market_value = self._position_market_value(position)
            position_values[symbol] = position_values.get(symbol, 0.0) + market_value
            total_from_positions += market_value
            if industry_configured:
                industry = self._position_primary_industry(position, symbol=symbol, settings=settings)
                if industry:
                    industry_values[industry] = industry_values.get(industry, 0.0) + market_value
                else:
                    state["industry_available"] = False

        total_market_value = _safe_float(account_snapshot.get("total_market_value"))
        if total_market_value is None:
            total_market_value = _safe_float(snapshot.get("total_market_value"))
        state.update(
            {
                "held_symbols": held_symbols,
                "position_values": position_values,
                "industry_values": industry_values,
                "total_market_value": total_market_value if total_market_value is not None else total_from_positions,
                "total_equity": _safe_float(account_snapshot.get("total_equity"))
                or _safe_float(snapshot.get("total_equity")),
                "base_currency": account_snapshot.get("base_currency"),
                "fx_stale": fx_stale,
            }
        )
        return state

    def _industry_exposure_diagnostics(
        self,
        *,
        settings: VnpyPaperSettings,
        snapshot: Optional[Dict[str, Any]],
        evaluated: bool,
        requested: bool = True,
    ) -> Dict[str, Any]:
        configured = self._industry_exposure_configured(settings) or bool(
            settings.auto_target_industry_weights
        )
        result: Dict[str, Any] = {
            "evaluated": evaluated,
            "configured": configured,
            "status": (
                "snapshot_not_requested"
                if not evaluated and not requested
                else "snapshot_unavailable"
                if not evaluated
                else "no_positions"
            ),
            "position_count": 0,
            "resolved_position_count": 0,
            "missing_position_count": 0,
            "coverage_pct": None,
            "industry_count": 0,
            "industry_values": {},
            "missing_symbols": [],
            "resolution_mode": "risk_guard" if configured else "snapshot_only",
        }
        if not evaluated or not isinstance(snapshot, dict):
            return result

        accounts = snapshot.get("accounts")
        account_payloads = accounts if isinstance(accounts, list) else [snapshot]
        industry_values: Dict[str, float] = {}
        missing_symbols: List[str] = []
        position_count = 0
        resolved_count = 0
        for account in account_payloads:
            if not isinstance(account, dict):
                continue
            positions = account.get("positions")
            if not isinstance(positions, list):
                continue
            for position in positions:
                if not isinstance(position, dict):
                    continue
                symbol = self._normalize_symbol(position.get("symbol"))
                quantity = _safe_float(position.get("quantity"))
                if not symbol or quantity is None or quantity <= 0:
                    continue
                position_count += 1
                industry = self._industry_name_from_mapping(position)
                if not industry and configured:
                    industry = self._fetch_symbol_primary_industry(
                        symbol,
                        market=str(position.get("market") or settings.auto_market),
                    )
                if not industry:
                    missing_symbols.append(symbol)
                    continue
                resolved_count += 1
                market_value = self._position_market_value(position)
                industry_values[industry] = industry_values.get(industry, 0.0) + market_value

        missing_count = position_count - resolved_count
        result.update(
            {
                "status": (
                    "no_positions"
                    if position_count == 0
                    else "complete"
                    if missing_count == 0
                    else "partial"
                ),
                "position_count": position_count,
                "resolved_position_count": resolved_count,
                "missing_position_count": missing_count,
                "coverage_pct": (
                    round(resolved_count / position_count * 100.0, 2)
                    if position_count > 0
                    else None
                ),
                "industry_count": len(industry_values),
                "industry_values": dict(
                    sorted(industry_values.items(), key=lambda item: (-item[1], item[0]))
                ),
                "missing_symbols": sorted(set(missing_symbols))[:20],
            }
        )
        return result

    @staticmethod
    def _position_market_value(position: Dict[str, Any]) -> float:
        quantity = _safe_float(position.get("quantity")) or 0.0
        for key in (
            "market_value_base",
            "market_value",
            "marketValueBase",
            "position_value",
            "value",
        ):
            value = _safe_float(position.get(key))
            if value is not None and value > 0:
                return value
        price = (
            _safe_float(position.get("last_price"))
            or _safe_float(position.get("price"))
            or _safe_float(position.get("avg_cost"))
            or 0.0
        )
        return max(0.0, quantity * price)

    def _position_exposure_limit_reason(
        self,
        *,
        settings: VnpyPaperSettings,
        exposure_state: Dict[str, Any],
        symbol: str,
        candidate: Dict[str, Any],
        next_cash_amount: float,
    ) -> Optional[str]:
        target_position_weights = dict(settings.auto_target_position_weights or {})
        target_industry_weights = dict(settings.auto_target_industry_weights or {})
        if not self._position_exposure_configured(settings):
            return None
        if not exposure_state.get("available", True):
            return "position_exposure_unavailable"

        next_amount = max(0.0, float(next_cash_amount or 0.0))
        position_values = exposure_state.get("position_values")
        if not isinstance(position_values, dict):
            position_values = {}
        current_symbol_value = _safe_float(position_values.get(symbol)) or 0.0
        total_market_value = _safe_float(exposure_state.get("total_market_value")) or 0.0

        if (
            settings.auto_max_single_position_value is not None
            and current_symbol_value + next_amount > settings.auto_max_single_position_value + 1e-8
        ):
            return "single_position_value_limit_reached"
        target_position_weight = target_position_weights.get(symbol)
        if target_position_weight is not None:
            total_equity = _safe_float(exposure_state.get("total_equity"))
            if total_equity is None or total_equity <= 0:
                return "position_exposure_unavailable"
            target_value = total_equity * target_position_weight / 100.0
            if current_symbol_value + next_amount > target_value + 1e-8:
                return "target_position_weight_limit_reached"
        if (
            settings.auto_max_total_position_value is not None
            and total_market_value + next_amount > settings.auto_max_total_position_value + 1e-8
        ):
            return "total_position_value_limit_reached"
        if settings.auto_max_total_position_pct is not None:
            total_equity = _safe_float(exposure_state.get("total_equity"))
            if total_equity is None or total_equity <= 0:
                return "position_exposure_unavailable"
            next_pct = (total_market_value + next_amount) / total_equity * 100.0
            if next_pct > settings.auto_max_total_position_pct + 1e-8:
                return "total_position_pct_limit_reached"
        if self._industry_exposure_configured(settings) or bool(target_industry_weights):
            if not exposure_state.get("industry_available", True):
                return "industry_exposure_unavailable"
            industry = self._candidate_primary_industry(candidate)
            if not industry:
                industry = self._fetch_symbol_primary_industry(symbol, market=settings.auto_market)
            if not industry:
                return "industry_exposure_unavailable"
            industry_values = exposure_state.get("industry_values")
            if not isinstance(industry_values, dict):
                industry_values = {}
            current_industry_value = _safe_float(industry_values.get(industry)) or 0.0
            if (
                settings.auto_max_industry_position_value is not None
                and current_industry_value + next_amount > settings.auto_max_industry_position_value + 1e-8
            ):
                return "industry_position_value_limit_reached"
            if settings.auto_max_industry_position_pct is not None:
                total_equity = _safe_float(exposure_state.get("total_equity"))
                if total_equity is None or total_equity <= 0:
                    return "position_exposure_unavailable"
                industry_pct = (current_industry_value + next_amount) / total_equity * 100.0
                if industry_pct > settings.auto_max_industry_position_pct + 1e-8:
                    return "industry_position_pct_limit_reached"
            target_industry_weight = target_industry_weights.get(industry)
            if target_industry_weight is not None:
                total_equity = _safe_float(exposure_state.get("total_equity"))
                if total_equity is None or total_equity <= 0:
                    return "position_exposure_unavailable"
                target_value = total_equity * target_industry_weight / 100.0
                if current_industry_value + next_amount > target_value + 1e-8:
                    return "target_industry_weight_limit_reached"
        return None

    def _target_weight_buy_budget(
        self,
        *,
        settings: VnpyPaperSettings,
        exposure_state: Dict[str, Any],
        symbol: str,
        candidate: Dict[str, Any],
        configured_base_amount: float,
    ) -> Tuple[float, Dict[str, Any], Optional[str]]:
        configured_amount = max(0.0, float(configured_base_amount or 0.0))
        position_targets = dict(settings.auto_target_position_weights or {})
        industry_targets = dict(settings.auto_target_industry_weights or {})
        diagnostics: Dict[str, Any] = {
            "method": "target_weight_gap_cap",
            "configured_base_amount": round(configured_amount, 6),
            "resolved_base_amount": round(configured_amount, 6),
            "adjusted": False,
            "constraints": [],
        }
        if symbol not in position_targets and not industry_targets:
            return configured_amount, diagnostics, None
        if not exposure_state.get("available", True):
            return 0.0, diagnostics, "position_exposure_unavailable"

        total_equity = _safe_float(exposure_state.get("total_equity"))
        amount = configured_amount
        reached_reason: Optional[str] = None
        position_values = exposure_state.get("position_values")
        if not isinstance(position_values, dict):
            position_values = {}
        if symbol in position_targets:
            if total_equity is None or total_equity <= 0:
                return 0.0, diagnostics, "position_exposure_unavailable"
            target_weight = float(position_targets[symbol])
            target_value = total_equity * target_weight / 100.0
            current_value = _safe_float(position_values.get(symbol)) or 0.0
            remaining = max(0.0, target_value - current_value)
            diagnostics["constraints"].append({
                "type": "position_target",
                "key": symbol,
                "target_weight_pct": target_weight,
                "target_value": round(target_value, 6),
                "current_value": round(current_value, 6),
                "remaining_value": round(remaining, 6),
            })
            amount = min(amount, remaining)
            if remaining <= PAPER_EPS:
                reached_reason = "target_position_weight_reached"

        if industry_targets:
            if not exposure_state.get("industry_available", True):
                return 0.0, diagnostics, "industry_exposure_unavailable"
            industry = self._candidate_primary_industry(candidate)
            if not industry:
                industry = self._fetch_symbol_primary_industry(symbol, market=settings.auto_market)
            if not industry:
                return 0.0, diagnostics, "industry_exposure_unavailable"
            diagnostics["industry"] = industry
            if industry in industry_targets:
                if total_equity is None or total_equity <= 0:
                    return 0.0, diagnostics, "position_exposure_unavailable"
                industry_values = exposure_state.get("industry_values")
                if not isinstance(industry_values, dict):
                    industry_values = {}
                target_weight = float(industry_targets[industry])
                target_value = total_equity * target_weight / 100.0
                current_value = _safe_float(industry_values.get(industry)) or 0.0
                remaining = max(0.0, target_value - current_value)
                diagnostics["constraints"].append({
                    "type": "industry_target",
                    "key": industry,
                    "target_weight_pct": target_weight,
                    "target_value": round(target_value, 6),
                    "current_value": round(current_value, 6),
                    "remaining_value": round(remaining, 6),
                })
                amount = min(amount, remaining)
                if remaining <= PAPER_EPS and reached_reason is None:
                    reached_reason = "target_industry_weight_reached"

        diagnostics["resolved_base_amount"] = round(amount, 6)
        diagnostics["adjusted"] = amount + PAPER_EPS < configured_amount
        if amount <= PAPER_EPS:
            return 0.0, diagnostics, reached_reason or "target_weight_reached"
        return amount, diagnostics, None

    def _apply_planned_exposure(
        self,
        exposure_state: Dict[str, Any],
        symbol: str,
        order: Dict[str, Any],
        *,
        settings: VnpyPaperSettings,
        candidate: Dict[str, Any],
    ) -> None:
        cash_amount = self._order_base_cash_amount(order, settings=settings)
        cash_amount = max(0.0, float(cash_amount or 0.0))
        position_values = exposure_state.setdefault("position_values", {})
        if isinstance(position_values, dict):
            position_values[symbol] = (_safe_float(position_values.get(symbol)) or 0.0) + cash_amount
        exposure_state["total_market_value"] = (
            (_safe_float(exposure_state.get("total_market_value")) or 0.0) + cash_amount
        )
        industry_configured = (
            self._industry_exposure_configured(settings)
            or bool(settings.auto_target_industry_weights)
        )
        if not industry_configured or not exposure_state.get("industry_available", True):
            return
        industry = self._candidate_primary_industry(candidate)
        if not industry:
            industry = self._fetch_symbol_primary_industry(symbol, market=settings.auto_market)
        if not industry:
            exposure_state["industry_available"] = False
            return
        industry_values = exposure_state.setdefault("industry_values", {})
        if isinstance(industry_values, dict):
            industry_values[industry] = (_safe_float(industry_values.get(industry)) or 0.0) + cash_amount

    def _position_primary_industry(
        self,
        position: Dict[str, Any],
        *,
        symbol: str,
        settings: VnpyPaperSettings,
    ) -> Optional[str]:
        industry = self._industry_name_from_mapping(position)
        if industry:
            return industry
        return self._fetch_symbol_primary_industry(symbol, market=str(position.get("market") or settings.auto_market))

    def _fetch_symbol_primary_industry(self, symbol: str, *, market: str) -> Optional[str]:
        if str(market or "").strip().lower() not in {"", "cn"}:
            return None
        try:
            boards = self.data_fetcher_manager.get_belong_boards(symbol)
        except Exception as exc:  # noqa: BLE001 - optional industry guard should report unavailable.
            logger.warning("Failed to resolve belong boards for vn.py paper industry exposure: %s", exc)
            return None
        return self._pick_primary_board_name(boards if isinstance(boards, list) else [])

    @classmethod
    def _candidate_primary_industry(cls, candidate: Dict[str, Any]) -> Optional[str]:
        for value in cls._candidate_values(
            candidate,
            (
                "industry",
                "sector",
                "board",
                "board_name",
                "belong_board",
                "llm_sector",
                "llmSector",
                "行业",
                "所属行业",
                "板块",
            ),
        ):
            industry = cls._industry_name_from_value(value)
            if industry:
                return industry
        for value in cls._candidate_values(candidate, ("belong_boards", "belongBoards", "boards")):
            industry = cls._industry_name_from_value(value)
            if industry:
                return industry
        return None

    @classmethod
    def _industry_name_from_mapping(cls, payload: Dict[str, Any]) -> Optional[str]:
        for key in ("industry", "sector", "board", "board_name", "belong_board", "所属行业", "行业", "板块"):
            industry = cls._industry_name_from_value(payload.get(key))
            if industry:
                return industry
        for key in ("belong_boards", "belongBoards", "boards"):
            industry = cls._industry_name_from_value(payload.get(key))
            if industry:
                return industry
        return None

    @classmethod
    def _industry_name_from_value(cls, value: Any) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, str):
            text = value.strip()
            return text[:128] if text else None
        if isinstance(value, dict):
            for key in ("name", "industry", "sector", "board_name", "板块名称", "行业"):
                industry = cls._industry_name_from_value(value.get(key))
                if industry:
                    return industry
            return None
        if isinstance(value, list):
            return cls._pick_primary_board_name([item for item in value if isinstance(item, dict)])
        return None

    @staticmethod
    def _pick_primary_board_name(boards: List[Dict[str, Any]]) -> Optional[str]:
        preferred: Optional[str] = None
        fallback: Optional[str] = None
        for item in boards:
            name = str(item.get("name") or item.get("板块名称") or item.get("industry") or "").strip()
            if not name:
                continue
            if fallback is None:
                fallback = name
            type_text = str(item.get("type") or item.get("板块类型") or "").strip().lower()
            if "行业" in type_text or "industry" in type_text or "sector" in type_text:
                preferred = name
                break
        return preferred or fallback

    def _run_auto_sell_checks(self, settings: VnpyPaperSettings, *, run_id: int) -> List[Dict[str, Any]]:
        if (
            not settings.auto_sell_enabled
            or settings.auto_execution_mode not in {"paper", "vnpy_paper"}
            or (
                settings.auto_stop_loss_pct is None
                and settings.auto_take_profit_pct is None
                and settings.auto_trailing_stop_pct is None
                and settings.auto_max_holding_days is None
                and not settings.auto_signal_exit_enabled
                and settings.auto_no_progress_days is None
                and not settings.auto_rebalance_enabled
            )
        ):
            return []

        account = self.ensure_account(settings=settings)
        account_id = int(account["id"])
        try:
            snapshot = self.portfolio.get_portfolio_snapshot(account_id=account_id)
        except Exception as exc:  # noqa: BLE001 - sell checks should not break buy screening.
            logger.warning("Failed to build vn.py paper snapshot for auto sell checks: %s", exc)
            return []

        accounts = snapshot.get("accounts") or []
        if not accounts:
            return []
        orders: List[Dict[str, Any]] = []
        positions = list(accounts[0].get("positions") or [])
        trailing_peaks = self._load_trailing_peaks() if settings.auto_trailing_stop_pct is not None else {}
        signal_exits = self._auto_signal_exit_map(settings=settings, positions=positions)
        rebalance_plans = self._auto_rebalance_plan_map(
            settings=settings,
            snapshot=snapshot,
            positions=positions,
        )
        trailing_changed = False
        active_symbols: set[str] = set()
        sold_symbols: set[str] = set()
        for offset, position in enumerate(positions, start=1):
            if not isinstance(position, dict):
                continue
            symbol = self._normalize_symbol(position.get("symbol") or "")
            quantity = _safe_float(position.get("quantity"))
            if not symbol or quantity is None or quantity <= 0:
                continue
            default_exit_quantity = self._auto_sell_quantity(quantity=quantity, settings=settings)
            position_market = str(position.get("market") or settings.auto_market or "cn").strip().lower() or "cn"
            decision_signal = signal_exits.get((position_market, symbol))
            rebalance_plan = rebalance_plans.get((position_market, symbol))
            active_symbols.add(symbol)
            holding_days = self._position_holding_days(account_id=account_id, symbol=symbol)
            price = self._position_last_price(position)
            exit_reason = self._auto_sell_reason(
                settings=settings,
                position=position,
                holding_days=holding_days,
            )
            if not exit_reason:
                exit_reason, trailing_changed = self._trailing_stop_reason(
                    settings=settings,
                    symbol=symbol,
                    position=position,
                    price=price,
                    trailing_peaks=trailing_peaks,
                    changed=trailing_changed,
                )
            if not exit_reason and decision_signal is not None:
                exit_reason = "strategy_invalidated"
            if not exit_reason:
                if rebalance_plan is not None:
                    exit_reason = str(rebalance_plan.get("reason") or "portfolio_rebalance")
                    exit_quantity = _safe_float(rebalance_plan.get("quantity")) or 0.0
                else:
                    continue
            else:
                exit_quantity = default_exit_quantity
            if exit_quantity <= 0:
                continue
            candidate = {
                "code": symbol,
                "name": position.get("name") or symbol,
                "price": price,
                "quantity": exit_quantity,
                "position_quantity": quantity,
                "sell_position_pct": settings.auto_sell_position_pct or 100.0,
                "avg_cost": position.get("avg_cost"),
                "unrealized_pnl_pct": position.get("unrealized_pnl_pct"),
                "holding_days": holding_days,
                "reason": exit_reason,
            }
            if decision_signal is not None:
                candidate["decision_signal"] = decision_signal
            if rebalance_plan is not None:
                candidate["rebalance_plan"] = rebalance_plan
            active_plan = self._active_vnpy_trade_plan(symbol=symbol, side="sell", settings=settings)
            if active_plan is not None:
                order = self._skipped_order(
                    symbol=symbol,
                    side="sell",
                    quantity=exit_quantity,
                    price=price,
                    reason="active_vnpy_order_exists",
                    raw={**candidate, "active_trade_plan": self._trade_plan_reference(active_plan)},
                )
            elif price is None or price <= 0:
                order = self._skipped_order(
                    symbol=symbol,
                    side="sell",
                    quantity=exit_quantity,
                    reason="sell_price_unavailable",
                    raw=candidate,
                )
            else:
                execution_route = "vnpy_bridge" if settings.auto_execution_mode == "vnpy_paper" else "local_paper"
                order = self.submit_order(
                    symbol=symbol,
                    side="sell",
                    market=str(position.get("market") or settings.auto_market),
                    quantity=exit_quantity,
                    price=price,
                    source="alphasift_auto_exit",
                    dedup_key=f"{date.today().isoformat()}:exit:{symbol}:{exit_reason}",
                    note=f"auto exit {exit_reason}",
                    raw=candidate,
                    execution_route=execution_route,
                )
            orders.append(order)
            if self._trade_plan_status(order) == "filled":
                sold_symbols.add(symbol)
            order_reason = str(order.get("reason") or "").strip()
            decision_reason = exit_reason if order.get("accepted") else (order_reason or exit_reason)
            risk_flags = [exit_reason]
            if order_reason and order_reason != exit_reason:
                risk_flags.append(order_reason)
            self._record_agent_decision(
                run_id=run_id,
                sequence=offset,
                candidate=candidate,
                symbol=symbol,
                settings=settings,
                action="sell",
                side="sell",
                order=order,
                reason=decision_reason,
                risk_flags=risk_flags,
            )
        if settings.auto_trailing_stop_pct is not None:
            cleaned = {
                symbol: value
                for symbol, value in trailing_peaks.items()
                if symbol in active_symbols and symbol not in sold_symbols
            }
            if cleaned != trailing_peaks:
                trailing_changed = True
            if trailing_changed:
                self._save_trailing_peaks(cleaned)
        return orders

    def _auto_rebalance_plan_map(
        self,
        *,
        settings: VnpyPaperSettings,
        snapshot: Dict[str, Any],
        positions: List[Dict[str, Any]],
    ) -> Dict[Tuple[str, str], Dict[str, Any]]:
        target_position_weights = dict(settings.auto_target_position_weights or {})
        target_industry_weights = dict(settings.auto_target_industry_weights or {})
        rebalance_configured = (
            self._position_exposure_configured(settings)
            or bool(target_position_weights)
            or bool(target_industry_weights)
        )
        if not settings.auto_rebalance_enabled or not rebalance_configured:
            return {}

        accounts = snapshot.get("accounts") or []
        account_snapshot = accounts[0] if accounts and isinstance(accounts[0], dict) else snapshot
        total_market_value = _safe_float(account_snapshot.get("total_market_value"))
        if total_market_value is None:
            total_market_value = _safe_float(snapshot.get("total_market_value"))
        total_equity = (
            _safe_float(account_snapshot.get("total_equity"))
            or _safe_float(snapshot.get("total_equity"))
        )

        entries: List[Dict[str, Any]] = []
        industry_entries: Dict[str, List[Dict[str, Any]]] = {}
        total_from_positions = 0.0
        for position in positions:
            if not isinstance(position, dict):
                continue
            symbol = self._normalize_symbol(position.get("symbol") or "")
            quantity = _safe_float(position.get("quantity"))
            if not symbol or quantity is None or quantity <= 0:
                continue
            market_value = self._position_market_value(position)
            price = self._position_last_price(position)
            if (price is None or price <= 0) and market_value > 0:
                price = market_value / quantity
            if price is None or price <= 0 or market_value <= 0:
                continue
            market = str(position.get("market") or settings.auto_market or "cn").strip().lower() or "cn"
            industry = (
                self._position_primary_industry(position, symbol=symbol, settings=settings)
                if self._industry_exposure_configured(settings) or bool(target_industry_weights)
                else None
            )
            entry = {
                "symbol": symbol,
                "market": market,
                "quantity": quantity,
                "price": price,
                "market_value": market_value,
                "industry": industry,
            }
            entries.append(entry)
            total_from_positions += market_value
            if industry:
                industry_entries.setdefault(industry, []).append(entry)
        if not entries:
            return {}
        if total_market_value is None:
            total_market_value = total_from_positions

        plans: Dict[Tuple[str, str], Dict[str, Any]] = {}

        def consider(
            entry: Dict[str, Any],
            *,
            reason: str,
            excess_value: float,
            threshold_value: Optional[float],
            current_value: Optional[float],
            industry: Optional[str] = None,
            target_weight_pct: Optional[float] = None,
        ) -> None:
            if excess_value <= PAPER_EPS:
                return
            price = _safe_float(entry.get("price"))
            quantity = _safe_float(entry.get("quantity"))
            if price is None or price <= 0 or quantity is None or quantity <= 0:
                return
            sell_quantity = min(quantity, excess_value / price)
            if sell_quantity <= PAPER_EPS:
                return
            key = (str(entry["market"]), str(entry["symbol"]))
            existing = plans.get(key)
            if existing is not None and (_safe_float(existing.get("excess_value")) or 0.0) >= excess_value:
                return
            plans[key] = {
                "reason": reason,
                "quantity": round(sell_quantity, 8),
                "price": round(price, 8),
                "excess_value": round(excess_value, 6),
                "threshold_value": round(threshold_value, 6) if threshold_value is not None else None,
                "current_value": round(current_value, 6) if current_value is not None else None,
                "position_market_value": round(_safe_float(entry.get("market_value")) or 0.0, 6),
                "industry": industry,
                "target_weight_pct": (
                    round(target_weight_pct, 6) if target_weight_pct is not None else None
                ),
            }

        for entry in entries:
            market_value = _safe_float(entry.get("market_value")) or 0.0
            if settings.auto_max_single_position_value is not None:
                consider(
                    entry,
                    reason="rebalance_single_position_value_exceeded",
                    excess_value=market_value - settings.auto_max_single_position_value,
                    threshold_value=settings.auto_max_single_position_value,
                    current_value=market_value,
                )
            target_weight = target_position_weights.get(str(entry.get("symbol") or ""))
            if target_weight is not None and total_equity is not None and total_equity > 0:
                target_value = total_equity * target_weight / 100.0
                consider(
                    entry,
                    reason="rebalance_target_position_weight_exceeded",
                    excess_value=market_value - target_value,
                    threshold_value=target_value,
                    current_value=market_value,
                    target_weight_pct=target_weight,
                )

        if total_market_value and total_market_value > 0:
            if settings.auto_max_total_position_value is not None:
                self._allocate_rebalance_excess(
                    entries=entries,
                    total_value=total_market_value,
                    excess_value=total_market_value - settings.auto_max_total_position_value,
                    threshold_value=settings.auto_max_total_position_value,
                    reason="rebalance_total_position_value_exceeded",
                    plans_consider=consider,
                )
            if (
                settings.auto_max_total_position_pct is not None
                and total_equity is not None
                and total_equity > 0
            ):
                threshold_value = total_equity * settings.auto_max_total_position_pct / 100.0
                self._allocate_rebalance_excess(
                    entries=entries,
                    total_value=total_market_value,
                    excess_value=total_market_value - threshold_value,
                    threshold_value=threshold_value,
                    reason="rebalance_total_position_pct_exceeded",
                    plans_consider=consider,
                )

        if self._industry_exposure_configured(settings) or bool(target_industry_weights):
            for industry, items in industry_entries.items():
                industry_value = sum(_safe_float(item.get("market_value")) or 0.0 for item in items)
                if industry_value <= 0:
                    continue
                if settings.auto_max_industry_position_value is not None:
                    self._allocate_rebalance_excess(
                        entries=items,
                        total_value=industry_value,
                        excess_value=industry_value - settings.auto_max_industry_position_value,
                        threshold_value=settings.auto_max_industry_position_value,
                        reason="rebalance_industry_position_value_exceeded",
                        plans_consider=consider,
                        industry=industry,
                    )
                if (
                    settings.auto_max_industry_position_pct is not None
                    and total_equity is not None
                    and total_equity > 0
                ):
                    threshold_value = total_equity * settings.auto_max_industry_position_pct / 100.0
                    self._allocate_rebalance_excess(
                        entries=items,
                        total_value=industry_value,
                        excess_value=industry_value - threshold_value,
                        threshold_value=threshold_value,
                        reason="rebalance_industry_position_pct_exceeded",
                        plans_consider=consider,
                        industry=industry,
                    )
                target_weight = target_industry_weights.get(industry)
                if target_weight is not None and total_equity is not None and total_equity > 0:
                    threshold_value = total_equity * target_weight / 100.0
                    self._allocate_rebalance_excess(
                        entries=items,
                        total_value=industry_value,
                        excess_value=industry_value - threshold_value,
                        threshold_value=threshold_value,
                        reason="rebalance_target_industry_weight_exceeded",
                        plans_consider=consider,
                        industry=industry,
                        target_weight_pct=target_weight,
                    )

        return plans

    @staticmethod
    def _allocate_rebalance_excess(
        *,
        entries: List[Dict[str, Any]],
        total_value: float,
        excess_value: float,
        threshold_value: Optional[float],
        reason: str,
        plans_consider: Any,
        industry: Optional[str] = None,
        target_weight_pct: Optional[float] = None,
    ) -> None:
        if excess_value <= PAPER_EPS or total_value <= PAPER_EPS:
            return
        for entry in entries:
            market_value = _safe_float(entry.get("market_value")) or 0.0
            if market_value <= 0:
                continue
            allocated_excess = excess_value * (market_value / total_value)
            plans_consider(
                entry,
                reason=reason,
                excess_value=allocated_excess,
                threshold_value=threshold_value,
                current_value=total_value,
                industry=industry,
                target_weight_pct=target_weight_pct,
            )

    @staticmethod
    def _position_last_price(position: Dict[str, Any]) -> Optional[float]:
        for key in ("last_price", "lastPrice", "price", "latest_price", "close", "avg_cost"):
            value = _safe_float(position.get(key))
            if value is not None and value > 0:
                return value
        quantity = _safe_float(position.get("quantity"))
        market_value = VnpyPaperTradingService._position_market_value(position)
        if quantity is not None and quantity > 0 and market_value > 0:
            return market_value / quantity
        return None

    def _auto_signal_exit_map(
        self,
        *,
        settings: VnpyPaperSettings,
        positions: List[Dict[str, Any]],
    ) -> Dict[Tuple[str, str], Dict[str, Any]]:
        if not settings.auto_signal_exit_enabled:
            return {}
        identities: set[Tuple[str, str]] = set()
        for position in positions:
            if not isinstance(position, dict):
                continue
            symbol = self._normalize_symbol(position.get("symbol") or "")
            if not symbol:
                continue
            market = str(position.get("market") or settings.auto_market or "cn").strip().lower() or "cn"
            identities.add((market, symbol))
        if not identities:
            return {}

        try:
            service = self._get_decision_signal_service()
            signal_map: Dict[Tuple[str, str], Dict[str, Any]] = {}
            page = 1
            while True:
                response = service.list_signals(
                    stock_identities=sorted(identities),
                    status="active",
                    page=page,
                    page_size=100,
                )
                items = response.get("items") if isinstance(response, dict) else []
                if not isinstance(items, list):
                    break
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    action = str(item.get("action") or "").strip().lower()
                    if action not in AUTO_SIGNAL_EXIT_ACTIONS:
                        continue
                    market = str(item.get("market") or "").strip().lower()
                    symbol = self._normalize_symbol(item.get("stock_code") or "")
                    key = (market, symbol)
                    if market and symbol and key not in signal_map:
                        signal_map[key] = self._decision_signal_exit_summary(item)
                total = _safe_int(response.get("total") if isinstance(response, dict) else None) or 0
                if page * 100 >= total or not items:
                    break
                page += 1
            return signal_map
        except Exception as exc:  # noqa: BLE001 - signal exits are optional and should fail open.
            logger.warning("Failed to load decision signals for auto sell checks: %s", exc)
            return {}

    def _get_decision_signal_service(self) -> Any:
        if self.decision_signal_service is None:
            from src.services.decision_signal_service import DecisionSignalService

            self.decision_signal_service = DecisionSignalService()
        return self.decision_signal_service

    @staticmethod
    def _decision_signal_exit_summary(signal: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": _safe_int(signal.get("id")),
            "action": signal.get("action"),
            "action_label": signal.get("action_label"),
            "confidence": _safe_float(signal.get("confidence")),
            "score": _safe_float(signal.get("score")),
            "horizon": signal.get("horizon"),
            "source_type": signal.get("source_type"),
            "source_report_id": _safe_int(signal.get("source_report_id")),
            "reason": signal.get("reason"),
            "risk_summary": signal.get("risk_summary"),
            "created_at": signal.get("created_at"),
            "expires_at": signal.get("expires_at"),
        }

    @staticmethod
    def _auto_sell_quantity(*, quantity: float, settings: VnpyPaperSettings) -> float:
        position_quantity = float(quantity or 0.0)
        if position_quantity <= 0:
            return 0.0
        sell_pct = settings.auto_sell_position_pct
        if sell_pct is None or sell_pct >= 100:
            return round(position_quantity, 8)
        target = position_quantity * (max(0.0, min(100.0, float(sell_pct))) / 100.0)
        if target <= PAPER_EPS:
            return 0.0
        return round(min(position_quantity, target), 8)

    def _position_holding_days(self, *, account_id: int, symbol: str) -> Optional[int]:
        try:
            payload = self.portfolio.list_trade_events(
                account_id=account_id,
                symbol=symbol,
                side="buy",
                page=1,
                page_size=200,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to resolve holding days for %s: %s", symbol, exc)
            return None
        dates: List[date] = []
        for item in payload.get("items") or []:
            text = str(item.get("trade_date") or "").strip()
            if not text:
                continue
            try:
                dates.append(date.fromisoformat(text[:10]))
            except ValueError:
                continue
        if not dates:
            return None
        return max(0, (date.today() - min(dates)).days)

    @staticmethod
    def _auto_sell_reason(
        *,
        settings: VnpyPaperSettings,
        position: Dict[str, Any],
        holding_days: Optional[int],
    ) -> Optional[str]:
        pnl_pct = _safe_float(position.get("unrealized_pnl_pct"))
        if (
            settings.auto_stop_loss_pct is not None
            and pnl_pct is not None
            and pnl_pct <= -abs(settings.auto_stop_loss_pct)
        ):
            return "stop_loss_triggered"
        if (
            settings.auto_take_profit_pct is not None
            and pnl_pct is not None
            and pnl_pct >= abs(settings.auto_take_profit_pct)
        ):
            return "take_profit_triggered"
        if (
            settings.auto_max_holding_days is not None
            and holding_days is not None
            and holding_days >= settings.auto_max_holding_days
        ):
            return "max_holding_days_reached"
        if (
            settings.auto_no_progress_days is not None
            and holding_days is not None
            and holding_days >= settings.auto_no_progress_days
            and pnl_pct is not None
            and pnl_pct <= (
                settings.auto_no_progress_min_return_pct
                if settings.auto_no_progress_min_return_pct is not None
                else 0.0
            )
        ):
            return "no_progress_timeout"
        return None

    def _trailing_stop_reason(
        self,
        *,
        settings: VnpyPaperSettings,
        symbol: str,
        position: Dict[str, Any],
        price: Optional[float],
        trailing_peaks: Dict[str, float],
        changed: bool,
    ) -> Tuple[Optional[str], bool]:
        if settings.auto_trailing_stop_pct is None or price is None or price <= 0:
            return None, changed
        stored_peak = _safe_float(trailing_peaks.get(symbol))
        avg_cost = _safe_float(position.get("avg_cost"))
        peak = max(
            value
            for value in (stored_peak or 0.0, avg_cost or 0.0, price)
            if value is not None
        )
        if stored_peak is None or peak > stored_peak + 1e-8:
            trailing_peaks[symbol] = peak
            changed = True
        if peak <= 0:
            return None, changed
        drawdown_pct = (peak - price) / peak * 100.0
        if drawdown_pct >= abs(settings.auto_trailing_stop_pct):
            return "trailing_stop_triggered", changed
        return None, changed

    def _load_trailing_peaks(self) -> Dict[str, float]:
        payload = self._read_config_payload()
        raw = payload.get("auto_trailing_peaks") if isinstance(payload, dict) else None
        if not isinstance(raw, dict):
            return {}
        peaks: Dict[str, float] = {}
        for symbol, value in raw.items():
            normalized = self._normalize_symbol(symbol)
            peak = _safe_float(value)
            if normalized and peak is not None and peak > 0:
                peaks[normalized] = peak
        return peaks

    def _save_trailing_peaks(self, peaks: Dict[str, float]) -> None:
        with self._lock:
            payload = self._read_config_payload()
            payload["auto_trailing_peaks"] = {
                self._normalize_symbol(symbol): float(value)
                for symbol, value in sorted(peaks.items())
                if self._normalize_symbol(symbol) and _safe_float(value) is not None and float(value) > 0
            }
            payload["updated_at"] = _utc_now_iso()
            self._write_config_payload(payload)

    def _daily_auto_trade_usage(self, settings: VnpyPaperSettings) -> Dict[str, Any]:
        if settings.auto_daily_max_orders is None and settings.auto_daily_budget is None:
            return {"order_count": 0.0, "cash_amount": 0.0, "fx_unavailable": False}

        account = self.ensure_account(settings=settings)
        base_currency = str(account.get("base_currency") or "CNY").strip().upper() or "CNY"
        today = date.today()
        order_count = 0
        cash_amount = 0.0
        page = 1
        while True:
            try:
                payload = self.portfolio.list_trade_events(
                    account_id=int(account["id"]),
                    date_from=today,
                    date_to=today,
                    side="buy",
                    page=page,
                    page_size=100,
                )
            except Exception as exc:  # noqa: BLE001 - fail closed for auto trade limits.
                logger.warning("Failed to resolve daily auto trade usage: %s", exc)
                return {
                    "order_count": float(settings.auto_daily_max_orders or 0),
                    "cash_amount": float(settings.auto_daily_budget or 0.0),
                    "fx_unavailable": True,
                }
            items = list(payload.get("items") or [])
            for item in items:
                note = str(item.get("note") or "")
                if "source=alphasift_auto" not in note:
                    continue
                order_count += 1
                local_notional = float(item.get("quantity") or 0.0) * float(item.get("price") or 0.0)
                trade_currency = str(
                    item.get("currency") or self._currency_for_market(item.get("market"))
                ).strip().upper()
                try:
                    converted, stale, _source = self.portfolio.convert_amount(
                        amount=local_notional,
                        from_currency=trade_currency,
                        to_currency=base_currency,
                        as_of_date=today,
                    )
                except Exception as exc:  # noqa: BLE001 - automatic limits must fail closed.
                    logger.warning("Failed to convert daily auto trade usage: %s", exc)
                    return {
                        "order_count": float(order_count),
                        "cash_amount": float(settings.auto_daily_budget or 0.0),
                        "fx_unavailable": True,
                    }
                if trade_currency != base_currency and stale:
                    return {
                        "order_count": float(order_count),
                        "cash_amount": float(settings.auto_daily_budget or 0.0),
                        "fx_unavailable": True,
                    }
                cash_amount += converted
            if page * int(payload.get("page_size") or 100) >= int(payload.get("total") or 0):
                break
            page += 1
        return {
            "order_count": float(order_count),
            "cash_amount": cash_amount,
            "fx_unavailable": False,
        }

    @staticmethod
    def _daily_limit_reason(
        *,
        settings: VnpyPaperSettings,
        order_count: int,
        cash_used: float,
        next_cash_amount: float,
    ) -> Optional[str]:
        if settings.auto_daily_max_orders is not None and order_count >= settings.auto_daily_max_orders:
            return "daily_order_limit_reached"
        if (
            settings.auto_daily_budget is not None
            and cash_used + max(0.0, float(next_cash_amount or 0.0)) > settings.auto_daily_budget + 1e-8
        ):
            return "daily_budget_exceeded"
        return None

    def _account_pre_trade_risk_reason(self, settings: VnpyPaperSettings) -> Optional[str]:
        reason, _diagnostics = self._account_pre_trade_risk(settings)
        return reason

    def _account_pre_trade_risk(self, settings: VnpyPaperSettings) -> Tuple[Optional[str], Dict[str, Any]]:
        diagnostics: Dict[str, Any] = {
            "min_cash_balance": settings.auto_min_cash_balance,
            "max_drawdown_pct": settings.auto_max_drawdown_pct,
        }
        if settings.auto_min_cash_balance is None and settings.auto_max_drawdown_pct is None:
            return None, diagnostics

        try:
            account = self.ensure_account(settings=settings)
            snapshot = self.portfolio.get_portfolio_snapshot(account_id=int(account["id"]))
            accounts = snapshot.get("accounts") or []
            account_snapshot = accounts[0] if accounts and isinstance(accounts[0], dict) else snapshot
        except Exception as exc:  # noqa: BLE001 - configured account guard should fail closed.
            logger.warning("Failed to resolve vn.py paper account risk snapshot: %s", exc)
            diagnostics["error"] = str(exc)
            return "account_risk_unavailable", diagnostics

        diagnostics["account_id"] = _safe_int(account.get("id")) if isinstance(account, dict) else None

        if settings.auto_min_cash_balance is not None:
            cash = _safe_float(account_snapshot.get("total_cash"))
            if cash is None:
                cash = _safe_float(snapshot.get("total_cash"))
            diagnostics["cash"] = cash
            if cash is None or cash < settings.auto_min_cash_balance:
                diagnostics["observed_value"] = cash
                diagnostics["threshold"] = settings.auto_min_cash_balance
                return "cash_low_watermark", diagnostics

        if settings.auto_max_drawdown_pct is not None:
            drawdown = self._account_drawdown_diagnostics(
                settings=settings,
                account=account,
                snapshot=snapshot,
                requested=True,
                update_peak=True,
            )
            diagnostics.update(drawdown)
            if drawdown.get("status") == "unavailable":
                diagnostics["observed_value"] = None
                diagnostics["threshold"] = settings.auto_max_drawdown_pct
                return "account_risk_unavailable", diagnostics
            drawdown_pct = _safe_float(drawdown.get("drawdown_pct")) or 0.0
            if drawdown_pct >= settings.auto_max_drawdown_pct:
                diagnostics["observed_value"] = drawdown_pct
                diagnostics["threshold"] = settings.auto_max_drawdown_pct
                return "account_drawdown_limit_reached", diagnostics

        return None, diagnostics

    def _account_drawdown_diagnostics(
        self,
        *,
        settings: VnpyPaperSettings,
        account: Optional[Dict[str, Any]],
        snapshot: Optional[Dict[str, Any]],
        requested: bool,
        update_peak: bool,
    ) -> Dict[str, Any]:
        configured = settings.auto_max_drawdown_pct is not None
        account_id = _safe_int(account.get("id")) if isinstance(account, dict) else _safe_int(settings.account_id)
        result: Dict[str, Any] = {
            "configured": configured,
            "status": "disabled" if not configured else "snapshot_not_requested",
            "basis": "observed_equity_peak",
            "account_id": account_id,
            "initial_cash": _safe_float(settings.initial_cash),
            "equity": None,
            "peak_equity": None,
            "drawdown_pct": None,
            "threshold_pct": settings.auto_max_drawdown_pct,
        }
        if not configured:
            return result
        if not requested:
            return result
        if account_id is None or not isinstance(snapshot, dict):
            result["status"] = "unavailable"
            return result

        accounts = snapshot.get("accounts") or []
        account_snapshot = accounts[0] if accounts and isinstance(accounts[0], dict) else snapshot
        equity = _safe_float(account_snapshot.get("total_equity"))
        if equity is None:
            equity = _safe_float(snapshot.get("total_equity"))
        initial_cash = _safe_float(settings.initial_cash)
        if equity is None or initial_cash is None or initial_cash <= 0:
            result["status"] = "unavailable"
            result["equity"] = equity
            return result

        peak_equity = self._resolve_account_equity_peak(
            account_id=account_id,
            initial_cash=initial_cash,
            equity=equity,
            update=update_peak,
        )
        drawdown_pct = max(0.0, (peak_equity - equity) / peak_equity * 100.0)
        result.update(
            {
                "status": (
                    "limit_reached"
                    if drawdown_pct >= float(settings.auto_max_drawdown_pct or 0.0)
                    else "ready"
                ),
                "equity": equity,
                "peak_equity": peak_equity,
                "drawdown_pct": round(drawdown_pct, 6),
            }
        )
        return result

    def _resolve_account_equity_peak(
        self,
        *,
        account_id: int,
        initial_cash: float,
        equity: float,
        update: bool,
    ) -> float:
        with self._lock:
            payload = self._read_config_payload()
            peaks = payload.get("auto_account_equity_peaks") if isinstance(payload, dict) else None
            peak_map = dict(peaks) if isinstance(peaks, dict) else {}
            stored_peak = _safe_float(peak_map.get(str(account_id)))
            peak = max(initial_cash, equity, stored_peak or 0.0)
            if update and (stored_peak is None or peak > stored_peak + 1e-8):
                if not isinstance(payload, dict):
                    payload = {}
                peak_map[str(account_id)] = peak
                payload["auto_account_equity_peaks"] = peak_map
                payload["updated_at"] = _utc_now_iso()
                self._write_config_payload(payload)
            return peak

    @staticmethod
    def _market_light_pre_trade_risk_reason(settings: VnpyPaperSettings) -> Optional[str]:
        if not settings.auto_market_light_gate_enabled:
            return None
        market = str(settings.auto_market or "").strip().lower()
        if market not in {"cn", "hk", "us", "jp", "kr"}:
            return None
        try:
            snapshot = load_previous_snapshot(
                market,
                before_trade_date=(date.today() + timedelta(days=1)).isoformat(),
            )
        except Exception as exc:  # noqa: BLE001 - missing market context should not break auto-trade runs.
            logger.warning("Failed to resolve market-light gate for vn.py paper auto trade: %s", exc)
            return None
        if not isinstance(snapshot, dict):
            return None
        status = str(snapshot.get("status") or "").strip().lower()
        if status in set(settings.auto_market_light_block_statuses or []):
            return f"market_light_{status}"
        return None

    @staticmethod
    def _screen_data_quality(screen: Dict[str, Any], candidates: List[Any]) -> Dict[str, Any]:
        warnings = [str(item) for item in list(screen.get("warnings") or []) if item]
        source_errors = [str(item) for item in list(screen.get("source_errors") or []) if item]
        explicit = str(
            screen.get("quality_status")
            or screen.get("data_quality")
            or ""
        ).strip().lower()
        fallback_used = bool(screen.get("fallback_used"))
        stale = bool(screen.get("stale"))
        status = "ok"
        reason = "screen_ok"

        if explicit in {"ok", "complete", "available", "realtime", "healthy"}:
            status = "ok"
            reason = f"explicit_{explicit}"
        elif explicit in {"partial", "fallback", "degraded", "directory_fallback"}:
            status = "partial"
            reason = f"explicit_{explicit}"
        elif explicit == "stale" or stale:
            status = "stale"
            reason = "explicit_stale" if explicit == "stale" else "stale_flag"
        elif explicit in {"unavailable", "failed", "error"}:
            status = "unavailable"
            reason = f"explicit_{explicit}"
        elif not candidates and source_errors:
            status = "unavailable"
            reason = "empty_candidates_with_source_errors"
        elif source_errors or warnings or fallback_used:
            status = "partial"
            reason = "degraded_but_usable"

        return {
            "status": status,
            "reason": reason,
            "warnings": warnings,
            "source_errors": source_errors,
            "fallback_used": fallback_used,
            "stale": stale,
            "stale_age_hours": screen.get("stale_age_hours"),
        }

    @staticmethod
    def _normalize_symbol(symbol: Any) -> str:
        return str(symbol or "").strip().upper()

    @staticmethod
    def _currency_for_market(market: Any) -> str:
        return MARKET_CURRENCIES.get(str(market or "cn").strip().lower(), "CNY")

    def _paper_trade_performance(
        self,
        account_id: int,
        *,
        initial_cash: float,
        current_equity: Optional[float],
        current_market_value: Optional[float],
    ) -> Dict[str, Any]:
        trades: List[Dict[str, Any]] = []
        page = 1
        page_size = 100
        while True:
            try:
                payload = self.portfolio.list_trade_events(
                    account_id=account_id,
                    page=page,
                    page_size=page_size,
                )
            except Exception as exc:  # noqa: BLE001 - performance summary should fail open.
                logger.warning("Failed to list vn.py paper trades for performance: %s", exc)
                metrics = self._empty_trade_metrics()
                metrics["diagnostics"] = {
                    "trade_events_error": str(exc),
                }
                return {
                    "trade_metrics": metrics,
                    "risk_metrics": self._empty_risk_metrics(),
                    "equity_curve": [],
                    "daily_returns": [],
                    "monthly_returns": [],
                }
            trades.extend([item for item in list(payload.get("items") or []) if isinstance(item, dict)])
            total = int(payload.get("total") or len(trades))
            if page * page_size >= total:
                break
            page += 1

        trades.sort(key=lambda item: (str(item.get("trade_date") or ""), int(item.get("id") or 0)))
        lots: Dict[str, List[Dict[str, float]]] = {}
        equity_curve: List[Dict[str, Any]] = []
        buy_count = 0
        sell_count = 0
        gross_turnover = 0.0
        buy_turnover = 0.0
        sell_turnover = 0.0
        sell_win_count = 0
        sell_loss_count = 0
        sell_flat_count = 0
        closed_trade_count = 0
        closed_quantity = 0.0
        realized_trade_pnl = 0.0
        cumulative_realized_pnl = 0.0
        sell_return_pct_sum = 0.0
        unmatched_sell_quantity = 0.0
        peak_equity = initial_cash if initial_cash > 0 else None
        max_drawdown_value = 0.0
        max_drawdown_pct: Optional[float] = 0.0 if initial_cash > 0 else None

        if initial_cash > 0:
            equity_curve.append({
                "date": None,
                "equity": round(initial_cash, 6),
                "realized_pnl": 0.0,
                "source": "initial",
            })

        for trade in trades:
            side = str(trade.get("side") or "").strip().lower()
            symbol = self._normalize_symbol(trade.get("symbol"))
            quantity = _safe_float(trade.get("quantity")) or 0.0
            price = _safe_float(trade.get("price")) or 0.0
            fee = _safe_float(trade.get("fee")) or 0.0
            tax = _safe_float(trade.get("tax")) or 0.0
            if not symbol or quantity <= 0 or price <= 0:
                continue
            gross_value = quantity * price
            gross_turnover += gross_value
            trade_realized_pnl = 0.0
            if side == "buy":
                buy_count += 1
                buy_turnover += gross_value
                lots.setdefault(symbol, []).append({
                    "quantity": quantity,
                    "cost": gross_value + fee + tax,
                })
            elif side == "sell":
                sell_count += 1
                sell_turnover += gross_value
                remaining = quantity
                cost_basis = 0.0
                symbol_lots = lots.setdefault(symbol, [])
                while remaining > PAPER_EPS and symbol_lots:
                    lot = symbol_lots[0]
                    lot_quantity = float(lot.get("quantity") or 0.0)
                    lot_cost = float(lot.get("cost") or 0.0)
                    if lot_quantity <= PAPER_EPS:
                        symbol_lots.pop(0)
                        continue
                    take = min(remaining, lot_quantity)
                    cost_basis += lot_cost * (take / lot_quantity)
                    lot["quantity"] = lot_quantity - take
                    lot["cost"] = lot_cost - lot_cost * (take / lot_quantity)
                    remaining -= take
                    if lot["quantity"] <= PAPER_EPS:
                        symbol_lots.pop(0)
                matched_quantity = quantity - remaining
                if remaining > PAPER_EPS:
                    unmatched_sell_quantity += remaining
                if matched_quantity <= PAPER_EPS or cost_basis <= 0:
                    continue
                matched_ratio = matched_quantity / quantity
                proceeds = gross_value * matched_ratio - (fee + tax) * matched_ratio
                pnl = proceeds - cost_basis
                trade_realized_pnl = pnl
                realized_trade_pnl += pnl
                cumulative_realized_pnl += pnl
                closed_trade_count += 1
                closed_quantity += matched_quantity
                sell_return_pct = (pnl / cost_basis) * 100.0
                sell_return_pct_sum += sell_return_pct
                if pnl > PAPER_EPS:
                    sell_win_count += 1
                elif pnl < -PAPER_EPS:
                    sell_loss_count += 1
                else:
                    sell_flat_count += 1
            else:
                continue

            if initial_cash > 0:
                point_equity = initial_cash + cumulative_realized_pnl
                peak_equity, max_drawdown_value, max_drawdown_pct = self._update_drawdown_metrics(
                    point_equity,
                    peak_equity=peak_equity,
                    max_drawdown_value=max_drawdown_value,
                    max_drawdown_pct=max_drawdown_pct,
                )
                equity_curve.append({
                    "date": trade.get("trade_date"),
                    "equity": round(point_equity, 6),
                    "realized_pnl": round(cumulative_realized_pnl, 6),
                    "trade_id": trade.get("id"),
                    "symbol": symbol,
                    "side": side,
                    "trade_realized_pnl": round(trade_realized_pnl, 6),
                    "source": "trade",
                })

        if initial_cash > 0 and current_equity is not None:
            peak_equity, max_drawdown_value, max_drawdown_pct = self._update_drawdown_metrics(
                current_equity,
                peak_equity=peak_equity,
                max_drawdown_value=max_drawdown_value,
                max_drawdown_pct=max_drawdown_pct,
            )
            equity_curve.append({
                "date": None,
                "equity": round(current_equity, 6),
                "realized_pnl": round(cumulative_realized_pnl, 6),
                "source": "snapshot",
            })

        win_rate_pct = (
            round((sell_win_count / closed_trade_count) * 100.0, 6)
            if closed_trade_count > 0
            else None
        )
        average_sell_return_pct = (
            round(sell_return_pct_sum / closed_trade_count, 6)
            if closed_trade_count > 0
            else None
        )
        current_exposure_pct = (
            round((current_market_value / current_equity) * 100.0, 6)
            if current_market_value is not None and current_equity is not None and current_equity > 0
            else None
        )
        turnover_pct = (
            round((gross_turnover / initial_cash) * 100.0, 6)
            if initial_cash > 0
            else None
        )
        trade_metrics = {
            "trade_count": buy_count + sell_count,
            "buy_count": buy_count,
            "sell_count": sell_count,
            "gross_turnover": round(gross_turnover, 6),
            "buy_turnover": round(buy_turnover, 6),
            "sell_turnover": round(sell_turnover, 6),
            "closed_trade_count": closed_trade_count,
            "closed_quantity": round(closed_quantity, 8),
            "sell_win_count": sell_win_count,
            "sell_loss_count": sell_loss_count,
            "sell_flat_count": sell_flat_count,
            "win_rate_pct": win_rate_pct,
            "average_sell_return_pct": average_sell_return_pct,
            "realized_trade_pnl": round(realized_trade_pnl, 6),
            "unmatched_sell_quantity": round(unmatched_sell_quantity, 8),
        }
        risk_metrics = {
            "max_drawdown_pct": round(max_drawdown_pct, 6) if max_drawdown_pct is not None else None,
            "max_drawdown_value": round(max_drawdown_value, 6),
            "peak_equity": round(peak_equity, 6) if peak_equity is not None else None,
            "turnover_pct": turnover_pct,
            "current_exposure_pct": current_exposure_pct,
            "curve_point_count": len(equity_curve),
        }
        daily_returns = self._daily_return_payload(equity_curve, initial_cash=initial_cash, limit=10000)
        return {
            "trade_metrics": trade_metrics,
            "risk_metrics": risk_metrics,
            "equity_curve": self._limited_equity_curve(equity_curve),
            "daily_returns": self._limited_return_rows(daily_returns, limit=120),
            "monthly_returns": self._monthly_return_payload(daily_returns, initial_cash=initial_cash),
        }

    @staticmethod
    def _empty_trade_metrics() -> Dict[str, Any]:
        return {
            "trade_count": 0,
            "buy_count": 0,
            "sell_count": 0,
            "gross_turnover": 0.0,
            "buy_turnover": 0.0,
            "sell_turnover": 0.0,
            "closed_trade_count": 0,
            "closed_quantity": 0.0,
            "sell_win_count": 0,
            "sell_loss_count": 0,
            "sell_flat_count": 0,
            "win_rate_pct": None,
            "average_sell_return_pct": None,
            "realized_trade_pnl": 0.0,
            "unmatched_sell_quantity": 0.0,
        }

    @staticmethod
    def _empty_risk_metrics() -> Dict[str, Any]:
        return {
            "max_drawdown_pct": None,
            "max_drawdown_value": 0.0,
            "peak_equity": None,
            "turnover_pct": None,
            "current_exposure_pct": None,
            "curve_point_count": 0,
        }

    @staticmethod
    def _update_drawdown_metrics(
        equity: float,
        *,
        peak_equity: Optional[float],
        max_drawdown_value: float,
        max_drawdown_pct: Optional[float],
    ) -> Tuple[Optional[float], float, Optional[float]]:
        if peak_equity is None or equity > peak_equity:
            return equity, max_drawdown_value, max_drawdown_pct
        drawdown_value = max(0.0, peak_equity - equity)
        if drawdown_value > max_drawdown_value:
            drawdown_pct = (drawdown_value / peak_equity) * 100.0 if peak_equity > 0 else None
            return peak_equity, drawdown_value, drawdown_pct
        return peak_equity, max_drawdown_value, max_drawdown_pct

    @staticmethod
    def _limited_equity_curve(points: List[Dict[str, Any]], *, limit: int = 80) -> List[Dict[str, Any]]:
        if len(points) <= limit:
            return points
        return [points[0], *points[-(limit - 1):]]

    @classmethod
    def _limited_return_rows(cls, rows: List[Dict[str, Any]], *, limit: int) -> List[Dict[str, Any]]:
        if len(rows) <= limit:
            return rows
        return [rows[0], *rows[-(limit - 1):]]

    @classmethod
    def _daily_return_payload(
        cls,
        points: List[Dict[str, Any]],
        *,
        initial_cash: float,
        limit: int = 120,
    ) -> List[Dict[str, Any]]:
        if initial_cash <= 0:
            return []
        daily_points: Dict[str, Dict[str, Any]] = {}
        today_key = date.today().isoformat()
        for point in points:
            equity = _safe_float(point.get("equity"))
            if equity is None:
                continue
            raw_date = point.get("date")
            date_key = str(raw_date or "").strip()
            if not date_key and point.get("source") == "snapshot":
                date_key = today_key
            if not date_key:
                continue
            date_key = date_key[:10]
            existing = daily_points.setdefault(
                date_key,
                {
                    "date": date_key,
                    "trade_count": 0,
                },
            )
            if point.get("source") == "trade":
                existing["trade_count"] = int(existing.get("trade_count") or 0) + 1
            existing["equity"] = round(equity, 6)
            realized_pnl = _safe_float(point.get("realized_pnl"))
            if realized_pnl is not None:
                existing["realized_pnl"] = round(realized_pnl, 6)
            existing["source"] = str(point.get("source") or "")

        previous_equity = initial_cash
        peak_equity = initial_cash
        rows: List[Dict[str, Any]] = []
        for date_key in sorted(daily_points):
            row = dict(daily_points[date_key])
            equity = float(row["equity"])
            daily_pnl = equity - previous_equity
            if equity > peak_equity:
                peak_equity = equity
            drawdown_pct = (max(0.0, peak_equity - equity) / peak_equity * 100.0) if peak_equity > 0 else None
            row["daily_pnl"] = round(daily_pnl, 6)
            row["daily_return_pct"] = round((daily_pnl / previous_equity) * 100.0, 6) if previous_equity > 0 else None
            row["cumulative_return_pct"] = round(((equity - initial_cash) / initial_cash) * 100.0, 6)
            row["drawdown_pct"] = round(drawdown_pct, 6) if drawdown_pct is not None else None
            rows.append(row)
            previous_equity = equity
        return cls._limited_return_rows(rows, limit=limit)

    @classmethod
    def _monthly_return_payload(
        cls,
        daily_rows: List[Dict[str, Any]],
        *,
        initial_cash: float,
        limit: int = 36,
    ) -> List[Dict[str, Any]]:
        if initial_cash <= 0:
            return []
        month_points: Dict[str, Dict[str, Any]] = {}
        for row in daily_rows:
            date_key = str(row.get("date") or "").strip()
            if len(date_key) < 7:
                continue
            month_key = date_key[:7]
            bucket = month_points.setdefault(
                month_key,
                {
                    "month": month_key,
                    "start_date": date_key[:10],
                    "end_date": date_key[:10],
                    "trade_count": 0,
                    "positive_days": 0,
                    "negative_days": 0,
                },
            )
            bucket["end_date"] = date_key[:10]
            bucket["equity"] = row.get("equity")
            bucket["realized_pnl"] = row.get("realized_pnl")
            bucket["trade_count"] = int(bucket.get("trade_count") or 0) + int(row.get("trade_count") or 0)
            daily_pnl = _safe_float(row.get("daily_pnl"))
            if daily_pnl is not None:
                if daily_pnl > PAPER_EPS:
                    bucket["positive_days"] = int(bucket.get("positive_days") or 0) + 1
                elif daily_pnl < -PAPER_EPS:
                    bucket["negative_days"] = int(bucket.get("negative_days") or 0) + 1

        previous_equity = initial_cash
        peak_equity = initial_cash
        rows: List[Dict[str, Any]] = []
        for month_key in sorted(month_points):
            bucket = dict(month_points[month_key])
            equity = _safe_float(bucket.get("equity"))
            if equity is None:
                continue
            monthly_pnl = equity - previous_equity
            if equity > peak_equity:
                peak_equity = equity
            drawdown_pct = (max(0.0, peak_equity - equity) / peak_equity * 100.0) if peak_equity > 0 else None
            bucket["start_equity"] = round(previous_equity, 6)
            bucket["end_equity"] = round(equity, 6)
            bucket["monthly_pnl"] = round(monthly_pnl, 6)
            bucket["monthly_return_pct"] = (
                round((monthly_pnl / previous_equity) * 100.0, 6)
                if previous_equity > 0
                else None
            )
            bucket["cumulative_return_pct"] = round(((equity - initial_cash) / initial_cash) * 100.0, 6)
            bucket["drawdown_pct"] = round(drawdown_pct, 6) if drawdown_pct is not None else None
            rows.append(bucket)
            previous_equity = equity
        return cls._limited_return_rows(rows, limit=limit)

    @staticmethod
    def _attribution_payload(
        stats: Dict[str, Dict[str, Any]],
        *,
        sort_key: str,
        fill_rate_keys: Tuple[str, str, str],
        limit: int = 8,
    ) -> List[Dict[str, Any]]:
        filled_key, total_key, skipped_key = fill_rate_keys
        items: List[Dict[str, Any]] = []
        for raw_item in stats.values():
            item = dict(raw_item)
            total = int(item.get(total_key) or 0)
            filled = int(item.get(filled_key) or 0)
            skipped = int(item.get(skipped_key) or 0)
            denominator = filled + skipped if total <= 0 else total
            item["fill_rate_pct"] = (
                round((filled / denominator) * 100.0, 6)
                if denominator > 0
                else None
            )
            for money_key in ("planned_cash_amount", "filled_cash_amount"):
                if money_key in item:
                    item[money_key] = round(float(item.get(money_key) or 0.0), 6)
            items.append(item)
        return sorted(
            items,
            key=lambda item: (
                -float(item.get(sort_key) or 0.0),
                str(item.get("key") or ""),
            ),
        )[:limit]

    @classmethod
    def _decision_industry(cls, decision: Dict[str, Any]) -> str:
        raw = decision.get("raw_candidate")
        raw_candidate = raw if isinstance(raw, dict) else {}
        for key in (
            "industry",
            "industry_name",
            "industry_board",
            "sector",
            "sector_name",
            "board",
            "board_name",
            "所属行业",
            "行业",
            "板块",
        ):
            value = raw_candidate.get(key)
            if value:
                return str(value).strip()[:64] or "unknown"
        boards = raw_candidate.get("boards") or raw_candidate.get("belong_boards")
        if isinstance(boards, list):
            for item in boards:
                if isinstance(item, dict):
                    value = item.get("name") or item.get("board_name") or item.get("industry")
                    if value:
                        return str(value).strip()[:64] or "unknown"
                elif item:
                    return str(item).strip()[:64] or "unknown"
        return "unknown"

    @staticmethod
    def _counter_payload(counter: Counter[str]) -> Dict[str, int]:
        return {key: int(value) for key, value in sorted(counter.items())}

    @staticmethod
    def _top_counter_items(counter: Counter[str], *, limit: int = 5) -> List[Dict[str, Any]]:
        return [
            {"key": key, "count": int(value)}
            for key, value in sorted(counter.items(), key=lambda item: (-item[1], item[0]))[:limit]
        ]

    @staticmethod
    def _quote_provider(quote: Any) -> str:
        if quote is None:
            return "unavailable"
        for attr in ("provider", "data_source", "source"):
            value = getattr(quote, attr, None)
            if value:
                return str(value)
        return "realtime"

    @staticmethod
    def _candidate_score(candidate: Dict[str, Any]) -> Optional[float]:
        for key in ("score", "final_score", "screen_score"):
            if key in candidate:
                score = _safe_float(candidate.get(key))
                if score is not None:
                    return score
        return None

    @staticmethod
    def _candidate_confidence(candidate: Dict[str, Any]) -> Optional[float]:
        for key in ("confidence", "llm_confidence", "rank_confidence"):
            if key in candidate:
                confidence = _safe_float(candidate.get(key))
                if confidence is not None:
                    return confidence
        return None

    @staticmethod
    def _candidate_name(candidate: Dict[str, Any]) -> Optional[str]:
        for key in ("name", "stock_name", "display_name"):
            value = candidate.get(key)
            if value:
                return str(value)[:128]
        return None

    @staticmethod
    def _candidate_rationale(candidate: Dict[str, Any]) -> Optional[str]:
        for key in ("rationale", "reason", "reason_text", "summary", "llm_reason"):
            value = candidate.get(key)
            if value:
                return str(value)
        tags = candidate.get("tags")
        if isinstance(tags, list) and tags:
            return " / ".join(str(item) for item in tags[:8])
        return None

    def _candidate_pre_trade_risk_reason(
        self,
        candidate: Dict[str, Any],
        settings: VnpyPaperSettings,
    ) -> Optional[str]:
        if settings.auto_exclude_st and self._candidate_has_st_risk(candidate):
            return "st_or_delisting_risk"
        if settings.auto_exclude_suspended and self._candidate_is_suspended(candidate):
            return "suspended_stock"
        if settings.auto_exclude_price_limit and self._candidate_is_price_limit_reached(candidate):
            return "price_limit_reached"
        if settings.auto_min_turnover is not None:
            turnover = self._candidate_turnover(candidate)
            if turnover is not None and turnover < settings.auto_min_turnover:
                return "liquidity_below_threshold"
        return None

    @classmethod
    def _candidate_has_st_risk(cls, candidate: Dict[str, Any]) -> bool:
        if cls._candidate_truthy_flag(
            candidate,
            (
                "is_st",
                "st",
                "is_special_treatment",
                "special_treatment",
                "delisting_risk",
                "is_delisting_risk",
                "退市风险",
                "是否ST",
            ),
            extra_markers=("*st", "退市", "风险警示"),
            negative_markers=("非st", "不是st", "未st", "正常"),
        ):
            return True

        for value in cls._candidate_values(
            candidate,
            ("name", "stock_name", "display_name", "security_name", "股票名称", "名称"),
        ):
            text = str(value or "").strip().upper().replace(" ", "")
            if text.startswith("*ST") or text.startswith("ST") or text.startswith("S*ST"):
                return True
            if "退市" in text or "风险警示" in text:
                return True

        return cls._candidate_text_matches(
            candidate,
            ("risk_status", "stock_status", "special_treatment_status", "退市状态", "风险状态"),
            markers=("*st", "退市", "风险警示", "退市整理"),
            negative_markers=("非st", "不是st", "未st", "正常"),
        )

    @classmethod
    def _candidate_is_suspended(cls, candidate: Dict[str, Any]) -> bool:
        if cls._candidate_truthy_flag(
            candidate,
            (
                "is_suspended",
                "suspended",
                "trading_suspended",
                "is_halt",
                "halted",
                "is_paused",
                "paused",
                "停牌",
                "是否停牌",
            ),
            extra_markers=("停牌", "suspended", "halted", "trading_halt"),
            negative_markers=("未停牌", "不停牌", "正常", "交易中", "active", "trading"),
        ):
            return True

        return cls._candidate_text_matches(
            candidate,
            ("trade_status", "trading_status", "stock_status", "status", "suspension_status", "停牌状态", "交易状态"),
            markers=("停牌", "暂停交易", "suspended", "halted", "trading halt"),
            negative_markers=("未停牌", "不停牌", "正常", "交易中", "active", "trading"),
        )

    @classmethod
    def _candidate_is_price_limit_reached(cls, candidate: Dict[str, Any]) -> bool:
        if cls._candidate_truthy_flag(
            candidate,
            (
                "is_limit_up",
                "is_limit_down",
                "at_limit_up",
                "at_limit_down",
                "price_limit_reached",
                "limit_reached",
                "hit_limit_up",
                "hit_limit_down",
                "sealed_limit",
                "is_sealed_limit",
            ),
            extra_markers=("涨停", "跌停", "limit_up", "limit_down", "price_limit"),
            negative_markers=("未涨停", "未跌停", "未封板", "打开", "正常"),
        ):
            return True

        if cls._candidate_text_matches(
            candidate,
            ("limit_status", "price_limit_status", "涨跌停状态", "涨停状态", "跌停状态"),
            markers=(
                "涨停",
                "跌停",
                "封板",
                "一字板",
                "limit up",
                "limit down",
                "limit_up",
                "limit_down",
                "limit-up",
                "limit-down",
            ),
            negative_markers=("未涨停", "未跌停", "打开", "正常"),
        ):
            return True

        price = cls._candidate_first_float(candidate, ("price", "last_price", "latest_price", "close", "最新价"))
        limit_up = cls._candidate_first_float(
            candidate,
            ("limit_up_price", "high_limit", "upper_limit", "涨停价"),
        )
        limit_down = cls._candidate_first_float(
            candidate,
            ("limit_down_price", "low_limit", "lower_limit", "跌停价"),
        )
        if price is None or price <= 0:
            return False
        if limit_up is not None and limit_up > 0 and price >= limit_up * 0.999:
            return True
        if limit_down is not None and limit_down > 0 and price <= limit_down * 1.001:
            return True
        return False

    @classmethod
    def _candidate_turnover(cls, candidate: Dict[str, Any]) -> Optional[float]:
        for value in cls._candidate_values(
            candidate,
            (
                "amount",
                "turnover",
                "turnover_amount",
                "trading_amount",
                "deal_amount",
                "volume_amount",
                "money",
                "成交额",
                "成交金额",
            ),
        ):
            amount = cls._candidate_amount(value)
            if amount is not None:
                return amount
        return None

    @staticmethod
    def _candidate_values(candidate: Dict[str, Any], keys: Tuple[str, ...]):
        containers: List[Dict[str, Any]] = [candidate]
        for container_key in ("quote", "realtime_quote", "raw"):
            value = candidate.get(container_key)
            if isinstance(value, dict):
                containers.append(value)
        context = candidate.get("dsa_context")
        if isinstance(context, dict):
            containers.append(context)
            for container_key in ("quote", "raw", "fundamentals"):
                value = context.get(container_key)
                if isinstance(value, dict):
                    containers.append(value)

        for container in containers:
            for key in keys:
                if key in container:
                    yield container.get(key)

    @classmethod
    def _candidate_truthy_flag(
        cls,
        candidate: Dict[str, Any],
        keys: Tuple[str, ...],
        *,
        extra_markers: Tuple[str, ...] = (),
        negative_markers: Tuple[str, ...] = (),
    ) -> bool:
        truthy_texts = {"1", "true", "yes", "y", "on", "是", "有", "已", "命中"}
        falsy_texts = {"", "0", "false", "no", "n", "off", "否", "无", "未", "正常", "none", "null"}
        for value in cls._candidate_values(candidate, keys):
            if isinstance(value, bool):
                if value:
                    return True
                continue
            if isinstance(value, (int, float)):
                number = _safe_float(value)
                if number is not None and abs(number) > 1e-8:
                    return True
                continue
            text = str(value or "").strip()
            if not text:
                continue
            normalized = text.lower().replace(" ", "_").replace("-", "_")
            compact = normalized.replace("_", "")
            if normalized in falsy_texts or compact in falsy_texts:
                continue
            if cls._text_contains_any(text, negative_markers, negative_markers=()):
                continue
            if normalized in truthy_texts or compact in truthy_texts:
                return True
            if cls._text_contains_any(text, extra_markers, negative_markers=()):
                return True
        return False

    @classmethod
    def _candidate_text_matches(
        cls,
        candidate: Dict[str, Any],
        keys: Tuple[str, ...],
        *,
        markers: Tuple[str, ...],
        negative_markers: Tuple[str, ...] = (),
    ) -> bool:
        for value in cls._candidate_values(candidate, keys):
            if cls._text_contains_any(value, markers, negative_markers=negative_markers):
                return True
        return False

    @staticmethod
    def _text_contains_any(
        value: Any,
        markers: Tuple[str, ...],
        *,
        negative_markers: Tuple[str, ...],
    ) -> bool:
        text = str(value or "").strip()
        if not text:
            return False
        folded = text.lower()
        if any(marker.lower() in folded for marker in negative_markers if marker):
            return False
        return any(marker.lower() in folded for marker in markers if marker)

    @classmethod
    def _candidate_first_float(cls, candidate: Dict[str, Any], keys: Tuple[str, ...]) -> Optional[float]:
        for value in cls._candidate_values(candidate, keys):
            if isinstance(value, bool):
                continue
            number = _safe_float(value)
            if number is not None:
                return number
        return None

    @staticmethod
    def _candidate_amount(value: Any) -> Optional[float]:
        if isinstance(value, bool):
            return None
        number = _safe_float(value)
        if number is not None:
            return number
        text = str(value or "").strip()
        if not text:
            return None
        multiplier = 1.0
        if "亿" in text:
            multiplier = 100000000.0
        elif "万" in text:
            multiplier = 10000.0
        cleaned = (
            text.replace(",", "")
            .replace("，", "")
            .replace("人民币", "")
            .replace("CNY", "")
            .replace("cny", "")
            .replace("亿元", "")
            .replace("亿", "")
            .replace("万元", "")
            .replace("万", "")
            .replace("元", "")
            .replace("￥", "")
            .replace("¥", "")
            .strip()
        )
        number = _safe_float(cleaned)
        if number is None:
            return None
        return number * multiplier

    def _record_agent_decision(
        self,
        *,
        run_id: int,
        sequence: int,
        candidate: Dict[str, Any],
        symbol: Optional[str],
        settings: VnpyPaperSettings,
        action: str,
        order: Dict[str, Any],
        reason: Optional[str],
        risk_flags: List[str],
        side: str = "buy",
    ) -> Dict[str, Any]:
        audit_order = self._candidate_order_audit_payload(
            order=order,
            candidate=candidate,
            settings=settings,
            side=side,
            reason=reason,
            risk_flags=risk_flags,
        )
        decision = self.agent_repo.record_decision(
            run_id=run_id,
            sequence=sequence,
            symbol=symbol,
            name=self._candidate_name(candidate),
            market=settings.auto_market,
            action=action,
            status=str(order.get("status") or ("filled" if order.get("accepted") else "skipped")),
            reason=reason,
            score=self._candidate_score(candidate),
            confidence=self._candidate_confidence(candidate),
            cash_amount=self._order_base_cash_amount(order, settings=settings),
            quantity=_safe_float(order.get("quantity")),
            price=_safe_float(order.get("price")) or _safe_float(candidate.get("price")),
            trade_id=_safe_int(order.get("trade_id")),
            rationale=self._candidate_rationale(candidate),
            risk_flags=risk_flags,
            order_result=audit_order,
            raw_candidate=candidate,
        )
        planned_cash_amount = self._order_base_cash_amount(order, settings=settings)
        self.agent_repo.record_trade_plan(
            plan_uid=f"plan-{run_id}-{sequence}-{uuid.uuid4().hex[:8]}",
            run_id=run_id,
            decision_id=int(decision["id"]),
            symbol=symbol,
            name=self._candidate_name(candidate),
            market=settings.auto_market,
            side=side,
            status=self._trade_plan_status(order),
            execution_mode=settings.auto_execution_mode,
            planned_cash_amount=planned_cash_amount,
            planned_quantity=_safe_float(order.get("quantity")),
            planned_price=_safe_float(order.get("price")) or _safe_float(candidate.get("price")),
            submitted_quantity=_safe_float(order.get("quantity")) if order.get("accepted") else None,
            submitted_price=_safe_float(order.get("price")) if order.get("accepted") else None,
            trade_id=_safe_int(order.get("trade_id")),
            skip_reason=reason if not order.get("accepted") else None,
            risk_flags=risk_flags,
            order_result=audit_order,
        )
        return decision

    def _candidate_order_audit_payload(
        self,
        *,
        order: Dict[str, Any],
        candidate: Dict[str, Any],
        settings: VnpyPaperSettings,
        side: str,
        reason: Optional[str],
        risk_flags: List[str],
    ) -> Dict[str, Any]:
        planned_cash_amount = self._order_base_cash_amount(order, settings=settings)
        planned_price = _safe_float(order.get("price")) or _safe_float(candidate.get("price"))
        planned_quantity = _safe_float(order.get("quantity"))
        position_quantity = _safe_float(candidate.get("position_quantity"))
        sell_position_pct = _safe_float(candidate.get("sell_position_pct"))
        trade_status = self._trade_plan_status(order)
        resolved_reason = reason or order.get("reason")
        benign_plan_reasons = {"dry_run", "pending_approval"}
        resolved_reason_text = str(resolved_reason or "").strip()
        blocking_risk_flags = [
            str(flag)
            for flag in risk_flags or []
            if str(flag or "").strip() not in benign_plan_reasons
        ]
        if blocking_risk_flags or (resolved_reason_text and resolved_reason_text not in benign_plan_reasons):
            review_status = "blocked"
        elif trade_status == "failed":
            review_status = "failed_execution"
        else:
            review_status = "passed"
        candidate_missing_fields = self._candidate_quality_text_list(candidate.get("missing_fields"))
        candidate_data_sources = self._candidate_quality_text_list(candidate.get("data_sources"))
        candidate_quality_status = str(
            candidate.get("data_quality")
            or candidate.get("quality_status")
            or candidate.get("dataQuality")
            or "unknown"
        ).strip() or "unknown"
        risk_review = {
            "status": review_status,
            "reason": resolved_reason_text or None,
            "risk_flags": list(risk_flags or []),
            "review_source": "rule_based_auto_trade",
            "reviewed_at": _utc_now_iso(),
            "candidate_data_quality": {
                "status": candidate_quality_status,
                "missing_fields": candidate_missing_fields,
                "data_sources": candidate_data_sources,
            },
            "gates": {
                "min_score": settings.auto_min_score,
                "skip_existing_positions": settings.auto_skip_existing_positions,
                "max_positions": settings.auto_max_positions,
                "candidate_filters_enabled": {
                    "exclude_st": settings.auto_exclude_st,
                    "exclude_suspended": settings.auto_exclude_suspended,
                    "exclude_price_limit": settings.auto_exclude_price_limit,
                    "min_turnover": settings.auto_min_turnover,
                },
                "portfolio_limits_enabled": any(
                    value is not None
                    for value in (
                        settings.auto_max_single_position_value,
                        settings.auto_max_total_position_value,
                        settings.auto_max_total_position_pct,
                        settings.auto_max_industry_position_value,
                        settings.auto_max_industry_position_pct,
                        settings.auto_daily_max_orders,
                        settings.auto_daily_budget,
                        settings.auto_min_cash_balance,
                        settings.auto_max_drawdown_pct,
                    )
                ),
            },
        }
        audit_order = dict(order)
        audit_order["position_plan"] = {
            "side": side,
            "market": settings.auto_market,
            "symbol": self._normalize_symbol(candidate.get("code") or candidate.get("symbol") or "") or order.get("symbol"),
            "planned_cash_amount": planned_cash_amount,
            "planned_quantity": planned_quantity,
            "planned_price": planned_price,
            "sizing_method": (
                "position_pct"
                if side == "sell" and position_quantity is not None
                else "cash_per_order"
            ),
            "cash_per_order": settings.auto_cash_per_order,
            "position_quantity": position_quantity,
            "sell_position_pct": sell_position_pct if side == "sell" else None,
            "execution_mode": settings.auto_execution_mode,
            "dedup_scope": "trade_date_strategy_market_symbol",
        }
        audit_order["risk_review"] = risk_review
        audit_order["agent_review"] = self._candidate_agent_review_payload(
            candidate=candidate,
            settings=settings,
            side=side,
            trade_status=trade_status,
            resolved_reason=resolved_reason_text or None,
            risk_review=risk_review,
        )
        llm_review = order.get("llm_review")
        if isinstance(llm_review, dict):
            audit_order["llm_review"] = llm_review
        return audit_order

    def _candidate_agent_review_payload(
        self,
        *,
        candidate: Dict[str, Any],
        settings: VnpyPaperSettings,
        side: str,
        trade_status: str,
        resolved_reason: Optional[str],
        risk_review: Dict[str, Any],
    ) -> Dict[str, Any]:
        score = self._candidate_score(candidate)
        confidence = self._candidate_confidence(candidate)
        rationale = self._candidate_rationale(candidate)
        quality = risk_review.get("candidate_data_quality")
        if not isinstance(quality, dict):
            quality = {}
        quality_status = str(quality.get("status") or "unknown").strip().lower() or "unknown"
        missing_fields = self._candidate_quality_text_list(quality.get("missing_fields"))
        risk_status = str(risk_review.get("status") or "unknown").strip().lower() or "unknown"
        checks: List[Dict[str, Any]] = []

        checks.append(
            {
                "key": "risk_review",
                "status": risk_status,
                "reason": risk_review.get("reason"),
                "risk_flags": risk_review.get("risk_flags") or [],
            }
        )

        data_quality_check_status = "passed"
        data_quality_reason = None
        if quality_status in {"stale", "unavailable", "failed", "error"}:
            data_quality_check_status = "warning"
            data_quality_reason = f"data_quality_{quality_status}"
        elif missing_fields:
            data_quality_check_status = "warning"
            data_quality_reason = "data_quality_missing_fields"
        elif quality_status in {"partial", "unknown"}:
            data_quality_check_status = "warning"
            data_quality_reason = f"data_quality_{quality_status}"
        checks.append(
            {
                "key": "data_quality",
                "status": data_quality_check_status,
                "value": quality_status,
                "missing_fields": missing_fields,
                "reason": data_quality_reason,
            }
        )

        score_check_status = "passed"
        score_reason = None
        if score is None:
            score_check_status = "warning"
            score_reason = "score_missing"
        elif settings.auto_min_score is not None and score < settings.auto_min_score:
            score_check_status = "blocked"
            score_reason = "score_below_threshold"
        checks.append(
            {
                "key": "score",
                "status": score_check_status,
                "value": score,
                "threshold": settings.auto_min_score,
                "reason": score_reason,
            }
        )

        rationale_check_status = "passed" if rationale else "warning"
        checks.append(
            {
                "key": "rationale",
                "status": rationale_check_status,
                "reason": None if rationale else "rationale_missing",
            }
        )

        confidence_check_status = "passed" if confidence is not None else "unknown"
        checks.append(
            {
                "key": "confidence",
                "status": confidence_check_status,
                "value": confidence,
            }
        )

        order_check_status = "passed"
        if trade_status == "failed":
            order_check_status = "failed_execution"
        elif trade_status == "skipped":
            order_check_status = "blocked"
        checks.append(
            {
                "key": "order_intent",
                "status": order_check_status,
                "value": trade_status,
                "reason": resolved_reason,
            }
        )

        check_statuses = {str(item.get("status") or "unknown") for item in checks}
        if "failed_execution" in check_statuses:
            overall_status = "failed_execution"
        elif "blocked" in check_statuses or risk_status == "blocked":
            overall_status = "blocked"
        elif "warning" in check_statuses:
            overall_status = "warning"
        else:
            overall_status = "passed"

        warning_reasons = [
            str(item.get("reason"))
            for item in checks
            if item.get("status") in {"warning", "blocked", "failed_execution"} and item.get("reason")
        ]
        benign_plan_reasons = {"dry_run", "pending_approval"}
        primary_reason = resolved_reason if resolved_reason not in benign_plan_reasons else None
        summary_reason = primary_reason or (warning_reasons[0] if warning_reasons else None)
        if overall_status == "passed":
            summary = "Pre-trade Agent review passed"
        elif overall_status == "warning":
            summary = f"Pre-trade Agent review warning: {summary_reason or 'review_warning'}"
        elif overall_status == "failed_execution":
            summary = f"Pre-trade Agent review failed during execution: {summary_reason or 'execution_failed'}"
        else:
            summary = f"Pre-trade Agent review blocked: {summary_reason or 'risk_gate_blocked'}"

        return {
            "schema_version": 1,
            "status": overall_status,
            "reviewer": "rule_agent_v1",
            "review_source": "rule_based_pre_trade_agent",
            "side": side,
            "trade_status": trade_status,
            "reason": summary_reason,
            "summary": summary,
            "checks": checks,
            "reviewed_at": _utc_now_iso(),
            "next_reviewer": "llm_reviewer_optional",
        }

    @staticmethod
    def _candidate_quality_text_list(value: Any) -> List[str]:
        if value is None:
            return []
        if isinstance(value, str):
            text = value.strip()
            return [text] if text else []
        if not isinstance(value, list):
            text = str(value).strip()
            return [text] if text else []
        return [text for item in value if (text := str(item).strip())]

    @staticmethod
    def _trade_plan_status(order: Dict[str, Any]) -> str:
        status = str(order.get("status") or "").strip().lower()
        if status in {"planned", "submitted", "part_filled", "cancel_requested", "filled", "skipped", "failed"}:
            return status
        return "filled" if order.get("accepted") else "skipped"

    @classmethod
    def _trade_plan_retry_payload(cls, plan: Dict[str, Any]) -> Dict[str, Any]:
        order_result = plan.get("order_result") if isinstance(plan.get("order_result"), dict) else {}
        retry_payload = order_result.get("retry") if isinstance(order_result, dict) else None
        if not isinstance(retry_payload, dict):
            raw_payload = order_result.get("raw") if isinstance(order_result, dict) else None
            retry_payload = raw_payload.get("retry") if isinstance(raw_payload, dict) else None
        return dict(retry_payload) if isinstance(retry_payload, dict) else {}

    @classmethod
    def _validate_trade_plan_retry(cls, plan: Dict[str, Any]) -> Dict[str, Any]:
        retry_payload = cls._trade_plan_retry_payload(plan)
        status = str(plan.get("status") or "").strip().lower()
        order_result = plan.get("order_result") if isinstance(plan.get("order_result"), dict) else {}
        reason = (
            str(plan.get("skip_reason") or "").strip()
            or str(order_result.get("reason") or "").strip()
        )
        if status == "skipped" and reason not in RETRYABLE_TRADE_PLAN_SKIP_REASONS:
            raise ValueError("trade_plan_not_retryable")
        if status == "failed" and reason in NON_RETRYABLE_TRADE_PLAN_FAILURE_REASONS:
            raise ValueError("trade_plan_not_retryable")

        max_attempts = _safe_int(retry_payload.get("max_attempts")) or TRADE_PLAN_RETRY_MAX_ATTEMPTS
        max_attempts = max(1, min(20, int(max_attempts)))
        attempt_count = _safe_int(retry_payload.get("attempt_count")) or 0
        if attempt_count >= max_attempts:
            raise ValueError("trade_plan_retry_limit_reached")

        next_retry_after = cls._parse_utc_datetime(retry_payload.get("next_retry_after"))
        now = datetime.now(timezone.utc)
        if next_retry_after is not None and now < next_retry_after:
            raise ValueError("trade_plan_retry_cooldown_active")

        return {
            "attempt_count": attempt_count + 1,
            "previous_attempt_count": attempt_count,
            "max_attempts": max_attempts,
            "cooldown_seconds": TRADE_PLAN_RETRY_COOLDOWN_SECONDS,
            "started_at": cls._format_utc_datetime(now),
            "previous_status": status or None,
            "previous_reason": reason or None,
        }

    def _attach_trade_plan_retry_metadata(
        self,
        *,
        plan: Dict[str, Any],
        order: Dict[str, Any],
        retry_context: Dict[str, Any],
    ) -> Dict[str, Any]:
        status = self._trade_plan_status(order)
        retry_payload = {
            **retry_context,
            "finished_at": _utc_now_iso(),
            "last_status": status,
            "last_reason": str(order.get("reason") or "").strip() or None,
            "last_source": str(order.get("source") or "").strip() or None,
            "plan_uid": str(plan.get("plan_uid") or "").strip() or None,
        }
        if status in {"failed", "skipped"}:
            retry_payload["next_retry_after"] = self._format_utc_datetime(
                datetime.now(timezone.utc) + timedelta(seconds=TRADE_PLAN_RETRY_COOLDOWN_SECONDS)
            )
        else:
            retry_payload["next_retry_after"] = None
        return self._with_trade_plan_retry_payload(order, retry_payload)

    @classmethod
    def _preserve_trade_plan_retry_metadata(
        cls,
        *,
        plan: Dict[str, Any],
        order: Dict[str, Any],
        status: str,
        reason: Optional[str],
    ) -> Dict[str, Any]:
        existing_retry = cls._trade_plan_retry_payload(plan)
        if not existing_retry:
            return order
        if isinstance(order.get("retry"), dict):
            return order

        retry_payload = {
            **existing_retry,
            "finished_at": _utc_now_iso(),
            "last_status": status,
            "last_reason": str(reason or "").strip() or None,
        }
        if status in {"failed", "skipped"}:
            retry_payload["next_retry_after"] = cls._format_utc_datetime(
                datetime.now(timezone.utc) + timedelta(seconds=TRADE_PLAN_RETRY_COOLDOWN_SECONDS)
            )
        elif status == "filled":
            retry_payload["next_retry_after"] = None
        return cls._with_trade_plan_retry_payload(order, retry_payload)

    @staticmethod
    def _with_trade_plan_retry_payload(order: Dict[str, Any], retry_payload: Dict[str, Any]) -> Dict[str, Any]:
        updated = dict(order)
        raw = updated.get("raw") if isinstance(updated.get("raw"), dict) else {}
        updated["raw"] = {**raw, "retry": retry_payload}
        updated["retry"] = retry_payload
        return updated

    @staticmethod
    def _format_utc_datetime(value: datetime) -> str:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    @staticmethod
    def _parse_utc_datetime(value: Any) -> Optional[datetime]:
        text = str(value or "").strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _parse_db_datetime(value: Any) -> Optional[datetime]:
        if value is None:
            return None
        local_tz = datetime.now().astimezone().tzinfo
        if isinstance(value, datetime):
            parsed = value
        else:
            text = str(value or "").strip()
            if not text:
                return None
            if text.endswith("Z"):
                text = f"{text[:-1]}+00:00"
            try:
                parsed = datetime.fromisoformat(text)
            except ValueError:
                return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=local_tz)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _dedup_hash(value: str) -> str:
        import hashlib

        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _coerce_trade_date(value: Optional[Any]) -> date:
        if value is None or value == "":
            return date.today()
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        text = str(value).strip()
        if not text:
            return date.today()
        try:
            return date.fromisoformat(text[:10])
        except ValueError as exc:
            raise ValueError("trade_date must be an ISO date") from exc

    @staticmethod
    def _build_trade_note(*, source: str, note: Optional[str], price_source: str) -> str:
        parts = [f"vn.py paper", f"source={source}", f"price={price_source}"]
        if note:
            parts.append(str(note).strip())
        return " | ".join(part for part in parts if part)[:255]

    @staticmethod
    def _skipped_order(
        *,
        symbol: Optional[str],
        side: str,
        quantity: Optional[float] = None,
        price: Optional[float] = None,
        cash_amount: Optional[float] = None,
        reason: str,
        message: Optional[str] = None,
        raw: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return {
            "accepted": False,
            "status": "skipped",
            "trade_id": None,
            "account_id": None,
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "price": price,
            "cash_amount": cash_amount,
            "source": "vnpy_local_paper_ledger",
            "message": message or reason,
            "reason": reason,
            "raw": raw or {},
        }

    @staticmethod
    def _failed_order(
        *,
        symbol: Optional[str],
        side: str,
        quantity: Optional[float] = None,
        price: Optional[float] = None,
        cash_amount: Optional[float] = None,
        reason: str,
        message: Optional[str] = None,
        raw: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return {
            "accepted": False,
            "status": "failed",
            "trade_id": None,
            "account_id": None,
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "price": price,
            "cash_amount": cash_amount,
            "source": "vnpy_main_engine",
            "message": message or reason,
            "reason": reason,
            "raw": raw or {},
        }

    def _planned_order(
        self,
        *,
        symbol: str,
        market: str,
        cash_amount: float,
        price: Optional[float],
        reason: str = "dry_run",
        raw: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        planned_price = _safe_float(price)
        planned_quantity = None
        planned_cash_amount = _safe_float(cash_amount) or 0.0
        if planned_price is not None and planned_price > 0:
            quantity = self._resolve_order_quantity(
                market=market,
                side="buy",
                quantity=None,
                cash_amount=planned_cash_amount,
                price=planned_price,
            )
            planned_quantity = quantity if quantity > 0 else None
        return {
            "accepted": False,
            "status": "planned",
            "trade_id": None,
            "account_id": None,
            "symbol": symbol,
            "side": "buy",
            "quantity": planned_quantity,
            "price": planned_price,
            "cash_amount": planned_cash_amount,
            "source": "vnpy_local_paper_ledger",
            "message": (
                "Manual approval is required before submitting this paper order."
                if reason == "pending_approval"
                else "Dry-run generated a paper trade plan without submitting an order."
            ),
            "reason": reason,
            "raw": raw or {},
        }


def build_vnpy_paper_trading_background_tasks(
    *,
    vnpy_main_engine: Optional[Any] = None,
    vnpy_event_engine: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    """Return scheduler background task entries for automatic paper trading."""

    service = VnpyPaperTradingService(
        vnpy_main_engine=vnpy_main_engine,
        vnpy_event_engine=vnpy_event_engine,
    )
    settings = service.get_settings()
    if not settings.enabled:
        return []

    def run_auto_trade() -> Dict[str, Any]:
        result = service.run_auto_trade_once()
        logger.info(
            "vn.py paper auto trade finished: submitted=%s skipped=%s reason=%s",
            result.get("submitted_count"),
            result.get("skipped_count"),
            result.get("reason"),
        )
        return result

    def run_auto_retry() -> Dict[str, Any]:
        result = service.retry_due_trade_plans()
        logger.info(
            "vn.py paper auto retry finished: attempted=%s submitted=%s skipped=%s failed=%s",
            result.get("attempted_count"),
            result.get("submitted_count"),
            result.get("skipped_count"),
            result.get("failed_count"),
        )
        return result

    retry_interval = max(
        TRADE_PLAN_RETRY_COOLDOWN_SECONDS,
        min(int(settings.auto_interval_minutes) * 60, TRADE_PLAN_AUTO_RETRY_INTERVAL_SECONDS),
    )
    tasks = [
        {
            "task": run_auto_retry,
            "interval_seconds": retry_interval,
            "run_immediately": True,
            "name": "vnpy_paper_auto_retry",
        }
    ]
    if settings.auto_trade_enabled:
        tasks.insert(
            0,
            {
                "task": run_auto_trade,
                "interval_seconds": int(settings.auto_interval_minutes) * 60,
                "run_immediately": False,
                "name": "vnpy_paper_auto_trade",
                "initial_delay_seconds": _auto_trade_initial_delay_seconds(service, settings),
            },
        )
    return tasks


def _auto_trade_initial_delay_seconds(
    service: VnpyPaperTradingService,
    settings: VnpyPaperSettings,
) -> Optional[int]:
    if not settings.auto_trade_time_gate_enabled or settings.auto_execution_mode in {"dry_run", "manual_approval"}:
        return None
    try:
        window = service._trading_window_diagnostics(settings)
    except Exception as exc:  # pragma: no cover - diagnostics should already be defensive.
        logger.warning("Failed to align vn.py paper auto trade schedule to trading window: %s", exc)
        return None
    if window.get("is_market_open_now") is True:
        return None
    next_open = VnpyPaperTradingService._parse_utc_datetime(window.get("next_open_at"))
    if next_open is None:
        return None
    delay = math.ceil((next_open - datetime.now(timezone.utc)).total_seconds())
    if delay <= 0:
        return None
    return delay
