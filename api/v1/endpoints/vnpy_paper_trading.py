# -*- coding: utf-8 -*-
"""vn.py-style local paper trading API routes."""

from __future__ import annotations

import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, Request

from api import __version__ as API_VERSION
from api.deps import get_runtime_scheduler_service
from api.v1.errors import api_error
from api.v1.schemas.common import ErrorResponse
from api.v1.schemas.vnpy_paper_trading import (
    VnpyPaperAccountListResponse,
    VnpyPaperArchivedAccountCleanupRequest,
    VnpyPaperArchivedAccountCleanupResponse,
    VnpyPaperAgentBacktestRequest,
    VnpyPaperAgentBacktestResponse,
    VnpyPaperAgentCrossRunQualityResponse,
    VnpyPaperAgentDailySummaryResponse,
    VnpyPaperAgentDataQualityTrendsResponse,
    VnpyPaperAgentRunFeedbackRequest,
    VnpyPaperAgentRunFeedbackResponse,
    VnpyPaperAgentRunRecapRequest,
    VnpyPaperAgentRunRecapResponse,
    VnpyPaperAgentRunDetail,
    VnpyPaperAgentRunExportResponse,
    VnpyPaperAgentRunListResponse,
    VnpyPaperAutoRunRequest,
    VnpyPaperAutoRunResponse,
    VnpyPaperOrderRequest,
    VnpyPaperOrderResult,
    VnpyPaperPerformanceResponse,
    VnpyPaperSettingsUpdate,
    VnpyPaperStatusResponse,
    VnpyPaperTaskEventListResponse,
    VnpyPaperTaskEventSummaryResponse,
    VnpyPaperTaskMetricsResponse,
    VnpyPaperTaskHealthResponse,
    VnpyPaperTradePlanRecoveryRunResponse,
    VnpyPaperTradePlanRecoverySummaryResponse,
    VnpyPaperVnpyAccountCallbackRequest,
    VnpyPaperVnpyOrderCallbackRequest,
    VnpyPaperVnpyPositionsCallbackRequest,
    VnpyPaperVnpySyncResult,
    VnpyPaperVnpyTradeCallbackRequest,
)
from src.repositories.stock_selection_agent_repo import StockSelectionAgentRepository
from src.services.portfolio_service import PortfolioBusyError
from src.services.runtime_scheduler import RuntimeSchedulerService
from src.services.stock_selection_agent_backtest_service import StockSelectionAgentBacktestService
from src.services.vnpy_paper_trading_service import VnpyPaperTradingService

logger = logging.getLogger(__name__)

router = APIRouter()

VNPY_PAPER_STATUS_CONTRACT_VERSION = 3
_PROCESS_STARTED_AT = datetime.now(timezone.utc).isoformat()


def _backend_runtime_status() -> Dict[str, Any]:
    build_sources = (
        ("DSA_BUILD_ID", os.getenv("DSA_BUILD_ID")),
        ("GITHUB_SHA", os.getenv("GITHUB_SHA")),
        ("RENDER_GIT_COMMIT", os.getenv("RENDER_GIT_COMMIT")),
        ("RAILWAY_GIT_COMMIT_SHA", os.getenv("RAILWAY_GIT_COMMIT_SHA")),
    )
    build_source, build_id = next(
        ((source, str(value).strip()) for source, value in build_sources if str(value or "").strip()),
        (None, None),
    )
    return {
        "api_version": API_VERSION,
        "vnpy_paper_contract_version": VNPY_PAPER_STATUS_CONTRACT_VERSION,
        "build_id": build_id,
        "build_source": build_source,
        "python_version": ".".join(str(item) for item in sys.version_info[:3]),
        "process_started_at": _PROCESS_STARTED_AT,
    }


def _service(request: Optional[Request] = None) -> VnpyPaperTradingService:
    app_state = getattr(getattr(request, "app", None), "state", None) if request is not None else None
    vnpy_main_engine = getattr(app_state, "vnpy_main_engine", None) if app_state is not None else None
    vnpy_event_engine = getattr(app_state, "vnpy_event_engine", None) if app_state is not None else None
    if vnpy_event_engine is None and vnpy_main_engine is not None:
        vnpy_event_engine = getattr(vnpy_main_engine, "event_engine", None)
    if vnpy_main_engine is not None:
        return VnpyPaperTradingService(
            vnpy_main_engine=vnpy_main_engine,
            vnpy_event_engine=vnpy_event_engine,
        )
    if vnpy_event_engine is not None:
        return VnpyPaperTradingService(vnpy_event_engine=vnpy_event_engine)
    return VnpyPaperTradingService()


def _agent_repo() -> StockSelectionAgentRepository:
    return StockSelectionAgentRepository()


def _bad_request(exc: Exception) -> HTTPException:
    return api_error(400, "validation_error", str(exc))


def _conflict_error(exc: Exception) -> HTTPException:
    return api_error(409, "portfolio_busy", str(exc))


def _internal_error(message: str, exc: Exception) -> HTTPException:
    logger.error("%s: %s", message, exc, exc_info=True)
    return api_error(500, "internal_error", f"{message}: {str(exc)}")


def _with_scheduler_status(
    payload: Dict[str, Any],
    scheduler: RuntimeSchedulerService,
) -> Dict[str, Any]:
    try:
        scheduler_status = scheduler.status()
    except Exception as exc:  # pragma: no cover - defensive status decoration.
        logger.warning("Get runtime scheduler status failed: %s", exc)
        scheduler_status = {}
    if not isinstance(scheduler_status, dict):
        scheduler_status = {}
    payload["scheduler"] = scheduler_status
    diagnostics = payload.get("diagnostics") if isinstance(payload.get("diagnostics"), dict) else {}
    diagnostics = dict(diagnostics)
    diagnostics["auto_trade_readiness"] = _auto_trade_readiness_payload(payload)
    payload["diagnostics"] = diagnostics
    return payload


def _with_system_health(payload: Dict[str, Any]) -> Dict[str, Any]:
    diagnostics = payload.get("diagnostics") if isinstance(payload.get("diagnostics"), dict) else {}
    diagnostics = dict(diagnostics)
    diagnostics["system_health"] = _system_health_payload(payload)
    payload["diagnostics"] = diagnostics
    return payload


def _with_backend_status(payload: Dict[str, Any]) -> Dict[str, Any]:
    diagnostics = payload.get("diagnostics") if isinstance(payload.get("diagnostics"), dict) else {}
    diagnostics = dict(diagnostics)
    diagnostics["backend"] = _backend_runtime_status()
    payload["diagnostics"] = diagnostics
    return payload


def _with_alphasift_status(payload: Dict[str, Any]) -> Dict[str, Any]:
    diagnostics = payload.get("diagnostics") if isinstance(payload.get("diagnostics"), dict) else {}
    diagnostics = dict(diagnostics)
    try:
        from src.config import get_config
        from src.services.alphasift_service import AlphaSiftService

        status = AlphaSiftService(config=get_config()).status()
        diagnostics["alphasift"] = {
            "enabled": bool(status.get("enabled")),
            "available": bool(status.get("available")),
            "version": status.get("version"),
            "contract_version": status.get("contract_version"),
            "strategy_count": status.get("strategy_count"),
            "source_health": status.get("source_health"),
            "diagnostics": status.get("diagnostics"),
        }
    except Exception as exc:  # pragma: no cover - status diagnostics must stay readable.
        logger.warning("Get AlphaSift dependency status failed: %s", exc)
        diagnostics["alphasift"] = {
            "enabled": None,
            "available": False,
            "error": str(exc),
        }
    payload["diagnostics"] = diagnostics
    return payload


_TASK_HEALTH_LABELS = {
    "vnpy_paper_auto_trade": "定时自动买入",
    "vnpy_paper_auto_retry": "自动恢复扫描",
}
_EXPECTED_VNPY_PAPER_TASKS = tuple(_TASK_HEALTH_LABELS.keys())


def _status_tone(status: str) -> str:
    if status == "ready":
        return "success"
    if status == "blocked":
        return "danger"
    if status == "warning":
        return "warning"
    return "info"


def _scheduler_loop_running(scheduler_status: Dict[str, Any]) -> bool:
    if "loop_running" in scheduler_status:
        return bool(scheduler_status.get("loop_running"))
    return bool(scheduler_status.get("enabled"))


def _health_status_rank(status: str) -> int:
    if status == "blocked":
        return 3
    if status == "warning":
        return 2
    if status == "disabled":
        return 1
    return 0


def _snapshot_valuation_health(status_payload: Dict[str, Any]) -> Dict[str, Any]:
    diagnostics = status_payload.get("diagnostics") if isinstance(status_payload.get("diagnostics"), dict) else {}
    snapshot_requested = diagnostics.get("snapshot_requested")
    if snapshot_requested is False:
        return {
            "status": "disabled",
            "reason": "snapshot_not_requested",
            "detail": "轻量状态未拉取持仓估值",
            "required": False,
        }
    snapshot_error = diagnostics.get("snapshot_error")
    if snapshot_error:
        return {
            "status": "warning",
            "reason": "snapshot_error",
            "detail": str(snapshot_error),
            "required": False,
        }
    snapshot = status_payload.get("snapshot") if isinstance(status_payload.get("snapshot"), dict) else {}
    if not snapshot:
        return {
            "status": "disabled",
            "reason": "snapshot_unavailable",
            "detail": "本次状态未返回持仓快照",
            "required": False,
        }
    limitations = []
    raw_limitations = snapshot.get("limitations")
    if isinstance(raw_limitations, list):
        limitations.extend(item for item in raw_limitations if isinstance(item, dict))
    stale_count = 0
    missing_count = 0
    unknown_count = 0
    available_count = 0
    fresh_count = 0
    account_count = 0
    position_count = 0
    source_counts: Dict[str, int] = {}
    provider_counts: Dict[str, int] = {}
    missing_symbols: List[str] = []
    stale_symbols: List[str] = []
    unknown_symbols: List[str] = []
    price_dates: List[str] = []
    for account in list(snapshot.get("accounts") or []):
        if not isinstance(account, dict):
            continue
        account_count += 1
        raw_account_limitations = account.get("limitations")
        if isinstance(raw_account_limitations, list):
            limitations.extend(item for item in raw_account_limitations if isinstance(item, dict))
        for position in list(account.get("positions") or []):
            if not isinstance(position, dict):
                continue
            position_count += 1
            symbol = str(position.get("symbol") or "").strip()
            source = str(position.get("price_source") or "unknown").strip() or "unknown"
            provider = str(position.get("price_provider") or "unknown").strip() or "unknown"
            source_counts[source] = source_counts.get(source, 0) + 1
            provider_counts[provider] = provider_counts.get(provider, 0) + 1
            price_date = str(position.get("price_date") or "").strip()
            if price_date:
                price_dates.append(price_date)
            price_available = position.get("price_available")
            if price_available is False:
                missing_count += 1
                if symbol:
                    missing_symbols.append(symbol)
            elif price_available is True:
                available_count += 1
            else:
                unknown_count += 1
                if symbol:
                    unknown_symbols.append(symbol)
            if position.get("price_stale") is True:
                stale_count += 1
                if symbol:
                    stale_symbols.append(symbol)
            elif price_available is True:
                fresh_count += 1
    coverage_pct = round(available_count / position_count * 100.0, 2) if position_count else 100.0
    fresh_coverage_pct = round(fresh_count / position_count * 100.0, 2) if position_count else 100.0
    source_summary = "、".join(
        f"{key}={value}" for key, value in sorted(source_counts.items())
    ) or "无持仓"
    health_fields = {
        "position_count": position_count,
        "account_count": account_count,
        "available_count": available_count,
        "fresh_count": fresh_count,
        "missing_count": missing_count,
        "unknown_count": unknown_count,
        "stale_count": stale_count,
        "coverage_pct": coverage_pct,
        "fresh_coverage_pct": fresh_coverage_pct,
        "source_counts": dict(sorted(source_counts.items())),
        "provider_counts": dict(sorted(provider_counts.items())),
        "missing_symbols": missing_symbols,
        "stale_symbols": stale_symbols,
        "unknown_symbols": unknown_symbols,
        "oldest_price_date": min(price_dates) if price_dates else None,
        "latest_price_date": max(price_dates) if price_dates else None,
    }
    if missing_count or stale_count or unknown_count or limitations:
        parts = []
        if missing_count:
            parts.append(f"缺价 {missing_count} 笔")
        if stale_count:
            parts.append(f"陈旧价格 {stale_count} 笔")
        if unknown_count:
            parts.append(f"价格状态未知 {unknown_count} 笔")
        if limitations:
            parts.append(f"限制 {len(limitations)} 项")
        return {
            "status": "warning",
            "reason": "valuation_degraded",
            "detail": (
                f"覆盖 {available_count}/{position_count}（{coverage_pct}%），"
                f"新鲜 {fresh_count}/{position_count}（{fresh_coverage_pct}%）；"
                f"{'；'.join(parts)}；来源 {source_summary}"
            ),
            "required": False,
            **health_fields,
        }
    return {
        "status": "ready",
        "reason": "valuation_ready",
        "detail": (
            f"覆盖 {available_count}/{position_count}（{coverage_pct}%），"
            f"新鲜 {fresh_count}/{position_count}（{fresh_coverage_pct}%）；"
            f"来源 {source_summary}"
        ),
        "required": False,
        **health_fields,
    }


