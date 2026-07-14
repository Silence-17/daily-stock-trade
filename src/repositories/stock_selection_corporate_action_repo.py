# -*- coding: utf-8 -*-
"""Persistence for historical stock-selection corporate actions."""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, Iterable, List, Optional, Sequence

from sqlalchemy import select

from src.storage import DatabaseManager, StockSelectionCorporateAction, utc_naive_now


class StockSelectionCorporateActionRepository:
    """Store one normalized action per market, symbol, date, and action type."""

    def __init__(self, db_manager: Optional[DatabaseManager] = None) -> None:
        self.db = db_manager or DatabaseManager.get_instance()

    def upsert_many(self, *, market: str, rows: Iterable[Dict[str, Any]]) -> Dict[str, int]:
        market_value = str(market or "").strip().lower()
        if not market_value:
            raise ValueError("market is required")
        inserted = 0
        updated = 0
        with self.db.get_session() as session:
            for raw in rows:
                payload = dict(raw or {})
                symbol = str(payload.get("symbol") or "").strip().upper()
                effective_date = payload.get("effective_date")
                action_type = str(payload.get("action_type") or "").strip().lower()
                if not symbol or not isinstance(effective_date, date):
                    raise ValueError("corporate action requires symbol and effective_date")
                if action_type not in {"cash_dividend", "split_adjustment"}:
                    raise ValueError("unsupported corporate action type")
                existing = session.execute(
                    select(StockSelectionCorporateAction).where(
                        StockSelectionCorporateAction.market == market_value,
                        StockSelectionCorporateAction.symbol == symbol,
                        StockSelectionCorporateAction.effective_date == effective_date,
                        StockSelectionCorporateAction.action_type == action_type,
                    )
                ).scalar_one_or_none()
                values = {
                    "cash_dividend_per_share": payload.get("cash_dividend_per_share"),
                    "split_ratio": payload.get("split_ratio"),
                    "source": str(payload.get("source") or "unknown"),
                    "source_record_key": str(payload.get("source_record_key") or "")[:160],
                    "updated_at": utc_naive_now(),
                }
                if existing is None:
                    session.add(StockSelectionCorporateAction(
                        market=market_value,
                        symbol=symbol,
                        effective_date=effective_date,
                        action_type=action_type,
                        **values,
                    ))
                    inserted += 1
                else:
                    for key, value in values.items():
                        setattr(existing, key, value)
                    updated += 1
            session.commit()
        return {"inserted": inserted, "updated": updated, "total": inserted + updated}

    def list_range(
        self,
        *,
        market: str,
        date_from: date,
        date_to: date,
        symbols: Optional[Sequence[str]] = None,
    ) -> List[Dict[str, Any]]:
        query = select(StockSelectionCorporateAction).where(
            StockSelectionCorporateAction.market == str(market or "").strip().lower(),
            StockSelectionCorporateAction.effective_date >= date_from,
            StockSelectionCorporateAction.effective_date <= date_to,
        )
        normalized_symbols = sorted({str(item).strip().upper() for item in symbols or [] if str(item).strip()})
        if normalized_symbols:
            query = query.where(StockSelectionCorporateAction.symbol.in_(normalized_symbols))
        query = query.order_by(
            StockSelectionCorporateAction.effective_date.asc(),
            StockSelectionCorporateAction.symbol.asc(),
            StockSelectionCorporateAction.action_type.asc(),
        )
        with self.db.get_session() as session:
            rows = session.execute(query).scalars().all()
            return [self._to_dict(row) for row in rows]

    @staticmethod
    def _to_dict(row: StockSelectionCorporateAction) -> Dict[str, Any]:
        return {
            "id": row.id,
            "market": row.market,
            "symbol": row.symbol,
            "effective_date": row.effective_date,
            "action_type": row.action_type,
            "cash_dividend_per_share": row.cash_dividend_per_share,
            "split_ratio": row.split_ratio,
            "source": row.source,
            "source_record_key": row.source_record_key,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }
