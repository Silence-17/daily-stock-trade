# -*- coding: utf-8 -*-
"""Observe the deployed vn.py API runtime without changing settings or placing orders."""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


EVENT_TYPES = {
    "order": "eOrder.",
    "trade": "eTrade.",
    "account": "eAccount.",
    "position": "ePosition.",
}


def _utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


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


def _validated_base_url(parser: argparse.ArgumentParser, value: str) -> str:
    candidate = str(value or "").strip().rstrip("/")
    parsed = urlsplit(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        parser.error("--base-url must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        parser.error("--base-url must not contain embedded credentials")
    return candidate


def _read_status(base_url: str, *, timeout_seconds: float) -> Dict[str, Any]:
    request = Request(
        f"{base_url}/api/v1/vnpy-paper/status"
        "?include_snapshot=false&include_recent_trades=false",
        headers={"Accept": "application/json"},
        method="GET",
    )
    with urlopen(request, timeout=timeout_seconds) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("API response must be a JSON object")
    return payload


def _ratio(count: int, total: int) -> float:
    return round(max(0, int(count or 0)) / total, 6) if total else 0.0


def evaluate_deployed_runtime_soak(
    *,
    duration_completed: bool,
    interrupted: bool,
    sample_count: int,
    successful_sample_count: int,
    runtime_ready_count: int,
    connected_count: int,
    event_bridge_registered_count: int,
    event_type_counts: Dict[str, int],
    required_event_types: Iterable[str],
    backend_identity_observation_count: int,
    process_change_count: int,
    gateway_identity_observation_count: int,
    gateway_change_count: int,
    incompatible_contract_count: int,
    reconnect_attempt_count: int,
    reconnect_success_count: int,
    reconnect_failure_count: int,
    reconnect_counter_regression_count: int,
    min_api_success_ratio: float,
    min_runtime_ready_ratio: float,
    min_connected_ratio: float,
    min_event_bridge_ratio: float,
    max_process_changes: int,
    max_gateway_changes: int,
    min_reconnect_success_count: int,
    max_reconnect_failure_count: int,
) -> Dict[str, Any]:
    samples = max(0, int(sample_count or 0))
    successful = max(0, int(successful_sample_count or 0))
    required = sorted(
        {str(event_type).strip() for event_type in required_event_types if str(event_type).strip()}
    )
    api_ratio = _ratio(successful, samples)
    runtime_ratio = _ratio(runtime_ready_count, successful)
    connected_ratio = _ratio(connected_count, successful)
    bridge_ratio = _ratio(event_bridge_registered_count, successful)
    event_ratios = {
        event_type: _ratio(event_type_counts.get(event_type, 0), successful)
        for event_type in required
    }
    missing_event_types = [
        event_type for event_type in required if int(event_type_counts.get(event_type) or 0) <= 0
    ]
    intermittent_event_types = [
        event_type
        for event_type in required
        if successful > 0
        and event_ratios[event_type] + 1e-12 < min_event_bridge_ratio
    ]

    failures = []
    if interrupted:
        failures.append("interrupted")
    elif not duration_completed:
        failures.append("duration_incomplete")
    if samples <= 0:
        failures.append("no_samples")
    elif successful <= 0:
        failures.append("api_never_reached")
    elif api_ratio + 1e-12 < min_api_success_ratio:
        failures.append("api_success_ratio_below_threshold")
    if successful > 0:
        if runtime_ready_count <= 0:
            failures.append("runtime_never_ready")
        elif runtime_ratio + 1e-12 < min_runtime_ready_ratio:
            failures.append("runtime_ready_ratio_below_threshold")
        if connected_count <= 0:
            failures.append("connection_never_confirmed")
        elif connected_ratio + 1e-12 < min_connected_ratio:
            failures.append("connected_ratio_below_threshold")
        if event_bridge_registered_count <= 0:
            failures.append("event_bridge_never_registered")
        elif bridge_ratio + 1e-12 < min_event_bridge_ratio:
            failures.append("event_bridge_ratio_below_threshold")
        if missing_event_types:
            failures.append("required_event_types_missing")
        elif intermittent_event_types:
            failures.append("required_event_type_ratio_below_threshold")
        if backend_identity_observation_count < successful:
            failures.append("backend_identity_missing")
        if gateway_identity_observation_count < successful:
            failures.append("gateway_identity_missing")
        if incompatible_contract_count > 0:
            failures.append("contract_incompatible")
    if process_change_count > max_process_changes:
        failures.append("process_changes_above_threshold")
    if gateway_change_count > max_gateway_changes:
        failures.append("gateway_changes_above_threshold")
    if reconnect_counter_regression_count > 0:
        failures.append("reconnect_counters_regressed")
    if reconnect_success_count < min_reconnect_success_count:
        failures.append("reconnect_successes_below_threshold")
    if reconnect_failure_count > max_reconnect_failure_count:
        failures.append("reconnect_failures_above_threshold")

    return {
        "ok": not failures,
        "failures": failures,
        "sample_count": samples,
        "successful_sample_count": successful,
        "api_success_ratio": api_ratio,
        "min_api_success_ratio": min_api_success_ratio,
        "runtime_ready_ratio": runtime_ratio,
        "min_runtime_ready_ratio": min_runtime_ready_ratio,
        "connected_ratio": connected_ratio,
        "min_connected_ratio": min_connected_ratio,
        "event_bridge_registered_ratio": bridge_ratio,
        "min_event_bridge_ratio": min_event_bridge_ratio,
        "required_event_types": required,
        "event_type_ratios": event_ratios,
        "missing_event_types": missing_event_types,
        "intermittent_event_types": intermittent_event_types,
        "backend_identity_observation_count": backend_identity_observation_count,
        "process_change_count": max(0, int(process_change_count or 0)),
        "max_process_changes": max_process_changes,
        "gateway_identity_observation_count": gateway_identity_observation_count,
        "gateway_change_count": max(0, int(gateway_change_count or 0)),
        "max_gateway_changes": max_gateway_changes,
        "incompatible_contract_count": max(0, int(incompatible_contract_count or 0)),
        "reconnect_attempt_count": max(0, int(reconnect_attempt_count or 0)),
        "reconnect_success_count": max(0, int(reconnect_success_count or 0)),
        "min_reconnect_success_count": min_reconnect_success_count,
        "reconnect_failure_count": max(0, int(reconnect_failure_count or 0)),
        "max_reconnect_failure_count": max_reconnect_failure_count,
        "reconnect_counter_regression_count": max(
            0, int(reconnect_counter_regression_count or 0)
        ),
    }


def _runtime_ready(runtime: Dict[str, Any]) -> bool:
    gateway = runtime.get("gateway")
    return bool(
        runtime.get("available")
        and runtime.get("mode") == "vnpy_runtime"
        and runtime.get("event_engine_created")
        and runtime.get("main_engine_created")
        and isinstance(gateway, dict)
        and gateway.get("added")
    )


def _counter_values(runtime: Dict[str, Any]) -> Dict[str, int]:
    reconnect = runtime.get("auto_reconnect")
    reconnect = reconnect if isinstance(reconnect, dict) else {}
    return {
        "attempt": max(0, int(reconnect.get("attempt_count") or 0)),
        "success": max(0, int(reconnect.get("success_count") or 0)),
        "failure": max(0, int(reconnect.get("failure_count") or 0)),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--duration-seconds", type=float, default=300.0)
    parser.add_argument("--sample-interval-seconds", type=float, default=5.0)
    parser.add_argument("--request-timeout-seconds", type=float, default=5.0)
    parser.add_argument("--min-api-success-ratio", type=float, default=0.99)
    parser.add_argument("--min-runtime-ready-ratio", type=float, default=0.99)
    parser.add_argument("--min-connected-ratio", type=float, default=0.99)
    parser.add_argument("--min-event-bridge-ratio", type=float, default=0.99)
    parser.add_argument("--min-contract-version", type=int, default=3)
    parser.add_argument("--max-process-changes", type=int, default=0)
    parser.add_argument("--max-gateway-changes", type=int, default=0)
    parser.add_argument("--min-reconnect-success-count", type=int, default=0)
    parser.add_argument("--max-reconnect-failure-count", type=int, default=0)
    parser.add_argument(
        "--require-event",
        action="append",
        choices=sorted(EVENT_TYPES),
        default=[],
        help="Require a callback type throughout the soak; defaults to all four types.",
    )
    parser.add_argument("--output-json", type=Path)
    args = parser.parse_args(argv)

    base_url = _validated_base_url(parser, args.base_url)
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
    timeout = _bounded_float(
        parser,
        "--request-timeout-seconds",
        args.request_timeout_seconds,
        minimum=0.1,
        maximum=60.0,
    )
    min_api_ratio = _bounded_float(
        parser,
        "--min-api-success-ratio",
        args.min_api_success_ratio,
        minimum=0.0,
        maximum=1.0,
    )
    min_runtime_ratio = _bounded_float(
        parser,
        "--min-runtime-ready-ratio",
        args.min_runtime_ready_ratio,
        minimum=0.0,
        maximum=1.0,
    )
    min_connected_ratio = _bounded_float(
        parser,
        "--min-connected-ratio",
        args.min_connected_ratio,
        minimum=0.0,
        maximum=1.0,
    )
    min_bridge_ratio = _bounded_float(
        parser,
        "--min-event-bridge-ratio",
        args.min_event_bridge_ratio,
        minimum=0.0,
        maximum=1.0,
    )
    min_contract = _bounded_int(
        parser,
        "--min-contract-version",
        args.min_contract_version,
        minimum=1,
        maximum=100000,
    )
    max_process_changes = _bounded_int(
        parser,
        "--max-process-changes",
        args.max_process_changes,
        minimum=0,
        maximum=100000,
    )
    max_gateway_changes = _bounded_int(
        parser,
        "--max-gateway-changes",
        args.max_gateway_changes,
        minimum=0,
        maximum=100000,
    )
    min_reconnect_successes = _bounded_int(
        parser,
        "--min-reconnect-success-count",
        args.min_reconnect_success_count,
        minimum=0,
        maximum=100000,
    )
    max_reconnect_failures = _bounded_int(
        parser,
        "--max-reconnect-failure-count",
        args.max_reconnect_failure_count,
        minimum=0,
        maximum=100000,
    )
    required_event_types = sorted(
        {EVENT_TYPES[name] for name in (args.require_event or sorted(EVENT_TYPES))}
    )

    started_at = _utc_iso()
    started_monotonic = time.monotonic()
    sample_count = 0
    successful_count = 0
    runtime_ready_count = 0
    connected_count = 0
    bridge_registered_count = 0
    event_type_counts: Counter[str] = Counter()
    backend_identity_count = 0
    process_change_count = 0
    gateway_identity_count = 0
    gateway_change_count = 0
    incompatible_contract_count = 0
    reconnect_counter_regression_count = 0
    reconnect_deltas: Counter[str] = Counter()
    error_counts: Counter[str] = Counter()
    last_identity = None
    last_gateway_identity = None
    last_counters: Dict[str, int] | None = None
    latest_backend: Dict[str, Any] = {}
    latest_gateway: Dict[str, Any] = {}
    interrupted = False

    try:
        while True:
            if time.monotonic() - started_monotonic >= duration:
                break
            sample_count += 1
            try:
                status = _read_status(base_url, timeout_seconds=timeout)
                successful_count += 1
                diagnostics = status.get("diagnostics")
                diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
                runtime = diagnostics.get("vnpy_runtime")
                runtime = runtime if isinstance(runtime, dict) else {}
                backend = diagnostics.get("backend")
                backend = backend if isinstance(backend, dict) else {}
                latest_backend = {
                    "api_version": backend.get("api_version"),
                    "contract_version": backend.get("vnpy_paper_contract_version"),
                    "build_id": backend.get("build_id"),
                    "python_version": backend.get("python_version"),
                    "process_started_at": backend.get("process_started_at"),
                }
                gateway = runtime.get("gateway")
                gateway = gateway if isinstance(gateway, dict) else {}
                latest_gateway = {
                    "class": runtime.get("gateway_class"),
                    "name": runtime.get("gateway_name"),
                    "added": bool(gateway.get("added")),
                }
                gateway_identity = (
                    str(runtime.get("gateway_class") or "").strip(),
                    str(runtime.get("gateway_name") or "").strip(),
                )
                if all(gateway_identity):
                    gateway_identity_count += 1
                    if (
                        last_gateway_identity is not None
                        and gateway_identity != last_gateway_identity
                    ):
                        gateway_change_count += 1
                    last_gateway_identity = gateway_identity
                if _runtime_ready(runtime):
                    runtime_ready_count += 1
                connect = runtime.get("connect")
                connect = connect if isinstance(connect, dict) else {}
                if connect.get("connected") is True and connect.get("status") == "connected":
                    connected_count += 1
                bridge = runtime.get("event_bridge")
                bridge = bridge if isinstance(bridge, dict) else {}
                if bridge.get("registered") is True:
                    bridge_registered_count += 1
                observed_types = {
                    str(event_type) for event_type in bridge.get("event_types") or []
                }
                for event_type in required_event_types:
                    if event_type in observed_types:
                        event_type_counts[event_type] += 1

                identity = str(backend.get("process_started_at") or "").strip()
                if identity:
                    backend_identity_count += 1
                if last_identity is not None and identity != last_identity:
                    process_change_count += 1
                current_counters = _counter_values(runtime)
                if last_counters is not None and identity == last_identity:
                    if any(
                        value < last_counters.get(name, 0)
                        for name, value in current_counters.items()
                    ):
                        reconnect_counter_regression_count += 1
                    for name, value in current_counters.items():
                        reconnect_deltas[name] += max(0, value - last_counters.get(name, 0))
                last_identity = identity
                last_counters = current_counters
                try:
                    contract_version = int(backend.get("vnpy_paper_contract_version") or 0)
                except (TypeError, ValueError):
                    contract_version = 0
                if contract_version < min_contract:
                    incompatible_contract_count += 1
            except HTTPError as exc:
                error_counts[f"http_{exc.code}"] += 1
            except (URLError, TimeoutError):
                error_counts["connection_error"] += 1
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError, TypeError):
                error_counts["invalid_response"] += 1

            remaining = duration - (time.monotonic() - started_monotonic)
            if remaining > 0:
                time.sleep(min(interval, remaining))
    except KeyboardInterrupt:
        interrupted = True

    elapsed = time.monotonic() - started_monotonic
    evaluation = evaluate_deployed_runtime_soak(
        duration_completed=elapsed + 1e-6 >= duration,
        interrupted=interrupted,
        sample_count=sample_count,
        successful_sample_count=successful_count,
        runtime_ready_count=runtime_ready_count,
        connected_count=connected_count,
        event_bridge_registered_count=bridge_registered_count,
        event_type_counts=dict(event_type_counts),
        required_event_types=required_event_types,
        backend_identity_observation_count=backend_identity_count,
        process_change_count=process_change_count,
        gateway_identity_observation_count=gateway_identity_count,
        gateway_change_count=gateway_change_count,
        incompatible_contract_count=incompatible_contract_count,
        reconnect_attempt_count=reconnect_deltas["attempt"],
        reconnect_success_count=reconnect_deltas["success"],
        reconnect_failure_count=reconnect_deltas["failure"],
        reconnect_counter_regression_count=reconnect_counter_regression_count,
        min_api_success_ratio=min_api_ratio,
        min_runtime_ready_ratio=min_runtime_ratio,
        min_connected_ratio=min_connected_ratio,
        min_event_bridge_ratio=min_bridge_ratio,
        max_process_changes=max_process_changes,
        max_gateway_changes=max_gateway_changes,
        min_reconnect_success_count=min_reconnect_successes,
        max_reconnect_failure_count=max_reconnect_failures,
    )
    result = {
        "schema_version": 1,
        "started_at": started_at,
        "finished_at": _utc_iso(),
        "configured_duration_seconds": duration,
        "observed_duration_seconds": round(elapsed, 3),
        "sample_interval_seconds": interval,
        "base_url": base_url,
        "gateway": latest_gateway,
        "backend": latest_backend,
        "event_type_sample_counts": dict(sorted(event_type_counts.items())),
        "error_counts": dict(sorted(error_counts.items())),
        "evaluation": evaluation,
    }
    encoded = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output_json is not None:
        output_path = args.output_json.expanduser()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    if interrupted:
        return 130
    return 0 if evaluation["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