def _system_health_payload(status_payload: Dict[str, Any]) -> Dict[str, Any]:
    diagnostics = status_payload.get("diagnostics") if isinstance(status_payload.get("diagnostics"), dict) else {}
    settings = status_payload.get("settings") if isinstance(status_payload.get("settings"), dict) else {}
    scheduler_status = status_payload.get("scheduler") if isinstance(status_payload.get("scheduler"), dict) else {}
    readiness = diagnostics.get("auto_trade_readiness") if isinstance(diagnostics.get("auto_trade_readiness"), dict) else {}
    timing_alignment = readiness.get("timing_alignment") if isinstance(readiness.get("timing_alignment"), dict) else {}
    alphasift = diagnostics.get("alphasift") if isinstance(diagnostics.get("alphasift"), dict) else {}
    trading_window = diagnostics.get("trading_window") if isinstance(diagnostics.get("trading_window"), dict) else {}
    vnpy_bridge = diagnostics.get("vnpy_bridge") if isinstance(diagnostics.get("vnpy_bridge"), dict) else {}
    vnpy_runtime = diagnostics.get("vnpy_runtime") if isinstance(diagnostics.get("vnpy_runtime"), dict) else {}
    backend = diagnostics.get("backend") if isinstance(diagnostics.get("backend"), dict) else {}

    auto_trade_enabled = bool(settings.get("auto_trade_enabled"))
    execution_mode = str(settings.get("auto_execution_mode") or status_payload.get("mode") or "paper")
    time_gate_enforced = bool(trading_window.get("time_gate_enforced"))
    market_open_now = trading_window.get("is_market_open_now") is True
    components: List[Dict[str, Any]] = []

    def add_component(
        *,
        key: str,
        label: str,
        status: str,
        reason: str,
        detail: str,
        required: bool = True,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        item = {
            "key": key,
            "label": label,
            "status": status,
            "reason": reason,
            "detail": detail,
            "required": required,
            "tone": _status_tone(status),
        }
        if extra:
            item.update(extra)
        components.append(item)

    paper_enabled = bool(settings.get("enabled", status_payload.get("enabled", False)))
    paper_available = bool(status_payload.get("available", paper_enabled))
    add_component(
        key="paper_ledger",
        label="本地账本",
        status="ready" if paper_enabled and paper_available else "blocked",
        reason="paper_ledger_ready" if paper_enabled and paper_available else "paper_ledger_unavailable",
        detail="本地 paper 账本可写入 Portfolio" if paper_enabled and paper_available else "模拟交易关闭或本地账本不可用",
    )

    backend_contract_version = int(backend.get("vnpy_paper_contract_version") or 0)
    backend_build_id = str(backend.get("build_id") or "local").strip()
    backend_compatible = backend_contract_version >= VNPY_PAPER_STATUS_CONTRACT_VERSION
    add_component(
        key="backend_version",
        label="后端版本",
        status="ready" if backend_compatible else "warning",
        reason="backend_contract_compatible" if backend_compatible else "backend_contract_unknown",
        detail=(
            f"API {backend.get('api_version') or '-'} · contract {backend_contract_version or '-'} · "
            f"build {backend_build_id[:16]} · Python {backend.get('python_version') or '-'}"
        ),
        required=False,
        extra={
            "api_version": backend.get("api_version"),
            "contract_version": backend_contract_version or None,
            "expected_contract_version": VNPY_PAPER_STATUS_CONTRACT_VERSION,
            "build_id": backend.get("build_id"),
            "build_source": backend.get("build_source"),
            "python_version": backend.get("python_version"),
            "process_started_at": backend.get("process_started_at"),
        },
    )

    alphasift_enabled = bool(alphasift.get("enabled", True)) if alphasift else True
    alphasift_available = bool(alphasift.get("available")) if alphasift else False
    add_component(
        key="selection_source",
        label="选股来源",
        status=(
            "disabled"
            if not auto_trade_enabled
            else "ready"
            if alphasift_enabled and alphasift_available
            else "blocked"
        ),
        reason=(
            "auto_trade_disabled"
            if not auto_trade_enabled
            else "alphasift_ready"
            if alphasift_enabled and alphasift_available
            else "alphasift_unavailable"
        ),
        detail=(
            "自动买入关闭"
            if not auto_trade_enabled
            else f"AlphaSift 策略 {alphasift.get('strategy_count') or '-'} 个"
            if alphasift_enabled and alphasift_available
            else str(alphasift.get("error") or alphasift.get("diagnostics") or "AlphaSift 不可用")
        ),
        required=auto_trade_enabled,
    )

    scheduler_loop_running = _scheduler_loop_running(scheduler_status)
    add_component(
        key="automation_loop",
        label="自动化调度",
        status=(
            "disabled"
            if not auto_trade_enabled
            else "ready"
            if scheduler_status.get("enabled") and scheduler_loop_running
            else "blocked"
        ),
        reason=(
            "auto_trade_disabled"
            if not auto_trade_enabled
            else "scheduler_loop_running"
            if scheduler_status.get("enabled") and scheduler_loop_running
            else "scheduler_not_running"
        ),
        detail=(
            "自动买入关闭"
            if not auto_trade_enabled
            else f"下次 {scheduler_status.get('next_run_at') or '-'}"
            if scheduler_status.get("enabled") and scheduler_loop_running
            else "Runtime scheduler 未运行"
        ),
        required=auto_trade_enabled,
    )

    add_component(
        key="scheduling_window",
        label="调度窗口",
        status=str(timing_alignment.get("status") or ("disabled" if not auto_trade_enabled else "warning")),
        reason=str(timing_alignment.get("reason") or ("auto_trade_disabled" if not auto_trade_enabled else "timing_alignment_unknown")),
        detail=str(timing_alignment.get("detail") or "暂无法判断自动任务是否落在交易窗口"),
        required=auto_trade_enabled and time_gate_enforced,
        extra={
            "next_run_at": timing_alignment.get("next_run_at"),
            "window_open_at": timing_alignment.get("window_open_at"),
            "window_close_at": timing_alignment.get("window_close_at"),
        },
    )
    readiness_components = [
        item for item in list(readiness.get("components") or []) if isinstance(item, dict)
    ]
    last_run_component = next(
        (item for item in readiness_components if item.get("key") == "last_auto_run"),
        None,
    )
    last_auto_run = (
        readiness.get("last_auto_run")
        if isinstance(readiness.get("last_auto_run"), dict)
        else {}
    )
    if last_run_component is not None:
        add_component(
            key="last_auto_run",
            label="最近运行结果",
            status=str(last_run_component.get("status") or "warning"),
            reason=str(last_run_component.get("reason") or "last_auto_run_unknown"),
            detail=str(last_run_component.get("detail") or "最近自动运行结果未知"),
            required=False,
            extra={
                "ran_at": last_auto_run.get("ran_at"),
                "agent_run_uid": last_auto_run.get("agent_run_uid"),
                "agent_run_id": last_auto_run.get("agent_run_id"),
                "accepted": last_auto_run.get("accepted"),
                "skipped": last_auto_run.get("skipped"),
                "candidate_count": last_auto_run.get("candidate_count"),
                "submitted_count": last_auto_run.get("submitted_count"),
            },
        )
    add_component(
        key="trading_window",
        label="交易窗口",
        status=(
            "disabled"
            if not auto_trade_enabled
            else "ready"
            if not time_gate_enforced or market_open_now
            else "warning"
        ),
        reason=(
            "auto_trade_disabled"
            if not auto_trade_enabled
            else "time_gate_not_enforced"
            if not time_gate_enforced
            else "market_open_now"
            if market_open_now
            else str(trading_window.get("gate_reason") or trading_window.get("next_window_status") or "waiting_for_trading_window")
        ),
        detail=(
            "自动买入关闭"
            if not auto_trade_enabled
            else f"{execution_mode} 模式不强制交易时段拦截"
            if not time_gate_enforced
            else f"当前窗口至 {trading_window.get('current_close_at') or trading_window.get('next_close_at') or '-'}"
            if market_open_now
            else f"等待 {trading_window.get('next_open_at') or trading_window.get('reason') or '-'}"
        ),
        required=auto_trade_enabled and time_gate_enforced,
    )

    valuation = _snapshot_valuation_health(status_payload)
    add_component(
        key="valuation",
        label="持仓估值",
        status=str(valuation.get("status") or "warning"),
        reason=str(valuation.get("reason") or "valuation_unknown"),
        detail=str(valuation.get("detail") or "暂无法判断持仓估值质量"),
        required=bool(valuation.get("required")),
        extra={
            key: value
            for key, value in valuation.items()
            if key not in {"status", "reason", "detail", "required"}
        },
    )

    industry_exposure = (
        diagnostics.get("industry_exposure")
        if isinstance(diagnostics.get("industry_exposure"), dict)
        else {}
    )
    industry_required = bool(
        settings.get("auto_max_industry_position_value") is not None
        or settings.get("auto_max_industry_position_pct") is not None
        or settings.get("auto_target_industry_weights")
    )
    industry_status = str(industry_exposure.get("status") or "snapshot_not_requested")
    industry_position_count = int(industry_exposure.get("position_count") or 0)
    industry_resolved_count = int(industry_exposure.get("resolved_position_count") or 0)
    industry_missing_count = int(industry_exposure.get("missing_position_count") or 0)
    industry_coverage_pct = industry_exposure.get("coverage_pct")
    industry_missing_symbols = list(industry_exposure.get("missing_symbols") or [])
    if industry_status == "snapshot_not_requested":
        industry_health_status = "disabled"
        industry_reason = "industry_snapshot_not_requested"
        industry_detail = "轻量状态未解析持仓行业"
    elif industry_status == "snapshot_unavailable":
        industry_health_status = "disabled"
        industry_reason = "industry_snapshot_unavailable"
        industry_detail = "账户或持仓快照尚未加载，暂未解析行业"
    elif industry_status == "no_positions":
        industry_health_status = "ready"
        industry_reason = "no_positions"
        industry_detail = "当前没有需要解析行业的持仓"
    elif industry_status == "complete":
        industry_health_status = "ready"
        industry_reason = "industry_coverage_complete"
        industry_detail = f"已解析 {industry_resolved_count}/{industry_position_count} 笔持仓（100%）"
    else:
        industry_health_status = "blocked" if industry_required else "disabled"
        industry_reason = (
            "industry_coverage_incomplete"
            if industry_required
            else "industry_coverage_not_required"
        )
        coverage_label = f"{industry_coverage_pct}%" if industry_coverage_pct is not None else "-"
        missing_label = "、".join(str(item) for item in industry_missing_symbols[:3])
        coverage_detail = (
            f"已解析 {industry_resolved_count}/{industry_position_count} 笔（{coverage_label}），"
            f"缺失 {industry_missing_count} 笔"
            f"：{missing_label}" if missing_label else
            f"已解析 {industry_resolved_count}/{industry_position_count} 笔（{coverage_label}），缺失 {industry_missing_count} 笔"
        )
        industry_detail = (
            coverage_detail
            if industry_required
            else f"行业风控未启用；快照字段{coverage_detail}"
        )
    add_component(
        key="industry_exposure",
        label="行业归属",
        status=industry_health_status,
        reason=industry_reason,
        detail=industry_detail,
        required=industry_required,
        extra={
            "configured": industry_required,
            "position_count": industry_position_count,
            "resolved_position_count": industry_resolved_count,
            "missing_position_count": industry_missing_count,
            "coverage_pct": industry_coverage_pct,
            "industry_count": int(industry_exposure.get("industry_count") or 0),
            "missing_symbols": industry_missing_symbols,
            "resolution_mode": industry_exposure.get("resolution_mode"),
        },
    )

    account_drawdown = (
        diagnostics.get("account_drawdown")
        if isinstance(diagnostics.get("account_drawdown"), dict)
        else {}
    )
    drawdown_configured = bool(account_drawdown.get("configured"))
    drawdown_status = str(account_drawdown.get("status") or "disabled")
    drawdown_pct = account_drawdown.get("drawdown_pct")
    drawdown_threshold = account_drawdown.get("threshold_pct")
    drawdown_peak = account_drawdown.get("peak_equity")
    drawdown_recovery_threshold = account_drawdown.get("recovery_threshold_pct")
    drawdown_guard_latched = bool(account_drawdown.get("drawdown_guard_latched"))
    if not drawdown_configured:
        drawdown_health_status = "disabled"
        drawdown_reason = "account_drawdown_not_configured"
        drawdown_detail = "未配置账户最大回撤"
    elif drawdown_status == "snapshot_not_requested":
        drawdown_health_status = "disabled"
        drawdown_reason = "account_drawdown_snapshot_not_requested"
        drawdown_detail = "轻量状态未计算账户回撤"
    elif drawdown_status == "unavailable":
        drawdown_health_status = "blocked"
        drawdown_reason = "account_drawdown_unavailable"
        drawdown_detail = "账户权益或历史峰值不可用"
    elif drawdown_status == "limit_reached":
        drawdown_health_status = "blocked"
        drawdown_reason = "account_drawdown_limit_reached"
        drawdown_detail = f"当前回撤 {drawdown_pct}% 已达到上限 {drawdown_threshold}%"
    elif drawdown_status == "recovery_pending":
        drawdown_health_status = "blocked"
        drawdown_reason = "account_drawdown_recovery_pending"
        drawdown_detail = (
            f"当前回撤 {drawdown_pct}% 已低于上限，但需恢复到 "
            f"{drawdown_recovery_threshold}% 以内才重新允许买入"
        )
    elif drawdown_status == "recovery_ready":
        drawdown_health_status = "warning"
        drawdown_reason = "account_drawdown_recovery_ready"
        drawdown_detail = (
            f"当前回撤 {drawdown_pct}% 已达到恢复线 {drawdown_recovery_threshold}%；"
            "下一次自动交易风控检查将解除门禁"
        )
    else:
        drawdown_health_status = "ready"
        drawdown_reason = "account_drawdown_ready"
        drawdown_detail = f"当前回撤 {drawdown_pct}% / 上限 {drawdown_threshold}%"
    add_component(
        key="account_drawdown",
        label="账户回撤",
        status=drawdown_health_status,
        reason=drawdown_reason,
        detail=drawdown_detail,
        required=drawdown_configured,
        extra={
            "basis": account_drawdown.get("basis"),
            "equity": account_drawdown.get("equity"),
            "peak_equity": drawdown_peak,
            "drawdown_pct": drawdown_pct,
            "threshold_pct": drawdown_threshold,
            "recovery_hysteresis_pct": account_drawdown.get("recovery_hysteresis_pct"),
            "recovery_threshold_pct": drawdown_recovery_threshold,
            "drawdown_guard_latched": drawdown_guard_latched,
            "drawdown_guard_opened_at": account_drawdown.get("drawdown_guard_opened_at"),
            "drawdown_guard_last_recovered_at": account_drawdown.get(
                "drawdown_guard_last_recovered_at"
            ),
        },
    )

    consecutive_losses = (
        diagnostics.get("consecutive_losses")
        if isinstance(diagnostics.get("consecutive_losses"), dict)
        else {}
    )
    consecutive_configured = bool(consecutive_losses.get("configured"))
    consecutive_status = str(consecutive_losses.get("status") or "disabled")
    consecutive_streak = int(consecutive_losses.get("current_streak") or 0)
    consecutive_limit = consecutive_losses.get("limit")
    consecutive_recover_at = consecutive_losses.get("recover_at")
    if not consecutive_configured:
        consecutive_health_status = "disabled"
        consecutive_reason = "consecutive_loss_guard_not_configured"
        consecutive_detail = "未配置连续已平仓亏损门禁"
    elif consecutive_status == "cooling_down":
        consecutive_health_status = "blocked"
        consecutive_reason = "consecutive_loss_limit_reached"
        consecutive_detail = (
            f"连续亏损 {consecutive_streak} 笔，达到上限 {consecutive_limit}；"
            f"预计 {consecutive_recover_at or '冷却到期后'} 恢复买入"
        )
    elif consecutive_status in {"unavailable", "account_not_ready"}:
        consecutive_health_status = "blocked"
        consecutive_reason = "consecutive_loss_data_unavailable"
        consecutive_detail = "连续亏损账本或模拟账户不可用"
    elif consecutive_status == "cooldown_elapsed":
        consecutive_health_status = "ready"
        consecutive_reason = "consecutive_loss_cooldown_elapsed"
        consecutive_detail = (
            f"连续亏损仍为 {consecutive_streak} 笔，但冷却已到期，允许恢复买入"
        )
    else:
        consecutive_health_status = "ready"
        consecutive_reason = "consecutive_loss_guard_ready"
        consecutive_detail = f"当前连续已平仓亏损 {consecutive_streak} / {consecutive_limit} 笔"
    add_component(
        key="consecutive_losses",
        label="连续亏损",
        status=consecutive_health_status,
        reason=consecutive_reason,
        detail=consecutive_detail,
        required=consecutive_configured,
        extra={
            "account_id": consecutive_losses.get("account_id"),
            "current_streak": consecutive_streak,
            "max_streak": consecutive_losses.get("max_streak"),
            "limit": consecutive_limit,
            "cooldown_minutes": consecutive_losses.get("cooldown_minutes"),
            "cooldown_remaining_seconds": consecutive_losses.get(
                "cooldown_remaining_seconds"
            ),
            "recover_at": consecutive_recover_at,
            "last_closed_trade_id": consecutive_losses.get("last_closed_trade_id"),
            "last_closed_trade_at": consecutive_losses.get("last_closed_trade_at"),
            "last_closed_trade_pnl": consecutive_losses.get("last_closed_trade_pnl"),
            "guard_blocked": consecutive_losses.get("guard_blocked"),
            "guard_opened_at": consecutive_losses.get("guard_opened_at"),
            "guard_last_recovered_at": consecutive_losses.get(
                "guard_last_recovered_at"
            ),
        },
    )

    vnpy_required = execution_mode == "vnpy_paper"
    vnpy_bridge_available = bool(vnpy_bridge.get("available"))
    add_component(
        key="vnpy_bridge",
        label="vn.py bridge",
        status=(
            "ready"
            if not vnpy_required or vnpy_bridge_available
            else "blocked"
        ),
        reason=(
            "vnpy_bridge_not_required"
            if not vnpy_required
            else "vnpy_bridge_ready"
            if vnpy_bridge_available
            else str(vnpy_bridge.get("reason") or "vnpy_bridge_unavailable")
        ),
        detail=(
            f"{execution_mode} 模式不要求 vn.py bridge"
            if not vnpy_required
            else "MainEngine 可提交委托"
            if vnpy_bridge_available
            else str(vnpy_bridge.get("mode") or "not_configured")
        ),
        required=vnpy_required,
        extra={
            "runtime_mode": vnpy_runtime.get("mode"),
            "runtime_available": vnpy_runtime.get("available"),
        },
    )

    required_components = [item for item in components if item.get("required") is not False]
    required_blockers = [item["reason"] for item in required_components if item.get("status") == "blocked"]
    warnings = [item["reason"] for item in components if item.get("status") == "warning"]
    disabled = [item["reason"] for item in components if item.get("status") == "disabled"]
    if required_blockers:
        status = "blocked"
        next_action = required_blockers[0]
    elif warnings:
        status = "warning"
        next_action = warnings[0]
    elif not auto_trade_enabled:
        status = "disabled"
        next_action = "enable_auto_trade"
    else:
        status = "ready"
        next_action = "wait_for_next_scheduled_run"
    score_denominator = max(len(components), 1)
    score_penalty = sum(_health_status_rank(str(item.get("status") or "")) for item in components)
    health_score = max(0, round(100 - (score_penalty / (score_denominator * 3) * 100), 2))
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "ready": status == "ready",
        "next_action": next_action,
        "health_score": health_score,
        "required_blockers": required_blockers,
        "warnings": warnings,
        "disabled": disabled,
        "components": components,
    }


