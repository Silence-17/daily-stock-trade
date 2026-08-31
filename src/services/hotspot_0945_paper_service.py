# -*- coding: utf-8 -*-
"""Pure-forward 09:27 exit-watch and 09:30-09:35 entry paper campaigns.

The service owns two isolated local Portfolio ledgers.  AlphaSift creates the
morning candidate pools, provider-timestamped quotes are refreshed throughout
the entry window, and a second quote snapshot is used for each simulated fill.
No historical replay can advance the campaign counter.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, time as dt_time, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional
from zoneinfo import ZoneInfo

from src.config import get_config
from src.core.trading_calendar import get_market_session_bounds
from src.services.alphasift_service import (
    AlphaSiftService,
    get_dsa_realtime_quote_with_provider_timestamp,
)
from src.services.portfolio_service import PortfolioService
from src.services.cross_market_paper_strategy import is_cn_main_board_symbol

logger = logging.getLogger(__name__)

SHANGHAI = ZoneInfo("Asia/Shanghai")
TARGET_SESSIONS = 30
INITIAL_CASH = 100_000.0
ENTRY_WATCH_START_TIME = dt_time(9, 30)
ENTRY_WATCH_END_TIME = dt_time(9, 35)
ENTRY_WATCH_POLL_SECONDS = 15.0
AUCTION_CHECK_TIME = dt_time(9, 27)
AUCTION_CHECK_DEADLINE = dt_time(9, 29, 30)
CONTINUOUS_OPEN_TIME = dt_time(9, 30)
OPENING_WATCH_END_TIME = dt_time(9, 45)
TRAILING_MONITOR_START_TIME = dt_time(9, 48)
TRAILING_MONITOR_END_TIME = dt_time(14, 54)
MORNING_SESSION_END_TIME = dt_time(11, 30)
AFTERNOON_SESSION_START_TIME = dt_time(13, 0)
CLOSE_SIGNAL_TIME = dt_time(14, 55)
CLOSE_FILL_TIME = dt_time(14, 56)
MAX_QUOTE_AGE_SECONDS = 120
MAX_AUCTION_QUOTE_AGE_SECONDS = 300
AUCTION_GAIN_THRESHOLD_PCT = 5.0
EXIT_WATCH_POLL_SECONDS = 15.0

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
    min_range_position: float
    requires_vwap_reclaim: bool
    early_return_weight: float
    rebound_weight: float
    upstream_weight: float
    resilience_weight: float
    vwap_weight: float
    max_candidates: int
    max_rank: int
    auction_gain_threshold_pct: float
    trailing_stop_pct: float


STRATEGY_SPECS: tuple[StrategySpec, ...] = (
    StrategySpec(
        strategy_id="hotspot_oversold_reversal_0945_v1",
        account_name="30日模拟｜热点超跌反转09:45",
        screen_strategy="oversold_reversal",
        position_pct=0.25,
        max_positions=2,
        min_early_return_pct=1.5,
        min_rebound_pct=1.0,
        min_range_position=0.60,
        requires_vwap_reclaim=False,
        early_return_weight=0.40,
        rebound_weight=0.30,
        upstream_weight=0.20,
        resilience_weight=0.10,
        vwap_weight=0.0,
        max_candidates=6,
        max_rank=2,
        auction_gain_threshold_pct=5.0,
        trailing_stop_pct=2.0,
    ),
    StrategySpec(
        strategy_id="hotspot_trend_reacceleration_0945_v1",
        account_name="30日模拟｜热点趋势再加速09:45",
        screen_strategy="momentum_quality",
        position_pct=0.25,
        max_positions=2,
        min_early_return_pct=2.0,
        min_rebound_pct=0.5,
        min_range_position=0.60,
        requires_vwap_reclaim=True,
        early_return_weight=0.35,
        rebound_weight=0.0,
        upstream_weight=0.25,
        resilience_weight=0.25,
        vwap_weight=0.15,
        max_candidates=6,
        max_rank=2,
        auction_gain_threshold_pct=5.0,
        trailing_stop_pct=3.0,
    ),
)

HOTSPOT_FACTOR_CONFIG_FILENAME = "factor_overrides.json"
HOTSPOT_FACTOR_FIELDS: Dict[str, Dict[str, Any]] = {
    "position_pct": {"label": "单票目标权益比例", "group": "仓位与风控", "type": "number", "min": 0.01, "max": 1.0, "step": 0.01, "unit": "比例"},
    "max_positions": {"label": "最大持仓数", "group": "仓位与风控", "type": "integer", "min": 1, "max": 20, "step": 1, "unit": "只"},
    "min_early_return_pct": {"label": "相对开盘最低涨幅", "group": "入场门槛", "type": "number", "min": -20.0, "max": 20.0, "step": 0.1, "unit": "%"},
    "min_rebound_pct": {"label": "自低点最低反弹", "group": "入场门槛", "type": "number", "min": 0.0, "max": 20.0, "step": 0.1, "unit": "%"},
    "min_range_position": {"label": "早盘区间最低位置", "group": "入场门槛", "type": "number", "min": 0.0, "max": 1.0, "step": 0.01, "unit": "比例"},
    "requires_vwap_reclaim": {"label": "要求重新站上 VWAP", "group": "入场门槛", "type": "boolean", "unit": ""},
    "early_return_weight": {"label": "开盘涨幅排名权重", "group": "综合评分", "type": "number", "min": 0.0, "max": 1.0, "step": 0.05, "unit": "权重"},
    "rebound_weight": {"label": "低点反弹排名权重", "group": "综合评分", "type": "number", "min": 0.0, "max": 1.0, "step": 0.05, "unit": "权重"},
    "upstream_weight": {"label": "AlphaSift 上游分权重", "group": "综合评分", "type": "number", "min": 0.0, "max": 1.0, "step": 0.05, "unit": "权重"},
    "resilience_weight": {"label": "低点抗跌性权重", "group": "综合评分", "type": "number", "min": 0.0, "max": 1.0, "step": 0.05, "unit": "权重"},
    "vwap_weight": {"label": "VWAP 确认权重", "group": "综合评分", "type": "number", "min": 0.0, "max": 1.0, "step": 0.05, "unit": "权重"},
    "max_candidates": {"label": "候选池数量", "group": "候选与排名", "type": "integer", "min": 1, "max": 30, "step": 1, "unit": "只"},
    "max_rank": {"label": "允许下单的最高排名范围", "group": "候选与排名", "type": "integer", "min": 1, "max": 10, "step": 1, "unit": "名"},
    "auction_gain_threshold_pct": {"label": "次日竞价继续观察阈值", "group": "退出规则", "type": "number", "min": -20.0, "max": 20.0, "step": 0.1, "unit": "%"},
    "trailing_stop_pct": {"label": "峰值回撤退出阈值", "group": "退出规则", "type": "number", "min": 0.1, "max": 20.0, "step": 0.1, "unit": "%"},
}


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

    diagnostic_base = {
        "provider": _source_text(raw.get("source")),
        "fetched_at": str(raw.get("fetched_at") or "").strip() or None,
    }
    provider_time = _parse_datetime(raw.get("provider_timestamp"))
    if provider_time is None:
        return {
            "available": False,
            "reason": "provider_timestamp_missing",
            **diagnostic_base,
        }
    age_seconds = (now.astimezone(timezone.utc) - provider_time.astimezone(timezone.utc)).total_seconds()
    if age_seconds < -1 or age_seconds > MAX_QUOTE_AGE_SECONDS:
        return {
            "available": False,
            "reason": "quote_stale_or_future",
            "age_seconds": round(age_seconds, 3),
            **diagnostic_base,
        }

    price = _safe_float(raw.get("price"))
    open_price = _safe_float(raw.get("open_price") or raw.get("open"))
    high = _safe_float(raw.get("high"))
    low = _safe_float(raw.get("low"))
    volume = _safe_float(raw.get("volume"))
    amount = _safe_float(raw.get("amount"))
    pre_close = _safe_float(raw.get("pre_close") or raw.get("prev_close"))
    limit_up_price = _safe_float(raw.get("limit_up_price"))
    if not all(value is not None and value > 0 for value in (price, open_price, high, low)):
        return {
            "available": False,
            "reason": "quote_price_fields_missing",
            **diagnostic_base,
        }
    if high < low or not (low <= price <= high * 1.001):
        return {
            "available": False,
            "reason": "quote_price_range_invalid",
            **diagnostic_base,
        }

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
        "pre_close": pre_close,
        "limit_up_price": limit_up_price,
        "vwap": vwap,
        "early_return_pct": early_return_pct,
        "rebound_pct": rebound_pct,
        "resilience_pct": resilience_pct,
        "range_position": range_position,
        "provider": _source_text(raw.get("source")),
        "provider_timestamp": provider_time.isoformat(),
        "age_seconds": round(age_seconds, 3),
    }


def normalize_auction_quote(raw: Mapping[str, Any], *, now: datetime) -> Dict[str, Any]:
    """Validate a 09:25 call-auction result observed during the 09:25-09:30 pause."""

    diagnostic_base = {
        "provider": _source_text(raw.get("source")),
        "fetched_at": str(raw.get("fetched_at") or "").strip() or None,
    }
    provider_time = _parse_datetime(raw.get("provider_timestamp"))
    if provider_time is None:
        return {
            "available": False,
            "reason": "provider_timestamp_missing",
            **diagnostic_base,
        }
    age_seconds = (now.astimezone(timezone.utc) - provider_time.astimezone(timezone.utc)).total_seconds()
    if age_seconds < -1 or age_seconds > MAX_AUCTION_QUOTE_AGE_SECONDS:
        return {
            "available": False,
            "reason": "auction_quote_stale_or_future",
            "age_seconds": round(age_seconds, 3),
            **diagnostic_base,
        }

    price = _safe_float(raw.get("price"))
    pre_close = _safe_float(raw.get("pre_close") or raw.get("prev_close"))
    limit_up_price = _safe_float(raw.get("limit_up_price"))
    if price is None or price <= 0 or pre_close is None or pre_close <= 0:
        return {
            "available": False,
            "reason": "auction_price_or_pre_close_missing",
            **diagnostic_base,
        }
    return {
        "available": True,
        "code": str(raw.get("code") or "").strip(),
        "name": str(raw.get("name") or "").strip(),
        "price": price,
        "pre_close": pre_close,
        "limit_up_price": limit_up_price,
        "auction_gain_pct": (price / pre_close - 1.0) * 100.0,
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
    """Rank candidates using the frozen pool and one entry-window quote."""

    rows: List[Dict[str, Any]] = []
    for candidate in candidates:
        code = str(candidate.get("code") or candidate.get("symbol") or "").strip()
        quote = dict(quotes.get(code) or {})
        if not code or not is_cn_main_board_symbol(code) or not quote.get("available"):
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
        vwap = _safe_float(quote.get("vwap"))
        vwap_pass = (
            not spec.requires_vwap_reclaim
            or (vwap is not None and float(quote["price"]) >= vwap)
        )
        weight_total = sum(
            (
                spec.early_return_weight,
                spec.rebound_weight,
                spec.upstream_weight,
                spec.resilience_weight,
                spec.vwap_weight,
            )
        )
        if weight_total <= 0:
            raise ValueError(f"hotspot_factor_weight_total_invalid:{spec.strategy_id}")
        composite = (
            spec.early_return_weight * early_scores[index]
            + spec.rebound_weight * rebound_scores[index]
            + spec.upstream_weight * upstream
            + spec.resilience_weight * resilience_scores[index]
            + spec.vwap_weight * (1.0 if vwap_pass else 0.0)
        ) / weight_total
        blockers: List[str] = []
        if float(quote["early_return_pct"]) < spec.min_early_return_pct:
            blockers.append("early_return_below_threshold")
        if float(quote["rebound_pct"]) < spec.min_rebound_pct:
            blockers.append("rebound_below_threshold")
        if float(quote["range_position"]) < spec.min_range_position:
            blockers.append("range_position_too_low")
        if not vwap_pass:
            blockers.append("vwap_reclaim_unconfirmed")
        row["score"] = round(composite * 100.0, 6)
        row["blockers"] = blockers
        row["eligible"] = not blockers

    rows.sort(key=lambda item: (-float(item["score"]), item["code"]))
    for rank, row in enumerate(rows, start=1):
        row["rank"] = rank
        if rank > spec.max_rank:
            row["blockers"].append("outside_combined_top_two")
            row["eligible"] = False
    return rows


class Hotspot0945PaperService:
    """Manage setup, auction exits, morning entries, valuation, and 30-day state."""

    def __init__(
        self,
        *,
        portfolio: Optional[PortfolioService] = None,
        alphasift: Optional[AlphaSiftService] = None,
        state_dir: Optional[Path] = None,
        now_fn: Any = None,
        sleep_fn: Any = None,
        quote_getter: Any = None,
    ):
        self.portfolio = portfolio or PortfolioService()
        self.alphasift = alphasift or AlphaSiftService(get_config())
        configured = os.getenv(STATE_DIR_ENV, "").strip()
        self.state_dir = Path(state_dir or configured or DEFAULT_STATE_DIR)
        self.state_path = self.state_dir / "state.json"
        self.audit_dir = self.state_dir / "audits"
        self.factor_config_path = self.state_dir / HOTSPOT_FACTOR_CONFIG_FILENAME
        self.now_fn = now_fn or (lambda: datetime.now(SHANGHAI))
        self.sleep_fn = sleep_fn or time.sleep
        self.quote_getter = quote_getter or get_dsa_realtime_quote_with_provider_timestamp

    def _load_factor_payload(self) -> Dict[str, Any]:
        if not self.factor_config_path.exists():
            return {"schema_version": 1, "strategies": {}}
        try:
            payload = json.loads(self.factor_config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("hotspot_factor_config_unavailable") from exc
        if not isinstance(payload, dict) or int(payload.get("schema_version") or 0) != 1:
            raise RuntimeError("unsupported_hotspot_factor_config")
        strategies = payload.get("strategies")
        if not isinstance(strategies, dict):
            payload["strategies"] = {}
        return payload

    @staticmethod
    def _validate_factor_overrides(
        spec: StrategySpec,
        overrides: Mapping[str, Any],
    ) -> Dict[str, Any]:
        normalized: Dict[str, Any] = {}
        for raw_key, raw_value in dict(overrides).items():
            key = str(raw_key).strip()
            metadata = HOTSPOT_FACTOR_FIELDS.get(key)
            if metadata is None:
                raise ValueError(f"unsupported_hotspot_factor:{key}")
            factor_type = metadata["type"]
            if factor_type == "boolean":
                if not isinstance(raw_value, bool):
                    raise ValueError(f"hotspot_factor_must_be_boolean:{key}")
                normalized[key] = raw_value
                continue
            number = _safe_float(raw_value)
            if number is None:
                raise ValueError(f"invalid_hotspot_factor:{key}")
            if number < float(metadata["min"]) or number > float(metadata["max"]):
                raise ValueError(f"hotspot_factor_out_of_range:{key}")
            if factor_type == "integer":
                if not number.is_integer():
                    raise ValueError(f"hotspot_factor_must_be_integer:{key}")
                normalized[key] = int(number)
            else:
                normalized[key] = float(number)
        effective = replace(spec, **normalized)
        weight_total = sum(
            (
                effective.early_return_weight,
                effective.rebound_weight,
                effective.upstream_weight,
                effective.resilience_weight,
                effective.vwap_weight,
            )
        )
        if weight_total <= 0:
            raise ValueError("hotspot_factor_weight_total_invalid")
        if effective.max_rank > effective.max_candidates:
            raise ValueError("hotspot_max_rank_exceeds_candidates")
        return normalized

    def strategy_specs(self) -> tuple[StrategySpec, ...]:
        payload = self._load_factor_payload()
        configured = payload.get("strategies") if isinstance(payload, dict) else {}
        result: List[StrategySpec] = []
        for spec in STRATEGY_SPECS:
            raw = configured.get(spec.strategy_id, {}) if isinstance(configured, dict) else {}
            overrides = self._validate_factor_overrides(spec, raw if isinstance(raw, dict) else {})
            result.append(replace(spec, **overrides))
        return tuple(result)

    def get_factor_catalog(self, strategy_id: str) -> Dict[str, Any]:
        defaults = next((item for item in STRATEGY_SPECS if item.strategy_id == strategy_id), None)
        if defaults is None:
            raise ValueError("hotspot_strategy_not_found")
        payload = self._load_factor_payload()
        configured = payload.get("strategies") if isinstance(payload, dict) else {}
        raw = configured.get(strategy_id, {}) if isinstance(configured, dict) else {}
        overrides = self._validate_factor_overrides(defaults, raw if isinstance(raw, dict) else {})
        effective = replace(defaults, **overrides)
        default_values = asdict(defaults)
        effective_values = asdict(effective)
        items: List[Dict[str, Any]] = []
        for key, metadata in HOTSPOT_FACTOR_FIELDS.items():
            items.append(
                {
                    "key": key,
                    "label": metadata["label"],
                    "description": f"{defaults.account_name} 的运行参数 `{key}`，保存后用于后续策略判断。",
                    "group": str(metadata["group"]),
                    "group_label": str(metadata["group"]),
                    "value": effective_values[key],
                    "default_value": default_values[key],
                    "overridden": key in overrides,
                    "editable": True,
                    "value_type": metadata["type"],
                    "unit": metadata.get("unit") or "",
                    "min": metadata.get("min"),
                    "max": metadata.get("max"),
                    "step": metadata.get("step"),
                }
            )
        return {"strategy_id": strategy_id, "items": items, "overrides": overrides}

    def update_factor_overrides(
        self,
        strategy_id: str,
        overrides: Mapping[str, Any],
        *,
        replace_existing: bool = True,
    ) -> Dict[str, Any]:
        defaults = next((item for item in STRATEGY_SPECS if item.strategy_id == strategy_id), None)
        if defaults is None:
            raise ValueError("hotspot_strategy_not_found")
        payload = self._load_factor_payload()
        strategies = payload.get("strategies")
        if not isinstance(strategies, dict):
            strategies = {}
            payload["strategies"] = strategies
        existing = strategies.get(strategy_id, {})
        merged = (
            dict(overrides)
            if replace_existing
            else {**dict(existing or {}), **dict(overrides)}
        )
        normalized = self._validate_factor_overrides(defaults, merged)
        strategies[strategy_id] = normalized
        payload["updated_at"] = self.now_fn().isoformat()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        temp = self.factor_config_path.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, self.factor_config_path)
        return self.get_factor_catalog(strategy_id)

    def setup(self) -> Dict[str, Any]:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.audit_dir.mkdir(parents=True, exist_ok=True)
        state = self._load_state()
        accounts = self.portfolio.list_accounts(include_inactive=True)
        created: List[int] = []
        for spec in self.strategy_specs():
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
            if selected_phase == "watch-exit":
                result = self.run_exit_watch()
            elif selected_phase == "open":
                result = self.run_open()
            elif selected_phase == "close":
                result = self.run_close()
            elif selected_phase == "status":
                result = self.status()
            else:
                raise ValueError("phase must be auto, watch-exit, open, close, or status")
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

    def run_exit_watch(self) -> Dict[str, Any]:
        """Apply the 09:27 auction decision and monitor qualified holdings forward-only."""

        now = self.now_fn()
        self._require_cn_trading_session(now)
        if now.timetz().replace(tzinfo=None) > AUCTION_CHECK_DEADLINE:
            return self._record_phase_failure(now.date(), "exit-watch", "auction_watch_window_missed")
        self._wait_until(AUCTION_CHECK_TIME)
        auction_now = self.now_fn()
        if auction_now.timetz().replace(tzinfo=None) > AUCTION_CHECK_DEADLINE:
            return self._record_phase_failure(now.date(), "exit-watch", "auction_watch_window_missed_after_wait")

        state = self._load_state()
        specs = self.strategy_specs()
        position_codes = {
            str(code)
            for item in state.get("strategies", {}).values()
            for code in (item.get("position_meta") or {}).keys()
        }
        auction_quotes = self._fetch_quotes(position_codes, now=auction_now, auction=True)
        sell_intents, watching, auction_evidence = self._classify_auction_positions(
            state,
            auction_quotes,
            auction_now.date(),
        )
        date_key = auction_now.date().isoformat()
        audit: Dict[str, Any] = {
            "phase": "exit-watch",
            "session_date": date_key,
            "status": "running",
            "started_at": auction_now.isoformat(),
            "auction_threshold_pct": {
                spec.strategy_id: spec.auction_gain_threshold_pct for spec in specs
            },
            "auction_quote_diagnostics": self._compact_quote_diagnostics(auction_quotes),
            "auction_decisions": auction_evidence,
            "opening_watch": watching,
            "trades": [],
            "evidence_failures": [
                {
                    "strategy_id": strategy_id,
                    "code": code,
                    "reason": item.get("reason") or "auction_evidence_missing",
                }
                for strategy_id, rows in auction_evidence.items()
                for code, item in rows.items()
                if item.get("status") == "evidence_missing"
            ],
        }
        self._write_audit(date_key, "exit-watch", audit)

        if sell_intents:
            self._wait_until(CONTINUOUS_OPEN_TIME)
            fill_now = self.now_fn()
            fill_quotes = self._fetch_quotes({item["code"] for item in sell_intents}, now=fill_now)
            for intent in sell_intents:
                trade = self._execute_sell(intent, fill_quotes, fill_now.date(), state)
                if trade:
                    audit["trades"].append(trade)
                else:
                    audit["evidence_failures"].append(
                        {
                            "strategy_id": intent["strategy_id"],
                            "code": intent["code"],
                            "reason": "auction_sell_fill_unavailable",
                        }
                    )
            audit["auction_fill_quote_diagnostics"] = self._compact_quote_diagnostics(fill_quotes)
            self._save_state(state)
            self._write_audit(date_key, "exit-watch", audit)

        if watching:
            self._wait_until(CONTINUOUS_OPEN_TIME)
            while self.now_fn().timetz().replace(tzinfo=None) < OPENING_WATCH_END_TIME:
                sample_now = self.now_fn()
                active_codes = {
                    str(item["code"])
                    for item in watching.values()
                    if not item.get("limit_up_seen")
                }
                if not active_codes:
                    break
                sample_quotes = self._fetch_quotes(active_codes, now=sample_now)
                self._update_opening_watch(watching, sample_quotes, self.now_fn())
                remaining = (
                    datetime.combine(sample_now.date(), OPENING_WATCH_END_TIME, tzinfo=SHANGHAI)
                    - self.now_fn()
                ).total_seconds()
                if remaining <= 0:
                    break
                self.sleep_fn(min(EXIT_WATCH_POLL_SECONDS, remaining))

            remaining_codes = {
                str(item["code"])
                for item in watching.values()
                if not item.get("limit_up_seen")
            }
            if remaining_codes:
                self._wait_until(OPENING_WATCH_END_TIME)
                final_watch_quotes = self._fetch_quotes(remaining_codes, now=self.now_fn())
                self._update_opening_watch(watching, final_watch_quotes, self.now_fn())
                audit["opening_watch_final_quote_diagnostics"] = self._compact_quote_diagnostics(final_watch_quotes)
            audit["opening_watch"] = watching
            audit["opening_watch_completed_at"] = self.now_fn().isoformat()
            self._write_audit(date_key, "exit-watch", audit)

        if watching:
            # The 09:45 entry process writes the same state file through 09:46.  Reload
            # after it has finished so the long-running watcher cannot erase new buys.
            self._wait_until(TRAILING_MONITOR_START_TIME)
            state = self._load_state()
            watch_results = self._apply_opening_watch_state(state, watching, auction_now.date())
            audit["opening_watch_results"] = watch_results
            audit["evidence_failures"].extend(
                {
                    "strategy_id": watching[key]["strategy_id"],
                    "code": watching[key]["code"],
                    "reason": result.get("reason") or "opening_watch_evidence_missing",
                }
                for key, result in watch_results.items()
                if result.get("status") == "held_fail_closed"
            )
        else:
            watch_results = {}

        armed_keys = {
            key
            for key, result in watch_results.items()
            if result.get("status") == "trailing_armed"
        }
        for spec in specs:
            observation = state["strategies"][spec.strategy_id]["observations"].setdefault(date_key, {})
            observation["exit_watch_status"] = "running" if armed_keys else "completed"
            observation["exit_watch_updated_at"] = self.now_fn().isoformat()
        self._save_state(state)

        if not armed_keys:
            final_status = "failed" if audit["evidence_failures"] else "completed"
            audit["status"] = final_status
            audit["completed_at"] = self.now_fn().isoformat()
            for spec in specs:
                state["strategies"][spec.strategy_id]["observations"][date_key]["exit_watch_status"] = final_status
            self._save_state(state)
            self._write_audit(date_key, "exit-watch", audit)
            return audit

        audit["trailing_monitor"] = {
            "status": "running",
            "started_at": self.now_fn().isoformat(),
            "poll_seconds": EXIT_WATCH_POLL_SECONDS,
            "sample_count": 0,
            "unavailable_quote_count": 0,
            "positions": {
                key: {"available_samples": 0, "unavailable_samples": 0}
                for key in armed_keys
            },
        }
        self._write_audit(date_key, "exit-watch", audit)
        last_state_save = self.now_fn()
        last_audit_save = self.now_fn()

        while self.now_fn().timetz().replace(tzinfo=None) <= TRAILING_MONITOR_END_TIME:
            sample_now = self.now_fn()
            local_time = sample_now.timetz().replace(tzinfo=None)
            if MORNING_SESSION_END_TIME < local_time < AFTERNOON_SESSION_START_TIME:
                self._wait_until(AFTERNOON_SESSION_START_TIME)
                continue
            active_pairs = {
                key: item
                for key, item in watching.items()
                if key in armed_keys
                and str(item["code"])
                in (state["strategies"][str(item["strategy_id"])].get("position_meta") or {})
            }
            if not active_pairs:
                break
            active_codes = {str(item["code"]) for item in active_pairs.values()}
            quotes = self._fetch_quotes(active_codes, now=sample_now)
            monitor_stats = audit["trailing_monitor"]
            monitor_stats["sample_count"] = int(monitor_stats["sample_count"]) + 1
            monitor_stats["last_sample_at"] = self.now_fn().isoformat()
            monitor_stats["unavailable_quote_count"] = int(monitor_stats["unavailable_quote_count"]) + sum(
                1 for quote in quotes.values() if not quote.get("available")
            )
            for key, item in active_pairs.items():
                position_stats = monitor_stats["positions"][key]
                if (quotes.get(str(item["code"])) or {}).get("available"):
                    position_stats["available_samples"] = int(position_stats["available_samples"]) + 1
                else:
                    position_stats["unavailable_samples"] = int(position_stats["unavailable_samples"]) + 1
            intents: List[Dict[str, Any]] = []
            for spec in specs:
                intents.extend(
                    self._build_trailing_exit_intents(
                        spec,
                        state["strategies"][spec.strategy_id],
                        quotes,
                        sample_now.date(),
                        check_phase="watch-exit",
                    )
                )
            new_trade = False
            for intent in intents:
                trade = self._execute_sell(intent, quotes, sample_now.date(), state)
                if trade:
                    audit["trades"].append(trade)
                    armed_keys.discard(f"{intent['strategy_id']}:{intent['code']}")
                    new_trade = True
            current_now = self.now_fn()
            if new_trade or (current_now - last_state_save).total_seconds() >= 60:
                self._save_state(state)
                last_state_save = current_now
            if new_trade or (current_now - last_audit_save).total_seconds() >= 300:
                self._write_audit(date_key, "exit-watch", audit)
                last_audit_save = current_now
            if not armed_keys:
                break
            remaining = (
                datetime.combine(sample_now.date(), TRAILING_MONITOR_END_TIME, tzinfo=SHANGHAI)
                - self.now_fn()
            ).total_seconds()
            if remaining <= 0:
                break
            self.sleep_fn(min(EXIT_WATCH_POLL_SECONDS, remaining))

        audit["evidence_failures"].extend(
            {
                "strategy_id": watching[key]["strategy_id"],
                "code": watching[key]["code"],
                "reason": "trailing_monitor_quotes_unavailable",
            }
            for key, stats in audit["trailing_monitor"]["positions"].items()
            if int(stats.get("available_samples") or 0) == 0
        )
        final_status = "failed" if audit["evidence_failures"] else "completed"
        for spec in specs:
            observation = state["strategies"][spec.strategy_id]["observations"].setdefault(date_key, {})
            observation["exit_watch_status"] = final_status
            observation["exit_watch_updated_at"] = self.now_fn().isoformat()
        self._save_state(state)
        audit["status"] = final_status
        audit["completed_at"] = self.now_fn().isoformat()
        audit["trailing_monitor"]["status"] = final_status
        self._write_audit(date_key, "exit-watch", audit)
        return audit

    def run_open(self) -> Dict[str, Any]:
        now = self.now_fn()
        self._require_cn_trading_session(now)
        if now.astimezone(SHANGHAI).timetz().replace(tzinfo=None) >= ENTRY_WATCH_END_TIME:
            return self._record_phase_failure(now.date(), "open", "entry_watch_window_missed")

        screens: Dict[str, Dict[str, Any]] = {}
        screen_errors: Dict[str, str] = {}
        specs = self.strategy_specs()
        for spec in specs:
            try:
                screens[spec.strategy_id] = self.alphasift.screen(
                    strategy=spec.screen_strategy,
                    market="cn",
                    max_results=spec.max_candidates,
                    use_llm=False,
                )
            except Exception as exc:  # noqa: BLE001 - fail closed and persist the reason.
                screen_errors[spec.strategy_id] = f"{type(exc).__name__}: {exc}"

        self._wait_until(ENTRY_WATCH_START_TIME)
        watch_started_at = self.now_fn()
        if watch_started_at.astimezone(SHANGHAI).timetz().replace(tzinfo=None) >= ENTRY_WATCH_END_TIME:
            return self._record_phase_failure(
                now.date(),
                "open",
                "entry_watch_window_missed_after_screen",
            )

        state = self._load_state()
        candidate_codes: set[str] = set()
        for payload in screens.values():
            for candidate in payload.get("candidates") or []:
                code = str(candidate.get("code") or candidate.get("symbol") or "").strip()
                if code:
                    candidate_codes.add(code)

        decisions: Dict[str, List[Dict[str, Any]]] = {
            spec.strategy_id: [] for spec in specs
        }
        valid_quote_codes: Dict[str, set[str]] = {
            spec.strategy_id: set() for spec in specs
        }
        resolved_strategy_ids = {
            spec.strategy_id
            for spec in specs
            if (
                state["strategies"][spec.strategy_id].get("status") == "completed"
                or spec.strategy_id in screen_errors
                or len(
                    state["strategies"][spec.strategy_id].get("position_meta") or {}
                ) >= spec.max_positions
            )
        }
        latest_quotes: Dict[str, Dict[str, Any]] = {}
        fill_quote_diagnostics: Dict[str, Any] = {}
        watch_samples: List[Dict[str, Any]] = []
        trades: List[Dict[str, Any]] = []
        max_samples = max(
            1,
            int(
                (
                    datetime.combine(now.date(), ENTRY_WATCH_END_TIME, tzinfo=SHANGHAI)
                    - datetime.combine(now.date(), ENTRY_WATCH_START_TIME, tzinfo=SHANGHAI)
                ).total_seconds()
                / ENTRY_WATCH_POLL_SECONDS
            ),
        )
        while candidate_codes and len(watch_samples) < max_samples:
            sample_now = self.now_fn()
            if sample_now.astimezone(SHANGHAI).timetz().replace(tzinfo=None) >= ENTRY_WATCH_END_TIME:
                break
            latest_quotes = self._fetch_quotes(candidate_codes, now=sample_now)
            eligible_by_strategy: Dict[str, Optional[str]] = {}
            for spec in specs:
                strategy_state = state["strategies"][spec.strategy_id]
                if spec.strategy_id in screen_errors or strategy_state.get("status") == "completed":
                    decisions[spec.strategy_id] = []
                    eligible_by_strategy[spec.strategy_id] = None
                    continue
                candidate_rows = screens.get(spec.strategy_id, {}).get("candidates") or []
                ranked = evaluate_open_candidates(spec, candidate_rows, latest_quotes)
                decisions[spec.strategy_id] = ranked
                valid_quote_codes[spec.strategy_id].update(
                    row["code"]
                    for row in ranked
                    if row.get("quote", {}).get("available")
                )
                held = set((strategy_state.get("position_meta") or {}).keys())
                eligible = next(
                    (
                        row
                        for row in ranked
                        if row.get("eligible") and row["code"] not in held
                    ),
                    None,
                )
                eligible_by_strategy[spec.strategy_id] = (
                    str(eligible["code"]) if eligible is not None else None
                )
                if spec.strategy_id in resolved_strategy_ids or eligible is None:
                    continue

                fill_requested_at = self.now_fn()
                if not self._entry_watch_is_open(fill_requested_at):
                    continue
                fill_quotes = self._fetch_quotes(
                    [str(eligible["code"])],
                    now=fill_requested_at,
                )
                fill_completed_at = self.now_fn()
                fill_quote_diagnostics[spec.strategy_id] = {
                    "requested_at": fill_requested_at.isoformat(),
                    "completed_at": fill_completed_at.isoformat(),
                    "quotes": self._compact_quote_diagnostics(fill_quotes),
                }
                if not self._entry_watch_is_open(fill_completed_at):
                    continue
                trade = self._execute_buy(
                    spec,
                    eligible,
                    fill_quotes,
                    fill_completed_at.date(),
                    state,
                )
                if trade:
                    trade["signal_observed_at"] = sample_now.isoformat()
                    trade["fill_observed_at"] = fill_completed_at.isoformat()
                    trades.append(trade)
                    resolved_strategy_ids.add(spec.strategy_id)

            watch_samples.append(
                {
                    "observed_at": sample_now.isoformat(),
                    "available_quote_count": sum(
                        1 for quote in latest_quotes.values() if quote.get("available")
                    ),
                    "candidate_quote_count": len(latest_quotes),
                    "eligible_by_strategy": eligible_by_strategy,
                }
            )
            if len(resolved_strategy_ids) == len(specs):
                break
            remaining = (
                datetime.combine(sample_now.date(), ENTRY_WATCH_END_TIME, tzinfo=SHANGHAI)
                - self.now_fn()
            ).total_seconds()
            if remaining <= 0:
                break
            self.sleep_fn(min(ENTRY_WATCH_POLL_SECONDS, remaining))

        completed_at = self.now_fn()
        date_key = now.date().isoformat()
        for spec in specs:
            strategy_state = state["strategies"][spec.strategy_id]
            observation = strategy_state["observations"].setdefault(date_key, {})
            error = screen_errors.get(spec.strategy_id)
            valid_quote_count = len(valid_quote_codes[spec.strategy_id])
            candidate_count = len(screens.get(spec.strategy_id, {}).get("candidates") or [])
            completed = (
                error is None
                and candidate_count > 0
                and valid_quote_count >= min(2, candidate_count)
            )
            observation.update(
                {
                    "open_status": "completed" if completed else "failed",
                    "open_completed_at": completed_at.isoformat(),
                    "entry_watch_start": ENTRY_WATCH_START_TIME.strftime("%H:%M:%S"),
                    "entry_watch_end": ENTRY_WATCH_END_TIME.strftime("%H:%M:%S"),
                    "entry_watch_sample_count": len(watch_samples),
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
            "completed_at": completed_at.isoformat(),
            "entry_watch": {
                "start_time": ENTRY_WATCH_START_TIME.strftime("%H:%M:%S"),
                "end_time": ENTRY_WATCH_END_TIME.strftime("%H:%M:%S"),
                "poll_seconds": ENTRY_WATCH_POLL_SECONDS,
                "started_at": watch_started_at.isoformat(),
                "sample_count": len(watch_samples),
                "samples": watch_samples,
            },
            "screen_errors": screen_errors,
            "screen_summary": {
                spec.strategy_id: {
                    "screen_strategy": spec.screen_strategy,
                    "candidate_count": len(screens.get(spec.strategy_id, {}).get("candidates") or []),
                    "quality_status": screens.get(spec.strategy_id, {}).get("quality_status"),
                    "fallback_used": screens.get(spec.strategy_id, {}).get("fallback_used"),
                    "stale": screens.get(spec.strategy_id, {}).get("stale"),
                }
                for spec in specs
            },
            "signal_quote_diagnostics": self._quote_diagnostics_by_strategy(
                screens,
                latest_quotes,
            ),
            "fill_quote_diagnostics": fill_quote_diagnostics,
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
        specs = self.strategy_specs()
        position_codes = {
            code
            for item in state.get("strategies", {}).values()
            for code in (item.get("position_meta") or {}).keys()
        }
        quotes = self._fetch_quotes(position_codes, now=signal_now)
        sell_intents: List[Dict[str, Any]] = []
        for spec in specs:
            strategy_state = state["strategies"][spec.strategy_id]
            sell_intents.extend(
                self._build_trailing_exit_intents(
                    spec,
                    strategy_state,
                    quotes,
                    signal_now.date(),
                    check_phase="close",
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
        for spec in specs:
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
            "signal_quote_diagnostics": self._compact_quote_diagnostics(quotes),
            "fill_quote_diagnostics": self._compact_quote_diagnostics(fill_quotes),
            "trades": trades,
            "results": results,
        }
        self._write_audit(date_key, "close", audit)
        return audit

    def _fetch_quotes(
        self,
        codes: Iterable[str],
        *,
        now: datetime,
        auction: bool = False,
    ) -> Dict[str, Dict[str, Any]]:
        unique = sorted({str(code).strip() for code in codes if str(code).strip()})
        if not unique:
            return {}
        result: Dict[str, Dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=min(4, len(unique))) as executor:
            futures = {executor.submit(self.quote_getter, code): code for code in unique}
            for future in as_completed(futures):
                code = futures[future]
                try:
                    raw = future.result()
                    validation_now = self.now_fn()
                    normalized = (
                        (
                            normalize_auction_quote(raw, now=validation_now)
                            if auction
                            else normalize_quote(raw, now=validation_now)
                        )
                        if raw
                        else {
                            "available": False,
                            "reason": "timestamped_quote_unavailable",
                        }
                    )
                except Exception as exc:  # noqa: BLE001
                    normalized = {"available": False, "reason": f"quote_fetch_failed:{type(exc).__name__}"}
                normalized.setdefault("code", code)
                result[code] = normalized
        return result

    @staticmethod
    def _compact_quote_diagnostics(quotes: Mapping[str, Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
        fields = (
            "available",
            "reason",
            "provider",
            "provider_timestamp",
            "fetched_at",
            "age_seconds",
            "price",
            "pre_close",
            "limit_up_price",
            "auction_gain_pct",
        )
        return {
            str(code): {
                field: quote.get(field)
                for field in fields
                if quote.get(field) is not None
            }
            for code, quote in quotes.items()
        }

    def _quote_diagnostics_by_strategy(
        self,
        screens: Mapping[str, Mapping[str, Any]],
        quotes: Mapping[str, Mapping[str, Any]],
    ) -> Dict[str, Dict[str, Dict[str, Any]]]:
        compact = self._compact_quote_diagnostics(quotes)
        result: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for spec in self.strategy_specs():
            rows: Dict[str, Dict[str, Any]] = {}
            for candidate in screens.get(spec.strategy_id, {}).get("candidates") or []:
                code = str(candidate.get("code") or candidate.get("symbol") or "").strip()
                if code:
                    rows[code] = compact.get(
                        code,
                        {"available": False, "reason": "quote_not_requested"},
                    )
            result[spec.strategy_id] = rows
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

    def _classify_auction_positions(
        self,
        state: Mapping[str, Any],
        quotes: Mapping[str, Mapping[str, Any]],
        session_date: date,
    ) -> tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
        sell_intents: List[Dict[str, Any]] = []
        watching: Dict[str, Dict[str, Any]] = {}
        evidence: Dict[str, Dict[str, Any]] = {}
        for spec in self.strategy_specs():
            strategy_state = state.get("strategies", {}).get(spec.strategy_id, {})
            strategy_evidence: Dict[str, Any] = {}
            evidence[spec.strategy_id] = strategy_evidence
            for code, meta in (strategy_state.get("position_meta") or {}).items():
                entry_date = date.fromisoformat(str(meta["entry_date"]))
                if entry_date >= session_date:
                    strategy_evidence[code] = {"status": "held_by_t_plus_one"}
                    continue
                quote = dict(quotes.get(code) or {})
                if not quote.get("available"):
                    strategy_evidence[code] = {
                        "status": "evidence_missing",
                        "reason": quote.get("reason") or "auction_quote_unavailable",
                    }
                    continue
                auction_gain_pct = float(quote["auction_gain_pct"])
                entry_price = float(meta["entry_price"])
                return_pct = (float(quote["price"]) / entry_price - 1.0) * 100.0
                common = {
                    "strategy_id": spec.strategy_id,
                    "account_id": int(strategy_state["account_id"]),
                    "code": str(code),
                    "auction_gain_pct": round(auction_gain_pct, 6),
                    "auction_price": float(quote["price"]),
                    "return_pct": round(return_pct, 6),
                    "trailing_stop_pct": spec.trailing_stop_pct,
                }
                if auction_gain_pct < spec.auction_gain_threshold_pct:
                    sell_intents.append(
                        {
                            **common,
                            "reason": "auction_gain_below_5pct",
                            "check_phase": "watch-exit",
                        }
                    )
                    strategy_evidence[code] = {
                        "status": "sell_at_continuous_open",
                        **common,
                    }
                else:
                    key = f"{spec.strategy_id}:{code}"
                    watching[key] = {
                        **common,
                        "peak_price": float(quote["price"]),
                        "last_price": float(quote["price"]),
                        "available_samples": 0,
                        "unavailable_samples": 0,
                        "limit_up_price_observed": False,
                        "limit_up_seen": False,
                    }
                    strategy_evidence[code] = {
                        "status": "watching_for_limit_up",
                        **common,
                    }
        return sell_intents, watching, evidence

    @staticmethod
    def _update_opening_watch(
        watching: Dict[str, Dict[str, Any]],
        quotes: Mapping[str, Mapping[str, Any]],
        observed_at: datetime,
    ) -> None:
        for item in watching.values():
            if item.get("limit_up_seen"):
                continue
            quote = dict(quotes.get(str(item["code"])) or {})
            if not quote.get("available"):
                item["unavailable_samples"] = int(item.get("unavailable_samples") or 0) + 1
                item["last_unavailable_reason"] = quote.get("reason") or "quote_unavailable"
                continue
            price = float(quote["price"])
            high = float(quote.get("high") or price)
            item["available_samples"] = int(item.get("available_samples") or 0) + 1
            item.setdefault("first_sample_at", observed_at.isoformat())
            item["last_sample_at"] = observed_at.isoformat()
            item["last_price"] = price
            item["peak_price"] = max(float(item.get("peak_price") or price), high, price)
            limit_up_price = _safe_float(quote.get("limit_up_price"))
            if limit_up_price is None or limit_up_price <= 0:
                continue
            item["limit_up_price_observed"] = True
            item["limit_up_price"] = limit_up_price
            if high >= limit_up_price - 0.005:
                item["limit_up_seen"] = True
                item["limit_up_seen_at"] = observed_at.isoformat()
                item["limit_up_seen_price"] = max(high, price)

    @staticmethod
    def _apply_opening_watch_state(
        state: Dict[str, Any],
        watching: Mapping[str, Mapping[str, Any]],
        session_date: date,
    ) -> Dict[str, Dict[str, Any]]:
        results: Dict[str, Dict[str, Any]] = {}
        date_key = session_date.isoformat()
        legacy_fields = (
            "breakeven_armed",
            "weakness_confirmation_count",
            "last_exit_check",
        )
        for key, item in watching.items():
            strategy_state = state["strategies"][str(item["strategy_id"])]
            meta = (strategy_state.get("position_meta") or {}).get(str(item["code"]))
            if meta is None:
                results[key] = {"status": "position_no_longer_held"}
                continue
            for field in legacy_fields:
                meta.pop(field, None)
            meta["exit_watch_date"] = date_key
            meta["auction_gain_pct"] = item.get("auction_gain_pct")
            meta["limit_up_seen"] = bool(item.get("limit_up_seen"))
            meta["limit_up_seen_at"] = item.get("limit_up_seen_at")
            meta["trailing_stop_pct"] = item.get("trailing_stop_pct")
            if item.get("limit_up_seen"):
                meta["trailing_armed"] = False
                meta.pop("trailing_peak_price", None)
                results[key] = {"status": "held_after_limit_up"}
            elif item.get("limit_up_price_observed"):
                meta["trailing_armed"] = True
                meta["trailing_peak_price"] = float(item["peak_price"])
                results[key] = {
                    "status": "trailing_armed",
                    "peak_price": float(item["peak_price"]),
                    "trailing_stop_pct": item.get("trailing_stop_pct"),
                }
            else:
                meta["trailing_armed"] = False
                meta.pop("trailing_peak_price", None)
                results[key] = {
                    "status": "held_fail_closed",
                    "reason": "limit_up_price_evidence_missing",
                }
        return results

    def _build_trailing_exit_intents(
        self,
        spec: StrategySpec,
        strategy_state: Dict[str, Any],
        quotes: Mapping[str, Mapping[str, Any]],
        session_date: date,
        *,
        check_phase: str,
        force: bool = False,
    ) -> List[Dict[str, Any]]:
        intents: List[Dict[str, Any]] = []
        for code, meta in list((strategy_state.get("position_meta") or {}).items()):
            quote = dict(quotes.get(code) or {})
            if not quote.get("available"):
                continue
            entry_date = date.fromisoformat(str(meta["entry_date"]))
            if entry_date >= session_date:
                continue
            price = float(quote["price"])
            entry_price = float(meta["entry_price"])
            return_pct = (price / entry_price - 1.0) * 100.0
            reason = "campaign_complete_liquidation" if force else None
            peak = _safe_float(meta.get("trailing_peak_price")) or price
            if not force:
                if (
                    meta.get("exit_watch_date") != session_date.isoformat()
                    or not meta.get("trailing_armed")
                    or meta.get("limit_up_seen")
                ):
                    continue
                peak = max(peak, float(quote.get("high") or price), price)
                meta["trailing_peak_price"] = peak
                trailing_stop_pct = float(meta.get("trailing_stop_pct") or spec.trailing_stop_pct)
                if price <= peak * (1.0 - trailing_stop_pct / 100.0):
                    reason = "opening_follow_through_trailing_stop"
            if reason:
                intents.append(
                    {
                        "strategy_id": spec.strategy_id,
                        "account_id": int(strategy_state["account_id"]),
                        "code": code,
                        "reason": reason,
                        "check_phase": check_phase,
                        "return_pct": round(return_pct, 6),
                        "peak_return_pct": round((peak / entry_price - 1.0) * 100.0, 6),
                        "auction_gain_pct": meta.get("auction_gain_pct"),
                        "trailing_peak_price": round(peak, 6),
                        "trailing_stop_pct": float(meta.get("trailing_stop_pct") or spec.trailing_stop_pct),
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
        exit_note = (
            f"paper exit phase={intent.get('check_phase')} reason={intent['reason']} "
            f"return_pct={intent.get('return_pct')} peak_return_pct={intent.get('peak_return_pct')} "
            f"auction_gain_pct={intent.get('auction_gain_pct')} "
            f"trailing_peak_price={intent.get('trailing_peak_price')} "
            f"trailing_stop_pct={intent.get('trailing_stop_pct')}"
        )
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
            note=exit_note,
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
            "check_phase": intent.get("check_phase"),
            "return_pct": intent.get("return_pct"),
            "peak_return_pct": intent.get("peak_return_pct"),
            "auction_gain_pct": intent.get("auction_gain_pct"),
            "trailing_peak_price": intent.get("trailing_peak_price"),
            "trailing_stop_pct": intent.get("trailing_stop_pct"),
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
        if not self._entry_watch_is_open(self.now_fn()):
            return None
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
        note = (
            f"{ENTRY_WATCH_START_TIME.strftime('%H:%M')}-"
            f"{ENTRY_WATCH_END_TIME.strftime('%H:%M')} watch signal "
            f"rank={row.get('rank')} "
            f"score={row.get('score')}"
        )
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
            note=note,
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

    @staticmethod
    def _entry_watch_is_open(now: datetime) -> bool:
        local_time = now.astimezone(SHANGHAI).timetz().replace(tzinfo=None)
        return ENTRY_WATCH_START_TIME <= local_time < ENTRY_WATCH_END_TIME

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
