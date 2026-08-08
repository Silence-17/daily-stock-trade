# -*- coding: utf-8 -*-
"""Persistent acceptance gate for the cross-market paper strategy."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from src.repositories.stock_selection_agent_repo import StockSelectionAgentRepository
from src.core.trading_calendar import is_market_open
from src.services.cross_market_paper_strategy import (
    GLOBAL_MARKET_LINKED_THEMES,
    STRATEGY_ID,
)


DEFAULT_ACCEPTANCE_PATH = Path("data") / "cross_market_strategy_acceptance.json"
DEFAULT_REQUIRED_TRADING_DAYS = 30
QUALIFIED_EXECUTION_MODES = {"dry_run", "vnpy_paper"}
QUALIFIED_RUN_STATUSES = {"completed"}
QUALIFIED_TRIGGER_SOURCES = {
    "cross_market_paper_observation",
    "vnpy_paper_auto",
}
FORMAL_QUALIFICATION_EXECUTION_MODES = {"vnpy_paper"}
FORMAL_QUALIFICATION_TRIGGER_SOURCES = {"vnpy_paper_auto"}
FORMAL_BASELINE_RUN_STATUSES = {"completed", "partial", "failed", "running"}
FORMAL_ENTRY_TIME = datetime_time(9, 35)
FORMAL_ENTRY_WINDOW_SECONDS = 120


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    return parsed.astimezone(timezone.utc)


class CrossMarketAcceptanceService:
    """Record optional replay evidence and count a forward paper campaign."""

    def __init__(
        self,
        *,
        repository: Optional[StockSelectionAgentRepository] = None,
        state_path: Optional[Path] = None,
        current_account_id: Optional[int] = None,
    ) -> None:
        self.repository = repository
        self.state_path = state_path or DEFAULT_ACCEPTANCE_PATH
        self.current_account_id = (
            int(current_account_id) if current_account_id is not None else None
        )

    def record_backtest(
        self,
        *,
        result: Dict[str, Any],
        dataset_kind: str,
        dataset_id: Optional[str],
        data_sources: List[str],
        minute_bar_source: Optional[str],
        request_payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        kind = str(dataset_kind or "deterministic_fixture").strip().lower()
        dataset_id_value = str(dataset_id or "").strip() or None
        source_values = sorted({str(item).strip() for item in data_sources if str(item).strip()})
        minute_source = str(minute_bar_source or "").strip() or None
        validation = result.get("validation") if isinstance(result.get("validation"), dict) else {}
        manifest_sha256 = str(request_payload.get("manifest_sha256") or "").strip().lower()
        frames_sha256 = str(request_payload.get("frames_sha256") or "").strip().lower()
        frame_count = int(request_payload.get("frame_count") or 0)
        provenance_schema_version = int(request_payload.get("provenance_schema_version") or 0)
        raw_artifact_count = int(request_payload.get("raw_artifact_count") or 0)
        raw_artifacts_sha256 = str(request_payload.get("raw_artifacts_sha256") or "").strip().lower()
        provenance_verified = bool(
            len(manifest_sha256) == 64
            and len(frames_sha256) == 64
            and all(character in "0123456789abcdef" for character in manifest_sha256 + frames_sha256)
            and frame_count >= int(result.get("session_count") or 0)
            and provenance_schema_version == 1
            and request_payload.get("source_artifacts_verified") is True
            and request_payload.get("frames_binding_verified") is True
            and raw_artifact_count >= len(source_values)
            and len(raw_artifacts_sha256) == 64
            and all(character in "0123456789abcdef" for character in raw_artifacts_sha256)
        )
        historical_eligible = bool(
            kind == "historical_market"
            and dataset_id_value
            and minute_source
            and source_values
            and provenance_verified
            and int(result.get("session_count") or 0) >= 500
            and validation.get("passed") is True
            and validation.get("theme_coverage_passed") is True
            and validation.get("range_coverage_passed") is True
            and validation.get("ablation_effects_passed") is True
        )
        canonical = json.dumps(request_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        record = {
            "schema_version": 3,
            "strategy_id": STRATEGY_ID,
            "recorded_at": _utc_now().isoformat(),
            "dataset_kind": kind,
            "dataset_id": dataset_id_value,
            "data_sources": source_values,
            "minute_bar_source": minute_source,
            "provenance_verified": provenance_verified,
            "manifest_sha256": manifest_sha256 or None,
            "frames_sha256": frames_sha256 or None,
            "frame_count": frame_count,
            "provenance_schema_version": provenance_schema_version,
            "source_artifacts_verified": request_payload.get("source_artifacts_verified") is True,
            "frames_binding_verified": request_payload.get("frames_binding_verified") is True,
            "raw_artifact_count": raw_artifact_count,
            "raw_artifacts_sha256": raw_artifacts_sha256 or None,
            "request_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            "session_count": int(result.get("session_count") or 0),
            "validation": validation,
            "historical_eligible": historical_eligible,
        }
        previous = self._read_state()
        paper_campaign = previous.get("paper_campaign")
        if isinstance(paper_campaign, dict):
            record["paper_campaign"] = paper_campaign
        previous_is_real = self._is_eligible_historical_record(previous)
        if historical_eligible or not previous_is_real:
            self._write_state(record)
            persisted = True
        else:
            persisted = False
        return {
            **record,
            "persisted": persisted,
            "preserved_prior_real_history": previous_is_real and not historical_eligible,
        }

    def start_paper_campaign(
        self,
        *,
        reset: bool = False,
        initial_equity: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Start the 30-trading-day forward simulation without historical prerequisites."""
        state = self._read_state()
        existing = state.get("paper_campaign")
        account_bound = False
        if isinstance(existing, dict) and existing.get("status") == "active" and not reset:
            if str(existing.get("strategy_id") or "").strip() != STRATEGY_ID:
                raise ValueError("paper_campaign_strategy_mismatch_requires_reset")
            existing_account_id = self._optional_int(existing.get("account_id"))
            if (
                existing_account_id is not None
                and self.current_account_id is not None
                and existing_account_id != self.current_account_id
            ):
                raise ValueError("paper_campaign_account_mismatch_requires_reset")
        if isinstance(existing, dict) and existing.get("status") == "active" and not reset:
            started = False
            if existing.get("account_id") is None and self.current_account_id is not None:
                existing = {
                    **existing,
                    "schema_version": max(2, int(existing.get("schema_version") or 1)),
                    "account_id": self.current_account_id,
                    "initial_equity": (
                        float(initial_equity) if initial_equity is not None else None
                    ),
                    "account_bound_at": _utc_now().isoformat(),
                }
                account_bound = True
        else:
            existing = {
                "schema_version": 2,
                "strategy_id": STRATEGY_ID,
                "status": "active",
                "started_at": _utc_now().isoformat(),
                "required_trading_days": DEFAULT_REQUIRED_TRADING_DAYS,
                "account_id": self.current_account_id,
                "initial_equity": (
                    float(initial_equity) if initial_equity is not None else None
                ),
            }
            started = True
        historical_evidence_cleared = any(
            key != "paper_campaign" for key in state
        )
        if started or account_bound or historical_evidence_cleared:
            self._write_state({"paper_campaign": existing})
        return {
            "started": started,
            "reset": bool(reset and started),
            "account_bound": account_bound,
            "historical_evidence_cleared": historical_evidence_cleared,
            "campaign": existing,
            "status": self.get_status(),
        }

    def get_status(self) -> Dict[str, Any]:
        state = self._read_state()
        historical_ready = self._is_eligible_historical_record(state)
        campaign = state.get("paper_campaign") if isinstance(state.get("paper_campaign"), dict) else {}
        campaign_active = bool(
            campaign.get("status") == "active"
            and campaign.get("strategy_id") == STRATEGY_ID
        )
        campaign_started_at = _parse_datetime(campaign.get("started_at")) if campaign_active else None
        campaign_account_id = self._optional_int(campaign.get("account_id"))
        account_matches_current = bool(
            self.current_account_id is None
            or campaign_account_id == self.current_account_id
        )
        required_trading_days = int(
            campaign.get("required_trading_days") or DEFAULT_REQUIRED_TRADING_DAYS
        )
        runs = self._list_strategy_runs(created_from=campaign_started_at) if campaign_started_at else []
        qualified: Dict[str, Dict[str, Any]] = {}
        formal_executions: Dict[str, Dict[str, Any]] = {}
        formal_candidates: List[Dict[str, Any]] = []
        rejected_counts: Dict[str, int] = {}
        formal_rejected_counts: Dict[str, int] = {}
        opening_formal_dates = set()
        shanghai = ZoneInfo("Asia/Shanghai")
        for run in runs:
            if (
                str(run.get("status") or "").strip().lower()
                not in FORMAL_BASELINE_RUN_STATUSES
                or not self._is_formal_execution_run(run)
            ):
                continue
            created_at = _parse_datetime(run.get("created_at"))
            if (
                created_at is None
                or campaign_started_at is None
                or created_at <= campaign_started_at
            ):
                continue
            settings = (
                run.get("settings")
                if isinstance(run.get("settings"), dict)
                else {}
            )
            if campaign_account_id is not None and self._optional_int(
                settings.get("account_id")
            ) != campaign_account_id:
                continue
            session_day = created_at.astimezone(shanghai).date()
            if not is_market_open("cn", session_day):
                continue
            baseline_candidate = {
                "run": run,
                "created_at": created_at,
                "session_date": session_day.isoformat(),
            }
            if self._is_formal_opening_baseline(baseline_candidate):
                opening_formal_dates.add(session_day.isoformat())
        for run in runs:
            reason = self._run_rejection_reason(
                run,
                campaign_started_at=campaign_started_at,
                campaign_account_id=campaign_account_id,
            )
            if reason:
                rejected_counts[reason] = rejected_counts.get(reason, 0) + 1
                continue
            created_at = _parse_datetime(run.get("created_at"))
            if created_at is None:
                rejected_counts["run_timestamp_unavailable"] = rejected_counts.get("run_timestamp_unavailable", 0) + 1
                continue
            session_date = created_at.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
            if not is_market_open("cn", created_at.astimezone(ZoneInfo("Asia/Shanghai")).date()):
                rejected_counts["non_trading_day"] = rejected_counts.get("non_trading_day", 0) + 1
                continue
            diagnostics = run.get("diagnostics") if isinstance(run.get("diagnostics"), dict) else {}
            observation = (
                diagnostics.get("cross_market_observation")
                if isinstance(diagnostics.get("cross_market_observation"), dict)
                else {}
            )
            observation = self._normalize_observation_contract(observation)
            candidate = {
                "run": run,
                "observation": observation,
                "fully_evidenced": observation.get("status") == "ready",
                "missing_requirement_count": self._missing_requirement_count(observation),
                "created_at": created_at,
                "session_date": session_date,
            }
            existing = qualified.get(session_date)
            if existing is None or self._prefer_daily_observation(candidate, existing):
                qualified[session_date] = candidate
            if self._is_formal_execution_run(run):
                formal_candidates.append(candidate)

        for candidate in formal_candidates:
            timing_rejection = self._formal_execution_timing_rejection_reason(
                candidate,
                opening_formal_dates=opening_formal_dates,
            )
            if timing_rejection:
                formal_rejected_counts[timing_rejection] = (
                    formal_rejected_counts.get(timing_rejection, 0) + 1
                )
                continue
            session_date = str(candidate.get("session_date") or "")
            existing_formal = formal_executions.get(session_date)
            if existing_formal is None or self._prefer_daily_observation(
                candidate,
                existing_formal,
            ):
                formal_executions[session_date] = candidate

        all_observation_dates = sorted(qualified)
        all_fully_evidenced_dates = sorted(
            session_date
            for session_date, item in qualified.items()
            if item["fully_evidenced"]
        )
        all_fully_evidenced_formal_execution_dates = sorted(
            session_date
            for session_date, item in formal_executions.items()
            if item["fully_evidenced"]
        )
        completion_session_date = (
            all_fully_evidenced_formal_execution_dates[required_trading_days - 1]
            if len(all_fully_evidenced_formal_execution_dates)
            >= required_trading_days
            else None
        )
        observation_dates = [
            session_date
            for session_date in all_observation_dates
            if completion_session_date is None
            or session_date <= completion_session_date
        ]
        fully_evidenced_dates = [
            session_date
            for session_date in all_fully_evidenced_dates
            if completion_session_date is None
            or session_date <= completion_session_date
        ]
        degraded_dates = sorted(set(observation_dates) - set(fully_evidenced_dates))
        session_results = [
            self._session_result(
                session_date,
                qualified[session_date],
                formal_execution=formal_executions.get(session_date),
            )
            for session_date in observation_dates
        ]
        formal_execution_dates = [
            session_date
            for session_date in observation_dates
            if session_date in formal_executions
        ]
        fully_evidenced_formal_execution_dates = [
            session_date
            for session_date in all_fully_evidenced_formal_execution_dates
            if completion_session_date is None
            or session_date <= completion_session_date
        ]
        qualified_paper_trading_dates = list(
            fully_evidenced_formal_execution_dates
        )
        qualified_date_set = set(qualified_paper_trading_dates)
        evidence_only_dates = [
            session_date
            for session_date in observation_dates
            if session_date not in formal_executions
        ]
        fully_evidenced_without_formal_execution_dates = [
            session_date
            for session_date in fully_evidenced_dates
            if session_date not in formal_executions
        ]
        fully_evidenced_via_later_observation_dates = [
            session_date
            for session_date in fully_evidenced_dates
            if session_date in formal_executions
            and not bool(formal_executions[session_date].get("fully_evidenced"))
        ]
        missing_requirement_counts: Dict[str, int] = {}
        for session_date in degraded_dates:
            observation = qualified[session_date]["observation"]
            missing = observation.get("missing_requirements")
            requirements = missing if isinstance(missing, list) else ["unspecified"]
            for requirement in {
                str(item).strip() or "unspecified"
                for item in requirements
            }:
                missing_requirement_counts[requirement] = (
                    missing_requirement_counts.get(requirement, 0) + 1
                )
        observed_days = len(observation_dates)
        fully_evidenced_days = len(fully_evidenced_dates)
        qualified_formal_results = [
            item.get("formal_execution")
            for item in session_results
            if item.get("session_date") in qualified_date_set
            and isinstance(item.get("formal_execution"), dict)
        ]
        qualified_candidate_count = sum(
            int(item.get("candidate_count") or 0)
            for item in qualified_formal_results
        )
        qualified_planned_count = sum(
            int(item.get("planned_count") or 0)
            for item in qualified_formal_results
        )
        qualified_submitted_count = sum(
            int(item.get("submitted_count") or 0)
            for item in qualified_formal_results
        )
        qualified_active_trade_days = sum(
            1
            for item in qualified_formal_results
            if int(item.get("submitted_count") or 0) > 0
        )
        completion_item = (
            formal_executions.get(completion_session_date)
            if completion_session_date is not None
            else None
        )
        completion_created_at = (
            completion_item.get("created_at")
            if isinstance(completion_item, dict)
            else None
        )
        paper_ready = bool(
            campaign_active
            and account_matches_current
            and completion_session_date is not None
        )
        historical_backtest = {
            key: value
            for key, value in state.items()
            if key != "paper_campaign"
        }
        return {
            "strategy_id": STRATEGY_ID,
            "generated_at": _utc_now().isoformat(),
            "historical_backtest": historical_backtest or {
                "historical_eligible": False,
                "reason": "historical_backtest_not_required",
            },
            "historical_ready": historical_ready,
            "historical_required": False,
            "paper_observation": {
                "campaign_active": campaign_active,
                "campaign_started_at": (
                    campaign_started_at.isoformat() if campaign_started_at else None
                ),
                "campaign_account_id": campaign_account_id,
                "current_account_id": self.current_account_id,
                "account_matches_current": account_matches_current,
                "initial_equity": campaign.get("initial_equity"),
                "required_trading_days": required_trading_days,
                "observed_trading_days": observed_days,
                "remaining_trading_days": max(
                    0,
                    required_trading_days - len(qualified_paper_trading_dates),
                ),
                "fully_evidenced_trading_days": fully_evidenced_days,
                "qualified_paper_trading_days": len(
                    qualified_paper_trading_dates
                ),
                "qualified_candidate_count": qualified_candidate_count,
                "qualified_planned_count": qualified_planned_count,
                "qualified_submitted_count": qualified_submitted_count,
                "qualified_active_trade_days": qualified_active_trade_days,
                "degraded_trading_days": len(degraded_dates),
                "data_completeness_pct": round(
                    fully_evidenced_days / observed_days * 100.0,
                    2,
                ) if observed_days else None,
                "first_observation_date": observation_dates[0] if observation_dates else None,
                "latest_observation_date": observation_dates[-1] if observation_dates else None,
                "completion_session_date": completion_session_date,
                "completed_at": (
                    completion_created_at.isoformat()
                    if isinstance(completion_created_at, datetime)
                    else None
                ),
                "post_completion_observation_days": sum(
                    1
                    for session_date in all_observation_dates
                    if completion_session_date is not None
                    and session_date > completion_session_date
                ),
                "observation_dates": observation_dates,
                "fully_evidenced_observation_dates": fully_evidenced_dates,
                "degraded_observation_dates": degraded_dates,
                "session_results": session_results,
                "formal_execution_trading_days": len(formal_execution_dates),
                "fully_evidenced_formal_execution_days": len(
                    fully_evidenced_formal_execution_dates
                ),
                "evidence_only_trading_days": len(evidence_only_dates),
                "fully_evidenced_without_formal_execution_days": len(
                    fully_evidenced_without_formal_execution_dates
                ),
                "fully_evidenced_via_later_observation_days": len(
                    fully_evidenced_via_later_observation_dates
                ),
                "formal_execution_observation_dates": formal_execution_dates,
                "fully_evidenced_formal_execution_dates": (
                    fully_evidenced_formal_execution_dates
                ),
                "qualified_paper_trading_dates": qualified_paper_trading_dates,
                "evidence_only_observation_dates": evidence_only_dates,
                "fully_evidenced_without_formal_execution_dates": (
                    fully_evidenced_without_formal_execution_dates
                ),
                "fully_evidenced_via_later_observation_dates": (
                    fully_evidenced_via_later_observation_dates
                ),
                "missing_requirement_counts": missing_requirement_counts,
                "observation_execution_modes": sorted(QUALIFIED_EXECUTION_MODES),
                "observation_trigger_sources": sorted(QUALIFIED_TRIGGER_SOURCES),
                "qualified_execution_modes": sorted(
                    FORMAL_QUALIFICATION_EXECUTION_MODES
                ),
                "qualified_trigger_sources": sorted(
                    FORMAL_QUALIFICATION_TRIGGER_SOURCES
                ),
                "rejected_run_counts": rejected_counts,
                "formal_execution_rejected_counts": formal_rejected_counts,
                "formal_entry_time": FORMAL_ENTRY_TIME.strftime("%H:%M"),
                "formal_entry_window_seconds": FORMAL_ENTRY_WINDOW_SECONDS,
                "completion_basis": "fully_evidenced_formal_vnpy_paper_run",
                "ready": paper_ready,
            },
            "ready": paper_ready,
        }

    @staticmethod
    def _missing_requirement_count(observation: Dict[str, Any]) -> int:
        missing = observation.get("missing_requirements")
        if not isinstance(missing, list):
            return 1
        normalized = {
            str(item).strip()
            for item in missing
            if str(item).strip()
        }
        return len(normalized)

    @staticmethod
    def _normalize_observation_contract(
        observation: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Fail closed when a ready row cannot prove the v1.3 evidence contract."""

        if observation.get("status") != "ready":
            return observation
        evidence = (
            observation.get("evidence")
            if isinstance(observation.get("evidence"), dict)
            else {}
        )
        korea = evidence.get("korea") if isinstance(evidence.get("korea"), dict) else {}
        gate = (
            korea.get("linked_technology_gate")
            if isinstance(korea.get("linked_technology_gate"), dict)
            else {}
        )
        nasdaq_futures = (
            evidence.get("nasdaq_futures")
            if isinstance(evidence.get("nasdaq_futures"), dict)
            else {}
        )
        us_premarket = (
            evidence.get("us_premarket")
            if isinstance(evidence.get("us_premarket"), dict)
            else {}
        )
        us_close_themes = (
            evidence.get("us_close_themes")
            if isinstance(evidence.get("us_close_themes"), dict)
            else {}
        )
        us_theme_collection_audit = (
            evidence.get("us_theme_collection_audit")
            if isinstance(evidence.get("us_theme_collection_audit"), dict)
            else {}
        )
        try:
            schema_version = int(observation.get("schema_version") or 0)
        except (TypeError, ValueError):
            schema_version = 0
        try:
            span_seconds = float(gate.get("confirmation_span_seconds") or 0.0)
            duration_seconds = float(
                gate.get("confirmation_duration_seconds") or 0.0
            )
        except (TypeError, ValueError):
            span_seconds = 0.0
            duration_seconds = 0.0
        try:
            nasdaq_span_seconds = float(
                nasdaq_futures.get("confirmation_span_seconds") or 0.0
            )
            nasdaq_sample_count = int(
                nasdaq_futures.get("confirmation_sample_count") or 0
            )
        except (TypeError, ValueError):
            nasdaq_span_seconds = 0.0
            nasdaq_sample_count = 0
        checks = (
            dict(observation.get("required_checks"))
            if isinstance(observation.get("required_checks"), dict)
            else {}
        )
        korea_proven = bool(
            schema_version >= 3
            and span_seconds >= 300.0
            and duration_seconds >= 300.0
        )
        required_themes = set(GLOBAL_MARKET_LINKED_THEMES)

        def full_theme_coverage_proven(payload: Dict[str, Any]) -> bool:
            recorded_required = {
                str(item).strip().lower()
                for item in list(payload.get("required_themes") or [])
                if str(item).strip()
            }
            qualified = {
                str(item).strip().lower()
                for item in list(payload.get("qualified_themes") or [])
                if str(item).strip()
            }
            missing = {
                str(item).strip().lower()
                for item in list(payload.get("missing_themes") or [])
                if str(item).strip()
            }
            return bool(
                schema_version >= 5
                and payload.get("full_strategy_coverage") is True
                and recorded_required == required_themes
                and qualified == required_themes
                and not missing
            )

        premarket_proven = bool(
            checks.get("us_premarket_themes_available") is True
            and full_theme_coverage_proven(us_premarket)
        )
        close_themes_proven = bool(
            checks.get("us_close_themes_available") is True
            and full_theme_coverage_proven(us_close_themes)
        )
        premarket_capture = (
            us_premarket.get("latest_capture")
            if isinstance(us_premarket.get("latest_capture"), dict)
            else {}
        )
        close_collection = (
            us_close_themes.get("collection")
            if isinstance(us_close_themes.get("collection"), dict)
            else {}
        )

        def normalized_theme_set(value: Any) -> set[str]:
            return {
                str(item).strip().lower()
                for item in list(value or [])
                if str(item).strip()
            }

        def positive_int(value: Any) -> Optional[int]:
            if isinstance(value, bool):
                return None
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                return None
            return parsed if parsed > 0 else None

        audit_required_themes = normalized_theme_set(
            us_theme_collection_audit.get("required_themes")
        )
        premarket_capture_themes = normalized_theme_set(
            premarket_capture.get("required_themes")
        )
        close_recorded_themes = normalized_theme_set(
            us_close_themes.get("required_themes")
        )
        close_theme_payloads = (
            us_close_themes.get("themes")
            if isinstance(us_close_themes.get("themes"), dict)
            else {}
        )
        close_payload_themes = {
            str(item).strip().lower()
            for item in close_theme_payloads
            if str(item).strip()
        }
        premarket_session_date = str(
            premarket_capture.get("session_date") or ""
        ).strip()
        close_session_date = str(
            close_collection.get("latest_session_date") or ""
        ).strip()
        premarket_collection_proven = bool(
            schema_version >= 6
            and checks.get("us_premarket_collection_audited") is True
            and us_theme_collection_audit.get("premarket_collection_audited")
            is True
            and audit_required_themes == required_themes
            and premarket_capture.get("available") is True
            and str(premarket_capture.get("session_stage") or "").strip().lower()
            == "premarket"
            and premarket_capture_themes == required_themes
            and positive_int(premarket_capture.get("universe_size")) is not None
            and positive_int(
                premarket_capture.get("collected_component_count")
            )
            is not None
            and premarket_session_date
            and premarket_session_date
            == str(
                us_theme_collection_audit.get("premarket_session_date") or ""
            ).strip()
        )
        close_collection_proven = bool(
            schema_version >= 6
            and checks.get("us_close_collection_audited") is True
            and us_theme_collection_audit.get("close_collection_audited") is True
            and audit_required_themes == required_themes
            and close_recorded_themes == required_themes
            and required_themes.issubset(close_payload_themes)
            and us_close_themes.get("available") is True
            and close_collection.get("required_stages_complete") is True
            and close_session_date
            and close_session_date
            == str(
                us_theme_collection_audit.get("close_session_date") or ""
            ).strip()
        )
        us_theme_session_pair_proven = bool(
            schema_version >= 6
            and checks.get("us_theme_session_pair_available") is True
            and us_theme_collection_audit.get("session_pair_available") is True
            and premarket_collection_proven
            and close_collection_proven
            and premarket_session_date == close_session_date
        )
        japan_proven = checks.get("japan_market_available") is True
        nasdaq_futures_proven = bool(
            schema_version >= 4
            and checks.get("nasdaq_futures_continuous_trend_available") is True
            and nasdaq_futures.get("code") == "NQ00Y"
            and nasdaq_futures.get("available") is True
            and nasdaq_futures.get("confirmed") is True
            and nasdaq_span_seconds >= 120.0
            and nasdaq_sample_count >= 3
        )
        stage_aligned_us_evidence_proven = bool(
            premarket_collection_proven
            and close_collection_proven
            and us_theme_session_pair_proven
        )
        legacy_full_theme_evidence_proven = bool(
            premarket_proven and close_themes_proven
        )
        us_evidence_proven = (
            stage_aligned_us_evidence_proven
            if schema_version >= 6
            else legacy_full_theme_evidence_proven
        )
        if (
            korea_proven
            and us_evidence_proven
            and japan_proven
            and nasdaq_futures_proven
        ):
            return observation

        normalized = dict(observation)
        missing = observation.get("missing_requirements")
        missing_requirements = {
            str(item).strip()
            for item in (missing if isinstance(missing, list) else [])
            if str(item).strip()
        }
        contract_reasons = []
        if not korea_proven:
            checks["korea_continuous_gate_available"] = False
            missing_requirements.add("korea_continuous_gate_available")
            contract_reasons.append("korea_confirmation_duration_unproven")
        if schema_version >= 6:
            if not premarket_collection_proven:
                checks["us_premarket_collection_audited"] = False
                missing_requirements.add("us_premarket_collection_audited")
                contract_reasons.append("us_premarket_collection_audit_unproven")
            if not close_collection_proven:
                checks["us_close_collection_audited"] = False
                missing_requirements.add("us_close_collection_audited")
                contract_reasons.append("us_close_collection_audit_unproven")
            if not us_theme_session_pair_proven:
                checks["us_theme_session_pair_available"] = False
                missing_requirements.add("us_theme_session_pair_available")
                contract_reasons.append("us_theme_session_pair_unproven")
        else:
            if not premarket_proven:
                checks["us_premarket_themes_available"] = False
                missing_requirements.add("us_premarket_themes_available")
                contract_reasons.append("us_premarket_theme_evidence_unproven")
            if not close_themes_proven:
                checks["us_close_themes_available"] = False
                missing_requirements.add("us_close_themes_available")
                contract_reasons.append("us_close_theme_evidence_unproven")
        if not japan_proven:
            checks["japan_market_available"] = False
            missing_requirements.add("japan_market_available")
            contract_reasons.append("japan_market_evidence_unproven")
        if not nasdaq_futures_proven:
            checks["nasdaq_futures_continuous_trend_available"] = False
            missing_requirements.add(
                "nasdaq_futures_continuous_trend_available"
            )
            contract_reasons.append("nasdaq_futures_trend_evidence_unproven")
        normalized.update({
            "status": "unavailable",
            "required_checks": checks,
            "missing_requirements": sorted(missing_requirements),
            "contract_reason": ";".join(contract_reasons),
        })
        return normalized

    @staticmethod
    def _session_result(
        session_date: str,
        item: Dict[str, Any],
        *,
        formal_execution: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Return one bounded, serializable audit row for the selected daily run."""

        run = item.get("run") if isinstance(item.get("run"), dict) else {}
        observation = (
            item.get("observation")
            if isinstance(item.get("observation"), dict)
            else {}
        )
        created_at = item.get("created_at")
        created_at_text = (
            created_at.isoformat()
            if isinstance(created_at, datetime)
            else str(run.get("created_at") or "").strip() or None
        )
        missing = observation.get("missing_requirements")
        missing_requirements = sorted({
            str(requirement).strip()
            for requirement in (missing if isinstance(missing, list) else [])
            if str(requirement).strip()
        })
        fully_evidenced = bool(item.get("fully_evidenced"))
        formal_fully_evidenced = bool(
            isinstance(formal_execution, dict)
            and formal_execution.get("fully_evidenced")
        )
        return {
            "session_date": session_date,
            "evidence_status": "ready" if fully_evidenced else "degraded",
            "fully_evidenced": fully_evidenced,
            "qualifies_for_campaign": formal_fully_evidenced,
            "missing_requirements": missing_requirements,
            "run_id": CrossMarketAcceptanceService._optional_int(run.get("id")),
            "run_uid": str(run.get("run_uid") or "").strip() or None,
            "run_status": str(run.get("status") or "").strip() or None,
            "trigger_source": str(run.get("trigger_source") or "").strip() or None,
            "execution_mode": (
                CrossMarketAcceptanceService._run_execution_mode(run) or None
            ),
            "created_at": created_at_text,
            "evidence_checked_at": (
                str(observation.get("checked_at") or "").strip() or None
            ),
            "candidate_count": int(run.get("candidate_count") or 0),
            "planned_count": int(run.get("planned_count") or 0),
            "submitted_count": int(run.get("submitted_count") or 0),
            "skipped_count": int(run.get("skipped_count") or 0),
            "formal_execution": CrossMarketAcceptanceService._formal_execution_result(
                formal_execution
            ),
        }

    @staticmethod
    def _formal_execution_result(item: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if not isinstance(item, dict):
            return {
                "observed": False,
                "fully_evidenced": False,
                "evidence_status": "unavailable",
                "missing_requirements": [],
                "run_id": None,
                "run_uid": None,
                "run_status": None,
                "trigger_source": None,
                "execution_mode": None,
                "created_at": None,
                "candidate_count": 0,
                "planned_count": 0,
                "submitted_count": 0,
                "skipped_count": 0,
                "formal_recovery": False,
            }
        run = item.get("run") if isinstance(item.get("run"), dict) else {}
        observation = (
            item.get("observation")
            if isinstance(item.get("observation"), dict)
            else {}
        )
        created_at = item.get("created_at")
        missing = observation.get("missing_requirements")
        fully_evidenced = bool(item.get("fully_evidenced"))
        return {
            "observed": True,
            "fully_evidenced": fully_evidenced,
            "evidence_status": "ready" if fully_evidenced else "degraded",
            "missing_requirements": sorted({
                str(requirement).strip()
                for requirement in (missing if isinstance(missing, list) else [])
                if str(requirement).strip()
            }),
            "run_id": CrossMarketAcceptanceService._optional_int(run.get("id")),
            "run_uid": str(run.get("run_uid") or "").strip() or None,
            "run_status": str(run.get("status") or "").strip() or None,
            "trigger_source": str(run.get("trigger_source") or "").strip() or None,
            "execution_mode": (
                CrossMarketAcceptanceService._run_execution_mode(run) or None
            ),
            "created_at": (
                created_at.isoformat()
                if isinstance(created_at, datetime)
                else str(run.get("created_at") or "").strip() or None
            ),
            "candidate_count": int(run.get("candidate_count") or 0),
            "planned_count": int(run.get("planned_count") or 0),
            "submitted_count": int(run.get("submitted_count") or 0),
            "skipped_count": int(run.get("skipped_count") or 0),
            "formal_recovery": CrossMarketAcceptanceService._is_formal_recovery_run(
                run
            ),
        }

    @staticmethod
    def _prefer_daily_observation(
        candidate: Dict[str, Any],
        existing: Dict[str, Any],
    ) -> bool:
        if bool(candidate["fully_evidenced"]) != bool(existing["fully_evidenced"]):
            return bool(candidate["fully_evidenced"])
        candidate_missing = int(candidate.get("missing_requirement_count") or 0)
        existing_missing = int(existing.get("missing_requirement_count") or 0)
        if candidate_missing != existing_missing:
            return candidate_missing < existing_missing
        candidate_mode = CrossMarketAcceptanceService._run_execution_mode(
            candidate.get("run")
        )
        existing_mode = CrossMarketAcceptanceService._run_execution_mode(
            existing.get("run")
        )
        mode_priority = {"vnpy_paper": 1, "dry_run": 0}
        if mode_priority.get(candidate_mode, -1) != mode_priority.get(
            existing_mode,
            -1,
        ):
            return mode_priority.get(candidate_mode, -1) > mode_priority.get(
                existing_mode,
                -1,
            )
        return candidate["created_at"] > existing["created_at"]

    @staticmethod
    def _run_execution_mode(run: Any) -> str:
        run_payload = run if isinstance(run, dict) else {}
        settings = (
            run_payload.get("settings")
            if isinstance(run_payload.get("settings"), dict)
            else {}
        )
        diagnostics = (
            run_payload.get("diagnostics")
            if isinstance(run_payload.get("diagnostics"), dict)
            else {}
        )
        return str(
            settings.get("auto_execution_mode")
            or diagnostics.get("execution_mode")
            or ""
        ).strip().lower()

    @staticmethod
    def _is_formal_execution_run(run: Any) -> bool:
        run_payload = run if isinstance(run, dict) else {}
        return bool(
            str(run_payload.get("trigger_source") or "").strip()
            == "vnpy_paper_auto"
            and CrossMarketAcceptanceService._run_execution_mode(run_payload)
            == "vnpy_paper"
        )

    @staticmethod
    def _is_formal_recovery_run(run: Any) -> bool:
        run_payload = run if isinstance(run, dict) else {}
        diagnostics = (
            run_payload.get("diagnostics")
            if isinstance(run_payload.get("diagnostics"), dict)
            else {}
        )
        return bool(
            diagnostics.get("formal_recovery") is True
            or diagnostics.get("intraday_entry_recheck") is True
        )

    @staticmethod
    def _is_formal_opening_baseline(item: Dict[str, Any]) -> bool:
        run = item.get("run") if isinstance(item.get("run"), dict) else {}
        created_at = item.get("created_at")
        if (
            not isinstance(created_at, datetime)
            or CrossMarketAcceptanceService._is_formal_recovery_run(run)
        ):
            return False
        diagnostics = (
            run.get("diagnostics")
            if isinstance(run.get("diagnostics"), dict)
            else {}
        )
        formal_entry_started_at = _parse_datetime(
            diagnostics.get("formal_entry_started_at")
        )
        entry_started_at = formal_entry_started_at or created_at
        shanghai = ZoneInfo("Asia/Shanghai")
        local_created_at = created_at.astimezone(shanghai)
        local_entry_started_at = entry_started_at.astimezone(shanghai)
        if local_entry_started_at.date() != local_created_at.date():
            return False
        window_start = datetime.combine(
            local_entry_started_at.date(),
            FORMAL_ENTRY_TIME,
            tzinfo=local_entry_started_at.tzinfo,
        )
        window_end = window_start + timedelta(
            seconds=FORMAL_ENTRY_WINDOW_SECONDS
        )
        return window_start <= local_entry_started_at < window_end

    @staticmethod
    def _formal_execution_timing_rejection_reason(
        item: Dict[str, Any],
        *,
        opening_formal_dates: set[str],
    ) -> Optional[str]:
        if CrossMarketAcceptanceService._is_formal_opening_baseline(item):
            return None
        run = item.get("run") if isinstance(item.get("run"), dict) else {}
        session_date = str(item.get("session_date") or "").strip()
        if CrossMarketAcceptanceService._is_formal_recovery_run(run):
            if session_date and session_date in opening_formal_dates:
                return None
            return "formal_recovery_without_opening_baseline"
        return "formal_entry_outside_opening_window"

    @staticmethod
    def _is_eligible_historical_record(record: Dict[str, Any]) -> bool:
        validation = record.get("validation") if isinstance(record.get("validation"), dict) else {}
        return bool(
            int(record.get("schema_version") or 0) >= 3
            and record.get("historical_eligible") is True
            and validation.get("passed") is True
            and validation.get("theme_coverage_passed") is True
            and validation.get("range_coverage_passed") is True
            and validation.get("ablation_effects_passed") is True
        )

    def _list_strategy_runs(self, *, created_from: Optional[datetime]) -> List[Dict[str, Any]]:
        repository = self.repository or StockSelectionAgentRepository()
        runs: List[Dict[str, Any]] = []
        seen_ids = set()
        for trigger_source in sorted(QUALIFIED_TRIGGER_SOURCES):
            offset = 0
            while True:
                payload = repository.list_runs(
                    limit=100,
                    offset=offset,
                    trigger_source=trigger_source,
                    strategy=STRATEGY_ID,
                    market="cn",
                    created_from=created_from,
                )
                items = [item for item in payload.get("items", []) if isinstance(item, dict)]
                for item in items:
                    run_key = item.get("id") or item.get("run_uid") or (
                        item.get("created_at"),
                        item.get("trigger_source"),
                    )
                    if run_key in seen_ids:
                        continue
                    seen_ids.add(run_key)
                    runs.append(item)
                offset += len(items)
                if not items or offset >= int(payload.get("total") or 0):
                    break
        return runs

    @staticmethod
    def _run_rejection_reason(
        run: Dict[str, Any],
        *,
        campaign_started_at: Optional[datetime],
        campaign_account_id: Optional[int] = None,
    ) -> Optional[str]:
        if str(run.get("status") or "").strip().lower() not in QUALIFIED_RUN_STATUSES:
            return "run_not_completed"
        created_at = _parse_datetime(run.get("created_at"))
        if created_at is None:
            return "run_timestamp_unavailable"
        if campaign_started_at is None or created_at <= campaign_started_at:
            return "run_before_campaign_start"
        settings = run.get("settings") if isinstance(run.get("settings"), dict) else {}
        if campaign_account_id is not None:
            run_account_id = CrossMarketAcceptanceService._optional_int(
                settings.get("account_id")
            )
            if run_account_id is None:
                return "run_account_unavailable"
            if run_account_id != campaign_account_id:
                return "run_account_mismatch"
        diagnostics = run.get("diagnostics") if isinstance(run.get("diagnostics"), dict) else {}
        execution_mode = str(
            settings.get("auto_execution_mode")
            or diagnostics.get("execution_mode")
            or ""
        ).strip().lower()
        if execution_mode not in QUALIFIED_EXECUTION_MODES:
            return "execution_mode_not_qualified"
        local_time = created_at.astimezone(ZoneInfo("Asia/Shanghai")).time()
        in_cn_session = (
            datetime_time(9, 30) <= local_time <= datetime_time(11, 30)
            or datetime_time(13, 0) <= local_time <= datetime_time(15, 0)
        )
        if not in_cn_session:
            return "run_outside_cn_observation_session"
        observation = (
            diagnostics.get("cross_market_observation")
            if isinstance(diagnostics.get("cross_market_observation"), dict)
            else {}
        )
        if observation.get("strategy_id") != STRATEGY_ID:
            return "cross_market_observation_not_recorded"
        if observation.get("status") not in {"ready", "unavailable"}:
            return "cross_market_observation_status_invalid"
        checked_at = _parse_datetime(observation.get("checked_at"))
        if checked_at is None:
            return "cross_market_observation_timestamp_unavailable"
        observation_skew_seconds = abs((created_at - checked_at).total_seconds())
        if observation_skew_seconds > 120:
            return "cross_market_observation_timestamp_misaligned"
        return None

    @staticmethod
    def _optional_int(value: Any) -> Optional[int]:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def _read_state(self) -> Dict[str, Any]:
        if not self.state_path.is_file():
            return {}
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _write_state(self, payload: Dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_name(
            f".{self.state_path.name}.{os.getpid()}.tmp"
        )
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temporary, self.state_path)
