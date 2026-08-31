# -*- coding: utf-8 -*-
"""Run a bounded online stock Agent to vn.py paper acceptance check.

The default mode is read-safe ``dry_run``. Real order submission is restricted to
the built-in ``DsaSimulatedGateway`` and requires ``--allow-simulated-orders``.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


ACTIVE_VNPY_PLAN_STATUSES = {"planned", "submitted", "part_filled", "cancel_requested"}
CROSS_MARKET_STRATEGY_ID = "cross_market_global_sector_rotation_v1.4_staged"


def _utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _validated_base_url(parser: argparse.ArgumentParser, value: str) -> str:
    candidate = str(value or "").strip().rstrip("/")
    parsed = urlsplit(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        parser.error("--base-url must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        parser.error("--base-url must not contain embedded credentials")
    return candidate


def _request_json(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    payload: Dict[str, Any] | None = None,
    timeout_seconds: float,
) -> Dict[str, Any]:
    encoded = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Accept": "application/json"}
    if encoded is not None:
        headers["Content-Type"] = "application/json"
    request = Request(
        f"{base_url.rstrip('/')}{path}",
        data=encoded,
        headers=headers,
        method=method,
    )
    with urlopen(request, timeout=timeout_seconds) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not isinstance(result, dict):
        raise ValueError(f"{method} {path} must return a JSON object")
    return result


def _number(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _snapshot_view(status: Dict[str, Any]) -> Dict[str, Any]:
    account = status.get("account") if isinstance(status.get("account"), dict) else {}
    snapshot = status.get("snapshot") if isinstance(status.get("snapshot"), dict) else {}
    positions: Dict[str, float] = {}
    accounts = snapshot.get("accounts") if isinstance(snapshot.get("accounts"), list) else []
    account_id = account.get("id")
    selected = next(
        (
            item
            for item in accounts
            if isinstance(item, dict) and item.get("account_id") == account_id
        ),
        accounts[0] if accounts and isinstance(accounts[0], dict) else {},
    )
    for item in selected.get("positions") or []:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "").strip()
        quantity = _number(item.get("quantity"))
        if symbol and quantity is not None:
            positions[symbol] = quantity
    cash = _number(selected.get("total_cash"))
    if cash is None:
        cash = _number(snapshot.get("total_cash"))
    return {
        "account_id": account_id,
        "cash": cash,
        "positions": positions,
    }


def _runtime_view(status: Dict[str, Any]) -> Dict[str, Any]:
    diagnostics = status.get("diagnostics")
    diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
    runtime = diagnostics.get("vnpy_runtime")
    runtime = runtime if isinstance(runtime, dict) else {}
    connect = runtime.get("connect") if isinstance(runtime.get("connect"), dict) else {}
    bridge = runtime.get("event_bridge")
    bridge = bridge if isinstance(bridge, dict) else {}
    return {
        "available": bool(runtime.get("available")),
        "gateway_class": runtime.get("gateway_class"),
        "gateway_name": runtime.get("gateway_name"),
        "connection_status": connect.get("status"),
        "connected": connect.get("connected") is True,
        "event_bridge_registered": bridge.get("registered") is True,
        "event_bridge_registered_count": int(bridge.get("registered_count") or 0),
    }


def _account_ids(payload: Dict[str, Any]) -> set[int]:
    result = set()
    for item in payload.get("items") or []:
        if not isinstance(item, dict):
            continue
        try:
            account_id = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        if account_id > 0:
            result.add(account_id)
    return result


def _agent_run_uids(payload: Dict[str, Any]) -> set[str]:
    return {
        str(item.get("run_uid") or "").strip()
        for item in payload.get("items") or []
        if isinstance(item, dict) and str(item.get("run_uid") or "").strip()
    }


def _scheduler_execution_task_name(
    status: Dict[str, Any],
    *,
    execution_mode_override: str | None = None,
) -> str:
    settings = status.get("settings")
    settings = settings if isinstance(settings, dict) else {}
    strategy = str(settings.get("auto_strategy") or "").strip()
    execution_mode = str(
        execution_mode_override or settings.get("auto_execution_mode") or ""
    ).strip()
    if strategy == CROSS_MARKET_STRATEGY_ID and execution_mode == "vnpy_paper":
        return "cross_market_intraday_entry_scan"
    return "vnpy_paper_auto_trade"


def _matching_scheduler_event(
    payload: Dict[str, Any],
    run_uid: str,
    task_name: str = "vnpy_paper_auto_trade",
) -> Dict[str, Any] | None:
    for item in reversed(payload.get("items") or []):
        if not isinstance(item, dict) or item.get("name") != task_name:
            continue
        details = item.get("details") if isinstance(item.get("details"), dict) else {}
        if (
            str(details.get("agent_run_uid") or "").strip() == run_uid
            and item.get("status") in {"completed", "skipped", "failed"}
        ):
            return item
    return None


def _scheduler_auto_trade_is_running(
    status: Dict[str, Any],
    task_name: str = "vnpy_paper_auto_trade",
) -> bool:
    scheduler = status.get("scheduler")
    scheduler = scheduler if isinstance(scheduler, dict) else {}
    for item in scheduler.get("background_tasks") or []:
        if not isinstance(item, dict) or item.get("name") != task_name:
            continue
        return bool(item.get("running") or item.get("previous_generation_running"))
    return False


def _decision_is_traceable(decision: Dict[str, Any]) -> bool:
    if not str(decision.get("symbol") or "").strip():
        return False
    if not str(decision.get("action") or "").strip():
        return False
    if not str(decision.get("status") or "").strip():
        return False
    return any(
        (
            str(decision.get("reason") or "").strip(),
            str(decision.get("rationale") or "").strip(),
            bool(decision.get("strategy_evidence")),
            bool(decision.get("risk_review")),
            bool(decision.get("agent_review")),
        )
    )


def _position_deltas(
    before: Dict[str, float], after: Dict[str, float]
) -> Dict[str, float]:
    symbols = set(before) | set(after)
    return {
        symbol: round(after.get(symbol, 0.0) - before.get(symbol, 0.0), 8)
        for symbol in sorted(symbols)
        if abs(after.get(symbol, 0.0) - before.get(symbol, 0.0)) > 1e-8
    }


def evaluate_acceptance(
    *,
    execution_mode: str,
    run_response: Dict[str, Any],
    run_detail: Dict[str, Any],
    before_status: Dict[str, Any],
    after_status: Dict[str, Any],
    min_candidates: int,
    max_failed_plans: int,
    require_execution: bool,
    settings_restored: bool,
    account_environment_restored: bool = True,
    cash_tolerance: float = 0.01,
) -> Dict[str, Any]:
    failures: list[str] = []
    before = _snapshot_view(before_status)
    after = _snapshot_view(after_status)
    runtime = _runtime_view(after_status)
    decisions = [item for item in run_detail.get("decisions") or [] if isinstance(item, dict)]
    plans = [item for item in run_detail.get("trade_plans") or [] if isinstance(item, dict)]
    candidate_count = int(run_detail.get("candidate_count") or 0)
    run_uid = str(run_response.get("agent_run_uid") or "").strip()
    detail_uid = str(run_detail.get("run_uid") or "").strip()
    failed_plans = [item for item in plans if str(item.get("status") or "") == "failed"]
    filled_plans = [item for item in plans if str(item.get("status") or "") == "filled"]
    active_plans = [
        item
        for item in plans
        if str(item.get("status") or "") in ACTIVE_VNPY_PLAN_STATUSES
    ]

    if run_response.get("accepted") is not True:
        failures.append("agent_run_not_accepted")
    if not run_uid or run_uid != detail_uid:
        failures.append("agent_run_identity_mismatch")
    if str(run_detail.get("status") or "").lower() in {"", "running", "failed"}:
        failures.append("agent_run_not_completed")
    if candidate_count < min_candidates:
        failures.append("candidate_count_below_threshold")
    if len(decisions) < candidate_count:
        failures.append("candidate_decisions_incomplete")
    if len(plans) < candidate_count:
        failures.append("candidate_trade_plans_incomplete")
    if any(not _decision_is_traceable(item) for item in decisions):
        failures.append("candidate_decision_not_traceable")
    decision_ids = {item.get("id") for item in decisions if item.get("id") is not None}
    if any(
        item.get("decision_id") is not None and item.get("decision_id") not in decision_ids
        for item in plans
    ):
        failures.append("trade_plan_decision_link_missing")
    if len(failed_plans) > max_failed_plans:
        failures.append("failed_trade_plans_above_threshold")
    if before.get("account_id") != after.get("account_id"):
        failures.append("active_account_changed")
    if not settings_restored:
        failures.append("settings_not_restored")
    if not account_environment_restored:
        failures.append("account_environment_not_restored")

    cash_delta = None
    if before.get("cash") is not None and after.get("cash") is not None:
        cash_delta = round(float(after["cash"]) - float(before["cash"]), 8)
    position_deltas = _position_deltas(before["positions"], after["positions"])

    if execution_mode == "dry_run":
        if any(item.get("trade_id") is not None for item in plans + decisions):
            failures.append("dry_run_created_trade")
        if cash_delta is not None and abs(cash_delta) > cash_tolerance:
            failures.append("dry_run_changed_cash")
        if position_deltas:
            failures.append("dry_run_changed_positions")
    else:
        if not runtime["available"]:
            failures.append("vnpy_runtime_unavailable")
        if not runtime["connected"] or runtime["connection_status"] != "connected":
            failures.append("vnpy_gateway_not_connected")
        if not runtime["event_bridge_registered"]:
            failures.append("vnpy_event_bridge_not_registered")
        if runtime["event_bridge_registered_count"] < 4:
            failures.append("vnpy_event_bridge_incomplete")
        if active_plans:
            failures.append("vnpy_trade_plans_not_terminal")
        if require_execution and not filled_plans:
            failures.append("vnpy_fill_not_observed")
        decision_trade_ids = {
            item.get("trade_id") for item in decisions if item.get("trade_id") is not None
        }
        if any(
            item.get("trade_id") is None or item.get("trade_id") not in decision_trade_ids
            for item in filled_plans
        ):
            failures.append("filled_plan_trade_link_incomplete")

        portfolio_change = run_detail.get("portfolio_change")
        portfolio_change = portfolio_change if isinstance(portfolio_change, dict) else {}
        change_items = [
            item for item in portfolio_change.get("items") or [] if isinstance(item, dict)
        ]
        if filled_plans and int(portfolio_change.get("booked_plan_count") or 0) < len(
            filled_plans
        ):
            failures.append("portfolio_change_missing_filled_plan")
        expected_cash_delta = round(
            sum(_number(item.get("net_cash_flow")) or 0.0 for item in change_items),
            8,
        )
        if (
            filled_plans
            and cash_delta is not None
            and abs(cash_delta - expected_cash_delta) > cash_tolerance
        ):
            failures.append("portfolio_cash_delta_mismatch")
        for item in change_items:
            symbol = str(item.get("symbol") or "").strip()
            expected_delta = _number(item.get("net_quantity"))
            if symbol and expected_delta is not None:
                actual_delta = position_deltas.get(symbol, 0.0)
                if abs(actual_delta - expected_delta) > 1e-8:
                    failures.append("portfolio_position_delta_mismatch")
                    break

    return {
        "ok": not failures,
        "failures": failures,
        "execution_mode": execution_mode,
        "run_uid": detail_uid or run_uid or None,
        "candidate_count": candidate_count,
        "decision_count": len(decisions),
        "trade_plan_count": len(plans),
        "filled_plan_count": len(filled_plans),
        "failed_plan_count": len(failed_plans),
        "active_plan_count": len(active_plans),
        "cash_delta": cash_delta,
        "position_deltas": position_deltas,
        "runtime": runtime,
        "settings_restored": settings_restored,
        "account_environment_restored": account_environment_restored,
    }


def _plans_are_terminal(execution_mode: str, detail: Dict[str, Any]) -> bool:
    if execution_mode == "dry_run":
        return True
    plans = [item for item in detail.get("trade_plans") or [] if isinstance(item, dict)]
    return not any(
        str(item.get("status") or "") in ACTIVE_VNPY_PLAN_STATUSES for item in plans
    )


def _run_is_terminal_and_coherent(
    execution_mode: str, detail: Dict[str, Any]
) -> bool:
    if not _plans_are_terminal(execution_mode, detail):
        return False
    decisions = {
        item.get("id"): item
        for item in detail.get("decisions") or []
        if isinstance(item, dict) and item.get("id") is not None
    }
    for plan in detail.get("trade_plans") or []:
        if not isinstance(plan, dict) or plan.get("decision_id") is None:
            continue
        decision = decisions.get(plan.get("decision_id"))
        if decision is None:
            return False
        if str(decision.get("status") or "") != str(plan.get("status") or ""):
            return False
    return True


def _bounded_float(
    parser: argparse.ArgumentParser,
    name: str,
    value: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    if value < minimum or value > maximum:
        parser.error(f"{name} must be between {minimum} and {maximum}")
    return value


def _bounded_int(
    parser: argparse.ArgumentParser,
    name: str,
    value: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if value < minimum or value > maximum:
        parser.error(f"{name} must be between {minimum} and {maximum}")
    return value


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--execution-mode", choices=("dry_run", "vnpy_paper"), default="dry_run"
    )
    parser.add_argument(
        "--trigger-mode",
        choices=("direct", "scheduler"),
        default="direct",
        help="Trigger directly or wait for the runtime scheduler to create the run.",
    )
    parser.add_argument(
        "--allow-simulated-orders",
        action="store_true",
        help="Allow order submission only when DsaSimulatedGateway is active.",
    )
    parser.add_argument(
        "--temporarily-disable-time-gate",
        action="store_true",
        help="Temporarily disable the time gate and restore it in finally.",
    )
    parser.add_argument(
        "--allow-no-fill",
        action="store_true",
        help="Accept an all-skipped vn.py run without requiring a fill.",
    )
    parser.add_argument(
        "--isolated-account",
        action="store_true",
        help="Create a clean paper account, then restore the original account.",
    )
    parser.add_argument(
        "--keep-isolated-account-visible",
        action="store_true",
        help="Keep the restored test account visible in archived account history.",
    )
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    parser.add_argument("--poll-interval-seconds", type=float, default=0.5)
    parser.add_argument("--request-timeout-seconds", type=float, default=120.0)
    parser.add_argument("--min-candidates", type=int, default=1)
    parser.add_argument("--max-failed-plans", type=int, default=0)
    parser.add_argument("--output-json", type=Path)
    args = parser.parse_args(list(argv) if argv is not None else None)

    base_url = _validated_base_url(parser, args.base_url)
    timeout = _bounded_float(
        parser, "--timeout-seconds", args.timeout_seconds, minimum=1.0, maximum=3600.0
    )
    poll_interval = _bounded_float(
        parser,
        "--poll-interval-seconds",
        args.poll_interval_seconds,
        minimum=0.05,
        maximum=30.0,
    )
    request_timeout = _bounded_float(
        parser,
        "--request-timeout-seconds",
        args.request_timeout_seconds,
        minimum=0.1,
        maximum=600.0,
    )
    min_candidates = _bounded_int(
        parser, "--min-candidates", args.min_candidates, minimum=0, maximum=50
    )
    max_failed_plans = _bounded_int(
        parser, "--max-failed-plans", args.max_failed_plans, minimum=0, maximum=50
    )
    if args.execution_mode == "vnpy_paper" and not args.allow_simulated_orders:
        parser.error("vnpy_paper requires --allow-simulated-orders")
    if args.temporarily_disable_time_gate and args.execution_mode != "vnpy_paper":
        parser.error("--temporarily-disable-time-gate requires vnpy_paper mode")
    if args.isolated_account and args.execution_mode != "vnpy_paper":
        parser.error("--isolated-account requires vnpy_paper mode")
    if args.keep_isolated_account_visible and not args.isolated_account:
        parser.error("--keep-isolated-account-visible requires --isolated-account")
    if args.trigger_mode == "scheduler" and (
        args.execution_mode != "vnpy_paper" or not args.isolated_account
    ):
        parser.error("scheduler mode requires vnpy_paper and --isolated-account")

    started_at = _utc_iso()
    original_status: Dict[str, Any] = {}
    before_status: Dict[str, Any] = {}
    after_status: Dict[str, Any] = {}
    final_status: Dict[str, Any] = {}
    run_response: Dict[str, Any] = {}
    run_detail: Dict[str, Any] = {}
    scheduler_event: Dict[str, Any] = {}
    changed_time_gate = False
    changed_auto_trade = False
    changed_auto_interval = False
    changed_execution_mode = False
    settings_restored = True
    account_environment_restored = not args.isolated_account
    isolated_account_cleanup_ok = not args.isolated_account
    original_account_id: int | None = None
    isolated_account_ids: set[int] = set()
    baseline_account_ids: set[int] = set()
    original_time_gate = True
    original_auto_trade = False
    original_auto_interval = 1440
    original_execution_mode = "paper"
    scheduler_task_name = "vnpy_paper_auto_trade"
    errors: list[str] = []
    try:
        original_status = _request_json(
            base_url,
            "/api/v1/vnpy-paper/status?include_snapshot=true&include_recent_trades=false",
            timeout_seconds=request_timeout,
        )
        before_status = original_status
        runtime = _runtime_view(original_status)
        if args.execution_mode == "vnpy_paper" and not str(
            runtime.get("gateway_class") or ""
        ).endswith(":DsaSimulatedGateway"):
            parser.error("order acceptance is restricted to DsaSimulatedGateway")

        settings = original_status.get("settings")
        settings = settings if isinstance(settings, dict) else {}
        scheduler_task_name = _scheduler_execution_task_name(
            original_status,
            execution_mode_override=(
                args.execution_mode if args.trigger_mode == "scheduler" else None
            ),
        )
        if args.trigger_mode == "scheduler" and _scheduler_auto_trade_is_running(
            original_status,
            scheduler_task_name,
        ):
            raise ValueError("scheduler acceptance requires an idle auto-trade task")
        if args.trigger_mode == "scheduler" and not {
            "auto_trade_enabled",
            "auto_trade_time_gate_enabled",
            "auto_interval_minutes",
            "auto_execution_mode",
        } <= set(settings):
            raise ValueError("status is missing scheduler settings required for restore")
        if (
            args.temporarily_disable_time_gate
            and "auto_trade_time_gate_enabled" not in settings
        ):
            raise ValueError("status is missing auto_trade_time_gate_enabled")
        original_time_gate = bool(settings.get("auto_trade_time_gate_enabled", True))
        original_auto_trade = bool(settings.get("auto_trade_enabled", False))
        original_auto_interval = int(settings.get("auto_interval_minutes", 1440))
        original_execution_mode = str(settings.get("auto_execution_mode") or "paper")
        original_account_id = _snapshot_view(original_status).get("account_id")

        if args.isolated_account:
            if original_account_id is None:
                raise ValueError("isolated acceptance requires an active paper account")
            if "auto_trade_enabled" not in settings:
                raise ValueError("status is missing auto_trade_enabled")
            accounts_before = _request_json(
                base_url,
                "/api/v1/vnpy-paper/accounts?include_inactive=true&include_hidden=true",
                timeout_seconds=request_timeout,
            )
            baseline_account_ids = _account_ids(accounts_before)
            if original_auto_trade:
                # An uncertain response may already have paused the scheduler.
                changed_auto_trade = True
                _request_json(
                    base_url,
                    "/api/v1/vnpy-paper/settings",
                    method="PUT",
                    payload={"auto_trade_enabled": False},
                    timeout_seconds=request_timeout,
                )
            before_status = _request_json(
                base_url,
                (
                    "/api/v1/vnpy-paper/account/reset"
                    "?include_snapshot=true&include_recent_trades=false"
                ),
                method="POST",
                payload={},
                timeout_seconds=request_timeout,
            )
            isolated_id = _snapshot_view(before_status).get("account_id")
            if isolated_id is None or isolated_id == original_account_id:
                raise ValueError("account reset did not create an isolated paper account")
            isolated_account_ids.add(int(isolated_id))

        if (
            args.temporarily_disable_time_gate
            and args.trigger_mode == "direct"
            and original_time_gate
        ):
            # Treat an uncertain PUT response as potentially applied and restore in finally.
            changed_time_gate = True
            _request_json(
                base_url,
                "/api/v1/vnpy-paper/settings",
                method="PUT",
                payload={"auto_trade_time_gate_enabled": False},
                timeout_seconds=request_timeout,
            )

        deadline = time.monotonic() + timeout
        if args.trigger_mode == "direct":
            run_response = _request_json(
                base_url,
                "/api/v1/vnpy-paper/auto/run",
                method="POST",
                payload={
                    "execution_mode": args.execution_mode,
                    "ignore_auto_trade_enabled": True,
                },
                timeout_seconds=request_timeout,
            )
        else:
            baseline_runs = _request_json(
                base_url,
                "/api/v1/vnpy-paper/agent-runs?limit=100&trigger_source=vnpy_paper_auto",
                timeout_seconds=request_timeout,
            )
            baseline_run_uids = _agent_run_uids(baseline_runs)
            changed_auto_trade = True
            changed_time_gate = original_time_gate
            changed_auto_interval = original_auto_interval != 1
            changed_execution_mode = original_execution_mode != "vnpy_paper"
            _request_json(
                base_url,
                "/api/v1/vnpy-paper/settings",
                method="PUT",
                payload={
                    "auto_trade_enabled": True,
                    "auto_trade_time_gate_enabled": False,
                    "auto_interval_minutes": 1,
                    "auto_execution_mode": "vnpy_paper",
                },
                timeout_seconds=request_timeout,
            )
            while time.monotonic() < deadline:
                runs = _request_json(
                    base_url,
                    "/api/v1/vnpy-paper/agent-runs?limit=100&trigger_source=vnpy_paper_auto",
                    timeout_seconds=request_timeout,
                )
                new_uids = _agent_run_uids(runs) - baseline_run_uids
                if new_uids:
                    run_uid = next(
                        (
                            str(item.get("run_uid") or "").strip()
                            for item in runs.get("items") or []
                            if isinstance(item, dict)
                            and str(item.get("run_uid") or "").strip() in new_uids
                        ),
                        sorted(new_uids)[0],
                    )
                    run_response = {"accepted": True, "agent_run_uid": run_uid}
                    # Stop future intervals while allowing the in-flight worker to finish.
                    _request_json(
                        base_url,
                        "/api/v1/vnpy-paper/settings",
                        method="PUT",
                        payload={"auto_trade_enabled": False},
                        timeout_seconds=request_timeout,
                    )
                    break
                time.sleep(poll_interval)

        run_uid = str(run_response.get("agent_run_uid") or "").strip()
        if run_uid:
            while True:
                run_detail = _request_json(
                    base_url,
                    f"/api/v1/vnpy-paper/agent-runs/{run_uid}",
                    timeout_seconds=request_timeout,
                )
                run_ready = _run_is_terminal_and_coherent(args.execution_mode, run_detail)
                if args.trigger_mode == "scheduler":
                    events = _request_json(
                        base_url,
                        "/api/v1/vnpy-paper/task-events?"
                        f"name={scheduler_task_name}&limit=100",
                        timeout_seconds=request_timeout,
                    )
                    scheduler_event = _matching_scheduler_event(
                        events,
                        run_uid,
                        scheduler_task_name,
                    ) or {}
                    run_ready = run_ready and bool(scheduler_event)
                if run_ready:
                    break
                if time.monotonic() >= deadline:
                    break
                time.sleep(poll_interval)
        after_status = _request_json(
            base_url,
            "/api/v1/vnpy-paper/status?include_snapshot=true&include_recent_trades=false",
            timeout_seconds=request_timeout,
        )
    except Exception as exc:  # pragma: no cover - exercised through CLI behavior
        errors.append(f"{type(exc).__name__}: {exc}")
    finally:
        if args.isolated_account and original_account_id is not None:
            restored_status: Dict[str, Any] = {}
            try:
                restored_status = _request_json(
                    base_url,
                    (
                        f"/api/v1/vnpy-paper/accounts/{original_account_id}/restore"
                        "?include_snapshot=false&include_recent_trades=false"
                    ),
                    method="POST",
                    payload={},
                    timeout_seconds=request_timeout,
                )
                account_environment_restored = (
                    _snapshot_view(restored_status).get("account_id")
                    == original_account_id
                )
            except Exception as exc:  # pragma: no cover - external recovery failure
                errors.append(f"account_restore_failed: {type(exc).__name__}: {exc}")

            try:
                accounts_after = _request_json(
                    base_url,
                    "/api/v1/vnpy-paper/accounts?include_inactive=true&include_hidden=true",
                    timeout_seconds=request_timeout,
                )
                isolated_account_ids.update(
                    _account_ids(accounts_after) - baseline_account_ids
                )
                current_account_id = accounts_after.get("current_account_id")
                account_environment_restored = account_environment_restored or (
                    current_account_id == original_account_id
                )
                if args.keep_isolated_account_visible:
                    isolated_account_cleanup_ok = True
                elif isolated_account_ids:
                    cleanup = _request_json(
                        base_url,
                        "/api/v1/vnpy-paper/accounts/archived/cleanup",
                        method="POST",
                        payload={
                            "account_ids": sorted(isolated_account_ids),
                            "dry_run": False,
                            "include_hidden": True,
                        },
                        timeout_seconds=request_timeout,
                    )
                    cleaned = {
                        int(item)
                        for item in cleanup.get("cleaned_account_ids") or []
                        if str(item).isdigit()
                    }
                    isolated_account_cleanup_ok = isolated_account_ids <= cleaned
                else:
                    isolated_account_cleanup_ok = True
            except Exception as exc:  # pragma: no cover - external recovery failure
                isolated_account_cleanup_ok = False
                errors.append(f"account_cleanup_failed: {type(exc).__name__}: {exc}")
            account_environment_restored = (
                account_environment_restored and isolated_account_cleanup_ok
            )

        restore_payload: Dict[str, Any] = {}
        if changed_time_gate:
            restore_payload["auto_trade_time_gate_enabled"] = original_time_gate
        if changed_auto_interval:
            restore_payload["auto_interval_minutes"] = original_auto_interval
        if changed_execution_mode:
            restore_payload["auto_execution_mode"] = original_execution_mode
        if changed_auto_trade:
            if account_environment_restored or not original_auto_trade:
                restore_payload["auto_trade_enabled"] = original_auto_trade
            else:
                settings_restored = False
        if restore_payload:
            try:
                _request_json(
                    base_url,
                    "/api/v1/vnpy-paper/settings",
                    method="PUT",
                    payload=restore_payload,
                    timeout_seconds=request_timeout,
                )
            except Exception as exc:  # pragma: no cover - external recovery failure
                settings_restored = False
                errors.append(f"settings_restore_failed: {type(exc).__name__}: {exc}")
        try:
            final_status = _request_json(
                base_url,
                "/api/v1/vnpy-paper/status?include_snapshot=true&include_recent_trades=false",
                timeout_seconds=request_timeout,
            )
            restored_settings = final_status.get("settings")
            restored_settings = (
                restored_settings if isinstance(restored_settings, dict) else {}
            )
            if changed_time_gate:
                settings_restored = settings_restored and (
                    bool(restored_settings.get("auto_trade_time_gate_enabled"))
                    == original_time_gate
                )
            if changed_auto_trade:
                settings_restored = settings_restored and (
                    bool(restored_settings.get("auto_trade_enabled"))
                    == original_auto_trade
                )
            if changed_auto_interval:
                settings_restored = settings_restored and (
                    int(restored_settings.get("auto_interval_minutes") or 0)
                    == original_auto_interval
                )
            if changed_execution_mode:
                settings_restored = settings_restored and (
                    str(restored_settings.get("auto_execution_mode") or "")
                    == original_execution_mode
                )
            if args.isolated_account:
                account_environment_restored = account_environment_restored and (
                    _snapshot_view(final_status).get("account_id")
                    == original_account_id
                )
        except Exception as exc:  # pragma: no cover - external status failure
            errors.append(f"final_status_failed: {type(exc).__name__}: {exc}")

    if not after_status and not args.isolated_account:
        after_status = final_status

    evaluation = evaluate_acceptance(
        execution_mode=args.execution_mode,
        run_response=run_response,
        run_detail=run_detail,
        before_status=before_status,
        after_status=after_status,
        min_candidates=min_candidates,
        max_failed_plans=max_failed_plans,
        require_execution=args.execution_mode == "vnpy_paper" and not args.allow_no_fill,
        settings_restored=settings_restored,
        account_environment_restored=account_environment_restored,
    )
    error = "; ".join(errors) if errors else None
    if error is not None:
        evaluation["ok"] = False
        evaluation["failures"] = ["acceptance_runtime_error", *evaluation["failures"]]
    if args.trigger_mode == "scheduler" and not scheduler_event:
        evaluation["ok"] = False
        evaluation["failures"].append("scheduler_task_event_not_correlated")
    elif args.trigger_mode == "scheduler" and scheduler_event.get("status") != "completed":
        evaluation["ok"] = False
        evaluation["failures"].append("scheduler_task_not_completed")
    result = {
        "schema_version": 2,
        "ok": evaluation["ok"],
        "started_at": started_at,
        "finished_at": _utc_iso(),
        "base_url": base_url,
        "execution_mode": args.execution_mode,
        "trigger_mode": args.trigger_mode,
        "time_gate_temporarily_disabled": changed_time_gate,
        "isolated_account": {
            "enabled": bool(args.isolated_account),
            "original_account_id": original_account_id,
            "created_account_ids": sorted(isolated_account_ids),
            "restored": account_environment_restored,
            "cleanup_ok": isolated_account_cleanup_ok,
        },
        "error": error,
        "run": {
            "run_uid": run_response.get("agent_run_uid"),
            "accepted": run_response.get("accepted"),
            "candidate_count": run_detail.get("candidate_count"),
            "planned_count": run_detail.get("planned_count"),
            "submitted_count": run_detail.get("submitted_count"),
            "skipped_count": run_detail.get("skipped_count"),
            "status": run_detail.get("status"),
        },
        "scheduler_event": scheduler_event,
        "evaluation": evaluation,
    }
    output = json.dumps(result, ensure_ascii=False, indent=2)
    print(output)
    if args.output_json is not None:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(output + "\n", encoding="utf-8")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
