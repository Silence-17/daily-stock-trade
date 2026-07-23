# -*- coding: utf-8 -*-
"""Stop one Windows Uvicorn listener with CTRL_BREAK and strict PID guards."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, List
from ctypes import wintypes


CONFIRMATION = "STOP_UVICORN_GRACEFULLY"
CTRL_BREAK_EVENT = 1
CTRL_EVENT_EXIT_CODES = {0, 0xC000013A, -1073741510}
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259


class StopError(RuntimeError):
    """A sanitized graceful-stop failure."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ListenerProcess:
    pid: int
    parent_pid: int
    command_line: str
    parent_command_line: str


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


def _query_listener(port: int) -> List[ListenerProcess]:
    command = rf"""
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$connections = @(Get-NetTCPConnection -State Listen -LocalPort {port} -ErrorAction SilentlyContinue)
$rows = foreach ($connection in $connections) {{
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$($connection.OwningProcess)" -ErrorAction SilentlyContinue
    if ($null -eq $process) {{ continue }}
    $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$($process.ParentProcessId)" -ErrorAction SilentlyContinue
    [pscustomobject]@{{
        pid = [int]$process.ProcessId
        parent_pid = [int]$process.ParentProcessId
        command_line = [string]$process.CommandLine
        parent_command_line = if ($null -eq $parent) {{ "" }} else {{ [string]$parent.CommandLine }}
    }}
}}
ConvertTo-Json -InputObject @($rows) -Compress
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
        raise StopError("listener_query_failed") from exc
    if completed.returncode != 0:
        raise StopError("listener_query_failed")
    try:
        payload = json.loads(completed.stdout.strip() or "[]")
    except json.JSONDecodeError as exc:
        raise StopError("listener_query_invalid") from exc
    if not isinstance(payload, list):
        raise StopError("listener_query_invalid")
    listeners: List[ListenerProcess] = []
    for row in payload:
        if not isinstance(row, dict):
            raise StopError("listener_query_invalid")
        listeners.append(
            ListenerProcess(
                pid=int(row.get("pid") or 0),
                parent_pid=int(row.get("parent_pid") or 0),
                command_line=str(row.get("command_line") or ""),
                parent_command_line=str(row.get("parent_command_line") or ""),
            )
        )
    return listeners


def _validated_target(
    listeners: List[ListenerProcess],
    *,
    port: int,
    expected_listener_pid: int,
    expected_parent_pid: int,
    expected_command_fragment: str,
) -> ListenerProcess:
    if len(listeners) != 1:
        raise StopError("listener_count_mismatch")
    target = listeners[0]
    if target.pid != expected_listener_pid:
        raise StopError("listener_pid_mismatch")
    if target.parent_pid != expected_parent_pid:
        raise StopError("parent_pid_mismatch")
    command = target.command_line.casefold()
    parent_command = target.parent_command_line.casefold()
    fragment = expected_command_fragment.strip().casefold()
    port_token = f"--port {port}"
    if not fragment:
        raise StopError("expected_command_fragment_empty")
    if "uvicorn" not in command or fragment not in command or port_token not in command:
        raise StopError("listener_command_mismatch")
    if "uvicorn" not in parent_command or fragment not in parent_command or port_token not in parent_command:
        raise StopError("parent_command_mismatch")
    return target


def _send_ctrl_break(listener_pid: int) -> int:
    helper_code = f"""
import ctypes
import sys
import time

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.FreeConsole()
if not kernel32.AttachConsole({listener_pid}):
    sys.exit(10)
kernel32.SetConsoleCtrlHandler(None, True)
if not kernel32.GenerateConsoleCtrlEvent({CTRL_BREAK_EVENT}, 0):
    kernel32.FreeConsole()
    sys.exit(11)
time.sleep(1.0)
kernel32.FreeConsole()
"""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            [sys.executable, "-c", helper_code],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StopError("ctrl_break_helper_failed") from exc
    return int(completed.returncode)


def _pid_alive(pid: int) -> bool:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.3):
            return True
    except OSError:
        return False


def stop_uvicorn_gracefully(
    *,
    port: int,
    expected_listener_pid: int,
    expected_parent_pid: int,
    expected_command_fragment: str,
    timeout_seconds: float,
) -> Dict[str, Any]:
    if os.name != "nt":
        raise StopError("windows_required")
    listeners = _query_listener(port)
    target = _validated_target(
        listeners,
        port=port,
        expected_listener_pid=expected_listener_pid,
        expected_parent_pid=expected_parent_pid,
        expected_command_fragment=expected_command_fragment,
    )
    if os.getpid() in {target.pid, target.parent_pid}:
        raise StopError("controller_matches_target")

    started_at = time.monotonic()
    helper_exit_code = _send_ctrl_break(target.pid)
    if helper_exit_code not in CTRL_EVENT_EXIT_CODES:
        raise StopError("ctrl_break_not_delivered")

    deadline = started_at + timeout_seconds
    while time.monotonic() < deadline:
        if not _pid_alive(target.pid) and not _pid_alive(target.parent_pid):
            break
        time.sleep(0.25)

    listener_alive = _pid_alive(target.pid)
    parent_alive = _pid_alive(target.parent_pid)
    remaining_listeners = _query_listener(port)
    port_closed = not _port_open("127.0.0.1", port)
    ok = not listener_alive and not parent_alive and not remaining_listeners and port_closed
    duration = round(max(0.0, time.monotonic() - started_at), 3)
    return {
        "schema_version": 1,
        "ok": ok,
        "port": port,
        "listener_pid": target.pid,
        "parent_pid": target.parent_pid,
        "ctrl_break_helper_exit_code": helper_exit_code,
        "listener_exited": not listener_alive,
        "parent_exited": not parent_alive,
        "listener_released": not remaining_listeners,
        "port_closed": port_closed,
        "timed_out": not ok and duration >= timeout_seconds,
        "duration_seconds": duration,
        "hard_kill_attempted": False,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--expected-listener-pid", type=int, required=True)
    parser.add_argument("--expected-parent-pid", type=int, required=True)
    parser.add_argument("--expected-command-fragment", default="server:app")
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument("--confirmation", required=True)
    return parser


def main(argv: List[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    port = _bounded_int(parser, "--port", args.port, minimum=1, maximum=65535)
    listener_pid = _bounded_int(
        parser,
        "--expected-listener-pid",
        args.expected_listener_pid,
        minimum=1,
        maximum=0xFFFFFFFF,
    )
    parent_pid = _bounded_int(
        parser,
        "--expected-parent-pid",
        args.expected_parent_pid,
        minimum=1,
        maximum=0xFFFFFFFF,
    )
    if args.timeout_seconds < 1 or args.timeout_seconds > 120:
        parser.error("--timeout-seconds must be between 1 and 120")
    if args.confirmation != CONFIRMATION:
        parser.error(f"--confirmation must equal {CONFIRMATION}")

    try:
        result = stop_uvicorn_gracefully(
            port=port,
            expected_listener_pid=listener_pid,
            expected_parent_pid=parent_pid,
            expected_command_fragment=args.expected_command_fragment,
            timeout_seconds=float(args.timeout_seconds),
        )
    except StopError as exc:
        print(json.dumps({"schema_version": 1, "ok": False, "error": exc.code}))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
