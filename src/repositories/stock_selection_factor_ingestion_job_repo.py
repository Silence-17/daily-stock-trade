# -*- coding: utf-8 -*-
"""Persistence for resumable full-market factor ingestion jobs."""

from __future__ import annotations

import json
from datetime import date
from typing import Any, Dict, List, Optional

from sqlalchemy import desc, select

from src.storage import (
    DatabaseManager,
    StockSelectionFactorIngestionJob,
    utc_naive_now,
)


class StockSelectionFactorIngestionJobRepository:
    def __init__(self, db_manager: Optional[DatabaseManager] = None) -> None:
        self.db = db_manager or DatabaseManager.get_instance()

    def create(
        self,
        *,
        job_id: str,
        market: str,
        snapshot_dates: List[date],
        universe: List[Dict[str, Any]],
        batch_size: int,
    ) -> Dict[str, Any]:
        now = utc_naive_now()
        with self.db.get_session() as session:
            row = StockSelectionFactorIngestionJob(
                job_id=job_id,
                market=market,
                snapshot_dates_json=self._dumps([item.isoformat() for item in snapshot_dates]),
                universe_json=self._dumps(universe),
                batch_size=batch_size,
                total_symbols=len({str(item.get("symbol") or "") for item in universe}),
                total_work_items=len(universe),
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return self._to_dict(row)

    def get(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self.db.get_session() as session:
            row = session.execute(
                select(StockSelectionFactorIngestionJob).where(
                    StockSelectionFactorIngestionJob.job_id == job_id
                )
            ).scalar_one_or_none()
            return self._to_dict(row) if row is not None else None

    def get_execution_payload(self, job_id: str) -> Dict[str, Any]:
        with self.db.get_session() as session:
            row = self._row(session, job_id)
            return {
                "universe": self._loads(row.universe_json, []),
                "snapshot_dates": self._loads(row.snapshot_dates_json, []),
            }

    def list_recent(self, *, limit: int = 20) -> List[Dict[str, Any]]:
        with self.db.get_session() as session:
            rows = session.execute(
                select(StockSelectionFactorIngestionJob)
                .order_by(desc(StockSelectionFactorIngestionJob.created_at))
                .limit(limit)
            ).scalars().all()
            return [self._to_dict(row) for row in rows]

    def begin(self, job_id: str, *, task_id: Optional[str], force: bool = False) -> Dict[str, Any]:
        with self.db.get_session() as session:
            row = self._row(session, job_id)
            if row.status == "completed":
                return self._to_dict(row)
            if row.status == "processing" and not force:
                raise ValueError("full-market ingestion job is already processing")
            now = utc_naive_now()
            row.status = "processing"
            row.task_id = task_id
            row.error = None
            row.started_at = row.started_at or now
            row.heartbeat_at = now
            row.updated_at = now
            session.commit()
            session.refresh(row)
            return self._to_dict(row)

    def checkpoint(
        self,
        job_id: str,
        *,
        task_id: Optional[str],
        next_offset: int,
        batch_result: Dict[str, Any],
    ) -> Dict[str, Any]:
        with self.db.get_session() as session:
            row = self._row(session, job_id)
            self._assert_lease(row, task_id)
            errors = self._loads(row.errors_json, [])
            errors.extend(list(batch_result.get("errors") or []))
            row.next_offset = next_offset
            row.completed_batches += 1
            row.row_count += int(batch_result.get("row_count") or 0)
            row.inserted_count += int(batch_result.get("inserted") or 0)
            row.updated_count += int(batch_result.get("updated") or 0)
            row.source_error_count += int(batch_result.get("error_count") or 0)
            result = self._loads(row.result_json, {})
            for key in (
                "corporate_action_count",
                "corporate_action_inserted",
                "corporate_action_updated",
            ):
                result[key] = int(result.get(key) or 0) + int(batch_result.get(key) or 0)
            row.result_json = self._dumps(result)
            row.errors_json = self._dumps(errors[-1000:])
            row.heartbeat_at = utc_naive_now()
            row.updated_at = row.heartbeat_at
            session.commit()
            session.refresh(row)
            return self._to_dict(row)

    def complete(self, job_id: str, *, task_id: Optional[str]) -> Dict[str, Any]:
        with self.db.get_session() as session:
            row = self._row(session, job_id)
            self._assert_lease(row, task_id)
            now = utc_naive_now()
            row.status = "completed"
            row.completed_at = now
            row.heartbeat_at = now
            row.updated_at = now
            result = self._loads(row.result_json, {})
            result.update({
                "row_count": row.row_count,
                "inserted": row.inserted_count,
                "updated": row.updated_count,
                "error_count": row.source_error_count,
            })
            row.result_json = self._dumps(result)
            session.commit()
            session.refresh(row)
            return self._to_dict(row)

    def fail(self, job_id: str, error: str, *, task_id: Optional[str]) -> Dict[str, Any]:
        with self.db.get_session() as session:
            row = self._row(session, job_id)
            if row.task_id != task_id:
                return self._to_dict(row)
            row.status = "failed"
            row.error = str(error)[:4000]
            row.heartbeat_at = utc_naive_now()
            row.updated_at = row.heartbeat_at
            session.commit()
            session.refresh(row)
            return self._to_dict(row)

    @staticmethod
    def _row(session: Any, job_id: str) -> StockSelectionFactorIngestionJob:
        row = session.execute(
            select(StockSelectionFactorIngestionJob).where(
                StockSelectionFactorIngestionJob.job_id == job_id
            )
        ).scalar_one_or_none()
        if row is None:
            raise ValueError("full-market ingestion job does not exist")
        return row

    @staticmethod
    def _assert_lease(row: StockSelectionFactorIngestionJob, task_id: Optional[str]) -> None:
        if row.status != "processing" or row.task_id != task_id:
            raise RuntimeError("full-market ingestion job lease was replaced")

    @classmethod
    def _to_dict(cls, row: StockSelectionFactorIngestionJob) -> Dict[str, Any]:
        total = int(row.total_work_items or 0)
        offset = int(row.next_offset or 0)
        return {
            "job_id": row.job_id,
            "market": row.market,
            "snapshot_dates": cls._loads(row.snapshot_dates_json, []),
            "universe_source": row.universe_source,
            "status": row.status,
            "batch_size": row.batch_size,
            "total_symbols": int(row.total_symbols or 0),
            "total_work_items": total,
            "next_offset": offset,
            "remaining_work_items": max(0, total - offset),
            "progress_pct": round(offset / total * 100, 4) if total else 0.0,
            "completed_batches": row.completed_batches,
            "row_count": row.row_count,
            "inserted": row.inserted_count,
            "updated": row.updated_count,
            "source_error_count": row.source_error_count,
            "errors": cls._loads(row.errors_json, []),
            "result": cls._loads(row.result_json, {}),
            "task_id": row.task_id,
            "error": row.error,
            "started_at": row.started_at,
            "heartbeat_at": row.heartbeat_at,
            "completed_at": row.completed_at,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }

    @staticmethod
    def _dumps(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)

    @staticmethod
    def _loads(value: Optional[str], default: Any) -> Any:
        try:
            return json.loads(value or "")
        except (TypeError, ValueError):
            return default