def _parse_alignment_datetime(value: Any) -> Optional[datetime]:
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
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed.astimezone(timezone.utc)


def _auto_trade_timing_alignment(
    *,
    scheduler_status: Dict[str, Any],
    auto_trade_task: Optional[Dict[str, Any]],
    trading_window: Dict[str, Any],
    auto_trade_enabled: bool,
    time_gate_enforced: bool,
) -> Dict[str, Any]:
    if auto_trade_task is not None:
        next_run_at = auto_trade_task.get("next_run_at") or scheduler_status.get("next_run_at")
    else:
        next_run_at = scheduler_status.get("next_run_at")
    market_open_now = trading_window.get("is_market_open_now") is True
    window_open_at = (
        trading_window.get("current_open_at")
        if market_open_now
        else trading_window.get("next_open_at")
    )
    window_close_at = (
        trading_window.get("current_close_at")
        if market_open_now
        else trading_window.get("next_close_at")
    )
    next_run_dt = _parse_alignment_datetime(next_run_at)
    open_dt = _parse_alignment_datetime(window_open_at)
    close_dt = _parse_alignment_datetime(window_close_at)
    payload: Dict[str, Any] = {
        "next_run_at": next_run_at,
        "window_open_at": window_open_at,
        "window_close_at": window_close_at,
    }
    if not auto_trade_enabled:
        payload.update({
            "status": "disabled",
            "reason": "auto_trade_disabled",
            "detail": "自动买入关闭",
        })
    elif not time_gate_enforced:
        payload.update({
            "status": "ready",
            "reason": "time_gate_not_enforced",
            "detail": "当前执行模式不强制交易窗口",
        })
    elif next_run_dt is None:
        payload.update({
            "status": "warning",
            "reason": "next_auto_run_unknown",
            "detail": "暂无法判断下次自动买入时间",
        })
    elif open_dt is None or close_dt is None:
        payload.update({
            "status": "warning",
            "reason": "trading_window_unknown",
            "detail": "暂无法判断下个可交易窗口",
        })
    elif next_run_dt < open_dt:
        payload.update({
            "status": "warning",
            "reason": "next_auto_run_before_window",
            "detail": f"下次自动买入 {next_run_at} 早于窗口 {window_open_at}",
        })
    elif next_run_dt > close_dt:
        payload.update({
            "status": "warning",
            "reason": "next_auto_run_after_window",
            "detail": f"下次自动买入 {next_run_at} 晚于窗口 {window_close_at}",
        })
    else:
        payload.update({
            "status": "ready",
            "reason": "next_auto_run_in_window",
            "detail": f"下次自动买入位于交易窗口内：{next_run_at}",
        })
    return payload


