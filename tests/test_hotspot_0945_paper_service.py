# -*- coding: utf-8 -*-

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from src.services.alphasift_service import get_dsa_realtime_quote_with_provider_timestamp
from src.services.hotspot_0945_paper_service import (
    Hotspot0945PaperService,
    STRATEGY_SPECS,
    evaluate_open_candidates,
    normalize_auction_quote,
    normalize_quote,
)


NOW = datetime(2026, 8, 10, 9, 45, 5, tzinfo=timezone.utc)


def test_factor_overrides_persist_and_apply_to_runtime_specs() -> None:
    with TemporaryDirectory() as temp_dir:
        service = Hotspot0945PaperService(
            portfolio=MagicMock(),
            alphasift=MagicMock(),
            state_dir=Path(temp_dir),
            now_fn=lambda: NOW,
        )
        strategy_id = STRATEGY_SPECS[0].strategy_id

        updated = service.update_factor_overrides(
            strategy_id,
            {"position_pct": 0.25, "requires_vwap_reclaim": True},
        )
        runtime = next(
            item for item in service.strategy_specs() if item.strategy_id == strategy_id
        )

        assert service.factor_config_path.exists()
        assert runtime.position_pct == 0.25
        assert runtime.requires_vwap_reclaim is True
        assert {item["key"] for item in updated["items"]} >= {
            "position_pct",
            "requires_vwap_reclaim",
            "early_return_weight",
            "trailing_stop_pct",
        }
        assert next(
            item for item in updated["items"] if item["key"] == "position_pct"
        )["overridden"] is True


def test_factor_overrides_reject_an_invalid_zero_weight_profile() -> None:
    with TemporaryDirectory() as temp_dir:
        service = Hotspot0945PaperService(
            portfolio=MagicMock(),
            alphasift=MagicMock(),
            state_dir=Path(temp_dir),
            now_fn=lambda: NOW,
        )

        try:
            service.update_factor_overrides(
                STRATEGY_SPECS[0].strategy_id,
                {
                    "early_return_weight": 0,
                    "rebound_weight": 0,
                    "upstream_weight": 0,
                    "resilience_weight": 0,
                    "vwap_weight": 0,
                },
            )
        except ValueError as exc:
            assert str(exc) == "hotspot_factor_weight_total_invalid"
        else:
            raise AssertionError("zero-weight factor profile must be rejected")


