# -*- coding: utf-8 -*-

from datetime import datetime, timezone

from src.services.hotspot_0945_paper_service import (
    STRATEGY_SPECS,
    evaluate_open_candidates,
    normalize_quote,
)


NOW = datetime(2026, 8, 10, 9, 45, 5, tzinfo=timezone.utc)


def _quote(code: str, *, price: float, open_price: float, low: float, high: float, vwap: float):
    return {
        "available": True,
        "code": code,
        "name": code,
        "price": price,
        "open_price": open_price,
        "low": low,
        "high": high,
        "vwap": vwap,
        "early_return_pct": (price / open_price - 1) * 100,
        "rebound_pct": (price / low - 1) * 100,
        "resilience_pct": (low / open_price - 1) * 100,
        "range_position": (price - low) / (high - low),
    }


def test_normalize_quote_requires_fresh_provider_timestamp() -> None:
    payload = normalize_quote(
        {
            "code": "600001",
            "price": 10.3,
            "open_price": 10.0,
            "high": 10.4,
            "low": 9.9,
            "volume": 1_000_000,
            "amount": 10_100_000,
            "provider_timestamp": NOW.isoformat(),
        },
        now=NOW,
    )
    assert payload["available"] is True
    assert round(payload["early_return_pct"], 3) == 3.0
    assert payload["vwap"] == 10.1

    stale = normalize_quote(
        {
            "price": 10.3,
            "open_price": 10.0,
            "high": 10.4,
            "low": 9.9,
            "provider_timestamp": "2026-08-10T09:40:00+00:00",
        },
        now=NOW,
    )
    assert stale == {
        "available": False,
        "reason": "quote_stale_or_future",
        "age_seconds": 305.0,
    }


def test_oversold_strategy_selects_strong_rebound_from_frozen_pool() -> None:
    spec = STRATEGY_SPECS[0]
    candidates = [
        {"code": "600001", "score": 82},
        {"code": "600002", "score": 90},
        {"code": "600003", "score": 70},
    ]
    quotes = {
        "600001": _quote("600001", price=10.3, open_price=10.0, low=9.9, high=10.35, vwap=10.1),
        "600002": _quote("600002", price=10.1, open_price=10.0, low=9.98, high=10.3, vwap=10.08),
        "600003": _quote("600003", price=10.2, open_price=10.0, low=10.05, high=10.25, vwap=10.15),
    }
    ranked = evaluate_open_candidates(spec, candidates, quotes)
    assert ranked[0]["code"] == "600001"
    assert ranked[0]["eligible"] is True
    assert ranked[0]["rank"] == 1
    assert ranked[1]["eligible"] is True
    assert ranked[2]["eligible"] is False
    assert "outside_combined_top_two" in ranked[2]["blockers"]


def test_trend_strategy_fails_closed_without_vwap_reclaim() -> None:
    spec = STRATEGY_SPECS[1]
    ranked = evaluate_open_candidates(
        spec,
        [{"code": "600001", "score": 95}],
        {"600001": _quote("600001", price=10.3, open_price=10.0, low=9.95, high=10.5, vwap=10.4)},
    )
    assert ranked[0]["eligible"] is False
    assert "vwap_reclaim_unconfirmed" in ranked[0]["blockers"]
