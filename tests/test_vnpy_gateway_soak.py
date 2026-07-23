# -*- coding: utf-8 -*-
"""Tests for the no-order vn.py gateway soak evaluator."""

from __future__ import annotations

import json
from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace
from unittest.mock import Mock

import scripts.check_vnpy_gateway_soak as gateway_soak
from src.services.vnpy_runtime import VnpyRuntimeSettings

from scripts.check_vnpy_gateway_soak import (
    _build_soak_result,
    _run_gateway_preflight,
    _safe_runtime_summary,
    _wait_for_stable_connection,
    _write_json_file,
    evaluate_soak,
)


def test_startup_wait_requires_continuous_connected_stability() -> None:
    statuses = iter(
        ["connected", "disconnected", "connected", "connected", "connected"]
    )
    clock = [0.0]
    runtime_handle = SimpleNamespace(
        refresh_diagnostics=lambda: {
            "available": True,
            "connect": {"status": next(statuses)},
        }
    )

    stable = _wait_for_stable_connection(
        runtime_handle,
        grace_seconds=2.0,
        stability_seconds=0.4,
        monotonic=lambda: clock[0],
        sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    )

    assert stable is True
    assert round(clock[0], 3) == 0.8


def test_evaluate_soak_accepts_connected_runtime_and_required_events() -> None:
    result = evaluate_soak(
        runtime_available=True,
        duration_completed=True,
        interrupted=False,
        sample_counts={"connected": 99, "disconnected": 1},
        event_counts={"account": 2, "position": 4},
        required_events=["account", "position"],
        min_connected_ratio=0.99,
    )

    assert result["ok"] is True
    assert result["connected_ratio"] == 0.99
    assert result["failures"] == []


def test_evaluate_soak_reports_each_failed_acceptance_condition() -> None:
    result = evaluate_soak(
        runtime_available=False,
        duration_completed=False,
        interrupted=False,
        sample_counts={"disconnected": 3},
        event_counts={"account": 0},
        required_events=["account"],
        min_connected_ratio=0.8,
    )

    assert result["ok"] is False
    assert result["failures"] == [
        "runtime_unavailable",
        "duration_incomplete",
        "connection_never_confirmed",
        "connected_ratio_below_threshold",
        "required_events_missing",
    ]
    assert result["missing_events"] == ["account"]


def test_evaluate_soak_marks_interruption_without_duration_duplicate() -> None:
    result = evaluate_soak(
        runtime_available=True,
        duration_completed=False,
        interrupted=True,
        sample_counts={"connected": 2},
        event_counts={},
        required_events=[],
        min_connected_ratio=1.0,
    )

    assert result["ok"] is False
    assert result["failures"] == ["interrupted"]


def test_evaluate_soak_requires_confirmed_reconnect_and_restored_connection() -> None:
    result = evaluate_soak(
        runtime_available=True,
        duration_completed=True,
        interrupted=False,
        sample_counts={"connected": 9, "disconnected": 1},
        event_counts={},
        required_events=[],
        min_connected_ratio=0.9,
        require_reconnect=True,
        disconnect_injection_required=True,
        disconnect_injected=True,
        reconnect_attempt_count=1,
        reconnect_success_count=1,
        final_connection_status="connected",
    )

    assert result["ok"] is True
    assert result["failures"] == []
    assert result["require_reconnect"] is True
    assert result["reconnect_success_count"] == 1


def test_evaluate_soak_reports_each_reconnect_acceptance_failure() -> None:
    result = evaluate_soak(
        runtime_available=True,
        duration_completed=True,
        interrupted=False,
        sample_counts={"connected": 10},
        event_counts={},
        required_events=[],
        min_connected_ratio=1.0,
        require_reconnect=True,
        disconnect_injection_required=True,
        disconnect_injected=False,
        reconnect_attempt_count=0,
        reconnect_success_count=0,
        final_connection_status="disconnected",
    )

    assert result["ok"] is False
    assert result["failures"] == [
        "simulated_disconnect_not_injected",
        "reconnect_not_attempted",
        "reconnect_not_confirmed",
        "connection_not_restored",
    ]


def test_safe_runtime_summary_omits_connection_path_and_message() -> None:
    summary = _safe_runtime_summary(
        {
            "available": True,
            "mode": "vnpy_runtime",
            "connect": {
                "status": "failed",
                "settings_source": "file",
                "settings_path": "C:/private/broker-account.json",
                "message": "password=should-not-leak",
            },
            "auto_reconnect": {
                "enabled": True,
                "running": True,
                "attempt_count": 2,
                "failure_count": 2,
            },
        }
    )

    encoded = json.dumps(summary)
    assert summary["settings_source"] == "file"
    assert "broker-account" not in encoded
    assert "should-not-leak" not in encoded


