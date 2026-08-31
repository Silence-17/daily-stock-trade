# -*- coding: utf-8 -*-
"""Tests for the online Agent to vn.py paper acceptance gate."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import URLError

import pytest

import scripts.check_online_agent_vnpy_e2e as e2e
from scripts.check_online_agent_vnpy_e2e import (
    _run_is_terminal_and_coherent,
    evaluate_acceptance,
    main,
)


def _status(
    *,
    account_id=1,
    cash=100000.0,
    positions=None,
    runtime=False,
    time_gate=True,
    auto_trade=False,
    auto_interval=1440,
    execution_mode="paper",
    strategy="dual_low",
):
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
        "account": {"id": account_id},
        "settings": {
            "account_id": account_id,
            "auto_trade_enabled": auto_trade,
            "auto_trade_time_gate_enabled": time_gate,
            "auto_interval_minutes": auto_interval,
            "auto_execution_mode": execution_mode,
            "auto_strategy": strategy,
        },
        "snapshot": {
            "total_cash": cash,
            "accounts": [
                {
                    "account_id": account_id,
                    "total_cash": cash,
                    "positions": positions or [],
                }
            ],
        },
        "diagnostics": diagnostics,
    }


def test_scheduler_task_name_uses_cross_market_entry_watch_for_vnpy_mode():
    status = _status(
        execution_mode="paper",
        strategy=e2e.CROSS_MARKET_STRATEGY_ID,
    )

    assert e2e._scheduler_execution_task_name(
        status,
        execution_mode_override="vnpy_paper",
    ) == "cross_market_intraday_entry_scan"
    assert e2e._scheduler_execution_task_name(status) == "vnpy_paper_auto_trade"


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


def test_terminal_run_waits_for_linked_decision_to_match_plan():
    detail = {
        "decisions": [_decision(status="submitted")],
        "trade_plans": [_plan(status="filled", trade_id=31)],
    }

    assert _run_is_terminal_and_coherent("vnpy_paper", detail) is False
    detail["decisions"][0]["status"] = "filled"
    detail["decisions"][0]["trade_id"] = 31
    assert _run_is_terminal_and_coherent("vnpy_paper", detail) is True


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


def test_cli_isolated_account_books_fill_then_restores_and_cleans_up(
    monkeypatch, tmp_path
):
    state = {
        "current_account_id": 1,
        "auto_trade": True,
        "time_gate": True,
        "created": False,
        "filled": False,
        "cleaned": False,
    }
    calls = []

    def current_status():
        if state["current_account_id"] == 2:
            return _status(
                account_id=2,
                cash=99000.0 if state["filled"] else 100000.0,
                positions=(
                    [{"symbol": "600000", "quantity": 100}]
                    if state["filled"]
                    else []
                ),
                runtime=True,
                time_gate=state["time_gate"],
                auto_trade=state["auto_trade"],
            )
        return _status(
            account_id=1,
            cash=50000.0,
            runtime=True,
            time_gate=state["time_gate"],
            auto_trade=state["auto_trade"],
        )

    def fake_request(_base_url, path, *, method="GET", payload=None, **_kwargs):
        calls.append((method, path, payload))
        if path.startswith("/api/v1/vnpy-paper/status"):
            return current_status()
        if path.startswith("/api/v1/vnpy-paper/accounts?"):
            items = [{"id": 1}]
            if state["created"]:
                items.append({"id": 2})
            return {"items": items, "current_account_id": state["current_account_id"]}
        if method == "PUT" and path == "/api/v1/vnpy-paper/settings":
            if "auto_trade_enabled" in payload:
                state["auto_trade"] = payload["auto_trade_enabled"]
            if "auto_trade_time_gate_enabled" in payload:
                state["time_gate"] = payload["auto_trade_time_gate_enabled"]
            return current_status()
        if method == "POST" and path.startswith("/api/v1/vnpy-paper/account/reset"):
            state["created"] = True
            state["current_account_id"] = 2
            return current_status()
        if method == "POST" and path == "/api/v1/vnpy-paper/auto/run":
            state["filled"] = True
            return {"accepted": True, "agent_run_uid": "run-1"}
        if path == "/api/v1/vnpy-paper/agent-runs/run-1":
            return {
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
        if method == "POST" and path.startswith(
            "/api/v1/vnpy-paper/accounts/1/restore"
        ):
            state["current_account_id"] = 1
            return current_status()
        if method == "POST" and path == "/api/v1/vnpy-paper/accounts/archived/cleanup":
            state["cleaned"] = True
            return {"cleaned_account_ids": [2]}
        raise AssertionError(f"unexpected request: {method} {path}")

    monkeypatch.setattr(e2e, "_request_json", fake_request)
    output_path = tmp_path / "isolated-fill.json"
    exit_code = main(
        [
            "--execution-mode",
            "vnpy_paper",
            "--allow-simulated-orders",
            "--temporarily-disable-time-gate",
            "--isolated-account",
            "--output-json",
            str(output_path),
        ]
    )

    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert report["evaluation"]["filled_plan_count"] == 1
    assert report["evaluation"]["cash_delta"] == -1000
    assert report["isolated_account"] == {
        "enabled": True,
        "original_account_id": 1,
        "created_account_ids": [2],
        "restored": True,
        "cleanup_ok": True,
    }
    assert state == {
        "current_account_id": 1,
        "auto_trade": True,
        "time_gate": True,
        "created": True,
        "filled": True,
        "cleaned": True,
    }
    assert any(path.startswith("/api/v1/vnpy-paper/account/reset") for _, path, _ in calls)


def test_cli_isolated_account_recovers_when_reset_response_is_uncertain(
    monkeypatch, tmp_path
):
    state = {"current_account_id": 1, "auto_trade": True, "created": False}

    def status():
        return _status(
            account_id=state["current_account_id"],
            runtime=True,
            auto_trade=state["auto_trade"],
        )

    def fake_request(_base_url, path, *, method="GET", payload=None, **_kwargs):
        if path.startswith("/api/v1/vnpy-paper/status"):
            return status()
        if path.startswith("/api/v1/vnpy-paper/accounts?"):
            items = [{"id": 1}, *([{"id": 2}] if state["created"] else [])]
            return {"items": items, "current_account_id": state["current_account_id"]}
        if method == "PUT" and path == "/api/v1/vnpy-paper/settings":
            if "auto_trade_enabled" in payload:
                state["auto_trade"] = payload["auto_trade_enabled"]
            return status()
        if method == "POST" and path.startswith("/api/v1/vnpy-paper/account/reset"):
            state["created"] = True
            state["current_account_id"] = 2
            raise URLError("response lost after account creation")
        if method == "POST" and path.startswith(
            "/api/v1/vnpy-paper/accounts/1/restore"
        ):
            state["current_account_id"] = 1
            return status()
        if method == "POST" and path == "/api/v1/vnpy-paper/accounts/archived/cleanup":
            return {"cleaned_account_ids": [2]}
        raise AssertionError(f"unexpected request: {method} {path}")

    monkeypatch.setattr(e2e, "_request_json", fake_request)
    output_path = tmp_path / "uncertain-reset.json"
    exit_code = main(
        [
            "--execution-mode",
            "vnpy_paper",
            "--allow-simulated-orders",
            "--isolated-account",
            "--output-json",
            str(output_path),
        ]
    )

    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert exit_code == 1
    assert state["current_account_id"] == 1
    assert state["auto_trade"] is True
    assert report["isolated_account"]["created_account_ids"] == [2]
    assert report["isolated_account"]["restored"] is True
    assert report["isolated_account"]["cleanup_ok"] is True
    assert report["evaluation"]["failures"][0] == "acceptance_runtime_error"


def test_cli_scheduler_mode_correlates_new_run_without_direct_trigger(
    monkeypatch, tmp_path
):
    state = {
        "current_account_id": 1,
        "auto_trade": False,
        "time_gate": True,
        "auto_interval": 1440,
        "execution_mode": "paper",
        "created": False,
        "new_run_visible": False,
        "filled": False,
    }
    calls = []

    def status():
        isolated = state["current_account_id"] == 2
        return _status(
            account_id=state["current_account_id"],
            cash=99000.0 if isolated and state["filled"] else 100000.0,
            positions=(
                [{"symbol": "600000", "quantity": 100}]
                if isolated and state["filled"]
                else []
            ),
            runtime=True,
            time_gate=state["time_gate"],
            auto_trade=state["auto_trade"],
            auto_interval=state["auto_interval"],
            execution_mode=state["execution_mode"],
        )

    def fake_request(_base_url, path, *, method="GET", payload=None, **_kwargs):
        calls.append((method, path, payload))
        if path.startswith("/api/v1/vnpy-paper/status"):
            return status()
        if path.startswith("/api/v1/vnpy-paper/accounts?"):
            items = [{"id": 1}, *([{"id": 2}] if state["created"] else [])]
            return {"items": items, "current_account_id": state["current_account_id"]}
        if method == "POST" and path.startswith("/api/v1/vnpy-paper/account/reset"):
            state["created"] = True
            state["current_account_id"] = 2
            return status()
        if method == "PUT" and path == "/api/v1/vnpy-paper/settings":
            if "auto_trade_enabled" in payload:
                state["auto_trade"] = payload["auto_trade_enabled"]
            if "auto_trade_time_gate_enabled" in payload:
                state["time_gate"] = payload["auto_trade_time_gate_enabled"]
            if "auto_interval_minutes" in payload:
                state["auto_interval"] = payload["auto_interval_minutes"]
            if "auto_execution_mode" in payload:
                state["execution_mode"] = payload["auto_execution_mode"]
            if payload.get("auto_trade_enabled") is True:
                state["new_run_visible"] = True
            return status()
        if path.startswith("/api/v1/vnpy-paper/agent-runs?"):
            items = [{"run_uid": "old-run"}]
            if state["new_run_visible"]:
                items.insert(0, {"run_uid": "scheduled-run"})
            return {"items": items, "limit": 100, "offset": 0, "total": len(items)}
        if path == "/api/v1/vnpy-paper/agent-runs/scheduled-run":
            state["filled"] = True
            return {
                "run_uid": "scheduled-run",
                "status": "completed",
                "candidate_count": 1,
                "submitted_count": 1,
                "decisions": [_decision(status="filled", trade_id=31)],
                "trade_plans": [_plan(status="filled", trade_id=31)],
                "portfolio_change": {
                    "booked_plan_count": 1,
                    "items": [{
                        "symbol": "600000",
                        "net_quantity": 100,
                        "net_cash_flow": -1000,
                    }],
                },
            }
        if path.startswith("/api/v1/vnpy-paper/task-events?"):
            return {
                "items": [{
                    "name": "vnpy_paper_auto_trade",
                    "status": "completed",
                    "details": {"agent_run_uid": "scheduled-run"},
                }]
            }
        if method == "POST" and path.startswith(
            "/api/v1/vnpy-paper/accounts/1/restore"
        ):
            state["current_account_id"] = 1
            return status()
        if method == "POST" and path == "/api/v1/vnpy-paper/accounts/archived/cleanup":
            return {"cleaned_account_ids": [2]}
        raise AssertionError(f"unexpected request: {method} {path}")

    monkeypatch.setattr(e2e, "_request_json", fake_request)
    output_path = tmp_path / "scheduled-fill.json"
    exit_code = main([
        "--execution-mode", "vnpy_paper",
        "--trigger-mode", "scheduler",
        "--allow-simulated-orders",
        "--isolated-account",
        "--output-json", str(output_path),
    ])

    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert report["trigger_mode"] == "scheduler"
    assert report["run"]["run_uid"] == "scheduled-run"
    assert report["scheduler_event"]["details"]["agent_run_uid"] == "scheduled-run"
    assert report["evaluation"]["filled_plan_count"] == 1
    assert not any(path == "/api/v1/vnpy-paper/auto/run" for _, path, _ in calls)
    assert state["current_account_id"] == 1
    assert state["auto_trade"] is False
    assert state["time_gate"] is True
    assert state["auto_interval"] == 1440
    assert state["execution_mode"] == "paper"


def test_cli_scheduler_mode_restores_settings_when_enable_response_is_lost(
    monkeypatch, tmp_path
):
    state = {
        "account": 1,
        "auto": False,
        "gate": True,
        "interval": 1440,
        "mode": "paper",
        "created": False,
    }
    updates = []

    def status():
        return _status(
            account_id=state["account"],
            runtime=True,
            auto_trade=state["auto"],
            time_gate=state["gate"],
            auto_interval=state["interval"],
            execution_mode=state["mode"],
        )

    def fake_request(_base_url, path, *, method="GET", payload=None, **_kwargs):
        if path.startswith("/api/v1/vnpy-paper/status"):
            return status()
        if path.startswith("/api/v1/vnpy-paper/accounts?"):
            items = [{"id": 1}, *([{"id": 2}] if state["created"] else [])]
            return {"items": items, "current_account_id": state["account"]}
        if method == "POST" and path.startswith("/api/v1/vnpy-paper/account/reset"):
            state["created"] = True
            state["account"] = 2
            return status()
        if path.startswith("/api/v1/vnpy-paper/agent-runs?"):
            return {"items": []}
        if method == "PUT" and path == "/api/v1/vnpy-paper/settings":
            updates.append(dict(payload))
            state["auto"] = payload.get("auto_trade_enabled", state["auto"])
            state["gate"] = payload.get("auto_trade_time_gate_enabled", state["gate"])
            state["interval"] = payload.get("auto_interval_minutes", state["interval"])
            state["mode"] = payload.get("auto_execution_mode", state["mode"])
            if payload.get("auto_trade_enabled") is True:
                raise URLError("response lost after scheduler enable")
            return status()
        if method == "POST" and path.startswith(
            "/api/v1/vnpy-paper/accounts/1/restore"
        ):
            state["account"] = 1
            return status()
        if method == "POST" and path == "/api/v1/vnpy-paper/accounts/archived/cleanup":
            return {"cleaned_account_ids": [2]}
        raise AssertionError(f"unexpected request: {method} {path}")

    monkeypatch.setattr(e2e, "_request_json", fake_request)
    output_path = tmp_path / "scheduler-enable-lost.json"
    exit_code = main([
        "--execution-mode", "vnpy_paper",
        "--trigger-mode", "scheduler",
        "--allow-simulated-orders",
        "--isolated-account",
        "--output-json", str(output_path),
    ])

    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert exit_code == 1
    assert report["evaluation"]["settings_restored"] is True
    assert report["evaluation"]["failures"][0] == "acceptance_runtime_error"
    assert state == {
        "account": 1,
        "auto": False,
        "gate": True,
        "interval": 1440,
        "mode": "paper",
        "created": True,
    }
    assert updates[-1] == {
        "auto_trade_time_gate_enabled": True,
        "auto_interval_minutes": 1440,
        "auto_execution_mode": "paper",
        "auto_trade_enabled": False,
    }
