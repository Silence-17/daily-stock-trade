# -*- coding: utf-8 -*-
"""Resumable batching for full-market point-in-time factor ingestion."""

from __future__ import annotations

import math
import uuid
from datetime import date
from typing import Any, Callable, Dict, Iterable, List, Optional

from src.repositories.stock_selection_factor_ingestion_job_repo import (
    StockSelectionFactorIngestionJobRepository,
)
from src.services.stock_selection_factor_ingestion_service import (
    StockSelectionFactorIngestionService,
)
from src.services.stock_selection_historical_universe_service import (
    StockSelectionHistoricalUniverseService,
)


class StockSelectionFullMarketIngestionService:
    """Freeze a dated universe and checkpoint each completed factor batch."""

    def __init__(
        self,
        *,
        repository: Optional[StockSelectionFactorIngestionJobRepository] = None,
        universe_service: Optional[StockSelectionHistoricalUniverseService] = None,
        ingestion_factory: Optional[Callable[[], StockSelectionFactorIngestionService]] = None,
        progress_callback: Optional[Callable[[int, str], None]] = None,
    ) -> None:
        self.repository = repository or StockSelectionFactorIngestionJobRepository()
        self.universe_service = universe_service or StockSelectionHistoricalUniverseService()
        self.ingestion_factory = ingestion_factory or StockSelectionFactorIngestionService
        self.progress_callback = progress_callback

    def create(
        self,
        *,
        market: str,
        snapshot_dates: Iterable[date],
        batch_size: int = 25,
    ) -> Dict[str, Any]:
        dates = sorted(set(snapshot_dates))
        if not dates or len(dates) > 50:
            raise ValueError("snapshot_dates must contain between 1 and 50 dates")
        if not 1 <= batch_size <= 50:
            raise ValueError("batch_size must be between 1 and 50")
        resolved_dates = self.universe_service.resolve_many(
            market=market,
            snapshot_dates=dates,
            limit=6000,
        )
        universe = []
        universe_sources = set()
        for resolved in resolved_dates:
            if resolved.get("truncated"):
                raise RuntimeError("historical universe exceeds the supported 6000-symbol safety limit")
            snapshot_date = str(resolved.get("snapshot_date") or "")
            methodology = resolved.get("methodology") or {}
            universe_sources.add(str(methodology.get("source") or "unknown"))
            universe.extend(
                {**item, "snapshot_date": snapshot_date}
                for item in list(resolved.get("items") or [])
            )
        if not universe:
            raise RuntimeError("historical universe is empty")
        universe_source = "+".join(sorted(universe_sources))
        return self.repository.create(
            job_id=uuid.uuid4().hex,
            market=str(market).lower(),
            snapshot_dates=dates,
            universe=universe,
            batch_size=batch_size,
            universe_source=universe_source,
        )

    def run(self, job_id: str, *, task_id: Optional[str] = None, force: bool = False) -> Dict[str, Any]:
        job = self.repository.begin(job_id, task_id=task_id, force=force)
        if job["status"] == "completed":
            return job
        stored = self.repository.get_execution_payload(job_id)
        universe = stored["universe"]
        total = len(universe)
        try:
            while job["next_offset"] < total:
                start = job["next_offset"]
                batch_date = universe[start]["snapshot_date"]
                date_end = start
                while date_end < total and universe[date_end]["snapshot_date"] == batch_date:
                    date_end += 1
                end = min(date_end, start + job["batch_size"])
                batch = universe[start:end]
                if self.progress_callback:
                    progress = max(1, min(99, math.floor(start / total * 100)))
                    self.progress_callback(progress, f"processing symbols {start + 1}-{end} of {total}")
                result = self.ingestion_factory().ingest(
                    market=job["market"],
                    snapshot_dates=[date.fromisoformat(batch_date)],
                    universe=[
                        {key: value for key, value in item.items() if key != "snapshot_date"}
                        for item in batch
                    ],
                )
                job = self.repository.checkpoint(
                    job_id,
                    task_id=task_id,
                    next_offset=end,
                    batch_result=result,
                )
            if self.progress_callback:
                self.progress_callback(99, "all historical factor batches are checkpointed")
            return self.repository.complete(job_id, task_id=task_id)
        except Exception as exc:
            self.repository.fail(job_id, str(exc), task_id=task_id)
            raise

    def get(self, job_id: str) -> Dict[str, Any]:
        job = self.repository.get(job_id)
        if job is None:
            raise ValueError("full-market ingestion job does not exist")
        return job

    def list_recent(self, *, limit: int = 20) -> List[Dict[str, Any]]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        return self.repository.list_recent(limit=limit)
