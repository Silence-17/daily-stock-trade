# -*- coding: utf-8 -*-
"""Pure-forward 09:45 hotspot paper campaigns.

The service owns two isolated local Portfolio ledgers.  AlphaSift creates the
morning candidate pools, provider-timestamped quotes confirm the 09:45 signal,
and a second quote snapshot is used as the simulated next-minute fill.  No
historical replay can advance the campaign counter.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional
from zoneinfo import ZoneInfo

from src.config import get_config
from src.core.trading_calendar import get_market_session_bounds
from src.services.alphasift_service import AlphaSiftService, get_dsa_realtime_quote
from src.services.portfolio_service import PortfolioService

logger = logging.getLogger(__name__)

SHANGHAI = ZoneInfo("Asia/Shanghai")
TARGET_SESSIONS = 30
INITIAL_CASH = 100_000.0
SIGNAL_TIME = dt_time(9, 45)
SIGNAL_DEADLINE = dt_time(9, 46, 30)
FILL_TIME = dt_time(9, 46)
CLOSE_SIGNAL_TIME = dt_time(14, 55)
CLOSE_FILL_TIME = dt_time(14, 56)
MAX_QUOTE_AGE_SECONDS = 120

COMMISSION_RATE = 0.00008
MIN_COMMISSION = 5.0
SELL_STAMP_DUTY_RATE = 0.0005
TRANSFER_FEE_RATE = 0.00001
SLIPPAGE_RATE = 0.0005

STATE_DIR_ENV = "HOTSPOT_0945_PAPER_DATA_DIR"
DEFAULT_STATE_DIR = Path("data") / "hotspot_0945_paper"


@dataclass(frozen=True)
class StrategySpec:
    strategy_id: str
    account_name: str
    screen_strategy: str
    position_pct: float
    max_positions: int
    min_early_return_pct: float
    min_rebound_pct: float
    stop_loss_pct: float
    take_profit_pct: float
    trailing_stop_pct: float
    max_holding_sessions: int


STRATEGY_SPECS: tuple[StrategySpec, ...] = (
    StrategySpec(
        strategy_id="hotspot_oversold_reversal_0945_v1",
        account_name="30日模拟｜热点超跌反转09:45",
        screen_strategy="oversold_reversal",
        position_pct=0.25,
        max_positions=2,
        min_early_return_pct=1.5,
        min_rebound_pct=1.0,
        stop_loss_pct=3.0,
        take_profit_pct=5.0,
        trailing_stop_pct=3.0,
        max_holding_sessions=2,
    ),
    StrategySpec(
        strategy_id="hotspot_trend_reacceleration_0945_v1",
        account_name="30日模拟｜热点趋势再加速09:45",
        screen_strategy="momentum_quality",
        position_pct=0.30,
        max_positions=2,
        min_early_return_pct=2.0,
        min_rebound_pct=0.5,
        stop_loss_pct=2.5,
        take_profit_pct=6.0,
        trailing_stop_pct=3.0,
        max_holding_sessions=3,
    ),
)


def _safe_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _source_text(value: Any) -> str:
    raw = getattr(value, "value", value)
    return str(raw or "").strip()


def _parse_datetime(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI)
    return parsed


def normalize_quote(raw: Mapping[str, Any], *, now: datetime) -> Dict[str, Any]:
    """Return the strict subset needed by the strategy or an unavailable row."""

    provider_time = _parse_datetime(raw.get("provider_timestamp"))
    if provider_time is None:
        return {"available": False, "reason": "provider_timestamp_missing"}
    age_seconds = (now.astimezone(timezone.utc) - provider_time.astimezone(timezone.utc)).total_seconds()
    if age_seconds < -1 or age_seconds > MAX_QUOTE_AGE_SECONDS:
        return {
            "available": False,
            "reason": "quote_stale_or_future",
            "age_seconds": round(age_seconds, 3),
        }

    price = _safe_float(raw.get("price"))
    open_price = _safe_float(raw.get("open_price") or raw.get("open"))
    high = _safe_float(raw.get("high"))
    low = _safe_float(raw.get("low"))
    volume = _safe_float(raw.get("volume"))
    amount = _safe_float(raw.get("amount"))
    if not all(value is not None and value > 0 for value in (price, open_price, high, low)):
        return {"available": False, "reason": "quote_price_fields_missing"}
    if high < low or not (low <= price <= high * 1.001):
        return {"available": False, "reason": "quote_price_range_invalid"}

    vwap = None
    if amount is not None and volume is not None and amount > 0 and volume > 0:
        candidate_vwap = amount / volume
        if low * 0.95 <= candidate_vwap <= high * 1.05:
            vwap = candidate_vwap

    early_return_pct = (price / open_price - 1.0) * 100.0
    rebound_pct = (price / low - 1.0) * 100.0
    resilience_pct = (low / open_price - 1.0) * 100.0
    range_position = (price - low) / (high - low) if high > low else 1.0
    return {
        "available": True,
        "code": str(raw.get("code") or "").strip(),
        "name": str(raw.get("name") or "").strip(),
        "price": price,
        "open_price": open_price,
        "high": high,
        "low": low,
        "vwap": vwap,
        "early_return_pct": early_return_pct,
        "rebound_pct": rebound_pct,
        "resilience_pct": resilience_pct,
        "range_position": range_position,
        "provider": _source_text(raw.get("source")),
        "provider_timestamp": provider_time.isoformat(),
        "age_seconds": round(age_seconds, 3),
    }


def _percentile_scores(values: List[float]) -> List[float]:
    if not values:
        return []
    if len(values) == 1:
        return [1.0]
    order = sorted(range(len(values)), key=lambda index: values[index])
    result = [0.0] * len(values)
    for rank, index in enumerate(order):
        result[index] = rank / (len(values) - 1)
    return result


def evaluate_open_candidates(
    spec: StrategySpec,
    candidates: Iterable[Mapping[str, Any]],
    quotes: Mapping[str, Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    """Rank candidates using only the frozen pool and the 09:45 quote."""

    rows: List[Dict[str, Any]] = []
    for candidate in candidates:
        code = str(candidate.get("code") or candidate.get("symbol") or "").strip()
        quote = dict(quotes.get(code) or {})
        if not code or not quote.get("available"):
            continue
        upstream_score = _safe_float(candidate.get("llm_score"))
        if upstream_score is None:
            upstream_score = _safe_float(candidate.get("score")) or 0.0
        rows.append(
            {
                "code": code,
                "name": str(candidate.get("name") or quote.get("name") or "").strip(),
                "upstream_score": max(0.0, min(upstream_score, 100.0)),
                "quote": quote,
            }
        )
    if not rows:
        return []

    early_scores = _percentile_scores([float(row["quote"]["early_return_pct"]) for row in rows])
    rebound_scores = _percentile_scores([float(row["quote"]["rebound_pct"]) for row in rows])
    resilience_scores = _percentile_scores([float(row["quote"]["resilience_pct"]) for row in rows])

    for index, row in enumerate(rows):
        quote = row["quote"]
        upstream = row["upstream_score"] / 100.0
        if spec.screen_strategy == "oversold_reversal":
            composite = 0.40 * early_scores[index] + 0.30 * rebound_scores[index] + 0.20 * upstream + 0.10 * resilience_scores[index]
            vwap_pass = True
        else:
            vwap = _safe_float(quote.get("vwap"))
            vwap_pass = vwap is not None and float(quote["price"]) >= vwap
            composite = 0.35 * early_scores[index] + 0.25 * resilience_scores[index] + 0.25 * upstream + 0.15 * (1.0 if vwap_pass else 0.0)
        blockers: List[str] = []
        if float(quote["early_return_pct"]) < spec.min_early_return_pct:
            blockers.append("early_return_below_threshold")
        if float(quote["rebound_pct"]) < spec.min_rebound_pct:
            blockers.append("rebound_below_threshold")
        if float(quote["range_position"]) < 0.60:
            blockers.append("range_position_too_low")
        if not vwap_pass:
            blockers.append("vwap_reclaim_unconfirmed")
        row["score"] = round(composite * 100.0, 6)
        row["blockers"] = blockers
        row["eligible"] = not blockers

    rows.sort(key=lambda item: (-float(item["score"]), item["code"]))
    for rank, row in enumerate(rows, start=1):
        row["rank"] = rank
        if rank > 2:
            row["blockers"].append("outside_combined_top_two")
            row["eligible"] = False
    return rows


class Hotspot0945PaperService:
    """Manage setup, morning execution, close risk checks, and 30-day state."""

    def __init__(
        self,
        *,
        portfolio: Optional[PortfolioService] = None,
        alphasift: Optional[AlphaSiftService] = None,
        state_dir: Optional[Path] = None,
        now_fn: Any = None,
        sleep_fn: Any = None,
    ):
        self.portfolio = portfolio or PortfolioService()
        self.alphasift = alphasift or AlphaSiftService(get_config())
        configured = os.getenv(STATE_DIR_ENV, "").strip()
        self.state_dir = Path(state_dir or configured or DEFAULT_STATE_DIR)
        self.state_path = self.state_dir / "state.json"
        self.audit_dir = self.state_dir / "audits"
        self.now_fn = now_fn or (lambda: datetime.now(SHANGHAI))
        self.sleep_fn = sleep_fn or time.sleep

    def setup(self) -> Dict[str, Any]:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.audit_dir.mkdir(parents=True, exist_ok=True)
        state = self._load_state()
        accounts = self.portfolio.list_accounts(include_inactive=True)
        created: List[int] = []
        for spec in STRATEGY_SPECS:
            matches = [
                account
                for account in accounts
                if account.get("owner_id") == spec.strategy_id and bool(account.get("is_active"))
            ]
            if len(matches) > 1:
                raise RuntimeError(f"multiple_active_campaign_accounts:{spec.strategy_id}")
            if matches:
                account = matches[0]
            else:
                account = self.portfolio.create_account(
                    name=spec.account_name,
                    broker="paper_0945",
                    market="cn",
                    base_currency="CNY",
                    owner_id=spec.strategy_id,
                )
                self.portfolio.record_cash_ledger(
                    account_id=int(account["id"]),
                    event_date=self.now_fn().date(),
                    direction="in",
                    amount=INITIAL_CASH,
                    currency="CNY",
                    note=f"{spec.strategy_id} 30-session initial capital",
                )
                created.append(int(account["id"]))
                accounts.append(account)
            strategy_state = state["strategies"].setdefault(spec.strategy_id, {})
            stored_id = strategy_state.get("account_id")
            if stored_id not in (None, int(account["id"])):
                raise RuntimeError(f"campaign_account_mismatch:{spec.strategy_id}")
            strategy_state.update(
                {
                    "account_id": int(account["id"]),
                    "account_name": spec.account_name,
                    "status": strategy_state.get("status") or "ready",
                    "completed_sessions": list(strategy_state.get("completed_sessions") or []),
                    "observations": dict(strategy_state.get("observations") or {}),
                    "position_meta": dict(strategy_state.get("position_meta") or {}),
                }
            )
            self._validate_account_ledger(spec, strategy_state)
        self._save_state(state)
        return {
            "ready": True,
            "target_sessions": TARGET_SESSIONS,
            "created_account_ids": created,
            "strategies": {
                key: {
                    "account_id": value["account_id"],
                    "status": value["status"],
                    "completed_session_count": len(value["completed_sessions"]),
                }
                for key, value in state["strategies"].items()
            },
        }

    def run(self, phase: str = "auto") -> Dict[str, Any]:
        setup = self.setup()
        now = self.now_fn()
        selected_phase = phase.strip().lower()
        if selected_phase == "auto":
            selected_phase = "open" if now.timetz().replace(tzinfo=None) < dt_time(12, 0) else "close"
        try:
            if selected_phase == "open":
                result = self.run_open()
            elif selected_phase == "close":
                result = self.run_close()
            elif selected_phase == "status":
                result = self.status()
            else:
                raise ValueError("phase must be auto, open, close, or status")
        except RuntimeError as exc:
            if str(exc) != "not_cn_trading_day":
                raise
            result = {
                "phase": selected_phase,
                "session_date": now.date().isoformat(),
                "status": "skipped",
                "reason": "not_cn_trading_day",
            }
        result.setdefault("setup", setup)
        return result

    def status(self) -> Dict[str, Any]:
        state = self._load_state()
        return {
            "target_sessions": TARGET_SESSIONS,
            "strategies": {
                strategy_id: {
                    "account_id": item.get("account_id"),
                    "status": item.get("status"),
                    "started_on": item.get("started_on"),
                    "completed_session_count": len(item.get("completed_sessions") or []),
                    "remaining_sessions": max(0, TARGET_SESSIONS - len(item.get("completed_sessions") or [])),
                    "last_completed_session": (item.get("completed_sessions") or [None])[-1],
                }
                for strategy_id, item in state.get("strategies", {}).items()
            },
        }

    def run_open(self) -> Dict[str, Any]:
        now = self.now_fn()
        self._require_cn_trading_session(now)
        if now.timetz().replace(tzinfo=None) > SIGNAL_DEADLINE:
            return self._record_phase_failure(now.date(), "open", "opening_signal_window_missed")

        screens: Dict[str, Dict[str, Any]] = {}
        screen_errors: Dict[str, str] = {}
        for spec in STRATEGY_SPECS:
            try:
                screens[spec.strategy_id] = self.alphasift.screen(
                    strategy=spec.screen_strategy,
                    market="cn",
                    max_results=6,
                    use_llm=False,
                )
            except Exception as exc:  # noqa: BLE001 - fail closed and persist the reason.
                screen_errors[spec.strategy_id] = f"{type(exc).__name__}: {exc}"

        self._wait_until(SIGNAL_TIME)
        signal_now = self.now_fn()
        if signal_now.timetz().replace(tzinfo=None) > SIGNAL_DEADLINE:
            return self._record_phase_failure(now.date(), "open", "opening_signal_window_missed_after_screen")

        state = self._load_state()
        candidate_codes: set[str] = set()
        for payload in screens.values():
            for candidate in payload.get("candidates") or []:
                code = str(candidate.get("code") or candidate.get("symbol") or "").strip()
                if code:
                    candidate_codes.add(code)
        for item in state.get("strategies", {}).values():
            candidate_codes.update(str(code) for code in (item.get("position_meta") or {}).keys())
        quotes = self._fetch_quotes(candidate_codes, now=signal_now)

        decisions: Dict[str, List[Dict[str, Any]]] = {}
        sell_intents: List[Dict[str, Any]] = []
        buy_intents: List[Dict[str, Any]] = []
        for spec in STRATEGY_SPECS:
            strategy_state = state["strategies"][spec.strategy_id]
            if strategy_state.get("status") == "completed":
                sell_intents.extend(self._build_exit_intents(spec, strategy_state, quotes, signal_now.date(), force=True))
                decisions[spec.strategy_id] = []
                continue
            if spec.strategy_id in screen_errors:
                decisions[spec.strategy_id] = []
                continue
            candidate_rows = screens.get(spec.strategy_id, {}).get("candidates") or []
            ranked = evaluate_open_candidates(spec, candidate_rows, quotes)
            decisions[spec.strategy_id] = ranked
            sell_intents.extend(self._build_exit_intents(spec, strategy_state, quotes, signal_now.date()))
            held = set((strategy_state.get("position_meta") or {}).keys())
            eligible = next((row for row in ranked if row.get("eligible") and row["code"] not in held), None)
            if eligible is not None:
                buy_intents.append({"spec": spec, "row": eligible})

        self._wait_until(FILL_TIME)
        fill_now = self.now_fn()
        fill_codes = {intent["code"] for intent in sell_intents}
        fill_codes.update(intent["row"]["code"] for intent in buy_intents)
        fill_quotes = self._fetch_quotes(fill_codes, now=fill_now)

        trades: List[Dict[str, Any]] = []
        for intent in sell_intents:
            trade = self._execute_sell(intent, fill_quotes, fill_now.date(), state)
            if trade:
                trades.append(trade)
        for intent in buy_intents:
            trade = self._execute_buy(intent["spec"], intent["row"], fill_quotes, fill_now.date(), state)
            if trade:
                trades.append(trade)

        date_key = fill_now.date().isoformat()
        for spec in STRATEGY_SPECS:
            strategy_state = state["strategies"][spec.strategy_id]
            observation = strategy_state["observations"].setdefault(date_key, {})
            error = screen_errors.get(spec.strategy_id)
            valid_quote_count = sum(
                1
                for row in decisions.get(spec.strategy_id, [])
                if row.get("quote", {}).get("available")
            )
            completed = error is None and valid_quote_count >= min(2, len(screens.get(spec.strategy_id, {}).get("candidates") or []))
            observation.update(
                {
                    "open_status": "completed" if completed else "failed",
                    "open_completed_at": fill_now.isoformat(),
                    "screen_strategy": spec.screen_strategy,
                    "screen_error": error,
                    "valid_quote_count": valid_quote_count,
                    "decision_count": len(decisions.get(spec.strategy_id, [])),
                    "trade_count": sum(1 for trade in trades if trade["strategy_id"] == spec.strategy_id),
                }
            )
            if completed and not strategy_state.get("started_on"):
                strategy_state["started_on"] = date_key
                strategy_state["status"] = "running"
        self._save_state(state)
        audit = {
            "phase": "open",
            "session_date": date_key,
            "completed_at": fill_now.isoformat(),
            "screen_errors": screen_errors,
            "decisions": decisions,
            "trades": trades,
        }
        self._write_audit(date_key, "open", audit)
        return audit

    def run_close(self) -> Dict[str, Any]:
        now = self.now_fn()
        self._require_cn_trading_session(now)
        self._wait_until(CLOSE_SIGNAL_TIME)
        signal_now = self.now_fn()
        state = self._load_state()
        position_codes = {
            code
            for item in state.get("strategies", {}).values()
            for code in (item.get("position_meta") or {}).keys()
        }
        quotes = self._fetch_quotes(position_codes, now=signal_now)
        sell_intents: List[Dict[str, Any]] = []
        for spec in STRATEGY_SPECS:
            strategy_state = state["strategies"][spec.strategy_id]
            sell_intents.extend(
                self._build_exit_intents(
                    spec,
                    strategy_state,
                    quotes,
                    signal_now.date(),
                    force=strategy_state.get("status") == "completed",
                )
            )
        self._wait_until(CLOSE_FILL_TIME)
        fill_now = self.now_fn()
        fill_quotes = self._fetch_quotes({item["code"] for item in sell_intents}, now=fill_now)
        trades = [
            trade
            for trade in (self._execute_sell(intent, fill_quotes, fill_now.date(), state) for intent in sell_intents)
            if trade
        ]

        date_key = fill_now.date().isoformat()
        results: Dict[str, Dict[str, Any]] = {}
        for spec in STRATEGY_SPECS:
            strategy_state = state["strategies"][spec.strategy_id]
            observation = strategy_state["observations"].setdefault(date_key, {})
            open_complete = observation.get("open_status") == "completed"
            remaining_codes = set((strategy_state.get("position_meta") or {}).keys())
            quotes_complete = all((fill_quotes.get(code) or quotes.get(code) or {}).get("available") for code in remaining_codes)
            close_complete = bool(open_complete and quotes_complete)
            observation.update(
                {
                    "close_status": "completed" if close_complete else "failed",
                    "close_completed_at": fill_now.isoformat(),
                    "valuation_quote_count": len(remaining_codes),
                }
            )
            if close_complete:
                overrides = {
                    code: {
                        "price": float((fill_quotes.get(code) or quotes[code])["price"]),
                        "provider": (fill_quotes.get(code) or quotes[code]).get("provider"),
                        "provider_timestamp": (fill_quotes.get(code) or quotes[code]).get("provider_timestamp"),
                    }
                    for code in remaining_codes
                }
                snapshot = self.portfolio.get_portfolio_snapshot(
                    account_id=int(strategy_state["account_id"]),
                    as_of=fill_now.date(),
                    persist=True,
                    realtime_price_overrides=overrides,
                )
                if date_key not in strategy_state["completed_sessions"]:
                    strategy_state["completed_sessions"].append(date_key)
                    strategy_state["completed_sessions"].sort()
                if len(strategy_state["completed_sessions"]) >= TARGET_SESSIONS:
                    strategy_state["status"] = "completed"
                    strategy_state["completed_on"] = date_key
                results[spec.strategy_id] = {
                    "status": strategy_state["status"],
                    "completed_session_count": len(strategy_state["completed_sessions"]),
                    "remaining_sessions": max(0, TARGET_SESSIONS - len(strategy_state["completed_sessions"])),
                    "total_equity": snapshot["accounts"][0]["total_equity"],
                }
            else:
                results[spec.strategy_id] = {
                    "status": strategy_state.get("status"),
                    "completed_session_count": len(strategy_state["completed_sessions"]),
                    "reason": "open_or_closing_evidence_incomplete",
                }
        self._save_state(state)
        audit = {
            "phase": "close",
            "session_date": date_key,
            "completed_at": fill_now.isoformat(),
            "trades": trades,
            "results": results,
        }
        self._write_audit(date_key, "close", audit)
        return audit

    def _fetch_quotes(self, codes: Iterable[str], *, now: datetime) -> Dict[str, Dict[str, Any]]:
        unique = sorted({str(code).strip() for code in codes if str(code).strip()})
        if not unique:
            return {}
        result: Dict[str, Dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=min(4, len(unique))) as executor:
            futures = {executor.submit(get_dsa_realtime_quote, code): code for code in unique}
            for future in as_completed(futures):
                code = futures[future]
                try:
                    raw = future.result()
                    normalized = normalize_quote(raw, now=now)
                except Exception as exc:  # noqa: BLE001
                    normalized = {"available": False, "reason": f"quote_fetch_failed:{type(exc).__name__}"}
                normalized.setdefault("code", code)
                result[code] = normalized
        return result

    def _validate_account_ledger(self, spec: StrategySpec, strategy_state: Mapping[str, Any]) -> None:
        """Fail closed when the dedicated account and campaign state diverge."""

        account_id = int(strategy_state["account_id"])
        trades = self.portfolio.repo.list_trades(account_id, as_of=self.now_fn().date())
        quantities: Dict[str, float] = {}
        expected_prefix = f"h0945:{spec.strategy_id}:"
        for trade in trades:
            trade_uid = str(getattr(trade, "trade_uid", "") or "")
            if not trade_uid.startswith(expected_prefix):
                raise RuntimeError(f"campaign_account_contains_non_strategy_trade:{spec.strategy_id}")
            code = str(getattr(trade, "symbol", "") or "").strip()
            quantity = float(getattr(trade, "quantity", 0.0) or 0.0)
            side = str(getattr(trade, "side", "") or "").strip().lower()
            quantities[code] = quantities.get(code, 0.0) + (quantity if side == "buy" else -quantity)
        ledger_symbols = {code for code, quantity in quantities.items() if quantity > 1e-6}
        state_symbols = {str(code) for code in (strategy_state.get("position_meta") or {}).keys()}
        if ledger_symbols != state_symbols:
            raise RuntimeError(f"campaign_account_state_mismatch:{spec.strategy_id}")

    def _build_exit_intents(
        self,
        spec: StrategySpec,
        strategy_state: Dict[str, Any],
        quotes: Mapping[str, Mapping[str, Any]],
        session_date: date,
        *,
        force: bool = False,
    ) -> List[Dict[str, Any]]:
        intents: List[Dict[str, Any]] = []
        completed = list(strategy_state.get("completed_sessions") or [])
        for code, meta in list((strategy_state.get("position_meta") or {}).items()):
            quote = dict(quotes.get(code) or {})
            if not quote.get("available"):
                continue
            entry_date = date.fromisoformat(str(meta["entry_date"]))
            if entry_date >= session_date:
                continue
            entry_price = float(meta["entry_price"])
            price = float(quote["price"])
            peak = max(float(meta.get("peak_price") or entry_price), price)
            meta["peak_price"] = peak
            return_pct = (price / entry_price - 1.0) * 100.0
            holding_sessions = sum(1 for value in completed if entry_date.isoformat() < value <= session_date.isoformat())
            reason = None
            if force:
                reason = "campaign_complete_liquidation"
            elif return_pct <= -spec.stop_loss_pct:
                reason = "stop_loss_triggered"
            elif return_pct >= spec.take_profit_pct:
                reason = "take_profit_triggered"
            elif peak >= entry_price * (1.0 + spec.take_profit_pct / 100.0) and price <= peak * (1.0 - spec.trailing_stop_pct / 100.0):
                reason = "trailing_stop_triggered"
            elif holding_sessions >= spec.max_holding_sessions:
                reason = "max_holding_sessions_reached"
            elif holding_sessions >= 1 and price < float(quote["open_price"]):
                reason = "session_strength_lost"
            if reason:
                intents.append(
                    {
                        "strategy_id": spec.strategy_id,
                        "account_id": int(strategy_state["account_id"]),
                        "code": code,
                        "reason": reason,
                    }
                )
        return intents

    def _execute_sell(
        self,
        intent: Mapping[str, Any],
        quotes: Mapping[str, Mapping[str, Any]],
        session_date: date,
        state: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        code = str(intent["code"])
        quote = dict(quotes.get(code) or {})
        if not quote.get("available"):
            return None
        account_id = int(intent["account_id"])
        quantity = self.portfolio.get_sellable_quantity(
            account_id=account_id,
            symbol=code,
            trade_date=session_date,
            market="cn",
            currency="CNY",
            enforce_t_plus_one=True,
        )
        quantity = math.floor(quantity / 100.0) * 100.0
        if quantity <= 0:
            return None
        price = float(quote["price"]) * (1.0 - SLIPPAGE_RATE)
        notional = price * quantity
        fee = max(MIN_COMMISSION, notional * COMMISSION_RATE) + notional * TRANSFER_FEE_RATE
        tax = notional * SELL_STAMP_DUTY_RATE
        uid = f"h0945:{intent['strategy_id']}:{session_date}:sell:{code}:{intent['reason']}"
        self.portfolio.record_trade(
            account_id=account_id,
            symbol=code,
            trade_date=session_date,
            side="sell",
            quantity=quantity,
            price=price,
            fee=fee,
            tax=tax,
            market="cn",
            currency="CNY",
            trade_uid=uid,
            dedup_hash=uid,
            note=f"09:45 paper exit: {intent['reason']}",
        )
        state["strategies"][intent["strategy_id"]]["position_meta"].pop(code, None)
        return {
            "strategy_id": intent["strategy_id"],
            "account_id": account_id,
            "side": "sell",
            "code": code,
            "quantity": quantity,
            "price": round(price, 6),
            "fee": round(fee, 6),
            "tax": round(tax, 6),
            "reason": intent["reason"],
            "provider_timestamp": quote.get("provider_timestamp"),
        }

    def _execute_buy(
        self,
        spec: StrategySpec,
        row: Mapping[str, Any],
        quotes: Mapping[str, Mapping[str, Any]],
        session_date: date,
        state: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        strategy_state = state["strategies"][spec.strategy_id]
        if strategy_state.get("status") == "completed":
            return None
        if len(strategy_state.get("position_meta") or {}) >= spec.max_positions:
            return None
        code = str(row["code"])
        quote = dict(quotes.get(code) or {})
        if not quote.get("available"):
            return None
        account_id = int(strategy_state["account_id"])
        overrides = {
            symbol: {
                "price": float((quotes.get(symbol) or meta).get("price") or meta.get("entry_price")),
                "provider": (quotes.get(symbol) or {}).get("provider"),
                "provider_timestamp": (quotes.get(symbol) or {}).get("provider_timestamp"),
            }
            for symbol, meta in (strategy_state.get("position_meta") or {}).items()
        }
        snapshot = self.portfolio.get_portfolio_snapshot(
            account_id=account_id,
            as_of=session_date,
            persist=False,
            realtime_price_overrides=overrides,
        )["accounts"][0]
        cash = float(snapshot["total_cash"])
        equity = float(snapshot["total_equity"])
        budget = min(cash, equity * spec.position_pct)
        price = float(quote["price"]) * (1.0 + SLIPPAGE_RATE)
        quantity = math.floor((max(0.0, budget - MIN_COMMISSION) / (price * (1.0 + TRANSFER_FEE_RATE))) / 100.0) * 100.0
        if quantity <= 0:
            return None
        notional = price * quantity
        fee = max(MIN_COMMISSION, notional * COMMISSION_RATE) + notional * TRANSFER_FEE_RATE
        if notional + fee > cash + 1e-6:
            return None
        uid = f"h0945:{spec.strategy_id}:{session_date}:buy:{code}"
        self.portfolio.record_trade(
            account_id=account_id,
            symbol=code,
            trade_date=session_date,
            side="buy",
            quantity=quantity,
            price=price,
            fee=fee,
            tax=0.0,
            market="cn",
            currency="CNY",
            trade_uid=uid,
            dedup_hash=uid,
            note=f"09:45 signal rank={row.get('rank')} score={row.get('score')}",
        )
        strategy_state["position_meta"][code] = {
            "entry_date": session_date.isoformat(),
            "entry_price": price,
            "peak_price": price,
            "entry_score": row.get("score"),
        }
        return {
            "strategy_id": spec.strategy_id,
            "account_id": account_id,
            "side": "buy",
            "code": code,
            "quantity": quantity,
            "price": round(price, 6),
            "fee": round(fee, 6),
            "tax": 0.0,
            "score": row.get("score"),
            "provider_timestamp": quote.get("provider_timestamp"),
        }

    def _require_cn_trading_session(self, now: datetime) -> None:
        try:
            session_open, session_close = get_market_session_bounds("cn", current_time=now, strict=True)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError("cn_trading_calendar_unavailable") from exc
        if session_open is None or session_close is None or session_open.date() != now.date():
            raise RuntimeError("not_cn_trading_day")

    def _wait_until(self, target: dt_time) -> None:
        now = self.now_fn()
        target_dt = datetime.combine(now.date(), target, tzinfo=SHANGHAI)
        remaining = (target_dt - now).total_seconds()
        while remaining > 0:
            self.sleep_fn(min(remaining, 30.0))
            now = self.now_fn()
            remaining = (target_dt - now).total_seconds()

    def _record_phase_failure(self, session_date: date, phase: str, reason: str) -> Dict[str, Any]:
        state = self._load_state()
        date_key = session_date.isoformat()
        for item in state.get("strategies", {}).values():
            item.setdefault("observations", {}).setdefault(date_key, {})[f"{phase}_status"] = "failed"
            item["observations"][date_key][f"{phase}_reason"] = reason
        self._save_state(state)
        payload = {"phase": phase, "session_date": date_key, "status": "failed", "reason": reason}
        self._write_audit(date_key, phase, payload)
        return payload

    def _load_state(self) -> Dict[str, Any]:
        if not self.state_path.exists():
            return {
                "schema_version": 1,
                "target_sessions": TARGET_SESSIONS,
                "initial_cash": INITIAL_CASH,
                "created_at": self.now_fn().isoformat(),
                "strategies": {},
            }
        data = json.loads(self.state_path.read_text(encoding="utf-8"))
        if int(data.get("schema_version") or 0) != 1:
            raise RuntimeError("unsupported_hotspot_campaign_state")
        return data

    def _save_state(self, state: Dict[str, Any]) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        state["updated_at"] = self.now_fn().isoformat()
        temp = self.state_path.with_suffix(".tmp")
        temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, self.state_path)

    def _write_audit(self, session_date: str, phase: str, payload: Dict[str, Any]) -> None:
        self.audit_dir.mkdir(parents=True, exist_ok=True)
        target = self.audit_dir / f"{session_date}.{phase}.json"
        temp = target.with_suffix(target.suffix + ".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, target)