def _auto_trade_readiness_payload(status_payload: Dict[str, Any]) -> Dict[str, Any]:
    scheduler_status = status_payload.get("scheduler") if isinstance(status_payload.get("scheduler"), dict) else {}
    settings = status_payload.get("settings") if isinstance(status_payload.get("settings"), dict) else {}
    diagnostics = status_payload.get("diagnostics") if isinstance(status_payload.get("diagnostics"), dict) else {}
    trading_window = diagnostics.get("trading_window") if isinstance(diagnostics.get("trading_window"), dict) else {}
    failure_fuse = diagnostics.get("failure_fuse") if isinstance(diagnostics.get("failure_fuse"), dict) else {}
    vnpy_bridge = diagnostics.get("vnpy_bridge") if isinstance(diagnostics.get("vnpy_bridge"), dict) else {}
    alphasift = diagnostics.get("alphasift") if isinstance(diagnostics.get("alphasift"), dict) else {}
    tasks = [task for task in list(scheduler_status.get("background_tasks") or []) if isinstance(task, dict)]
    events = [event for event in list(scheduler_status.get("task_events") or []) if isinstance(event, dict)]
    tasks_by_name = {str(task.get("name") or ""): task for task in tasks}
    auto_trade_task = tasks_by_name.get("vnpy_paper_auto_trade")
    auto_trade_event = _latest_task_event(events, "vnpy_paper_auto_trade")
    event_details = (
        auto_trade_event.get("details")
        if auto_trade_event is not None and isinstance(auto_trade_event.get("details"), dict)
        else {}
    )
    last_auto_run = (
        status_payload.get("last_auto_run")
        if isinstance(status_payload.get("last_auto_run"), dict)
        else {}
    )

    paper_enabled = bool(settings.get("enabled", status_payload.get("enabled", False)))
    local_available = bool(status_payload.get("available", paper_enabled))
    auto_trade_enabled = bool(settings.get("auto_trade_enabled"))
    execution_mode = str(settings.get("auto_execution_mode") or status_payload.get("mode") or "paper")
    scheduler_enabled = bool(scheduler_status.get("enabled"))
    scheduler_loop_running = _scheduler_loop_running(scheduler_status)
    time_gate_enforced = bool(trading_window.get("time_gate_enforced"))
    market_open_now = trading_window.get("is_market_open_now") is True
    failure_fuse_enabled = bool(failure_fuse.get("enabled"))
    failure_fuse_open = bool(failure_fuse.get("open"))
    vnpy_bridge_available = bool(vnpy_bridge.get("available"))
    alphasift_known = bool(alphasift)
    alphasift_enabled = bool(alphasift.get("enabled", True)) if alphasift_known else True
    alphasift_available = bool(alphasift.get("available")) if alphasift_known else True

    components: List[Dict[str, Any]] = []

    def add_component(
        *,
        key: str,
        label: str,
        status: str,
        reason: str,
        detail: str,
        required: bool = True,
    ) -> None:
        components.append({
            "key": key,
            "label": label,
            "status": status,
            "reason": reason,
            "detail": detail,
            "required": required,
            "tone": _status_tone(status),
        })

    add_component(
        key="local_paper",
        label="本地账本",
        status="ready" if paper_enabled and local_available else "blocked",
        reason="local_paper_available" if paper_enabled and local_available else "paper_trading_disabled",
        detail="本地 paper 账本可写入 Portfolio" if paper_enabled and local_available else "模拟交易关闭或本地账本不可用",
    )
    add_component(
        key="auto_trade",
        label="自动交易",
        status="ready" if auto_trade_enabled else "disabled",
        reason="auto_trade_enabled" if auto_trade_enabled else "auto_trade_disabled",
        detail="定时自动选股交易已开启" if auto_trade_enabled else "自动买入关闭",
        required=False,
    )
    add_component(
        key="scheduler",
        label="Runtime scheduler",
        status=(
            "disabled"
            if not auto_trade_enabled
            else "ready"
            if scheduler_enabled and scheduler_loop_running
            else "blocked"
        ),
        reason=(
            "auto_trade_disabled"
            if not auto_trade_enabled
            else "scheduler_loop_running"
            if scheduler_enabled and scheduler_loop_running
            else "scheduler_not_running"
        ),
        detail=(
            "自动买入关闭"
            if not auto_trade_enabled
            else "后台调度循环运行中"
            if scheduler_enabled and scheduler_loop_running
            else "后台调度器未运行或未启用"
        ),
    )
    add_component(
        key="auto_trade_task",
        label="自动任务",
        status=(
            "disabled"
            if not auto_trade_enabled
            else "ready"
            if auto_trade_task is not None
            else "blocked"
        ),
        reason=(
            "auto_trade_disabled"
            if not auto_trade_enabled
            else "task_registered"
            if auto_trade_task is not None
            else "task_not_registered"
        ),
        detail=(
            "自动买入关闭"
            if not auto_trade_enabled
            else f"下次 {auto_trade_task.get('next_run_at') or scheduler_status.get('next_run_at') or '-'}"
            if auto_trade_task is not None
            else "vnpy_paper_auto_trade 未注册"
        ),
    )
    add_component(
        key="alphasift",
        label="AlphaSift",
        status=(
            "disabled"
            if not auto_trade_enabled
            else "ready"
            if alphasift_enabled and alphasift_available
            else "blocked"
        ),
        reason=(
            "auto_trade_disabled"
            if not auto_trade_enabled
            else "alphasift_available"
            if alphasift_enabled and alphasift_available
            else "alphasift_disabled"
            if not alphasift_enabled
            else "alphasift_unavailable"
        ),
        detail=(
            "自动买入关闭"
            if not auto_trade_enabled
            else f"策略 {alphasift.get('strategy_count') or '-'} 个"
            if alphasift_enabled and alphasift_available
            else str(alphasift.get("error") or alphasift.get("diagnostics") or "AlphaSift 不可用")
        ),
    )
    timing_alignment = _auto_trade_timing_alignment(
        scheduler_status=scheduler_status,
        auto_trade_task=auto_trade_task,
        trading_window=trading_window,
        auto_trade_enabled=auto_trade_enabled,
        time_gate_enforced=time_gate_enforced,
    )
    add_component(
        key="timing_alignment",
        label="调度窗口",
        status=str(timing_alignment.get("status") or "warning"),
        reason=str(timing_alignment.get("reason") or "timing_alignment_unknown"),
        detail=str(timing_alignment.get("detail") or "暂无法判断调度窗口"),
    )
    if auto_trade_event is not None and str(auto_trade_event.get("status") or "") in {"failed", "skipped"}:
        event_status = str(auto_trade_event.get("status") or "")
        add_component(
            key="last_auto_trade_event",
            label="最近自动任务",
            status="blocked" if event_status == "failed" else "warning",
            reason=str(event_details.get("reason") or f"last_event_{event_status}"),
            detail=str(auto_trade_event.get("message") or event_status),
            required=False,
        )
    if last_auto_run:
        last_run_reason = str(last_auto_run.get("reason") or "").strip()
        last_run_skipped = bool(last_auto_run.get("skipped"))
        last_run_accepted = bool(last_auto_run.get("accepted"))
        last_run_at = str(last_auto_run.get("ran_at") or "-")
        last_run_uid = str(last_auto_run.get("agent_run_uid") or "").strip()
        submitted_count = int(last_auto_run.get("submitted_count") or 0)
        candidate_count = int(last_auto_run.get("candidate_count") or 0)
        if last_run_skipped:
            last_run_status = "warning"
            last_run_component_reason = last_run_reason or "last_auto_run_skipped"
            last_run_detail = f"最近运行 {last_run_at} 跳过：{last_run_component_reason}"
        elif last_run_accepted:
            last_run_status = "ready"
            last_run_component_reason = "last_auto_run_completed"
            last_run_detail = (
                f"最近运行 {last_run_at} 完成：候选 {candidate_count}，提交 {submitted_count}"
            )
        else:
            last_run_status = "warning"
            last_run_component_reason = last_run_reason or "last_auto_run_failed"
            last_run_detail = f"最近运行 {last_run_at} 未完成：{last_run_component_reason}"
        if last_run_uid:
            last_run_detail = f"{last_run_detail}（{last_run_uid}）"
        add_component(
            key="last_auto_run",
            label="最近运行结果",
            status=last_run_status,
            reason=last_run_component_reason,
            detail=last_run_detail,
            required=False,
        )
    add_component(
        key="trading_window",
        label="交易窗口",
        status=(
            "ready"
            if not time_gate_enforced or market_open_now
            else "warning"
        ),
        reason=(
            "time_gate_not_enforced"
            if not time_gate_enforced
            else "market_open_now"
            if market_open_now
            else str(trading_window.get("gate_reason") or trading_window.get("next_window_status") or "waiting_for_trading_window")
        ),
        detail=(
            f"{execution_mode} 模式不强制交易时段拦截"
            if not time_gate_enforced
            else f"当前窗口至 {trading_window.get('current_close_at') or trading_window.get('next_close_at') or '-'}"
            if market_open_now
            else f"等待 {trading_window.get('next_open_at') or trading_window.get('reason') or '-'}"
        ),
    )
    add_component(
        key="failure_fuse",
        label="连续失败熔断",
        status="blocked" if failure_fuse_open else "ready" if failure_fuse_enabled else "disabled",
        reason="failure_fuse_open" if failure_fuse_open else "failure_fuse_ok" if failure_fuse_enabled else "failure_fuse_disabled",
        detail=(
            f"{failure_fuse.get('consecutive_failure_count', 0)}/{failure_fuse.get('threshold', '-')}"
            if failure_fuse_enabled
            else "未启用连续失败熔断"
        ),
        required=False,
    )
    add_component(
        key="vnpy_bridge",
        label="vn.py bridge",
        status=(
            "ready"
            if execution_mode != "vnpy_paper" or vnpy_bridge_available
            else "blocked"
        ),
        reason=(
            "vnpy_bridge_not_required"
            if execution_mode != "vnpy_paper"
            else "vnpy_bridge_available"
            if vnpy_bridge_available
            else str(vnpy_bridge.get("reason") or vnpy_bridge.get("mode") or "vnpy_bridge_unavailable")
        ),
        detail=(
            f"{execution_mode} 模式不需要 vn.py bridge"
            if execution_mode != "vnpy_paper"
            else "MainEngine 可提交委托"
            if vnpy_bridge_available
            else str(vnpy_bridge.get("mode") or "not_configured")
        ),
    )

    blockers = [item["reason"] for item in components if item["status"] == "blocked"]
    warnings = [item["reason"] for item in components if item["status"] == "warning"]
    disabled = [item["reason"] for item in components if item["status"] == "disabled"]
    if blockers:
        status = "blocked"
        next_action = blockers[0]
    elif not auto_trade_enabled:
        status = "disabled"
        next_action = "enable_auto_trade"
    elif warnings:
        status = "warning"
        next_action = warnings[0]
    else:
        status = "ready"
        next_action = "wait_for_next_scheduled_run"

    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "ready": status == "ready",
        "next_action": next_action,
        "auto_trade_enabled": auto_trade_enabled,
        "auto_execution_mode": execution_mode,
        "timing_alignment": timing_alignment,
        "last_auto_run": dict(last_auto_run) if last_auto_run else None,
        "blockers": blockers,
        "warnings": warnings,
        "disabled": disabled,
        "components": components,
    }


