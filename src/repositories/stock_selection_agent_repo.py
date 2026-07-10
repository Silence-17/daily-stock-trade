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
    StockSelectionAgentTradePlan,
)

logger = logging.getLogger(__name__)


class StockSelectionAgentRepository:
    """Persist and read stock-selection agent runs and candidate decisions."""

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
                order_result_json=self._json_dumps(order_result or {}),
                raw_candidate_json=self._json_dumps(raw_candidate or {}),
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
                .where(StockSelectionAgentTradePlan.status.in_(["submitted", "part_filled", "filled", "failed"]))
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
            row.order_result_json = self._json_dumps(order_result or {})
            session.commit()
            session.refresh(row)
            return self._decision_to_dict(row)

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
            return {
                "items": [self._run_to_dict(row) for row in rows],
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
            return [self._run_to_dict(row) for row in rows]

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
            runs = [self._run_to_dict(row) for row in rows]

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
            payload["decisions"] = [self._decision_to_dict(row) for row in decisions]
            payload["trade_plans"] = [self._trade_plan_to_dict(row) for row in plans]
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
    def _decision_to_dict(cls, row: StockSelectionAgentDecision) -> Dict[str, Any]:
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
            "order_result": cls._json_loads(row.order_result_json, {}),
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
