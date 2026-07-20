# -*- coding: utf-8 -*-
"""Tests for the no-order vn.py gateway soak evaluator."""

from __future__ import annotations

import json

from scripts.check_vnpy_gateway_soak import _safe_runtime_summary, evaluate_soak


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
