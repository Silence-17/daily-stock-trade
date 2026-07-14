# -*- coding: utf-8 -*-
"""Persistence for point-in-time stock-selection factor snapshots."""

from __future__ import annotations

import json
from datetime import date
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import delete, select

from src.storage import DatabaseManager, StockSelectionFactorSnapshot, utc_naive_now


class StockSelectionFactorSnapshotRepository:
    """Store one normalized factor row per market, symbol, and trading date."""

    DIRECT_FIELDS = {
        "name",
        "industry",
        "price",
        "change_pct",
        "amount",
        "volume",
        "turnover_rate",
        "volume_ratio",
        "pe_ratio",
        "pb_ratio",
        "total_mv",
    }
    RESERVED_FIELDS = DIRECT_FIELDS | {
        "id",
        "market",
        "symbol",
        "code",
        "snapshot_date",
        "factors",
        "source",
        "quality_status",
        "missing_fields",
        "created_at",
        "updated_at",
    }

    def __init__(self, db_manager: Optional[DatabaseManager] = None) -> None:
        self.db = db_manager or DatabaseManager.get_instance()

    def upsert_many(
        self,
        *,
        market: str,
        snapshot_date: date,
        rows: Iterable[Dict[str, Any]],
    ) -> Dict[str, int]:
        market_value = str(market or "cn").strip().lower()
        if not market_value:
            raise ValueError("market is required")
        if not isinstance(snapshot_date, date):
            raise ValueError("snapshot_date must be a date")

        inserted = 0
        updated = 0
        with self.db.get_session() as session:
            for raw in rows:
                payload = dict(raw or {})
                symbol = str(payload.get("symbol") or payload.get("code") or "").strip()
                if not symbol:
                    raise ValueError("snapshot row symbol is required")
                existing = session.execute(
                    select(StockSelectionFactorSnapshot).where(
                        StockSelectionFactorSnapshot.market == market_value,
                        StockSelectionFactorSnapshot.symbol == symbol,
                        StockSelectionFactorSnapshot.snapshot_date == snapshot_date,
                    )
                ).scalar_one_or_none()
                direct = {key: payload.get(key) for key in self.DIRECT_FIELDS}
                factors = {
                    key: value
                    for key, value in dict(payload.get("factors") or {}).items()
                    if key not in self.RESERVED_FIELDS
                }
                for key, value in payload.items():
                    if key not in self.RESERVED_FIELDS:
                        factors[key] = value
                values = {
                    **direct,
                    "factors_json": self._dumps(factors),
                    "source_json": self._dumps(payload.get("source") or {}),
                    "quality_status": str(payload.get("quality_status") or "unknown"),
                    "missing_fields_json": self._dumps(payload.get("missing_fields") or []),
                    "updated_at": utc_naive_now(),
                }
                if existing is None:
                    session.add(
                        StockSelectionFactorSnapshot(
                            market=market_value,
                            symbol=symbol,
                            snapshot_date=snapshot_date,
                            **values,
                        )
                    )
                    inserted += 1
                else:
                    for key, value in values.items():
                        setattr(existing, key, value)
                    updated += 1
            session.commit()
        return {"inserted": inserted, "updated": updated, "total": inserted + updated}

    def list_for_date(self, *, market: str, snapshot_date: date) -> List[Dict[str, Any]]:
        with self.db.get_session() as session:
            rows = session.execute(
                select(StockSelectionFactorSnapshot)
                .where(
                    StockSelectionFactorSnapshot.market == str(market or "cn").strip().lower(),
                    StockSelectionFactorSnapshot.snapshot_date == snapshot_date,
                )
                .order_by(StockSelectionFactorSnapshot.symbol.asc())
            ).scalars().all()
            return [self._to_dict(row) for row in rows]

    def list_dates(
        self,
        *,
        market: str,
        date_from: Optional[date] = None,
        date_to: Optional[date] = None,
    ) -> List[date]:
        query = select(StockSelectionFactorSnapshot.snapshot_date).where(
            StockSelectionFactorSnapshot.market == str(market or "cn").strip().lower()
        )
        if date_from is not None:
            query = query.where(StockSelectionFactorSnapshot.snapshot_date >= date_from)
        if date_to is not None:
            query = query.where(StockSelectionFactorSnapshot.snapshot_date <= date_to)
        with self.db.get_session() as session:
            statement = query.distinct().order_by(StockSelectionFactorSnapshot.snapshot_date)
            return list(session.execute(statement).scalars())

    def delete_date(self, *, market: str, snapshot_date: date) -> int:
        with self.db.get_session() as session:
            result = session.execute(
                delete(StockSelectionFactorSnapshot).where(
                    StockSelectionFactorSnapshot.market == str(market or "cn").strip().lower(),
                    StockSelectionFactorSnapshot.snapshot_date == snapshot_date,
                )
            )
            session.commit()
            return int(result.rowcount or 0)

    @classmethod
    def _to_dict(cls, row: StockSelectionFactorSnapshot) -> Dict[str, Any]:
        payload = {
            "id": row.id,
            "market": row.market,
            "symbol": row.symbol,
            "snapshot_date": row.snapshot_date,
            **{key: getattr(row, key) for key in cls.DIRECT_FIELDS},
            "quality_status": row.quality_status,
            "missing_fields": cls._loads(row.missing_fields_json, []),
            "source": cls._loads(row.source_json, {}),
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }
        payload.update(cls._loads(row.factors_json, {}))
        return payload

    @staticmethod
    def _dumps(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)

    @staticmethod
    def _loads(value: Optional[str], default: Any) -> Any:
        try:
            return json.loads(value or "")
        except (TypeError, ValueError):
            return default
