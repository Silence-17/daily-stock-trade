# -*- coding: utf-8 -*-
"""Tests for the online Agent to vn.py paper acceptance gate."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import URLError

import pytest

import scripts.check_online_agent_vnpy_e2e as e2e
from scripts.check_online_agent_vnpy_e2e import evaluate_acceptance, main


def _status(*, cash=100000.0, positions=None, runtime=False, time_gate=True):
    diagnostics = {}
    if runtime:
        diagnostics["vnpy_runtime"] = {
            "available": True,
            "gateway_class": (
                "src.services.vnpy_simulated_gateway:DsaSimulatedGateway"
            ),
            "gateway_name": "DSA_SIM",
            "connect": {"status": "connected", "connected": True},
            "event_bridge": {"registered": True, "registered_count": 4},
        }
    return {
        "account": {"id": 1},
        "settings": {"auto_trade_time_gate_enabled": time_gate},
        "snapshot": {
            "total_cash": cash,
            "accounts": [
                {
                    "account_id": 1,
                    "total_cash": cash,
                    "positions": positions or [],
                }
            ],
        },
        "diagnostics": diagnostics,
    }


def _decision(*, status="planned", trade_id=None):
    return {
        "id": 11,
        "symbol": "600000",
        "action": "buy",
        "status": status,
        "trade_id": trade_id,
        "strategy_evidence": {"summary": "selected by strategy"},
    }


def _plan(*, status="planned", trade_id=None):
    return {
        "id": 21,
        "decision_id": 11,
        "plan_uid": "plan-1",
        "symbol": "600000",
        "status": status,
        "trade_id": trade_id,
    }


def test_evaluate_acceptance_accepts_traceable_dry_run_without_mutation():
    status = _status()
    result = evaluate_acceptance(
        execution_mode="dry_run",
        run_response={"accepted": True, "agent_run_uid": "run-1"},
        run_detail={
            "run_uid": "run-1",
            "status": "completed",
            "candidate_count": 1,
            "decisions": [_decision()],
            "trade_plans": [_plan()],
        },
        before_status=status,
        after_status=status,
        min_candidates=1,
        max_failed_plans=0,
        require_execution=False,
        settings_restored=True,
    )

    assert result["ok"] is True
    assert result["failures"] == []
    assert result["cash_delta"] == 0
    assert result["position_deltas"] == {}


def test_evaluate_acceptance_validates_filled_vnpy_portfolio_change():
    before = _status(runtime=True)
    after = _status(
        cash=99000.0,
        positions=[{"symbol": "600000", "quantity": 100}],
        runtime=True,
    )
    result = evaluate_acceptance(
        execution_mode="vnpy_paper",
        run_response={"accepted": True, "agent_run_uid": "run-1"},
        run_detail={
            "run_uid": "run-1",
            "status": "completed",
            "candidate_count": 1,
            "decisions": [_decision(status="filled", trade_id=31)],
            "trade_plans": [_plan(status="filled", trade_id=31)],
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
        },
        before_status=before,
        after_status=after,
        min_candidates=1,
        max_failed_plans=0,
        require_execution=True,
        settings_restored=True,
    )

    assert result["ok"] is True
    assert result["filled_plan_count"] == 1
    assert result["cash_delta"] == -1000
    assert result["position_deltas"] == {"600000": 100}


def test_evaluate_acceptance_reports_execution_and_traceability_failures():
    before = _status(runtime=True)
    after = _status(runtime=True)
    result = evaluate_acceptance(
        execution_mode="vnpy_paper",
        run_response={"accepted": False, "agent_run_uid": "run-other"},
        run_detail={
            "run_uid": "run-1",
            "status": "failed",
            "candidate_count": 1,
            "decisions": [{"id": 11, "symbol": "600000"}],
            "trade_plans": [_plan(status="failed")],
        },
        before_status=before,
        after_status=after,
        min_candidates=1,
        max_failed_plans=0,
        require_execution=True,
        settings_restored=False,
    )

    assert result["ok"] is False
    assert result["failures"] == [
        "agent_run_not_accepted",
        "agent_run_identity_mismatch",
        "agent_run_not_completed",
        "candidate_decision_not_traceable",
        "failed_trade_plans_above_threshold",
        "settings_not_restored",
        "vnpy_fill_not_observed",
    ]


def test_cli_simulated_order_restores_time_gate_and_writes_evidence(tmp_path):
    before_status = _status(runtime=True)
    after_status = _status(
        cash=99000.0,
        positions=[{"symbol": "600000", "quantity": 100}],
        runtime=True,
        time_gate=True,
    )
    run_detail = {
        "run_uid": "run-1",
        "status": "completed",
        "candidate_count": 1,
        "planned_count": 0,
        "submitted_count": 1,
        "skipped_count": 0,
        "decisions": [_decision(status="filled", trade_id=31)],
        "trade_plans": [_plan(status="filled", trade_id=31)],
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

    class _Handler(BaseHTTPRequestHandler):
        status_reads = 0
        setting_updates = []

        def _write(self, payload):
            encoded = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self):
            if self.path.startswith("/api/v1/vnpy-paper/status"):
                type(self).status_reads += 1
                self._write(before_status if type(self).status_reads == 1 else after_status)
                return
            if self.path == "/api/v1/vnpy-paper/agent-runs/run-1":
                self._write(run_detail)
                return
            self.send_error(404)

        def do_POST(self):
            if self.path == "/api/v1/vnpy-paper/auto/run":
                self._write({"accepted": True, "agent_run_uid": "run-1"})
                return
            self.send_error(404)

        def do_PUT(self):
            if self.path != "/api/v1/vnpy-paper/settings":
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            type(self).setting_updates.append(payload)
            self._write({"accepted": True})

        def log_message(self, _format, *_args):
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    output_path = tmp_path / "online-agent-vnpy-e2e.json"
    try:
        exit_code = main(
            [
                "--base-url",
                f"http://127.0.0.1:{server.server_port}",
                "--execution-mode",
                "vnpy_paper",
                "--allow-simulated-orders",
                "--temporarily-disable-time-gate",
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
    assert report["ok"] is True
    assert report["evaluation"]["filled_plan_count"] == 1
    assert _Handler.setting_updates == [
        {"auto_trade_time_gate_enabled": False},
        {"auto_trade_time_gate_enabled": True},
    ]


def test_cli_requires_explicit_order_permission():
    with pytest.raises(SystemExit) as exc_info:
        main(["--execution-mode", "vnpy_paper"])

    assert exc_info.value.code == 2


def test_cli_restores_time_gate_when_disable_response_is_uncertain(
    monkeypatch, tmp_path
):
    updates = []
    status_reads = 0

    def fake_request(_base_url, path, *, method="GET", payload=None, **_kwargs):
        nonlocal status_reads
        if path.startswith("/api/v1/vnpy-paper/status"):
            status_reads += 1
            return _status(runtime=True, time_gate=True)
        if method == "PUT":
            updates.append(payload)
            if payload == {"auto_trade_time_gate_enabled": False}:
                raise URLError("response lost after apply")
            return {"accepted": True}
        raise AssertionError(f"unexpected request: {method} {path}")

    monkeypatch.setattr(e2e, "_request_json", fake_request)
    output_path = tmp_path / "uncertain-disable.json"
    exit_code = main(
        [
            "--execution-mode",
            "vnpy_paper",
            "--allow-simulated-orders",
            "--temporarily-disable-time-gate",
            "--output-json",
            str(output_path),
        ]
    )

    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert exit_code == 1
    assert status_reads == 2
    assert updates == [
        {"auto_trade_time_gate_enabled": False},
        {"auto_trade_time_gate_enabled": True},
    ]
    assert report["evaluation"]["settings_restored"] is True
    assert report["evaluation"]["failures"][0] == "acceptance_runtime_error"
