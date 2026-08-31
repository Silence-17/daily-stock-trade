"""Bundle read-only production evidence for the online stock-selection Agent.

The command first runs the zero-connect gateway preflight, then observes the
deployed vn.py runtime and scheduler concurrently, and finally evaluates the
persisted multi-market calibration evidence. It never triggers an Agent run or
creates an order.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

if __package__:
    from scripts.check_online_agent_vnpy_e2e import _scheduler_execution_task_name
else:
    from check_online_agent_vnpy_e2e import _scheduler_execution_task_name


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REQUIRED_TASKS = (
    "vnpy_paper_auto_retry",
    "agent_calibration_shadow",
    "agent_calibration_evidence",
)
DEFAULT_OBSERVED_EVENTS = ("account", "position")


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bounded_float(
    parser: argparse.ArgumentParser,
    name: str,
    value: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    numeric = float(value)
    if numeric < minimum or numeric > maximum:
        parser.error(f"{name} must be between {minimum} and {maximum}")
    return numeric


def _validated_base_url(parser: argparse.ArgumentParser, value: str) -> str:
    base_url = str(value or "").strip().rstrip("/")
    parsed = urlparse(base_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        parser.error("--base-url must be an HTTP(S) origin without credentials, query, or fragment")
    return base_url


def _write_json_file(payload: Mapping[str, Any], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_name(f".{output_path.name}.{os.getpid()}.tmp")
    temporary_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary_path, output_path)


def _read_json_file(path: Path) -> Dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _read_json_url(url: str, *, timeout_seconds: float) -> Dict[str, Any]:
    request = Request(url, headers={"Accept": "application/json"})
    with urlopen(request, timeout=timeout_seconds) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise ValueError("response must be an object")
    return payload


def evaluate_production_acceptance(
    *,
    preflight: Mapping[str, Any],
    runtime: Mapping[str, Any],
    scheduler: Mapping[str, Any],
    calibration: Mapping[str, Any],
    stage_exit_codes: Mapping[str, int | None],
    interrupted: bool = False,
) -> Dict[str, Any]:
    failures: list[str] = []
    if interrupted:
        failures.append("acceptance_interrupted")

    if not preflight.get("ok"):
        failures.append("gateway_preflight_failed")
    if not preflight.get("external_gateway"):
        failures.append("external_gateway_not_confirmed")
    if not preflight.get("production_preflight_enabled"):
        failures.append("production_preflight_not_enabled")
    if preflight.get("connect_attempted"):
        failures.append("preflight_connected_unexpectedly")
    if preflight.get("orders_created"):
        failures.append("preflight_created_orders")

    reports = {
        "runtime": runtime,
        "scheduler": scheduler,
        "calibration": calibration,
    }
    downstream_required = bool(preflight.get("ok") and preflight.get("external_gateway"))
    for stage, report in reports.items():
        exit_code = stage_exit_codes.get(stage)
        if downstream_required and exit_code is None:
            failures.append(f"{stage}_not_finished")
        if exit_code not in {0, None}:
            failures.append(f"{stage}_exit_{exit_code}")
        if not report:
            if downstream_required:
                failures.append(f"{stage}_report_missing")
            continue
        if stage in {"runtime", "scheduler"} and report.get("phase") != "completed":
            failures.append(f"{stage}_not_completed")
        evaluation = report.get("evaluation")
        if not isinstance(evaluation, Mapping) or not evaluation.get("ok"):
            failures.append(f"{stage}_gate_failed")

    calibration_methodology = calibration.get("methodology")
    if not isinstance(calibration_methodology, Mapping):
        if downstream_required:
            failures.append("calibration_methodology_missing")
    else:
        if not calibration_methodology.get("read_only"):
            failures.append("calibration_not_read_only")
        if calibration_methodology.get("creates_agent_runs"):
            failures.append("calibration_created_agent_runs")
        if calibration_methodology.get("places_orders"):
            failures.append("calibration_placed_orders")

    failures = list(dict.fromkeys(failures))
    return {
        "ok": not failures,
        "production_ready": not failures,
        "failures": failures,
        "stage_exit_codes": dict(stage_exit_codes),
    }


def _stage_command(script_name: str, arguments: Sequence[str]) -> list[str]:
    return [sys.executable, str(ROOT / "scripts" / script_name), *arguments]


def _run_parallel_soaks(
    commands: Mapping[str, Sequence[str]],
    *,
    checkpoint: Callable[[Mapping[str, int | None]], None],
    poll_interval_seconds: float = 1.0,
) -> tuple[Dict[str, int | None], bool]:
    processes: Dict[str, subprocess.Popen[Any]] = {}
    try:
        for name, command in commands.items():
            processes[name] = subprocess.Popen(
                list(command),
                cwd=ROOT,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    except OSError:
        for process in processes.values():
            if process.poll() is None:
                process.terminate()
        for process in processes.values():
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        exit_codes = {
            name: processes[name].returncode if name in processes else 127
            for name in commands
        }
        checkpoint(exit_codes)
        return exit_codes, False
    interrupted = False
    try:
        while True:
            exit_codes = {name: process.poll() for name, process in processes.items()}
            checkpoint(exit_codes)
            if all(exit_code is not None for exit_code in exit_codes.values()):
                return exit_codes, interrupted
            time.sleep(poll_interval_seconds)
    except KeyboardInterrupt:
        interrupted = True
        for process in processes.values():
            if process.poll() is None:
                process.terminate()
        for process in processes.values():
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        return {name: process.returncode for name, process in processes.items()}, interrupted


def _run_stage(command: Sequence[str]) -> int:
    completed = subprocess.run(
        list(command),
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return int(completed.returncode)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--duration-seconds", type=float, default=86400.0)
    parser.add_argument("--sample-interval-seconds", type=float, default=30.0)
    parser.add_argument("--request-timeout-seconds", type=float, default=10.0)
    parser.add_argument("--checkpoint-interval-seconds", type=float, default=60.0)
    parser.add_argument("--expected-gateway-class")
    parser.add_argument("--expected-gateway-name")
    parser.add_argument(
        "--require-observed-event",
        action="append",
        choices=("account", "position", "order", "trade"),
        default=[],
    )
    parser.add_argument("--require-task", action="append", default=[])
    parser.add_argument("--market", action="append", dest="markets", default=[])
    parser.add_argument("--required-version", action="append", default=[])
    parser.add_argument("--output-json", type=Path, required=True)
    args = parser.parse_args(argv)

    base_url = _validated_base_url(parser, args.base_url)
    duration = _bounded_float(
        parser, "--duration-seconds", args.duration_seconds, minimum=1.0, maximum=86400.0
    )
    sample_interval = _bounded_float(
        parser,
        "--sample-interval-seconds",
        args.sample_interval_seconds,
        minimum=0.1,
        maximum=60.0,
    )
    request_timeout = _bounded_float(
        parser,
        "--request-timeout-seconds",
        args.request_timeout_seconds,
        minimum=0.1,
        maximum=120.0,
    )
    checkpoint_interval = _bounded_float(
        parser,
        "--checkpoint-interval-seconds",
        args.checkpoint_interval_seconds,
        minimum=0.0,
        maximum=3600.0,
    )
    output_path = args.output_json.resolve()
    stage_dir = output_path.with_name(f".{output_path.stem}.stages")
    runtime_path = stage_dir / "runtime.json"
    scheduler_path = stage_dir / "scheduler.json"
    calibration_path = stage_dir / "calibration.json"
    for stage_path in (runtime_path, scheduler_path, calibration_path):
        stage_path.unlink(missing_ok=True)
    started_at = _utc_iso()
    stage_exit_codes: Dict[str, int | None] = {
        "runtime": None,
        "scheduler": None,
        "calibration": None,
    }
    preflight: Dict[str, Any] = {}
    status: Dict[str, Any] = {}
    last_checkpoint_at = 0.0

    def build_result(phase: str, *, interrupted: bool = False) -> Dict[str, Any]:
        runtime = _read_json_file(runtime_path)
        scheduler = _read_json_file(scheduler_path)
        calibration = _read_json_file(calibration_path)
        evaluation = evaluate_production_acceptance(
            preflight=preflight,
            runtime=runtime,
            scheduler=scheduler,
            calibration=calibration,
            stage_exit_codes=stage_exit_codes,
            interrupted=interrupted,
        )
        return {
            "schema_version": 1,
            "phase": phase,
            "checkpoint": phase == "running",
            "started_at": started_at,
            "finished_at": None if phase == "running" else _utc_iso(),
            "base_url": base_url,
            "production_mode": True,
            "preflight": preflight,
            "stages": {
                "runtime": runtime,
                "scheduler": scheduler,
                "calibration": calibration,
            },
            "evaluation": evaluation,
            "methodology": {
                "read_only": True,
                "creates_agent_runs": False,
                "places_orders": False,
                "connects_gateway": False,
                "parallel_observation": True,
                "external_gateway_required": True,
            },
        }

    def write_checkpoint(exit_codes: Mapping[str, int | None]) -> None:
        nonlocal last_checkpoint_at
        stage_exit_codes.update(exit_codes)
        now = time.monotonic()
        if checkpoint_interval > 0 and now - last_checkpoint_at >= checkpoint_interval:
            _write_json_file(build_result("running"), output_path)
            last_checkpoint_at = now

    preflight_url = f"{base_url}/api/v1/vnpy-paper/gateway/preflight"
    try:
        preflight = _read_json_url(preflight_url, timeout_seconds=request_timeout)
    except HTTPError as exc:
        preflight = {"ok": False, "failures": [f"http_{exc.code}"]}
    except (URLError, TimeoutError):
        preflight = {"ok": False, "failures": ["connection_error"]}
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError, TypeError):
        preflight = {"ok": False, "failures": ["invalid_response"]}

    if not preflight.get("ok") or not preflight.get("external_gateway"):
        result = build_result("preflight_failed")
        _write_json_file(result, output_path)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1

    if not args.require_task:
        try:
            status = _read_json_url(
                f"{base_url}/api/v1/vnpy-paper/status"
                "?include_snapshot=false&include_recent_trades=false",
                timeout_seconds=request_timeout,
            )
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError,
                UnicodeDecodeError, ValueError, TypeError):
            status = {}

    common_soak_args = [
        "--base-url", base_url,
        "--duration-seconds", str(duration),
        "--sample-interval-seconds", str(sample_interval),
        "--request-timeout-seconds", str(request_timeout),
        "--checkpoint-interval-seconds", str(checkpoint_interval),
    ]
    runtime_args = [
        *common_soak_args,
        "--require-external-gateway",
        "--output-json", str(runtime_path),
    ]
    if args.expected_gateway_class:
        runtime_args.extend(["--expected-gateway-class", args.expected_gateway_class])
    if args.expected_gateway_name:
        runtime_args.extend(["--expected-gateway-name", args.expected_gateway_name])
    for event_name in args.require_observed_event or DEFAULT_OBSERVED_EVENTS:
        runtime_args.extend(["--require-observed-event", event_name])

    scheduler_args = [*common_soak_args, "--output-json", str(scheduler_path)]
    required_tasks = args.require_task or [
        _scheduler_execution_task_name(status),
        *DEFAULT_REQUIRED_TASKS,
    ]
    for task_name in required_tasks:
        scheduler_args.extend(["--require-task", task_name])

    commands = {
        "runtime": _stage_command("check_vnpy_deployed_runtime_soak.py", runtime_args),
        "scheduler": _stage_command("check_vnpy_scheduler_soak.py", scheduler_args),
    }
    _write_json_file(build_result("running"), output_path)
    last_checkpoint_at = time.monotonic()
    exit_codes, interrupted = _run_parallel_soaks(commands, checkpoint=write_checkpoint)
    stage_exit_codes.update(exit_codes)
    if interrupted:
        result = build_result("interrupted", interrupted=True)
        _write_json_file(result, output_path)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 130

    calibration_args = [
        "--base-url", base_url,
        "--request-timeout-seconds", str(request_timeout),
        "--output-json", str(calibration_path),
    ]
    for market in args.markets or ("cn", "hk", "us"):
        calibration_args.extend(["--market", market])
    for version in args.required_version or ("candidate-return-risk-v1",):
        calibration_args.extend(["--required-version", version])
    try:
        stage_exit_codes["calibration"] = _run_stage(
            _stage_command("check_agent_calibration_evidence.py", calibration_args)
        )
    except KeyboardInterrupt:
        stage_exit_codes["calibration"] = 130
        result = build_result("interrupted", interrupted=True)
        _write_json_file(result, output_path)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 130

    result = build_result("completed")
    _write_json_file(result, output_path)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["evaluation"]["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
