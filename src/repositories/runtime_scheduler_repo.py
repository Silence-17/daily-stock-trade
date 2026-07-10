# -*- coding: utf-8 -*-
"""Repository helpers for runtime scheduler background task events."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import delete, desc, select

from src.storage import DatabaseManager, RuntimeSchedulerTaskEvent

logger = logging.getLogger(__name__)


class RuntimeSchedulerRepository:
    """Persist and read runtime scheduler task events."""

    def __init__(self, db_manager: Optional[DatabaseManager] = None) -> None:
        self.db = db_manager or DatabaseManager.get_instance()

    def record_task_event(
        self,
        *,
        name: str,
        status: str,
        message: str,
        details: Optional[Dict[str, Any]] = None,
        duration_seconds: Optional[float] = None,
        timestamp: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        event_time = timestamp or datetime.now()
        with self.db.get_session() as session:
            row = RuntimeSchedulerTaskEvent(
                name=str(name),
                status=str(status),
                message=str(message or ""),
                details_json=self._json_dumps(details or {}),
                duration_seconds=duration_seconds,
                timestamp=event_time,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return self._event_to_dict(row)

    def list_task_events(
        self,
        *,
        name: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        try:
            safe_limit = max(1, min(100, int(limit)))
        except (TypeError, ValueError):
            safe_limit = 50
        name_filter = str(name or "").strip()
        status_filter = str(status or "").strip()
        query = select(RuntimeSchedulerTaskEvent)
        if name_filter:
            query = query.where(RuntimeSchedulerTaskEvent.name == name_filter)
        if status_filter:
            query = query.where(RuntimeSchedulerTaskEvent.status == status_filter)
        query = query.order_by(
            desc(RuntimeSchedulerTaskEvent.timestamp),
            desc(RuntimeSchedulerTaskEvent.id),
        ).limit(safe_limit)
        with self.db.get_session() as session:
            rows = session.execute(query).scalars().all()
        return [self._event_to_dict(row) for row in reversed(rows)]

    def cleanup_task_events(self, *, older_than: datetime) -> int:
        """Delete persisted task events older than the supplied cutoff."""
        cutoff = older_than
        with self.db.get_session() as session:
            result = session.execute(
                delete(RuntimeSchedulerTaskEvent).where(
                    RuntimeSchedulerTaskEvent.timestamp < cutoff,
                )
            )
            session.commit()
            return int(result.rowcount or 0)

    @staticmethod
    def _json_dumps(value: Any) -> str:
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        except TypeError:
            logger.warning("Runtime scheduler event details are not JSON serializable; storing string fallback")
            return json.dumps({"raw": str(value)}, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def _json_loads(raw: Optional[str]) -> Dict[str, Any]:
        if not raw:
            return {}
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    @classmethod
    def _event_to_dict(cls, row: RuntimeSchedulerTaskEvent) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "id": row.id,
            "name": row.name,
            "status": row.status,
            "message": row.message,
            "timestamp": row.timestamp.isoformat() if row.timestamp is not None else None,
            "details": cls._json_loads(row.details_json),
        }
        if row.duration_seconds is not None:
            payload["duration_seconds"] = row.duration_seconds
        return payload
