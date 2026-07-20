# -*- coding: utf-8 -*-
"""Validate checkpointed full-market historical factor ingestion through the API."""

from __future__ import annotations

import argparse
import json
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen


def _utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _request_json(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    payload: Dict[str, Any] | None = None,
    timeout_seconds: float,
) -> Dict[str, Any]:
    encoded = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Accept": "application/json"}
    if encoded is not None:
        headers["Content-Type"] = "application/json"
    request = Request(
        f"{base_url.rstrip('/')}{path}",
        data=encoded,
        headers=headers,
        method=method,
    )
    with urlopen(request, timeout=timeout_seconds) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not isinstance(result, dict):
        raise ValueError(f"{method} {path} must return a JSON object")
    return result


def evaluate_full_market_job(
    job: Dict[str, Any],
    *,
    min_symbols: int,
    min_coverage_ratio: float,
    max_source_error_ratio: float,
    progress_regressed: bool,
    stalled: bool,
) -> Dict[str, Any]:
    failures: list[str] = []
    status = str(job.get("status") or "")
    total_symbols = int(job.get("total_symbols") or 0)
    total_work_items = int(job.get("total_work_items") or 0)
    next_offset = int(job.get("next_offset") or 0)
    remaining = int(job.get("remaining_work_items") or 0)
    row_count = int(job.get("row_count") or 0)
    result_payload = job.get("result") or {}
    complete_row_count = int(result_payload.get("complete_row_count") or 0)
    exact_daily_row_count = int(result_payload.get("exact_daily_row_count") or 0)
    valuation_complete_row_count = int(
        result_payload.get("valuation_complete_row_count") or 0
    )
    source_errors = int(job.get("source_error_count") or 0)
    coverage_ratio = complete_row_count / total_work_items if total_work_items else 0.0
    exact_daily_coverage_ratio = (
        exact_daily_row_count / total_work_items if total_work_items else 0.0
    )
    valuation_coverage_ratio = (
        valuation_complete_row_count / total_work_items if total_work_items else 0.0
    )
    source_error_ratio = source_errors / total_work_items if total_work_items else 1.0

    if status != "completed":
        failures.append("job_not_completed")
    if total_symbols < min_symbols:
        failures.append("full_market_symbol_count_below_threshold")
    if total_work_items < total_symbols or total_work_items <= 0:
        failures.append("invalid_total_work_items")
    if next_offset != total_work_items or remaining != 0:
        failures.append("checkpoint_not_complete")
    if float(job.get("progress_pct") or 0.0) < 100.0:
        failures.append("progress_not_complete")
    if coverage_ratio < min_coverage_ratio:
        failures.append("factor_coverage_below_threshold")
    if source_error_ratio > max_source_error_ratio:
        failures.append("source_error_ratio_above_threshold")
    if progress_regressed:
        failures.append("checkpoint_progress_regressed")
    if stalled:
        failures.append("checkpoint_progress_stalled")
    if str(job.get("recovery_state") or "") not in {"complete", ""}:
        failures.append("recovery_state_not_complete")

    return {
        "ok": not failures,
        "failures": failures,
        "job_id": job.get("job_id"),
        "status": status,
        "market": job.get("market"),
        "snapshot_dates": list(job.get("snapshot_dates") or []),
        "total_symbols": total_symbols,
        "total_work_items": total_work_items,
        "completed_work_items": next_offset,
        "completed_batches": int(job.get("completed_batches") or 0),
        "row_count": row_count,
        "complete_row_count": complete_row_count,
        "coverage_ratio": round(coverage_ratio, 6),
        "exact_daily_row_count": exact_daily_row_count,
        "exact_daily_coverage_ratio": round(exact_daily_coverage_ratio, 6),
        "valuation_complete_row_count": valuation_complete_row_count,
        "valuation_coverage_ratio": round(valuation_coverage_ratio, 6),
        "source_error_count": source_errors,
        "source_error_ratio": round(source_error_ratio, 6),
        "corporate_action_count": int(
            (job.get("result") or {}).get("corporate_action_count") or 0
        ),
        "recovery_state": job.get("recovery_state"),
    }


