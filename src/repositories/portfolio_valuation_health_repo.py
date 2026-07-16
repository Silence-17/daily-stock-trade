# -*- coding: utf-8 -*-
"""Persistence and window aggregation for portfolio valuation health."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from src.storage import (
    DatabaseManager,
    PortfolioValuationHealthObservation,
    to_utc_naive_datetime,
)


class PortfolioValuationHealthRepository:
    """Store bounded, deduplicated valuation observations and summarize trends."""

    BUCKET_MINUTES = 15
    RETENTION_DAYS = 120
    WINDOW_DAYS = (7, 30, 90)
    DEFAULT_SCOPE = "active_accounts"

    def __init__(self, db_manager: Optional[DatabaseManager] = None) -> None:
        self.db = db_manager or DatabaseManager.get_instance()

    @classmethod
    def _utc_naive(cls, value: Optional[datetime] = None) -> datetime:
        current = value or datetime.now(timezone.utc)
        return to_utc_naive_datetime(current)

    @classmethod
    def _bucket_start(cls, value: datetime) -> datetime:
        minute = value.minute - (value.minute % cls.BUCKET_MINUTES)
        return value.replace(minute=minute, second=0, microsecond=0)

    def record_observation(
        self,
        health: Dict[str, Any],
        *,
        observed_at: Optional[datetime] = None,
        scope: str = DEFAULT_SCOPE,
    ) -> Dict[str, Any]:
        observed = self._utc_naive(observed_at)
        bucket = self._bucket_start(observed)
        values = {
            "scope": str(scope or self.DEFAULT_SCOPE),
            "bucket_started_at": bucket,
            "observed_at": observed,
            "status": str(health.get("status") or "unknown"),
            "position_count": int(health.get("position_count") or 0),
            "account_count": int(health.get("account_count") or 0),
            "available_count": int(health.get("available_count") or 0),
            "fresh_count": int(health.get("fresh_count") or 0),
            "missing_count": int(health.get("missing_count") or 0),
            "unknown_count": int(health.get("unknown_count") or 0),
            "stale_count": int(health.get("stale_count") or 0),
            "coverage_pct": float(health.get("coverage_pct") or 0.0),
            "fresh_coverage_pct": float(health.get("fresh_coverage_pct") or 0.0),
            "source_counts_json": self._json_dumps(health.get("source_counts")),
            "provider_counts_json": self._json_dumps(health.get("provider_counts")),
            "oldest_price_date": health.get("oldest_price_date"),
            "latest_price_date": health.get("latest_price_date"),
        }
        inserted = False
        try:
            with self.db.get_session() as session:
                row = PortfolioValuationHealthObservation(**values)
                session.add(row)
                session.commit()
                session.refresh(row)
                inserted = True
                result = self._to_dict(row)
        except IntegrityError:
            with self.db.get_session() as session:
                row = session.execute(
                    select(PortfolioValuationHealthObservation).where(
                        PortfolioValuationHealthObservation.scope == values["scope"],
                        PortfolioValuationHealthObservation.bucket_started_at == bucket,
                    )
                ).scalar_one()
                result = self._to_dict(row)
        if inserted:
            self.cleanup(older_than=observed - timedelta(days=self.RETENTION_DAYS))
        result["inserted"] = inserted
        return result

    def trends(
        self,
        *,
        now: Optional[datetime] = None,
        scope: str = DEFAULT_SCOPE,
    ) -> Dict[str, Any]:
        ended_at = self._utc_naive(now)
        started_at = ended_at - timedelta(days=max(self.WINDOW_DAYS))
        with self.db.get_session() as session:
            rows = session.execute(
                select(PortfolioValuationHealthObservation).where(
                    PortfolioValuationHealthObservation.scope == str(scope or self.DEFAULT_SCOPE),
                    PortfolioValuationHealthObservation.observed_at >= started_at,
                    PortfolioValuationHealthObservation.observed_at <= ended_at,
                ).order_by(PortfolioValuationHealthObservation.observed_at)
            ).scalars().all()
        windows = [
            self._aggregate_window(
                [row for row in rows if row.observed_at >= ended_at - timedelta(days=days)],
                days=days,
            )
            for days in self.WINDOW_DAYS
        ]
        return {
            "schema_version": 1,
            "scope": str(scope or self.DEFAULT_SCOPE),
            "bucket_minutes": self.BUCKET_MINUTES,
            "retention_days": self.RETENTION_DAYS,
            "generated_at": ended_at.isoformat(),
            "windows": windows,
        }

    def cleanup(self, *, older_than: datetime) -> int:
        cutoff = self._utc_naive(older_than)
        with self.db.get_session() as session:
            result = session.execute(
                delete(PortfolioValuationHealthObservation).where(
                    PortfolioValuationHealthObservation.observed_at < cutoff
                )
            )
            session.commit()
            return int(result.rowcount or 0)

    @classmethod
    def _aggregate_window(
        cls,
        rows: List[PortfolioValuationHealthObservation],
        *,
        days: int,
    ) -> Dict[str, Any]:
        count = len(rows)
        health_rows = [row for row in rows if int(row.position_count or 0) > 0]
        health_count = len(health_rows)
        degraded = sum(1 for row in health_rows if row.status != "ready")
        position_observation_count = sum(
            max(0, int(row.position_count or 0)) for row in health_rows
        )
        available_position_observation_count = sum(
            max(0, min(int(row.available_count or 0), int(row.position_count or 0)))
            for row in health_rows
        )
        fresh_position_observation_count = sum(
            max(
                0,
                min(
                    int(row.fresh_count or 0),
                    int(row.available_count or 0),
                    int(row.position_count or 0),
                ),
            )
            for row in health_rows
        )
        coverage_values = [
            cls._percentage(
                max(0, min(int(row.available_count or 0), int(row.position_count or 0))),
                int(row.position_count or 0),
            )
            for row in health_rows
        ]
        fresh_coverage_values = [
            cls._percentage(
                max(
                    0,
                    min(
                        int(row.fresh_count or 0),
                        int(row.available_count or 0),
                        int(row.position_count or 0),
                    ),
                ),
                int(row.position_count or 0),
            )
            for row in health_rows
        ]
        provider_totals: Dict[str, int] = {}
        provider_observations: Dict[str, int] = {}
        for row in health_rows:
            for provider, raw_count in cls._json_loads(row.provider_counts_json).items():
                count_value = max(0, int(raw_count or 0))
                provider_totals[provider] = provider_totals.get(provider, 0) + count_value
                if count_value > 0:
                    provider_observations[provider] = provider_observations.get(provider, 0) + 1
        attributed_position_observation_count = sum(provider_totals.values())
        return {
            "window_days": days,
            "observation_count": count,
            "health_observation_count": health_count,
            "empty_position_observation_count": count - health_count,
            "degraded_count": degraded,
            "degraded_pct": round(degraded / health_count * 100.0, 2) if health_count else None,
            "position_observation_count": position_observation_count,
            "available_position_observation_count": available_position_observation_count,
            "fresh_position_observation_count": fresh_position_observation_count,
            "position_weighted_coverage_pct": cls._percentage(
                available_position_observation_count,
                position_observation_count,
            ),
            "position_weighted_fresh_coverage_pct": cls._percentage(
                fresh_position_observation_count,
                position_observation_count,
            ),
            "average_coverage_pct": cls._average(coverage_values),
            "minimum_coverage_pct": cls._minimum(coverage_values),
            "average_fresh_coverage_pct": cls._average(fresh_coverage_values),
            "minimum_fresh_coverage_pct": cls._minimum(fresh_coverage_values),
            "latest_observed_at": health_rows[-1].observed_at.isoformat() if health_rows else None,
            "provider_attributed_position_observation_count": attributed_position_observation_count,
            "provider_attribution_coverage_pct": cls._percentage(
                attributed_position_observation_count,
                position_observation_count,
            ),
            "provider_usage": [
                {
                    "provider": provider,
                    "position_observation_count": provider_totals[provider],
                    "observation_count": provider_observations.get(provider, 0),
                    "share_pct": cls._percentage(
                        provider_totals[provider],
                        attributed_position_observation_count,
                    ),
                }
                for provider in sorted(
                    provider_totals,
                    key=lambda item: (-provider_totals[item], item),
                )
            ],
        }

    @staticmethod
    def _average(values: List[Optional[float]]) -> Optional[float]:
        values = [value for value in values if value is not None]
        return round(sum(values) / len(values), 2) if values else None

    @staticmethod
    def _minimum(values: List[Optional[float]]) -> Optional[float]:
        values = [value for value in values if value is not None]
        return round(min(values), 2) if values else None

    @staticmethod
    def _percentage(numerator: int, denominator: int) -> Optional[float]:
        return round(numerator / denominator * 100.0, 2) if denominator > 0 else None

    @staticmethod
    def _json_dumps(value: Any) -> str:
        payload = value if isinstance(value, dict) else {}
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def _json_loads(raw: Optional[str]) -> Dict[str, Any]:
        try:
            value = json.loads(raw or "{}")
        except (TypeError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    @classmethod
    def _to_dict(cls, row: PortfolioValuationHealthObservation) -> Dict[str, Any]:
        return {
            "id": row.id,
            "scope": row.scope,
            "bucket_started_at": row.bucket_started_at.isoformat(),
            "observed_at": row.observed_at.isoformat(),
            "status": row.status,
            "position_count": row.position_count,
            "coverage_pct": row.coverage_pct,
            "fresh_coverage_pct": row.fresh_coverage_pct,
        }