def _quote(
    code: str,
    *,
    price: float,
    open_price: float,
    low: float,
    high: float,
    vwap: float,
    pre_close: float = 10.0,
    limit_up_price: float = 11.0,
):
    return {
        "available": True,
        "code": code,
        "name": code,
        "price": price,
        "open_price": open_price,
        "low": low,
        "high": high,
        "vwap": vwap,
        "pre_close": pre_close,
        "limit_up_price": limit_up_price,
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
    assert stale["available"] is False
    assert stale["reason"] == "quote_stale_or_future"
    assert stale["age_seconds"] == 305.0


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


def test_strict_dsa_quote_helper_uses_timestamp_required_manager_route() -> None:
    manager = MagicMock()
    quote = MagicMock()
    quote.to_dict.return_value = {
        "code": "600001",
        "price": 10.3,
        "provider_timestamp": NOW.isoformat(),
        "source": "tencent",
    }
    manager.get_realtime_quote_with_provider_timestamp.return_value = quote

    with patch("src.services.alphasift_service._get_dsa_fetcher_manager", return_value=manager):
        payload = get_dsa_realtime_quote_with_provider_timestamp("600001")

    manager.get_realtime_quote_with_provider_timestamp.assert_called_once_with("600001")
    manager.get_realtime_quote.assert_not_called()
    assert payload["provider_timestamp"] == NOW.isoformat()


def test_campaign_fetch_quotes_uses_strict_getter_and_keeps_rejection_reason() -> None:
    calls = []

    def strict_getter(code: str):
        calls.append(code)
        if code == "600001":
            return {
                "code": code,
                "price": 10.3,
                "open_price": 10.0,
                "high": 10.4,
                "low": 9.9,
                "provider_timestamp": NOW.isoformat(),
                "source": "tencent",
            }
        return {}

    service = Hotspot0945PaperService(
        portfolio=MagicMock(),
        alphasift=MagicMock(),
        quote_getter=strict_getter,
        now_fn=lambda: NOW,
    )
    quotes = service._fetch_quotes(["600001", "600002"], now=NOW)

    assert sorted(calls) == ["600001", "600002"]
    assert quotes["600001"]["available"] is True
    assert quotes["600001"]["provider"] == "tencent"
    assert quotes["600002"] == {
        "available": False,
        "reason": "timestamped_quote_unavailable",
        "code": "600002",
    }


def test_campaign_validates_quote_against_request_completion_time() -> None:
    request_started = datetime(2026, 8, 10, 1, 45, 0, tzinfo=timezone.utc)
    provider_time = datetime(2026, 8, 10, 1, 45, 2, tzinfo=timezone.utc)
    request_completed = datetime(2026, 8, 10, 1, 45, 3, tzinfo=timezone.utc)
    service = Hotspot0945PaperService(
        portfolio=MagicMock(),
        alphasift=MagicMock(),
        quote_getter=lambda code: {
            "code": code,
            "price": 10.3,
            "open_price": 10.0,
            "high": 10.4,
            "low": 9.9,
            "provider_timestamp": provider_time.isoformat(),
            "source": "tencent",
        },
        now_fn=lambda: request_completed,
    )

    quote = service._fetch_quotes(["600001"], now=request_started)["600001"]

    assert quote["available"] is True
    assert quote["age_seconds"] == 1.0


def test_empty_candidate_pools_do_not_complete_open_observation() -> None:
    local_signal_time = datetime(2026, 8, 10, 9, 45, 5, tzinfo=ZoneInfo("Asia/Shanghai"))
    alphasift = MagicMock()
    alphasift.screen.return_value = {"candidates": []}
    state = {
        "schema_version": 1,
        "strategies": {
            spec.strategy_id: {
                "account_id": index,
                "status": "ready",
                "started_on": None,
                "completed_sessions": [],
                "observations": {},
                "position_meta": {},
            }
            for index, spec in enumerate(STRATEGY_SPECS, start=1)
        },
    }

    with TemporaryDirectory() as temp_dir:
        service = Hotspot0945PaperService(
            portfolio=MagicMock(),
            alphasift=alphasift,
            state_dir=Path(temp_dir),
            now_fn=lambda: local_signal_time,
        )
        service._require_cn_trading_session = MagicMock()
        service._wait_until = MagicMock()
        service._load_state = MagicMock(return_value=state)
        service._save_state = MagicMock()
        service._write_audit = MagicMock()

        service.run_open()

    observation_date = local_signal_time.date().isoformat()
    for spec in STRATEGY_SPECS:
        strategy_state = state["strategies"][spec.strategy_id]
        assert strategy_state["observations"][observation_date]["open_status"] == "failed"
        assert strategy_state["started_on"] is None
        assert strategy_state["status"] == "ready"


def test_open_watches_every_fifteen_seconds_and_buys_on_first_eligible_sample() -> None:
    clock = {"now": datetime(2026, 8, 18, 9, 29, 50, tzinfo=ZoneInfo("Asia/Shanghai"))}

    def sleep(seconds: float) -> None:
        clock["now"] += timedelta(seconds=seconds)

    alphasift = MagicMock()
    alphasift.screen.side_effect = [
        {"candidates": [{"code": "600001", "score": 90}]},
        RuntimeError("trend screen unavailable"),
    ]
    state = {
        "schema_version": 1,
        "strategies": {
            spec.strategy_id: {
                "account_id": index,
                "status": "ready",
                "started_on": None,
                "completed_sessions": [],
                "observations": {},
                "position_meta": {},
            }
            for index, spec in enumerate(STRATEGY_SPECS, start=1)
        },
    }
    first = _quote(
        "600001",
        price=10.1,
        open_price=10.0,
        low=9.9,
        high=10.3,
        vwap=10.0,
    )
    eligible = _quote(
        "600001",
        price=10.3,
        open_price=10.0,
        low=9.9,
        high=10.35,
        vwap=10.1,
    )
    fill = {**eligible, "price": 10.31, "provider_timestamp": clock["now"].isoformat()}

    with TemporaryDirectory() as temp_dir:
        service = Hotspot0945PaperService(
            portfolio=MagicMock(),
            alphasift=alphasift,
            state_dir=Path(temp_dir),
            now_fn=lambda: clock["now"],
            sleep_fn=sleep,
        )
        service._require_cn_trading_session = MagicMock()
        service._load_state = MagicMock(return_value=state)
        service._save_state = MagicMock()
        service._write_audit = MagicMock()
        service._fetch_quotes = MagicMock(side_effect=[
            {"600001": first},
            {"600001": eligible},
            {"600001": fill},
        ])
        service._execute_buy = MagicMock(return_value={
            "strategy_id": STRATEGY_SPECS[0].strategy_id,
            "side": "buy",
            "code": "600001",
        })

        result = service.run_open()

    assert result["entry_watch"]["start_time"] == "09:30:00"
    assert result["entry_watch"]["end_time"] == "09:35:00"
    assert result["entry_watch"]["poll_seconds"] == 15.0
    assert result["entry_watch"]["sample_count"] == 2
    assert result["trades"][0]["signal_observed_at"].endswith("09:30:15+08:00")
    assert result["trades"][0]["fill_observed_at"].endswith("09:30:15+08:00")
    assert service._fetch_quotes.call_count == 3
    service._execute_buy.assert_called_once()


def test_open_never_backfills_at_or_after_0935() -> None:
    now = datetime(2026, 8, 18, 9, 35, tzinfo=ZoneInfo("Asia/Shanghai"))
    service = Hotspot0945PaperService(
        portfolio=MagicMock(),
        alphasift=MagicMock(),
        now_fn=lambda: now,
    )
    service._require_cn_trading_session = MagicMock()
    service._record_phase_failure = MagicMock(return_value={"status": "failed"})

    result = service.run_open()

    assert result["status"] == "failed"
    service._record_phase_failure.assert_called_once_with(
        now.date(),
        "open",
        "entry_watch_window_missed",
    )
    service.alphasift.screen.assert_not_called()


def _exit_service() -> Hotspot0945PaperService:
    return Hotspot0945PaperService(
        portfolio=MagicMock(),
        alphasift=MagicMock(),
        now_fn=lambda: NOW,
    )


def _strategy_state(code: str = "600001", *, account_id: int = 11):
    return {
        "account_id": account_id,
        "completed_sessions": [],
        "observations": {},
        "position_meta": {
            code: {
                "entry_date": "2026-08-10",
                "entry_price": 10.0,
                "peak_price": 10.0,
            }
        },
    }


def _campaign_state(code: str = "600001"):
    return {
        "strategies": {
            STRATEGY_SPECS[0].strategy_id: _strategy_state(code, account_id=11),
            STRATEGY_SPECS[1].strategy_id: {
                "account_id": 12,
                "completed_sessions": [],
                "observations": {},
                "position_meta": {},
            },
        }
    }


def test_normalize_auction_quote_accepts_the_frozen_0925_result_at_0927() -> None:
    observed_at = datetime(2026, 8, 11, 9, 27, tzinfo=ZoneInfo("Asia/Shanghai"))
    provider_at = datetime(2026, 8, 11, 9, 25, tzinfo=ZoneInfo("Asia/Shanghai"))

    quote = normalize_auction_quote(
        {
            "code": "600001",
            "price": 10.5,
            "pre_close": 10.0,
            "limit_up_price": 11.0,
            "provider_timestamp": provider_at.isoformat(),
            "source": "tencent",
        },
        now=observed_at,
    )

    assert quote["available"] is True
    assert quote["age_seconds"] == 120.0
    assert round(quote["auction_gain_pct"], 6) == 5.0


def test_auction_gain_below_five_percent_sells_at_continuous_open() -> None:
    service = _exit_service()
    state = _campaign_state()
    quotes = {
        "600001": {
            "available": True,
            "price": 10.49,
            "auction_gain_pct": 4.9,
        }
    }

    intents, watching, evidence = service._classify_auction_positions(
        state,
        quotes,
        date(2026, 8, 11),
    )

    assert intents[0]["reason"] == "auction_gain_below_5pct"
    assert watching == {}
    assert evidence[STRATEGY_SPECS[0].strategy_id]["600001"]["status"] == "sell_at_continuous_open"


def test_auction_gain_equal_to_five_percent_enters_limit_up_watch() -> None:
    service = _exit_service()
    state = _campaign_state()

    intents, watching, _ = service._classify_auction_positions(
        state,
        {"600001": {"available": True, "price": 10.5, "auction_gain_pct": 5.0}},
        date(2026, 8, 11),
    )

    assert intents == []
    assert list(watching.values())[0]["auction_gain_pct"] == 5.0


def test_auction_exit_observes_t_plus_one() -> None:
    service = _exit_service()
    state = _campaign_state()
    state["strategies"][STRATEGY_SPECS[0].strategy_id]["position_meta"]["600001"]["entry_date"] = "2026-08-11"

    intents, watching, evidence = service._classify_auction_positions(
        state,
        {"600001": {"available": True, "price": 9.0, "auction_gain_pct": -10.0}},
        date(2026, 8, 11),
    )

    assert intents == []
    assert watching == {}
    assert evidence[STRATEGY_SPECS[0].strategy_id]["600001"]["status"] == "held_by_t_plus_one"


def test_opening_watch_holds_after_touching_limit_up() -> None:
    state = _campaign_state()
    watching = {
        f"{STRATEGY_SPECS[0].strategy_id}:600001": {
            "strategy_id": STRATEGY_SPECS[0].strategy_id,
            "account_id": 11,
            "code": "600001",
            "auction_gain_pct": 6.0,
            "peak_price": 10.6,
            "trailing_stop_pct": 2.0,
            "limit_up_seen": False,
        }
    }
    observed_at = datetime(2026, 8, 11, 9, 35, tzinfo=ZoneInfo("Asia/Shanghai"))

    Hotspot0945PaperService._update_opening_watch(
        watching,
        {"600001": _quote("600001", price=10.95, open_price=10.6, low=10.55, high=11.0, vwap=10.8)},
        observed_at,
    )
    results = Hotspot0945PaperService._apply_opening_watch_state(state, watching, date(2026, 8, 11))

    key = next(iter(watching))
    meta = state["strategies"][STRATEGY_SPECS[0].strategy_id]["position_meta"]["600001"]
    assert results[key]["status"] == "held_after_limit_up"
    assert meta["limit_up_seen"] is True
    assert meta["trailing_armed"] is False


def test_exit_watch_integrates_auction_and_limit_up_without_selling() -> None:
    class Clock:
        current = datetime(2026, 8, 11, 9, 27, tzinfo=ZoneInfo("Asia/Shanghai"))

        def now(self):
            return self.current

        def sleep(self, seconds: float):
            self.current += timedelta(seconds=seconds)

    clock = Clock()
    state = _campaign_state()

    def quote_getter(code: str):
        if clock.now().hour == 9 and clock.now().minute < 30:
            return {
                "code": code,
                "price": 10.6,
                "pre_close": 10.0,
                "limit_up_price": 11.0,
                "provider_timestamp": clock.now().isoformat(),
                "source": "test",
            }
        return {
            "code": code,
            "price": 11.0,
            "open_price": 10.6,
            "high": 11.0,
            "low": 10.6,
            "pre_close": 10.0,
            "limit_up_price": 11.0,
            "provider_timestamp": clock.now().isoformat(),
            "source": "test",
        }

    service = Hotspot0945PaperService(
        portfolio=MagicMock(),
        alphasift=MagicMock(),
        quote_getter=quote_getter,
        now_fn=clock.now,
        sleep_fn=clock.sleep,
    )
    service._require_cn_trading_session = MagicMock()
    service._load_state = MagicMock(return_value=state)
    service._save_state = MagicMock()
    service._write_audit = MagicMock()

    result = service.run_exit_watch()

    meta = state["strategies"][STRATEGY_SPECS[0].strategy_id]["position_meta"]["600001"]
    assert result["status"] == "completed"
    assert result["trades"] == []
    assert meta["limit_up_seen"] is True
    assert meta["trailing_armed"] is False
    assert clock.now().hour == 9 and clock.now().minute == 48


def test_no_limit_up_arms_strategy_specific_trailing_stop() -> None:
    state = _campaign_state()
    key = f"{STRATEGY_SPECS[0].strategy_id}:600001"
    watching = {
        key: {
            "strategy_id": STRATEGY_SPECS[0].strategy_id,
            "account_id": 11,
            "code": "600001",
            "auction_gain_pct": 6.0,
            "peak_price": 10.8,
            "trailing_stop_pct": 2.0,
            "limit_up_price_observed": True,
            "limit_up_seen": False,
        }
    }

    results = Hotspot0945PaperService._apply_opening_watch_state(state, watching, date(2026, 8, 11))

    meta = state["strategies"][STRATEGY_SPECS[0].strategy_id]["position_meta"]["600001"]
    assert results[key]["status"] == "trailing_armed"
    assert meta["trailing_peak_price"] == 10.8
    assert meta["trailing_stop_pct"] == 2.0


def test_missing_limit_up_price_holds_fail_closed() -> None:
    state = _campaign_state()
    key = f"{STRATEGY_SPECS[0].strategy_id}:600001"
    watching = {
        key: {
            "strategy_id": STRATEGY_SPECS[0].strategy_id,
            "account_id": 11,
            "code": "600001",
            "auction_gain_pct": 6.0,
            "peak_price": 10.8,
            "trailing_stop_pct": 2.0,
            "limit_up_price_observed": False,
            "limit_up_seen": False,
        }
    }

    results = Hotspot0945PaperService._apply_opening_watch_state(state, watching, date(2026, 8, 11))

    meta = state["strategies"][STRATEGY_SPECS[0].strategy_id]["position_meta"]["600001"]
    assert results[key]["status"] == "held_fail_closed"
    assert meta["trailing_armed"] is False


def test_oversold_trailing_stop_uses_two_percent_drawdown() -> None:
    service = _exit_service()
    state = _strategy_state()
    meta = state["position_meta"]["600001"]
    meta.update(
        {
            "exit_watch_date": "2026-08-11",
            "trailing_armed": True,
            "limit_up_seen": False,
            "trailing_peak_price": 10.8,
            "trailing_stop_pct": 2.0,
        }
    )
    quote = _quote("600001", price=10.58, open_price=10.6, low=10.55, high=10.8, vwap=10.65)

    intents = service._build_trailing_exit_intents(
        STRATEGY_SPECS[0],
        state,
        {"600001": quote},
        date(2026, 8, 11),
        check_phase="watch-exit",
    )

    assert intents[0]["reason"] == "opening_follow_through_trailing_stop"
    assert intents[0]["trailing_stop_pct"] == 2.0


def test_trend_trailing_stop_keeps_three_percent_width() -> None:
    service = _exit_service()
    state = _strategy_state(account_id=12)
    meta = state["position_meta"]["600001"]
    meta.update(
        {
            "exit_watch_date": "2026-08-11",
            "trailing_armed": True,
            "limit_up_seen": False,
            "trailing_peak_price": 10.8,
            "trailing_stop_pct": 3.0,
        }
    )

    not_triggered = service._build_trailing_exit_intents(
        STRATEGY_SPECS[1],
        state,
        {"600001": _quote("600001", price=10.5, open_price=10.6, low=10.45, high=10.8, vwap=10.6)},
        date(2026, 8, 11),
        check_phase="watch-exit",
    )
    triggered = service._build_trailing_exit_intents(
        STRATEGY_SPECS[1],
        state,
        {"600001": _quote("600001", price=10.47, open_price=10.6, low=10.45, high=10.8, vwap=10.58)},
        date(2026, 8, 11),
        check_phase="watch-exit",
    )

    assert not_triggered == []
    assert triggered[0]["trailing_stop_pct"] == 3.0


def test_old_hard_stop_no_longer_sells_without_todays_watch_state() -> None:
    service = _exit_service()
    state = _strategy_state()
    quote = _quote("600001", price=9.0, open_price=9.1, low=8.95, high=9.2, vwap=9.05)

    intents = service._build_trailing_exit_intents(
        STRATEGY_SPECS[0],
        state,
        {"600001": quote},
        date(2026, 8, 11),
        check_phase="close",
    )

    assert intents == []
