# -*- coding: utf-8 -*-
"""Observe vn.py paper scheduler health through read-only API calls."""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen


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


def _read_json(base_url: str, path: str, *, timeout_seconds: float) -> Dict[str, Any]:
    request = Request(
        f"{base_url.rstrip('/')}{path}",
        headers={"Accept": "application/json"},
        method="GET",
    )
    with urlopen(request, timeout=timeout_seconds) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("API response must be a JSON object")
    return payload


def _validated_base_url(parser: argparse.ArgumentParser, value: str) -> str:
    candidate = str(value or "").strip().rstrip("/")
    parsed = urlsplit(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        parser.error("--base-url must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        parser.error("--base-url must not contain embedded credentials")
    return candidate


def _event_key(event: Dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(event.get("timestamp") or ""),
        str(event.get("name") or ""),
        str(event.get("status") or ""),
        str(event.get("message") or ""),
    )


def evaluate_scheduler_soak(
    *,
    duration_completed: bool,
    interrupted: bool,
    sample_count: int,
    successful_sample_count: int,
    loop_running_count: int,
    task_registration_counts: Dict[str, int],
    terminal_counts: Dict[str, int],
    failed_counts: Dict[str, int],
    overlap_skip_counts: Dict[str, int],
    required_tasks: Iterable[str],
    min_api_success_ratio: float,
    min_loop_running_ratio: float,
    min_task_registration_ratio: float,
    max_failed_count: int,
    max_overlap_skip_count: int,
) -> Dict[str, Any]:
    samples = max(0, int(sample_count or 0))
    successful = max(0, int(successful_sample_count or 0))
    loop_running = max(0, int(loop_running_count or 0))
    api_success_ratio = successful / samples if samples else 0.0
    loop_running_ratio = loop_running / successful if successful else 0.0
    required = sorted({str(name).strip() for name in required_tasks if str(name).strip()})
    missing_registrations = [
        name for name in required if int(task_registration_counts.get(name) or 0) <= 0
    ]
    missing_terminal_runs = [
        name for name in required if int(terminal_counts.get(name) or 0) <= 0
    ]
    task_registration_ratios = {
        name: round(
            max(0, int(task_registration_counts.get(name) or 0)) / successful,
            6,
        )
        if successful
        else 0.0
        for name in required
    }
    intermittently_missing_tasks = [
        name
        for name in required
        if successful > 0
        and task_registration_ratios[name] + 1e-12 < min_task_registration_ratio
    ]
    total_failed = sum(max(0, int(value or 0)) for value in failed_counts.values())
    total_overlap_skips = sum(
        max(0, int(value or 0)) for value in overlap_skip_counts.values()
    )
    failures = []
    if interrupted:
        failures.append("interrupted")
    elif not duration_completed:
        failures.append("duration_incomplete")
    if samples <= 0:
        failures.append("no_samples")
    elif successful <= 0:
        failures.append("api_never_reached")
    if samples > 0 and api_success_ratio + 1e-12 < min_api_success_ratio:
        failures.append("api_success_ratio_below_threshold")
    if successful > 0 and loop_running <= 0:
        failures.append("scheduler_loop_never_running")
    if successful > 0 and loop_running_ratio + 1e-12 < min_loop_running_ratio:
        failures.append("scheduler_loop_ratio_below_threshold")
    if missing_registrations:
        failures.append("required_tasks_not_registered")
    elif intermittently_missing_tasks:
        failures.append("required_task_registration_ratio_below_threshold")
    if missing_terminal_runs:
        failures.append("required_task_terminal_runs_missing")
    if total_failed > max_failed_count:
        failures.append("task_failures_above_threshold")
    if total_overlap_skips > max_overlap_skip_count:
        failures.append("task_overlap_skips_above_threshold")
    return {
        "ok": not failures,
        "failures": failures,
        "sample_count": samples,
        "successful_sample_count": successful,
        "api_success_ratio": round(api_success_ratio, 6),
        "min_api_success_ratio": min_api_success_ratio,
        "loop_running_count": loop_running,
        "loop_running_ratio": round(loop_running_ratio, 6),
        "min_loop_running_ratio": min_loop_running_ratio,
        "required_tasks": required,
        "task_registration_ratios": task_registration_ratios,
        "min_task_registration_ratio": min_task_registration_ratio,
        "missing_registrations": missing_registrations,
        "intermittently_missing_tasks": intermittently_missing_tasks,
        "missing_terminal_runs": missing_terminal_runs,
        "failed_count": total_failed,
        "max_failed_count": max_failed_count,
        "overlap_skip_count": total_overlap_skips,
        "max_overlap_skip_count": max_overlap_skip_count,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--duration-seconds", type=float, default=300.0)
    parser.add_argument("--sample-interval-seconds", type=float, default=5.0)
    parser.add_argument("--request-timeout-seconds", type=float, default=5.0)
    parser.add_argument("--min-api-success-ratio", type=float, default=0.99)
    parser.add_argument("--min-loop-running-ratio", type=float, default=0.99)
    parser.add_argument("--min-task-registration-ratio", type=float, default=0.99)
    parser.add_argument(
        "--require-task",
        action="append",
        default=[],
        help="Require a registered task and at least one terminal event; may be repeated.",
    )
    parser.add_argument("--max-failed-count", type=int, default=0)
    parser.add_argument("--max-overlap-skip-count", type=int, default=0)
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
    min_loop_ratio = _bounded_float(
        parser,
        "--min-loop-running-ratio",
        args.min_loop_running_ratio,
        minimum=0.0,
        maximum=1.0,
    )
    min_task_ratio = _bounded_float(
        parser,
        "--min-task-registration-ratio",
        args.min_task_registration_ratio,
        minimum=0.0,
        maximum=1.0,
    )
    max_failed = _bounded_int(
        parser, "--max-failed-count", args.max_failed_count, minimum=0, maximum=100000
    )
    max_overlap = _bounded_int(
        parser,
        "--max-overlap-skip-count",
        args.max_overlap_skip_count,
        minimum=0,
        maximum=100000,
    )

    started_at = _utc_iso()
    started_monotonic = time.monotonic()
    sample_count = 0
    successful_sample_count = 0
    loop_running_count = 0
    task_registration_counts: Counter[str] = Counter()
    terminal_counts: Counter[str] = Counter()
    failed_counts: Counter[str] = Counter()
    overlap_skip_counts: Counter[str] = Counter()
    seen_events: set[tuple[str, str, str, str]] = set()
    event_baseline_initialized = False
    error_counts: Counter[str] = Counter()
    interrupted = False

    try:
        while True:
            elapsed = time.monotonic() - started_monotonic
            if elapsed >= duration:
                break
            sample_count += 1
            try:
                status = _read_json(
                    base_url,
                    "/api/v1/vnpy-paper/status?include_snapshot=false&include_recent_trades=false",
                    timeout_seconds=timeout,
                )
                events_payload = _read_json(
                    base_url,
                    f"/api/v1/vnpy-paper/task-events?{urlencode({'limit': 100})}",
                    timeout_seconds=timeout,
                )
                successful_sample_count += 1
                scheduler = status.get("scheduler")
                scheduler = scheduler if isinstance(scheduler, dict) else {}
                if bool(scheduler.get("loop_running")):
                    loop_running_count += 1
                for task in scheduler.get("background_tasks") or []:
                    if isinstance(task, dict) and str(task.get("name") or "").strip():
                        task_registration_counts[str(task["name"]).strip()] += 1
                current_events = [
                    event
                    for event in events_payload.get("items") or []
                    if isinstance(event, dict)
                ]
                if not event_baseline_initialized:
                    seen_events.update(_event_key(event) for event in current_events)
                    event_baseline_initialized = True
                    current_events = []
                for event in current_events:
                    key = _event_key(event)
                    if key in seen_events:
                        continue
                    seen_events.add(key)
                    name = str(event.get("name") or "unknown").strip() or "unknown"
                    event_status = str(event.get("status") or "").strip()
                    if event_status in {"completed", "skipped", "failed"}:
                        terminal_counts[name] += 1
                    if event_status == "failed":
                        failed_counts[name] += 1
                    details = event.get("details")
                    if (
                        event_status == "skipped"
                        and isinstance(details, dict)
                        and details.get("reason") == "task_already_running"
                    ):
                        overlap_skip_counts[name] += 1
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

    elapsed_seconds = time.monotonic() - started_monotonic
    evaluation = evaluate_scheduler_soak(
        duration_completed=elapsed_seconds + 1e-6 >= duration,
        interrupted=interrupted,
        sample_count=sample_count,
        successful_sample_count=successful_sample_count,
        loop_running_count=loop_running_count,
        task_registration_counts=dict(task_registration_counts),
        terminal_counts=dict(terminal_counts),
        failed_counts=dict(failed_counts),
        overlap_skip_counts=dict(overlap_skip_counts),
        required_tasks=args.require_task,
        min_api_success_ratio=min_api_ratio,
        min_loop_running_ratio=min_loop_ratio,
        min_task_registration_ratio=min_task_ratio,
        max_failed_count=max_failed,
        max_overlap_skip_count=max_overlap,
    )
    result = {
        "schema_version": 1,
        "started_at": started_at,
        "finished_at": _utc_iso(),
        "duration_seconds": round(elapsed_seconds, 3),
        "configured_duration_seconds": duration,
        "base_url": base_url,
        "evaluation": evaluation,
        "task_registration_counts": dict(task_registration_counts),
        "terminal_counts": dict(terminal_counts),
        "failed_counts": dict(failed_counts),
        "overlap_skip_counts": dict(overlap_skip_counts),
        "error_counts": dict(error_counts),
    }
    encoded = json.dumps(result, ensure_ascii=False, indent=2)
    print(encoded)
    if args.output_json is not None:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(encoded + "\n", encoding="utf-8")
    return 0 if evaluation["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
