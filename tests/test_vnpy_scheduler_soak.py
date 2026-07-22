# -*- coding: utf-8 -*-
"""Tests for the read-only vn.py paper scheduler soak evaluator."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import scripts.check_vnpy_scheduler_soak as scheduler_soak
from scripts.check_vnpy_scheduler_soak import evaluate_scheduler_soak, main


def test_evaluate_scheduler_soak_accepts_healthy_required_tasks() -> None:
    result = evaluate_scheduler_soak(
        duration_completed=True,
        interrupted=False,
        sample_count=100,
        successful_sample_count=100,
        loop_running_count=100,
        task_registration_counts={
            "vnpy_paper_auto_trade": 100,
            "vnpy_paper_auto_retry": 100,
        },
        terminal_counts={
            "vnpy_paper_auto_trade": 2,
            "vnpy_paper_auto_retry": 20,
        },
        failed_counts={},
        overlap_skip_counts={},
        required_tasks=["vnpy_paper_auto_trade", "vnpy_paper_auto_retry"],
        min_api_success_ratio=0.99,
        min_loop_running_ratio=0.99,
        min_task_registration_ratio=0.99,
        max_failed_count=0,
        max_overlap_skip_count=0,
    )

    assert result["ok"] is True
    assert result["api_success_ratio"] == 1.0
    assert result["loop_running_ratio"] == 1.0
    assert result["failures"] == []


def test_evaluate_scheduler_soak_reports_each_acceptance_failure() -> None:
    result = evaluate_scheduler_soak(
        duration_completed=False,
        interrupted=False,
        sample_count=10,
        successful_sample_count=8,
        loop_running_count=6,
        task_registration_counts={"vnpy_paper_auto_retry": 8},
        terminal_counts={"vnpy_paper_auto_retry": 1},
        failed_counts={"vnpy_paper_auto_retry": 2},
        overlap_skip_counts={"vnpy_paper_auto_retry": 1},
        required_tasks=["vnpy_paper_auto_trade", "vnpy_paper_auto_retry"],
        min_api_success_ratio=0.9,
        min_loop_running_ratio=0.9,
        min_task_registration_ratio=0.9,
        max_failed_count=1,
        max_overlap_skip_count=0,
    )

    assert result["ok"] is False
    assert result["failures"] == [
        "duration_incomplete",
        "api_success_ratio_below_threshold",
        "scheduler_loop_ratio_below_threshold",
        "required_tasks_not_registered",
        "required_task_terminal_runs_missing",
        "task_failures_above_threshold",
        "task_overlap_skips_above_threshold",
    ]
    assert result["missing_registrations"] == ["vnpy_paper_auto_trade"]
    assert result["missing_terminal_runs"] == ["vnpy_paper_auto_trade"]


def test_evaluate_scheduler_soak_marks_interruption_without_duration_duplicate() -> None:
    result = evaluate_scheduler_soak(
        duration_completed=False,
        interrupted=True,
        sample_count=1,
        successful_sample_count=1,
        loop_running_count=1,
        task_registration_counts={},
        terminal_counts={},
        failed_counts={},
        overlap_skip_counts={},
        required_tasks=[],
        min_api_success_ratio=1.0,
        min_loop_running_ratio=1.0,
        min_task_registration_ratio=1.0,
        max_failed_count=0,
        max_overlap_skip_count=0,
    )

    assert result["failures"] == ["interrupted"]


def test_evaluate_scheduler_soak_handles_missing_samples() -> None:
    result = evaluate_scheduler_soak(
        duration_completed=True,
        interrupted=False,
        sample_count=0,
        successful_sample_count=0,
        loop_running_count=0,
        task_registration_counts={},
        terminal_counts={},
        failed_counts={},
        overlap_skip_counts={},
        required_tasks=[],
        min_api_success_ratio=0.9,
        min_loop_running_ratio=0.9,
        min_task_registration_ratio=0.9,
        max_failed_count=0,
        max_overlap_skip_count=0,
    )

    assert result["ok"] is False
    assert result["failures"] == ["no_samples"]


def test_evaluate_scheduler_soak_rejects_intermittent_task_registration() -> None:
    result = evaluate_scheduler_soak(
        duration_completed=True,
        interrupted=False,
        sample_count=10,
        successful_sample_count=10,
        loop_running_count=10,
        task_registration_counts={"vnpy_paper_auto_retry": 8},
        terminal_counts={"vnpy_paper_auto_retry": 1},
        failed_counts={},
        overlap_skip_counts={},
        required_tasks=["vnpy_paper_auto_retry"],
        min_api_success_ratio=1.0,
        min_loop_running_ratio=1.0,
        min_task_registration_ratio=0.9,
        max_failed_count=0,
        max_overlap_skip_count=0,
    )

    assert result["ok"] is False
    assert result["failures"] == [
        "required_task_registration_ratio_below_threshold"
    ]
    assert result["task_registration_ratios"] == {
        "vnpy_paper_auto_retry": 0.8
    }


def test_scheduler_soak_cli_reads_http_and_excludes_baseline_failures(tmp_path) -> None:
    class _Handler(BaseHTTPRequestHandler):
        event_request_count = 0

        def do_GET(self):
            if self.path.startswith("/api/v1/vnpy-paper/status"):
                payload = {
                    "scheduler": {
                        "loop_running": True,
                        "background_tasks": [{"name": "vnpy_paper_auto_retry"}],
                    }
                }
            elif self.path.startswith("/api/v1/vnpy-paper/task-events"):
                type(self).event_request_count += 1
                items = [{
                    "timestamp": "2026-07-20T09:00:00",
                    "name": "vnpy_paper_auto_retry",
                    "status": "failed",
                    "message": "historical failure",
                    "details": {},
                }]
                if type(self).event_request_count > 1:
                    items.append({
                        "timestamp": "2026-07-20T09:01:00",
                        "name": "vnpy_paper_auto_retry",
                        "status": "completed",
                        "message": "current completion",
                        "details": {},
                    })
                payload = {"items": items}
            else:
                self.send_error(404)
                return
            encoded = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, _format, *_args):
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    output_path = tmp_path / "scheduler-soak.json"
    try:
        with patch.object(
            scheduler_soak,
            "_write_json_file",
            wraps=scheduler_soak._write_json_file,
        ) as write_json:
            exit_code = main([
                "--base-url",
                f"http://127.0.0.1:{server.server_port}",
                "--duration-seconds",
                "1",
                "--sample-interval-seconds",
                "0.1",
                "--request-timeout-seconds",
                "1",
                "--min-api-success-ratio",
                "1",
                "--min-loop-running-ratio",
                "1",
                "--min-task-registration-ratio",
                "1",
                "--require-task",
                "vnpy_paper_auto_retry",
                "--checkpoint-interval-seconds",
                "0.2",
                "--output-json",
                str(output_path),
            ])
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)

    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert exit_code == 0
    phases = [call.args[0]["phase"] for call in write_json.call_args_list]
    assert "running" in phases
    assert phases[-1] == "completed"
    assert report["phase"] == "completed"
    assert report["checkpoint"] is False
    assert report["evaluation"]["ok"] is True
    assert report["terminal_counts"] == {"vnpy_paper_auto_retry": 1}
    assert report["failed_counts"] == {}


def test_scheduler_soak_cli_persists_interrupted_terminal_state(
    tmp_path,
    monkeypatch,
) -> None:
    def read_json(_base_url, path, **_kwargs):
        if "/status" in path:
            return {"scheduler": {"loop_running": True, "background_tasks": []}}
        return {"items": []}

    output_path = tmp_path / "interrupted-scheduler.json"
    monkeypatch.setattr(scheduler_soak, "_read_json", read_json)
    monkeypatch.setattr(
        scheduler_soak.time,
        "sleep",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    exit_code = main([
        "--duration-seconds", "10",
        "--sample-interval-seconds", "1",
        "--output-json", str(output_path),
    ])

    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert exit_code == 130
    assert report["phase"] == "interrupted"
    assert report["checkpoint"] is False
    assert report["evaluation"]["failures"] == ["interrupted"]