def test_preflight_subprocess_uses_environment_without_exposing_settings_path(
    monkeypatch,
) -> None:
    def invoke(command, **_kwargs):
        output_index = command.index("--output-json") + 1
        Path(command[output_index]).write_text(
            json.dumps({"ok": True, "evaluation": {"failures": []}}),
            encoding="utf-8",
        )
        return CompletedProcess(
            args=command,
            returncode=0,
            stdout="plugin startup banner\n",
            stderr="",
        )

    run = Mock(side_effect=invoke)
    monkeypatch.setattr(gateway_soak.subprocess, "run", run)

    result = _run_gateway_preflight(
        require_external_gateway=True,
        require_all_default_keys=True,
        timeout_seconds=45.0,
    )

    command = run.call_args.args[0]
    assert result["ok"] is True
    assert result["exit_code"] == 0
    assert "--require-external-gateway" in command
    assert "--require-all-default-keys" in command
    assert "--output-json" in command
    assert all("settings" not in argument.lower() for argument in command)
    assert run.call_args.kwargs["timeout"] == 45.0


def test_preflight_failure_exits_before_runtime_bootstrap(
    monkeypatch,
    capsys,
) -> None:
    settings = VnpyRuntimeSettings(
        enabled=True,
        gateway_class="src.services.vnpy_simulated_gateway:DsaSimulatedGateway",
        gateway_name="DSA_SIM",
        connect_on_start=True,
    )
    monkeypatch.setattr(gateway_soak, "load_vnpy_runtime_settings", lambda: settings)
    monkeypatch.setattr(
        gateway_soak,
        "_run_gateway_preflight",
        lambda **_kwargs: {
            "ok": False,
            "evaluation": {"failures": ["builtin_gateway_not_external"]},
        },
    )
    bootstrap = Mock(side_effect=AssertionError("must not connect"))
    monkeypatch.setattr(gateway_soak, "bootstrap_vnpy_runtime", bootstrap)

    exit_code = gateway_soak.main(
        ["--duration-seconds", "1", "--require-external-gateway"]
    )
    result = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert bootstrap.call_count == 0
    assert result["schema_version"] == 3
    assert result["connection_attempted"] is False
    assert result["evaluation"]["failures"] == ["preflight_failed"]
    assert result["evaluation"]["preflight_failures"] == [
        "builtin_gateway_not_external"
    ]


def test_preflight_timeout_is_sanitized(monkeypatch) -> None:
    monkeypatch.setattr(
        gateway_soak.subprocess,
        "run",
        Mock(side_effect=gateway_soak.subprocess.TimeoutExpired("secret-command", 5)),
    )

    result = _run_gateway_preflight(
        require_external_gateway=True,
        require_all_default_keys=True,
        timeout_seconds=5.0,
    )

    assert result == {
        "ok": False,
        "error_type": "TimeoutExpired",
        "failures": ["preflight_process_failed"],
    }


def test_running_checkpoint_is_explicitly_incomplete_and_sanitized() -> None:
    result = _build_soak_result(
        phase="running",
        started_at="2026-07-22T00:00:00+00:00",
        settings=SimpleNamespace(gateway_class="vendor:Gateway", gateway_name="SIM"),
        preflight={"ok": True},
        requested_duration=3600.0,
        observed_duration=120.0,
        sample_interval=5.0,
        startup_grace=30.0,
        startup_stability=10.0,
        sample_counts=gateway_soak.Counter({"connected": 24}),
        transitions=[{"elapsed_seconds": 0.0, "from": None, "to": "connected"}],
        event_counts=gateway_soak.Counter({"account": 2}),
        disconnect_injected=False,
        runtime_summary={
            "available": True,
            "connection_status": "connected",
            "auto_reconnect": {"attempt_count": 0, "success_count": 0},
        },
        required_events=["account"],
        min_connected_ratio=0.99,
        require_reconnect=False,
        disconnect_injection_required=False,
        interrupted=False,
    )

    assert result["phase"] == "running"
    assert result["checkpoint"] is True
    assert result["ended_at"] is None
    assert result["ok"] is False
    assert result["evaluation"]["failures"] == ["duration_incomplete"]
    assert result["event_counts"]["account"] == 2
    assert result["startup_stability_seconds"] == 10.0


def test_json_checkpoint_replaces_atomically(tmp_path) -> None:
    output_path = tmp_path / "nested" / "soak.json"
    _write_json_file({"phase": "running", "sample": 1}, output_path)
    _write_json_file({"phase": "completed", "sample": 2}, output_path)

    assert json.loads(output_path.read_text(encoding="utf-8")) == {
        "phase": "completed",
        "sample": 2,
    }
    assert list(output_path.parent.glob(".*.tmp")) == []
