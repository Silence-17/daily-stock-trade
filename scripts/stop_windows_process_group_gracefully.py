# -*- coding: utf-8 -*-
"""Stop one guarded Windows process group with CTRL_BREAK and no hard kill."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Dict, List

if __package__:
    from scripts.stop_windows_uvicorn_gracefully import (
        CTRL_EVENT_EXIT_CODES,
        _pid_alive,
        _send_ctrl_break,
    )
else:
    from stop_windows_uvicorn_gracefully import (  # type: ignore[no-redef]
        CTRL_EVENT_EXIT_CODES,
        _pid_alive,
        _send_ctrl_break,
    )


CONFIRMATION = "STOP_PROCESS_GROUP_GRACEFULLY"


class ProcessGroupStopError(RuntimeError):
    """A sanitized guarded-stop failure."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ProcessGroup:
    pid: int
    parent_pid: int
    command_line: str
    parent_command_line: str


def _bounded_pid(parser: argparse.ArgumentParser, name: str, value: int) -> int:
    if value < 1 or value > 0xFFFFFFFF:
        parser.error(f"{name} must be between 1 and 4294967295")
    return value


def _query_process(pid: int) -> ProcessGroup:
    command = rf"""
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$process = Get-CimInstance Win32_Process -Filter "ProcessId={pid}" -ErrorAction SilentlyContinue
if ($null -eq $process) {{
    @{{ found = $false }} | ConvertTo-Json -Compress
    exit 0
}}
$parent = Get-CimInstance Win32_Process -Filter "ProcessId=$($process.ParentProcessId)" -ErrorAction SilentlyContinue
@{{
    found = $true
    pid = [int]$process.ProcessId
    parent_pid = [int]$process.ParentProcessId
    command_line = [string]$process.CommandLine
    parent_command_line = if ($null -eq $parent) {{ "" }} else {{ [string]$parent.CommandLine }}
}} | ConvertTo-Json -Compress
"""
    try:
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                command,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProcessGroupStopError("process_query_failed") from exc
    if completed.returncode != 0:
        raise ProcessGroupStopError("process_query_failed")
    try:
        payload = json.loads(completed.stdout.strip() or "{}")
    except json.JSONDecodeError as exc:
        raise ProcessGroupStopError("process_query_invalid") from exc
    if not isinstance(payload, dict):
        raise ProcessGroupStopError("process_query_invalid")
    if payload.get("found") is not True:
        raise ProcessGroupStopError("process_not_found")
    return ProcessGroup(
        pid=int(payload.get("pid") or 0),
        parent_pid=int(payload.get("parent_pid") or 0),
        command_line=str(payload.get("command_line") or ""),
        parent_command_line=str(payload.get("parent_command_line") or ""),
    )


def _validated_target(
    target: ProcessGroup,
    *,
    expected_pid: int,
    expected_parent_pid: int,
    expected_command_fragment: str,
) -> ProcessGroup:
    if target.pid != expected_pid:
        raise ProcessGroupStopError("process_pid_mismatch")
    if target.parent_pid != expected_parent_pid:
        raise ProcessGroupStopError("parent_pid_mismatch")
    fragment = str(expected_command_fragment or "").strip().casefold()
    if not fragment:
        raise ProcessGroupStopError("expected_command_fragment_empty")
    if fragment not in target.command_line.casefold():
        raise ProcessGroupStopError("process_command_mismatch")
    if fragment not in target.parent_command_line.casefold():
        raise ProcessGroupStopError("parent_command_mismatch")
    return target


def stop_process_group_gracefully(
    *,
    expected_pid: int,
    expected_parent_pid: int,
    expected_command_fragment: str,
    timeout_seconds: float,
) -> Dict[str, Any]:
    if os.name != "nt":
        raise ProcessGroupStopError("windows_required")
    target = _validated_target(
        _query_process(expected_pid),
        expected_pid=expected_pid,
        expected_parent_pid=expected_parent_pid,
        expected_command_fragment=expected_command_fragment,
    )
    if os.getpid() in {target.pid, target.parent_pid}:
        raise ProcessGroupStopError("controller_matches_target")

    started_at = time.monotonic()
    helper_exit_code = _send_ctrl_break(target.pid)
    if helper_exit_code not in CTRL_EVENT_EXIT_CODES:
        raise ProcessGroupStopError("ctrl_break_not_delivered")

    deadline = started_at + timeout_seconds
    while time.monotonic() < deadline:
        if not _pid_alive(target.pid) and not _pid_alive(target.parent_pid):
            break
        time.sleep(0.25)

    process_alive = _pid_alive(target.pid)
    parent_alive = _pid_alive(target.parent_pid)
    duration = round(max(0.0, time.monotonic() - started_at), 3)
    ok = not process_alive and not parent_alive
    return {
        "schema_version": 1,
        "ok": ok,
        "process_pid": target.pid,
        "parent_pid": target.parent_pid,
        "ctrl_break_helper_exit_code": helper_exit_code,
        "process_exited": not process_alive,
        "parent_exited": not parent_alive,
        "timed_out": not ok and duration >= timeout_seconds,
        "duration_seconds": duration,
        "hard_kill_attempted": False,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-pid", type=int, required=True)
    parser.add_argument("--expected-parent-pid", type=int, required=True)
    parser.add_argument("--expected-command-fragment", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument("--confirmation", required=True)
    return parser


def main(argv: List[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    process_pid = _bounded_pid(parser, "--expected-pid", args.expected_pid)
    parent_pid = _bounded_pid(
        parser,
        "--expected-parent-pid",
        args.expected_parent_pid,
    )
    if args.timeout_seconds < 1 or args.timeout_seconds > 120:
        parser.error("--timeout-seconds must be between 1 and 120")
    if args.confirmation != CONFIRMATION:
        parser.error(f"--confirmation must equal {CONFIRMATION}")

    try:
        result = stop_process_group_gracefully(
            expected_pid=process_pid,
            expected_parent_pid=parent_pid,
            expected_command_fragment=args.expected_command_fragment,
            timeout_seconds=float(args.timeout_seconds),
        )
    except ProcessGroupStopError as exc:
        print(json.dumps({"schema_version": 1, "ok": False, "error": exc.code}))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
