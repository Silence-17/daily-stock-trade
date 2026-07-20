# -*- coding: utf-8 -*-
"""Tests for the read-only deployed vn.py runtime soak gate."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from scripts.check_vnpy_deployed_runtime_soak import (
    EVENT_TYPES,
    evaluate_deployed_runtime_soak,
    main,
)


def _evaluate(**overrides):
    values = {
        "duration_completed": True,
        "interrupted": False,
        "sample_count": 100,
        "successful_sample_count": 100,
        "runtime_ready_count": 100,
        "connected_count": 100,
        "event_bridge_registered_count": 100,
        "event_type_counts": {event_type: 100 for event_type in EVENT_TYPES.values()},
        "required_event_types": EVENT_TYPES.values(),
        "backend_identity_observation_count": 100,
        "process_change_count": 0,
        "gateway_identity_observation_count": 100,
        "gateway_change_count": 0,
        "incompatible_contract_count": 0,
        "reconnect_attempt_count": 1,
        "reconnect_success_count": 1,
        "reconnect_failure_count": 0,
        "reconnect_counter_regression_count": 0,
        "min_api_success_ratio": 0.99,
        "min_runtime_ready_ratio": 0.99,
        "min_connected_ratio": 0.99,
        "min_event_bridge_ratio": 0.99,
        "max_process_changes": 0,
        "max_gateway_changes": 0,
        "min_reconnect_success_count": 1,
        "max_reconnect_failure_count": 0,
    }
    values.update(overrides)
    return evaluate_deployed_runtime_soak(**values)


def test_evaluate_deployed_runtime_soak_accepts_healthy_runtime() -> None:
    result = _evaluate()

    assert result["ok"] is True
    assert result["failures"] == []
    assert result["connected_ratio"] == 1.0
    assert result["event_bridge_registered_ratio"] == 1.0


def test_evaluate_deployed_runtime_soak_reports_full_failure_matrix() -> None:
    result = _evaluate(
        duration_completed=False,
        successful_sample_count=80,
        runtime_ready_count=60,
        connected_count=0,
        event_bridge_registered_count=60,
        event_type_counts={EVENT_TYPES["order"]: 60},
        backend_identity_observation_count=79,
        process_change_count=2,
        gateway_identity_observation_count=79,
        gateway_change_count=1,
        incompatible_contract_count=1,
        reconnect_success_count=0,
        reconnect_failure_count=2,
        reconnect_counter_regression_count=1,
    )

    assert result["ok"] is False
    assert result["failures"] == [
        "duration_incomplete",
        "api_success_ratio_below_threshold",
        "runtime_ready_ratio_below_threshold",
        "connection_never_confirmed",
        "event_bridge_ratio_below_threshold",
        "required_event_types_missing",
        "backend_identity_missing",
        "gateway_identity_missing",
        "contract_incompatible",
        "process_changes_above_threshold",
        "gateway_changes_above_threshold",
        "reconnect_counters_regressed",
        "reconnect_successes_below_threshold",
        "reconnect_failures_above_threshold",
    ]


def test_evaluate_deployed_runtime_soak_rejects_intermittent_callback() -> None:
    counts = {event_type: 100 for event_type in EVENT_TYPES.values()}
    counts[EVENT_TYPES["position"]] = 98

    result = _evaluate(event_type_counts=counts)

    assert result["ok"] is False
    assert result["failures"] == ["required_event_type_ratio_below_threshold"]
    assert result["intermittent_event_types"] == [EVENT_TYPES["position"]]


def test_deployed_runtime_soak_cli_reads_live_contract(tmp_path) -> None:
    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if not self.path.startswith("/api/v1/vnpy-paper/status"):
                self.send_error(404)
                return
            payload = {
                "diagnostics": {
                    "backend": {
                        "api_version": "1.0",
                        "vnpy_paper_contract_version": 3,
                        "build_id": "test-build",
                        "python_version": "3.13",
                        "process_started_at": "2026-07-20T10:00:00+00:00",
                    },
                    "vnpy_runtime": {
                        "available": True,
                        "mode": "vnpy_runtime",
                        "event_engine_created": True,
                        "main_engine_created": True,
                        "gateway_class": "example:Gateway",
                        "gateway_name": "PAPER",
                        "gateway": {"added": True},
                        "connect": {"connected": True, "status": "connected"},
                        "event_bridge": {
                            "registered": True,
                            "registered_count": 4,
                            "event_types": list(EVENT_TYPES.values()),
                        },
                        "auto_reconnect": {
                            "attempt_count": 0,
                            "success_count": 0,
                            "failure_count": 0,
                        },
                    },
                }
            }
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
    output_path = tmp_path / "nested" / "runtime-soak.json"
    try:
        exit_code = main([
            "--base-url",
            f"http://127.0.0.1:{server.server_port}",
            "--duration-seconds",
            "1",
            "--sample-interval-seconds",
            "0.1",
            "--request-timeout-seconds",
            "1",
            "--output-json",
            str(output_path),
        ])
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)

    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert report["evaluation"]["ok"] is True
    assert report["gateway"] == {
        "added": True,
        "class": "example:Gateway",
        "name": "PAPER",
    }
    assert report["backend"]["build_id"] == "test-build"
