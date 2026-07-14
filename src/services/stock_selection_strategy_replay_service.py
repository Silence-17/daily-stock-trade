# -*- coding: utf-8 -*-
"""Point-in-time AlphaSift strategy compatibility and replay service."""

from __future__ import annotations

from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

import pandas as pd

from src.repositories.stock_selection_factor_snapshot_repo import (
    StockSelectionFactorSnapshotRepository,
)
from src.storage import DatabaseManager


class StockSelectionStrategyReplayService:
    """Replay AlphaSift hard filters and scoring against dated factor rows."""

    HARD_FILTER_FIELDS = {
        "exclude_st": "name",
        "price_min": "price",
        "price_max": "price",
        "amount_min": "amount",
        "market_cap_min": "total_mv",
        "market_cap_max": "total_mv",
        "pe_ttm_min": "pe_ratio",
        "pe_ttm_max": "pe_ratio",
        "pb_min": "pb_ratio",
        "pb_max": "pb_ratio",
        "volume_ratio_min": "volume_ratio",
        "turnover_rate_min": "turnover_rate",
        "change_pct_min": "change_pct",
        "change_pct_max": "change_pct",
        "change_60d_min": "change_60d",
        "change_60d_max": "change_60d",
        "require_ma_bullish": "ma_bullish",
        "require_price_above_ma20": "price_above_ma20",
        "signal_score_min": "signal_score",
        "macd_status_whitelist": "macd_status",
        "rsi_status_whitelist": "rsi_status",
        "breakout_20d_pct_min": "breakout_20d_pct",
        "breakout_20d_pct_max": "breakout_20d_pct",
        "range_20d_pct_max": "range_20d_pct",
        "volume_ratio_20d_min": "volume_ratio_20d",
        "volume_ratio_20d_max": "volume_ratio_20d",
        "body_pct_min": "body_pct",
        "body_pct_max": "body_pct",
        "pullback_to_ma20_pct_min": "pullback_to_ma20_pct",
        "pullback_to_ma20_pct_max": "pullback_to_ma20_pct",
        "consolidation_days_20d_min": "consolidation_days_20d",
        "consolidation_days_20d_max": "consolidation_days_20d",
        "volatility_20d_pct_min": "volatility_20d_pct",
        "volatility_20d_pct_max": "volatility_20d_pct",
        "max_drawdown_20d_pct_min": "max_drawdown_20d_pct",
        "max_drawdown_20d_pct_max": "max_drawdown_20d_pct",
        "atr_20_pct_min": "atr_20_pct",
        "atr_20_pct_max": "atr_20_pct",
    }
    SCORE_FACTOR_FIELDS = {
        "value": {"pe_ratio", "pb_ratio"},
        "liquidity": {"amount"},
        "momentum": {"change_pct", "change_60d", "signal_score"},
        "reversal": {"change_pct", "rsi14"},
        "activity": {"volume_ratio", "turnover_rate"},
        "stability": {
            "change_pct", "volume_ratio", "turnover_rate", "volatility_20d_pct",
            "max_drawdown_20d_pct", "atr_20_pct",
        },
        "size": {"total_mv"},
        "theme_heat": {"theme_heat_score"},
        "topic_alignment": {"topic_alignment_score"},
    }

    def __init__(
        self,
        db_manager: Optional[DatabaseManager] = None,
        repository: Optional[StockSelectionFactorSnapshotRepository] = None,
    ) -> None:
        self.repository = repository or StockSelectionFactorSnapshotRepository(db_manager)

    def compatibility(self, *, strategy: str, market: str, snapshot_date: date) -> Dict[str, Any]:
        strategy_obj = self._load_strategy(strategy)
        rows = self.repository.list_for_date(market=market, snapshot_date=snapshot_date)
        hard_fields = self._hard_filter_fields(strategy_obj.screening.hard_filters)
        score_fields = self._score_fields(strategy_obj.screening.factor_weights)
        return self._compatibility_payload(
            strategy=strategy,
            market=market,
            snapshot_date=snapshot_date,
            rows=rows,
            hard_fields=hard_fields,
            score_fields=score_fields,
        )

    def replay(
        self,
        *,
        strategy: str,
        market: str,
        snapshot_date: date,
        max_results: Optional[int] = None,
        min_hard_coverage: float = 0.95,
        min_score_coverage: float = 0.80,
    ) -> Dict[str, Any]:
        if not 0 < min_hard_coverage <= 1 or not 0 < min_score_coverage <= 1:
            raise ValueError("coverage thresholds must be in (0, 1]")
        strategy_obj = self._load_strategy(strategy)
        rows = self.repository.list_for_date(market=market, snapshot_date=snapshot_date)
        hard_fields = self._hard_filter_fields(strategy_obj.screening.hard_filters)
        score_fields = self._score_fields(strategy_obj.screening.factor_weights)
        compatibility = self._compatibility_payload(
            strategy=strategy,
            market=market,
            snapshot_date=snapshot_date,
            rows=rows,
            hard_fields=hard_fields,
            score_fields=score_fields,
        )
        if not rows:
            raise ValueError("no point-in-time factor snapshot exists for the requested date")
        if compatibility["hard_coverage_ratio"] < min_hard_coverage:
            raise ValueError("hard-filter field coverage is below the replay threshold")
        if compatibility["score_coverage_ratio"] < min_score_coverage:
            raise ValueError("score field coverage is below the replay threshold")

        eligible = [row for row in rows if not self._missing(row, hard_fields | score_fields)]
        frame = pd.DataFrame(eligible)
        from alphasift.filter import apply_hard_filters
        from alphasift.scorer import compute_screen_scores, factor_score_columns

        filtered = apply_hard_filters(frame, strategy_obj.screening.hard_filters)
        scored = compute_screen_scores(filtered, strategy_obj.screening)
        scored = scored.sort_values(["screen_score", "symbol"], ascending=[False, True])
        limit = int(max_results or strategy_obj.screening.max_output or 5)
        limit = max(1, min(100, limit))
        factor_columns = set(factor_score_columns().values())
        output_columns = [
            column for column in (
                "symbol", "name", "industry", "price", "change_pct", "screen_score"
            ) if column in scored.columns
        ]
        output_columns.extend(sorted(factor_columns.intersection(scored.columns)))
        selected = scored.head(limit)[output_columns]
        candidates = selected.where(pd.notna(selected), None).to_dict("records")
        return {
            "strategy": strategy,
            "market": str(market).lower(),
            "snapshot_date": snapshot_date.isoformat(),
            "universe_count": len(rows),
            "complete_row_count": len(eligible),
            "filtered_count": len(filtered),
            "candidate_count": len(candidates),
            "candidates": candidates,
            "compatibility": compatibility,
            "methodology": {
                "point_in_time": True,
                "lookahead_protection": True,
                "uses_current_snapshot_fallback": False,
                "llm_ranking_enabled": False,
                "ranking": "alphasift_hard_filters_and_screen_score",
            },
        }

    @classmethod
    def _hard_filter_fields(cls, filters: Any) -> Set[str]:
        required = set()
        for config_field, snapshot_field in cls.HARD_FILTER_FIELDS.items():
            value = getattr(filters, config_field, None)
            if value is not None and value is not False and value != []:
                required.add(snapshot_field)
        return required

    @classmethod
    def _score_fields(cls, factor_weights: Dict[str, float]) -> Set[str]:
        required = set()
        for factor, weight in (factor_weights or {}).items():
            if float(weight or 0) > 0:
                required.update(cls.SCORE_FACTOR_FIELDS.get(factor, set()))
        return required

    @classmethod
    def _compatibility_payload(
        cls,
        *,
        strategy: str,
        market: str,
        snapshot_date: date,
        rows: List[Dict[str, Any]],
        hard_fields: Set[str],
        score_fields: Set[str],
    ) -> Dict[str, Any]:
        hard_missing = Counter()
        score_missing = Counter()
        hard_complete = 0
        score_complete = 0
        for row in rows:
            missing_hard = cls._missing(row, hard_fields)
            missing_score = cls._missing(row, score_fields)
            hard_missing.update(missing_hard)
            score_missing.update(missing_score)
            hard_complete += not missing_hard
            score_complete += not missing_score
        total = len(rows)
        return {
            "strategy": strategy,
            "market": str(market).lower(),
            "snapshot_date": snapshot_date.isoformat(),
            "universe_count": total,
            "required_hard_fields": sorted(hard_fields),
            "required_score_fields": sorted(score_fields),
            "hard_complete_rows": hard_complete,
            "score_complete_rows": score_complete,
            "hard_coverage_ratio": round(hard_complete / total, 6) if total else 0.0,
            "score_coverage_ratio": round(score_complete / total, 6) if total else 0.0,
            "hard_missing_counts": dict(sorted(hard_missing.items())),
            "score_missing_counts": dict(sorted(score_missing.items())),
        }

    @staticmethod
    def _missing(row: Dict[str, Any], fields: Iterable[str]) -> Set[str]:
        return {field for field in fields if row.get(field) is None or row.get(field) == ""}

    @staticmethod
    def _load_strategy(name: str) -> Any:
        try:
            import alphasift
            from alphasift.strategy import load_all_strategies
        except ImportError as exc:
            raise RuntimeError("AlphaSift is unavailable") from exc
        strategy_name = str(name or "").strip()
        strategies = load_all_strategies(Path(alphasift.__file__).resolve().parent / "strategies")
        if strategy_name not in strategies:
            raise ValueError(f"unknown AlphaSift strategy: {strategy_name}")
        return strategies[strategy_name]
