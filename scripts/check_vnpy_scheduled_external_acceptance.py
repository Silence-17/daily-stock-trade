# -*- coding: utf-8 -*-
"""Observe one scheduled Agent run through an external vn.py gateway.

This verifier is deliberately read-only. It records the current paper account and
Agent-run baseline, waits for a new ``vnpy_paper_auto`` run, correlates the
scheduler event, and verifies that a gateway fill reached the isolated ledger.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

if __package__:
    from scripts.check_online_agent_vnpy_e2e import (
        _agent_run_uids,
        _matching_scheduler_event,
        _run_is_terminal_and_coherent,
        _runtime_view,
        evaluate_acceptance,
    )
else:
    from check_online_agent_vnpy_e2e import (
        _agent_run_uids,
        _matching_scheduler_event,
        _run_is_terminal_and_coherent,
        _runtime_view,
        evaluate_acceptance,
    )


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
    timeout_seconds: float,
) -> Dict[str, Any]:
    request = Request(
        f"{base_url.rstrip('/')}{path}",
        headers={"Accept": "application/json"},
        method="GET",
    )
    with urlopen(request, timeout=timeout_seconds) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not isinstance(result, dict):
        raise ValueError(f"GET {path} must return a JSON object")
    return result


def _write_json_file(result: Dict[str, Any], output_path: Path) -> None:
    target = output_path.expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    encoded = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    try:
        temporary.write_text(encoded + "\n", encoding="utf-8")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


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


def _settings_view(status: Dict[str, Any]) -> Dict[str, Any]:
    settings = status.get("settings")
    settings = settings if isinstance(settings, dict) else {}
    return {
        "enabled": settings.get("enabled") is True,
        "auto_trade_enabled": settings.get("auto_trade_enabled") is True,
        "auto_execution_mode": str(settings.get("auto_execution_mode") or ""),
        "auto_trade_time_gate_enabled": (
            settings.get("auto_trade_time_gate_enabled") is True
        ),
        "auto_market": str(settings.get("auto_market") or ""),
        "auto_strategy": str(settings.get("auto_strategy") or ""),
        "account_id": settings.get("account_id"),
        "gateway_name": str(settings.get("vnpy_gateway_name") or ""),
    }


def _production_preflight(status: Dict[str, Any]) -> Dict[str, Any]:
    diagnostics = status.get("diagnostics")
    diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
    runtime = diagnostics.get("vnpy_runtime")
    runtime = runtime if isinstance(runtime, dict) else {}
    preflight = runtime.get("production_preflight")
    return preflight if isinstance(preflight, dict) else {}


def _event_handler_failures(status: Dict[str, Any]) -> int:
    diagnostics = status.get("diagnostics")
    diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
    runtime = diagnostics.get("vnpy_runtime")
    runtime = runtime if isinstance(runtime, dict) else {}
    bridge = runtime.get("event_bridge")
    bridge = bridge if isinstance(bridge, dict) else {}
    try:
        return max(0, int(bridge.get("handler_failure_count") or 0))
    except (TypeError, ValueError):
        return 0


def _registered_auto_trade_task(status: Dict[str, Any]) -> Dict[str, Any] | None:
    scheduler = status.get("scheduler")
    scheduler = scheduler if isinstance(scheduler, dict) else {}
    for task in scheduler.get("background_tasks") or []:
        if isinstance(task, dict) and task.get("name") == "vnpy_paper_auto_trade":
            return task
    return None


def evaluate_external_scheduled_acceptance(
    *,
    run_uid: str,
    run_detail: Dict[str, Any],
    scheduler_event: Dict[str, Any],
    before_status: Dict[str, Any],
    after_status: Dict[str, Any],
    min_candidates: int,
    max_failed_plans: int,
) -> Dict[str, Any]:
    base = evaluate_acceptance(
        execution_mode="vnpy_paper",
        run_response={"accepted": bool(run_uid), "agent_run_uid": run_uid},
        run_detail=run_detail,
        before_status=before_status,
        after_status=after_status,
        min_candidates=min_candidates,
        max_failed_plans=max_failed_plans,
        require_execution=True,
        settings_restored=True,
    )
    failures = list(base.get("failures") or [])
    before_settings = _settings_view(before_status)
    after_settings = _settings_view(after_status)
    runtime = _runtime_view(after_status)
    preflight = _production_preflight(after_status)
    task = _registered_auto_trade_task(after_status)
    event_details = (
        scheduler_event.get("details")
        if isinstance(scheduler_event.get("details"), dict)
        else {}
    )
    run_trigger = str(run_detail.get("trigger_source") or "")

    if not before_settings["enabled"] or not after_settings["enabled"]:
        failures.append("paper_trading_disabled")
    if not before_settings["auto_trade_enabled"] or not after_settings["auto_trade_enabled"]:
        failures.append("auto_trade_disabled")
    if (
        before_settings["auto_execution_mode"] != "vnpy_paper"
        or after_settings["auto_execution_mode"] != "vnpy_paper"
    ):
        failures.append("external_execution_mode_not_enabled")
    if (
        not before_settings["auto_trade_time_gate_enabled"]
        or not after_settings["auto_trade_time_gate_enabled"]
    ):
        failures.append("trading_time_gate_disabled")
    if before_settings != after_settings:
        failures.append("paper_settings_changed_during_observation")
    if task is None:
        failures.append("auto_trade_task_not_registered")
    if run_trigger != "vnpy_paper_auto":
        failures.append("unexpected_agent_run_trigger")
    if scheduler_event.get("status") != "completed":
        failures.append("scheduler_task_not_completed")
    if str(event_details.get("agent_run_uid") or "") != run_uid:
        failures.append("scheduler_task_event_not_correlated")
    gateway_class = str(runtime.get("gateway_class") or "")
    if not gateway_class or gateway_class.endswith(":DsaSimulatedGateway"):
        failures.append("external_gateway_not_observed")
    if preflight.get("ok") is not True or preflight.get("external_gateway") is not True:
        failures.append("external_gateway_preflight_not_ready")
    before_handler_failures = _event_handler_failures(before_status)
    after_handler_failures = _event_handler_failures(after_status)
    if after_handler_failures > before_handler_failures:
        failures.append("event_handler_failure_observed")

    failures = list(dict.fromkeys(failures))
    return {
        **base,
        "ok": not failures,
        "failures": failures,
        "scheduler_event_status": scheduler_event.get("status"),
        "scheduler_event_correlated": (
            str(event_details.get("agent_run_uid") or "") == run_uid
        ),
        "run_trigger_source": run_trigger or None,
        "external_gateway_class": gateway_class or None,
        "external_gateway_preflight_ok": preflight.get("ok") is True,
        "event_handler_failure_delta": (
            after_handler_failures - before_handler_failures
        ),
        "settings_unchanged": before_settings == after_settings,
        "auto_trade_task_registered": task is not None,
    }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout-seconds", type=float, default=1800.0)
    parser.add_argument("--poll-interval-seconds", type=float, default=5.0)
    parser.add_argument("--request-timeout-seconds", type=float, default=30.0)
    parser.add_argument("--checkpoint-interval-seconds", type=float, default=60.0)
    parser.add_argument("--min-candidates", type=int, default=1)
    parser.add_argument("--max-failed-plans", type=int, default=0)
    parser.add_argument("--output-json", type=Path)
    args = parser.parse_args(list(argv) if argv is not None else None)

    base_url = _validated_base_url(parser, args.base_url)
    timeout = _bounded_float(
        parser, "--timeout-seconds", args.timeout_seconds, minimum=1.0, maximum=86400.0
    )
    poll_interval = _bounded_float(
        parser,
        "--poll-interval-seconds",
        args.poll_interval_seconds,
        minimum=0.1,
        maximum=60.0,
    )
    request_timeout = _bounded_float(
        parser,
        "--request-timeout-seconds",
        args.request_timeout_seconds,
        minimum=0.1,
        maximum=600.0,
    )
    checkpoint_interval = _bounded_float(
        parser,
        "--checkpoint-interval-seconds",
        args.checkpoint_interval_seconds,
        minimum=0.0,
        maximum=3600.0,
    )
    min_candidates = _bounded_int(
        parser, "--min-candidates", args.min_candidates, minimum=0, maximum=50
    )
    max_failed_plans = _bounded_int(
        parser,
        "--max-failed-plans",
        args.max_failed_plans,
        minimum=0,
        maximum=50,
    )

    started_at = _utc_iso()
    started_monotonic = time.monotonic()
    deadline = started_monotonic + timeout
    next_checkpoint_at = started_monotonic + checkpoint_interval
    before_status: Dict[str, Any] = {}
    after_status: Dict[str, Any] = {}
    run_detail: Dict[str, Any] = {}
    scheduler_event: Dict[str, Any] = {}
    run_uid = ""
    baseline_run_uids: set[str] = set()
    sample_count = 0
    successful_sample_count = 0
    error_counts: Dict[str, int] = {}
    runtime_errors: list[str] = []

    def add_error(key: str) -> None:
        error_counts[key] = int(error_counts.get(key) or 0) + 1

    def checkpoint(phase: str) -> Dict[str, Any]:
        return {
            "schema_version": 1,
            "phase": phase,
            "checkpoint": phase not in {"completed", "failed", "timed_out"},
            "read_only": True,
            "places_orders": False,
            "started_at": started_at,
            "updated_at": _utc_iso(),
            "finished_at": (
                _utc_iso() if phase in {"completed", "failed", "timed_out"} else None
            ),
            "base_url": base_url,
            "duration_seconds": round(time.monotonic() - started_monotonic, 3),
            "configured_timeout_seconds": timeout,
            "sample_count": sample_count,
            "successful_sample_count": successful_sample_count,
            "baseline_run_count": len(baseline_run_uids),
            "run_uid": run_uid or None,
            "run_status": run_detail.get("status"),
            "scheduler_event_status": scheduler_event.get("status"),
            "error_counts": dict(error_counts),
            "runtime_errors": list(runtime_errors),
        }

    try:
        before_status = _request_json(
            base_url,
            "/api/v1/vnpy-paper/status?include_snapshot=true&include_recent_trades=false",
            timeout_seconds=request_timeout,
        )
        baseline_runs = _request_json(
            base_url,
            "/api/v1/vnpy-paper/agent-runs?limit=100&trigger_source=vnpy_paper_auto",
            timeout_seconds=request_timeout,
        )
        baseline_run_uids = _agent_run_uids(baseline_runs)
        while time.monotonic() < deadline:
            sample_count += 1
            try:
                if not run_uid:
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
                if run_uid:
                    run_detail = _request_json(
                        base_url,
                        f"/api/v1/vnpy-paper/agent-runs/{run_uid}",
                        timeout_seconds=request_timeout,
                    )
                    events = _request_json(
                        base_url,
                        "/api/v1/vnpy-paper/task-events?"
                        + urlencode({"name": "vnpy_paper_auto_trade", "limit": 100}),
                        timeout_seconds=request_timeout,
                    )
                    scheduler_event = _matching_scheduler_event(events, run_uid) or {}
                successful_sample_count += 1
                if (
                    run_uid
                    and scheduler_event
                    and _run_is_terminal_and_coherent("vnpy_paper", run_detail)
                ):
                    break
            except HTTPError as exc:
                add_error(f"http_{exc.code}")
            except (URLError, TimeoutError):
                add_error("connection_error")
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError, TypeError):
                add_error("invalid_response")

            now = time.monotonic()
            if (
                args.output_json is not None
                and checkpoint_interval > 0
                and now >= next_checkpoint_at
            ):
                _write_json_file(
                    checkpoint("waiting_for_terminal" if run_uid else "waiting_for_run"),
                    args.output_json,
                )
                while next_checkpoint_at <= now:
                    next_checkpoint_at += checkpoint_interval
            time.sleep(min(poll_interval, max(0.0, deadline - now)))
    except Exception as exc:  # noqa: BLE001 - report initialization failures.
        runtime_errors.append(f"{type(exc).__name__}: {exc}")

    try:
        after_status = _request_json(
            base_url,
            "/api/v1/vnpy-paper/status?include_snapshot=true&include_recent_trades=false",
            timeout_seconds=request_timeout,
        )
    except Exception as exc:  # noqa: BLE001 - preserve the primary observation.
        runtime_errors.append(f"final_status_failed: {type(exc).__name__}: {exc}")

    terminal_observed = bool(
        run_uid
        and scheduler_event
        and _run_is_terminal_and_coherent("vnpy_paper", run_detail)
    )
    evaluation = evaluate_external_scheduled_acceptance(
        run_uid=run_uid,
        run_detail=run_detail,
        scheduler_event=scheduler_event,
        before_status=before_status,
        after_status=after_status,
        min_candidates=min_candidates,
        max_failed_plans=max_failed_plans,
    )
    if not run_uid:
        evaluation["failures"] = ["scheduled_agent_run_not_observed", *evaluation["failures"]]
    elif not terminal_observed:
        evaluation["failures"] = ["scheduled_agent_run_not_terminal", *evaluation["failures"]]
    if runtime_errors:
        evaluation["failures"] = ["acceptance_runtime_error", *evaluation["failures"]]
    evaluation["failures"] = list(dict.fromkeys(evaluation["failures"]))
    evaluation["ok"] = not evaluation["failures"]
    phase = "completed" if evaluation["ok"] else (
        "timed_out" if time.monotonic() >= deadline else "failed"
    )
    result = {
        **checkpoint(phase),
        "evaluation": evaluation,
        "run": {
            "run_uid": run_uid or None,
            "status": run_detail.get("status"),
            "trigger_source": run_detail.get("trigger_source"),
            "candidate_count": run_detail.get("candidate_count"),
            "planned_count": run_detail.get("planned_count"),
            "submitted_count": run_detail.get("submitted_count"),
            "skipped_count": run_detail.get("skipped_count"),
        },
        "scheduler_event": scheduler_event,
    }
    encoded = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    print(encoded)
    if args.output_json is not None:
        _write_json_file(result, args.output_json)
    return 0 if evaluation["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
