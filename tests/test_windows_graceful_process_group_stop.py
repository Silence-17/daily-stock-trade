from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import scripts.stop_windows_process_group_gracefully as stop_script


REPO_ROOT = Path(__file__).resolve().parents[1]


def _target() -> stop_script.ProcessGroup:
    return stop_script.ProcessGroup(
        pid=200,
        parent_pid=100,
        command_line="python check_vnpy_scheduled_external_acceptance.py",
        parent_command_line="python check_vnpy_scheduled_external_acceptance.py",
    )


def test_validated_target_requires_pid_parent_and_both_commands() -> None:
    result = stop_script._validated_target(
        _target(),
        expected_pid=200,
        expected_parent_pid=100,
        expected_command_fragment="check_vnpy_scheduled_external_acceptance.py",
    )

    assert result.pid == 200


@pytest.mark.parametrize(
    ("target", "pid", "parent_pid", "fragment", "error"),
    [
        (_target(), 201, 100, "acceptance.py", "process_pid_mismatch"),
        (_target(), 200, 101, "acceptance.py", "parent_pid_mismatch"),
        (_target(), 200, 100, "", "expected_command_fragment_empty"),
        (_target(), 200, 100, "different.py", "process_command_mismatch"),
        (
            stop_script.ProcessGroup(200, 100, "python acceptance.py", "python other.py"),
            200,
            100,
            "acceptance.py",
            "parent_command_mismatch",
        ),
    ],
)
def test_validated_target_rejects_identity_drift(
    target,
    pid,
    parent_pid,
    fragment,
    error,
) -> None:
    with pytest.raises(stop_script.ProcessGroupStopError, match=error):
        stop_script._validated_target(
            target,
            expected_pid=pid,
            expected_parent_pid=parent_pid,
            expected_command_fragment=fragment,
        )


def test_stop_process_group_requires_both_processes_to_exit() -> None:
    fake_os = SimpleNamespace(name="nt", getpid=lambda: 999)
    with patch.object(stop_script, "os", fake_os), patch.object(
        stop_script,
        "_query_process",
        return_value=_target(),
    ), patch.object(
        stop_script,
        "_send_ctrl_break",
        return_value=0,
    ) as send_break, patch.object(
        stop_script,
        "_pid_alive",
        return_value=False,
    ):
        result = stop_script.stop_process_group_gracefully(
            expected_pid=200,
            expected_parent_pid=100,
            expected_command_fragment="acceptance.py",
            timeout_seconds=1,
        )

    assert result["ok"] is True
    assert result["process_exited"] is True
    assert result["parent_exited"] is True
    assert result["hard_kill_attempted"] is False
    send_break.assert_called_once_with(200)


def test_stop_process_group_rejects_failed_ctrl_break_delivery() -> None:
    fake_os = SimpleNamespace(name="nt", getpid=lambda: 999)
    with patch.object(stop_script, "os", fake_os), patch.object(
        stop_script,
        "_query_process",
        return_value=_target(),
    ), patch.object(stop_script, "_send_ctrl_break", return_value=11):
        with pytest.raises(
            stop_script.ProcessGroupStopError,
            match="ctrl_break_not_delivered",
        ):
            stop_script.stop_process_group_gracefully(
                expected_pid=200,
                expected_parent_pid=100,
                expected_command_fragment="acceptance.py",
                timeout_seconds=1,
            )


def test_cli_reports_sanitized_identity_failure(capsys) -> None:
    with patch.object(
        stop_script,
        "stop_process_group_gracefully",
        side_effect=stop_script.ProcessGroupStopError("process_command_mismatch"),
    ):
        exit_code = stop_script.main([
            "--expected-pid",
            "200",
            "--expected-parent-pid",
            "100",
            "--expected-command-fragment",
            "acceptance.py",
            "--confirmation",
            stop_script.CONFIRMATION,
        ])

    assert exit_code == 1
    assert '"error": "process_command_mismatch"' in capsys.readouterr().out


def test_direct_script_help_resolves_sibling_import() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "stop_windows_process_group_gracefully.py"),
            "--help",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "STOP_PROCESS_GROUP_GRACEFULLY" not in completed.stderr
    assert "--expected-command-fragment" in completed.stdout