def _validated_base_url(parser: argparse.ArgumentParser, value: str) -> str:
    candidate = str(value or "").strip().rstrip("/")
    parsed = urlsplit(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        parser.error("--base-url must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        parser.error("--base-url must not contain embedded credentials")
    return candidate


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--job-id")
    parser.add_argument("--create", action="store_true")
    parser.add_argument("--allow-external-ingestion", action="store_true")
    parser.add_argument("--market", default="cn")
    parser.add_argument("--snapshot-date", action="append", default=[])
    parser.add_argument("--batch-size", type=int, default=25)
    parser.add_argument("--resume-orphaned", action="store_true")
    parser.add_argument("--resume-failed", action="store_true")
    parser.add_argument("--min-symbols", type=int, default=1000)
    parser.add_argument("--min-coverage-ratio", type=float, default=0.95)
    parser.add_argument("--max-source-error-ratio", type=float, default=0.05)
    parser.add_argument("--timeout-seconds", type=float, default=21600.0)
    parser.add_argument("--max-stall-seconds", type=float, default=1800.0)
    parser.add_argument("--poll-interval-seconds", type=float, default=5.0)
    parser.add_argument("--request-timeout-seconds", type=float, default=120.0)
    parser.add_argument("--output-json", type=Path)
    args = parser.parse_args(list(argv) if argv is not None else None)

    base_url = _validated_base_url(parser, args.base_url)
    if args.create and args.job_id:
        parser.error("--create and --job-id are mutually exclusive")
    if args.create and not args.allow_external_ingestion:
        parser.error("--create requires --allow-external-ingestion")
    if args.create and not args.snapshot_date:
        parser.error("--create requires at least one --snapshot-date")
    if not 1 <= args.batch_size <= 50:
        parser.error("--batch-size must be between 1 and 50")
    if args.min_symbols < 1:
        parser.error("--min-symbols must be positive")
    for name in ("min_coverage_ratio", "max_source_error_ratio"):
        value = getattr(args, name)
        if value < 0 or value > 1:
            parser.error(f"--{name.replace('_', '-')} must be between 0 and 1")
    for name in (
        "timeout_seconds",
        "max_stall_seconds",
        "poll_interval_seconds",
        "request_timeout_seconds",
    ):
        if getattr(args, name) <= 0:
            parser.error(f"--{name.replace('_', '-')} must be positive")
    for raw_date in args.snapshot_date:
        try:
            date.fromisoformat(raw_date)
        except ValueError:
            parser.error(f"invalid --snapshot-date: {raw_date}")

    started_at = _utc_iso()
    errors: list[str] = []
    job: Dict[str, Any] = {}
    progress_regressed = False
    stalled = False
    resumed = False
    try:
        if args.create:
            job = _request_json(
                base_url,
                "/api/v1/alphasift/replay/full-market-ingestion/jobs",
                method="POST",
                payload={
                    "market": args.market,
                    "snapshot_dates": args.snapshot_date,
                    "batch_size": args.batch_size,
                },
                timeout_seconds=args.request_timeout_seconds,
            )
        elif args.job_id:
            job = _request_json(
                base_url,
                "/api/v1/alphasift/replay/full-market-ingestion/jobs/"
                + quote(args.job_id, safe=""),
                timeout_seconds=args.request_timeout_seconds,
            )
        else:
            recent = _request_json(
                base_url,
                "/api/v1/alphasift/replay/full-market-ingestion/jobs?limit=1",
                timeout_seconds=args.request_timeout_seconds,
            )
            items = [item for item in recent.get("items") or [] if isinstance(item, dict)]
            if not items:
                raise ValueError("no full-market ingestion job exists")
            job = items[0]

        job_id = str(job.get("job_id") or "").strip()
        if not job_id:
            raise ValueError("full-market ingestion response is missing job_id")
        deadline = time.monotonic() + max(1.0, args.timeout_seconds)
        last_offset = int(job.get("next_offset") or 0)
        last_progress_at = time.monotonic()
        while str(job.get("status") or "") != "completed":
            recovery = str(job.get("recovery_state") or "")
            should_resume = (
                recovery == "orphaned" and args.resume_orphaned
            ) or (recovery == "retryable" and args.resume_failed)
            if should_resume and not resumed:
                job = _request_json(
                    base_url,
                    f"/api/v1/alphasift/replay/full-market-ingestion/jobs/{quote(job_id, safe='')}/resume",
                    method="POST",
                    timeout_seconds=args.request_timeout_seconds,
                )
                resumed = True
            if str(job.get("status") or "") == "failed" and (
                not should_resume or resumed
            ):
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(max(0.05, args.poll_interval_seconds))
            job = _request_json(
                base_url,
                f"/api/v1/alphasift/replay/full-market-ingestion/jobs/{quote(job_id, safe='')}",
                timeout_seconds=args.request_timeout_seconds,
            )
            offset = int(job.get("next_offset") or 0)
            if offset < last_offset:
                progress_regressed = True
            if offset > last_offset:
                last_offset = offset
                last_progress_at = time.monotonic()
            elif (
                str(job.get("status") or "") in {"pending", "processing"}
                and time.monotonic() - last_progress_at > args.max_stall_seconds
            ):
                stalled = True
                break
    except Exception as exc:  # pragma: no cover - exercised through CLI tests
        errors.append(f"{type(exc).__name__}: {exc}")

    evaluation = evaluate_full_market_job(
        job,
        min_symbols=args.min_symbols,
        min_coverage_ratio=args.min_coverage_ratio,
        max_source_error_ratio=args.max_source_error_ratio,
        progress_regressed=progress_regressed,
        stalled=stalled,
    )
    if errors:
        evaluation["ok"] = False
        evaluation["failures"] = ["acceptance_runtime_error", *evaluation["failures"]]
    result = {
        "schema_version": 1,
        "ok": evaluation["ok"],
        "started_at": started_at,
        "finished_at": _utc_iso(),
        "base_url": base_url,
        "created": bool(args.create),
        "resumed": resumed,
        "error": "; ".join(errors) if errors else None,
        "evaluation": evaluation,
    }
    output = json.dumps(result, ensure_ascii=False, indent=2)
    print(output)
    if args.output_json is not None:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(output + "\n", encoding="utf-8")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
