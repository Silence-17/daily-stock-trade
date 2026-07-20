"""Read-only production evidence gate for stock-selection Agent calibration."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Mapping, Optional, Protocol


DEFAULT_CALIBRATION_EVIDENCE_WINDOW_DAYS = 90
DEFAULT_CALIBRATION_EVIDENCE_MIN_RUNS = 20
DEFAULT_CALIBRATION_EVIDENCE_MIN_OBSERVED = 10
DEFAULT_CALIBRATION_EVIDENCE_MIN_OBSERVATION_RATE_PCT = 80.0
DEFAULT_CALIBRATION_EVIDENCE_MIN_MATURE_SAMPLES = 20
DEFAULT_CALIBRATION_EVIDENCE_MIN_OBSERVATION_DAYS = 5
DEFAULT_CALIBRATION_EVIDENCE_MAX_LATEST_AGE_HOURS = 72.0


class CalibrationEvidenceRepository(Protocol):
    def summarize_return_risk_calibration_trends(
        self,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        ...


def _parse_datetime(value: Any) -> Optional[datetime]:
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
    now: Optional[datetime] = None,
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
        observation_days = len({
            str(item.get("date"))
            for item in daily
            if isinstance(item, Mapping) and item.get("date")
        })
        version_counts = payload.get("version_counts")
        version_counts = version_counts if isinstance(version_counts, Mapping) else {}
        missing_versions = [
            version for version in versions if int(version_counts.get(version) or 0) <= 0
        ]
        latest = payload.get("latest") if isinstance(payload.get("latest"), Mapping) else {}
        latest_at = _parse_datetime(latest.get("created_at"))
        summary_generated_at = _parse_datetime(payload.get("generated_at"))
        age_reference = summary_generated_at or checked_at
        if latest_at is not None and latest_at.tzinfo is None and age_reference.tzinfo is not None:
            age_reference = age_reference.replace(tzinfo=None)
        latest_future_seconds = (
            (latest_at - age_reference).total_seconds() if latest_at is not None else 0.0
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
            "latest_age_hours": (
                round(latest_age_hours, 3) if latest_age_hours is not None else None
            ),
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


def collect_persisted_calibration_evidence(
    repository: CalibrationEvidenceRepository,
    *,
    markets: Iterable[str],
    days: int = DEFAULT_CALIBRATION_EVIDENCE_WINDOW_DAYS,
    trigger_source: str = "agent_calibration_shadow",
    status: str = "completed",
) -> Dict[str, Any]:
    required_markets = sorted({
        str(item).strip().lower() for item in markets if str(item).strip()
    })
    summaries = {
        market: repository.summarize_return_risk_calibration_trends(
            days=days,
            limit=5000,
            trigger_source=trigger_source,
            market=market,
            status=status,
        )
        for market in required_markets
    }
    evaluation = evaluate_calibration_evidence(
        summaries,
        required_markets=required_markets,
        required_versions=[],
        min_runs_per_market=DEFAULT_CALIBRATION_EVIDENCE_MIN_RUNS,
        min_observed_per_market=DEFAULT_CALIBRATION_EVIDENCE_MIN_OBSERVED,
        min_observation_rate_pct=DEFAULT_CALIBRATION_EVIDENCE_MIN_OBSERVATION_RATE_PCT,
        min_mature_samples=DEFAULT_CALIBRATION_EVIDENCE_MIN_MATURE_SAMPLES,
        min_observation_days=DEFAULT_CALIBRATION_EVIDENCE_MIN_OBSERVATION_DAYS,
        max_latest_age_hours=DEFAULT_CALIBRATION_EVIDENCE_MAX_LATEST_AGE_HOURS,
    )
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "window_days": days,
        "filters": {
            "trigger_source": trigger_source,
            "status": status,
        },
        "evaluation": evaluation,
        "methodology": {
            "read_only": True,
            "creates_agent_runs": False,
            "places_orders": False,
            "overlapping_rolling_samples": True,
            "independent_sample_count_claimed": False,
        },
    }
