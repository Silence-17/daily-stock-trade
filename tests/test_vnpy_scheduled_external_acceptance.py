import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import sys
import threading

from scripts.check_vnpy_scheduled_external_acceptance import (
    evaluate_external_scheduled_acceptance,
    main,
)


def _status(*, cash=100000.0, positions=None, handler_failures=0):
    return {
        "settings": {
            "enabled": True,
            "auto_trade_enabled": True,
            "auto_execution_mode": "vnpy_paper",
            "auto_trade_time_gate_enabled": True,
            "auto_market": "cn",
            "auto_strategy": "dual_low",
            "account_id": 8,
            "vnpy_gateway_name": "XTP",
        },
        "account": {"id": 8},
        "snapshot": {
            "accounts": [
                {
                    "account_id": 8,
                    "total_cash": cash,
                    "positions": positions or [],
                }
            ]
        },
        "scheduler": {
            "loop_running": True,
            "background_tasks": [
                {"name": "vnpy_paper_auto_trade", "running": False}
            ],
        },
        "diagnostics": {
            "vnpy_runtime": {
                "available": True,
                "gateway_class": "vnpy_xtp:XtpGateway",
                "gateway_name": "XTP",
                "connect": {"status": "connected", "connected": True},
                "event_bridge": {
                    "registered": True,
                    "registered_count": 4,
                    "handler_failure_count": handler_failures,
                },
                "production_preflight": {"ok": True, "external_gateway": True},
            }
        },
    }


def _run_detail():
    return {
        "run_uid": "scheduled-run",
        "trigger_source": "vnpy_paper_auto",
        "status": "completed",
        "candidate_count": 1,
        "planned_count": 0,
        "submitted_count": 1,
        "skipped_count": 0,
        "decisions": [
            {
                "id": 11,
                "symbol": "600000",
                "action": "buy",
                "status": "filled",
                "reason": "filled",
                "trade_id": 31,
            }
        ],
        "trade_plans": [
            {
                "id": 21,
                "decision_id": 11,
                "symbol": "600000",
                "status": "filled",
                "trade_id": 31,
            }
        ],
        "portfolio_change": {
            "booked_plan_count": 1,
            "items": [
                {
                    "symbol": "600000",
                    "net_quantity": 100,
                    "net_cash_flow": -1000,
                }
            ],
        },
    }


def _event():
    return {
        "name": "vnpy_paper_auto_trade",
        "status": "completed",
        "details": {"agent_run_uid": "scheduled-run"},
    }


def test_evaluate_external_scheduled_acceptance_proves_correlated_fill():
    result = evaluate_external_scheduled_acceptance(
        run_uid="scheduled-run",
        run_detail=_run_detail(),
        scheduler_event=_event(),
        before_status=_status(),
        after_status=_status(
            cash=99000.0,
            positions=[{"symbol": "600000", "quantity": 100}],
        ),
        min_candidates=1,
        max_failed_plans=0,
    )

    assert result["ok"] is True
    assert result["filled_plan_count"] == 1
    assert result["cash_delta"] == -1000
    assert result["position_deltas"] == {"600000": 100.0}
    assert result["scheduler_event_correlated"] is True
    assert result["external_gateway_preflight_ok"] is True


def test_script_entrypoint_can_load_shared_verifier():
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "check_vnpy_scheduled_external_acceptance.py"
    )
    completed = subprocess.run(
        [sys.executable, str(script), "--help"],
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 0
    assert "Observe one scheduled Agent run" in completed.stdout


def test_evaluate_external_scheduled_acceptance_rejects_simulated_gateway_and_handler_failure():
    before = _status()
    after = _status(
        cash=99000.0,
        positions=[{"symbol": "600000", "quantity": 100}],
        handler_failures=1,
    )
    after["diagnostics"]["vnpy_runtime"]["gateway_class"] = (
        "src.services.vnpy_runtime:DsaSimulatedGateway"
    )
    result = evaluate_external_scheduled_acceptance(
        run_uid="scheduled-run",
        run_detail=_run_detail(),
        scheduler_event=_event(),
        before_status=before,
        after_status=after,
        min_candidates=1,
        max_failed_plans=0,
    )

    assert result["ok"] is False
    assert "external_gateway_not_observed" in result["failures"]
    assert "event_handler_failure_observed" in result["failures"]


def test_cli_observes_scheduler_fill_using_get_requests_only(tmp_path):
    before = _status()
    after = _status(
        cash=99000.0,
        positions=[{"symbol": "600000", "quantity": 100}],
    )

    class Handler(BaseHTTPRequestHandler):
        run_list_reads = 0
        methods = []

        def _write(self, payload):
            encoded = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self):
            type(self).methods.append("GET")
            if self.path.startswith("/api/v1/vnpy-paper/status"):
                self._write(before if type(self).run_list_reads < 2 else after)
                return
            if self.path.startswith("/api/v1/vnpy-paper/agent-runs?"):
                type(self).run_list_reads += 1
                items = [{"run_uid": "old-run"}]
                if type(self).run_list_reads >= 2:
                    items.insert(0, {"run_uid": "scheduled-run"})
                self._write({"items": items})
                return
            if self.path == "/api/v1/vnpy-paper/agent-runs/scheduled-run":
                self._write(_run_detail())
                return
            if self.path.startswith("/api/v1/vnpy-paper/task-events?"):
                self._write({"items": [_event()]})
                return
            self.send_error(404)

        def do_POST(self):
            type(self).methods.append("POST")
            self.send_error(405)

        def do_PUT(self):
            type(self).methods.append("PUT")
            self.send_error(405)

        def log_message(self, _format, *_args):
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    output_path = tmp_path / "external-scheduled-fill.json"
    try:
        exit_code = main(
            [
                "--base-url",
                f"http://127.0.0.1:{server.server_port}",
                "--timeout-seconds",
                "2",
                "--poll-interval-seconds",
                "0.1",
                "--request-timeout-seconds",
                "1",
                "--output-json",
                str(output_path),
            ]
        )
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)

    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert report["phase"] == "completed"
    assert report["read_only"] is True
    assert report["places_orders"] is False
    assert report["evaluation"]["filled_plan_count"] == 1
    assert Handler.methods and set(Handler.methods) == {"GET"}
