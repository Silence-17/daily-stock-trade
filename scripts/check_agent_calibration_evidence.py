# -*- coding: utf-8 -*-
"""Gate persisted Agent return/risk calibration evidence across markets."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen


def _utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _validated_base_url(parser: argparse.ArgumentParser, value: str) -> str:
    candidate = str(value or "").strip().rstrip("/")
    parsed = urlsplit(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        parser.error("--base-url must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        parser.error("--base-url must not contain embedded credentials")
    return candidate


def _bounded_int(
    parser: argparse.ArgumentParser,
    name: str,
    value: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if value < minimum or value > maximum:
        parser.error(f"{name} must be between {minimum} and {maximum}")
    return value


def _bounded_float(
    parser: argparse.ArgumentParser,
    name: str,
    value: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    if value < minimum or value > maximum:
        parser.error(f"{name} must be between {minimum} and {maximum}")
    return value


def _read_json(url: str, *, timeout_seconds: float) -> Dict[str, Any]:
    request = Request(url, headers={"Accept": "application/json"}, method="GET")
    with urlopen(request, timeout=timeout_seconds) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("API response must be a JSON object")
    return payload


def _parse_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else parsed


def _ratio_pct(value: Any) -> float:
    try:
        return max(0.0, min(100.0, float(value or 0.0)))
    except (TypeError, ValueError):
        return 0.0


def evaluate_calibration_evidence(
    summaries: Mapping[str, Mapping[str, Any]],
    *,
    required_markets: Iterable[str],
    required_versions: Iterable[str],
    min_runs_per_market: int,
    min_observed_per_market: int,
    min_observation_rate_pct: float,
    min_mature_samples: int,
    min_observation_days: int,
    max_latest_age_hours: float,
    now: datetime | None = None,
) -> Dict[str, Any]:
    checked_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    markets = sorted({str(item).strip().lower() for item in required_markets if str(item).strip()})
    versions = sorted({str(item).strip() for item in required_versions if str(item).strip()})
    market_results: Dict[str, Dict[str, Any]] = {}
    failures = []

    for market in markets:
        payload = summaries.get(market)
        payload = payload if isinstance(payload, Mapping) else {}
        total = max(0, int(payload.get("total") or 0))
        scanned = max(0, int(payload.get("scanned_count") or 0))
        observed = max(0, int(payload.get("observed_count") or 0))
        observation_rate = _ratio_pct(payload.get("observation_rate_pct"))
        latest_mature = max(0, int(payload.get("latest_mature_sample_count") or 0))
        daily = payload.get("daily") if isinstance(payload.get("daily"), list) else []
        observation_days = len({str(item.get("date")) for item in daily if isinstance(item, Mapping) and item.get("date")})
        version_counts = payload.get("version_counts")
        version_counts = version_counts if isinstance(version_counts, Mapping) else {}
        missing_versions = [version for version in versions if int(version_counts.get(version) or 0) <= 0]
        latest = payload.get("latest") if isinstance(payload.get("latest"), Mapping) else {}
        latest_at = _parse_datetime(latest.get("created_at"))
        summary_generated_at = _parse_datetime(payload.get("generated_at"))
        age_reference = summary_generated_at or checked_at
        if latest_at is not None and latest_at.tzinfo is None and age_reference.tzinfo is not None:
            age_reference = age_reference.replace(tzinfo=None)
        latest_future_seconds = (
            (latest_at - age_reference).total_seconds()
            if latest_at is not None
            else 0.0
        )
        latest_age_hours = (
            max(0.0, (age_reference - latest_at).total_seconds() / 3600.0)
            if latest_at is not None
            else None
        )
        market_failures = []
        if not payload:
            market_failures.append("summary_unavailable")
        if bool(payload.get("truncated")) or scanned < total:
            market_failures.append("summary_truncated")
        filters = payload.get("filters") if isinstance(payload.get("filters"), Mapping) else {}
        if str(filters.get("market") or "").strip().lower() != market:
            market_failures.append("market_filter_mismatch")
        if total < min_runs_per_market:
            market_failures.append("runs_below_threshold")
        if observed < min_observed_per_market:
            market_failures.append("observed_runs_below_threshold")
        if observation_rate + 1e-12 < min_observation_rate_pct:
            market_failures.append("observation_rate_below_threshold")
        if latest_mature < min_mature_samples:
            market_failures.append("mature_samples_below_threshold")
        if observation_days < min_observation_days:
            market_failures.append("observation_days_below_threshold")
        if latest_at is None:
            market_failures.append("latest_observation_missing")
        elif latest_future_seconds > 300.0:
            market_failures.append("latest_observation_in_future")
        elif latest_age_hours is not None and latest_age_hours > max_latest_age_hours:
            market_failures.append("latest_observation_stale")
        if missing_versions:
            market_failures.append("required_versions_missing")
        if str(latest.get("state") or "").strip().lower() in {"blocked", "unavailable"}:
            market_failures.append("latest_state_not_deployable")

        market_results[market] = {
            "ok": not market_failures,
            "failures": market_failures,
            "total_runs": total,
            "observed_runs": observed,
            "observation_rate_pct": observation_rate,
            "latest_mature_sample_count": latest_mature,
            "observation_days": observation_days,
            "age_reference_at": age_reference.isoformat(),
            "latest_at": latest_at.isoformat() if latest_at is not None else None,
            "latest_age_hours": round(latest_age_hours, 3) if latest_age_hours is not None else None,
            "latest_state": latest.get("state"),
            "version_counts": dict(version_counts),
            "missing_versions": missing_versions,
        }
        failures.extend(f"{market}:{failure}" for failure in market_failures)

    if not markets:
        failures.append("no_required_markets")
    return {
        "ok": not failures,
        "failures": failures,
        "required_markets": markets,
        "required_versions": versions,
        "thresholds": {
            "min_runs_per_market": min_runs_per_market,
            "min_observed_per_market": min_observed_per_market,
            "min_observation_rate_pct": min_observation_rate_pct,
            "min_mature_samples": min_mature_samples,
            "min_observation_days": min_observation_days,
            "max_latest_age_hours": max_latest_age_hours,
        },
        "markets": market_results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--market", action="append", dest="markets", default=[])
    parser.add_argument("--strategy")
    parser.add_argument(
        "--trigger-source",
        default="agent_calibration_shadow",
        help=(
            "Agent run trigger source to include. Defaults to the order-free "
            "calibration shadow collector."
        ),
    )
    parser.add_argument("--status", default="completed")
    parser.add_argument("--required-version", action="append", default=[])
    parser.add_argument("--min-runs-per-market", type=int, default=20)
    parser.add_argument("--min-observed-per-market", type=int, default=10)
    parser.add_argument("--min-observation-rate-pct", type=float, default=80.0)
    parser.add_argument("--min-mature-samples", type=int, default=20)
    parser.add_argument("--min-observation-days", type=int, default=5)
    parser.add_argument("--max-latest-age-hours", type=float, default=72.0)
    parser.add_argument("--request-timeout-seconds", type=float, default=30.0)
    parser.add_argument("--output-json", type=Path)
    args = parser.parse_args(argv)

    base_url = _validated_base_url(parser, args.base_url)
    days = _bounded_int(parser, "--days", args.days, minimum=1, maximum=90)
    markets = args.markets or ["cn", "hk", "us"]
    markets = sorted({str(item).strip().lower() for item in markets if str(item).strip()})
    min_runs = _bounded_int(parser, "--min-runs-per-market", args.min_runs_per_market, minimum=1, maximum=5000)
    min_observed = _bounded_int(parser, "--min-observed-per-market", args.min_observed_per_market, minimum=1, maximum=5000)
    min_rate = _bounded_float(parser, "--min-observation-rate-pct", args.min_observation_rate_pct, minimum=0.0, maximum=100.0)
    min_mature = _bounded_int(parser, "--min-mature-samples", args.min_mature_samples, minimum=1, maximum=1000000)
    min_days = _bounded_int(parser, "--min-observation-days", args.min_observation_days, minimum=1, maximum=90)
    max_age = _bounded_float(parser, "--max-latest-age-hours", args.max_latest_age_hours, minimum=0.1, maximum=24.0 * 365.0)
    timeout = _bounded_float(parser, "--request-timeout-seconds", args.request_timeout_seconds, minimum=0.1, maximum=120.0)

    summaries: Dict[str, Dict[str, Any]] = {}
    errors: Dict[str, str] = {}
    for market in markets:
        params = {"days": days, "market": market}
        if args.strategy:
            params["strategy"] = str(args.strategy).strip()
        if args.trigger_source:
            params["trigger_source"] = str(args.trigger_source).strip()
        if args.status:
            params["status"] = str(args.status).strip()
        url = f"{base_url}/api/v1/vnpy-paper/agent-runs/return-risk-calibration-trends?{urlencode(params)}"
        try:
            summaries[market] = _read_json(url, timeout_seconds=timeout)
        except HTTPError as exc:
            errors[market] = f"http_{exc.code}"
        except (URLError, TimeoutError):
            errors[market] = "connection_error"
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError, TypeError):
            errors[market] = "invalid_response"

    evaluation = evaluate_calibration_evidence(
        summaries,
        required_markets=markets,
        required_versions=args.required_version,
        min_runs_per_market=min_runs,
        min_observed_per_market=min_observed,
        min_observation_rate_pct=min_rate,
        min_mature_samples=min_mature,
        min_observation_days=min_days,
        max_latest_age_hours=max_age,
    )
    result = {
        "schema_version": 1,
        "generated_at": _utc_iso(),
        "base_url": base_url,
        "window_days": days,
        "filters": {
            "strategy": args.strategy,
            "trigger_source": args.trigger_source,
            "status": args.status,
        },
        "errors": errors,
        "evaluation": evaluation,
        "methodology": {
            "read_only": True,
            "creates_agent_runs": False,
            "places_orders": False,
            "overlapping_rolling_samples": True,
            "independent_sample_count_claimed": False,
        },
    }
    if errors:
        result["evaluation"]["ok"] = False
        result["evaluation"]["failures"].extend(
            f"{market}:{error}" for market, error in sorted(errors.items())
        )
    encoded = json.dumps(result, ensure_ascii=False, indent=2)
    print(encoded)
    if args.output_json is not None:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(encoded + "\n", encoding="utf-8")
    return 0 if result["evaluation"]["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
