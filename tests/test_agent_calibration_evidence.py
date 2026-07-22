# -*- coding: utf-8 -*-
"""Tests for the production calibration evidence gate."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from scripts.check_agent_calibration_evidence import (
    evaluate_calibration_evidence,
    main,
)
from src.services.agent_calibration_evidence_service import (
    attach_current_forward_quality,
    collect_persisted_calibration_evidence,
)


NOW = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)


def test_calibration_evidence_cli_can_run_directly() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/check_agent_calibration_evidence.py", "--help"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "Gate persisted Agent return/risk calibration evidence" in result.stdout


def _summary(market: str, *, state: str = "healthy") -> dict:
    return {
        "generated_at": NOW.isoformat(),
        "total": 30,
        "scanned_count": 30,
        "observed_count": 28,
        "observation_rate_pct": 93.33,
        "latest_mature_sample_count": 24,
        "truncated": False,
        "filters": {"market": market},
        "version_counts": {"candidate-return-risk-v1": 28},
        "latest": {
            "created_at": (NOW - timedelta(hours=2)).isoformat(),
            "state": state,
        },
        "daily": [{"date": f"2026-07-{day:02d}"} for day in range(11, 20)],
    }


def _evaluate(summaries: dict) -> dict:
    return evaluate_calibration_evidence(
        summaries,
        required_markets=["cn", "hk", "us"],
        required_versions=["candidate-return-risk-v1"],
        min_runs_per_market=20,
        min_observed_per_market=10,
        min_observation_rate_pct=80.0,
        min_mature_samples=20,
        min_observation_days=5,
        max_latest_age_hours=72.0,
        now=NOW,
    )


def test_accepts_complete_fresh_multi_market_evidence() -> None:
    result = _evaluate({market: _summary(market) for market in ("cn", "hk", "us")})

    assert result["ok"] is True
    assert result["failures"] == []
    assert result["markets"]["cn"]["latest_age_hours"] == 2.0


def test_rejects_missing_stale_immature_and_blocked_market_evidence() -> None:
    cn = _summary("cn")
    cn.update({
        "total": 4,
        "scanned_count": 3,
        "observed_count": 2,
        "observation_rate_pct": 50.0,
        "latest_mature_sample_count": 3,
        "truncated": True,
        "version_counts": {},
        "daily": [{"date": "2026-07-19"}],
    })
    cn["latest"] = {
        "created_at": (NOW - timedelta(hours=100)).isoformat(),
        "state": "blocked",
    }

    result = _evaluate({"cn": cn, "hk": _summary("hk")})

    assert result["ok"] is False
    failures = set(result["failures"])
    assert "cn:summary_truncated" in failures
    assert "cn:runs_below_threshold" in failures
    assert "cn:observed_runs_below_threshold" in failures
    assert "cn:observation_rate_below_threshold" in failures
    assert "cn:mature_samples_below_threshold" in failures
    assert "cn:observation_days_below_threshold" in failures
    assert "cn:latest_observation_stale" in failures
    assert "cn:required_versions_missing" in failures
    assert "cn:latest_state_not_deployable" in failures
    assert "us:summary_unavailable" in failures
    assert "us:market_filter_mismatch" in failures


def test_rejects_latest_observation_ahead_of_summary_clock() -> None:
    summaries = {market: _summary(market) for market in ("cn", "hk", "us")}
    summaries["us"]["latest"]["created_at"] = (NOW + timedelta(minutes=6)).isoformat()

    result = _evaluate(summaries)

    assert result["ok"] is False
    assert "us:latest_observation_in_future" in result["failures"]


def test_cli_reads_each_market_and_writes_machine_readable_evidence(tmp_path: Path) -> None:
    output_path = tmp_path / "calibration.json"

    def read_summary(url: str, *, timeout_seconds: float) -> dict:
        assert timeout_seconds == 4.0
        assert "trigger_source=agent_calibration_shadow" in url
        market = next(item for item in ("cn", "hk", "us") if f"market={item}" in url)
        return _summary(market)

    with patch(
        "scripts.check_agent_calibration_evidence._read_json",
        side_effect=read_summary,
    ) as reader:
        exit_code = main([
            "--base-url",
            "http://127.0.0.1:8000",
            "--required-version",
            "candidate-return-risk-v1",
            "--request-timeout-seconds",
            "4",
            "--output-json",
            str(output_path),
        ])

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert reader.call_count == 3
    assert payload["evaluation"]["ok"] is True
    assert payload["evaluation"]["required_markets"] == ["cn", "hk", "us"]
    assert payload["filters"]["trigger_source"] == "agent_calibration_shadow"
    assert payload["methodology"]["creates_agent_runs"] is False
    assert payload["methodology"]["places_orders"] is False


def test_shared_collector_reads_each_market_without_write_side_effects() -> None:
    class ReadOnlyRepository:
        def __init__(self) -> None:
            self.calls = []

        def summarize_return_risk_calibration_trends(self, **kwargs) -> dict:
            self.calls.append(kwargs)
            current = datetime.now(timezone.utc)
            payload = _summary(kwargs["market"])
            payload["generated_at"] = current.isoformat()
            payload["latest"]["created_at"] = (current - timedelta(hours=2)).isoformat()
            payload["filters"].update({
                "trigger_source": kwargs["trigger_source"],
                "status": kwargs["status"],
            })
            return payload

    repository = ReadOnlyRepository()
    result = collect_persisted_calibration_evidence(
        repository,
        markets=["us", "cn", "cn", "hk"],
    )

    assert result["evaluation"]["ok"] is True
    assert result["evaluation"]["required_markets"] == ["cn", "hk", "us"]
    assert [call["market"] for call in repository.calls] == ["cn", "hk", "us"]
    assert all(call["days"] == 90 for call in repository.calls)
    assert all(call["trigger_source"] == "agent_calibration_shadow" for call in repository.calls)
    assert result["methodology"]["read_only"] is True
    assert result["methodology"]["creates_agent_runs"] is False
    assert result["methodology"]["places_orders"] is False


def test_current_shadow_quality_updates_effective_maturity_without_refresh() -> None:
    summaries = {market: _summary(market) for market in ("cn", "hk", "us")}
    for payload in summaries.values():
        payload["latest_mature_sample_count"] = 0
        payload["strategy_counts"] = {"dual_low": 28}

    class ReadOnlyQualityService:
        def __init__(self) -> None:
            self.calls = []

        def build_quality_snapshot(self, **kwargs) -> dict:
            self.calls.append(kwargs)
            return {
                "generated_at": NOW.isoformat(),
                "state": "healthy",
                "reason": "forward_quality_thresholds_met",
                "horizon_days": 5,
                "sample_count": 28,
                "mature_sample_count": 24,
                "truncated": False,
            }

    quality_service = ReadOnlyQualityService()
    attach_current_forward_quality(
        summaries,
        quality_service=quality_service,
        days=90,
        trigger_source="agent_calibration_shadow",
        status="completed",
    )
    result = _evaluate(summaries)

    assert result["ok"] is True
    assert result["markets"]["cn"]["latest_mature_sample_count"] == 24
    assert result["markets"]["cn"]["persisted_latest_mature_sample_count"] == 0
    assert result["markets"]["cn"]["current_mature_sample_count"] == 24
    assert result["markets"]["cn"]["mature_sample_source"] == (
        "current_shadow_decisions_and_local_daily_bars"
    )
    assert len(quality_service.calls) == 3
    assert all(call["refresh_missing"] is False for call in quality_service.calls)
    assert all(
        call["trigger_source"] == "agent_calibration_shadow"
        for call in quality_service.calls
    )
    assert all(call["run_status"] == "completed" for call in quality_service.calls)


def test_current_quality_failure_falls_back_to_persisted_snapshot_safely() -> None:
    summaries = {market: _summary(market) for market in ("cn", "hk", "us")}
    for payload in summaries.values():
        payload["strategy_counts"] = {"dual_low": 28}

    class FailingQualityService:
        def build_quality_snapshot(self, **kwargs) -> dict:
            raise RuntimeError("sensitive provider detail")

    attach_current_forward_quality(
        summaries,
        quality_service=FailingQualityService(),
        days=90,
        trigger_source="agent_calibration_shadow",
        status="completed",
    )
    result = _evaluate(summaries)

    assert result["ok"] is False
    assert "cn:current_forward_quality_unavailable" in result["failures"]
    current = result["markets"]["cn"]["current_forward_quality"]
    assert current["available"] is False
    assert current["error_type"] == "RuntimeError"
    assert "sensitive provider detail" not in str(result)
    assert result["markets"]["cn"]["latest_mature_sample_count"] == 24
    assert result["markets"]["cn"]["mature_sample_source"] == (
        "persisted_agent_run_cross_run_quality_snapshot"
    )
