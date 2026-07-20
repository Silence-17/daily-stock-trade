# -*- coding: utf-8 -*-
"""Tests for the full-market ingestion acceptance gate."""

import json

import pytest

import scripts.check_full_market_ingestion_e2e as e2e
from scripts.check_full_market_ingestion_e2e import evaluate_full_market_job, main


def _completed_job():
    return {
        "job_id": "job-1",
        "market": "cn",
        "snapshot_dates": ["2024-01-05"],
        "status": "completed",
        "batch_size": 25,
        "total_symbols": 5000,
        "total_work_items": 5000,
        "next_offset": 5000,
        "remaining_work_items": 0,
        "progress_pct": 100,
        "completed_batches": 200,
        "row_count": 4950,
        "source_error_count": 50,
        "result": {
            "corporate_action_count": 12,
            "complete_row_count": 4950,
            "exact_daily_row_count": 4975,
            "valuation_complete_row_count": 4960,
        },
        "recovery_state": "complete",
    }


def test_evaluate_full_market_job_accepts_complete_bounded_coverage():
    result = evaluate_full_market_job(
        _completed_job(),
        min_symbols=4000,
        min_coverage_ratio=0.95,
        max_source_error_ratio=0.02,
        progress_regressed=False,
        stalled=False,
    )

    assert result["ok"] is True
    assert result["coverage_ratio"] == 0.99
    assert result["exact_daily_coverage_ratio"] == 0.995
    assert result["valuation_coverage_ratio"] == 0.992
    assert result["source_error_ratio"] == 0.01


def test_evaluate_full_market_job_reports_completion_quality_failures():
    job = _completed_job()
    job.update({
        "status": "processing",
        "total_symbols": 10,
        "next_offset": 5,
        "remaining_work_items": 4995,
        "progress_pct": 0.1,
        "row_count": 4,
        "source_error_count": 1000,
        "recovery_state": "orphaned",
    })
    job["result"]["complete_row_count"] = 4
    result = evaluate_full_market_job(
        job,
        min_symbols=1000,
        min_coverage_ratio=0.95,
        max_source_error_ratio=0.05,
        progress_regressed=True,
        stalled=True,
    )

    assert result["ok"] is False
    assert result["failures"] == [
        "job_not_completed",
        "full_market_symbol_count_below_threshold",
        "checkpoint_not_complete",
        "progress_not_complete",
        "factor_coverage_below_threshold",
        "source_error_ratio_above_threshold",
        "checkpoint_progress_regressed",
        "checkpoint_progress_stalled",
        "recovery_state_not_complete",
    ]


def test_evaluate_full_market_job_rejects_inserted_but_partial_factor_rows():
    job = _completed_job()
    job["row_count"] = 5000
    job["result"]["complete_row_count"] = 4000

    result = evaluate_full_market_job(
        job,
        min_symbols=4000,
        min_coverage_ratio=0.95,
        max_source_error_ratio=0.02,
        progress_regressed=False,
        stalled=False,
    )

    assert result["ok"] is False
    assert result["failures"] == ["factor_coverage_below_threshold"]
    assert result["coverage_ratio"] == 0.8


def test_cli_observes_latest_completed_job(monkeypatch, tmp_path):
    calls = []

    def fake_request(_base_url, path, **_kwargs):
        calls.append(path)
        return {"items": [_completed_job()], "limit": 1}

    monkeypatch.setattr(e2e, "_request_json", fake_request)
    output_path = tmp_path / "full-market.json"
    exit_code = main([
        "--min-symbols", "4000",
        "--output-json", str(output_path),
    ])

    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert report["ok"] is True
    assert report["created"] is False
    assert calls == [
        "/api/v1/alphasift/replay/full-market-ingestion/jobs?limit=1"
    ]


def test_cli_requires_explicit_permission_to_create_external_job():
    with pytest.raises(SystemExit) as exc_info:
        main(["--create", "--snapshot-date", "2024-01-05"])

    assert exc_info.value.code == 2
