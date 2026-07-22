# -*- coding: utf-8 -*-
"""Point-in-time forward evaluation for persisted stock-selection Agent decisions."""

from __future__ import annotations

import math
import statistics
from collections import Counter
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from src.core.backtest_engine import BacktestEngine, EvaluationConfig
from src.repositories.stock_selection_agent_repo import StockSelectionAgentRepository
from src.services.backtest_service import BacktestService
from src.storage import DatabaseManager


class StockSelectionAgentBacktestService:
    """Evaluate recorded Agent buy candidates against strictly later daily bars."""

    ENGINE_VERSION = "agent-forward-v1"
    RETURN_RISK_OBJECTIVE_VERSION = "candidate-return-risk-v1"
    RETURN_RISK_DOWNSIDE_WEIGHT = 0.5
    RETURN_RISK_ADVERSE_EXCURSION_WEIGHT = 0.25
    DEFAULT_WINDOWS = (1, 5, 10, 20)

    def build_quality_snapshot(
        self,
        *,
        strategy: str,
        market: str,
        horizon_days: int = 5,
        min_mature_samples: int = 10,
        min_win_rate_pct: float = 45.0,
        max_decisions: int = 200,
        previous_state: Optional[str] = None,
        refresh_missing: bool = False,
    ) -> Dict[str, Any]:
        """Build a deterministic rolling quality state from mature persisted candidates."""

        horizon = int(horizon_days)
        minimum = max(1, int(min_mature_samples))
        threshold = float(min_win_rate_pct)
        result = self.evaluate(
            strategy=strategy,
            market=market,
            eval_windows=[horizon],
            include_skipped=True,
            max_decisions=max_decisions,
            refresh_missing=refresh_missing,
        )
        metrics = dict(result["matrix"].get(str(horizon)) or {})
        completed_count = int(metrics.get("completed_count") or 0)
        win_rate = self._finite_float(metrics.get("win_rate_pct"))
        average_return = self._finite_float(metrics.get("average_return_pct"))
        if completed_count < minimum:
            selection_state = "insufficient_evidence"
            selection_reason = "mature_sample_count_below_threshold"
        elif win_rate is None:
            selection_state = "unavailable"
            selection_reason = "win_rate_unavailable"
        elif win_rate < threshold:
            selection_state = "blocked"
            selection_reason = "forward_win_rate_below_threshold"
        elif average_return is not None and average_return < 0:
            selection_state = "guarded"
            selection_reason = "average_forward_return_negative"
        else:
            selection_state = "healthy"
            selection_reason = "forward_quality_thresholds_met"
        review_policy_quality = dict(result.get("review_policy_quality") or {})
        review_metrics = dict(review_policy_quality.get("horizons", {}).get(str(horizon)) or {})
        review_state, review_reason = self._review_policy_state(
            review_metrics,
            min_mature_samples=minimum,
            min_accuracy_pct=threshold,
        )
        return_risk_objective = self._return_risk_objective_state(
            metrics,
            min_mature_samples=minimum,
        )
        objective_state = str(return_risk_objective["state"])
        objective_reason = str(return_risk_objective["reason"])
        severity = {
            "insufficient_evidence": 0,
            "healthy": 1,
            "guarded": 2,
            "blocked": 3,
            "unavailable": 4,
        }
        state = selection_state
        reason = selection_reason
        return_risk_objective_applied = severity.get(objective_state, 0) > severity.get(state, 0)
        if return_risk_objective_applied:
            state = objective_state
            reason = objective_reason
        review_quality_applied = severity.get(review_state, 0) > severity.get(state, 0)
        if review_quality_applied:
            state = review_state
            reason = review_reason
        previous = str(previous_state or "").strip().lower() or None
        return {
            "schema_version": 3,
            "generated_at": result.get("generated_at"),
            "state": state,
            "reason": reason,
            "previous_state": previous,
            "transition": f"{previous}->{state}" if previous and previous != state else None,
            "changed": bool(previous and previous != state),
            "strategy": strategy,
            "market": market,
            "selection_quality_state": selection_state,
            "selection_quality_reason": selection_reason,
            "return_risk_objective_state": objective_state,
            "return_risk_objective_reason": objective_reason,
            "return_risk_objective_applied": return_risk_objective_applied,
            "return_risk_objective": return_risk_objective,
            "review_quality_state": review_state,
            "review_quality_reason": review_reason,
            "review_quality_applied": review_quality_applied,
            "review_policy_quality": review_policy_quality,
            "horizon_days": horizon,
            "min_mature_samples": minimum,
            "min_win_rate_pct": threshold,
            "max_decisions": max(1, min(2000, int(max_decisions))),
            "refresh_missing": bool(refresh_missing),
            "refresh_attempted_count": int(result.get("refresh_attempted_count") or 0),
            "refresh_skipped_not_due_count": int(
                result.get("refresh_skipped_not_due_count") or 0
            ),
            "refresh_succeeded_count": int(result.get("refresh_succeeded_count") or 0),
            "refresh_failed_count": int(result.get("refresh_failed_count") or 0),
            "refresh_source_counts": dict(result.get("refresh_source_counts") or {}),
            "refresh_saved_row_count": int(result.get("refresh_saved_row_count") or 0),
            "refresh_resolved_anchor_count": int(
                result.get("refresh_resolved_anchor_count") or 0
            ),
            "refresh_unresolved_anchor_count": int(
                result.get("refresh_unresolved_anchor_count") or 0
            ),
            "sample_count": int(metrics.get("sample_count") or 0),
            "mature_sample_count": completed_count,
            "coverage_pct": metrics.get("coverage_pct"),
            "win_rate_pct": metrics.get("win_rate_pct"),
            "average_return_pct": metrics.get("average_return_pct"),
            "median_return_pct": metrics.get("median_return_pct"),
            "average_max_adverse_excursion_pct": metrics.get(
                "average_max_adverse_excursion_pct"
            ),
            "average_daily_return_pct": metrics.get("average_daily_return_pct"),
            "daily_return_coverage_pct": metrics.get("daily_return_coverage_pct"),
            "daily_return_volatility_pct": metrics.get("daily_return_volatility_pct"),
            "downside_deviation_pct": metrics.get("downside_deviation_pct"),
            "daily_expected_shortfall_20_pct": metrics.get(
                "daily_expected_shortfall_20_pct"
            ),
            "return_risk_utility_pct": metrics.get("return_risk_utility_pct"),
            "unable_reason_counts": dict(metrics.get("unable_reason_counts") or {}),
            "lookahead_protection": True,
            "source": "persisted_agent_decisions_and_stock_daily",
            "truncated": bool(result.get("truncated")),
        }

    @classmethod
    def _return_risk_objective_state(
        cls,
        metrics: Dict[str, Any],
        *,
        min_mature_samples: int,
    ) -> Dict[str, Any]:
        minimum = max(1, int(min_mature_samples))
        completed_count = int(metrics.get("completed_count") or 0)
        average_return = cls._finite_float(metrics.get("average_return_pct"))
        utility = cls._finite_float(metrics.get("return_risk_utility_pct"))
        if completed_count < minimum:
            state = "insufficient_evidence"
            reason = "return_risk_mature_sample_count_below_threshold"
        elif utility is None:
            state = "unavailable"
            reason = "return_risk_utility_unavailable"
        elif utility < 0 and average_return is not None and average_return < 0:
            state = "blocked"
            reason = "return_risk_negative_return_and_utility"
        elif utility < 0:
            state = "guarded"
            reason = "return_risk_utility_below_zero"
        else:
            state = "healthy"
            reason = "return_risk_objective_met"
        return {
            "schema_version": 1,
            "version": cls.RETURN_RISK_OBJECTIVE_VERSION,
            "state": state,
            "reason": reason,
            "min_mature_samples": minimum,
            "formula": (
                "average_forward_return_pct"
                " - 0.5 * horizon_downside_deviation_pct"
                " - 0.25 * abs(average_max_adverse_excursion_pct)"
            ),
            "weights": {
                "horizon_downside_deviation": cls.RETURN_RISK_DOWNSIDE_WEIGHT,
                "average_max_adverse_excursion": cls.RETURN_RISK_ADVERSE_EXCURSION_WEIGHT,
            },
            "metrics": {
                "completed_count": completed_count,
                "daily_observation_count": int(metrics.get("daily_observation_count") or 0),
                "daily_return_coverage_pct": metrics.get("daily_return_coverage_pct"),
                "average_forward_return_pct": average_return,
                "average_daily_return_pct": metrics.get("average_daily_return_pct"),
                "daily_return_volatility_pct": metrics.get("daily_return_volatility_pct"),
                "downside_deviation_pct": metrics.get("downside_deviation_pct"),
                "horizon_downside_deviation_pct": metrics.get(
                    "horizon_downside_deviation_pct"
                ),
                "daily_expected_shortfall_20_pct": metrics.get(
                    "daily_expected_shortfall_20_pct"
                ),
                "average_max_adverse_excursion_pct": metrics.get(
                    "average_max_adverse_excursion_pct"
                ),
                "return_risk_utility_pct": utility,
            },
        }

    @classmethod
    def _review_policy_state(
        cls,
        metrics: Dict[str, Any],
        *,
        min_mature_samples: int,
        min_accuracy_pct: float,
    ) -> tuple[str, str]:
        minimum = max(1, int(min_mature_samples))
        threshold = float(min_accuracy_pct)
        passed_count = int(metrics.get("passed_completed_count") or 0)
        blocked_count = int(metrics.get("blocked_completed_count") or 0)
        passed_precision = cls._finite_float(metrics.get("passed_precision_pct"))
        blocked_avoidance = cls._finite_float(metrics.get("blocked_avoidance_rate_pct"))
        passed_average = cls._finite_float(metrics.get("passed_average_return_pct"))
        return_spread = cls._finite_float(metrics.get("return_spread_pct"))
        passed_mature = passed_count >= minimum and passed_precision is not None
        blocked_mature = blocked_count >= minimum and blocked_avoidance is not None
        if not passed_mature and not blocked_mature:
            return "insufficient_evidence", "review_mature_sample_count_below_threshold"
        if passed_mature and passed_precision < threshold:
            return "blocked", "review_passed_precision_below_threshold"
        if blocked_mature and blocked_avoidance < threshold:
            return "blocked", "review_blocked_avoidance_below_threshold"
        if passed_mature and passed_average is not None and passed_average < 0:
            return "guarded", "review_passed_average_return_negative"
        if passed_mature and blocked_mature and return_spread is not None and return_spread < 0:
            return "guarded", "review_return_spread_negative"
        return "healthy", "review_quality_thresholds_met"

    def __init__(self, db_manager: Optional[DatabaseManager] = None) -> None:
        self.db = db_manager or DatabaseManager.get_instance()
        self.agent_repo = StockSelectionAgentRepository(self.db)
        self.backtest = BacktestService(self.db)

    def evaluate(
        self,
        *,
        strategy: Optional[str] = None,
        market: Optional[str] = None,
        created_from: Optional[datetime] = None,
        created_to: Optional[datetime] = None,
        eval_windows: Optional[Iterable[int]] = None,
        include_skipped: bool = True,
        max_decisions: int = 500,
        refresh_missing: bool = False,
        neutral_band_pct: float = 2.0,
    ) -> Dict[str, Any]:
        windows = self._normalize_windows(eval_windows)
        if created_from and created_to and created_from > created_to:
            raise ValueError("created_from cannot be after created_to")
        neutral_band = float(neutral_band_pct)
        if not math.isfinite(neutral_band) or neutral_band < 0 or neutral_band > 25:
            raise ValueError("neutral_band_pct must be between 0 and 25")

        source = self.agent_repo.list_forward_evaluation_decisions(
            strategy=strategy,
            market=market,
            created_from=created_from,
            created_to=created_to,
            include_skipped=include_skipped,
            limit=max_decisions,
        )
        items: List[Dict[str, Any]] = []
        max_window = max(windows)
        refresh_audit = self._empty_refresh_audit()
        if refresh_missing:
            refresh_audit = self._prefetch_missing_forward_bars(
                decisions=source["items"],
                min_window=min(windows),
                max_window=max_window,
            )

        for decision in source["items"]:
            created_at = decision.get("created_at") or decision.get("run_created_at")
            anchor_date = created_at.date() if isinstance(created_at, datetime) else None
            symbol = str(decision.get("symbol") or "").strip()
            start_price = self._positive_float(decision.get("price"))
            if start_price is None:
                raw_candidate = decision.get("raw_candidate")
                if isinstance(raw_candidate, dict):
                    start_price = self._positive_float(raw_candidate.get("price"))

            bars = []
            matched_code = None
            code_candidates = self.backtest._build_daily_code_candidates(symbol)
            if anchor_date is not None and code_candidates:
                bars, matched_code = self._load_forward_bars(
                    code_candidates=code_candidates,
                    anchor_date=anchor_date,
                    eval_window_days=max_window,
                )

            horizons: Dict[str, Dict[str, Any]] = {}
            for window in windows:
                if anchor_date is None:
                    evaluation = {
                        "eval_status": "unable",
                        "unable_reason": "missing_anchor_date",
                        "eval_window_days": window,
                    }
                elif start_price is None:
                    evaluation = {
                        "eval_status": "unable",
                        "unable_reason": "invalid_anchor_price",
                        "eval_window_days": window,
                    }
                else:
                    evaluation = BacktestEngine.evaluate_decision_signal(
                        direction_expected="up",
                        anchor_date=anchor_date,
                        start_price=start_price,
                        forward_bars=bars,
                        config=EvaluationConfig(
                            eval_window_days=window,
                            neutral_band_pct=neutral_band,
                            engine_version=self.ENGINE_VERSION,
                        ),
                    )
                    if evaluation.get("eval_status") == "completed":
                        max_high = self._finite_float(evaluation.get("max_high"))
                        min_low = self._finite_float(evaluation.get("min_low"))
                        evaluation["max_favorable_excursion_pct"] = (
                            round((max_high - start_price) / start_price * 100, 6)
                            if max_high is not None and start_price
                            else None
                        )
                        evaluation["max_adverse_excursion_pct"] = (
                            round((min_low - start_price) / start_price * 100, 6)
                            if min_low is not None and start_price
                            else None
                        )
                        evaluation["daily_returns_pct"] = self._daily_return_series(
                            start_price=start_price,
                            bars=bars[:window],
                        )
                horizons[str(window)] = evaluation

            order_result = (
                decision.get("order_result")
                if isinstance(decision.get("order_result"), dict)
                else {}
            )

            items.append(
                {
                    "decision_id": decision.get("id"),
                    "run_uid": decision.get("run_uid"),
                    "strategy": decision.get("strategy"),
                    "trigger_source": decision.get("trigger_source"),
                    "symbol": symbol,
                    "name": decision.get("name"),
                    "market": decision.get("market"),
                    "decision_status": decision.get("status"),
                    "decision_reason": decision.get("reason"),
                    "score": decision.get("score"),
                    "anchor_at": created_at,
                    "anchor_date": anchor_date,
                    "anchor_price": start_price,
                    "daily_code": matched_code,
                    "forward_bar_count": len(bars),
                    "reviews": self._decision_reviews(order_result),
                    "horizons": horizons,
                }
            )

        matrix = {
            str(window): self._summarize(items, window=window, neutral_band_pct=neutral_band)
            for window in windows
        }
        strategy_matrix: Dict[str, Dict[str, Any]] = {}
        for strategy_name in sorted({str(item.get("strategy") or "unknown") for item in items}):
            strategy_items = [item for item in items if str(item.get("strategy") or "unknown") == strategy_name]
            strategy_matrix[strategy_name] = {
                str(window): self._summarize(
                    strategy_items,
                    window=window,
                    neutral_band_pct=neutral_band,
                )
                for window in windows
            }
        review_quality_matrix = self._build_review_quality_matrix(items, windows=windows)
        review_policy_quality = self._build_effective_review_policy_quality(items, windows=windows)

        return {
            "generated_at": datetime.now(),
            "methodology": {
                "type": "point_in_time_candidate_forward_evaluation",
                "engine_version": self.ENGINE_VERSION,
                "anchor": "persisted_decision_price_and_date",
                "forward_bar_rule": "stock_daily.date > decision.created_at.date",
                "lookahead_protection": True,
                "reruns_historical_strategy": False,
                "includes_fees_or_slippage": False,
                "review_grouping": "source_model_prompt_evaluator_version",
                "passed_precision_rule": "completed_passed_reviews_with_hit_or_win_outcome",
                "blocked_avoidance_rule": "completed_blocked_reviews_with_miss_or_loss_outcome",
                "return_risk_objective_version": self.RETURN_RISK_OBJECTIVE_VERSION,
                "return_risk_objective_formula": (
                    "average_forward_return_pct"
                    " - 0.5 * horizon_downside_deviation_pct"
                    " - 0.25 * abs(average_max_adverse_excursion_pct)"
                ),
            },
            "filters": {
                "strategy": strategy,
                "market": market,
                "created_from": created_from,
                "created_to": created_to,
                "include_skipped": bool(include_skipped),
                "eval_windows": windows,
                "neutral_band_pct": neutral_band,
                "max_decisions": max(1, min(2000, int(max_decisions or 500))),
                "refresh_missing": bool(refresh_missing),
            },
            "total": source["total"],
            "scanned_count": len(items),
            "truncated": bool(source["truncated"]),
            **refresh_audit,
            "status_counts": dict(Counter(str(item.get("decision_status") or "unknown") for item in items)),
            "matrix": matrix,
            "strategy_matrix": strategy_matrix,
            "review_quality_matrix": review_quality_matrix,
            "review_policy_quality": review_policy_quality,
            "items": items,
        }

    @staticmethod
    def _empty_refresh_audit() -> Dict[str, Any]:
        return {
            "refresh_attempted_count": 0,
            "refresh_succeeded_count": 0,
            "refresh_failed_count": 0,
            "refresh_skipped_not_due_count": 0,
            "refresh_source_counts": {},
            "refresh_saved_row_count": 0,
            "refresh_resolved_anchor_count": 0,
            "refresh_unresolved_anchor_count": 0,
        }

    def _prefetch_missing_forward_bars(
        self,
        *,
        decisions: List[Dict[str, Any]],
        min_window: int,
        max_window: int,
    ) -> Dict[str, Any]:
        """Refresh each symbol once, using its earliest eligible decision anchor."""

        today = datetime.now().date()
        grouped: Dict[str, Dict[str, Any]] = {}
        for decision in decisions:
            created_at = decision.get("created_at") or decision.get("run_created_at")
            anchor_date = created_at.date() if isinstance(created_at, datetime) else None
            symbol = str(decision.get("symbol") or "").strip()
            code_candidates = self.backtest._build_daily_code_candidates(symbol)
            if anchor_date is None or not code_candidates:
                continue
            key = code_candidates[0]
            current = grouped.setdefault(
                key,
                {
                    "anchor_dates": set(),
                    "code_candidates": code_candidates,
                },
            )
            current["anchor_dates"].add(anchor_date)

        audit = self._empty_refresh_audit()
        source_counts: Counter[str] = Counter()
        for item in grouped.values():
            eligible_anchors = sorted(
                anchor_date
                for anchor_date in item["anchor_dates"]
                if (today - anchor_date).days >= min_window
            )
            if not eligible_anchors:
                audit["refresh_skipped_not_due_count"] += 1
                continue
            code_candidates = item["code_candidates"]
            missing_anchors: List[tuple[Any, Optional[str]]] = []
            for anchor_date in eligible_anchors:
                bars, matched_code = self._load_forward_bars(
                    code_candidates=code_candidates,
                    anchor_date=anchor_date,
                    eval_window_days=max_window,
                )
                if len(bars) < max_window:
                    missing_anchors.append((anchor_date, matched_code))
            if not missing_anchors:
                continue
            earliest_anchor = missing_anchors[0][0]
            latest_anchor = missing_anchors[-1][0]
            refresh_window = max_window + max(
                0,
                (latest_anchor - earliest_anchor).days,
            )
            audit["refresh_attempted_count"] += 1
            refresh_result = self.backtest._try_fill_daily_data(
                code=missing_anchors[0][1] or code_candidates[0],
                analysis_date=earliest_anchor,
                eval_window_days=refresh_window,
            )
            if isinstance(refresh_result, dict) and refresh_result.get("succeeded"):
                audit["refresh_succeeded_count"] += 1
                source_counts[str(refresh_result.get("source") or "unknown")] += 1
                audit["refresh_saved_row_count"] += int(
                    refresh_result.get("saved_row_count") or 0
                )
            else:
                audit["refresh_failed_count"] += 1

            for anchor_date, _matched_code in missing_anchors:
                refreshed_bars, _refreshed_code = self._load_forward_bars(
                    code_candidates=code_candidates,
                    anchor_date=anchor_date,
                    eval_window_days=max_window,
                )
                key = (
                    "refresh_resolved_anchor_count"
                    if len(refreshed_bars) >= max_window
                    else "refresh_unresolved_anchor_count"
                )
                audit[key] += 1
        audit["refresh_source_counts"] = dict(sorted(source_counts.items()))
        return audit

    @classmethod
    def _decision_reviews(cls, order_result: Dict[str, Any]) -> List[Dict[str, Any]]:
        reviews: List[Dict[str, Any]] = []
        definitions = (
            ("rule_agent", order_result.get("agent_review")),
            ("llm", order_result.get("llm_review")),
        )
        for source, raw in definitions:
            if not isinstance(raw, dict):
                continue
            status = str(raw.get("status") or "unknown").strip().lower() or "unknown"
            reviewer = str(raw.get("reviewer") or "unknown").strip() or "unknown"
            model = str(raw.get("model") or "").strip() or None
            prompt_version = str(raw.get("prompt_version") or "").strip() or None
            evaluator_version = str(raw.get("evaluator_version") or "").strip() or None
            identity = model or reviewer
            version_parts = [value for value in (prompt_version, evaluator_version) if value]
            schema_version = str(raw.get("schema_version") or "1").strip() or "1"
            version = "/".join(version_parts) or f"schema_v{schema_version}"
            reviews.append(
                {
                    "key": f"{source}:{identity}:{version}",
                    "source": source,
                    "reviewer": reviewer,
                    "model": model,
                    "status": status,
                    "prompt_version": prompt_version,
                    "evaluator_version": evaluator_version,
                    "version": version,
                }
            )
        return reviews

    @classmethod
    def _build_review_quality_matrix(
        cls,
        items: List[Dict[str, Any]],
        *,
        windows: List[int],
    ) -> List[Dict[str, Any]]:
        groups: Dict[str, Dict[str, Any]] = {}
        for item in items:
            for review in item.get("reviews") or []:
                key = str(review.get("key") or "").strip()
                if not key:
                    continue
                group = groups.setdefault(
                    key,
                    {
                        "key": key,
                        "source": review.get("source"),
                        "reviewer": review.get("reviewer"),
                        "model": review.get("model"),
                        "prompt_version": review.get("prompt_version"),
                        "evaluator_version": review.get("evaluator_version"),
                        "version": review.get("version"),
                        "status_counts": Counter(),
                        "samples": [],
                    },
                )
                group["status_counts"][str(review.get("status") or "unknown")] += 1
                group["samples"].append((item, review))

        result: List[Dict[str, Any]] = []
        for key in sorted(groups):
            group = groups[key]
            samples = group.pop("samples")
            status_counts = group.pop("status_counts")
            horizons = {
                str(window): cls._summarize_review_samples(samples, window=window)
                for window in windows
            }
            result.append(
                {
                    **group,
                    "sample_count": len(samples),
                    "status_counts": dict(sorted(status_counts.items())),
                    "horizons": horizons,
                }
            )
        return result

    @classmethod
    def _build_effective_review_policy_quality(
        cls,
        items: List[Dict[str, Any]],
        *,
        windows: List[int],
    ) -> Dict[str, Any]:
        samples: List[tuple[Dict[str, Any], Dict[str, Any]]] = []
        status_counts: Counter[str] = Counter()
        source_counts: Counter[str] = Counter()
        model_counts: Counter[str] = Counter()
        version_counts: Counter[str] = Counter()
        for item in items:
            reviews = [review for review in item.get("reviews") or [] if isinstance(review, dict)]
            llm_review = next((review for review in reviews if review.get("source") == "llm"), None)
            rule_review = next(
                (review for review in reviews if review.get("source") == "rule_agent"),
                None,
            )
            effective = llm_review or rule_review
            if not isinstance(effective, dict):
                continue
            samples.append((item, effective))
            status_counts[str(effective.get("status") or "unknown")] += 1
            source_counts[str(effective.get("source") or "unknown")] += 1
            model = str(effective.get("model") or "").strip()
            if model:
                model_counts[model] += 1
            version_counts[str(effective.get("version") or "unknown")] += 1
        return {
            "schema_version": 1,
            "policy": "llm_review_then_rule_agent",
            "sample_count": len(samples),
            "status_counts": dict(sorted(status_counts.items())),
            "source_counts": dict(sorted(source_counts.items())),
            "model_counts": dict(sorted(model_counts.items())),
            "version_counts": dict(sorted(version_counts.items())),
            "horizons": {
                str(window): cls._summarize_review_samples(samples, window=window)
                for window in windows
            },
        }

    @classmethod
    def _summarize_review_samples(
        cls,
        samples: List[tuple[Dict[str, Any], Dict[str, Any]]],
        *,
        window: int,
    ) -> Dict[str, Any]:
        completed: List[tuple[Dict[str, Any], Dict[str, Any]]] = []
        unable_reasons: Counter[str] = Counter()
        for item, review in samples:
            evaluation = dict(item.get("horizons", {}).get(str(window)) or {})
            if evaluation.get("eval_status") == "completed":
                completed.append((evaluation, review))
            else:
                unable_reasons[str(evaluation.get("unable_reason") or "unknown")] += 1

        passed = [sample for sample in completed if sample[1].get("status") == "passed"]
        blocked = [sample for sample in completed if sample[1].get("status") == "blocked"]
        passed_returns = [
            value
            for value in (cls._finite_float(sample[0].get("stock_return_pct")) for sample in passed)
            if value is not None
        ]
        blocked_returns = [
            value
            for value in (cls._finite_float(sample[0].get("stock_return_pct")) for sample in blocked)
            if value is not None
        ]
        passed_average = cls._average(passed_returns)
        blocked_average = cls._average(blocked_returns)
        return {
            "eval_window_days": window,
            "sample_count": len(samples),
            "completed_count": len(completed),
            "coverage_pct": cls._pct(len(completed), len(samples)),
            "passed_completed_count": len(passed),
            "blocked_completed_count": len(blocked),
            "passed_precision_pct": cls._pct(
                sum(1 for evaluation, _review in passed if cls._is_winning_outcome(evaluation)),
                len(passed),
            ),
            "blocked_avoidance_rate_pct": cls._pct(
                sum(1 for evaluation, _review in blocked if cls._is_losing_outcome(evaluation)),
                len(blocked),
            ),
            "passed_average_return_pct": passed_average,
            "blocked_average_return_pct": blocked_average,
            "return_spread_pct": (
                round(passed_average - blocked_average, 6)
                if passed_average is not None and blocked_average is not None
                else None
            ),
            "unable_reason_counts": dict(unable_reasons),
        }

    @staticmethod
    def _is_winning_outcome(evaluation: Dict[str, Any]) -> bool:
        return str(evaluation.get("outcome") or "").strip().lower() in {"hit", "win"}

    @staticmethod
    def _is_losing_outcome(evaluation: Dict[str, Any]) -> bool:
        return str(evaluation.get("outcome") or "").strip().lower() in {"miss", "loss"}

    def _load_forward_bars(
        self,
        *,
        code_candidates: List[str],
        anchor_date: Any,
        eval_window_days: int,
    ) -> tuple[List[Any], Optional[str]]:
        best_rows: List[Any] = []
        best_code: Optional[str] = None
        for code in code_candidates:
            rows = self.backtest.stock_repo.get_forward_bars(
                code=code,
                analysis_date=anchor_date,
                eval_window_days=eval_window_days,
            )
            if len(rows) > len(best_rows):
                best_rows = rows
                best_code = code
            if len(rows) >= eval_window_days:
                return rows, code
        return best_rows, best_code

    @classmethod
    def _normalize_windows(cls, values: Optional[Iterable[int]]) -> List[int]:
        raw = list(values) if values is not None else list(cls.DEFAULT_WINDOWS)
        try:
            windows = sorted({int(value) for value in raw})
        except (TypeError, ValueError) as exc:
            raise ValueError("eval_windows must contain positive integers") from exc
        if not windows or len(windows) > 6 or any(value < 1 or value > 60 for value in windows):
            raise ValueError("eval_windows must contain 1 to 6 values between 1 and 60")
        return windows

    @classmethod
    def _summarize(
        cls,
        items: List[Dict[str, Any]],
        *,
        window: int,
        neutral_band_pct: float,
    ) -> Dict[str, Any]:
        evaluations = [item["horizons"].get(str(window), {}) for item in items]
        completed = [item for item in evaluations if item.get("eval_status") == "completed"]
        returns = [
            value
            for value in (cls._finite_float(item.get("stock_return_pct")) for item in completed)
            if value is not None
        ]
        favorable = [
            value
            for value in (cls._finite_float(item.get("max_favorable_excursion_pct")) for item in completed)
            if value is not None
        ]
        adverse = [
            value
            for value in (cls._finite_float(item.get("max_adverse_excursion_pct")) for item in completed)
            if value is not None
        ]
        daily_returns = [
            value
            for item in completed
            for value in (
                cls._finite_float(raw_value)
                for raw_value in item.get("daily_returns_pct") or []
            )
            if value is not None
        ]
        expected_daily_observations = len(completed) * window
        daily_return_coverage_pct = cls._pct(
            len(daily_returns),
            expected_daily_observations,
        )
        daily_returns_complete = bool(
            completed and len(daily_returns) == expected_daily_observations
        )
        average_return = cls._average(returns)
        average_adverse = cls._average(adverse)
        downside_deviation = cls._downside_deviation(daily_returns)
        horizon_downside = (
            round(downside_deviation * math.sqrt(window), 6)
            if downside_deviation is not None
            else None
        )
        return_risk_utility = (
            round(
                average_return
                - cls.RETURN_RISK_DOWNSIDE_WEIGHT * horizon_downside
                - cls.RETURN_RISK_ADVERSE_EXCURSION_WEIGHT * abs(average_adverse),
                6,
            )
            if daily_returns_complete
            and average_return is not None
            and horizon_downside is not None
            and average_adverse is not None
            else None
        )
        outcome_counts = Counter(str(item.get("outcome") or "unknown") for item in completed)
        win_count = outcome_counts.get("hit", 0) + outcome_counts.get("win", 0)
        loss_count = outcome_counts.get("miss", 0) + outcome_counts.get("loss", 0)
        unable_reasons = Counter(
            str(item.get("unable_reason") or "unknown")
            for item in evaluations
            if item.get("eval_status") != "completed"
        )
        return {
            "eval_window_days": window,
            "sample_count": len(evaluations),
            "completed_count": len(completed),
            "insufficient_count": len(evaluations) - len(completed),
            "coverage_pct": cls._pct(len(completed), len(evaluations)),
            "win_count": win_count,
            "loss_count": loss_count,
            "neutral_count": outcome_counts.get("neutral", 0),
            "win_rate_pct": cls._pct(win_count, len(completed)),
            "direction_accuracy_pct": cls._pct(
                sum(1 for item in completed if item.get("direction_correct") is True),
                len(completed),
            ),
            "average_return_pct": average_return,
            "median_return_pct": round(statistics.median(returns), 6) if returns else None,
            "average_max_favorable_excursion_pct": cls._average(favorable),
            "average_max_adverse_excursion_pct": average_adverse,
            "daily_observation_count": len(daily_returns),
            "expected_daily_observation_count": expected_daily_observations,
            "daily_return_coverage_pct": daily_return_coverage_pct,
            "average_daily_return_pct": cls._average(daily_returns),
            "daily_return_volatility_pct": (
                round(statistics.pstdev(daily_returns), 6) if daily_returns else None
            ),
            "downside_deviation_pct": downside_deviation,
            "horizon_downside_deviation_pct": horizon_downside,
            "daily_expected_shortfall_20_pct": cls._expected_shortfall(
                daily_returns,
                fraction=0.2,
            ),
            "return_risk_utility_pct": return_risk_utility,
            "return_risk_objective_version": cls.RETURN_RISK_OBJECTIVE_VERSION,
            "neutral_band_pct": neutral_band_pct,
            "unable_reason_counts": dict(unable_reasons),
        }

    @classmethod
    def _daily_return_series(cls, *, start_price: float, bars: List[Any]) -> List[float]:
        previous = cls._positive_float(start_price)
        if previous is None:
            return []
        result: List[float] = []
        for bar in bars:
            close = cls._positive_float(getattr(bar, "close", None))
            if close is None:
                return []
            result.append(round((close - previous) / previous * 100.0, 6))
            previous = close
        return result

    @staticmethod
    def _downside_deviation(values: List[float]) -> Optional[float]:
        if not values:
            return None
        return round(math.sqrt(sum(min(0.0, value) ** 2 for value in values) / len(values)), 6)

    @staticmethod
    def _expected_shortfall(values: List[float], *, fraction: float) -> Optional[float]:
        if not values:
            return None
        count = max(1, math.ceil(len(values) * fraction))
        return round(sum(sorted(values)[:count]) / count, 6)

    @staticmethod
    def _pct(numerator: int, denominator: int) -> Optional[float]:
        return round(numerator / denominator * 100, 2) if denominator else None

    @staticmethod
    def _average(values: List[float]) -> Optional[float]:
        return round(sum(values) / len(values), 6) if values else None

    @staticmethod
    def _finite_float(value: Any) -> Optional[float]:
        try:
            result = float(value)
        except (TypeError, ValueError):
            return None
        return result if math.isfinite(result) else None

    @classmethod
    def _positive_float(cls, value: Any) -> Optional[float]:
        result = cls._finite_float(value)
        return result if result is not None and result > 0 else None
