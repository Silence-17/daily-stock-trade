# -*- coding: utf-8 -*-
"""Tests for the guarded Windows Uvicorn graceful-stop helper."""

from __future__ import annotations

from unittest.mock import patch

import pytest

import scripts.stop_windows_uvicorn_gracefully as graceful_stop
from scripts.stop_windows_uvicorn_gracefully import ListenerProcess, StopError


def _listener() -> ListenerProcess:
    command = "python -m uvicorn server:app --host 127.0.0.1 --port 8000"
    return ListenerProcess(
        pid=200,
        parent_pid=100,
        command_line=command,
        parent_command_line=command,
    )


def test_validated_target_requires_exact_listener_and_parent_identity() -> None:
    with pytest.raises(StopError, match="listener_pid_mismatch"):
        graceful_stop._validated_target(
            [_listener()],
            port=8000,
            expected_listener_pid=201,
            expected_parent_pid=100,
            expected_command_fragment="server:app",
        )

    with pytest.raises(StopError, match="parent_pid_mismatch"):
        graceful_stop._validated_target(
            [_listener()],
            port=8000,
            expected_listener_pid=200,
            expected_parent_pid=101,
            expected_command_fragment="server:app",
        )


def test_validated_target_rejects_command_or_port_drift() -> None:
    with pytest.raises(StopError, match="listener_command_mismatch"):
        graceful_stop._validated_target(
            [_listener()],
            port=8001,
            expected_listener_pid=200,
            expected_parent_pid=100,
            expected_command_fragment="server:app",
        )

    with pytest.raises(StopError, match="listener_command_mismatch"):
        graceful_stop._validated_target(
            [_listener()],
            port=8000,
            expected_listener_pid=200,
            expected_parent_pid=100,
            expected_command_fragment="other:app",
        )


def test_stop_accepts_helper_ctrl_event_exit_when_target_closes() -> None:
    with patch.object(graceful_stop, "os") as os_module, patch.object(
        graceful_stop,
        "_query_listener",
        side_effect=[[_listener()], []],
    ), patch.object(
        graceful_stop,
        "_send_ctrl_break",
        return_value=0xC000013A,
    ), patch.object(
        graceful_stop,
        "_pid_alive",
        return_value=False,
    ), patch.object(
        graceful_stop,
        "_port_open",
        return_value=False,
    ):
        os_module.name = "nt"
        os_module.getpid.return_value = 999
        result = graceful_stop.stop_uvicorn_gracefully(
            port=8000,
            expected_listener_pid=200,
            expected_parent_pid=100,
            expected_command_fragment="server:app",
            timeout_seconds=30,
        )

    assert result["ok"] is True
    assert result["ctrl_break_helper_exit_code"] == 0xC000013A
    assert result["hard_kill_attempted"] is False


def test_stop_fails_closed_when_ctrl_break_helper_cannot_attach() -> None:
    with patch.object(graceful_stop, "os") as os_module, patch.object(
        graceful_stop,
        "_query_listener",
        return_value=[_listener()],
    ), patch.object(
        graceful_stop,
        "_send_ctrl_break",
        return_value=10,
    ):
        os_module.name = "nt"
        os_module.getpid.return_value = 999
        with pytest.raises(StopError, match="ctrl_break_not_delivered"):
            graceful_stop.stop_uvicorn_gracefully(
                port=8000,
                expected_listener_pid=200,
                expected_parent_pid=100,
                expected_command_fragment="server:app",
                timeout_seconds=30,
            )


def test_stop_times_out_without_attempting_hard_kill() -> None:
    with patch.object(graceful_stop, "os") as os_module, patch.object(
        graceful_stop,
        "_query_listener",
        side_effect=[[_listener()], [_listener()]],
    ), patch.object(
        graceful_stop,
        "_send_ctrl_break",
        return_value=0xC000013A,
    ), patch.object(
        graceful_stop,
        "_pid_alive",
        return_value=True,
    ), patch.object(
        graceful_stop,
        "_port_open",
        return_value=True,
    ), patch.object(
        graceful_stop.time,
        "monotonic",
        side_effect=[0.0, 2.0, 2.0],
    ):
        os_module.name = "nt"
        os_module.getpid.return_value = 999
        result = graceful_stop.stop_uvicorn_gracefully(
            port=8000,
            expected_listener_pid=200,
            expected_parent_pid=100,
            expected_command_fragment="server:app",
            timeout_seconds=1,
        )

    assert result["ok"] is False
    assert result["timed_out"] is True
    assert result["hard_kill_attempted"] is False


def test_stop_rejects_controller_that_is_target_parent() -> None:
    with patch.object(graceful_stop, "os") as os_module, patch.object(
        graceful_stop,
        "_query_listener",
        return_value=[_listener()],
    ):
        os_module.name = "nt"
        os_module.getpid.return_value = 100
        with pytest.raises(StopError, match="controller_matches_target"):
            graceful_stop.stop_uvicorn_gracefully(
                port=8000,
                expected_listener_pid=200,
                expected_parent_pid=100,
                expected_command_fragment="server:app",
                timeout_seconds=30,
            )


def test_main_requires_explicit_confirmation() -> None:
    with pytest.raises(SystemExit) as exc_info:
        graceful_stop.main([
            "--port",
            "8000",
            "--expected-listener-pid",
            "200",
            "--expected-parent-pid",
            "100",
            "--confirmation",
            "wrong",
        ])

    assert exc_info.value.code == 2