def _latest_task_event(events: List[Dict[str, Any]], name: str) -> Optional[Dict[str, Any]]:
    for event in reversed(events):
        if str(event.get("name") or "") == name:
            return event
    return None


def _task_health_payload(status_payload: Dict[str, Any]) -> Dict[str, Any]:
    scheduler_status = status_payload.get("scheduler") if isinstance(status_payload.get("scheduler"), dict) else {}
    settings = status_payload.get("settings") if isinstance(status_payload.get("settings"), dict) else {}
    paper_enabled = bool(settings.get("enabled", status_payload.get("enabled", False)))
    auto_trade_enabled = bool(settings.get("auto_trade_enabled"))
    scheduler_enabled = bool(scheduler_status.get("enabled"))
    scheduler_running = bool(scheduler_status.get("running"))
    scheduler_loop_running = _scheduler_loop_running(scheduler_status)
    tasks = [task for task in list(scheduler_status.get("background_tasks") or []) if isinstance(task, dict)]
    events = [event for event in list(scheduler_status.get("task_events") or []) if isinstance(event, dict)]
    tasks_by_name = {str(task.get("name") or ""): task for task in tasks}
    extra_names = sorted(name for name in tasks_by_name if name and name not in _EXPECTED_VNPY_PAPER_TASKS)
    names = list(_EXPECTED_VNPY_PAPER_TASKS) + extra_names
    items: List[Dict[str, Any]] = []

    for name in names:
        task = tasks_by_name.get(name)
        registered = task is not None
        is_auto_trade_task = name == "vnpy_paper_auto_trade"
        is_recovery_task = name == "vnpy_paper_auto_retry"
        required = bool(
            paper_enabled
            and (is_recovery_task or (is_auto_trade_task and auto_trade_enabled))
        )
        event = _latest_task_event(events, name)
        event_status = str(event.get("status") or "") if event is not None else None
        event_details = event.get("details") if event is not None and isinstance(event.get("details"), dict) else {}
        running = bool(task.get("running")) if task is not None else False
        health = "healthy"
        reason = "task_registered"
        if not paper_enabled:
            health = "disabled"
            reason = "paper_trading_disabled"
        elif is_auto_trade_task and not auto_trade_enabled:
            health = "disabled"
            reason = "auto_trade_disabled"
        elif not scheduler_enabled:
            health = "error" if required else "warning"
            reason = "scheduler_not_enabled"
        elif required and not scheduler_loop_running:
            health = "error"
            reason = "scheduler_not_running"
        elif required and not registered:
            health = "error"
            reason = "task_not_registered"
        elif registered:
            if running:
                health = "healthy"
                reason = "task_running"
            elif event_status == "failed":
                health = "error"
                reason = "last_event_failed"
            elif event_status == "skipped":
                health = "warning"
                reason = str(event_details.get("reason") or "last_event_skipped")
            elif not task.get("next_run_at"):
                health = "warning"
                reason = "next_run_unknown"
        else:
            health = "disabled"
            reason = "task_not_configured"

        items.append({
            "name": name,
            "label": _TASK_HEALTH_LABELS.get(name, name),
            "health": health,
            "reason": reason,
            "required": required,
            "registered": registered,
            "running": running,
            "interval_seconds": task.get("interval_seconds") if task is not None else None,
            "last_run": task.get("last_run") if task is not None else None,
            "next_run_at": task.get("next_run_at") if task is not None else None,
            "last_event_status": event_status,
            "last_event_at": event.get("timestamp") if event is not None else None,
            "last_event_message": event.get("message") if event is not None else None,
            "duration_seconds": event.get("duration_seconds") if event is not None else None,
            "details": event_details,
        })

    summary = {
        "healthy": sum(1 for item in items if item["health"] == "healthy"),
        "warning": sum(1 for item in items if item["health"] == "warning"),
        "error": sum(1 for item in items if item["health"] == "error"),
        "disabled": sum(1 for item in items if item["health"] == "disabled"),
        "total": len(items),
    }
    if summary["error"]:
        overall = "error"
    elif summary["warning"]:
        overall = "warning"
    elif items and summary["disabled"] == len(items):
        overall = "disabled"
    else:
        overall = "healthy"

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "overall_health": overall,
        "scheduler_enabled": scheduler_enabled,
        "scheduler_running": scheduler_running,
        "scheduler_loop_running": scheduler_loop_running,
        "paper_trading_enabled": paper_enabled,
        "auto_trade_enabled": auto_trade_enabled,
        "auto_execution_mode": settings.get("auto_execution_mode"),
        "summary": summary,
        "items": items,
    }


def _scheduler_task_events(
    scheduler: RuntimeSchedulerService,
    *,
    name: Optional[str],
    status: Optional[str],
    limit: int,
    started_at: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    name_filter = (name or "").strip()
    status_filter = (status or "").strip()
    if hasattr(scheduler, "task_events"):
        query = {
            "name": name_filter or None,
            "status": status_filter or None,
            "limit": limit,
        }
        if started_at is not None:
            query["started_at"] = started_at
        events = scheduler.task_events(**query)
        return [event for event in list(events or []) if isinstance(event, dict)]
    try:
        scheduler_status = scheduler.status()
    except Exception as exc:  # pragma: no cover - defensive fallback.
        logger.warning("Get runtime scheduler task events failed: %s", exc)
        scheduler_status = {}
    if not isinstance(scheduler_status, dict):
        scheduler_status = {}
    events = [event for event in list(scheduler_status.get("task_events") or []) if isinstance(event, dict)]
    if name_filter:
        events = [event for event in events if str(event.get("name") or "") == name_filter]
    if status_filter:
        events = [event for event in events if str(event.get("status") or "") == status_filter]
    if started_at is not None:
        cutoff = started_at.isoformat()
        events = [event for event in events if str(event.get("timestamp") or "") >= cutoff]
    return events[-limit:]


def _task_event_summary_payload(events: List[Dict[str, Any]], *, limit: int) -> Dict[str, Any]:
    status_counts = {
        "started": 0,
        "completed": 0,
        "skipped": 0,
        "failed": 0,
    }
    task_buckets: Dict[str, Dict[str, Any]] = {}
    for event in events:
        name = str(event.get("name") or "").strip()
        if not name:
            continue
        status = str(event.get("status") or "").strip()
        if status:
            status_counts[status] = int(status_counts.get(status, 0)) + 1
        bucket = task_buckets.setdefault(name, {
            "name": name,
            "label": _TASK_HEALTH_LABELS.get(name, name),
            "total": 0,
            "started_count": 0,
            "completed_count": 0,
            "skipped_count": 0,
            "failed_count": 0,
            "duration_total": 0.0,
            "duration_count": 0,
            "last_event_status": None,
            "last_event_at": None,
            "last_event_message": None,
            "last_failed_at": None,
            "last_skipped_at": None,
        })
        bucket["total"] += 1
        count_key = f"{status}_count"
        if count_key in bucket:
            bucket[count_key] += 1
        duration = event.get("duration_seconds")
        if duration is not None:
            try:
                bucket["duration_total"] += float(duration)
                bucket["duration_count"] += 1
            except (TypeError, ValueError):
                pass
        timestamp = event.get("timestamp")
        bucket["last_event_status"] = status or None
        bucket["last_event_at"] = timestamp
        bucket["last_event_message"] = event.get("message")
        if status == "failed":
            bucket["last_failed_at"] = timestamp
        elif status == "skipped":
            bucket["last_skipped_at"] = timestamp

    items = []
    for bucket in task_buckets.values():
        total = int(bucket.get("total") or 0)
        failed = int(bucket.get("failed_count") or 0)
        duration_count = int(bucket.pop("duration_count") or 0)
        duration_total = float(bucket.pop("duration_total") or 0.0)
        bucket["failure_rate_pct"] = round((failed / total * 100), 2) if total else 0.0
        bucket["avg_duration_seconds"] = round(duration_total / duration_count, 3) if duration_count else None
        items.append(bucket)
    items.sort(key=lambda item: (-int(item.get("failed_count") or 0), -int(item.get("skipped_count") or 0), item["name"]))
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "limit": limit,
        "count": len(events),
        "status_counts": status_counts,
        "items": items,
    }


def _percentile(values: List[float], percentile: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * percentile + 0.999999)))
    return round(ordered[index], 3)


def _task_metrics_payload(
    events: List[Dict[str, Any]],
    *,
    window_days: int,
    window_started_at: datetime,
    window_ended_at: datetime,
    limit: int,
) -> Dict[str, Any]:
    terminal_statuses = {"completed", "skipped", "failed"}
    task_buckets: Dict[str, Dict[str, Any]] = {}
    daily_buckets: Dict[str, Dict[str, Any]] = {}
    terminal_events: List[Dict[str, Any]] = []
    started_count = 0

    for event in events:
        status = str(event.get("status") or "").strip()
        if status == "started":
            started_count += 1
        if status not in terminal_statuses:
            continue
        terminal_events.append(event)
        name = str(event.get("name") or "").strip() or "unknown"
        timestamp = str(event.get("timestamp") or "")
        date_key = timestamp[:10] if len(timestamp) >= 10 else "unknown"
        duration = event.get("duration_seconds")
        try:
            duration_value = float(duration) if duration is not None else None
        except (TypeError, ValueError):
            duration_value = None

        task = task_buckets.setdefault(name, {
            "name": name,
            "label": _TASK_HEALTH_LABELS.get(name, name),
            "run_count": 0,
            "completed_count": 0,
            "skipped_count": 0,
            "failed_count": 0,
            "durations": [],
            "last_run_at": None,
            "last_failure_at": None,
        })
        day = daily_buckets.setdefault(date_key, {
            "date": date_key,
            "run_count": 0,
            "completed_count": 0,
            "skipped_count": 0,
            "failed_count": 0,
            "durations": [],
        })
        for bucket in (task, day):
            bucket["run_count"] += 1
            bucket[f"{status}_count"] += 1
            if duration_value is not None:
                bucket["durations"].append(duration_value)
        task["last_run_at"] = timestamp or task["last_run_at"]
        if status == "failed":
            task["last_failure_at"] = timestamp or task["last_failure_at"]

    def finalize(bucket: Dict[str, Any], *, include_p95: bool) -> Dict[str, Any]:
        run_count = int(bucket.get("run_count") or 0)
        durations = list(bucket.pop("durations", []))
        bucket["success_rate_pct"] = round(int(bucket.get("completed_count") or 0) / run_count * 100, 2) if run_count else 0.0
        bucket["failure_rate_pct"] = round(int(bucket.get("failed_count") or 0) / run_count * 100, 2) if run_count else 0.0
        if include_p95:
            bucket["skip_rate_pct"] = round(int(bucket.get("skipped_count") or 0) / run_count * 100, 2) if run_count else 0.0
        bucket["avg_duration_seconds"] = round(sum(durations) / len(durations), 3) if durations else None
        if include_p95:
            bucket["p95_duration_seconds"] = _percentile(durations, 0.95)
        return bucket

    items = [finalize(bucket, include_p95=True) for bucket in task_buckets.values()]
    items.sort(key=lambda item: (-int(item["failed_count"]), -int(item["run_count"]), item["name"]))
    daily = [finalize(bucket, include_p95=False) for bucket in daily_buckets.values()]
    daily.sort(key=lambda item: item["date"])

    completed_count = sum(1 for event in terminal_events if event.get("status") == "completed")
    skipped_count = sum(1 for event in terminal_events if event.get("status") == "skipped")
    failed_count = sum(1 for event in terminal_events if event.get("status") == "failed")
    durations = []
    for event in terminal_events:
        try:
            if event.get("duration_seconds") is not None:
                durations.append(float(event["duration_seconds"]))
        except (TypeError, ValueError):
            pass
    failure_streak = 0
    for event in reversed(terminal_events):
        if str(event.get("status") or "") != "failed":
            break
        failure_streak += 1
    run_count = len(terminal_events)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_days": window_days,
        "window_started_at": window_started_at.isoformat(),
        "window_ended_at": window_ended_at.isoformat(),
        "event_count": len(events),
        "run_count": run_count,
        "started_count": started_count,
        "completed_count": completed_count,
        "skipped_count": skipped_count,
        "failed_count": failed_count,
        "success_rate_pct": round(completed_count / run_count * 100, 2) if run_count else 0.0,
        "skip_rate_pct": round(skipped_count / run_count * 100, 2) if run_count else 0.0,
        "failure_rate_pct": round(failed_count / run_count * 100, 2) if run_count else 0.0,
        "avg_duration_seconds": round(sum(durations) / len(durations), 3) if durations else None,
        "p95_duration_seconds": _percentile(durations, 0.95),
        "current_failure_streak": failure_streak,
        "truncated": len(events) >= limit,
        "items": items,
        "daily": daily,
    }


