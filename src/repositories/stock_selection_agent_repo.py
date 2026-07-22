# -*- coding: utf-8 -*-
"""Repository helpers for stock-selection agent audit records."""

from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import desc, func, select

from src.storage import (
    DatabaseManager,
    StockSelectionAgentDecision,
    StockSelectionAgentRun,
    StockSelectionAgentRunFeedback,
    StockSelectionAgentTradePlan,
)

logger = logging.getLogger(__name__)


class StockSelectionAgentRepository:
    """Persist and read stock-selection agent runs and candidate decisions."""

    _DECISION_AUDIT_COLUMNS = {
        "strategy_evidence": "strategy_evidence_json",
        "position_plan": "position_plan_json",
        "risk_review": "risk_review_json",
        "agent_review": "agent_review_json",
        "llm_review": "llm_review_json",
    }

    def __init__(self, db_manager: Optional[DatabaseManager] = None) -> None:
        self.db = db_manager or DatabaseManager.get_instance()

    def create_run(
        self,
        *,
        run_uid: str,
        trigger_source: str,
        strategy: str,
        market: str,
        max_results: Optional[int] = None,
        cash_per_order: Optional[float] = None,
        min_score: Optional[float] = None,
        skip_existing_positions: bool = True,
        settings: Optional[Dict[str, Any]] = None,
        diagnostics: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        with self.db.get_session() as session:
            row = StockSelectionAgentRun(
                run_uid=run_uid,
                trigger_source=trigger_source,
                status="running",
                strategy=strategy,
                market=market,
                max_results=max_results,
                cash_per_order=cash_per_order,
                min_score=min_score,
                skip_existing_positions=bool(skip_existing_positions),
                settings_json=self._json_dumps(settings or {}),
                diagnostics_json=self._json_dumps(diagnostics or {}),
                started_at=datetime.now(),
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return self._run_to_dict(row)

    def complete_run(
        self,
        *,
        run_id: int,
        status: str,
        candidate_count: int,
        submitted_count: int,
        skipped_count: int,
        planned_count: int = 0,
        message_count: int = 0,
        error: Optional[str] = None,
        diagnostics: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        with self.db.get_session() as session:
            row = session.get(StockSelectionAgentRun, int(run_id))
            if row is None:
                return None
            row.status = status
            row.candidate_count = int(candidate_count or 0)
            row.planned_count = int(planned_count or 0)
            row.submitted_count = int(submitted_count or 0)
            row.skipped_count = int(skipped_count or 0)
            row.message_count = int(message_count or 0)
            row.error = error
            row.completed_at = datetime.now()
            row.updated_at = datetime.now()

            diagnostics_payload = (
                dict(diagnostics)
                if isinstance(diagnostics, dict)
                else self._json_loads(row.diagnostics_json, {})
            )
            decisions = session.execute(
                select(StockSelectionAgentDecision)
                .where(StockSelectionAgentDecision.run_id == row.id)
                .order_by(StockSelectionAgentDecision.id.asc())
            ).scalars().all()
            plans = session.execute(
                select(StockSelectionAgentTradePlan)
                .where(StockSelectionAgentTradePlan.run_id == row.id)
                .order_by(StockSelectionAgentTradePlan.id.asc())
            ).scalars().all()
            run_payload = self._run_to_dict(row)
            run_payload["diagnostics"] = diagnostics_payload
            diagnostics_payload["agent_summary"] = self._build_agent_summary(
                run_payload,
                [self._decision_to_dict(item) for item in decisions],
                [self._trade_plan_to_dict(item) for item in plans],
            )
            row.diagnostics_json = self._json_dumps(diagnostics_payload)
            session.commit()
            session.refresh(row)
            return self._run_to_dict(row)

    def record_decision(
        self,
        *,
        run_id: int,
        sequence: int,
        symbol: Optional[str],
        market: str,
        action: str,
        status: str,
        reason: Optional[str] = None,
        name: Optional[str] = None,
        score: Optional[float] = None,
        confidence: Optional[float] = None,
        cash_amount: Optional[float] = None,
        quantity: Optional[float] = None,
        price: Optional[float] = None,
        trade_id: Optional[int] = None,
        rationale: Optional[str] = None,
        risk_flags: Optional[List[str]] = None,
        order_result: Optional[Dict[str, Any]] = None,
        raw_candidate: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        with self.db.get_session() as session:
            order_result_payload = order_result or {}
            row = StockSelectionAgentDecision(
                run_id=int(run_id),
                sequence=int(sequence),
                symbol=symbol,
                name=name,
                market=market,
                action=action,
                status=status,
                reason=reason,
                score=score,
                confidence=confidence,
                cash_amount=cash_amount,
                quantity=quantity,
                price=price,
                trade_id=trade_id,
                rationale=rationale,
                risk_flags_json=self._json_dumps(risk_flags or []),
                order_result_json=self._json_dumps(order_result_payload),
                raw_candidate_json=self._json_dumps(raw_candidate or {}),
            )
            self._sync_decision_audit_columns(
                row,
                order_result_payload,
                clear_missing=True,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return self._decision_to_dict(row)

    def record_trade_plan(
        self,
        *,
        plan_uid: str,
        run_id: int,
        decision_id: Optional[int],
        symbol: Optional[str],
        market: str,
        side: str,
        status: str,
        execution_mode: str,
        name: Optional[str] = None,
        planned_cash_amount: Optional[float] = None,
        planned_quantity: Optional[float] = None,
        planned_price: Optional[float] = None,
        submitted_quantity: Optional[float] = None,
        submitted_price: Optional[float] = None,
        trade_id: Optional[int] = None,
        skip_reason: Optional[str] = None,
        risk_flags: Optional[List[str]] = None,
        order_result: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        with self.db.get_session() as session:
            row = StockSelectionAgentTradePlan(
                plan_uid=plan_uid,
                run_id=int(run_id),
                decision_id=int(decision_id) if decision_id is not None else None,
                symbol=symbol,
                name=name,
                market=market,
                side=side,
                status=status,
                execution_mode=execution_mode,
                planned_cash_amount=planned_cash_amount,
                planned_quantity=planned_quantity,
                planned_price=planned_price,
                submitted_quantity=submitted_quantity,
                submitted_price=submitted_price,
                trade_id=trade_id,
                skip_reason=skip_reason,
                risk_flags_json=self._json_dumps(risk_flags or []),
                order_result_json=self._json_dumps(order_result or {}),
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return self._trade_plan_to_dict(row)

    def list_forward_evaluation_decisions(
        self,
        *,
        strategy: Optional[str] = None,
        market: Optional[str] = None,
        trigger_source: Optional[str] = None,
        run_status: Optional[str] = None,
        created_from: Optional[datetime] = None,
        created_to: Optional[datetime] = None,
        include_skipped: bool = True,
        limit: int = 500,
    ) -> Dict[str, Any]:
        """Return persisted buy intents, including audited skips when requested."""

        limit = max(1, min(2000, int(limit or 500)))
        strategy_value = str(strategy or "").strip()
        market_value = str(market or "").strip().lower()
        trigger_source_value = str(trigger_source or "").strip()
        run_status_value = str(run_status or "").strip()
        created_from_norm = self._datetime_filter_value(created_from)
        created_to_norm = self._datetime_filter_value(created_to)

        with self.db.get_session() as session:
            query = (
                select(StockSelectionAgentDecision, StockSelectionAgentRun)
                .join(StockSelectionAgentRun, StockSelectionAgentRun.id == StockSelectionAgentDecision.run_id)
                .where(StockSelectionAgentDecision.symbol.is_not(None))
            )
            if include_skipped:
                query = query.where(StockSelectionAgentDecision.action.in_(["buy", "skip"]))
            else:
                query = query.where(StockSelectionAgentDecision.action == "buy")
            if strategy_value:
                query = query.where(StockSelectionAgentRun.strategy == strategy_value)
            if market_value:
                query = query.where(StockSelectionAgentDecision.market == market_value)
            if trigger_source_value:
                query = query.where(
                    StockSelectionAgentRun.trigger_source == trigger_source_value
                )
            if run_status_value:
                query = query.where(StockSelectionAgentRun.status == run_status_value)
            if created_from_norm is not None:
                query = query.where(StockSelectionAgentDecision.created_at >= created_from_norm)
            if created_to_norm is not None:
                query = query.where(StockSelectionAgentDecision.created_at <= created_to_norm)
            if not include_skipped:
                query = query.where(
                    StockSelectionAgentDecision.status.in_(
                        ["planned", "submitted", "part_filled", "filled", "executed"]
                    )
                )

            total = int(session.execute(select(func.count()).select_from(query.subquery())).scalar_one() or 0)
            rows = session.execute(
                query.order_by(
                    desc(StockSelectionAgentDecision.created_at),
                    desc(StockSelectionAgentDecision.id),
                ).limit(limit)
            ).all()
            items = []
            for decision, run in rows:
                payload = self._decision_to_dict(decision)
                payload["run_uid"] = run.run_uid
                payload["strategy"] = run.strategy
                payload["trigger_source"] = run.trigger_source
                payload["run_status"] = run.status
                payload["run_created_at"] = run.created_at
                items.append(payload)
            return {
                "items": items,
                "total": total,
                "scanned_count": len(items),
                "truncated": total > len(items),
                "limit": limit,
            }

    def get_trade_plan(self, plan_uid: str) -> Optional[Dict[str, Any]]:
        with self.db.get_session() as session:
            row = session.execute(
                select(StockSelectionAgentTradePlan)
                .where(StockSelectionAgentTradePlan.plan_uid == str(plan_uid))
                .limit(1)
            ).scalar_one_or_none()
            if row is None:
                return None
            return self._trade_plan_to_dict(row)

    def list_trade_plans(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        statuses: Optional[List[str]] = None,
        execution_modes: Optional[List[str]] = None,
        created_from: Optional[datetime] = None,
        created_to: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        limit = max(1, min(500, int(limit or 100)))
        offset = max(0, int(offset or 0))
        status_values = [str(item).strip() for item in (statuses or []) if str(item or "").strip()]
        mode_values = [str(item).strip() for item in (execution_modes or []) if str(item or "").strip()]
        with self.db.get_session() as session:
            query = select(StockSelectionAgentTradePlan)
            if status_values:
                query = query.where(StockSelectionAgentTradePlan.status.in_(status_values))
            if mode_values:
                query = query.where(StockSelectionAgentTradePlan.execution_mode.in_(mode_values))
            created_from_norm = self._datetime_filter_value(created_from)
            created_to_norm = self._datetime_filter_value(created_to)
            if created_from_norm is not None:
                query = query.where(StockSelectionAgentTradePlan.created_at >= created_from_norm)
            if created_to_norm is not None:
                query = query.where(StockSelectionAgentTradePlan.created_at <= created_to_norm)
            total = int(session.execute(select(func.count()).select_from(query.subquery())).scalar_one() or 0)
            rows = session.execute(
                query.order_by(desc(StockSelectionAgentTradePlan.updated_at), desc(StockSelectionAgentTradePlan.id))
                .offset(offset)
                .limit(limit)
            ).scalars().all()
            return {
                "items": [self._trade_plan_to_dict(row) for row in rows],
                "limit": limit,
                "offset": offset,
                "total": total,
            }

    def find_trade_plan_by_vnpy_order_id(
        self,
        vt_orderid: str,
        *,
        limit: int = 500,
    ) -> Optional[Dict[str, Any]]:
        order_id = str(vt_orderid or "").strip()
        if not order_id:
            return None
        limit = max(1, min(2000, int(limit or 500)))
        with self.db.get_session() as session:
            rows = session.execute(
                select(StockSelectionAgentTradePlan)
                .where(StockSelectionAgentTradePlan.execution_mode == "vnpy_paper")
                .where(
                    StockSelectionAgentTradePlan.status.in_(
                        ["submitted", "part_filled", "cancel_requested", "filled", "failed"]
                    )
                )
                .order_by(desc(StockSelectionAgentTradePlan.updated_at), desc(StockSelectionAgentTradePlan.id))
                .limit(limit)
            ).scalars().all()
            for row in rows:
                payload = self._json_loads(row.order_result_json, {})
                if self._order_result_has_vnpy_order_id(payload, order_id):
                    return self._trade_plan_to_dict(row)
        return None

    def find_active_trade_plan(
        self,
        *,
        symbol: str,
        side: str,
        execution_mode: str = "vnpy_paper",
        statuses: Optional[List[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        symbol_norm = str(symbol or "").strip()
        side_norm = str(side or "").strip().lower()
        if not symbol_norm or not side_norm:
            return None
        status_values = [str(item).strip() for item in (statuses or []) if str(item or "").strip()]
        if not status_values:
            status_values = ["submitted", "part_filled", "cancel_requested"]
        with self.db.get_session() as session:
            row = session.execute(
                select(StockSelectionAgentTradePlan)
                .where(StockSelectionAgentTradePlan.symbol == symbol_norm)
                .where(StockSelectionAgentTradePlan.side == side_norm)
                .where(StockSelectionAgentTradePlan.execution_mode == str(execution_mode or "").strip())
                .where(StockSelectionAgentTradePlan.status.in_(status_values))
                .order_by(desc(StockSelectionAgentTradePlan.updated_at), desc(StockSelectionAgentTradePlan.id))
                .limit(1)
            ).scalar_one_or_none()
            return self._trade_plan_to_dict(row) if row is not None else None

    def update_trade_plan_execution(
        self,
        *,
        plan_uid: str,
        status: str,
        submitted_quantity: Optional[float] = None,
        submitted_price: Optional[float] = None,
        trade_id: Optional[int] = None,
        skip_reason: Optional[str] = None,
        order_result: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        with self.db.get_session() as session:
            row = session.execute(
                select(StockSelectionAgentTradePlan)
                .where(StockSelectionAgentTradePlan.plan_uid == str(plan_uid))
                .limit(1)
            ).scalar_one_or_none()
            if row is None:
                return None
            row.status = status
            row.submitted_quantity = submitted_quantity
            row.submitted_price = submitted_price
            row.trade_id = trade_id
            row.skip_reason = skip_reason
            row.order_result_json = self._json_dumps(order_result or {})
            row.updated_at = datetime.now()
            session.commit()
            session.refresh(row)
            return self._trade_plan_to_dict(row)

    def update_decision_execution(
        self,
        *,
        decision_id: int,
        status: str,
        reason: Optional[str] = None,
        quantity: Optional[float] = None,
        price: Optional[float] = None,
        trade_id: Optional[int] = None,
        order_result: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        with self.db.get_session() as session:
            row = session.get(StockSelectionAgentDecision, int(decision_id))
            if row is None:
                return None
            row.status = status
            row.reason = reason
            row.quantity = quantity
            row.price = price
            row.trade_id = trade_id
            previous_order_result = self._json_loads(row.order_result_json, {})
            order_result_payload = order_result or {}
            self._sync_decision_audit_columns(
                row,
                order_result_payload,
                previous_order_result,
            )
            row.order_result_json = self._json_dumps(order_result_payload)
            session.commit()
            session.refresh(row)
            return self._decision_to_dict(row)

    def update_trade_plan_and_decision_execution(
        self,
        *,
        plan_uid: str,
        status: str,
        submitted_quantity: Optional[float] = None,
        submitted_price: Optional[float] = None,
        trade_id: Optional[int] = None,
        skip_reason: Optional[str] = None,
        order_result: Optional[Dict[str, Any]] = None,
        expected_updated_at: Optional[datetime] = None,
    ) -> Optional[Dict[str, Any]]:
        """Update a plan and its linked decision in one database transaction."""

        with self.db.get_session() as session:
            plan = session.execute(
                select(StockSelectionAgentTradePlan)
                .where(StockSelectionAgentTradePlan.plan_uid == str(plan_uid))
                .limit(1)
            ).scalar_one_or_none()
            if plan is None:
                return None
            if (
                expected_updated_at is not None
                and plan.updated_at != expected_updated_at
            ):
                decision = (
                    session.get(
                        StockSelectionAgentDecision,
                        int(plan.decision_id),
                    )
                    if plan.decision_id is not None
                    else None
                )
                return {
                    "trade_plan": self._trade_plan_to_dict(plan),
                    "decision": (
                        self._decision_to_dict(decision)
                        if decision is not None
                        else None
                    ),
                    "stale_write_skipped": True,
                }

            payload = order_result or {}
            now = datetime.now()
            plan.status = status
            plan.submitted_quantity = submitted_quantity
            plan.submitted_price = submitted_price
            plan.trade_id = trade_id
            plan.skip_reason = skip_reason
            plan.order_result_json = self._json_dumps(payload)
            plan.updated_at = now

            decision = None
            if plan.decision_id is not None:
                decision = session.get(
                    StockSelectionAgentDecision,
                    int(plan.decision_id),
                )
            if decision is not None:
                decision.status = status
                decision.reason = skip_reason
                decision.quantity = submitted_quantity
                decision.price = submitted_price
                decision.trade_id = trade_id
                previous_order_result = self._json_loads(
                    decision.order_result_json,
                    {},
                )
                self._sync_decision_audit_columns(
                    decision,
                    payload,
                    previous_order_result,
                )
                decision.order_result_json = self._json_dumps(payload)

            session.commit()
            session.refresh(plan)
            if decision is not None:
                session.refresh(decision)
            return {
                "trade_plan": self._trade_plan_to_dict(plan),
                "decision": (
                    self._decision_to_dict(decision)
                    if decision is not None
                    else None
                ),
                "stale_write_skipped": False,
            }

    def refresh_run_trade_counts(self, run_id: int) -> Optional[Dict[str, Any]]:
        with self.db.get_session() as session:
            run = session.get(StockSelectionAgentRun, int(run_id))
            if run is None:
                return None
            counts = dict(
                session.execute(
                    select(
                        StockSelectionAgentTradePlan.status,
                        func.count(StockSelectionAgentTradePlan.id),
                    )
                    .where(StockSelectionAgentTradePlan.run_id == int(run_id))
                    .group_by(StockSelectionAgentTradePlan.status)
                ).all()
            )
            run.planned_count = int(counts.get("planned", 0))
            run.submitted_count = (
                int(counts.get("filled", 0))
                + int(counts.get("submitted", 0))
                + int(counts.get("part_filled", 0))
                + int(counts.get("cancel_requested", 0))
            )
            run.skipped_count = int(counts.get("skipped", 0)) + int(counts.get("failed", 0))
            run.updated_at = datetime.now()
            decisions = session.execute(
                select(StockSelectionAgentDecision)
                .where(StockSelectionAgentDecision.run_id == run.id)
                .order_by(StockSelectionAgentDecision.id.asc())
            ).scalars().all()
            plans = session.execute(
                select(StockSelectionAgentTradePlan)
                .where(StockSelectionAgentTradePlan.run_id == run.id)
                .order_by(StockSelectionAgentTradePlan.id.asc())
            ).scalars().all()
            diagnostics_payload = self._json_loads(run.diagnostics_json, {})
            run_payload = self._run_to_dict(run)
            run_payload["diagnostics"] = diagnostics_payload
            diagnostics_payload["agent_summary"] = self._build_agent_summary(
                run_payload,
                [self._decision_to_dict(item) for item in decisions],
                [self._trade_plan_to_dict(item) for item in plans],
            )
            run.diagnostics_json = self._json_dumps(diagnostics_payload)
            session.commit()
            session.refresh(run)
            return self._run_to_dict(run)

    def merge_run_diagnostics(self, run_uid: str, patch: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        with self.db.get_session() as session:
            run = session.execute(
                select(StockSelectionAgentRun)
                .where(StockSelectionAgentRun.run_uid == str(run_uid))
                .limit(1)
            ).scalar_one_or_none()
            if run is None:
                return None
            diagnostics_payload = self._json_loads(run.diagnostics_json, {})
            if not isinstance(diagnostics_payload, dict):
                diagnostics_payload = {}
            diagnostics_payload.update(patch or {})
            run.diagnostics_json = self._json_dumps(diagnostics_payload)
            run.updated_at = datetime.now()
            session.commit()
            session.refresh(run)
            return self._run_to_dict(run)

    def upsert_run_feedback(
        self,
        run_uid: str,
        *,
        verdict: str,
        note: Optional[str] = None,
        reviewer: Optional[str] = None,
        source: str = "web",
    ) -> Optional[Dict[str, Any]]:
        with self.db.get_session() as session:
            run = session.execute(
                select(StockSelectionAgentRun)
                .where(StockSelectionAgentRun.run_uid == str(run_uid).strip())
                .limit(1)
            ).scalar_one_or_none()
            if run is None:
                return None
            feedback = session.execute(
                select(StockSelectionAgentRunFeedback)
                .where(StockSelectionAgentRunFeedback.run_id == run.id)
                .limit(1)
            ).scalar_one_or_none()
            now = datetime.now()
            if feedback is None:
                feedback = StockSelectionAgentRunFeedback(
                    run_id=run.id,
                    created_at=now,
                )
                session.add(feedback)
            feedback.verdict = str(verdict).strip().lower()
            feedback.note = str(note).strip()[:2000] if note and str(note).strip() else None
            feedback.reviewer = (
                str(reviewer).strip()[:80]
                if reviewer and str(reviewer).strip()
                else None
            )
            feedback.source = str(source or "web").strip().lower()[:24] or "web"
            feedback.updated_at = now
            session.commit()
            session.refresh(feedback)
            return self._feedback_to_dict(feedback)

    def list_runs(
        self,
        *,
        limit: int = 20,
        offset: int = 0,
        trigger_source: Optional[str] = None,
        strategy: Optional[str] = None,
        market: Optional[str] = None,
        status: Optional[str] = None,
        created_from: Optional[datetime] = None,
        created_to: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        limit = max(1, min(100, int(limit or 20)))
        offset = max(0, int(offset or 0))
        with self.db.get_session() as session:
            query = select(StockSelectionAgentRun)
            if trigger_source:
                query = query.where(StockSelectionAgentRun.trigger_source == str(trigger_source).strip())
            if strategy:
                query = query.where(StockSelectionAgentRun.strategy == str(strategy).strip())
            if market:
                query = query.where(StockSelectionAgentRun.market == str(market).strip())
            if status:
                query = query.where(StockSelectionAgentRun.status == str(status).strip())
            created_from_norm = self._datetime_filter_value(created_from)
            created_to_norm = self._datetime_filter_value(created_to)
            if created_from_norm is not None:
                query = query.where(StockSelectionAgentRun.created_at >= created_from_norm)
            if created_to_norm is not None:
                query = query.where(StockSelectionAgentRun.created_at <= created_to_norm)
            total = int(session.execute(select(func.count()).select_from(query.subquery())).scalar_one() or 0)
            rows = session.execute(
                query.order_by(desc(StockSelectionAgentRun.created_at), desc(StockSelectionAgentRun.id))
                .offset(offset)
                .limit(limit)
            ).scalars().all()
            feedback_by_run = self._feedback_by_run_ids(session, [row.id for row in rows])
            items = []
            for row in rows:
                item = self._run_to_dict(row)
                item["human_feedback"] = feedback_by_run.get(int(row.id))
                items.append(item)
            return {
                "items": items,
                "limit": limit,
                "offset": offset,
                "total": total,
            }

    def list_recent_runs(
        self,
        *,
        trigger_source: Optional[str] = None,
        strategy: Optional[str] = None,
        market: Optional[str] = None,
        limit: int = 20,
        before_run_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        limit = max(1, min(100, int(limit or 20)))
        with self.db.get_session() as session:
            query = select(StockSelectionAgentRun)
            if trigger_source:
                query = query.where(StockSelectionAgentRun.trigger_source == str(trigger_source))
            if strategy:
                query = query.where(StockSelectionAgentRun.strategy == str(strategy))
            if market:
                query = query.where(StockSelectionAgentRun.market == str(market))
            if before_run_id is not None:
                query = query.where(StockSelectionAgentRun.id < int(before_run_id))
            rows = session.execute(
                query.order_by(desc(StockSelectionAgentRun.created_at), desc(StockSelectionAgentRun.id))
                .limit(limit)
            ).scalars().all()
            feedback_by_run = self._feedback_by_run_ids(session, [row.id for row in rows])
            items = []
            for row in rows:
                item = self._run_to_dict(row)
                item["human_feedback"] = feedback_by_run.get(int(row.id))
                items.append(item)
            return items

    def summarize_daily_runs(
        self,
        *,
        target_date: Optional[date] = None,
        limit: int = 100,
        trigger_source: Optional[str] = None,
        strategy: Optional[str] = None,
        market: Optional[str] = None,
        status: Optional[str] = None,
    ) -> Dict[str, Any]:
        limit = max(1, min(500, int(limit or 100)))
        day = target_date or datetime.now().date()
        start_at = datetime.combine(day, time.min)
        end_at = start_at + timedelta(days=1)

        with self.db.get_session() as session:
            query = select(StockSelectionAgentRun).where(
                StockSelectionAgentRun.created_at >= start_at,
                StockSelectionAgentRun.created_at < end_at,
            )
            if trigger_source:
                query = query.where(StockSelectionAgentRun.trigger_source == str(trigger_source).strip())
            if strategy:
                query = query.where(StockSelectionAgentRun.strategy == str(strategy).strip())
            if market:
                query = query.where(StockSelectionAgentRun.market == str(market).strip())
            if status:
                query = query.where(StockSelectionAgentRun.status == str(status).strip())
            total = int(session.execute(select(func.count()).select_from(query.subquery())).scalar_one() or 0)
            rows = session.execute(
                query.order_by(desc(StockSelectionAgentRun.created_at), desc(StockSelectionAgentRun.id))
                .limit(limit)
            ).scalars().all()
            feedback_by_run = self._feedback_by_run_ids(session, [row.id for row in rows])
            runs = []
            for row in rows:
                item = self._run_to_dict(row)
                item["human_feedback"] = feedback_by_run.get(int(row.id))
                runs.append(item)

        status_counts: Counter[str] = Counter()
        strategy_counts: Counter[str] = Counter()
        market_counts: Counter[str] = Counter()
        execution_mode_counts: Counter[str] = Counter()
        data_quality_counts: Counter[str] = Counter()
        agent_review_counts: Counter[str] = Counter()
        llm_review_counts: Counter[str] = Counter()
        workflow_status_counts: Counter[str] = Counter()
        workflow_stage_counts: Counter[str] = Counter()
        review_quality_counts: Counter[str] = Counter()
        review_quality_flag_counts: Counter[str] = Counter()
        human_feedback_counts: Counter[str] = Counter()
        trade_plan_status_counts: Counter[str] = Counter()
        side_counts: Counter[str] = Counter()
        skip_reason_counts: Counter[str] = Counter()
        symbol_counts: Counter[str] = Counter()
        review_quality_score_total = 0.0
        review_quality_score_count = 0

        candidate_count = 0
        planned_count = 0
        submitted_count = 0
        skipped_count = 0
        message_count = 0
        latest_run = runs[0] if runs else None

        for run in runs:
            status = str(run.get("status") or "unknown")
            run_strategy = str(run.get("strategy") or "unknown")
            run_market = str(run.get("market") or "unknown")
            status_counts[status] += 1
            strategy_counts[run_strategy] += 1
            market_counts[run_market] += 1
            candidate_count += int(run.get("candidate_count") or 0)
            planned_count += int(run.get("planned_count") or 0)
            submitted_count += int(run.get("submitted_count") or 0)
            skipped_count += int(run.get("skipped_count") or 0)
            message_count += int(run.get("message_count") or 0)
            human_feedback = (
                run.get("human_feedback")
                if isinstance(run.get("human_feedback"), dict)
                else {}
            )
            feedback_verdict = str(human_feedback.get("verdict") or "").strip().lower()
            if feedback_verdict:
                human_feedback_counts[feedback_verdict] += 1

            diagnostics = run.get("diagnostics") if isinstance(run.get("diagnostics"), dict) else {}
            settings = run.get("settings") if isinstance(run.get("settings"), dict) else {}
            agent_plan = diagnostics.get("agent_plan") if isinstance(diagnostics.get("agent_plan"), dict) else {}
            agent_summary = diagnostics.get("agent_summary") if isinstance(diagnostics.get("agent_summary"), dict) else {}
            execution_mode = (
                agent_plan.get("execution_mode")
                or diagnostics.get("execution_mode")
                or settings.get("auto_execution_mode")
                or "unknown"
            )
            execution_mode_counts[str(execution_mode)] += 1
            data_quality = diagnostics.get("data_quality")
            data_quality_status = (
                data_quality.get("status")
                if isinstance(data_quality, dict)
                else agent_summary.get("data_quality_status")
            )
            if data_quality_status:
                data_quality_counts[str(data_quality_status)] += 1

            detail = self.get_run_detail(str(run.get("run_uid") or ""))
            if not isinstance(detail, dict):
                continue
            detail_diagnostics = detail.get("diagnostics") if isinstance(detail.get("diagnostics"), dict) else {}
            agent_workflow = (
                detail_diagnostics.get("agent_workflow")
                if isinstance(detail_diagnostics.get("agent_workflow"), dict)
                else {}
            )
            detail_summary = (
                detail_diagnostics.get("agent_summary")
                if isinstance(detail_diagnostics.get("agent_summary"), dict)
                else {}
            )
            review_quality = (
                detail_summary.get("review_quality")
                if isinstance(detail_summary.get("review_quality"), dict)
                else {}
            )
            review_quality_status = str(review_quality.get("status") or "").strip()
            if review_quality_status:
                review_quality_counts[review_quality_status] += 1
            review_quality_score = self._safe_float(review_quality.get("score"))
            if review_quality_score is not None:
                review_quality_score_total += review_quality_score
                review_quality_score_count += 1
            for flag in list(review_quality.get("risk_flags") or []):
                flag_text = str(flag or "").strip()
                if flag_text:
                    review_quality_flag_counts[flag_text] += 1
            workflow_status = str(agent_workflow.get("status") or "").strip()
            if workflow_status:
                workflow_status_counts[workflow_status] += 1
            workflow_stage = str(agent_workflow.get("current_stage") or "").strip()
            if workflow_stage:
                workflow_stage_counts[workflow_stage] += 1
            trade_plans = [item for item in list(detail.get("trade_plans") or []) if isinstance(item, dict)]
            decisions = [item for item in list(detail.get("decisions") or []) if isinstance(item, dict)]
            for plan in trade_plans:
                plan_status = str(plan.get("status") or "unknown")
                trade_plan_status_counts[plan_status] += 1
                side_counts[str(plan.get("side") or "unknown")] += 1
                reason = str(plan.get("skip_reason") or "").strip()
                if reason:
                    skip_reason_counts[reason] += 1
                symbol = str(plan.get("symbol") or "").strip()
                if symbol:
                    symbol_counts[symbol] += 1
                order_result = plan.get("order_result")
                if isinstance(order_result, dict):
                    agent_review = order_result.get("agent_review")
                    if isinstance(agent_review, dict):
                        agent_review_counts[str(agent_review.get("status") or "unknown")] += 1
                    llm_review = order_result.get("llm_review")
                    if isinstance(llm_review, dict):
                        llm_review_counts[str(llm_review.get("status") or "unknown")] += 1
            if not trade_plans:
                for decision in decisions:
                    reason = str(decision.get("reason") or "").strip()
                    if reason:
                        skip_reason_counts[reason] += 1
                    symbol = str(decision.get("symbol") or "").strip()
                    if symbol:
                        symbol_counts[symbol] += 1
                    order_result = decision.get("order_result")
                    if isinstance(order_result, dict):
                        agent_review = order_result.get("agent_review")
                        if isinstance(agent_review, dict):
                            agent_review_counts[str(agent_review.get("status") or "unknown")] += 1
                        llm_review = order_result.get("llm_review")
                        if isinstance(llm_review, dict):
                            llm_review_counts[str(llm_review.get("status") or "unknown")] += 1

        if total <= 0:
            health = "idle"
        elif status_counts.get("failed", 0) > 0:
            health = "error"
        elif (
            skipped_count > 0
            or any(key in data_quality_counts for key in ("stale", "unavailable"))
            or review_quality_counts.get("needs_review", 0) > 0
            or review_quality_counts.get("guarded", 0) > 0
        ):
            health = "warning"
        else:
            health = "ok"

        top_skip_reasons = [
            {"reason": reason, "count": int(count)}
            for reason, count in sorted(skip_reason_counts.items(), key=lambda item: (-item[1], item[0]))[:8]
        ]
        top_symbols = [
            {"symbol": symbol, "count": int(count)}
            for symbol, count in sorted(symbol_counts.items(), key=lambda item: (-item[1], item[0]))[:8]
        ]
        return {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "date": day.isoformat(),
            "created_from": start_at.isoformat(timespec="seconds"),
            "created_to": end_at.isoformat(timespec="seconds"),
            "limit": limit,
            "total": total,
            "scanned_count": len(runs),
            "run_count": len(runs),
            "health": health,
            "candidate_count": candidate_count,
            "planned_count": planned_count,
            "submitted_count": submitted_count,
            "skipped_count": skipped_count,
            "message_count": message_count,
            "status_counts": dict(sorted(status_counts.items())),
            "strategy_counts": dict(sorted(strategy_counts.items())),
            "market_counts": dict(sorted(market_counts.items())),
            "execution_mode_counts": dict(sorted(execution_mode_counts.items())),
            "data_quality_counts": dict(sorted(data_quality_counts.items())),
            "agent_review_counts": dict(sorted(agent_review_counts.items())),
            "llm_review_counts": dict(sorted(llm_review_counts.items())),
            "review_quality_counts": dict(sorted(review_quality_counts.items())),
            "review_quality_flag_counts": dict(sorted(review_quality_flag_counts.items())),
            "human_feedback_counts": dict(sorted(human_feedback_counts.items())),
            "human_feedback_reviewed_count": sum(human_feedback_counts.values()),
            "review_quality_score_avg": (
                round(review_quality_score_total / review_quality_score_count, 2)
                if review_quality_score_count > 0
                else None
            ),
            "workflow_status_counts": dict(sorted(workflow_status_counts.items())),
            "workflow_stage_counts": dict(sorted(workflow_stage_counts.items())),
            "trade_plan_status_counts": dict(sorted(trade_plan_status_counts.items())),
            "side_counts": dict(sorted(side_counts.items())),
            "top_skip_reasons": top_skip_reasons,
            "top_symbols": top_symbols,
            "latest_run": latest_run,
            "filters": {
                "trigger_source": trigger_source,
                "strategy": strategy,
                "market": market,
                "status": status,
            },
        }

    def summarize_data_quality_trends(
        self,
        *,
        days: int = 30,
        limit: int = 5000,
        trigger_source: Optional[str] = None,
        strategy: Optional[str] = None,
        market: Optional[str] = None,
        status: Optional[str] = None,
    ) -> Dict[str, Any]:
        days = max(1, min(90, int(days or 30)))
        limit = max(1, min(5000, int(limit or 5000)))
        window_ended_at = datetime.now()
        window_started_at = window_ended_at - timedelta(days=days)

        with self.db.get_session() as session:
            query = select(StockSelectionAgentRun).where(
                StockSelectionAgentRun.created_at >= window_started_at,
                StockSelectionAgentRun.created_at <= window_ended_at,
            )
            if trigger_source:
                query = query.where(StockSelectionAgentRun.trigger_source == str(trigger_source).strip())
            if strategy:
                query = query.where(StockSelectionAgentRun.strategy == str(strategy).strip())
            if market:
                query = query.where(StockSelectionAgentRun.market == str(market).strip())
            if status:
                query = query.where(StockSelectionAgentRun.status == str(status).strip())
            total = int(session.execute(select(func.count()).select_from(query.subquery())).scalar_one() or 0)
            rows = session.execute(
                query.order_by(StockSelectionAgentRun.created_at.asc(), StockSelectionAgentRun.id.asc())
                .limit(limit)
            ).scalars().all()

        quality_counts: Counter[str] = Counter()
        warning_counts: Counter[str] = Counter()
        source_error_counts: Counter[str] = Counter()
        source_health_stats: Dict[str, Dict[str, Any]] = {}
        daily: Dict[str, Dict[str, Any]] = {}
        latest_quality = "unknown"
        accepted_quality = {"ok", "partial", "stale", "unavailable"}

        for row in rows:
            run = self._run_to_dict(row)
            diagnostics = run.get("diagnostics") if isinstance(run.get("diagnostics"), dict) else {}
            agent_summary = diagnostics.get("agent_summary") if isinstance(diagnostics.get("agent_summary"), dict) else {}
            quality = diagnostics.get("data_quality")
            raw_quality = (
                quality.get("status")
                if isinstance(quality, dict)
                else agent_summary.get("data_quality_status")
            )
            quality_status = str(raw_quality or "unknown").strip().lower()
            if quality_status not in accepted_quality:
                quality_status = "unknown"
            latest_quality = quality_status
            quality_counts[quality_status] += 1

            created_at = run.get("created_at")
            day_key = created_at.date().isoformat() if isinstance(created_at, datetime) else "unknown"
            bucket = daily.setdefault(
                day_key,
                {"date": day_key, "run_count": 0, "quality_counts": Counter()},
            )
            bucket["run_count"] += 1
            bucket["quality_counts"][quality_status] += 1

            for item in list(diagnostics.get("warnings") or []):
                text = str(item or "").strip()
                if text:
                    warning_counts[text] += 1
            for item in list(diagnostics.get("source_errors") or []):
                text = str(item or "").strip()
                if text:
                    source_error_counts[text] += 1

            source_health = diagnostics.get("source_health")
            if isinstance(source_health, dict):
                for source_group, raw_sources in source_health.items():
                    if not isinstance(raw_sources, dict):
                        continue
                    for source_name, raw_state in raw_sources.items():
                        if not isinstance(raw_state, dict):
                            continue
                        key = f"{source_group}/{source_name}"
                        item = source_health_stats.setdefault(
                            key,
                            {
                                "key": key,
                                "group": str(source_group),
                                "source": str(source_name),
                                "observation_count": 0,
                                "degraded_observation_count": 0,
                                "max_failures": 0,
                                "latest_status": "unknown",
                                "latest_failures": 0,
                                "last_observed_at": None,
                            },
                        )
                        failures = self._safe_non_negative_int(raw_state.get("failures"))
                        state_text = str(
                            raw_state.get("status")
                            or raw_state.get("state")
                            or raw_state.get("circuit_state")
                            or ""
                        ).strip().lower()
                        disabled = bool(raw_state.get("disabled"))
                        degraded = disabled or failures > 0 or state_text in {
                            "open",
                            "degraded",
                            "failed",
                            "error",
                            "unavailable",
                            "disabled",
                        }
                        item["observation_count"] += 1
                        if degraded:
                            item["degraded_observation_count"] += 1
                        item["max_failures"] = max(int(item["max_failures"]), failures)
                        item["latest_status"] = (
                            "disabled"
                            if disabled
                            else state_text
                            if state_text
                            else "degraded"
                            if failures > 0
                            else "ok"
                        )
                        item["latest_failures"] = failures
                        item["last_observed_at"] = (
                            created_at.isoformat(timespec="seconds")
                            if isinstance(created_at, datetime)
                            else None
                        )

        degraded_statuses = {"partial", "stale", "unavailable"}
        degraded_count = sum(quality_counts.get(key, 0) for key in degraded_statuses)
        known_count = len(rows) - quality_counts.get("unknown", 0)
        unavailable_count = quality_counts.get("unavailable", 0)
        stale_count = quality_counts.get("stale", 0)
        if not rows:
            health = "idle"
        elif unavailable_count > 0 or stale_count > 0:
            health = "error"
        elif degraded_count > 0 or quality_counts.get("unknown", 0) > 0:
            health = "warning"
        else:
            health = "ok"

        daily_items: List[Dict[str, Any]] = []
        for key in sorted(daily):
            bucket = daily[key]
            bucket_counts = bucket["quality_counts"]
            bucket_degraded = sum(bucket_counts.get(item, 0) for item in degraded_statuses)
            daily_items.append({
                "date": bucket["date"],
                "run_count": bucket["run_count"],
                "quality_counts": dict(sorted(bucket_counts.items())),
                "degraded_count": bucket_degraded,
                "degraded_rate_pct": round(bucket_degraded / bucket["run_count"] * 100, 2),
            })

        source_health_items: List[Dict[str, Any]] = []
        for item in source_health_stats.values():
            observations = int(item["observation_count"])
            degraded_observations = int(item["degraded_observation_count"])
            source_health_items.append({
                **item,
                "degraded_rate_pct": round(
                    degraded_observations / observations * 100,
                    2,
                ) if observations else 0.0,
            })
        source_health_items.sort(
            key=lambda item: (
                -float(item["degraded_rate_pct"]),
                -int(item["degraded_observation_count"]),
                str(item["key"]),
            )
        )

        return {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "window_days": days,
            "window_started_at": window_started_at.isoformat(timespec="seconds"),
            "window_ended_at": window_ended_at.isoformat(timespec="seconds"),
            "total": total,
            "scanned_count": len(rows),
            "known_count": known_count,
            "quality_counts": dict(sorted(quality_counts.items())),
            "degraded_count": degraded_count,
            "degraded_rate_pct": round(degraded_count / len(rows) * 100, 2) if rows else 0.0,
            "health": health,
            "latest_quality": latest_quality if rows else None,
            "warning_counts": dict(sorted(warning_counts.items(), key=lambda item: (-item[1], item[0]))[:20]),
            "source_error_counts": dict(sorted(source_error_counts.items(), key=lambda item: (-item[1], item[0]))[:20]),
            "source_health_items": source_health_items[:50],
            "truncated": total > len(rows),
            "daily": daily_items,
            "filters": {
                "trigger_source": trigger_source,
                "strategy": strategy,
                "market": market,
                "status": status,
            },
        }

    def summarize_return_risk_calibration_trends(
        self,
        *,
        days: int = 30,
        limit: int = 5000,
        trigger_source: Optional[str] = None,
        strategy: Optional[str] = None,
        market: Optional[str] = None,
        status: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Aggregate persisted cross-run return/risk snapshots without claiming independence."""

        days = max(1, min(90, int(days or 30)))
        limit = max(1, min(5000, int(limit or 5000)))
        window_ended_at = datetime.now()
        window_started_at = window_ended_at - timedelta(days=days)
        with self.db.get_session() as session:
            query = select(StockSelectionAgentRun).where(
                StockSelectionAgentRun.created_at >= window_started_at,
                StockSelectionAgentRun.created_at <= window_ended_at,
            )
            if trigger_source:
                query = query.where(
                    StockSelectionAgentRun.trigger_source == str(trigger_source).strip()
                )
            if strategy:
                query = query.where(StockSelectionAgentRun.strategy == str(strategy).strip())
            if market:
                query = query.where(StockSelectionAgentRun.market == str(market).strip())
            if status:
                query = query.where(StockSelectionAgentRun.status == str(status).strip())
            total = int(
                session.execute(select(func.count()).select_from(query.subquery())).scalar_one()
                or 0
            )
            rows = session.execute(
                query.order_by(
                    StockSelectionAgentRun.created_at.asc(),
                    StockSelectionAgentRun.id.asc(),
                ).limit(limit)
            ).scalars().all()

        state_counts: Counter[str] = Counter()
        version_counts: Counter[str] = Counter()
        market_counts: Counter[str] = Counter()
        strategy_counts: Counter[str] = Counter()
        run_strategy_counts: Counter[str] = Counter()
        transition_counts: Counter[str] = Counter()
        daily: Dict[str, Dict[str, Any]] = {}
        groups: Dict[str, Dict[str, Any]] = {}
        utilities: List[float] = []
        applied_count = 0
        gate_blocked_count = 0
        latest: Optional[Dict[str, Any]] = None
        max_mature_sample_count = 0
        scope_mismatch_count = 0

        for row in rows:
            run = self._run_to_dict(row)
            run_strategy_value = str(
                run.get("strategy") or "unknown"
            ).strip() or "unknown"
            run_strategy_counts[run_strategy_value] += 1
            diagnostics = (
                run.get("diagnostics") if isinstance(run.get("diagnostics"), dict) else {}
            )
            quality = diagnostics.get("cross_run_quality")
            if not isinstance(quality, dict):
                continue
            if str(trigger_source or "").strip() == "agent_calibration_shadow":
                quality_trigger_source = str(
                    quality.get("trigger_source") or ""
                ).strip()
                quality_run_status = str(quality.get("run_status") or "").strip()
                if (
                    quality_trigger_source != "agent_calibration_shadow"
                    or quality_run_status != "completed"
                ):
                    scope_mismatch_count += 1
                    continue
            objective = quality.get("return_risk_objective")
            if not isinstance(objective, dict):
                continue
            version = str(objective.get("version") or "").strip()
            objective_state = str(
                quality.get("return_risk_objective_state")
                or objective.get("state")
                or ""
            ).strip().lower()
            if not version or not objective_state:
                continue
            metrics = objective.get("metrics") if isinstance(objective.get("metrics"), dict) else {}
            utility = self._safe_float(
                quality.get("return_risk_utility_pct")
                if quality.get("return_risk_utility_pct") is not None
                else metrics.get("return_risk_utility_pct")
            )
            created_at = run.get("created_at")
            created_at_text = (
                created_at.isoformat(timespec="seconds")
                if isinstance(created_at, datetime)
                else None
            )
            day_key = created_at.date().isoformat() if isinstance(created_at, datetime) else "unknown"
            market_value = str(run.get("market") or "unknown").strip().lower() or "unknown"
            strategy_value = run_strategy_value
            mature_sample_count = self._safe_non_negative_int(
                quality.get("mature_sample_count")
            )
            max_mature_sample_count = max(max_mature_sample_count, mature_sample_count)
            if utility is not None:
                utilities.append(utility)
            if quality.get("return_risk_objective_applied"):
                applied_count += 1
            if quality.get("gate_blocked"):
                gate_blocked_count += 1
            transition = str(quality.get("transition") or "").strip()
            if transition:
                transition_counts[transition] += 1
            state_counts[objective_state] += 1
            version_counts[version] += 1
            market_counts[market_value] += 1
            strategy_counts[strategy_value] += 1

            snapshot = {
                "run_uid": run.get("run_uid"),
                "created_at": created_at_text,
                "market": market_value,
                "strategy": strategy_value,
                "version": version,
                "state": objective_state,
                "reason": quality.get("return_risk_objective_reason") or objective.get("reason"),
                "utility_pct": utility,
                "daily_return_coverage_pct": metrics.get("daily_return_coverage_pct"),
                "mature_sample_count": mature_sample_count,
                "combined_state": quality.get("state"),
                "gate_blocked": bool(quality.get("gate_blocked")),
            }
            latest = snapshot

            day = daily.setdefault(
                day_key,
                {
                    "date": day_key,
                    "run_snapshot_count": 0,
                    "state_counts": Counter(),
                    "utilities": [],
                },
            )
            day["run_snapshot_count"] += 1
            day["state_counts"][objective_state] += 1
            if utility is not None:
                day["utilities"].append(utility)

            group_key = f"{market_value}/{strategy_value}/{version}"
            group = groups.setdefault(
                group_key,
                {
                    "key": group_key,
                    "market": market_value,
                    "strategy": strategy_value,
                    "version": version,
                    "run_snapshot_count": 0,
                    "state_counts": Counter(),
                    "utilities": [],
                    "latest_state": None,
                    "latest_utility_pct": None,
                    "latest_run_uid": None,
                    "latest_at": None,
                },
            )
            group["run_snapshot_count"] += 1
            group["state_counts"][objective_state] += 1
            if utility is not None:
                group["utilities"].append(utility)
            group["latest_state"] = objective_state
            group["latest_utility_pct"] = utility
            group["latest_run_uid"] = run.get("run_uid")
            group["latest_at"] = created_at_text

        observed_count = sum(state_counts.values())
        unknown_count = len(rows) - observed_count
        if latest is None:
            health = "idle" if not rows else "collecting"
        elif latest["state"] in {"blocked", "unavailable"}:
            health = "error"
        elif latest["state"] == "guarded":
            health = "warning"
        elif latest["state"] == "healthy":
            health = "ok"
        else:
            health = "collecting"

        daily_items: List[Dict[str, Any]] = []
        for key in sorted(daily):
            item = daily[key]
            item_utilities = item.pop("utilities")
            state_counter = item.pop("state_counts")
            daily_items.append(
                {
                    **item,
                    "state_counts": dict(sorted(state_counter.items())),
                    "average_utility_pct": self._average_float(item_utilities),
                }
            )

        group_items: List[Dict[str, Any]] = []
        for group in groups.values():
            group_utilities = group.pop("utilities")
            state_counter = group.pop("state_counts")
            group_items.append(
                {
                    **group,
                    "state_counts": dict(sorted(state_counter.items())),
                    "utility_observation_count": len(group_utilities),
                    "average_utility_pct": self._average_float(group_utilities),
                    "minimum_utility_pct": min(group_utilities) if group_utilities else None,
                    "maximum_utility_pct": max(group_utilities) if group_utilities else None,
                }
            )
        group_items.sort(
            key=lambda item: (-int(item["run_snapshot_count"]), str(item["key"]))
        )

        return {
            "schema_version": 1,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "window_days": days,
            "window_started_at": window_started_at.isoformat(timespec="seconds"),
            "window_ended_at": window_ended_at.isoformat(timespec="seconds"),
            "total": total,
            "scanned_count": len(rows),
            "observed_count": observed_count,
            "unknown_count": unknown_count,
            "scope_mismatch_count": scope_mismatch_count,
            "observation_rate_pct": (
                round(observed_count / len(rows) * 100.0, 2) if rows else 0.0
            ),
            "health": health,
            "state_counts": dict(sorted(state_counts.items())),
            "version_counts": dict(sorted(version_counts.items())),
            "market_counts": dict(sorted(market_counts.items())),
            "strategy_counts": dict(sorted(strategy_counts.items())),
            "run_strategy_counts": dict(sorted(run_strategy_counts.items())),
            "transition_counts": dict(sorted(transition_counts.items())),
            "applied_count": applied_count,
            "applied_rate_pct": (
                round(applied_count / observed_count * 100.0, 2) if observed_count else 0.0
            ),
            "gate_blocked_count": gate_blocked_count,
            "utility_observation_count": len(utilities),
            "average_utility_pct": self._average_float(utilities),
            "minimum_utility_pct": min(utilities) if utilities else None,
            "maximum_utility_pct": max(utilities) if utilities else None,
            "latest_mature_sample_count": (
                int(latest["mature_sample_count"]) if latest is not None else 0
            ),
            "max_mature_sample_count": max_mature_sample_count,
            "latest": latest,
            "groups": group_items,
            "daily": daily_items,
            "truncated": total > len(rows),
            "methodology": {
                "unit": "persisted_agent_run_cross_run_quality_snapshot",
                "objective_source": "diagnostics.cross_run_quality.return_risk_objective",
                "overlapping_rolling_samples": True,
                "independent_sample_count_claimed": False,
                "legacy_runs_without_objective": "unknown",
                "calibration_shadow_scope_requirement": (
                    "cross_run_quality.trigger_source=agent_calibration_shadow"
                    " and cross_run_quality.run_status=completed"
                    if str(trigger_source or "").strip()
                    == "agent_calibration_shadow"
                    else None
                ),
            },
            "filters": {
                "trigger_source": trigger_source,
                "strategy": strategy,
                "market": market,
                "status": status,
            },
        }

    @staticmethod
    def _average_float(values: List[float]) -> Optional[float]:
        return round(sum(values) / len(values), 6) if values else None

    @staticmethod
    def _safe_non_negative_int(value: Any) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    def get_run_detail(self, run_uid: str) -> Optional[Dict[str, Any]]:
        with self.db.get_session() as session:
            run = session.execute(
                select(StockSelectionAgentRun)
                .where(StockSelectionAgentRun.run_uid == run_uid)
                .limit(1)
            ).scalar_one_or_none()
            if run is None:
                return None
            decisions = session.execute(
                select(StockSelectionAgentDecision)
                .where(StockSelectionAgentDecision.run_id == run.id)
                .order_by(StockSelectionAgentDecision.sequence.asc(), StockSelectionAgentDecision.id.asc())
            ).scalars().all()
            plans = session.execute(
                select(StockSelectionAgentTradePlan)
                .where(StockSelectionAgentTradePlan.run_id == run.id)
                .order_by(StockSelectionAgentTradePlan.id.asc())
            ).scalars().all()
            payload = self._run_to_dict(run)
            feedback = session.execute(
                select(StockSelectionAgentRunFeedback)
                .where(StockSelectionAgentRunFeedback.run_id == run.id)
                .limit(1)
            ).scalar_one_or_none()
            payload["human_feedback"] = (
                self._feedback_to_dict(feedback) if feedback is not None else None
            )
            payload["decisions"] = [self._decision_to_dict(row) for row in decisions]
            payload["trade_plans"] = [self._trade_plan_to_dict(row) for row in plans]
            payload["portfolio_change"] = self._build_portfolio_change(payload["trade_plans"])
            diagnostics = payload.get("diagnostics") if isinstance(payload.get("diagnostics"), dict) else {}
            agent_summary = (
                diagnostics.get("agent_summary")
                if isinstance(diagnostics.get("agent_summary"), dict)
                else None
            )
            if not isinstance(agent_summary, dict) or "review_quality" not in agent_summary:
                diagnostics = dict(diagnostics)
                diagnostics["agent_summary"] = self._build_agent_summary(
                    payload,
                    payload["decisions"],
                    payload["trade_plans"],
                )
                payload["diagnostics"] = diagnostics
            if "agent_workflow" not in diagnostics:
                diagnostics = dict(diagnostics)
                diagnostics["agent_workflow"] = self._build_agent_workflow(
                    payload,
                    payload["decisions"],
                    payload["trade_plans"],
                )
                payload["diagnostics"] = diagnostics
            payload["timeline"] = self._build_timeline(payload, payload["decisions"], payload["trade_plans"])
            return payload

    @classmethod
    def _build_portfolio_change(
        cls,
        trade_plans: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        grouped: Dict[tuple[str, str], Dict[str, Any]] = {}
        booked_plan_count = 0
        pending_plan_count = 0
        planned_plan_count = 0
        updated_at: Any = None

        for plan in trade_plans:
            status = str(plan.get("status") or "").strip().lower()
            if status in {"submitted", "part_filled", "cancel_requested"}:
                pending_plan_count += 1
            elif status == "planned":
                planned_plan_count += 1

            trade_id = cls._safe_non_negative_int(plan.get("trade_id"))
            quantity = cls._safe_float(plan.get("submitted_quantity")) or 0.0
            side = str(plan.get("side") or "").strip().lower()
            symbol = str(plan.get("symbol") or "").strip()
            if trade_id <= 0 or quantity <= 0 or side not in {"buy", "sell"} or not symbol:
                continue

            market = str(plan.get("market") or "").strip().lower()
            key = (market, symbol)
            item = grouped.setdefault(
                key,
                {
                    "symbol": symbol,
                    "name": plan.get("name"),
                    "market": market,
                    "buy_quantity": 0.0,
                    "sell_quantity": 0.0,
                    "buy_notional": 0.0,
                    "sell_notional": 0.0,
                    "plan_count": 0,
                    "trade_ids": [],
                },
            )
            price = cls._safe_float(plan.get("submitted_price")) or 0.0
            item[f"{side}_quantity"] += quantity
            item[f"{side}_notional"] += quantity * price
            item["plan_count"] += 1
            if trade_id not in item["trade_ids"]:
                item["trade_ids"].append(trade_id)
            booked_plan_count += 1
            plan_updated_at = plan.get("updated_at") or plan.get("created_at")
            if plan_updated_at is not None and (updated_at is None or plan_updated_at > updated_at):
                updated_at = plan_updated_at

        items: List[Dict[str, Any]] = []
        for key in sorted(grouped):
            item = grouped[key]
            buy_quantity = float(item["buy_quantity"])
            sell_quantity = float(item["sell_quantity"])
            buy_notional = float(item["buy_notional"])
            sell_notional = float(item["sell_notional"])
            items.append(
                {
                    **item,
                    "buy_quantity": round(buy_quantity, 8),
                    "sell_quantity": round(sell_quantity, 8),
                    "net_quantity": round(buy_quantity - sell_quantity, 8),
                    "buy_notional": round(buy_notional, 6),
                    "sell_notional": round(sell_notional, 6),
                    "net_cash_flow": round(sell_notional - buy_notional, 6),
                    "trade_ids": sorted(item["trade_ids"]),
                }
            )

        if items and pending_plan_count:
            status = "changed_pending"
        elif items:
            status = "changed"
        elif pending_plan_count:
            status = "pending"
        elif planned_plan_count:
            status = "planned"
        else:
            status = "unchanged"
        return {
            "schema_version": 1,
            "basis": "persisted_portfolio_trade_ids",
            "status": status,
            "booked_plan_count": booked_plan_count,
            "pending_plan_count": pending_plan_count,
            "planned_plan_count": planned_plan_count,
            "symbol_count": len(items),
            "items": items,
            "updated_at": updated_at,
        }

    @classmethod
    def _build_timeline(
        cls,
        run: Dict[str, Any],
        decisions: List[Dict[str, Any]],
        trade_plans: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        timeline: List[Dict[str, Any]] = []
        started_at = run.get("started_at") or run.get("created_at")
        if started_at:
            timeline.append(
                {
                    "stage": "started",
                    "status": "running",
                    "message": f"Agent run started with strategy {run.get('strategy') or '-'} on {run.get('market') or '-'}",
                    "timestamp": started_at,
                }
            )

        diagnostics = run.get("diagnostics") if isinstance(run.get("diagnostics"), dict) else {}
        agent_plan = diagnostics.get("agent_plan") if isinstance(diagnostics, dict) else None
        if isinstance(agent_plan, dict):
            strategy = str(agent_plan.get("strategy") or run.get("strategy") or "-")
            market = str(agent_plan.get("market") or run.get("market") or "-")
            execution_mode = str(agent_plan.get("execution_mode") or "-")
            max_results = agent_plan.get("max_results")
            cash_per_order = agent_plan.get("cash_per_order")
            timeline.append(
                {
                    "stage": "agent_plan",
                    "status": "planned",
                    "message": (
                        f"Agent plan: strategy={strategy}, market={market}, "
                        f"execution={execution_mode}, max_results={max_results}, "
                        f"cash_per_order={cash_per_order}"
                    ),
                    "timestamp": agent_plan.get("created_at") or started_at,
                    "details": {
                        "strategy": strategy,
                        "market": market,
                        "execution_mode": execution_mode,
                        "max_results": max_results,
                        "cash_per_order": cash_per_order,
                        "plan_profile": agent_plan.get("plan_profile") or {},
                        "execution_policy": agent_plan.get("execution_policy") or {},
                        "risk_budget": agent_plan.get("risk_budget") or {},
                        "sizing_plan": agent_plan.get("sizing_plan") or {},
                        "gates": agent_plan.get("gates") or {},
                        "adaptive_controls": agent_plan.get("adaptive_controls") or {},
                    },
                }
            )

        llm_policy = diagnostics.get("alphasift_llm_policy") if isinstance(diagnostics, dict) else None
        llm_result = diagnostics.get("alphasift_llm_result") if isinstance(diagnostics, dict) else None
        if isinstance(llm_policy, dict):
            result_status = (
                str(llm_result.get("status") or "unknown")
                if isinstance(llm_result, dict)
                else "unknown"
            )
            decision = str(llm_policy.get("decision") or "normal")
            timeline.append(
                {
                    "stage": "llm_ranking_health",
                    "status": result_status,
                    "message": (
                        f"AlphaSift LLM ranking: decision={decision}, "
                        f"result={result_status}, state={llm_policy.get('state') or 'closed'}"
                    ),
                    "timestamp": (
                        llm_result.get("observed_at")
                        if isinstance(llm_result, dict)
                        else started_at
                    ),
                    "details": {
                        "policy": dict(llm_policy),
                        "result": dict(llm_result) if isinstance(llm_result, dict) else None,
                    },
                }
            )

        data_quality = diagnostics.get("data_quality") if isinstance(diagnostics, dict) else None
        if isinstance(data_quality, dict):
            quality_status = str(data_quality.get("status") or "unknown")
            reason = str(data_quality.get("reason") or "").strip()
            timeline.append(
                {
                    "stage": "data_quality",
                    "status": quality_status,
                    "message": (
                        f"Data quality {quality_status}"
                        + (f": {reason}" if reason else "")
                    ),
                    "timestamp": started_at,
                    "details": {
                        "warnings": data_quality.get("warnings") or [],
                        "source_errors": data_quality.get("source_errors") or [],
                        "fallback_used": bool(data_quality.get("fallback_used")),
                    },
                }
            )

        market_context_risk = (
            diagnostics.get("market_context_risk") if isinstance(diagnostics, dict) else None
        )
        if isinstance(market_context_risk, dict) and market_context_risk.get("enabled"):
            risk_status = str(market_context_risk.get("status") or "unknown")
            risk_reason = str(market_context_risk.get("reason") or "").strip()
            breadth = (
                market_context_risk.get("market_breadth")
                if isinstance(market_context_risk.get("market_breadth"), dict)
                else {}
            )
            retreat = (
                market_context_risk.get("hotspot_retreat")
                if isinstance(market_context_risk.get("hotspot_retreat"), dict)
                else {}
            )
            freshness = (
                market_context_risk.get("freshness")
                if isinstance(market_context_risk.get("freshness"), dict)
                else {}
            )
            intraday = (
                market_context_risk.get("intraday_market")
                if isinstance(market_context_risk.get("intraday_market"), dict)
                else {}
            )
            intraday_index = (
                intraday.get("index") if isinstance(intraday.get("index"), dict) else {}
            )
            intraday_breadth = (
                intraday.get("breadth")
                if isinstance(intraday.get("breadth"), dict)
                else {}
            )
            cross_market = (
                market_context_risk.get("cross_market")
                if isinstance(market_context_risk.get("cross_market"), dict)
                else {}
            )
            timeline.append(
                {
                    "stage": "market_context_risk",
                    "status": risk_status,
                    "message": (
                        f"Market context risk {risk_status}"
                        + (f": {risk_reason}" if risk_reason else "")
                        + f", breadth={breadth.get('score')}"
                        + f", hotspot_drop={retreat.get('score_drop')}"
                        + f", age_days={freshness.get('age_days')}"
                        + f", intraday_index={intraday_index.get('aggregate_change_pct')}"
                        + f", intraday_breadth={intraday_breadth.get('score')}"
                        + f", linked_blocked={cross_market.get('blocked_markets')}"
                    ),
                    "timestamp": started_at,
                    "details": dict(market_context_risk),
                }
            )

        if decisions:
            reason_counts: Dict[str, int] = {}
            action_counts: Dict[str, int] = {}
            for item in decisions:
                action = str(item.get("action") or "unknown")
                action_counts[action] = action_counts.get(action, 0) + 1
                reason = str(item.get("reason") or "").strip()
                if reason:
                    reason_counts[reason] = reason_counts.get(reason, 0) + 1
            top_reasons = sorted(reason_counts.items(), key=lambda item: (-item[1], item[0]))[:5]
            timeline.append(
                {
                    "stage": "candidate_decisions",
                    "status": "completed",
                    "message": (
                        f"{len(decisions)} candidate decisions: "
                        + ", ".join(f"{key}={value}" for key, value in sorted(action_counts.items()))
                    ),
                    "timestamp": decisions[0].get("created_at") or started_at,
                    "details": {
                        "action_counts": action_counts,
                        "reason_counts": reason_counts,
                        "top_reasons": [{"reason": reason, "count": count} for reason, count in top_reasons],
                    },
                }
            )

        if trade_plans:
            status_counts: Dict[str, int] = {}
            side_counts: Dict[str, int] = {}
            skip_counts: Dict[str, int] = {}
            for item in trade_plans:
                status = str(item.get("status") or "unknown")
                side = str(item.get("side") or "unknown")
                status_counts[status] = status_counts.get(status, 0) + 1
                side_counts[side] = side_counts.get(side, 0) + 1
                reason = str(item.get("skip_reason") or "").strip()
                if reason:
                    skip_counts[reason] = skip_counts.get(reason, 0) + 1
            timeline.append(
                {
                    "stage": "trade_plans",
                    "status": "completed",
                    "message": (
                        f"{len(trade_plans)} trade plans: "
                        + ", ".join(f"{key}={value}" for key, value in sorted(status_counts.items()))
                    ),
                    "timestamp": trade_plans[0].get("created_at") or started_at,
                    "details": {
                        "status_counts": status_counts,
                        "side_counts": side_counts,
                        "skip_counts": skip_counts,
                    },
                }
            )

        agent_summary = diagnostics.get("agent_summary") if isinstance(diagnostics, dict) else None
        if isinstance(agent_summary, dict):
            timeline.append(
                {
                    "stage": "agent_summary",
                    "status": str(agent_summary.get("outcome") or run.get("status") or "unknown"),
                    "message": str(agent_summary.get("headline") or "Agent run summary generated"),
                    "timestamp": agent_summary.get("generated_at") or run.get("completed_at") or started_at,
                    "details": {
                        "candidate_count": agent_summary.get("candidate_count"),
                        "planned_count": agent_summary.get("planned_count"),
                        "submitted_count": agent_summary.get("submitted_count"),
                        "skipped_count": agent_summary.get("skipped_count"),
                        "top_skip_reasons": agent_summary.get("top_skip_reasons") or [],
                        "agent_review_counts": agent_summary.get("agent_review_counts") or {},
                        "llm_review_counts": agent_summary.get("llm_review_counts") or {},
                    },
                }
            )

        portfolio_change = (
            run.get("portfolio_change") if isinstance(run.get("portfolio_change"), dict) else None
        )
        if isinstance(portfolio_change, dict):
            change_status = str(portfolio_change.get("status") or "unchanged")
            timeline.append(
                {
                    "stage": "portfolio_change",
                    "status": change_status,
                    "message": (
                        f"Portfolio change {change_status}: "
                        f"symbols={portfolio_change.get('symbol_count', 0)}, "
                        f"booked={portfolio_change.get('booked_plan_count', 0)}, "
                        f"pending={portfolio_change.get('pending_plan_count', 0)}"
                    ),
                    "timestamp": portfolio_change.get("updated_at") or run.get("updated_at") or started_at,
                    "details": dict(portfolio_change),
                }
            )

        stage_timings = diagnostics.get("stage_timings") if isinstance(diagnostics, dict) else None
        if isinstance(stage_timings, dict) and stage_timings.get("total_seconds") is not None:
            total_seconds = stage_timings.get("total_seconds")
            screen_seconds = stage_timings.get("alphasift_screen_seconds")
            screen_summary = (
                f", AlphaSift={screen_seconds}s"
                if screen_seconds is not None
                else ""
            )
            timeline.append(
                {
                    "stage": "performance",
                    "status": str(run.get("status") or "completed"),
                    "message": f"Run timing: total={total_seconds}s{screen_summary}",
                    "timestamp": run.get("completed_at") or run.get("updated_at") or started_at,
                    "details": dict(stage_timings),
                }
            )

        completed_at = run.get("completed_at") or run.get("updated_at")
        if completed_at:
            status = str(run.get("status") or "unknown")
            error = str(run.get("error") or "").strip()
            timeline.append(
                {
                    "stage": "completed",
                    "status": status,
                    "message": (
                        f"Run {status}: planned={run.get('planned_count', 0)}, "
                        f"submitted={run.get('submitted_count', 0)}, skipped={run.get('skipped_count', 0)}"
                        + (f", error={error}" if error else "")
                    ),
                    "timestamp": completed_at,
                }
            )
        human_feedback = (
            run.get("human_feedback") if isinstance(run.get("human_feedback"), dict) else None
        )
        if human_feedback:
            verdict = str(human_feedback.get("verdict") or "unknown")
            reviewer = str(human_feedback.get("reviewer") or "").strip()
            timeline.append(
                {
                    "stage": "human_feedback",
                    "status": verdict,
                    "message": (
                        f"Human review {verdict}"
                        + (f" by {reviewer}" if reviewer else "")
                    ),
                    "timestamp": human_feedback.get("updated_at") or human_feedback.get("created_at"),
                    "details": {
                        "verdict": verdict,
                        "note": human_feedback.get("note"),
                        "reviewer": human_feedback.get("reviewer"),
                        "source": human_feedback.get("source"),
                    },
                }
            )
        return timeline

    @classmethod
    def _build_agent_workflow(
        cls,
        run: Dict[str, Any],
        decisions: List[Dict[str, Any]],
        trade_plans: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        diagnostics = run.get("diagnostics") if isinstance(run.get("diagnostics"), dict) else {}
        agent_plan = diagnostics.get("agent_plan") if isinstance(diagnostics, dict) else None
        data_quality = diagnostics.get("data_quality") if isinstance(diagnostics, dict) else None
        agent_summary = diagnostics.get("agent_summary") if isinstance(diagnostics, dict) else None
        if not isinstance(agent_summary, dict):
            agent_summary = cls._build_agent_summary(run, decisions, trade_plans)

        run_status = str(run.get("status") or "unknown")
        error = str(run.get("error") or "").strip()
        candidate_count = int(run.get("candidate_count") or 0)
        planned_count = int(run.get("planned_count") or 0)
        submitted_count = int(run.get("submitted_count") or 0)
        skipped_count = int(run.get("skipped_count") or 0)

        if error or run_status == "failed":
            workflow_status = "failed"
            current_stage = "failed"
            next_action = "inspect_error"
        elif run_status == "running":
            workflow_status = "running"
            current_stage = "running"
            next_action = "wait_for_completion"
        elif submitted_count > 0:
            workflow_status = "executed"
            current_stage = "execution"
            next_action = "monitor_positions_and_events"
        elif planned_count > 0:
            workflow_status = "planned"
            current_stage = "trade_plan"
            next_action = "review_or_approve_trade_plan"
        elif skipped_count > 0:
            workflow_status = "skipped"
            current_stage = "candidate_review"
            next_action = "review_skip_reasons"
        else:
            workflow_status = run_status
            current_stage = "completed" if run_status == "completed" else "started"
            next_action = "none"

        stages: List[Dict[str, Any]] = []

        def add_stage(
            key: str,
            label: str,
            status: str,
            message: str,
            details: Optional[Dict[str, Any]] = None,
        ) -> None:
            stages.append(
                {
                    "key": key,
                    "label": label,
                    "status": status,
                    "message": message,
                    "details": details or {},
                }
            )

        if isinstance(agent_plan, dict):
            add_stage(
                "agent_plan",
                "Agent plan",
                "completed",
                (
                    f"strategy={agent_plan.get('strategy') or run.get('strategy') or '-'}, "
                    f"market={agent_plan.get('market') or run.get('market') or '-'}, "
                    f"execution={agent_plan.get('execution_mode') or '-'}"
                ),
                {
                    "strategy": agent_plan.get("strategy") or run.get("strategy"),
                    "market": agent_plan.get("market") or run.get("market"),
                    "execution_mode": agent_plan.get("execution_mode"),
                    "llm_dynamic_plan_status": (
                        (agent_plan.get("llm_dynamic_plan") or {}).get("status")
                        if isinstance(agent_plan.get("llm_dynamic_plan"), dict)
                        else None
                    ),
                },
            )
        else:
            add_stage("agent_plan", "Agent plan", "pending", "Agent plan is not available")

        if isinstance(data_quality, dict):
            quality_status = str(data_quality.get("status") or "unknown")
            if quality_status in {"ok", "partial"}:
                stage_status = "completed"
            elif quality_status in {"stale", "unavailable"}:
                stage_status = "blocked"
            else:
                stage_status = "warning"
            add_stage(
                "data_quality",
                "Data quality",
                stage_status,
                f"data_quality={quality_status}",
                {
                    "status": quality_status,
                    "warnings": data_quality.get("warnings") or [],
                    "source_errors": data_quality.get("source_errors") or [],
                    "fallback_used": bool(data_quality.get("fallback_used")),
                },
            )
        else:
            add_stage("data_quality", "Data quality", "pending", "Data quality diagnostics are not available")

        action_counts: Counter[str] = Counter()
        reason_counts: Counter[str] = Counter()
        for item in decisions:
            action_counts[str(item.get("action") or "unknown")] += 1
            reason = str(item.get("reason") or "").strip()
            if reason:
                reason_counts[reason] += 1
        if decisions:
            decision_status = "completed"
            if action_counts and action_counts.get("buy", 0) <= 0 and skipped_count > 0:
                decision_status = "blocked"
            elif skipped_count > 0:
                decision_status = "warning"
            add_stage(
                "candidate_review",
                "Candidate review",
                decision_status,
                f"{len(decisions)} candidate decisions",
                {
                    "action_counts": dict(sorted(action_counts.items())),
                    "reason_counts": dict(sorted(reason_counts.items())),
                    "risk_review_counts": agent_summary.get("risk_review_counts") or {},
                    "agent_review_counts": agent_summary.get("agent_review_counts") or {},
                    "llm_review_counts": agent_summary.get("llm_review_counts") or {},
                },
            )
        else:
            add_stage(
                "candidate_review",
                "Candidate review",
                "skipped" if candidate_count <= 0 else "pending",
                "No candidate decisions recorded",
                {"candidate_count": candidate_count},
            )

        plan_status_counts: Counter[str] = Counter()
        side_counts: Counter[str] = Counter()
        skip_counts: Counter[str] = Counter()
        for item in trade_plans:
            plan_status_counts[str(item.get("status") or "unknown")] += 1
            side_counts[str(item.get("side") or "unknown")] += 1
            reason = str(item.get("skip_reason") or "").strip()
            if reason:
                skip_counts[reason] += 1
        if trade_plans:
            if submitted_count > 0:
                plan_stage_status = "executed"
            elif planned_count > 0:
                plan_stage_status = "planned"
            elif skipped_count > 0:
                plan_stage_status = "skipped"
            else:
                plan_stage_status = "completed"
            add_stage(
                "trade_plan",
                "Trade plan",
                plan_stage_status,
                f"{len(trade_plans)} trade plans",
                {
                    "status_counts": dict(sorted(plan_status_counts.items())),
                    "side_counts": dict(sorted(side_counts.items())),
                    "skip_counts": dict(sorted(skip_counts.items())),
                },
            )
        else:
            add_stage("trade_plan", "Trade plan", "pending", "No trade plans recorded")

        execution_status = "pending"
        execution_message = "No order execution recorded"
        if submitted_count > 0:
            execution_status = "completed"
            execution_message = f"{submitted_count} orders submitted or filled"
        elif planned_count > 0:
            execution_status = "planned"
            execution_message = f"{planned_count} plans await approval or execution"
        elif skipped_count > 0:
            execution_status = "skipped"
            execution_message = f"{skipped_count} candidates skipped before execution"
        add_stage(
            "execution",
            "Execution",
            execution_status,
            execution_message,
            {
                "planned_count": planned_count,
                "submitted_count": submitted_count,
                "skipped_count": skipped_count,
            },
        )

        return {
            "schema_version": 1,
            "status": workflow_status,
            "current_stage": current_stage,
            "next_action": next_action,
            "outcome": agent_summary.get("outcome") or workflow_status,
            "run_status": run_status,
            "error": error or None,
            "stages": stages,
            "top_skip_reasons": agent_summary.get("top_skip_reasons") or [],
        }

    @classmethod
    def _build_agent_summary(
        cls,
        run: Dict[str, Any],
        decisions: List[Dict[str, Any]],
        trade_plans: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        diagnostics = run.get("diagnostics") if isinstance(run.get("diagnostics"), dict) else {}
        data_quality = diagnostics.get("data_quality") if isinstance(diagnostics, dict) else None
        agent_plan = diagnostics.get("agent_plan") if isinstance(diagnostics, dict) else None

        action_counts: Counter[str] = Counter()
        reason_counts: Counter[str] = Counter()
        risk_review_counts: Counter[str] = Counter()
        agent_review_counts: Counter[str] = Counter()
        llm_review_counts: Counter[str] = Counter()
        for item in decisions:
            action_counts[str(item.get("action") or "unknown")] += 1
            reason = str(item.get("reason") or "").strip()
            if reason:
                reason_counts[reason] += 1
            order_result = item.get("order_result")
            if isinstance(order_result, dict):
                risk_review = order_result.get("risk_review")
                if isinstance(risk_review, dict):
                    risk_review_counts[str(risk_review.get("status") or "unknown")] += 1
                agent_review = order_result.get("agent_review")
                if isinstance(agent_review, dict):
                    agent_review_counts[str(agent_review.get("status") or "unknown")] += 1
                llm_review = order_result.get("llm_review")
                if isinstance(llm_review, dict):
                    llm_review_counts[str(llm_review.get("status") or "unknown")] += 1

        trade_plan_status_counts: Counter[str] = Counter()
        side_counts: Counter[str] = Counter()
        for item in trade_plans:
            trade_plan_status_counts[str(item.get("status") or "unknown")] += 1
            side_counts[str(item.get("side") or "unknown")] += 1
            reason = str(item.get("skip_reason") or "").strip()
            if reason:
                reason_counts[reason] += 1

        candidate_count = int(run.get("candidate_count") or 0)
        planned_count = int(run.get("planned_count") or 0)
        submitted_count = int(run.get("submitted_count") or 0)
        skipped_count = int(run.get("skipped_count") or 0)
        status = str(run.get("status") or "unknown")
        error = str(run.get("error") or "").strip()
        if error:
            outcome = "failed" if status == "failed" else "skipped"
        elif submitted_count > 0:
            outcome = "executed"
        elif planned_count > 0:
            outcome = "planned"
        elif skipped_count > 0:
            outcome = "skipped"
        else:
            outcome = status

        data_quality_status = None
        if isinstance(data_quality, dict):
            data_quality_status = str(data_quality.get("status") or "unknown")
        execution_mode = None
        if isinstance(agent_plan, dict):
            execution_mode = agent_plan.get("execution_mode")
        if execution_mode is None and isinstance(diagnostics, dict):
            execution_mode = diagnostics.get("execution_mode")
        review_quality = cls._build_review_quality(
            decision_count=len(decisions),
            data_quality_status=data_quality_status,
            agent_review_counts=agent_review_counts,
            llm_review_counts=llm_review_counts,
            agent_plan=agent_plan if isinstance(agent_plan, dict) else {},
        )

        top_skip_reasons = [
            {"reason": reason, "count": int(count)}
            for reason, count in sorted(reason_counts.items(), key=lambda item: (-item[1], item[0]))[:5]
        ]
        headline_parts = [
            f"outcome={outcome}",
            f"candidates={candidate_count}",
            f"planned={planned_count}",
            f"submitted={submitted_count}",
            f"skipped={skipped_count}",
        ]
        if error:
            headline_parts.append(f"error={error}")
        elif top_skip_reasons:
            headline_parts.append(f"top_skip={top_skip_reasons[0]['reason']}")

        return {
            "schema_version": 1,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "status": status,
            "outcome": outcome,
            "headline": "Agent summary: " + ", ".join(headline_parts),
            "strategy": run.get("strategy"),
            "market": run.get("market"),
            "execution_mode": execution_mode,
            "candidate_count": candidate_count,
            "decision_count": len(decisions),
            "trade_plan_count": len(trade_plans),
            "planned_count": planned_count,
            "submitted_count": submitted_count,
            "skipped_count": skipped_count,
            "message_count": int(run.get("message_count") or 0),
            "error": error or None,
            "data_quality_status": data_quality_status,
            "action_counts": dict(sorted(action_counts.items())),
            "trade_plan_status_counts": dict(sorted(trade_plan_status_counts.items())),
            "side_counts": dict(sorted(side_counts.items())),
            "risk_review_counts": dict(sorted(risk_review_counts.items())),
            "agent_review_counts": dict(sorted(agent_review_counts.items())),
            "llm_review_counts": dict(sorted(llm_review_counts.items())),
            "review_quality": review_quality,
            "top_skip_reasons": top_skip_reasons,
        }

    @classmethod
    def _build_review_quality(
        cls,
        *,
        decision_count: int,
        data_quality_status: Optional[str],
        agent_review_counts: Counter[str],
        llm_review_counts: Counter[str],
        agent_plan: Dict[str, Any],
    ) -> Dict[str, Any]:
        decision_total = max(0, int(decision_count or 0))
        agent_total = sum(int(value or 0) for value in agent_review_counts.values())
        llm_total = sum(int(value or 0) for value in llm_review_counts.values())
        gates = agent_plan.get("gates") if isinstance(agent_plan.get("gates"), dict) else {}
        llm_review_enabled = bool(gates.get("llm_review_enabled"))

        def count_status(counter: Counter[str], statuses: set[str]) -> int:
            return sum(int(counter.get(status, 0) or 0) for status in statuses)

        agent_blocked = count_status(agent_review_counts, {"blocked"})
        agent_warning = count_status(agent_review_counts, {"warning"})
        agent_failed = count_status(agent_review_counts, {"failed", "failed_execution"})
        llm_blocked = count_status(llm_review_counts, {"blocked"})
        llm_failed = count_status(llm_review_counts, {"failed", "failed_execution"})

        risk_flags: List[str] = []
        if decision_total > 0 and agent_total <= 0:
            risk_flags.append("missing_agent_review")
        if decision_total > 0 and llm_review_enabled and llm_total <= 0:
            risk_flags.append("missing_llm_review")
        if agent_warning > 0:
            risk_flags.append("agent_review_warning")
        if agent_blocked > 0:
            risk_flags.append("agent_review_blocked")
        if agent_failed > 0:
            risk_flags.append("agent_review_failed")
        if llm_blocked > 0:
            risk_flags.append("llm_review_blocked")
        if llm_failed > 0:
            risk_flags.append("llm_review_failed")

        quality_status = str(data_quality_status or "").strip().lower()
        if quality_status in {"partial", "stale", "unavailable"}:
            risk_flags.append(f"data_quality_{quality_status}")

        if decision_total <= 0:
            status = "idle"
        elif agent_failed > 0 or llm_failed > 0 or "missing_agent_review" in risk_flags:
            status = "needs_review"
        elif llm_review_enabled and "missing_llm_review" in risk_flags:
            status = "needs_review"
        elif agent_blocked > 0 or llm_blocked > 0 or agent_warning > 0 or quality_status in {"partial", "stale", "unavailable"}:
            status = "guarded"
        else:
            status = "audited"

        score = 100.0
        score -= 40.0 if "missing_agent_review" in risk_flags else 0.0
        score -= 25.0 if "missing_llm_review" in risk_flags else 0.0
        score -= min(40.0, (agent_failed + llm_failed) * 25.0)
        score -= min(35.0, (agent_blocked + llm_blocked) * 15.0)
        score -= min(30.0, agent_warning * 10.0)
        if quality_status == "partial":
            score -= 10.0
        elif quality_status in {"stale", "unavailable"}:
            score -= 25.0
        score = max(0.0, min(100.0, score))

        return {
            "schema_version": 1,
            "status": status,
            "score": round(score, 2),
            "decision_count": decision_total,
            "agent_review_total": agent_total,
            "llm_review_total": llm_total,
            "agent_review_coverage_pct": (
                round(agent_total / decision_total * 100.0, 2)
                if decision_total > 0
                else None
            ),
            "llm_review_coverage_pct": (
                round(llm_total / decision_total * 100.0, 2)
                if decision_total > 0
                else None
            ),
            "llm_review_enabled": llm_review_enabled,
            "risk_flags": sorted(set(risk_flags)),
            "human_review_recommended": status in {"guarded", "needs_review"},
        }

    @staticmethod
    def _safe_float(value: Any) -> Optional[float]:
        try:
            if value is None or value == "":
                return None
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if number == number else None

    @staticmethod
    def _json_dumps(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, default=str)

    @staticmethod
    def _json_loads(value: Optional[str], fallback: Any) -> Any:
        if not value:
            return fallback
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            logger.warning("Invalid stock selection agent JSON payload ignored")
            return fallback

    @classmethod
    def _sync_decision_audit_columns(
        cls,
        row: StockSelectionAgentDecision,
        *payloads: Dict[str, Any],
        clear_missing: bool = False,
    ) -> None:
        for payload_key, column_name in cls._DECISION_AUDIT_COLUMNS.items():
            value = next(
                (
                    payload.get(payload_key)
                    for payload in payloads
                    if isinstance(payload, dict) and isinstance(payload.get(payload_key), dict)
                ),
                None,
            )
            if isinstance(value, dict):
                setattr(row, column_name, cls._json_dumps(value))
            elif clear_missing:
                setattr(row, column_name, cls._json_dumps({}))

    @classmethod
    def _decision_audit_payloads(
        cls,
        row: StockSelectionAgentDecision,
        order_result: Dict[str, Any],
    ) -> Dict[str, Dict[str, Any]]:
        payloads: Dict[str, Dict[str, Any]] = {}
        for payload_key, column_name in cls._DECISION_AUDIT_COLUMNS.items():
            dedicated = cls._json_loads(getattr(row, column_name, None), {})
            legacy = order_result.get(payload_key)
            if isinstance(dedicated, dict) and dedicated:
                value = dedicated
            elif isinstance(legacy, dict):
                value = legacy
            else:
                value = {}
            payloads[payload_key] = value
            if value and not isinstance(order_result.get(payload_key), dict):
                order_result[payload_key] = value
        return payloads

    @staticmethod
    def _datetime_filter_value(value: Optional[datetime]) -> Optional[datetime]:
        if value is None:
            return None
        if value.tzinfo is not None:
            return value.astimezone().replace(tzinfo=None)
        return value

    @classmethod
    def _order_result_has_vnpy_order_id(cls, payload: Any, vt_orderid: str) -> bool:
        if not isinstance(payload, dict):
            return False
        wanted = str(vt_orderid or "").strip()
        if not wanted:
            return False
        candidates = [
            payload.get("vt_orderid"),
            payload.get("orderid"),
            payload.get("order_id"),
        ]
        raw = payload.get("raw")
        if isinstance(raw, dict):
            candidates.extend([
                raw.get("vt_orderid"),
                raw.get("orderid"),
                raw.get("order_id"),
            ])
        return any(str(candidate or "").strip() == wanted for candidate in candidates)

    @classmethod
    def _run_to_dict(cls, row: StockSelectionAgentRun) -> Dict[str, Any]:
        return {
            "id": int(row.id),
            "run_uid": row.run_uid,
            "trigger_source": row.trigger_source,
            "status": row.status,
            "strategy": row.strategy,
            "market": row.market,
            "max_results": row.max_results,
            "cash_per_order": row.cash_per_order,
            "min_score": row.min_score,
            "skip_existing_positions": bool(row.skip_existing_positions),
            "candidate_count": int(row.candidate_count or 0),
            "planned_count": int(row.planned_count or 0),
            "submitted_count": int(row.submitted_count or 0),
            "skipped_count": int(row.skipped_count or 0),
            "message_count": int(row.message_count or 0),
            "error": row.error,
            "settings": cls._json_loads(row.settings_json, {}),
            "diagnostics": cls._json_loads(row.diagnostics_json, {}),
            "started_at": row.started_at,
            "completed_at": row.completed_at,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }

    @classmethod
    def _feedback_to_dict(cls, row: StockSelectionAgentRunFeedback) -> Dict[str, Any]:
        return {
            "id": int(row.id),
            "run_id": int(row.run_id),
            "verdict": row.verdict,
            "note": row.note,
            "reviewer": row.reviewer,
            "source": row.source,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }

    @classmethod
    def _feedback_by_run_ids(
        cls,
        session: Any,
        run_ids: List[int],
    ) -> Dict[int, Dict[str, Any]]:
        normalized = sorted({int(run_id) for run_id in run_ids if run_id is not None})
        if not normalized:
            return {}
        rows = session.execute(
            select(StockSelectionAgentRunFeedback)
            .where(StockSelectionAgentRunFeedback.run_id.in_(normalized))
        ).scalars().all()
        return {int(row.run_id): cls._feedback_to_dict(row) for row in rows}

    @classmethod
    def _decision_to_dict(cls, row: StockSelectionAgentDecision) -> Dict[str, Any]:
        order_result = cls._json_loads(row.order_result_json, {})
        if not isinstance(order_result, dict):
            order_result = {}
        audit_payloads = cls._decision_audit_payloads(row, order_result)
        return {
            "id": int(row.id),
            "run_id": int(row.run_id),
            "sequence": int(row.sequence or 0),
            "symbol": row.symbol,
            "name": row.name,
            "market": row.market,
            "action": row.action,
            "status": row.status,
            "reason": row.reason,
            "score": row.score,
            "confidence": row.confidence,
            "cash_amount": row.cash_amount,
            "quantity": row.quantity,
            "price": row.price,
            "trade_id": row.trade_id,
            "rationale": row.rationale,
            "risk_flags": cls._json_loads(row.risk_flags_json, []),
            **audit_payloads,
            "order_result": order_result,
            "raw_candidate": cls._json_loads(row.raw_candidate_json, {}),
            "created_at": row.created_at,
        }

    @classmethod
    def _trade_plan_to_dict(cls, row: StockSelectionAgentTradePlan) -> Dict[str, Any]:
        return {
            "id": int(row.id),
            "plan_uid": row.plan_uid,
            "run_id": int(row.run_id),
            "decision_id": row.decision_id,
            "symbol": row.symbol,
            "name": row.name,
            "market": row.market,
            "side": row.side,
            "status": row.status,
            "execution_mode": row.execution_mode,
            "planned_cash_amount": row.planned_cash_amount,
            "planned_quantity": row.planned_quantity,
            "planned_price": row.planned_price,
            "submitted_quantity": row.submitted_quantity,
            "submitted_price": row.submitted_price,
            "trade_id": row.trade_id,
            "skip_reason": row.skip_reason,
            "risk_flags": cls._json_loads(row.risk_flags_json, []),
            "order_result": cls._json_loads(row.order_result_json, {}),
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }
