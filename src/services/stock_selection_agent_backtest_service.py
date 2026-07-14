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
            refresh_missing=False,
        )
        metrics = dict(result["matrix"].get(str(horizon)) or {})
        completed_count = int(metrics.get("completed_count") or 0)
        win_rate = self._finite_float(metrics.get("win_rate_pct"))
        average_return = self._finite_float(metrics.get("average_return_pct"))
        if completed_count < minimum:
            state = "insufficient_evidence"
            reason = "mature_sample_count_below_threshold"
        elif win_rate is None:
            state = "unavailable"
            reason = "win_rate_unavailable"
        elif win_rate < threshold:
            state = "blocked"
            reason = "forward_win_rate_below_threshold"
        elif average_return is not None and average_return < 0:
            state = "guarded"
            reason = "average_forward_return_negative"
        else:
            state = "healthy"
            reason = "forward_quality_thresholds_met"
        previous = str(previous_state or "").strip().lower() or None
        return {
            "schema_version": 1,
            "generated_at": result.get("generated_at"),
            "state": state,
            "reason": reason,
            "previous_state": previous,
            "transition": f"{previous}->{state}" if previous and previous != state else None,
            "changed": bool(previous and previous != state),
            "strategy": strategy,
            "market": market,
            "horizon_days": horizon,
            "min_mature_samples": minimum,
            "min_win_rate_pct": threshold,
            "max_decisions": max(1, min(2000, int(max_decisions))),
            "sample_count": int(metrics.get("sample_count") or 0),
            "mature_sample_count": completed_count,
            "coverage_pct": metrics.get("coverage_pct"),
            "win_rate_pct": metrics.get("win_rate_pct"),
            "average_return_pct": metrics.get("average_return_pct"),
            "median_return_pct": metrics.get("median_return_pct"),
            "average_max_adverse_excursion_pct": metrics.get(
                "average_max_adverse_excursion_pct"
            ),
            "unable_reason_counts": dict(metrics.get("unable_reason_counts") or {}),
            "lookahead_protection": True,
            "source": "persisted_agent_decisions_and_stock_daily",
            "truncated": bool(result.get("truncated")),
        }

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
        refresh_attempted = 0

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
                if refresh_missing and len(bars) < max_window:
                    refresh_attempted += 1
                    self.backtest._try_fill_daily_data(
                        code=matched_code or code_candidates[0],
                        analysis_date=anchor_date,
                        eval_window_days=max_window,
                    )
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
                horizons[str(window)] = evaluation

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
            "refresh_attempted_count": refresh_attempted,
            "status_counts": dict(Counter(str(item.get("decision_status") or "unknown") for item in items)),
            "matrix": matrix,
            "strategy_matrix": strategy_matrix,
            "items": items,
        }

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
            "average_return_pct": cls._average(returns),
            "median_return_pct": round(statistics.median(returns), 6) if returns else None,
            "average_max_favorable_excursion_pct": cls._average(favorable),
            "average_max_adverse_excursion_pct": cls._average(adverse),
            "neutral_band_pct": neutral_band_pct,
            "unable_reason_counts": dict(unable_reasons),
        }

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