def _with_vnpy_runtime_status(payload: Dict[str, Any], request: Request) -> Dict[str, Any]:
    runtime_diagnostics = getattr(request.app.state, "vnpy_runtime_diagnostics", None)
    if isinstance(runtime_diagnostics, dict):
        payload.setdefault("diagnostics", {})["vnpy_runtime"] = runtime_diagnostics
    return payload


def _with_status_dependencies(
    payload: Dict[str, Any],
    scheduler: RuntimeSchedulerService,
    request: Request,
) -> Dict[str, Any]:
    payload = _with_backend_status(payload)
    payload = _with_alphasift_status(payload)
    payload = _with_scheduler_status(payload, scheduler)
    payload = _with_vnpy_runtime_status(payload, request)
    return _with_system_health(payload)


@router.get(
    "/status",
    response_model=VnpyPaperStatusResponse,
    responses={500: {"model": ErrorResponse}},
    summary="Get vn.py paper trading status",
)
def get_vnpy_paper_status(
    request: Request,
    scheduler: RuntimeSchedulerService = Depends(get_runtime_scheduler_service),
    include_snapshot: bool = Query(True, description="Include portfolio snapshot and position valuation."),
    include_recent_trades: bool = Query(True, description="Include recent paper trade events."),
) -> VnpyPaperStatusResponse:
    try:
        payload = _service(request).get_status(
            include_snapshot=include_snapshot,
            include_recent_trades=include_recent_trades,
        )
        payload = _with_status_dependencies(payload, scheduler, request)
        return VnpyPaperStatusResponse.model_validate(
            payload
        )
    except Exception as exc:
        raise _internal_error("Get vn.py paper trading status failed", exc)


@router.get(
    "/task-health",
    response_model=VnpyPaperTaskHealthResponse,
    responses={500: {"model": ErrorResponse}},
    summary="Get vn.py paper background task health summary",
)
def get_vnpy_paper_task_health(
    request: Request,
    scheduler: RuntimeSchedulerService = Depends(get_runtime_scheduler_service),
) -> VnpyPaperTaskHealthResponse:
    try:
        payload = _service(request).get_status(
            include_snapshot=False,
            include_recent_trades=False,
        )
        payload = _with_scheduler_status(payload, scheduler)
        persisted_events = _scheduler_task_events(
            scheduler,
            name=None,
            status=None,
            limit=100,
        )
        if persisted_events:
            scheduler_payload = payload.get("scheduler") if isinstance(payload.get("scheduler"), dict) else {}
            payload["scheduler"] = {
                **scheduler_payload,
                "task_events": persisted_events,
            }
        return VnpyPaperTaskHealthResponse.model_validate(_task_health_payload(payload))
    except Exception as exc:
        raise _internal_error("Get vn.py paper task health failed", exc)


@router.get(
    "/task-events",
    response_model=VnpyPaperTaskEventListResponse,
    responses={500: {"model": ErrorResponse}},
    summary="List recent vn.py paper background task events",
)
def list_vnpy_paper_task_events(
    scheduler: RuntimeSchedulerService = Depends(get_runtime_scheduler_service),
    limit: int = Query(50, ge=1, le=100, description="Maximum number of events to return."),
    name: Optional[str] = Query(None, description="Filter by background task name."),
    status: Optional[str] = Query(None, description="Filter by event status."),
) -> VnpyPaperTaskEventListResponse:
    try:
        event_name = (name or "").strip() or None
        event_status = (status or "").strip() or None
        events = _scheduler_task_events(
            scheduler,
            name=event_name,
            status=event_status,
            limit=limit,
        )
        return VnpyPaperTaskEventListResponse.model_validate({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "limit": limit,
            "name": event_name,
            "status": event_status,
            "count": len(events),
            "items": events,
        })
    except Exception as exc:
        raise _internal_error("List vn.py paper task events failed", exc)


@router.get(
    "/task-event-summary",
    response_model=VnpyPaperTaskEventSummaryResponse,
    responses={500: {"model": ErrorResponse}},
    summary="Summarize recent vn.py paper background task events",
)
def get_vnpy_paper_task_event_summary(
    scheduler: RuntimeSchedulerService = Depends(get_runtime_scheduler_service),
    limit: int = Query(100, ge=1, le=100, description="Maximum number of recent events to aggregate."),
) -> VnpyPaperTaskEventSummaryResponse:
    try:
        events = _scheduler_task_events(
            scheduler,
            name=None,
            status=None,
            limit=limit,
        )
        return VnpyPaperTaskEventSummaryResponse.model_validate(
            _task_event_summary_payload(events, limit=limit)
        )
    except Exception as exc:
        raise _internal_error("Summarize vn.py paper task events failed", exc)


@router.get(
    "/task-metrics",
    response_model=VnpyPaperTaskMetricsResponse,
    responses={500: {"model": ErrorResponse}},
    summary="Get long-window vn.py paper background task metrics",
)
def get_vnpy_paper_task_metrics(
    scheduler: RuntimeSchedulerService = Depends(get_runtime_scheduler_service),
    days: int = Query(30, ge=1, le=90, description="Calendar window in days to aggregate."),
) -> VnpyPaperTaskMetricsResponse:
    try:
        window_ended_at = datetime.now()
        window_started_at = window_ended_at - timedelta(days=days)
        limit = 5000
        events = _scheduler_task_events(
            scheduler,
            name=None,
            status=None,
            limit=limit,
            started_at=window_started_at,
        )
        return VnpyPaperTaskMetricsResponse.model_validate(
            _task_metrics_payload(
                events,
                window_days=days,
                window_started_at=window_started_at,
                window_ended_at=window_ended_at,
                limit=limit,
            )
        )
    except Exception as exc:
        raise _internal_error("Get vn.py paper task metrics failed", exc)


