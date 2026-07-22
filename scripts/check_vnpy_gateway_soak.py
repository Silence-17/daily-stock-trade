# -*- coding: utf-8 -*-
"""Observe a configured vn.py gateway for a bounded period without placing orders."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import subprocess
import sys
import time
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Dict, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.services.vnpy_runtime import (  # noqa: E402
    bootstrap_vnpy_runtime,
    load_vnpy_runtime_settings,
)

EVENT_NAMES = ("order", "trade", "account", "position")
PREFLIGHT_SCRIPT = ROOT / "scripts" / "check_vnpy_gateway_preflight.py"


def _utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _connection_status(diagnostics: Dict[str, Any]) -> str:
    if not diagnostics.get("available"):
        return "unavailable"
    connect = diagnostics.get("connect")
    if not isinstance(connect, dict):
        return "not_requested"
    status = str(connect.get("status") or "").strip().lower()
    if status:
        return status
    if connect.get("connected") is True:
        return "connected"
    if connect.get("request_accepted") is True:
        return "connection_unconfirmed"
    return "unknown"


def evaluate_soak(
    *,
    runtime_available: bool,
    duration_completed: bool,
    interrupted: bool,
    sample_counts: Dict[str, int],
    event_counts: Dict[str, int],
    required_events: Iterable[str],
    min_connected_ratio: float,
    require_reconnect: bool = False,
    disconnect_injection_required: bool = False,
    disconnect_injected: bool = False,
    reconnect_attempt_count: int = 0,
    reconnect_success_count: int = 0,
    final_connection_status: str = "unknown",
) -> Dict[str, Any]:
    total_samples = sum(max(0, int(value or 0)) for value in sample_counts.values())
    connected_samples = max(0, int(sample_counts.get("connected") or 0))
    connected_ratio = connected_samples / total_samples if total_samples else 0.0
    required = sorted({str(item).strip().lower() for item in required_events if str(item).strip()})
    missing_events = [name for name in required if int(event_counts.get(name) or 0) <= 0]
    failures = []
    if not runtime_available:
        failures.append("runtime_unavailable")
    if interrupted:
        failures.append("interrupted")
    elif not duration_completed:
        failures.append("duration_incomplete")
    if total_samples <= 0:
        failures.append("no_connection_samples")
    elif connected_samples <= 0:
        failures.append("connection_never_confirmed")
    if total_samples > 0 and connected_ratio + 1e-12 < min_connected_ratio:
        failures.append("connected_ratio_below_threshold")
    if missing_events:
        failures.append("required_events_missing")
    if disconnect_injection_required and not disconnect_injected:
        failures.append("simulated_disconnect_not_injected")
    if require_reconnect:
        if reconnect_attempt_count <= 0:
            failures.append("reconnect_not_attempted")
        if reconnect_success_count <= 0:
            failures.append("reconnect_not_confirmed")
        if final_connection_status != "connected":
            failures.append("connection_not_restored")
    return {
        "ok": not failures,
        "failures": failures,
        "total_samples": total_samples,
        "connected_samples": connected_samples,
        "connected_ratio": round(connected_ratio, 6),
        "min_connected_ratio": min_connected_ratio,
        "required_events": required,
        "missing_events": missing_events,
        "require_reconnect": require_reconnect,
        "disconnect_injection_required": disconnect_injection_required,
        "disconnect_injected": disconnect_injected,
        "reconnect_attempt_count": max(0, int(reconnect_attempt_count or 0)),
        "reconnect_success_count": max(0, int(reconnect_success_count or 0)),
        "final_connection_status": final_connection_status,
    }


def _register_event_counters(event_engine: Any) -> tuple[Counter[str], list[tuple[str, Any]]]:
    module = importlib.import_module("vnpy.trader.event")
    event_types = {
        "order": getattr(module, "EVENT_ORDER", None),
        "trade": getattr(module, "EVENT_TRADE", None),
        "account": getattr(module, "EVENT_ACCOUNT", None),
        "position": getattr(module, "EVENT_POSITION", None),
    }
    counters: Counter[str] = Counter()
    lock = Lock()
    registrations = []
    for name, event_type in event_types.items():
        if not event_type:
            continue

        def handler(_event: Any, *, event_name: str = name) -> None:
            with lock:
                counters[event_name] += 1

        event_engine.register(event_type, handler)
        registrations.append((event_type, handler))
    return counters, registrations


def _unregister_event_counters(event_engine: Any, registrations: list[tuple[str, Any]]) -> None:
    unregister = getattr(event_engine, "unregister", None)
    if not callable(unregister):
        return
    for event_type, handler in registrations:
        unregister(event_type, handler)


def _safe_runtime_summary(diagnostics: Dict[str, Any]) -> Dict[str, Any]:
    reconnect = diagnostics.get("auto_reconnect")
    reconnect = reconnect if isinstance(reconnect, dict) else {}
    connect = diagnostics.get("connect")
    connect = connect if isinstance(connect, dict) else {}
    return {
        "available": bool(diagnostics.get("available")),
        "mode": diagnostics.get("mode"),
        "reason": diagnostics.get("reason"),
        "gateway_added": bool(
            isinstance(diagnostics.get("gateway"), dict)
            and diagnostics["gateway"].get("added")
        ),
        "connection_status": _connection_status(diagnostics),
        "confirmation_source": connect.get("confirmation_source"),
        "settings_source": connect.get("settings_source"),
        "auto_reconnect": {
            "enabled": bool(reconnect.get("enabled")),
            "running": bool(reconnect.get("running")),
            "attempt_count": int(reconnect.get("attempt_count") or 0),
            "success_count": int(reconnect.get("success_count") or 0),
            "failure_count": int(reconnect.get("failure_count") or 0),
            "consecutive_failure_count": int(
                reconnect.get("consecutive_failure_count") or 0
            ),
            "last_result": reconnect.get("last_result"),
            "last_reason": reconnect.get("last_reason"),
        },
    }


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


def _run_gateway_preflight(
    *,
    require_external_gateway: bool,
    require_all_default_keys: bool,
    timeout_seconds: float,
) -> Dict[str, Any]:
    command = [sys.executable, str(PREFLIGHT_SCRIPT)]
    if require_external_gateway:
        command.append("--require-external-gateway")
    if require_all_default_keys:
        command.append("--require-all-default-keys")
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
        )
    except Exception as exc:  # noqa: BLE001 - report only the error type.
        return {
            "ok": False,
            "error_type": type(exc).__name__,
            "failures": ["preflight_process_failed"],
        }
    try:
        payload = json.loads(completed.stdout)
    except (TypeError, json.JSONDecodeError):
        return {
            "ok": False,
            "error_type": "PreflightOutputInvalid",
            "exit_code": int(completed.returncode),
            "failures": ["preflight_output_invalid"],
        }
    if not isinstance(payload, dict):
        return {
            "ok": False,
            "error_type": "PreflightOutputMustBeObject",
            "exit_code": int(completed.returncode),
            "failures": ["preflight_output_invalid"],
        }
    evaluation = payload.get("evaluation")
    evaluation = evaluation if isinstance(evaluation, dict) else {}
    payload["exit_code"] = int(completed.returncode)
    payload["ok"] = bool(payload.get("ok")) and completed.returncode == 0
    if not payload["ok"] and not isinstance(evaluation.get("failures"), list):
        payload["failures"] = ["preflight_failed"]
    return payload


def _write_json_file(result: Dict[str, Any], output_path: Path) -> None:
    output = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    target = output_path.expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(output + "\n", encoding="utf-8")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def _write_result(result: Dict[str, Any], output_path: Path | None) -> None:
    output = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    if output_path is not None:
        _write_json_file(result, output_path)
    print(output)


def _build_soak_result(
    *,
    phase: str,
    started_at: str,
    settings: Any,
    preflight: Dict[str, Any],
    requested_duration: float,
    observed_duration: float,
    sample_interval: float,
    startup_grace: float,
    sample_counts: Counter[str],
    transitions: list[Dict[str, Any]],
    event_counts: Counter[str],
    disconnect_injected: bool,
    runtime_summary: Dict[str, Any],
    required_events: Iterable[str],
    min_connected_ratio: float,
    require_reconnect: bool,
    disconnect_injection_required: bool,
    interrupted: bool,
) -> Dict[str, Any]:
    reconnect_summary = runtime_summary["auto_reconnect"]
    duration_completed = observed_duration + 0.05 >= requested_duration
    evaluation = evaluate_soak(
        runtime_available=bool(runtime_summary.get("available")),
        duration_completed=duration_completed,
        interrupted=interrupted,
        sample_counts=dict(sample_counts),
        event_counts=dict(event_counts),
        required_events=required_events,
        min_connected_ratio=min_connected_ratio,
        require_reconnect=require_reconnect,
        disconnect_injection_required=disconnect_injection_required,
        disconnect_injected=disconnect_injected,
        reconnect_attempt_count=int(reconnect_summary["attempt_count"]),
        reconnect_success_count=int(reconnect_summary["success_count"]),
        final_connection_status=str(runtime_summary["connection_status"]),
    )
    return {
        "schema_version": 3,
        "phase": phase,
        "checkpoint": phase == "running",
        "ok": evaluation["ok"],
        "started_at": started_at,
        "ended_at": None if phase == "running" else _utc_iso(),
        "updated_at": _utc_iso(),
        "gateway": {
            "class": settings.gateway_class,
            "name": settings.gateway_name,
        },
        "preflight": preflight,
        "connection_attempted": True,
        "requested_duration_seconds": requested_duration,
        "observed_duration_seconds": round(observed_duration, 3),
        "sample_interval_seconds": sample_interval,
        "startup_grace_seconds": startup_grace,
        "sample_counts": dict(sorted(sample_counts.items())),
        "transitions": transitions[:200],
        "event_counts": {
            name: int(event_counts.get(name) or 0) for name in EVENT_NAMES
        },
        "disconnect_injected": disconnect_injected,
        "runtime": runtime_summary,
        "evaluation": evaluation,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration-seconds", type=float, default=300.0)
    parser.add_argument("--sample-interval-seconds", type=float, default=5.0)
    parser.add_argument("--startup-grace-seconds", type=float, default=30.0)
    parser.add_argument("--min-connected-ratio", type=float, default=0.99)
    parser.add_argument(
        "--require-event",
        action="append",
        default=[],
        choices=EVENT_NAMES,
        help="Require at least one observed event of this type; may be repeated.",
    )
    parser.add_argument(
        "--simulated-disconnect-at-seconds",
        type=float,
        default=0.0,
        help="Explicit DsaSimulatedGateway-only disconnect injection (0 disables it).",
    )
    parser.add_argument(
        "--require-reconnect",
        action="store_true",
        help="Require an observed reconnect attempt, success, and connected final state.",
    )
    parser.add_argument(
        "--require-external-gateway",
        action="store_true",
        help="Reject the built-in DSA_SIM gateway before any connection is attempted.",
    )
    parser.add_argument(
        "--require-all-default-keys",
        action="store_true",
        help="Require every gateway default_setting key in the external settings JSON.",
    )
    parser.add_argument("--preflight-timeout-seconds", type=float, default=120.0)
    parser.add_argument(
        "--checkpoint-interval-seconds",
        type=float,
        default=60.0,
        help="Atomically refresh --output-json during the soak (0 disables it).",
    )
    parser.add_argument("--output-json", type=Path)
    args = parser.parse_args(argv)
    duration = _bounded_float(
        parser, "--duration-seconds", args.duration_seconds, minimum=1.0, maximum=86400.0
    )
    interval = _bounded_float(
        parser,
        "--sample-interval-seconds",
        args.sample_interval_seconds,
        minimum=0.1,
        maximum=60.0,
    )
    startup_grace = _bounded_float(
        parser,
        "--startup-grace-seconds",
        args.startup_grace_seconds,
        minimum=0.0,
        maximum=600.0,
    )
    min_ratio = _bounded_float(
        parser, "--min-connected-ratio", args.min_connected_ratio, minimum=0.0, maximum=1.0
    )
    disconnect_at = _bounded_float(
        parser,
        "--simulated-disconnect-at-seconds",
        args.simulated_disconnect_at_seconds,
        minimum=0.0,
        maximum=86400.0,
    )
    preflight_timeout = _bounded_float(
        parser,
        "--preflight-timeout-seconds",
        args.preflight_timeout_seconds,
        minimum=5.0,
        maximum=600.0,
    )
    checkpoint_interval = _bounded_float(
        parser,
        "--checkpoint-interval-seconds",
        args.checkpoint_interval_seconds,
        minimum=0.0,
        maximum=3600.0,
    )

    loaded_settings = load_vnpy_runtime_settings()
    settings = replace(loaded_settings, auto_attach_events=False)
    if disconnect_at > 0 and not str(settings.gateway_class or "").endswith(
        ":DsaSimulatedGateway"
    ):
        parser.error(
            "--simulated-disconnect-at-seconds is only valid for DsaSimulatedGateway"
        )

    started_at = _utc_iso()
    preflight = _run_gateway_preflight(
        require_external_gateway=bool(args.require_external_gateway),
        require_all_default_keys=bool(args.require_all_default_keys),
        timeout_seconds=preflight_timeout,
    )
    if not preflight.get("ok"):
        preflight_evaluation = preflight.get("evaluation")
        if not isinstance(preflight_evaluation, dict):
            preflight_evaluation = {}
        result = {
            "schema_version": 3,
            "phase": "preflight_failed",
            "checkpoint": False,
            "ok": False,
            "started_at": started_at,
            "ended_at": _utc_iso(),
            "gateway": {
                "class": settings.gateway_class,
                "name": settings.gateway_name,
            },
            "preflight": preflight,
            "connection_attempted": False,
            "evaluation": {
                "ok": False,
                "failures": ["preflight_failed"],
                "preflight_failures": list(
                    preflight_evaluation.get("failures")
                    or preflight.get("failures")
                    or []
                ),
            },
        }
        _write_result(result, args.output_json)
        return 1

    started_monotonic = time.monotonic()
    measurement_started = started_monotonic
    measurement_ended = started_monotonic
    sample_counts: Counter[str] = Counter()
    transitions = []
    event_counts: Counter[str] = Counter()
    registrations: list[tuple[str, Any]] = []
    runtime_handle = bootstrap_vnpy_runtime(settings=settings)
    interrupted = False
    disconnect_injected = False
    last_status = None
    try:
        runtime_ready = bool(
            runtime_handle.diagnostics.get("available")
            and runtime_handle.main_engine is not None
            and runtime_handle.event_engine is not None
        )
        if runtime_ready:
            event_counts, registrations = _register_event_counters(
                runtime_handle.event_engine
            )

        if runtime_ready:
            grace_deadline = time.monotonic() + startup_grace
            while time.monotonic() < grace_deadline:
                diagnostics = runtime_handle.refresh_diagnostics()
                if _connection_status(diagnostics) == "connected":
                    break
                time.sleep(min(0.2, max(0.01, grace_deadline - time.monotonic())))

            measurement_started = time.monotonic()
            measurement_deadline = measurement_started + duration
            next_sample_at = measurement_started
            next_checkpoint_at = measurement_started + checkpoint_interval
            while True:
                now = time.monotonic()
                if now >= measurement_deadline:
                    break
                if (
                    disconnect_at > 0
                    and not disconnect_injected
                    and now - measurement_started >= disconnect_at
                ):
                    gateway = runtime_handle.main_engine.get_gateway(settings.gateway_name)
                    gateway.close()
                    disconnect_injected = True
                diagnostics = runtime_handle.refresh_diagnostics()
                status = _connection_status(diagnostics)
                sample_counts[status] += 1
                if status != last_status:
                    transitions.append(
                        {
                            "elapsed_seconds": round(now - measurement_started, 3),
                            "from": last_status,
                            "to": status,
                        }
                    )
                    last_status = status
                if (
                    args.output_json is not None
                    and checkpoint_interval > 0
                    and now >= next_checkpoint_at
                ):
                    checkpoint_runtime = _safe_runtime_summary(diagnostics)
                    checkpoint = _build_soak_result(
                        phase="running",
                        started_at=started_at,
                        settings=settings,
                        preflight=preflight,
                        requested_duration=duration,
                        observed_duration=max(0.0, now - measurement_started),
                        sample_interval=interval,
                        startup_grace=startup_grace,
                        sample_counts=sample_counts,
                        transitions=transitions,
                        event_counts=event_counts,
                        disconnect_injected=disconnect_injected,
                        runtime_summary=checkpoint_runtime,
                        required_events=args.require_event,
                        min_connected_ratio=min_ratio,
                        require_reconnect=bool(args.require_reconnect or disconnect_at > 0),
                        disconnect_injection_required=disconnect_at > 0,
                        interrupted=False,
                    )
                    _write_json_file(checkpoint, args.output_json)
                    while next_checkpoint_at <= now:
                        next_checkpoint_at += checkpoint_interval
                next_sample_at += interval
                time.sleep(
                    max(
                        0.0,
                        min(next_sample_at, measurement_deadline) - time.monotonic(),
                    )
                )
    except KeyboardInterrupt:
        interrupted = True
    finally:
        measurement_ended = time.monotonic()
        if runtime_handle.event_engine is not None:
            _unregister_event_counters(runtime_handle.event_engine, registrations)
        final_diagnostics = runtime_handle.refresh_diagnostics()
        runtime_summary = _safe_runtime_summary(final_diagnostics)
        runtime_handle.close()

    observed_duration = max(0.0, measurement_ended - measurement_started)
    result = _build_soak_result(
        phase="interrupted" if interrupted else "completed",
        started_at=started_at,
        settings=settings,
        preflight=preflight,
        requested_duration=duration,
        observed_duration=observed_duration,
        sample_interval=interval,
        startup_grace=startup_grace,
        sample_counts=sample_counts,
        transitions=transitions,
        event_counts=event_counts,
        disconnect_injected=disconnect_injected,
        runtime_summary=runtime_summary,
        required_events=args.require_event,
        min_connected_ratio=min_ratio,
        require_reconnect=bool(args.require_reconnect or disconnect_at > 0),
        disconnect_injection_required=disconnect_at > 0,
        interrupted=interrupted,
    )
    _write_result(result, args.output_json)
    if interrupted:
        return 130
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
