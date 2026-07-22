"""Tests for the unified online Agent production acceptance bundle."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import scripts.check_online_agent_production_acceptance as acceptance


def _preflight(*, ok: bool = True) -> dict:
    return {
        "ok": ok,
        "external_gateway": ok,
        "production_preflight_enabled": ok,
        "connect_attempted": False,
        "orders_created": False,
    }


def _gate_report(*, ok: bool = True) -> dict:
    return {"phase": "completed", "evaluation": {"ok": ok, "failures": []}}


def _calibration_report(*, ok: bool = True) -> dict:
    return {
        "evaluation": {"ok": ok, "failures": []},
        "methodology": {
            "read_only": True,
            "creates_agent_runs": False,
            "places_orders": False,
        },
    }


def _flag_value(command: list[str], flag: str) -> str:
    return command[command.index(flag) + 1]


def test_evaluate_accepts_external_read_only_evidence() -> None:
    result = acceptance.evaluate_production_acceptance(
        preflight=_preflight(),
        runtime=_gate_report(),
        scheduler=_gate_report(),
        calibration=_calibration_report(),
        stage_exit_codes={"runtime": 0, "scheduler": 0, "calibration": 0},
    )

    assert result["ok"] is True
    assert result["production_ready"] is True
    assert result["failures"] == []


def test_evaluate_rejects_builtin_failed_and_unsafe_evidence() -> None:
    calibration = _calibration_report(ok=False)
    calibration["methodology"]["places_orders"] = True
    result = acceptance.evaluate_production_acceptance(
        preflight={
            "ok": False,
            "external_gateway": False,
            "connect_attempted": True,
            "orders_created": True,
        },
        runtime={},
        scheduler=_gate_report(ok=False),
        calibration=calibration,
        stage_exit_codes={"runtime": 1, "scheduler": 1, "calibration": 1},
    )

    assert result["ok"] is False
    assert result["production_ready"] is False
    assert "external_gateway_not_confirmed" in result["failures"]
    assert "production_preflight_not_enabled" in result["failures"]
    assert "preflight_connected_unexpectedly" in result["failures"]
    assert "preflight_created_orders" in result["failures"]
    assert "scheduler_gate_failed" in result["failures"]
    assert "calibration_placed_orders" in result["failures"]


def test_evaluate_rejects_nonterminal_soak_checkpoint() -> None:
    runtime = _gate_report()
    runtime["phase"] = "running"
    result = acceptance.evaluate_production_acceptance(
        preflight=_preflight(),
        runtime=runtime,
        scheduler=_gate_report(),
        calibration=_calibration_report(),
        stage_exit_codes={"runtime": None, "scheduler": 0, "calibration": 0},
    )

    assert result["production_ready"] is False
    assert "runtime_not_completed" in result["failures"]


def test_main_stops_after_failed_zero_connect_preflight(monkeypatch, tmp_path: Path) -> None:
    output_path = tmp_path / "acceptance.json"
    monkeypatch.setattr(acceptance, "_read_json_url", lambda *_args, **_kwargs: _preflight(ok=False))

    def fail_if_started(*_args, **_kwargs):
        raise AssertionError("soaks must not start after a failed preflight")

    monkeypatch.setattr(acceptance, "_run_parallel_soaks", fail_if_started)

    exit_code = acceptance.main(["--output-json", str(output_path)])
    report = json.loads(output_path.read_text(encoding="utf-8"))

    assert exit_code == 1
    assert report["phase"] == "preflight_failed"
    assert report["evaluation"]["production_ready"] is False
    assert report["methodology"]["connects_gateway"] is False


def test_main_bundles_parallel_soaks_and_calibration(monkeypatch, tmp_path: Path) -> None:
    output_path = tmp_path / "acceptance.json"
    captured_commands: dict[str, list[str]] = {}
    monkeypatch.setattr(acceptance, "_read_json_url", lambda *_args, **_kwargs: _preflight())

    def run_parallel(commands, *, checkpoint, poll_interval_seconds=1.0):
        del poll_interval_seconds
        for name, raw_command in commands.items():
            command = list(raw_command)
            captured_commands[name] = command
            acceptance._write_json_file(
                _gate_report(),
                Path(_flag_value(command, "--output-json")),
            )
        checkpoint({"runtime": 0, "scheduler": 0})
        return {"runtime": 0, "scheduler": 0}, False

    def run_stage(raw_command):
        command = list(raw_command)
        captured_commands["calibration"] = command
        acceptance._write_json_file(
            _calibration_report(),
            Path(_flag_value(command, "--output-json")),
        )
        return 0

    monkeypatch.setattr(acceptance, "_run_parallel_soaks", run_parallel)
    monkeypatch.setattr(acceptance, "_run_stage", run_stage)

    exit_code = acceptance.main([
        "--duration-seconds", "1",
        "--sample-interval-seconds", "0.1",
        "--checkpoint-interval-seconds", "0",
        "--expected-gateway-name", "SIMNOW",
        "--output-json", str(output_path),
    ])
    report = json.loads(output_path.read_text(encoding="utf-8"))

    assert exit_code == 0
    assert report["phase"] == "completed"
    assert report["evaluation"]["production_ready"] is True
    assert report["methodology"] == {
        "read_only": True,
        "creates_agent_runs": False,
        "places_orders": False,
        "connects_gateway": False,
        "parallel_observation": True,
        "external_gateway_required": True,
    }
    runtime_command = captured_commands["runtime"]
    assert "--require-external-gateway" in runtime_command
    assert _flag_value(runtime_command, "--expected-gateway-name") == "SIMNOW"
    assert runtime_command.count("--require-observed-event") == 2
    assert captured_commands["scheduler"].count("--require-task") == 4
    assert captured_commands["calibration"].count("--market") == 3
    assert _flag_value(captured_commands["calibration"], "--required-version") == (
        "candidate-return-risk-v1"
    )


def test_main_removes_stale_stage_reports_before_preflight(monkeypatch, tmp_path: Path) -> None:
    output_path = tmp_path / "acceptance.json"
    stage_dir = output_path.with_name(f".{output_path.stem}.stages")
    stage_dir.mkdir()
    for stage_name in ("runtime", "scheduler", "calibration"):
        (stage_dir / f"{stage_name}.json").write_text('{"stale": true}', encoding="utf-8")
    monkeypatch.setattr(acceptance, "_read_json_url", lambda *_args, **_kwargs: _preflight(ok=False))

    exit_code = acceptance.main(["--output-json", str(output_path)])
    report = json.loads(output_path.read_text(encoding="utf-8"))

    assert exit_code == 1
    assert report["stages"] == {"runtime": {}, "scheduler": {}, "calibration": {}}


def test_main_preserves_interrupted_terminal_evidence(monkeypatch, tmp_path: Path) -> None:
    output_path = tmp_path / "acceptance.json"
    monkeypatch.setattr(acceptance, "_read_json_url", lambda *_args, **_kwargs: _preflight())

    def interrupt(commands, *, checkpoint, poll_interval_seconds=1.0):
        del commands, poll_interval_seconds
        checkpoint({"runtime": None, "scheduler": None})
        return {"runtime": 130, "scheduler": -15}, True

    monkeypatch.setattr(acceptance, "_run_parallel_soaks", interrupt)

    exit_code = acceptance.main([
        "--duration-seconds", "1",
        "--output-json", str(output_path),
    ])
    report = json.loads(output_path.read_text(encoding="utf-8"))

    assert exit_code == 130
    assert report["phase"] == "interrupted"
    assert report["checkpoint"] is False
    assert "acceptance_interrupted" in report["evaluation"]["failures"]
    assert report["evaluation"]["stage_exit_codes"]["runtime"] == 130


def test_main_preserves_calibration_interruption(monkeypatch, tmp_path: Path) -> None:
    output_path = tmp_path / "acceptance.json"
    monkeypatch.setattr(acceptance, "_read_json_url", lambda *_args, **_kwargs: _preflight())

    def complete_soaks(commands, *, checkpoint, poll_interval_seconds=1.0):
        del poll_interval_seconds
        for command in commands.values():
            acceptance._write_json_file(
                _gate_report(),
                Path(_flag_value(list(command), "--output-json")),
            )
        checkpoint({"runtime": 0, "scheduler": 0})
        return {"runtime": 0, "scheduler": 0}, False

    monkeypatch.setattr(acceptance, "_run_parallel_soaks", complete_soaks)
    monkeypatch.setattr(
        acceptance,
        "_run_stage",
        lambda _command: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    exit_code = acceptance.main([
        "--duration-seconds", "1",
        "--output-json", str(output_path),
    ])
    report = json.loads(output_path.read_text(encoding="utf-8"))

    assert exit_code == 130
    assert report["phase"] == "interrupted"
    assert report["evaluation"]["stage_exit_codes"]["calibration"] == 130
    assert "acceptance_interrupted" in report["evaluation"]["failures"]


def test_parallel_soak_start_failure_stops_started_process(monkeypatch) -> None:
    class StartedProcess:
        returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = -15

        def wait(self, timeout):
            del timeout
            return self.returncode

    started = StartedProcess()
    calls = 0

    def popen(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return started
        raise OSError("sanitized by stage exit code")

    checkpoints = []
    monkeypatch.setattr(acceptance.subprocess, "Popen", popen)

    exit_codes, interrupted = acceptance._run_parallel_soaks(
        {"runtime": ["runtime"], "scheduler": ["scheduler"]},
        checkpoint=lambda payload: checkpoints.append(dict(payload)),
    )

    assert interrupted is False
    assert exit_codes == {"runtime": -15, "scheduler": 127}
    assert checkpoints == [exit_codes]


def test_cli_can_run_directly() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/check_online_agent_production_acceptance.py", "--help"],
        cwd=acceptance.ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "Bundle read-only production evidence" in result.stdout
