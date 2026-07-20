# -*- coding: utf-8 -*-
"""Tests for the production calibration evidence gate."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from unittest.mock import patch

from scripts.check_agent_calibration_evidence import (
    evaluate_calibration_evidence,
    main,
)


NOW = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)


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
    assert payload["methodology"]["creates_agent_runs"] is False
    assert payload["methodology"]["places_orders"] is False
