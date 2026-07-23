# -*- coding: utf-8 -*-
"""Observe the deployed vn.py API runtime without changing settings or placing orders."""

from __future__ import annotations

import argparse
import json
import os
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
BUILTIN_SIMULATED_GATEWAY_CLASS = (
    "src.services.vnpy_simulated_gateway:DsaSimulatedGateway"
)
BUILTIN_SIMULATED_GATEWAY_NAME = "DSA_SIM"
PUBLIC_ACCOUNT_FORBIDDEN_KEYS = frozenset({"account_id", "raw"})


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
    gateway_expectation_mismatch_count: int,
    external_gateway_required: bool,
    external_gateway_observation_count: int,
    incompatible_contract_count: int,
    reconnect_attempt_count: int,
    reconnect_success_count: int,
    reconnect_failure_count: int,
    reconnect_counter_regression_count: int,
    observed_event_counts: Dict[str, int],
    required_observed_events: Iterable[str],
    min_observed_event_count: int,
    event_observation_counter_regression_count: int,
    event_handler_failure_count: int,
    min_api_success_ratio: float,
    min_runtime_ready_ratio: float,
    min_connected_ratio: float,
    min_event_bridge_ratio: float,
    max_process_changes: int,
    max_gateway_changes: int,
    min_reconnect_success_count: int,
    max_reconnect_failure_count: int,
    max_event_handler_failures: int,
    expected_build_id: str = "",
    build_identity_observation_count: int = 0,
    build_expectation_mismatch_count: int = 0,
    public_account_redaction_required: bool = False,
    public_account_observation_count: int = 0,
    public_account_redaction_failure_count: int = 0,
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
    required_observed = sorted(
        {
            str(event_name).strip()
            for event_name in required_observed_events
            if str(event_name).strip()
        }
    )
    missing_observed_events = [
        event_name
        for event_name in required_observed
        if int(observed_event_counts.get(event_name) or 0) < min_observed_event_count
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
        if expected_build_id:
            if build_identity_observation_count < successful:
                failures.append("build_identity_missing")
            if build_expectation_mismatch_count > 0:
                failures.append("build_identity_mismatch")
        if gateway_identity_observation_count < successful:
            failures.append("gateway_identity_missing")
        if gateway_expectation_mismatch_count > 0:
            failures.append("gateway_identity_mismatch")
        if (
            external_gateway_required
            and external_gateway_observation_count < successful
        ):
            failures.append("external_gateway_not_confirmed")
        if incompatible_contract_count > 0:
            failures.append("contract_incompatible")
        if public_account_redaction_required:
            if public_account_observation_count <= 0:
                failures.append("public_account_redaction_unobserved")
            if public_account_redaction_failure_count > 0:
                failures.append("public_account_redaction_failed")
    if process_change_count > max_process_changes:
        failures.append("process_changes_above_threshold")
    if gateway_change_count > max_gateway_changes:
        failures.append("gateway_changes_above_threshold")
    if reconnect_counter_regression_count > 0:
        failures.append("reconnect_counters_regressed")
    if event_observation_counter_regression_count > 0:
        failures.append("event_observation_counters_regressed")
    if missing_observed_events:
        failures.append("required_event_observations_below_threshold")
    if event_handler_failure_count > max_event_handler_failures:
        failures.append("event_handler_failures_above_threshold")
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
        "expected_build_id": expected_build_id or None,
        "build_identity_observation_count": max(
            0, int(build_identity_observation_count or 0)
        ),
        "build_expectation_mismatch_count": max(
            0, int(build_expectation_mismatch_count or 0)
        ),
        "public_account_redaction_required": bool(
            public_account_redaction_required
        ),
        "public_account_observation_count": max(
            0, int(public_account_observation_count or 0)
        ),
        "public_account_redaction_failure_count": max(
            0, int(public_account_redaction_failure_count or 0)
        ),
        "public_account_forbidden_keys": sorted(PUBLIC_ACCOUNT_FORBIDDEN_KEYS),
        "process_change_count": max(0, int(process_change_count or 0)),
        "max_process_changes": max_process_changes,
        "gateway_identity_observation_count": gateway_identity_observation_count,
        "gateway_change_count": max(0, int(gateway_change_count or 0)),
        "max_gateway_changes": max_gateway_changes,
        "gateway_expectation_mismatch_count": max(
            0, int(gateway_expectation_mismatch_count or 0)
        ),
        "external_gateway_required": bool(external_gateway_required),
        "external_gateway_observation_count": max(
            0, int(external_gateway_observation_count or 0)
        ),
        "incompatible_contract_count": max(0, int(incompatible_contract_count or 0)),
        "reconnect_attempt_count": max(0, int(reconnect_attempt_count or 0)),
        "reconnect_success_count": max(0, int(reconnect_success_count or 0)),
        "min_reconnect_success_count": min_reconnect_success_count,
        "reconnect_failure_count": max(0, int(reconnect_failure_count or 0)),
        "max_reconnect_failure_count": max_reconnect_failure_count,
        "reconnect_counter_regression_count": max(
            0, int(reconnect_counter_regression_count or 0)
        ),
        "observed_event_counts": {
            event_name: max(0, int(observed_event_counts.get(event_name) or 0))
            for event_name in sorted(observed_event_counts)
        },
        "required_observed_events": required_observed,
        "min_observed_event_count": min_observed_event_count,
        "missing_observed_events": missing_observed_events,
        "event_observation_counter_regression_count": max(
            0, int(event_observation_counter_regression_count or 0)
        ),
        "event_handler_failure_count": max(0, int(event_handler_failure_count or 0)),
        "max_event_handler_failures": max_event_handler_failures,
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


def _event_observation_values(runtime: Dict[str, Any]) -> Dict[str, int]:
    bridge = runtime.get("event_bridge")
    bridge = bridge if isinstance(bridge, dict) else {}
    observations = bridge.get("observations")
    observations = observations if isinstance(observations, dict) else {}
    values = {
        event_name: max(
            0,
            int(
                observation.get("count") or 0
                if isinstance(observation, dict)
                else 0
            ),
        )
        for event_name, observation in observations.items()
        if str(event_name).strip() in EVENT_TYPES
    }
    values["handler_failure"] = max(
        0, int(bridge.get("handler_failure_count") or 0)
    )
    return values


def _is_builtin_simulated_gateway(gateway_class: str, gateway_name: str) -> bool:
    return (
        str(gateway_class or "").strip() == BUILTIN_SIMULATED_GATEWAY_CLASS
        or str(gateway_name or "").strip().upper() == BUILTIN_SIMULATED_GATEWAY_NAME
    )


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
    parser.add_argument("--expected-gateway-class")
    parser.add_argument("--expected-gateway-name")
    parser.add_argument(
        "--expected-build-id",
        help="Require every successful status sample to expose this exact build id.",
    )
    parser.add_argument(
        "--require-public-account-redaction",
        action="store_true",
        help="Require public vn.py account diagnostics and reject account_id/raw fields.",
    )
    parser.add_argument(
        "--require-external-gateway",
        action="store_true",
        help="Reject DSA's built-in simulated gateway as production evidence.",
    )
    parser.add_argument("--min-reconnect-success-count", type=int, default=0)
    parser.add_argument("--max-reconnect-failure-count", type=int, default=0)
    parser.add_argument(
        "--require-observed-event",
        action="append",
        choices=sorted(EVENT_TYPES),
        default=[],
        help="Require this callback to receive events during the observation window.",
    )
    parser.add_argument("--min-observed-event-count", type=int, default=1)
    parser.add_argument("--max-event-handler-failures", type=int, default=0)
    parser.add_argument(
        "--require-event",
        action="append",
        choices=sorted(EVENT_TYPES),
        default=[],
        help="Require a callback type throughout the soak; defaults to all four types.",
    )
    parser.add_argument(
        "--checkpoint-interval-seconds",
        type=float,
        default=60.0,
        help="Atomically refresh --output-json during the soak (0 disables it).",
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
    expected_gateway_class = str(args.expected_gateway_class or "").strip()
    expected_gateway_name = str(args.expected_gateway_name or "").strip()
    expected_build_id = str(args.expected_build_id or "").strip()
    if args.expected_build_id is not None and not expected_build_id:
        parser.error("--expected-build-id must not be empty")
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
    min_observed_event_count = _bounded_int(
        parser,
        "--min-observed-event-count",
        args.min_observed_event_count,
        minimum=1,
        maximum=1000000000,
    )
    max_event_handler_failures = _bounded_int(
        parser,
        "--max-event-handler-failures",
        args.max_event_handler_failures,
        minimum=0,
        maximum=1000000000,
    )
    checkpoint_interval = _bounded_float(
        parser,
        "--checkpoint-interval-seconds",
        args.checkpoint_interval_seconds,
        minimum=0.0,
        maximum=3600.0,
    )
    required_event_types = sorted(
        {EVENT_TYPES[name] for name in (args.require_event or sorted(EVENT_TYPES))}
    )
    required_observed_events = sorted(set(args.require_observed_event or []))

    started_at = _utc_iso()
    started_monotonic = time.monotonic()
    sample_count = 0
    successful_count = 0
    runtime_ready_count = 0
    connected_count = 0
    bridge_registered_count = 0
    event_type_counts: Counter[str] = Counter()
    backend_identity_count = 0
    build_identity_count = 0
    build_expectation_mismatch_count = 0
    public_account_observation_count = 0
    public_account_redaction_failure_count = 0
    process_change_count = 0
    gateway_identity_count = 0
    gateway_change_count = 0
    gateway_expectation_mismatch_count = 0
    external_gateway_observation_count = 0
    incompatible_contract_count = 0
    reconnect_counter_regression_count = 0
    reconnect_deltas: Counter[str] = Counter()
    event_observation_deltas: Counter[str] = Counter()
    event_observation_counter_regression_count = 0
    error_counts: Counter[str] = Counter()
    last_identity = None
    last_gateway_identity = None
    last_counters: Dict[str, int] | None = None
    last_event_observations: Dict[str, int] | None = None
    latest_backend: Dict[str, Any] = {}
    latest_gateway: Dict[str, Any] = {}
    interrupted = False
    next_checkpoint_at = started_monotonic + checkpoint_interval

    def build_result(phase: str, elapsed_seconds: float) -> Dict[str, Any]:
        evaluation = evaluate_deployed_runtime_soak(
            duration_completed=elapsed_seconds + 1e-6 >= duration,
            interrupted=phase == "interrupted",
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
            gateway_expectation_mismatch_count=gateway_expectation_mismatch_count,
            external_gateway_required=bool(args.require_external_gateway),
            external_gateway_observation_count=external_gateway_observation_count,
            incompatible_contract_count=incompatible_contract_count,
            reconnect_attempt_count=reconnect_deltas["attempt"],
            reconnect_success_count=reconnect_deltas["success"],
            reconnect_failure_count=reconnect_deltas["failure"],
            reconnect_counter_regression_count=reconnect_counter_regression_count,
            observed_event_counts={
                name: event_observation_deltas[name] for name in EVENT_TYPES
            },
            required_observed_events=required_observed_events,
            min_observed_event_count=min_observed_event_count,
            event_observation_counter_regression_count=(
                event_observation_counter_regression_count
            ),
            event_handler_failure_count=event_observation_deltas["handler_failure"],
            min_api_success_ratio=min_api_ratio,
            min_runtime_ready_ratio=min_runtime_ratio,
            min_connected_ratio=min_connected_ratio,
            min_event_bridge_ratio=min_bridge_ratio,
            max_process_changes=max_process_changes,
            max_gateway_changes=max_gateway_changes,
            min_reconnect_success_count=min_reconnect_successes,
            max_reconnect_failure_count=max_reconnect_failures,
            max_event_handler_failures=max_event_handler_failures,
            expected_build_id=expected_build_id,
            build_identity_observation_count=build_identity_count,
            build_expectation_mismatch_count=build_expectation_mismatch_count,
            public_account_redaction_required=bool(
                args.require_public_account_redaction
            ),
            public_account_observation_count=public_account_observation_count,
            public_account_redaction_failure_count=(
                public_account_redaction_failure_count
            ),
        )
        return {
            "schema_version": 2,
            "phase": phase,
            "checkpoint": phase == "running",
            "started_at": started_at,
            "finished_at": None if phase == "running" else _utc_iso(),
            "updated_at": _utc_iso(),
            "configured_duration_seconds": duration,
            "observed_duration_seconds": round(elapsed_seconds, 3),
            "sample_interval_seconds": interval,
            "base_url": base_url,
            "gateway": latest_gateway,
            "backend": latest_backend,
            "event_type_sample_counts": dict(sorted(event_type_counts.items())),
            "observed_event_deltas": {
                name: event_observation_deltas[name] for name in sorted(EVENT_TYPES)
            },
            "error_counts": dict(sorted(error_counts.items())),
            "evaluation": evaluation,
        }

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
                build_id = str(backend.get("build_id") or "").strip()
                if build_id:
                    build_identity_count += 1
                if expected_build_id and build_id != expected_build_id:
                    build_expectation_mismatch_count += 1
                sync_state = diagnostics.get("vnpy_sync_state")
                sync_state = sync_state if isinstance(sync_state, dict) else {}
                public_account = sync_state.get("account")
                if isinstance(public_account, dict):
                    public_account_observation_count += 1
                    if PUBLIC_ACCOUNT_FORBIDDEN_KEYS.intersection(public_account):
                        public_account_redaction_failure_count += 1
                gateway = runtime.get("gateway")
                gateway = gateway if isinstance(gateway, dict) else {}
                latest_gateway = {
                    "class": runtime.get("gateway_class"),
                    "name": runtime.get("gateway_name"),
                    "added": bool(gateway.get("added")),
                    "builtin_simulated": _is_builtin_simulated_gateway(
                        str(runtime.get("gateway_class") or ""),
                        str(runtime.get("gateway_name") or ""),
                    ),
                }
                gateway_identity = (
                    str(runtime.get("gateway_class") or "").strip(),
                    str(runtime.get("gateway_name") or "").strip(),
                )
                if all(gateway_identity):
                    gateway_identity_count += 1
                    if (
                        expected_gateway_class
                        and gateway_identity[0] != expected_gateway_class
                    ) or (
                        expected_gateway_name
                        and gateway_identity[1] != expected_gateway_name
                    ):
                        gateway_expectation_mismatch_count += 1
                    if not _is_builtin_simulated_gateway(*gateway_identity):
                        external_gateway_observation_count += 1
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
                current_event_observations = _event_observation_values(runtime)
                if last_counters is not None and identity == last_identity:
                    if any(
                        value < last_counters.get(name, 0)
                        for name, value in current_counters.items()
                    ):
                        reconnect_counter_regression_count += 1
                    for name, value in current_counters.items():
                        reconnect_deltas[name] += max(0, value - last_counters.get(name, 0))
                if last_event_observations is not None and identity == last_identity:
                    if any(
                        value < last_event_observations.get(name, 0)
                        for name, value in current_event_observations.items()
                    ):
                        event_observation_counter_regression_count += 1
                    for name, value in current_event_observations.items():
                        event_observation_deltas[name] += max(
                            0, value - last_event_observations.get(name, 0)
                        )
                last_identity = identity
                last_counters = current_counters
                last_event_observations = current_event_observations
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

            now = time.monotonic()
            if (
                args.output_json is not None
                and checkpoint_interval > 0
                and now >= next_checkpoint_at
            ):
                _write_json_file(
                    build_result("running", now - started_monotonic),
                    args.output_json,
                )
                while next_checkpoint_at <= now:
                    next_checkpoint_at += checkpoint_interval
            remaining = duration - (now - started_monotonic)
            if remaining > 0:
                time.sleep(min(interval, remaining))
    except KeyboardInterrupt:
        interrupted = True

    elapsed = time.monotonic() - started_monotonic
    result = build_result("interrupted" if interrupted else "completed", elapsed)
    encoded = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output_json is not None:
        _write_json_file(result, args.output_json)
    print(encoded)
    if interrupted:
        return 130
    return 0 if result["evaluation"]["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