@router.post(
    "/account/ensure",
    response_model=VnpyPaperStatusResponse,
    responses={400: {"model": ErrorResponse}, 409: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Ensure vn.py paper trading account exists",
)
def ensure_vnpy_paper_account(
    request: Request,
    scheduler: RuntimeSchedulerService = Depends(get_runtime_scheduler_service),
    include_snapshot: bool = Query(True, description="Include portfolio snapshot and position valuation."),
    include_recent_trades: bool = Query(True, description="Include recent paper trade events."),
) -> VnpyPaperStatusResponse:
    try:
        payload = _service(request).get_status(
            ensure_account=True,
            include_snapshot=include_snapshot,
            include_recent_trades=include_recent_trades,
        )
        payload = _with_status_dependencies(payload, scheduler, request)
        return VnpyPaperStatusResponse.model_validate(
            payload
        )
    except PortfolioBusyError as exc:
        raise _conflict_error(exc)
    except ValueError as exc:
        raise _bad_request(exc)
    except Exception as exc:
        raise _internal_error("Ensure vn.py paper account failed", exc)


@router.post(
    "/account/reset",
    response_model=VnpyPaperStatusResponse,
    responses={400: {"model": ErrorResponse}, 409: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Archive and recreate the local vn.py paper trading account",
)
def reset_vnpy_paper_account(
    request: Request,
    scheduler: RuntimeSchedulerService = Depends(get_runtime_scheduler_service),
    include_snapshot: bool = Query(True, description="Include portfolio snapshot and position valuation."),
    include_recent_trades: bool = Query(True, description="Include recent paper trade events."),
) -> VnpyPaperStatusResponse:
    try:
        payload = _service(request).reset_account(
            include_snapshot=include_snapshot,
            include_recent_trades=include_recent_trades,
        )
        payload = _with_status_dependencies(payload, scheduler, request)
        return VnpyPaperStatusResponse.model_validate(payload)
    except PortfolioBusyError as exc:
        raise _conflict_error(exc)
    except ValueError as exc:
        raise _bad_request(exc)
    except Exception as exc:
        raise _internal_error("Reset vn.py paper account failed", exc)


@router.get(
    "/accounts",
    response_model=VnpyPaperAccountListResponse,
    responses={500: {"model": ErrorResponse}},
    summary="List local vn.py paper accounts including archived ledgers",
)
def list_vnpy_paper_accounts(
    request: Request,
    include_inactive: bool = Query(True, description="Include archived inactive paper accounts."),
    include_hidden: bool = Query(False, description="Include archived accounts hidden by cleanup."),
) -> VnpyPaperAccountListResponse:
    try:
        return VnpyPaperAccountListResponse.model_validate(
            _service(request).list_paper_accounts(
                include_inactive=include_inactive,
                include_hidden=include_hidden,
            )
        )
    except Exception as exc:
        raise _internal_error("List vn.py paper accounts failed", exc)


@router.post(
    "/accounts/archived/cleanup",
    response_model=VnpyPaperArchivedAccountCleanupResponse,
    responses={400: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Hide archived local vn.py paper accounts from the paper account history",
)
def cleanup_archived_vnpy_paper_accounts(
    request_obj: Request,
    request: VnpyPaperArchivedAccountCleanupRequest,
) -> VnpyPaperArchivedAccountCleanupResponse:
    try:
        return VnpyPaperArchivedAccountCleanupResponse.model_validate(
            _service(request_obj).cleanup_archived_paper_accounts(
                request.account_ids,
                dry_run=request.dry_run,
                include_hidden=request.include_hidden,
            )
        )
    except ValueError as exc:
        raise _bad_request(exc)
    except Exception as exc:
        raise _internal_error("Cleanup archived vn.py paper accounts failed", exc)


@router.post(
    "/accounts/{account_id}/restore",
    response_model=VnpyPaperStatusResponse,
    responses={400: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Reactivate an archived local vn.py paper account and make it current",
)
def restore_vnpy_paper_account(
    request: Request,
    scheduler: RuntimeSchedulerService = Depends(get_runtime_scheduler_service),
    account_id: int = Path(..., ge=1, description="Local vn.py paper account id to restore."),
    include_snapshot: bool = Query(True, description="Include portfolio snapshot and position valuation."),
    include_recent_trades: bool = Query(True, description="Include recent paper trade events."),
) -> VnpyPaperStatusResponse:
    try:
        payload = _service(request).restore_paper_account(
            account_id,
            include_snapshot=include_snapshot,
            include_recent_trades=include_recent_trades,
        )
        payload = _with_status_dependencies(payload, scheduler, request)
        return VnpyPaperStatusResponse.model_validate(payload)
    except ValueError as exc:
        raise _bad_request(exc)
    except Exception as exc:
        raise _internal_error("Restore vn.py paper account failed", exc)


@router.put(
    "/settings",
    response_model=VnpyPaperStatusResponse,
    responses={400: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Update vn.py paper trading settings",
)
def update_vnpy_paper_settings(
    request_obj: Request,
    request: VnpyPaperSettingsUpdate,
    scheduler: RuntimeSchedulerService = Depends(get_runtime_scheduler_service),
    include_snapshot: bool = Query(True, description="Include portfolio snapshot and position valuation."),
    include_recent_trades: bool = Query(True, description="Include recent paper trade events."),
) -> VnpyPaperStatusResponse:
    try:
        payload = _service(request_obj).update_settings(
            request.model_dump(exclude_unset=True),
            include_snapshot=include_snapshot,
            include_recent_trades=include_recent_trades,
        )
        scheduler.reconcile_from_config()
        payload = _with_status_dependencies(payload, scheduler, request_obj)
        return VnpyPaperStatusResponse.model_validate(payload)
    except ValueError as exc:
        raise _bad_request(exc)
    except Exception as exc:
        raise _internal_error("Update vn.py paper settings failed", exc)


@router.post(
    "/failure-fuse/reset",
    response_model=VnpyPaperStatusResponse,
    responses={400: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Reset vn.py paper consecutive failure fuse baseline",
)
def reset_vnpy_paper_failure_fuse(
    request: Request,
    scheduler: RuntimeSchedulerService = Depends(get_runtime_scheduler_service),
) -> VnpyPaperStatusResponse:
    try:
        payload = _service(request).reset_failure_fuse()
        payload = _with_status_dependencies(payload, scheduler, request)
        return VnpyPaperStatusResponse.model_validate(payload)
    except ValueError as exc:
        raise _bad_request(exc)
    except Exception as exc:
        raise _internal_error("Reset vn.py paper failure fuse failed", exc)


@router.get(
    "/performance",
    response_model=VnpyPaperPerformanceResponse,
    responses={500: {"model": ErrorResponse}},
    summary="Get vn.py paper trading performance summary",
)
def get_vnpy_paper_performance(
    request: Request,
    run_limit: int = Query(50, ge=1, le=100, description="Recent Agent runs to aggregate."),
    created_from: Optional[datetime] = Query(None, description="Only include Agent runs created at or after this time."),
    created_to: Optional[datetime] = Query(None, description="Only include Agent runs created at or before this time."),
) -> VnpyPaperPerformanceResponse:
    try:
        return VnpyPaperPerformanceResponse.model_validate(
            _service(request).get_performance_summary(
                run_limit=run_limit,
                created_from=created_from,
                created_to=created_to,
            )
        )
    except Exception as exc:
        raise _internal_error("Get vn.py paper performance failed", exc)


@router.get(
    "/trade-plans/recovery-summary",
    response_model=VnpyPaperTradePlanRecoverySummaryResponse,
    responses={500: {"model": ErrorResponse}},
    summary="Get vn.py paper trade-plan recovery matrix",
)
def get_vnpy_paper_trade_plan_recovery_summary(
    request: Request,
    limit: int = Query(100, ge=1, le=500, description="Recent trade plans to inspect."),
    include_terminal: bool = Query(False, description="Include terminal filled plans in the matrix."),
) -> VnpyPaperTradePlanRecoverySummaryResponse:
    try:
        return VnpyPaperTradePlanRecoverySummaryResponse.model_validate(
            _service(request).get_trade_plan_recovery_summary(
                limit=limit,
                include_terminal=include_terminal,
            )
        )
    except Exception as exc:
        raise _internal_error("Get vn.py paper trade plan recovery summary failed", exc)


@router.post(
    "/trade-plans/recovery/run",
    response_model=VnpyPaperTradePlanRecoveryRunResponse,
    responses={500: {"model": ErrorResponse}},
    summary="Run one bounded vn.py paper trade-plan recovery scan",
)
def run_vnpy_paper_trade_plan_recovery(
    request: Request,
    max_plans: int = Query(5, ge=1, le=20, description="Maximum due plans to retry."),
    scan_limit: int = Query(200, ge=1, le=500, description="Recent plans to inspect for retry and timeout recovery."),
) -> VnpyPaperTradePlanRecoveryRunResponse:
    try:
        return VnpyPaperTradePlanRecoveryRunResponse.model_validate(
            _service(request).retry_due_trade_plans(
                max_plans=max_plans,
                scan_limit=scan_limit,
            )
        )
    except Exception as exc:
        raise _internal_error("Run vn.py paper trade plan recovery failed", exc)


@router.post(
    "/orders",
    response_model=VnpyPaperOrderResult,
    responses={400: {"model": ErrorResponse}, 409: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Submit one local vn.py paper order",
)
def submit_vnpy_paper_order(request_obj: Request, request: VnpyPaperOrderRequest) -> VnpyPaperOrderResult:
    try:
        payload = _service(request_obj).submit_order(
            symbol=request.symbol,
            side=request.side,
            market=request.market,
            quantity=request.quantity,
            cash_amount=request.cash_amount,
            price=request.price,
            note=request.note,
            source="manual",
            execution_route=request.execution_route,
        )
        return VnpyPaperOrderResult.model_validate(payload)
    except PortfolioBusyError as exc:
        raise _conflict_error(exc)
    except ValueError as exc:
        raise _bad_request(exc)
    except Exception as exc:
        raise _internal_error("Submit vn.py paper order failed", exc)


@router.post(
    "/vnpy-events/trades",
    response_model=VnpyPaperOrderResult,
    responses={400: {"model": ErrorResponse}, 409: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Sync one vn.py trade callback into the local paper ledger",
)
def sync_vnpy_trade_callback(
    request_obj: Request,
    request: VnpyPaperVnpyTradeCallbackRequest,
) -> VnpyPaperOrderResult:
    try:
        payload = _service(request_obj).sync_vnpy_trade_callback(
            vt_orderid=request.vt_orderid,
            vt_tradeid=request.vt_tradeid,
            symbol=request.symbol,
            side=request.side,
            market=request.market,
            quantity=request.quantity,
            price=request.price,
            trade_date=request.trade_date,
            fee=request.fee,
            tax=request.tax,
            currency=request.currency,
            raw=request.raw,
        )
        return VnpyPaperOrderResult.model_validate(payload)
    except PortfolioBusyError as exc:
        raise _conflict_error(exc)
    except ValueError as exc:
        raise _bad_request(exc)
    except Exception as exc:
        raise _internal_error("Sync vn.py trade callback failed", exc)


@router.post(
    "/vnpy-events/orders",
    response_model=VnpyPaperOrderResult,
    responses={400: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Sync one vn.py order status callback into Agent audit records",
)
def sync_vnpy_order_callback(
    request_obj: Request,
    request: VnpyPaperVnpyOrderCallbackRequest,
) -> VnpyPaperOrderResult:
    try:
        payload = _service(request_obj).sync_vnpy_order_callback(
            vt_orderid=request.vt_orderid,
            status=request.status,
            symbol=request.symbol,
            side=request.side,
            market=request.market,
            volume=request.volume,
            traded=request.traded,
            price=request.price,
            rejected_reason=request.rejected_reason,
            raw=request.raw,
        )
        return VnpyPaperOrderResult.model_validate(payload)
    except ValueError as exc:
        raise _bad_request(exc)
    except Exception as exc:
        raise _internal_error("Sync vn.py order callback failed", exc)


@router.post(
    "/vnpy-events/account",
    response_model=VnpyPaperVnpySyncResult,
    responses={400: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Sync one vn.py account snapshot for diagnostics",
)
def sync_vnpy_account_callback(
    request_obj: Request,
    request: VnpyPaperVnpyAccountCallbackRequest,
) -> VnpyPaperVnpySyncResult:
    try:
        payload = _service(request_obj).sync_vnpy_account_callback(
            account_id=request.account_id,
            balance=request.balance,
            available=request.available,
            frozen=request.frozen,
            margin=request.margin,
            close_profit=request.close_profit,
            holding_profit=request.holding_profit,
            currency=request.currency,
            raw=request.raw,
        )
        return VnpyPaperVnpySyncResult.model_validate(payload)
    except ValueError as exc:
        raise _bad_request(exc)
    except Exception as exc:
        raise _internal_error("Sync vn.py account callback failed", exc)


@router.post(
    "/vnpy-events/positions",
    response_model=VnpyPaperVnpySyncResult,
    responses={400: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Sync vn.py position snapshots for diagnostics",
)
def sync_vnpy_positions_callback(
    request_obj: Request,
    request: VnpyPaperVnpyPositionsCallbackRequest,
) -> VnpyPaperVnpySyncResult:
    try:
        payload = _service(request_obj).sync_vnpy_positions_callback(
            positions=[item.model_dump(exclude_none=True) for item in request.positions],
            raw=request.raw,
        )
        return VnpyPaperVnpySyncResult.model_validate(payload)
    except ValueError as exc:
        raise _bad_request(exc)
    except Exception as exc:
        raise _internal_error("Sync vn.py positions callback failed", exc)


@router.post(
    "/vnpy-events/attach",
    response_model=VnpyPaperVnpySyncResult,
    responses={400: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Attach injected vn.py EventEngine callbacks to DSA sync handlers",
)
def attach_vnpy_event_engine(request_obj: Request) -> VnpyPaperVnpySyncResult:
    try:
        existing = getattr(request_obj.app.state, "vnpy_paper_event_bridge", None)
        if callable(getattr(existing, "unregister", None)):
            existing.unregister()
        bridge = _service(request_obj).attach_vnpy_event_engine()
        request_obj.app.state.vnpy_paper_event_bridge = bridge
        return VnpyPaperVnpySyncResult.model_validate(
            {
                "accepted": True,
                "status": "attached",
                "message": "vn.py EventEngine callbacks attached.",
                "reason": None,
                "raw": bridge.status(),
            }
        )
    except ValueError as exc:
        raise _bad_request(exc)
    except Exception as exc:
        raise _internal_error("Attach vn.py EventEngine callbacks failed", exc)


@router.post(
    "/auto/run",
    response_model=VnpyPaperAutoRunResponse,
    responses={400: {"model": ErrorResponse}, 424: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Run AlphaSift-driven vn.py paper trading once",
)
def run_vnpy_paper_auto_once(
    request_obj: Request,
    request: Optional[VnpyPaperAutoRunRequest] = Body(None),
) -> VnpyPaperAutoRunResponse:
    try:
        payload = request or VnpyPaperAutoRunRequest()
        return VnpyPaperAutoRunResponse.model_validate(
            _service(request_obj).run_auto_trade_once(
                execution_mode_override=payload.execution_mode,
                ignore_auto_trade_enabled=payload.ignore_auto_trade_enabled,
            )
        )
    except HTTPException:
        raise
    except ValueError as exc:
        raise _bad_request(exc)
    except Exception as exc:
        raise _internal_error("Run vn.py paper auto trading failed", exc)


@router.post(
    "/trade-plans/{plan_uid}/approve",
    response_model=VnpyPaperOrderResult,
    responses={400: {"model": ErrorResponse}, 409: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Approve and submit one manual vn.py paper trade plan",
)
def approve_vnpy_paper_trade_plan(request: Request, plan_uid: str) -> VnpyPaperOrderResult:
    try:
        return VnpyPaperOrderResult.model_validate(_service(request).approve_trade_plan(plan_uid))
    except PortfolioBusyError as exc:
        raise _conflict_error(exc)
    except ValueError as exc:
        raise _bad_request(exc)
    except Exception as exc:
        raise _internal_error("Approve vn.py paper trade plan failed", exc)


@router.post(
    "/trade-plans/{plan_uid}/retry",
    response_model=VnpyPaperOrderResult,
    responses={400: {"model": ErrorResponse}, 409: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Retry one failed or recoverable skipped vn.py paper trade plan",
)
def retry_vnpy_paper_trade_plan(request: Request, plan_uid: str) -> VnpyPaperOrderResult:
    try:
        return VnpyPaperOrderResult.model_validate(_service(request).retry_trade_plan(plan_uid))
    except PortfolioBusyError as exc:
        raise _conflict_error(exc)
    except ValueError as exc:
        raise _bad_request(exc)
    except Exception as exc:
        raise _internal_error("Retry vn.py paper trade plan failed", exc)


@router.post(
    "/trade-plans/{plan_uid}/cancel",
    response_model=VnpyPaperOrderResult,
    responses={400: {"model": ErrorResponse}, 409: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Request cancellation for one submitted vn.py paper trade plan",
)
def cancel_vnpy_paper_trade_plan(request: Request, plan_uid: str) -> VnpyPaperOrderResult:
    try:
        return VnpyPaperOrderResult.model_validate(_service(request).cancel_trade_plan(plan_uid))
    except PortfolioBusyError as exc:
        raise _conflict_error(exc)
    except ValueError as exc:
        raise _bad_request(exc)
    except Exception as exc:
        raise _internal_error("Cancel vn.py paper trade plan failed", exc)


@router.get(
    "/agent-runs",
    response_model=VnpyPaperAgentRunListResponse,
    responses={500: {"model": ErrorResponse}},
    summary="List stock-selection agent runs used by vn.py paper trading",
)
def list_vnpy_paper_agent_runs(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    trigger_source: Optional[str] = Query(None, min_length=1, max_length=64),
    strategy: Optional[str] = Query(None, min_length=1, max_length=64),
    market: Optional[str] = Query(None, min_length=1, max_length=16),
    status: Optional[str] = Query(None, min_length=1, max_length=32),
    created_from: Optional[datetime] = Query(None, description="Only include runs created at or after this time."),
    created_to: Optional[datetime] = Query(None, description="Only include runs created at or before this time."),
) -> VnpyPaperAgentRunListResponse:
    try:
        return VnpyPaperAgentRunListResponse.model_validate(
            _agent_repo().list_runs(
                limit=limit,
                offset=offset,
                trigger_source=trigger_source,
                strategy=strategy,
                market=market,
                status=status,
                created_from=created_from,
                created_to=created_to,
            )
        )
    except Exception as exc:
        raise _internal_error("List vn.py paper agent runs failed", exc)


@router.get(
    "/agent-runs/export",
    response_model=VnpyPaperAgentRunExportResponse,
    responses={500: {"model": ErrorResponse}},
    summary="Export recent stock-selection agent runs used by vn.py paper trading",
)
def export_vnpy_paper_agent_runs(
    limit: int = Query(50, ge=1, le=100),
    include_details: bool = Query(True, description="Include decisions, trade plans and timeline for each run."),
    trigger_source: Optional[str] = Query(None, min_length=1, max_length=64),
    strategy: Optional[str] = Query(None, min_length=1, max_length=64),
    market: Optional[str] = Query(None, min_length=1, max_length=16),
    status: Optional[str] = Query(None, min_length=1, max_length=32),
    created_from: Optional[datetime] = Query(None, description="Only include runs created at or after this time."),
    created_to: Optional[datetime] = Query(None, description="Only include runs created at or before this time."),
) -> VnpyPaperAgentRunExportResponse:
    try:
        repo = _agent_repo()
        payload = repo.list_runs(
            limit=limit,
            offset=0,
            trigger_source=trigger_source,
            strategy=strategy,
            market=market,
            status=status,
            created_from=created_from,
            created_to=created_to,
        )
        runs = [item for item in list(payload.get("items") or []) if isinstance(item, dict)]
        if include_details:
            items = []
            for item in runs:
                detail = repo.get_run_detail(str(item.get("run_uid") or ""))
                items.append(detail if isinstance(detail, dict) else item)
        else:
            items = runs
        return VnpyPaperAgentRunExportResponse.model_validate({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "limit": int(payload.get("limit") or limit),
            "include_details": bool(include_details),
            "count": len(items),
            "items": items,
        })
    except Exception as exc:
        raise _internal_error("Export vn.py paper agent runs failed", exc)


@router.get(
    "/agent-runs/daily-summary",
    response_model=VnpyPaperAgentDailySummaryResponse,
    responses={500: {"model": ErrorResponse}},
    summary="Summarize one trading day's stock-selection agent runs",
)
def get_vnpy_paper_agent_daily_summary(
    target_date: Optional[date] = Query(None, alias="date", description="Local date to summarize."),
    limit: int = Query(100, ge=1, le=500, description="Maximum runs to inspect for detail-level aggregates."),
    trigger_source: Optional[str] = Query(None, min_length=1, max_length=64),
    strategy: Optional[str] = Query(None, min_length=1, max_length=64),
    market: Optional[str] = Query(None, min_length=1, max_length=16),
    status: Optional[str] = Query(None, min_length=1, max_length=32),
) -> VnpyPaperAgentDailySummaryResponse:
    try:
        return VnpyPaperAgentDailySummaryResponse.model_validate(
            _agent_repo().summarize_daily_runs(
                target_date=target_date,
                limit=limit,
                trigger_source=trigger_source,
                strategy=strategy,
                market=market,
                status=status,
            )
        )
    except Exception as exc:
        raise _internal_error("Summarize vn.py paper agent daily runs failed", exc)


@router.get(
    "/agent-runs/data-quality-trends",
    response_model=VnpyPaperAgentDataQualityTrendsResponse,
    responses={500: {"model": ErrorResponse}},
    summary="Summarize cross-run stock-selection data quality trends",
)
def get_vnpy_paper_agent_data_quality_trends(
    days: int = Query(30, ge=1, le=90),
    trigger_source: Optional[str] = Query(None, min_length=1, max_length=64),
    strategy: Optional[str] = Query(None, min_length=1, max_length=64),
    market: Optional[str] = Query(None, min_length=1, max_length=16),
    status: Optional[str] = Query(None, min_length=1, max_length=32),
) -> VnpyPaperAgentDataQualityTrendsResponse:
    try:
        return VnpyPaperAgentDataQualityTrendsResponse.model_validate(
            _agent_repo().summarize_data_quality_trends(
                days=days,
                trigger_source=trigger_source,
                strategy=strategy,
                market=market,
                status=status,
            )
        )
    except Exception as exc:
        raise _internal_error("Summarize vn.py paper agent data quality trends failed", exc)


@router.post(
    "/agent-runs/backtest",
    response_model=VnpyPaperAgentBacktestResponse,
    responses={400: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Evaluate persisted Agent candidates against forward daily bars",
)
def run_vnpy_paper_agent_backtest(
    payload: VnpyPaperAgentBacktestRequest,
) -> VnpyPaperAgentBacktestResponse:
    try:
        result = StockSelectionAgentBacktestService().evaluate(
            strategy=payload.strategy,
            market=payload.market,
            created_from=payload.created_from,
            created_to=payload.created_to,
            eval_windows=payload.eval_windows,
            include_skipped=payload.include_skipped,
            max_decisions=payload.max_decisions,
            refresh_missing=payload.refresh_missing,
            neutral_band_pct=payload.neutral_band_pct,
        )
        return VnpyPaperAgentBacktestResponse.model_validate(result)
    except ValueError as exc:
        raise _bad_request(exc) from exc
    except Exception as exc:
        raise _internal_error("Run Agent forward evaluation failed", exc)


@router.get(
    "/agent-runs/cross-run-quality",
    response_model=VnpyPaperAgentCrossRunQualityResponse,
    responses={500: {"model": ErrorResponse}},
    summary="Get current cross-run forward-quality gate state",
)
def get_vnpy_paper_agent_cross_run_quality(
    request: Request,
) -> VnpyPaperAgentCrossRunQualityResponse:
    try:
        return VnpyPaperAgentCrossRunQualityResponse.model_validate(
            _service(request).get_cross_run_quality_status()
        )
    except Exception as exc:
        raise _internal_error("Get Agent cross-run quality failed", exc)


@router.post(
    "/agent-runs/{run_uid}/llm-recap",
    response_model=VnpyPaperAgentRunRecapResponse,
    responses={404: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Generate an optional LLM recap for one stock-selection agent run",
)
def generate_vnpy_paper_agent_run_llm_recap(
    run_uid: str,
    request: Request,
    payload: VnpyPaperAgentRunRecapRequest = Body(default_factory=VnpyPaperAgentRunRecapRequest),
) -> VnpyPaperAgentRunRecapResponse:
    try:
        result = _service(request).generate_agent_run_llm_recap(
            run_uid,
            max_output_tokens=payload.max_output_tokens,
        )
        return VnpyPaperAgentRunRecapResponse.model_validate(result)
    except ValueError as exc:
        if str(exc) == "agent_run_not_found":
            raise api_error(404, "not_found", "Agent run not found.")
        raise api_error(400, "invalid_request", str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        raise _internal_error("Generate vn.py paper agent run LLM recap failed", exc)


@router.put(
    "/agent-runs/{run_uid}/feedback",
    response_model=VnpyPaperAgentRunFeedbackResponse,
    responses={404: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Upsert human acceptance feedback for one stock-selection agent run",
)
def put_vnpy_paper_agent_run_feedback(
    run_uid: str,
    payload: VnpyPaperAgentRunFeedbackRequest,
) -> VnpyPaperAgentRunFeedbackResponse:
    try:
        repo = _agent_repo()
        feedback = repo.upsert_run_feedback(
            run_uid,
            verdict=payload.verdict,
            note=payload.note,
            reviewer=payload.reviewer,
            source="web",
        )
        if feedback is None:
            raise api_error(404, "not_found", "Agent run not found.")
        detail = repo.get_run_detail(run_uid)
        if detail is None:
            raise api_error(404, "not_found", "Agent run not found.")
        return VnpyPaperAgentRunFeedbackResponse.model_validate({
            "accepted": True,
            "run_uid": run_uid,
            "human_feedback": feedback,
            "run_detail": detail,
        })
    except HTTPException:
        raise
    except Exception as exc:
        raise _internal_error("Update vn.py paper Agent run feedback failed", exc)


@router.get(
    "/agent-runs/{run_uid}",
    response_model=VnpyPaperAgentRunDetail,
    responses={404: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Get one stock-selection agent run with candidate decisions",
)
def get_vnpy_paper_agent_run(run_uid: str) -> VnpyPaperAgentRunDetail:
    try:
        payload = _agent_repo().get_run_detail(run_uid)
        if payload is None:
            raise api_error(404, "not_found", "Agent run not found.")
        return VnpyPaperAgentRunDetail.model_validate(payload)
    except HTTPException:
        raise
    except Exception as exc:
        raise _internal_error("Get vn.py paper agent run failed", exc)
