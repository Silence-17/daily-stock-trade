# -*- coding: utf-8 -*-
"""Deterministic domain model for the cross-market paper strategy.

This module intentionally contains no network or database access. Runtime services
freeze point-in-time observations and pass them here for scoring and execution so
the same rules can be reused by live paper trading and historical replay.
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, fields, replace
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence


STRATEGY_ID = "cross_market_global_sector_rotation_v1.4_staged"

KR_COMPONENT_WEIGHTS = {
    "KS11": 0.20,
    "KQ11": 0.20,
    "005930.KS": 0.30,
    "000660.KS": 0.30,
}
KR_COMPONENT_SCALES_PCT = {
    "KS11": 1.5,
    "KQ11": 1.5,
    "005930.KS": 2.0,
    "000660.KS": 2.0,
}
KR_LINKED_THEMES = {"semiconductor", "memory", "equipment", "materials"}
TECHNOLOGY_WEIGHTED_THEMES = KR_LINKED_THEMES | {
    "cpo",
    "ccl",
    "mlcc",
    "artificial_intelligence",
    "compute_services",
    "gaming",
}
DOMESTIC_ROTATION_THEMES = {
    "pharma",
    "consumer",
    "bank",
    "utilities",
    "energy",
    "industrials",
}
GLOBAL_MARKET_LINKED_THEMES = TECHNOLOGY_WEIGHTED_THEMES | {"pharma"}
NASDAQ_FUTURES_LINKED_THEMES = set(TECHNOLOGY_WEIGHTED_THEMES)
TRADEABLE_THEMES = TECHNOLOGY_WEIGHTED_THEMES | DOMESTIC_ROTATION_THEMES | {"gold"}
CN_MAIN_BOARD_PREFIXES = ("000", "001", "002", "003", "600", "601", "603", "605")


def is_cn_main_board_symbol(symbol: object) -> bool:
    """Return whether a code is tradable by a main-board-only A-share account."""

    normalized = re.sub(r"\D", "", str(symbol or ""))[:6]
    return len(normalized) == 6 and normalized.startswith(CN_MAIN_BOARD_PREFIXES)

# Ordered from narrow supply-chain concepts to broad themes so a specific board
# such as MLCC or CCL is not swallowed by a generic electronics/semiconductor tag.
A_SHARE_THEME_KEYWORDS = (
    ("cpo", ("cpo", "optical module", "optical communication", "silicon photonics", "fiber optic", "\u5149\u6a21\u5757", "\u5149\u901a\u4fe1", "\u5149\u5668\u4ef6", "\u7845\u5149", "\u5149\u7ea4", "\u5149\u7535\u5b50")),
    ("mlcc", ("mlcc", "multilayer ceramic capacitor", "passive component", "\u591a\u5c42\u9676\u74f7\u7535\u5bb9", "\u9676\u74f7\u7535\u5bb9", "\u88ab\u52a8\u5143\u4ef6", "\u7535\u5bb9\u5668")),
    ("ccl", ("ccl", "copper clad laminate", "printed circuit board", "pcb", "\u8986\u94dc\u677f", "\u5370\u5236\u7535\u8def\u677f", "\u7535\u5b50\u5e03", "\u73bb\u7ea4\u5e03", "\u94dc\u7b94\u57fa\u677f")),
    ("memory", ("memory", "dram", "nand", "hbm", "\u5b58\u50a8", "\u5b58\u50a8\u82af\u7247")),
    ("materials", ("semiconductor material", "photoresist", "silicon wafer", "\u534a\u5bfc\u4f53\u6750\u6599", "\u534a\u5bfc\u4f53\u7845\u7247", "\u6676\u5706\u7845\u7247", "\u5927\u7845\u7247", "\u5149\u523b\u80f6", "\u7535\u5b50\u5316\u5b66\u54c1")),
    ("equipment", ("semiconductor equipment", "lithography", "etching", "\u534a\u5bfc\u4f53\u8bbe\u5907", "\u5149\u523b\u673a", "\u5149\u523b\u8bbe\u5907", "\u523b\u8680", "\u6e05\u6d17\u8bbe\u5907")),
    ("semiconductor", ("semiconductor", "chip", "integrated circuit", "\u534a\u5bfc\u4f53", "\u82af\u7247", "\u96c6\u6210\u7535\u8def")),
    ("gaming", ("gaming", "game", "esports", "\u6e38\u620f", "\u624b\u6e38", "\u7535\u7ade", "\u4e91\u6e38\u620f")),
    ("compute_services", ("computing power", "ai compute", "data center", "cloud computing", "\u7b97\u529b", "\u667a\u7b97", "\u6570\u636e\u4e2d\u5fc3", "\u4e1c\u6570\u897f\u7b97", "\u6db2\u51b7\u670d\u52a1\u5668", "\u670d\u52a1\u5668", "\u4e91\u8ba1\u7b97", "idc")),
    ("artificial_intelligence", ("artificial intelligence", "aigc", "chatgpt", "ai application", "ai agent", "\u4eba\u5de5\u667a\u80fd", "\u5927\u6a21\u578b", "\u673a\u5668\u5b66\u4e60", "\u751f\u6210\u5f0f", "\u591a\u6a21\u6001", "ai\u5e94\u7528", "ai\u667a\u80fd\u4f53")),
    ("pharma", ("pharma", "biotech", "innovative drug", "medical device", "cro", "cxo", "\u533b\u836f", "\u5236\u836f", "\u521b\u65b0\u836f", "\u751f\u7269\u533b\u836f", "\u751f\u7269\u5236\u54c1", "\u533b\u7597\u5668\u68b0", "\u533b\u7597\u670d\u52a1", "\u4e2d\u836f", "\u75ab\u82d7")),
    ("consumer", ("food and beverage", "retail", "home appliance", "tourism", "\u98df\u54c1\u996e\u6599", "\u767d\u9152", "\u5546\u8d38\u96f6\u552e", "\u5bb6\u7528\u7535\u5668", "\u65c5\u6e38", "\u9152\u5e97\u9910\u996e")),
    ("bank", ("bank", "banking", "\u94f6\u884c")),
    ("utilities", ("utilities", "power utility", "water utility", "gas utility", "\u516c\u7528\u4e8b\u4e1a", "\u706b\u7535", "\u6c34\u7535", "\u7535\u529b\u8fd0\u8425", "\u6c34\u52a1", "\u71c3\u6c14")),
    ("energy", ("oil and gas", "coal", "\u77f3\u6cb9\u77f3\u5316", "\u6cb9\u6c14\u5f00\u91c7", "\u7164\u70ad")),
    ("industrials", ("industrials", "machinery", "construction", "transportation", "\u5de5\u4e1a\u6bcd\u673a", "\u673a\u68b0\u8bbe\u5907", "\u5efa\u7b51\u88c5\u9970", "\u4ea4\u901a\u8fd0\u8f93")),
)

_A_SHARE_THEME_KEYWORD_EXPANSIONS = {
    "cpo": (
        "co-packaged optics", "optical transceiver", "800g", "1.6t",
        "\u5171\u5c01\u88c5\u5149\u5b66", "\u5149\u82af\u7247", "\u5149\u7f51\u7edc", "\u6ce2\u5206\u590d\u7528",
    ),
    "mlcc": (
        "\u88ab\u52a8\u5668\u4ef6", "\u7535\u5b50\u5143\u4ef6", "\u9676\u74f7\u7c89\u4f53",
    ),
    "ccl": (
        "hdi", "\u5370\u5237\u7535\u8def\u677f", "\u7ebf\u8def\u677f", "\u9ad8\u9891\u9ad8\u901f\u57fa\u677f", "pcb\u8bbe\u5907",
    ),
    "memory": (
        "sram", "flash storage", "\u5185\u5b58", "\u95ea\u5b58", "\u56fa\u6001\u786c\u76d8", "\u5b58\u50a8\u6a21\u7ec4", "\u5148\u8fdb\u5b58\u50a8",
    ),
    "equipment": (
        "wafer equipment", "deposition", "ion implantation", "\u6676\u5706\u8bbe\u5907", "\u8584\u819c\u6c89\u79ef",
        "\u79bb\u5b50\u6ce8\u5165", "\u6d82\u80f6\u663e\u5f71", "\u91cf\u6d4b\u8bbe\u5907", "\u5c01\u6d4b\u8bbe\u5907",
    ),
    "materials": (
        "electronic chemical", "\u6e7f\u7535\u5b50\u5316\u5b66\u54c1", "\u7535\u5b50\u7279\u6c14", "\u9776\u6750", "\u629b\u5149\u6db2", "cmp\u6750\u6599", "\u5c01\u88c5\u6750\u6599",
    ),
    "semiconductor": (
        "foundry", "fabless", "\u82af\u7247\u8bbe\u8ba1", "\u6676\u5706\u5236\u9020", "\u6676\u5706\u4ee3\u5de5", "\u5c01\u88c5\u6d4b\u8bd5",
        "\u5148\u8fdb\u5c01\u88c5", "\u6a21\u62df\u82af\u7247", "mcu", "\u6c7d\u8f66\u82af\u7247", "\u7b2c\u4e09\u4ee3\u534a\u5bfc\u4f53", "\u78b3\u5316\u7845", "\u6c2e\u5316\u9553",
        "microled", "micro led", "mini led", "miniled", "microcontroller",
    ),
    "gaming": (
        "video game", "\u7aef\u6e38", "\u6e38\u620f\u51fa\u6d77", "\u6e38\u620f\u7248\u53f7",
    ),
    "compute_services": (
        "aidc", "\u7b97\u529b\u79df\u8d41", "\u6db2\u51b7", "\u6570\u636e\u8981\u7d20", "\u8fb9\u7f18\u8ba1\u7b97",
    ),
    "artificial_intelligence": (
        "large language model", "machine vision", "\u673a\u5668\u89c6\u89c9", "\u667a\u80fd\u8bed\u97f3", "\u81ea\u7136\u8bed\u8a00\u5904\u7406",
    ),
    "pharma": (
        "cdmo", "adc", "glp-1", "\u8840\u6db2\u5236\u54c1", "\u539f\u6599\u836f", "\u4eff\u5236\u836f", "\u7ec6\u80de\u6cbb\u7597", "\u57fa\u56e0\u6cbb\u7597",
        "\u51cf\u80a5\u836f", "\u773c\u79d1\u533b\u7597", "\u53e3\u8154\u533b\u7597",
    ),
}
A_SHARE_THEME_KEYWORDS = tuple(
    (
        theme,
        tuple(dict.fromkeys((*keywords, *_A_SHARE_THEME_KEYWORD_EXPANSIONS.get(theme, ())))),
    )
    for theme, keywords in A_SHARE_THEME_KEYWORDS
)


def contains_theme_keyword(text: str, keyword: str) -> bool:
    """Match ASCII finance abbreviations without accepting inner substrings."""

    normalized_text = str(text or "").lower()
    normalized_keyword = str(keyword or "").strip().lower()
    if not normalized_keyword:
        return False
    if normalized_keyword.isascii() and any(
        character.isalnum() for character in normalized_keyword
    ):
        pattern = rf"(?<![a-z0-9]){re.escape(normalized_keyword)}(?![a-z0-9])"
        return re.search(pattern, normalized_text) is not None
    return normalized_keyword in normalized_text


ROTATION_REFERENCE_KEYWORDS = {
    "liquor": ("liquor", "baijiu", "\u767d\u9152", "\u917f\u9152"),
    "bank": ("bank", "banking", "\u94f6\u884c"),
}


def _finite_float(value: object) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamps must include timezone information")
    return value.astimezone(timezone.utc)


def _normalized_change_score(change_pct: float, scale_pct: float) -> float:
    if scale_pct <= 0:
        raise ValueError("scale_pct must be positive")
    return max(-100.0, min(100.0, float(change_pct) / scale_pct * 100.0))


@dataclass(frozen=True)
class StrategyConfig:
    strategy_id: str = STRATEGY_ID
    evidence_max_age_seconds: int = 120
    confirmation_samples: int = 5
    confirmation_duration_seconds: int = 300
    confirmation_min_gap_seconds: int = 45
    confirmation_max_gap_seconds: int = 90
    us_buy_score: float = 40.0
    us_close_strong_score: float = 45.0
    us_close_min_sector_change_pct: float = 0.5
    us_close_min_advancing_ratio: float = 0.60
    us_close_min_leader_change_pct: float = 1.0
    us_premarket_strong_score: float = 70.0
    us_premarket_min_sector_change_pct: float = 2.0
    us_premarket_min_advancing_ratio: float = 0.75
    us_premarket_min_leader_change_pct: float = 3.0
    low_position_max_percentile: float = 35.0
    board_support_tolerance_pct: float = 1.5
    board_resistance_warning_pct: float = 2.0
    board_breakout_confirmation_pct: float = 1.0
    board_breakout_min_volume_ratio: float = 1.5
    board_breakout_required_5m_closes: int = 2
    opening_sector_score_without_support: float = 60.0
    flat_open_min_sector_score: float = 60.0
    minimum_sector_score: float = 0.0
    board_support_score_bonus_max: float = 5.0
    flat_open_initial_position_pct: float = 50.0
    staged_entry_tranche_pct: float = 50.0
    reduced_entry_tranche_pct: float = 50.0
    entry_score_a: float = 75.0
    entry_score_b: float = 68.0
    entry_score_late_probe: float = 65.0
    entry_cross_market_weight_pct: float = 30.0
    entry_sector_weight_pct: float = 25.0
    entry_stock_weight_pct: float = 20.0
    entry_intraday_weight_pct: float = 15.0
    entry_technical_weight_pct: float = 10.0
    domestic_entry_cross_market_weight_pct: float = 10.0
    domestic_entry_sector_weight_pct: float = 35.0
    domestic_entry_stock_weight_pct: float = 25.0
    domestic_entry_intraday_weight_pct: float = 20.0
    domestic_entry_technical_weight_pct: float = 10.0
    unconfirmed_intraday_entry_cap_pct: float = 25.0
    core_leader_min_candidate_score: float = 68.0
    core_leader_min_turnover: float = 1_000_000_000.0
    core_leader_min_volume_ratio: float = 1.5
    core_leader_min_change_pct: float = 2.0
    core_leader_min_relative_strength_pct: float = 2.0
    core_leader_min_sector_change_pct: float = 0.0
    range_max_tranches: int = 2
    intraday_pullback_min_pct: float = 1.0
    nasdaq_futures_confirmation_samples: int = 3
    nasdaq_futures_confirmation_duration_seconds: int = 120
    nasdaq_futures_trend_window_minutes: int = 15
    nasdaq_futures_buy_block_change_pct: float = -1.0
    nasdaq_futures_buy_block_trend_pct: float = -0.5
    nasdaq_futures_reduce_change_pct: float = -1.5
    nasdaq_futures_reduce_trend_pct: float = -0.8
    nasdaq_futures_reduce_fraction: float = 0.5
    kr_buy_score: float = 40.0
    kr_hold_score: float = 25.0
    kr_reduce_score: float = -25.0
    kr_exit_score: float = -45.0
    asia_market_block_mean_change_pct: float = -0.7
    cn_high_open_pct: float = 0.19
    cn_low_open_upper_pct: float = -0.3
    cn_extreme_low_open_pct: float = -1.5
    gold_buy_score: float = 45.0
    gold_min_return_pct: float = 0.6
    gold_price_only_return_pct: float = 1.2
    range_adx_max: float = 20.0
    range_ma20_max_abs_slope_pct: float = 0.15
    range_buy_rsi: float = 35.0
    range_sell_rsi: float = 65.0
    min_edge_buffer_pct: float = 0.5
    max_total_exposure_pct: float = 100.0
    max_theme_exposure_pct: float = 100.0
    max_symbol_exposure_pct: float = 50.0
    max_positions: int = 2
    next_day_stock_high_open_pct: float = 1.0
    high_open_trailing_pullback_pct: float = 0.8
    stop_loss_pct: float = -5.0
    take_profit_pct: float = 8.0
    trailing_stop_pullback_pct: float = 4.0
    daily_loss_limit_pct: float = -1.5
    max_account_drawdown_pct: float = 8.0
    loss_streak_limit: int = 3
    loss_streak_cooldown_days: int = 3


_FACTOR_GROUP_LABELS = {
    "evidence": "证据时效与连续确认",
    "us": "美股科技与盘前信号",
    "board": "板块技术位与突破",
    "entry": "入场评分与分档",
    "nasdaq": "纳指期货联动",
    "korea": "韩股联动",
    "asia": "亚洲市场联动",
    "cn_open": "A 股开盘环境",
    "gold": "黄金信号",
    "range": "震荡交易",
    "risk": "仓位与风险",
    "exit": "退出与冷却",
}

_FACTOR_LABELS = {
    "evidence_max_age_seconds": "证据最长年龄",
    "confirmation_samples": "连续确认样本数",
    "confirmation_duration_seconds": "连续确认观察时长",
    "confirmation_min_gap_seconds": "确认样本最小间隔",
    "confirmation_max_gap_seconds": "确认样本最大间隔",
    "us_buy_score": "美股科技买入分",
    "us_close_strong_score": "美股收盘强势分",
    "us_close_min_sector_change_pct": "美股收盘板块最低涨幅",
    "us_close_min_advancing_ratio": "美股收盘上涨股最低占比",
    "us_close_min_leader_change_pct": "美股收盘龙头最低涨幅",
    "us_premarket_strong_score": "美股盘前强势分",
    "us_premarket_min_sector_change_pct": "美股盘前板块最低涨幅",
    "us_premarket_min_advancing_ratio": "美股盘前上涨股最低占比",
    "us_premarket_min_leader_change_pct": "美股盘前龙头最低涨幅",
    "low_position_max_percentile": "低位候选最高分位",
    "board_support_tolerance_pct": "板块支撑容差",
    "board_resistance_warning_pct": "板块压力预警距离",
    "board_breakout_confirmation_pct": "板块突破确认幅度",
    "board_breakout_min_volume_ratio": "板块突破最低量比",
    "board_breakout_required_5m_closes": "突破所需 5 分钟收盘次数",
    "opening_sector_score_without_support": "开盘无支撑时板块最低分",
    "flat_open_min_sector_score": "平开震荡板块最低分",
    "minimum_sector_score": "板块绝对最低分",
    "board_support_score_bonus_max": "板块支撑最高加分",
    "flat_open_initial_position_pct": "平开首次目标仓位",
    "staged_entry_tranche_pct": "分档入场单档仓位",
    "reduced_entry_tranche_pct": "降级入场单档仓位",
    "entry_score_a": "A 档入场分",
    "entry_score_b": "B 档入场分",
    "entry_score_late_probe": "尾档试仓分",
    "entry_cross_market_weight_pct": "科技/黄金跨市场权重",
    "entry_sector_weight_pct": "科技/黄金 A 股板块权重",
    "entry_stock_weight_pct": "科技/黄金个股量价权重",
    "entry_intraday_weight_pct": "科技/黄金盘中确认权重",
    "entry_technical_weight_pct": "科技/黄金技术位权重",
    "domestic_entry_cross_market_weight_pct": "国内轮动跨市场权重",
    "domestic_entry_sector_weight_pct": "国内轮动 A 股板块权重",
    "domestic_entry_stock_weight_pct": "国内轮动个股量价权重",
    "domestic_entry_intraday_weight_pct": "国内轮动盘中确认权重",
    "domestic_entry_technical_weight_pct": "国内轮动技术位权重",
    "unconfirmed_intraday_entry_cap_pct": "未站回开盘价/VWAP 仓位上限",
    "core_leader_min_candidate_score": "核心龙头最低候选分",
    "core_leader_min_turnover": "核心龙头最低成交额",
    "core_leader_min_volume_ratio": "核心龙头最低量比",
    "core_leader_min_change_pct": "核心龙头最低涨幅",
    "core_leader_min_relative_strength_pct": "核心龙头最低相对强度",
    "core_leader_min_sector_change_pct": "核心龙头板块最低涨幅",
    "range_max_tranches": "震荡策略最多分档数",
    "intraday_pullback_min_pct": "日内回撤最低幅度",
    "nasdaq_futures_confirmation_samples": "纳指期货确认样本数",
    "nasdaq_futures_confirmation_duration_seconds": "纳指期货确认时长",
    "nasdaq_futures_trend_window_minutes": "纳指期货趋势窗口",
    "nasdaq_futures_buy_block_change_pct": "纳指期货禁买涨跌幅",
    "nasdaq_futures_buy_block_trend_pct": "纳指期货禁买趋势幅度",
    "nasdaq_futures_reduce_change_pct": "纳指期货减仓涨跌幅",
    "nasdaq_futures_reduce_trend_pct": "纳指期货减仓趋势幅度",
    "nasdaq_futures_reduce_fraction": "纳指期货触发减仓比例",
    "kr_buy_score": "韩股联动买入分",
    "kr_hold_score": "韩股联动持有分",
    "kr_reduce_score": "韩股联动减仓分",
    "kr_exit_score": "韩股联动退出分",
    "asia_market_block_mean_change_pct": "亚洲市场明显走弱阈值",
    "cn_high_open_pct": "沪深 300 高开禁追阈值",
    "cn_low_open_upper_pct": "沪深 300 低开上界",
    "cn_extreme_low_open_pct": "沪深 300 极端低开阈值",
    "gold_buy_score": "黄金买入分",
    "gold_min_return_pct": "黄金最低收益率",
    "gold_price_only_return_pct": "仅价格证据最低收益率",
    "range_adx_max": "震荡行情最高 ADX",
    "range_ma20_max_abs_slope_pct": "MA20 最大绝对斜率",
    "range_buy_rsi": "震荡买入 RSI",
    "range_sell_rsi": "震荡卖出 RSI",
    "min_edge_buffer_pct": "成本外最低优势",
    "max_total_exposure_pct": "最大总仓位",
    "max_theme_exposure_pct": "最大单主题仓位",
    "max_symbol_exposure_pct": "最大单票仓位",
    "max_positions": "最大持仓数",
    "next_day_stock_high_open_pct": "次日个股高开阈值",
    "high_open_trailing_pullback_pct": "高开移动止盈回撤",
    "stop_loss_pct": "硬止损收益率",
    "take_profit_pct": "止盈启动收益率",
    "trailing_stop_pullback_pct": "移动止盈回撤",
    "daily_loss_limit_pct": "单日亏损熔断",
    "max_account_drawdown_pct": "账户最大回撤",
    "loss_streak_limit": "连续亏损上限",
    "loss_streak_cooldown_days": "连续亏损冷却交易日",
}

_FACTOR_CONSTRAINT_OVERRIDES: Dict[str, Dict[str, float]] = {
    "evidence_max_age_seconds": {"min": 1, "max": 3_600, "step": 1},
    "confirmation_samples": {"min": 1, "max": 100, "step": 1},
    "confirmation_duration_seconds": {"min": 1, "max": 3_600, "step": 1},
    "confirmation_min_gap_seconds": {"min": 1, "max": 600, "step": 1},
    "confirmation_max_gap_seconds": {"min": 1, "max": 600, "step": 1},
    "low_position_max_percentile": {"min": 0, "max": 100, "step": 0.5},
    "board_support_tolerance_pct": {"min": 0, "max": 20, "step": 0.05},
    "board_resistance_warning_pct": {"min": 0, "max": 20, "step": 0.05},
    "board_breakout_confirmation_pct": {"min": 0, "max": 20, "step": 0.05},
    "board_breakout_required_5m_closes": {"min": 2, "max": 20, "step": 1},
    "board_support_score_bonus_max": {"min": 0, "max": 100, "step": 0.5},
    "flat_open_initial_position_pct": {"min": 0, "max": 100, "step": 0.5},
    "staged_entry_tranche_pct": {"min": 0, "max": 100, "step": 0.5},
    "reduced_entry_tranche_pct": {"min": 0, "max": 100, "step": 0.5},
    "entry_score_a": {"min": 0, "max": 100, "step": 0.5},
    "entry_score_b": {"min": 0, "max": 100, "step": 0.5},
    "entry_score_late_probe": {"min": 0, "max": 100, "step": 0.5},
    "entry_cross_market_weight_pct": {"min": 0, "max": 100, "step": 0.5},
    "entry_sector_weight_pct": {"min": 0, "max": 100, "step": 0.5},
    "entry_stock_weight_pct": {"min": 0, "max": 100, "step": 0.5},
    "entry_intraday_weight_pct": {"min": 0, "max": 100, "step": 0.5},
    "entry_technical_weight_pct": {"min": 0, "max": 100, "step": 0.5},
    "domestic_entry_cross_market_weight_pct": {"min": 0, "max": 100, "step": 0.5},
    "domestic_entry_sector_weight_pct": {"min": 0, "max": 100, "step": 0.5},
    "domestic_entry_stock_weight_pct": {"min": 0, "max": 100, "step": 0.5},
    "domestic_entry_intraday_weight_pct": {"min": 0, "max": 100, "step": 0.5},
    "domestic_entry_technical_weight_pct": {"min": 0, "max": 100, "step": 0.5},
    "unconfirmed_intraday_entry_cap_pct": {"min": 0, "max": 100, "step": 0.5},
    "range_max_tranches": {"min": 1, "max": 20, "step": 1},
    "nasdaq_futures_confirmation_samples": {"min": 1, "max": 100, "step": 1},
    "nasdaq_futures_confirmation_duration_seconds": {"min": 1, "max": 3_600, "step": 1},
    "nasdaq_futures_trend_window_minutes": {"min": 1, "max": 240, "step": 1},
    "range_adx_max": {"min": 0, "max": 100, "step": 0.5},
    "range_buy_rsi": {"min": 0, "max": 100, "step": 0.5},
    "range_sell_rsi": {"min": 0, "max": 100, "step": 0.5},
    "min_edge_buffer_pct": {"min": 0, "max": 100, "step": 0.05},
    "max_total_exposure_pct": {"min": 0, "max": 100, "step": 0.5},
    "max_theme_exposure_pct": {"min": 0, "max": 100, "step": 0.5},
    "max_symbol_exposure_pct": {"min": 0, "max": 100, "step": 0.5},
    "max_positions": {"min": 1, "max": 100, "step": 1},
    "high_open_trailing_pullback_pct": {"min": 0, "max": 100, "step": 0.05},
    "stop_loss_pct": {"min": -100, "max": 0, "step": 0.05},
    "take_profit_pct": {"min": 0, "max": 100, "step": 0.05},
    "trailing_stop_pullback_pct": {"min": 0, "max": 100, "step": 0.05},
    "daily_loss_limit_pct": {"min": -100, "max": 0, "step": 0.05},
    "max_account_drawdown_pct": {"min": 0, "max": 100, "step": 0.05},
    "loss_streak_limit": {"min": 1, "max": 100, "step": 1},
    "loss_streak_cooldown_days": {"min": 0, "max": 365, "step": 1},
    "asia_market_block_mean_change_pct": {"min": -10, "max": 0, "step": 0.05},
}


def _strategy_factor_group(key: str) -> str:
    if key.startswith(("evidence_", "confirmation_")):
        return "evidence"
    if key.startswith("us_"):
        return "us"
    if key.startswith("board_"):
        return "board"
    if key.startswith(("opening_", "flat_open_", "minimum_sector_", "entry_", "core_leader_", "staged_", "reduced_", "low_position_")):
        return "entry"
    if key.startswith("nasdaq_"):
        return "nasdaq"
    if key.startswith("kr_"):
        return "korea"
    if key.startswith("asia_market_"):
        return "asia"
    if key.startswith("cn_"):
        return "cn_open"
    if key.startswith("gold_"):
        return "gold"
    if key.startswith(("range_", "intraday_pullback_")):
        return "range"
    if key.startswith(("next_day_", "high_open_", "stop_loss_", "take_profit_", "trailing_stop_", "loss_streak_")):
        return "exit"
    return "risk"


def _strategy_factor_constraints(key: str, default: int | float) -> Dict[str, Any]:
    override = _FACTOR_CONSTRAINT_OVERRIDES.get(key)
    if override is not None:
        return {
            "value_type": "integer" if isinstance(default, int) else "number",
            **override,
        }
    if isinstance(default, int):
        maximum = 86_400 if key.endswith("_seconds") else 10_000
        return {"value_type": "integer", "min": 0, "max": maximum, "step": 1}
    if key.endswith("_fraction") or "advancing_ratio" in key:
        return {"value_type": "number", "min": 0.0, "max": 1.0, "step": 0.01}
    if key.endswith("_turnover"):
        return {"value_type": "number", "min": 0.0, "max": 1_000_000_000_000.0, "step": 1_000_000.0}
    if key.endswith("_ratio"):
        return {"value_type": "number", "min": 0.0, "max": 20.0, "step": 0.05}
    if "score" in key:
        return {"value_type": "number", "min": -100.0, "max": 100.0, "step": 0.5}
    if key.endswith("_pct") or key.endswith("_percentile"):
        return {"value_type": "number", "min": -100.0, "max": 100.0, "step": 0.05}
    return {"value_type": "number", "min": -100_000.0, "max": 100_000.0, "step": 0.1}


def strategy_config_from_overrides(overrides: Mapping[str, object] | None) -> StrategyConfig:
    """Build one validated runtime config from persisted numeric overrides."""

    defaults = StrategyConfig()
    allowed = {item.name: getattr(defaults, item.name) for item in fields(defaults) if item.name != "strategy_id"}
    normalized: Dict[str, int | float] = {}
    for raw_key, raw_value in dict(overrides or {}).items():
        key = str(raw_key).strip()
        if key not in allowed:
            raise ValueError(f"unsupported_cross_market_factor:{key}")
        default = allowed[key]
        try:
            number = float(raw_value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid_cross_market_factor:{key}") from exc
        if not math.isfinite(number):
            raise ValueError(f"invalid_cross_market_factor:{key}")
        constraints = _strategy_factor_constraints(key, default)
        if number < float(constraints["min"]) or number > float(constraints["max"]):
            raise ValueError(f"cross_market_factor_out_of_range:{key}")
        if isinstance(default, int):
            if not number.is_integer():
                raise ValueError(f"cross_market_factor_must_be_integer:{key}")
            normalized[key] = int(number)
        else:
            normalized[key] = number

    config = replace(defaults, **normalized)
    if not (config.entry_score_a >= config.entry_score_b >= config.entry_score_late_probe):
        raise ValueError("cross_market_entry_score_order_invalid")
    global_entry_weight_total = sum((
        config.entry_cross_market_weight_pct,
        config.entry_sector_weight_pct,
        config.entry_stock_weight_pct,
        config.entry_intraday_weight_pct,
        config.entry_technical_weight_pct,
    ))
    domestic_entry_weight_total = sum((
        config.domestic_entry_cross_market_weight_pct,
        config.domestic_entry_sector_weight_pct,
        config.domestic_entry_stock_weight_pct,
        config.domestic_entry_intraday_weight_pct,
        config.domestic_entry_technical_weight_pct,
    ))
    if min(global_entry_weight_total, domestic_entry_weight_total) <= 0:
        raise ValueError("cross_market_entry_weights_invalid")
    if not (config.kr_buy_score >= config.kr_hold_score > config.kr_reduce_score >= config.kr_exit_score):
        raise ValueError("cross_market_korea_score_order_invalid")
    if not (config.cn_high_open_pct > config.cn_low_open_upper_pct > config.cn_extreme_low_open_pct):
        raise ValueError("cross_market_cn_open_threshold_order_invalid")
    if config.confirmation_min_gap_seconds > config.confirmation_max_gap_seconds:
        raise ValueError("cross_market_confirmation_gap_order_invalid")
    if config.confirmation_duration_seconds < (
        max(0, config.confirmation_samples - 1)
        * config.confirmation_min_gap_seconds
    ):
        raise ValueError("cross_market_confirmation_duration_too_short")
    if config.max_positions < 1:
        raise ValueError("cross_market_max_positions_invalid")
    if not (
        config.max_symbol_exposure_pct
        <= config.max_theme_exposure_pct
        <= config.max_total_exposure_pct
    ):
        raise ValueError("cross_market_exposure_order_invalid")
    if max(
        config.flat_open_initial_position_pct,
        config.staged_entry_tranche_pct,
        config.reduced_entry_tranche_pct,
    ) > config.max_symbol_exposure_pct:
        raise ValueError("cross_market_entry_tranche_exposure_invalid")
    if config.range_buy_rsi >= config.range_sell_rsi:
        raise ValueError("cross_market_range_rsi_order_invalid")
    if config.gold_price_only_return_pct < config.gold_min_return_pct:
        raise ValueError("cross_market_gold_return_order_invalid")
    if (
        config.nasdaq_futures_reduce_change_pct
        > config.nasdaq_futures_buy_block_change_pct
        or config.nasdaq_futures_reduce_trend_pct
        > config.nasdaq_futures_buy_block_trend_pct
    ):
        raise ValueError("cross_market_nasdaq_reduce_order_invalid")
    return config


def strategy_factor_catalog(
    config: Optional[StrategyConfig] = None,
    overrides: Mapping[str, object] | None = None,
) -> List[Dict[str, Any]]:
    """Return every numeric runtime factor in declaration order for the dashboard."""

    defaults = StrategyConfig()
    effective = config or strategy_config_from_overrides(overrides)
    override_keys = {str(key) for key in dict(overrides or {})}
    result: List[Dict[str, Any]] = []
    for item in fields(defaults):
        if item.name in {
            "strategy_id",
            "opening_sector_score_without_support",
            "flat_open_min_sector_score",
        }:
            continue
        default = getattr(defaults, item.name)
        value = getattr(effective, item.name)
        group = _strategy_factor_group(item.name)
        result.append(
            {
                "key": item.name,
                "label": _FACTOR_LABELS.get(item.name, item.name),
                "description": f"跨市场 V1.4 运行参数 `{item.name}`，保存后用于后续策略判断。",
                "group": group,
                "group_label": _FACTOR_GROUP_LABELS[group],
                "value": value,
                "default_value": default,
                "overridden": item.name in override_keys,
                "editable": True,
                "unit": (
                    "秒" if item.name.endswith("_seconds")
                    else "分钟" if item.name.endswith("_minutes")
                    else "交易日" if item.name.endswith("_days")
                    else "%" if item.name.endswith(("_pct", "_percentile"))
                    else "分" if item.name.endswith("_score")
                    else "元" if item.name.endswith("_turnover")
                    else ""
                ),
                **_strategy_factor_constraints(item.name, default),
            }
        )
    return result


@dataclass(frozen=True)
class AccountRiskState:
    total_exposure_pct: float = 0.0
    theme_exposure_pct: float = 0.0
    symbol_exposure_pct: float = 0.0
    position_count: int = 0
    daily_pnl_pct: float = 0.0
    account_drawdown_pct: float = 0.0
    consecutive_losses: int = 0
    in_loss_streak_cooldown: bool = False


@dataclass(frozen=True)
class StrategyDecisionInput:
    theme: str
    cn_gap_pct: float
    entry_phase: str = "opening"
    has_position: bool = False
    flat_open_staged_entry: bool = False
    strategy_cost_basis_pct: float = 0.0
    current_tranche_count: int = 0
    sellable_fraction: float = 0.0
    reclaimed_open: bool = False
    above_vwap: bool = False
    sector_signal_score: float = 0.0
    core_leader_signal: Optional[Mapping[str, object]] = None
    us_tech_score: Optional[float] = None
    us_close_theme_signal: Optional[Mapping[str, object]] = None
    us_premarket_signal: Optional[Mapping[str, object]] = None
    nasdaq_futures_signal: Optional[Mapping[str, object]] = None
    asia_supply_chain_signal: Optional[Mapping[str, object]] = None
    low_position_signal: Optional[Mapping[str, object]] = None
    intraday_pullback_signal: Optional[Mapping[str, object]] = None
    asia_market_gate: Optional[Mapping[str, object]] = None
    board_technical_signal: Optional[Mapping[str, object]] = None
    rotation_signal: Optional[Mapping[str, object]] = None
    next_day_high_open_exit_signal: Optional[Mapping[str, object]] = None
    korea_gate: Optional[Mapping[str, object]] = None
    cpo_signal: Optional[Mapping[str, object]] = None
    gold_signal: Optional[Mapping[str, object]] = None
    range_signal: Optional[Mapping[str, object]] = None
    position_return_pct: Optional[float] = None
    pullback_from_peak_pct: float = 0.0
    expected_gross_edge_pct: float = 0.0
    estimated_round_trip_cost_pct: float = 0.0
    entry_score: Optional[float] = None
    analysis_slot: Optional[str] = None
    failed_breakout_signal: Optional[Mapping[str, object]] = None
    risk: AccountRiskState = AccountRiskState()


@dataclass(frozen=True)
class StrategyDecision:
    action: str
    reason: str
    buy_allowed: bool = False
    sell_fraction: float = 0.0
    target_tranche_delta: int = 0
    target_position_pct: float = 0.0
    deferred_action: Optional[str] = None
    entry_score: Optional[float] = None
    entry_grade: Optional[str] = None

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class TimedMarketObservation:
    code: str
    change_pct: float
    observed_at: datetime
    provider_timestamp: datetime

    def normalized_code(self) -> str:
        return str(self.code or "").strip().upper()


@dataclass(frozen=True)
class KoreaSignalSnapshot:
    observed_at: datetime
    score: float
    component_changes_pct: Dict[str, float]


@dataclass(frozen=True)
class TradeFeeSchedule:
    commission_rate: float = 0.00008
    minimum_commission: float = 5.0
    stock_sell_stamp_tax_rate: float = 0.0005
    stock_transfer_fee_rate: float = 0.00001
    commission_includes_exchange_regulatory_fees: bool = True

    def calculate(self, *, side: str, notional: float, instrument_type: str = "stock") -> Dict[str, float]:
        side_norm = str(side or "").strip().lower()
        if side_norm not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")
        value = _finite_float(notional)
        if value is None or value <= 0:
            raise ValueError("notional must be positive")
        instrument = str(instrument_type or "stock").strip().lower()
        commission = max(float(self.minimum_commission), value * float(self.commission_rate))
        stamp_tax = value * float(self.stock_sell_stamp_tax_rate) if instrument == "stock" and side_norm == "sell" else 0.0
        transfer_fee = value * float(self.stock_transfer_fee_rate) if instrument == "stock" else 0.0
        total = commission + stamp_tax + transfer_fee
        return {
            "commission": round(commission, 6),
            "stamp_tax": round(stamp_tax, 6),
            "transfer_fee": round(transfer_fee, 6),
            "total": round(total, 6),
        }

    def max_buy_notional_for_cash_budget(
        self,
        *,
        cash_budget: float,
        instrument_type: str = "stock",
    ) -> float:
        """Return the largest buy notional whose debit, including fees, fits."""

        budget = _finite_float(cash_budget)
        if budget is None or budget <= 0:
            raise ValueError("cash_budget must be positive")
        if budget <= float(self.minimum_commission):
            return 0.0

        low = 0.0
        high = budget
        for _ in range(80):
            midpoint = (low + high) / 2.0
            fees = self.calculate(
                side="buy",
                notional=midpoint,
                instrument_type=instrument_type,
            )
            if midpoint + float(fees["total"]) <= budget:
                low = midpoint
            else:
                high = midpoint

        # Never round upward into a cash deficit at the six-decimal ledger scale.
        resolved = math.floor(low * 1_000_000.0) / 1_000_000.0
        while resolved > 0:
            fees = self.calculate(
                side="buy",
                notional=resolved,
                instrument_type=instrument_type,
            )
            if resolved + float(fees["total"]) <= budget:
                break
            resolved = max(0.0, resolved - 0.000001)
        return resolved

    def estimate_round_trip_cost_pct(
        self,
        *,
        notional: float,
        instrument_type: str = "stock",
        buy_slippage_bps: float = 0.0,
        sell_slippage_bps: float = 0.0,
    ) -> float:
        value = _finite_float(notional)
        if value is None or value <= 0:
            raise ValueError("notional must be positive")
        fees = self.calculate(side="buy", notional=value, instrument_type=instrument_type)["total"]
        fees += self.calculate(side="sell", notional=value, instrument_type=instrument_type)["total"]
        slippage = value * (max(0.0, buy_slippage_bps) + max(0.0, sell_slippage_bps)) / 10000.0
        return round((fees + slippage) / value * 100.0, 6)


@dataclass(frozen=True)
class MinuteBar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: Optional[float] = None
    bid_ask_spread_bps: Optional[float] = None
    atr_1m_pct: Optional[float] = None
    suspended: bool = False
    limit_up_price: Optional[float] = None
    limit_down_price: Optional[float] = None

    @property
    def vwap(self) -> float:
        amount = _finite_float(self.amount)
        volume = _finite_float(self.volume)
        if amount is not None and amount > 0 and volume is not None and volume > 0:
            return amount / volume
        return (float(self.high) + float(self.low) + float(self.close)) / 3.0


@dataclass(frozen=True)
class PaperOrder:
    symbol: str
    side: str
    quantity: float
    signal_at: datetime
    declared_quantity: Optional[float] = None
    limit_price: Optional[float] = None
    instrument_type: str = "stock"
    market: str = "cn"
    sellable_quantity: Optional[float] = None
    cancel_requested: bool = False
    previous_close: Optional[float] = None
    price_limit_pct: Optional[float] = None


@dataclass(frozen=True)
class PaperFill:
    status: str
    reason: Optional[str]
    requested_quantity: float
    filled_quantity: float
    unfilled_quantity: float
    reference_price: Optional[float]
    fill_price: Optional[float]
    slippage_bps: Optional[float]
    notional: float
    fees: Dict[str, float]
    net_cash_change: float

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


class CrossMarketSignalEngine:
    """Pure scoring and gate evaluation for the strategy."""

    def __init__(self, config: Optional[StrategyConfig] = None) -> None:
        self.config = config or StrategyConfig()

    @staticmethod
    def classify_theme(*values: object) -> str:
        explicit_theme_keys = (
            "_cross_market_source_theme",
            "entry_theme",
            "_cross_market_prefilter_theme",
            "theme",
        )
        for value in values:
            if not isinstance(value, Mapping):
                continue
            for key in explicit_theme_keys:
                explicit = str(value.get(key) or "").strip().lower()
                if explicit in TRADEABLE_THEMES:
                    return explicit

        texts: List[str] = []
        pending = list(values)
        while pending:
            value = pending.pop(0)
            if isinstance(value, Mapping):
                pending.extend(value.values())
            elif isinstance(value, (list, tuple, set)):
                pending.extend(value)
            elif value is not None:
                texts.append(str(value).strip().lower())
        text = " ".join(item for item in texts if item)
        finance_markers = (
            "bank",
            "insurance",
            "securities",
            "financial",
            "\u94f6\u884c",
            "\u4fdd\u9669",
            "\u8bc1\u5238",
            "\u591a\u5143\u91d1\u878d",
        )
        gold_keywords = ("gold", "\u9ec4\u91d1", "\u8d35\u91d1\u5c5e")
        for theme, keywords in A_SHARE_THEME_KEYWORDS:
            if any(contains_theme_keyword(text, keyword) for keyword in keywords):
                return theme
        if (
            any(contains_theme_keyword(text, keyword) for keyword in gold_keywords)
            and not any(contains_theme_keyword(text, marker) for marker in finance_markers)
        ):
            return "gold"
        return "other"

    @staticmethod
    def calculate_us_tech_score(
        *,
        first_hour_change_pct: float,
        semiconductor_change_pct: float,
        memory_basket_change_pct: float,
        nasdaq_change_pct: float,
        advancing_ratio: float,
    ) -> float:
        breadth = max(-100.0, min(100.0, (float(advancing_ratio) - 0.5) * 200.0))
        score = (
            0.25 * _normalized_change_score(first_hour_change_pct, 2.0)
            + 0.30 * _normalized_change_score(semiconductor_change_pct, 2.0)
            + 0.30 * _normalized_change_score(memory_basket_change_pct, 2.0)
            + 0.10 * _normalized_change_score(nasdaq_change_pct, 1.5)
            + 0.05 * breadth
        )
        return round(score, 6)

    def build_korea_snapshot(
        self,
        observations: Iterable[TimedMarketObservation],
        *,
        now: datetime,
    ) -> KoreaSignalSnapshot:
        current = _as_utc(now)
        by_code = {item.normalized_code(): item for item in observations}
        missing = sorted(set(KR_COMPONENT_WEIGHTS) - set(by_code))
        if missing:
            raise ValueError(f"korea_evidence_missing:{','.join(missing)}")

        changes: Dict[str, float] = {}
        observed_times: List[datetime] = []
        score = 0.0
        for code, weight in KR_COMPONENT_WEIGHTS.items():
            item = by_code[code]
            observed_at = _as_utc(item.observed_at)
            provider_at = _as_utc(item.provider_timestamp)
            if observed_at > current + timedelta(seconds=1) or provider_at > current + timedelta(seconds=1):
                raise ValueError("korea_evidence_from_future")
            provider_age = (current - provider_at).total_seconds()
            if provider_age > self.config.evidence_max_age_seconds:
                raise ValueError(f"korea_evidence_stale:{code}")
            change = _finite_float(item.change_pct)
            if change is None:
                raise ValueError(f"korea_evidence_invalid:{code}")
            changes[code] = change
            observed_times.append(observed_at)
            score += weight * _normalized_change_score(change, KR_COMPONENT_SCALES_PCT[code])

        return KoreaSignalSnapshot(
            observed_at=max(observed_times),
            score=round(score, 6),
            component_changes_pct=changes,
        )

    def evaluate_korea_gate(
        self,
        snapshots: Sequence[KoreaSignalSnapshot],
        *,
        theme: str,
        now: datetime,
    ) -> Dict[str, object]:
        theme_norm = str(theme or "").strip().lower()
        if theme_norm == "cpo":
            return {
                "status": "bypassed",
                "reason": "cpo_independent_theme",
                "buy_allowed": True,
                "sell_fraction": 0.0,
                "confirmed": True,
            }
        if theme_norm not in KR_LINKED_THEMES:
            return {
                "status": "not_applicable",
                "reason": "theme_not_korea_linked",
                "buy_allowed": True,
                "sell_fraction": 0.0,
                "confirmed": True,
            }

        current = _as_utc(now)
        required = max(1, int(self.config.confirmation_samples))
        ordered = sorted(snapshots, key=lambda item: _as_utc(item.observed_at))
        if len(ordered) < required:
            return self._unavailable_gate("korea_confirmation_samples_insufficient")

        min_gap = max(1, int(self.config.confirmation_min_gap_seconds))
        max_gap = max(min_gap, int(self.config.confirmation_max_gap_seconds))
        required_duration = max(
            0,
            int(self.config.confirmation_duration_seconds),
        )
        selected: Optional[List[KoreaSignalSnapshot]] = None
        duration_shortfall = False
        for anchor_index in range(len(ordered) - 1, -1, -1):
            candidate_window = [ordered[anchor_index]]
            for candidate in reversed(ordered[:anchor_index]):
                gap = (
                    _as_utc(candidate_window[-1].observed_at)
                    - _as_utc(candidate.observed_at)
                ).total_seconds()
                if gap < min_gap:
                    continue
                if gap > max_gap:
                    break
                candidate_window.append(candidate)
                span_seconds = (
                    _as_utc(candidate_window[0].observed_at)
                    - _as_utc(candidate_window[-1].observed_at)
                ).total_seconds()
                if len(candidate_window) >= required:
                    if span_seconds >= required_duration:
                        selected = candidate_window
                        break
                    duration_shortfall = True
            if selected is not None:
                break
        if selected is None and duration_shortfall:
            return self._unavailable_gate("korea_confirmation_duration_insufficient")
        if selected is None:
            return self._unavailable_gate("korea_confirmation_not_continuous")

        window = list(reversed(selected))
        times = [_as_utc(item.observed_at) for item in window]
        confirmation_span_seconds = (times[-1] - times[0]).total_seconds()
        if confirmation_span_seconds < required_duration:
            return self._unavailable_gate("korea_confirmation_duration_insufficient")
        if times[-1] > current + timedelta(seconds=1):
            return self._unavailable_gate("korea_evidence_from_future")
        if (current - times[-1]).total_seconds() > self.config.evidence_max_age_seconds:
            return self._unavailable_gate("korea_evidence_stale")
        gaps = [(right - left).total_seconds() for left, right in zip(times, times[1:])]
        if any(gap < min_gap or gap > max_gap for gap in gaps):
            return self._unavailable_gate("korea_confirmation_not_continuous")

        scores = [float(item.score) for item in window]
        latest = scores[-1]
        all_exit = all(score <= self.config.kr_exit_score for score in scores)
        all_reduce = all(score <= self.config.kr_reduce_score for score in scores)
        all_buy = all(score >= self.config.kr_buy_score for score in scores)
        all_hold = all(score >= self.config.kr_hold_score for score in scores)
        if all_exit:
            status, reason, sell_fraction = "exit", "korea_strong_decline_confirmed", 1.0
        elif all_reduce:
            status, reason, sell_fraction = "reduce", "korea_decline_confirmed", 0.5
        elif all_hold:
            status, reason, sell_fraction = "hold", "korea_strength_confirmed", 0.0
        else:
            status, reason, sell_fraction = "neutral", "korea_direction_unconfirmed", 0.0
        return {
            "status": status,
            "reason": reason,
            "buy_allowed": all_buy,
            "sell_fraction": sell_fraction,
            "confirmed": all_exit or all_reduce or all_hold,
            "latest_score": round(latest, 6),
            "scores": [round(score, 6) for score in scores],
            "confirmation_sample_count": len(window),
            "confirmation_span_seconds": round(confirmation_span_seconds, 6),
            "confirmation_duration_seconds": required_duration,
            "confirmation_min_gap_seconds": min_gap,
            "confirmation_max_gap_seconds": max_gap,
            "latest_component_changes_pct": dict(window[-1].component_changes_pct),
        }

    @staticmethod
    def _unavailable_gate(reason: str) -> Dict[str, object]:
        return {
            "status": "unavailable",
            "reason": reason,
            "buy_allowed": False,
            "sell_fraction": 0.0,
            "confirmed": False,
        }

    def classify_cn_open(self, gap_pct: float) -> Dict[str, object]:
        gap = float(gap_pct)
        if gap >= self.config.cn_high_open_pct:
            return {"regime": "high_open", "buy_allowed": False, "force_sell": True}
        if gap < self.config.cn_extreme_low_open_pct:
            return {"regime": "extreme_low_open", "buy_allowed": False, "force_sell": False}
        if gap <= self.config.cn_low_open_upper_pct:
            return {"regime": "low_open", "buy_allowed": True, "force_sell": False}
        return {"regime": "flat_open", "buy_allowed": False, "force_sell": False}

    def calculate_gold_score(
        self,
        *,
        overnight_return_pct: float,
        five_day_return_pct: float,
        above_ma20: bool,
        rate_cut_news_score: float,
        ma5_above_ma20: Optional[bool] = None,
    ) -> Dict[str, object]:
        price_score = _normalized_change_score(overnight_return_pct, 1.2)
        trend_confirmed = above_ma20 and ma5_above_ma20 is not False
        trend_score = 0.6 * _normalized_change_score(five_day_return_pct, 3.0) + (40.0 if trend_confirmed else -40.0)
        news_score = max(-100.0, min(100.0, float(rate_cut_news_score)))
        total = 0.55 * price_score + 0.25 * trend_score + 0.20 * news_score
        normal_buy = (
            total >= self.config.gold_buy_score
            and overnight_return_pct >= self.config.gold_min_return_pct
            and trend_confirmed
            and news_score > 0
        )
        half_buy = not normal_buy and overnight_return_pct >= self.config.gold_price_only_return_pct and trend_confirmed
        return {
            "score": round(total, 6),
            "buy_allowed": normal_buy or half_buy,
            "target_fraction": 1.0 if normal_buy else 0.5 if half_buy else 0.0,
            "reason": "gold_price_and_news_confirmed" if normal_buy else "gold_price_only_confirmed" if half_buy else "gold_signal_unconfirmed",
            "trend_confirmed": trend_confirmed,
        }

    @staticmethod
    def calculate_rate_cut_news_score(
        items: Iterable[Mapping[str, object]],
        *,
        window_start: datetime,
        window_end: datetime,
    ) -> Dict[str, object]:
        start = _as_utc(window_start)
        end = _as_utc(window_end)
        if end < start:
            raise ValueError("news window end must not precede start")
        positive_terms = (
            "rate cut",
            "rates cut",
            "interest rate reduction",
            "basis point cut",
            "monetary easing",
            "easing cycle",
            "fed pause",
            "pause rate hikes",
            "dovish",
            "lower interest rates",
            "降息",
            "下调利率",
            "暂停加息",
            "货币宽松",
            "宽松周期",
            "鸽派",
        )
        negative_terms = (
            "no rate cut",
            "no cuts",
            "delay rate cuts",
            "delayed rate cuts",
            "higher for longer",
            "higher real yields",
            "rising real yields",
            "rate hike",
            "rates hike",
            "hawkish",
            "不降息",
            "延后降息",
            "推迟降息",
            "维持高利率",
            "实际利率上升",
            "加息",
            "上调利率",
            "鹰派",
        )
        positive_count = 0
        negative_count = 0
        matched_items = []
        for item in items:
            timestamp = item.get("published_at") or item.get("fetched_at")
            if isinstance(timestamp, str):
                try:
                    timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                except ValueError:
                    continue
            if not isinstance(timestamp, datetime) or timestamp.tzinfo is None:
                continue
            observed_at = _as_utc(timestamp)
            if observed_at < start or observed_at > end:
                continue
            text = f"{item.get('title') or ''} {item.get('summary') or ''}".lower()
            negative = any(term in text for term in negative_terms)
            positive = not negative and any(term in text for term in positive_terms)
            if not positive and not negative:
                continue
            positive_count += int(positive)
            negative_count += int(negative)
            matched_items.append({
                "title": str(item.get("title") or "")[:200],
                "observed_at": observed_at.isoformat(),
                "published_at": observed_at.isoformat(),
                "summary": str(item.get("summary") or "")[:500],
                "source": str(item.get("source") or "")[:100],
                "direction": "positive" if positive else "negative",
            })
        score = max(-100.0, min(100.0, positive_count * 35.0 - negative_count * 45.0))
        return {
            "score": score,
            "positive_count": positive_count,
            "negative_count": negative_count,
            "matched_items": matched_items,
            "window_start": start.isoformat(),
            "window_end": end.isoformat(),
        }

    def evaluate_range_action(
        self,
        *,
        adx14: float,
        ma20_slope_pct_per_day: float,
        price: float,
        range_low: float,
        rsi14: float,
        bollinger_position: float,
        above_vwap: bool,
    ) -> Dict[str, object]:
        ranging = (
            adx14 < self.config.range_adx_max
            and abs(ma20_slope_pct_per_day) <= self.config.range_ma20_max_abs_slope_pct
            and price >= range_low
        )
        if not ranging:
            return {
                "regime": "not_range",
                "action": "exit" if price < range_low else "hold",
                "tranche_delta": -self.config.range_max_tranches if price < range_low else 0,
            }
        if bollinger_position <= 0.15 and rsi14 <= self.config.range_buy_rsi and above_vwap:
            return {"regime": "range", "action": "buy", "tranche_delta": 1}
        if bollinger_position >= 0.85 or rsi14 >= self.config.range_sell_rsi:
            return {"regime": "range", "action": "sell", "tranche_delta": -1}
        return {"regime": "range", "action": "hold", "tranche_delta": 0}

    def calculate_range_indicators(
        self,
        *,
        highs: Sequence[float],
        lows: Sequence[float],
        closes: Sequence[float],
        intraday_vwap: float,
    ) -> Dict[str, object]:
        if len(highs) != len(lows) or len(lows) != len(closes) or len(closes) < 28:
            raise ValueError("range_history_requires_28_aligned_bars")
        high_values = [float(value) for value in highs]
        low_values = [float(value) for value in lows]
        close_values = [float(value) for value in closes]
        if any(
            not math.isfinite(value) or value <= 0
            for value in high_values + low_values + close_values + [float(intraday_vwap)]
        ):
            raise ValueError("range_history_contains_invalid_value")
        if any(low > high for low, high in zip(low_values, high_values)):
            raise ValueError("range_history_low_above_high")

        true_ranges = []
        plus_dm = []
        minus_dm = []
        gains = []
        losses = []
        for index in range(1, len(close_values)):
            up_move = high_values[index] - high_values[index - 1]
            down_move = low_values[index - 1] - low_values[index]
            plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0.0)
            minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0.0)
            true_ranges.append(
                max(
                    high_values[index] - low_values[index],
                    abs(high_values[index] - close_values[index - 1]),
                    abs(low_values[index] - close_values[index - 1]),
                )
            )
            change = close_values[index] - close_values[index - 1]
            gains.append(max(0.0, change))
            losses.append(max(0.0, -change))

        dx_values = []
        for end in range(14, len(true_ranges) + 1):
            tr_sum = sum(true_ranges[end - 14 : end])
            if tr_sum <= 0:
                dx_values.append(0.0)
                continue
            plus_di = 100.0 * sum(plus_dm[end - 14 : end]) / tr_sum
            minus_di = 100.0 * sum(minus_dm[end - 14 : end]) / tr_sum
            denominator = plus_di + minus_di
            dx_values.append(0.0 if denominator <= 0 else 100.0 * abs(plus_di - minus_di) / denominator)
        adx14 = sum(dx_values[-14:]) / min(14, len(dx_values))

        average_gain = sum(gains[-14:]) / 14.0
        average_loss = sum(losses[-14:]) / 14.0
        if average_loss <= 0 and average_gain <= 0:
            rsi14 = 50.0
        elif average_loss <= 0:
            rsi14 = 100.0
        else:
            rsi14 = 100.0 - 100.0 / (1.0 + average_gain / average_loss)
        ma20_values = [
            sum(close_values[end - 20 : end]) / 20.0
            for end in range(20, len(close_values) + 1)
        ]
        slope_days = min(5, len(ma20_values) - 1)
        base_ma = ma20_values[-1 - slope_days]
        ma20_slope = 0.0 if base_ma <= 0 else (ma20_values[-1] / base_ma - 1.0) * 100.0 / slope_days
        recent = close_values[-20:]
        ma20 = sum(recent) / 20.0
        variance = sum((value - ma20) ** 2 for value in recent) / 20.0
        standard_deviation = math.sqrt(variance)
        lower = ma20 - 2.0 * standard_deviation
        upper = ma20 + 2.0 * standard_deviation
        width = upper - lower
        bollinger_position = 0.5 if width <= 0 else (close_values[-1] - lower) / width
        range_low = min(low_values[-20:])
        action = self.evaluate_range_action(
            adx14=adx14,
            ma20_slope_pct_per_day=ma20_slope,
            price=close_values[-1],
            range_low=range_low,
            rsi14=rsi14,
            bollinger_position=bollinger_position,
            above_vwap=close_values[-1] >= float(intraday_vwap),
        )
        return {
            "adx14": round(adx14, 6),
            "rsi14": round(rsi14, 6),
            "ma20": round(ma20, 6),
            "ma20_slope_pct_per_day": round(ma20_slope, 6),
            "bollinger_lower": round(lower, 6),
            "bollinger_upper": round(upper, 6),
            "bollinger_position": round(bollinger_position, 6),
            "range_low": round(range_low, 6),
            "above_vwap": close_values[-1] >= float(intraday_vwap),
            **action,
        }

    def has_sufficient_net_edge(
        self,
        *,
        expected_gross_edge_pct: float,
        estimated_round_trip_cost_pct: float,
    ) -> bool:
        required = max(0.0, float(estimated_round_trip_cost_pct)) + self.config.min_edge_buffer_pct
        return float(expected_gross_edge_pct) >= required

    def evaluate_account_risk(
        self,
        risk: AccountRiskState,
        *,
        has_position: bool,
    ) -> Dict[str, object]:
        if risk.account_drawdown_pct >= self.config.max_account_drawdown_pct:
            return {"buy_allowed": False, "force_exit": False, "reason": "account_drawdown_fuse"}
        if risk.daily_pnl_pct <= self.config.daily_loss_limit_pct:
            return {"buy_allowed": False, "force_exit": False, "reason": "daily_loss_fuse"}
        if risk.in_loss_streak_cooldown or risk.consecutive_losses >= self.config.loss_streak_limit:
            return {"buy_allowed": False, "force_exit": False, "reason": "loss_streak_cooldown"}
        if risk.total_exposure_pct >= self.config.max_total_exposure_pct:
            return {"buy_allowed": False, "force_exit": False, "reason": "total_exposure_limit"}
        if risk.theme_exposure_pct >= self.config.max_theme_exposure_pct:
            return {"buy_allowed": False, "force_exit": False, "reason": "theme_exposure_limit"}
        if risk.symbol_exposure_pct >= self.config.max_symbol_exposure_pct:
            return {"buy_allowed": False, "force_exit": False, "reason": "symbol_exposure_limit"}
        if not has_position and risk.position_count >= self.config.max_positions:
            return {"buy_allowed": False, "force_exit": False, "reason": "position_count_limit"}
        return {"buy_allowed": True, "force_exit": False, "reason": "risk_limits_passed"}

    def target_entry_position_pct(
        self,
        risk: AccountRiskState,
        *,
        order_cap_pct: Optional[float] = None,
    ) -> float:
        available = min(
            self.config.max_total_exposure_pct - risk.total_exposure_pct,
            self.config.max_theme_exposure_pct - risk.theme_exposure_pct,
            self.config.max_symbol_exposure_pct - risk.symbol_exposure_pct,
        )
        order_cap = (
            self.config.max_symbol_exposure_pct
            if order_cap_pct is None
            else max(0.0, float(order_cap_pct))
        )
        return round(
            max(0.0, min(order_cap, available)),
            6,
        )

    def decide(self, request: StrategyDecisionInput) -> StrategyDecision:
        """Resolve one auditable action using the strategy's fixed priority order."""

        theme = str(request.theme or "").strip().lower()
        open_state = self.classify_cn_open(request.cn_gap_pct)
        risk_gate = self.evaluate_account_risk(request.risk, has_position=request.has_position)

        if request.has_position and bool(risk_gate["force_exit"]):
            return self._sell_decision(request, fraction=1.0, reason=str(risk_gate["reason"]))

        position_return = _finite_float(request.position_return_pct)
        if request.has_position and position_return is not None:
            if position_return <= self.config.stop_loss_pct:
                return self._sell_decision(request, fraction=1.0, reason="hard_stop_loss")

        korea_gate = dict(request.korea_gate or {})
        if request.has_position and theme in KR_LINKED_THEMES:
            kr_sell_fraction = max(0.0, min(1.0, float(korea_gate.get("sell_fraction") or 0.0)))
            if kr_sell_fraction > 0:
                return self._sell_decision(
                    request,
                    fraction=kr_sell_fraction,
                    reason=str(korea_gate.get("reason") or "korea_decline_confirmed"),
                )

        nasdaq_futures = dict(request.nasdaq_futures_signal or {})
        if request.has_position and theme in NASDAQ_FUTURES_LINKED_THEMES:
            nq_sell_fraction = max(
                0.0,
                min(1.0, float(nasdaq_futures.get("sell_fraction") or 0.0)),
            )
            if (
                nasdaq_futures.get("available") is True
                and nasdaq_futures.get("confirmed") is True
                and nq_sell_fraction > 0
            ):
                return self._sell_decision(
                    request,
                    fraction=nq_sell_fraction,
                    reason=str(
                        nasdaq_futures.get("reason")
                        or "nasdaq_futures_severe_downtrend"
                    ),
                )

        high_open_wait_reason: Optional[str] = None
        if request.has_position and bool(open_state["force_sell"]):
            high_open_exit = dict(request.next_day_high_open_exit_signal or {})
            if high_open_exit.get("eligible") is True and high_open_exit.get("action") == "sell":
                high_open_sell_fraction = _finite_float(
                    high_open_exit.get("sell_fraction")
                )
                return self._sell_decision(
                    request,
                    fraction=(
                        max(0.0, min(1.0, high_open_sell_fraction))
                        if high_open_sell_fraction is not None
                        else 1.0
                    ),
                    reason=str(
                        high_open_exit.get("reason")
                        or "next_day_high_open_trailing_exit"
                    ),
                )
            if high_open_exit.get("eligible") is True:
                high_open_wait_reason = str(
                    high_open_exit.get("reason")
                    or "next_day_high_open_waiting_for_intraday_high"
                )

        if request.has_position and position_return is not None and position_return >= self.config.take_profit_pct:
            return self._sell_decision(request, fraction=0.5, reason="take_profit")
        range_signal = dict(request.range_signal or {})
        if request.has_position and range_signal.get("action") in {"sell", "exit"}:
            fraction = (
                1.0
                if range_signal.get("action") == "exit"
                else 1.0 / max(1, int(request.current_tranche_count or 0))
            )
            return self._sell_decision(request, fraction=fraction, reason="range_exit_signal")

        if (
            request.has_position
            and position_return is not None
            and position_return > 0
            and request.pullback_from_peak_pct
            >= self.config.trailing_stop_pullback_pct
        ):
            return self._sell_decision(request, fraction=1.0, reason="trailing_stop")

        if request.has_position and high_open_wait_reason:
            return StrategyDecision(action="hold", reason=high_open_wait_reason)

        if not bool(risk_gate["buy_allowed"]):
            return StrategyDecision(action="hold" if request.has_position else "blocked", reason=str(risk_gate["reason"]))
        board_technical = dict(request.board_technical_signal or {})
        entry_phase = str(request.entry_phase or "opening").strip().lower()
        failed_breakout = dict(request.failed_breakout_signal or {})
        if failed_breakout.get("confirmed") is True:
            return StrategyDecision(
                action="hold" if request.has_position else "blocked",
                reason=str(
                    failed_breakout.get("reason")
                    or "long_pressure_failed_breakout_below_vwap"
                ),
            )
        staged_add = bool(
            request.has_position
            and entry_phase == "intraday_dip"
            and int(request.current_tranche_count or 0) < self.config.range_max_tranches
            and float(request.strategy_cost_basis_pct)
            <= self.config.staged_entry_tranche_pct + 2.5
        )
        if request.has_position and not staged_add:
            return StrategyDecision(action="hold", reason="no_sell_signal")
        if entry_phase not in {"opening", "intraday_dip"}:
            return StrategyDecision(
                action="hold" if request.has_position else "blocked",
                reason="entry_phase_invalid",
            )
        if open_state["regime"] == "extreme_low_open":
            return StrategyDecision(
                action="blocked",
                reason="cn_extreme_low_open_buy_blocked",
            )
        if open_state["regime"] == "high_open":
            if entry_phase != "intraday_dip":
                return StrategyDecision(
                    action="blocked",
                    reason="cn_high_open_buy_blocked",
                )
            pullback = dict(request.intraday_pullback_signal or {})
            if (
                pullback.get("available") is not True
                or pullback.get("confirmed") is not True
                or not (request.reclaimed_open or request.above_vwap)
            ):
                return StrategyDecision(
                    action="hold" if request.has_position else "blocked",
                    reason="cn_high_open_pullback_unconfirmed",
                )

        close_theme_ready = self._us_close_theme_ready(request)
        intraday_dip_ready = self._premarket_intraday_dip_ready(request)
        if open_state["regime"] == "flat_open":
            range_buy_ready = bool(
                range_signal.get("regime") == "range"
                and range_signal.get("action") == "buy"
            )
            if theme not in TRADEABLE_THEMES and not range_buy_ready:
                return StrategyDecision(
                    action="hold" if request.has_position else "blocked",
                    reason="flat_open_range_or_strong_cross_market_signal_required",
                )
            if board_technical.get("available") is not True:
                return StrategyDecision(
                    action="hold" if request.has_position else "blocked",
                    reason="flat_open_board_technical_required",
                )

        effective_sector_score = float(request.sector_signal_score)
        support_score = _finite_float(board_technical.get("support_score"))
        if (
            open_state["regime"] == "flat_open"
            and board_technical.get("supportive") is True
            and support_score is not None
        ):
            effective_sector_score += min(
                self.config.board_support_score_bonus_max,
                max(0.0, support_score) / 100.0
                * self.config.board_support_score_bonus_max,
            )
        if theme == "cpo":
            cpo_signal = dict(request.cpo_signal or {})
            cpo_score = _finite_float(cpo_signal.get("score"))
            if bool(cpo_signal.get("available")) and cpo_score is not None:
                effective_sector_score += 0.10 * cpo_score
        if theme in NASDAQ_FUTURES_LINKED_THEMES:
            futures_adjustment = _finite_float(
                nasdaq_futures.get("sector_score_adjustment")
            )
            if futures_adjustment is not None:
                effective_sector_score += max(-5.0, min(5.0, futures_adjustment))
        rotation = dict(request.rotation_signal or {})
        if (
            theme in TECHNOLOGY_WEIGHTED_THEMES
            and rotation.get("tailwind") is True
            and board_technical.get("supportive") is True
        ):
            effective_sector_score += 5.0
        low_position = dict(request.low_position_signal or {})
        if low_position.get("available") is True:
            if low_position.get("confirmed") is True:
                effective_sector_score += 5.0
            else:
                range_percentile = _finite_float(low_position.get("range_percentile"))
                if range_percentile is not None and range_percentile >= 75.0:
                    effective_sector_score -= 5.0
        effective_sector_score = max(0.0, min(100.0, effective_sector_score))
        required_sector_score = self.config.minimum_sector_score
        core_leader_override = bool(
            effective_sector_score < required_sector_score
            and self._core_leader_divergence_ready(request)
        )
        if effective_sector_score < required_sector_score and not core_leader_override:
            return StrategyDecision(action="blocked", reason="sector_signal_too_weak")

        if theme in TECHNOLOGY_WEIGHTED_THEMES:
            nasdaq_block_reason = self._nasdaq_futures_entry_block_reason(request)
            if nasdaq_block_reason:
                return StrategyDecision(
                    action="hold" if request.has_position else "blocked",
                    reason=nasdaq_block_reason,
                )
            if entry_phase == "opening" and not close_theme_ready:
                return StrategyDecision(
                    action="blocked",
                    reason=f"{theme}_us_close_theme_unconfirmed",
                )
            if entry_phase == "intraday_dip" and not intraday_dip_ready:
                return StrategyDecision(
                    action="blocked",
                    reason=f"{theme}_premarket_close_dip_unconfirmed",
                )
            asia_gate = dict(request.asia_market_gate or {})
            if not bool(asia_gate.get("buy_allowed")):
                return StrategyDecision(
                    action="blocked",
                    reason=str(
                        asia_gate.get("reason")
                        or "asia_market_buy_signal_unconfirmed"
                    ),
                )
        if theme == "cpo":
            cpo_signal = dict(request.cpo_signal or {})
            if (
                cpo_signal.get("available") is not True
                or cpo_signal.get("supportive") is not True
            ):
                return StrategyDecision(
                    action="hold" if request.has_position else "blocked",
                    reason=str(
                        cpo_signal.get("reason")
                        or "cpo_us_close_signal_unconfirmed"
                    ),
                )
        signal_order_cap_pct: Optional[float] = None
        primary_board = (
            board_technical.get("primary_board")
            if isinstance(board_technical.get("primary_board"), Mapping)
            else {}
        )
        pressure_windows = {
            int(value)
            for value in list(primary_board.get("pressure_windows") or [])
            if _finite_float(value) is not None
        }
        if (
            pressure_windows & {20, 30, 60}
            and primary_board.get("breakout_confirmed") is not True
        ):
            signal_order_cap_pct = self.config.reduced_entry_tranche_pct
        if theme == "gold":
            gold_signal = dict(request.gold_signal or {})
            if not bool(gold_signal.get("buy_allowed")):
                return StrategyDecision(
                    action="blocked",
                    reason=str(gold_signal.get("reason") or "gold_signal_unconfirmed"),
                )
        range_buy_confirmed = bool(
            range_signal.get("regime") == "range"
            and range_signal.get("action") == "buy"
        )
        if (
            theme in DOMESTIC_ROTATION_THEMES
            and not range_buy_confirmed
            and not self._core_leader_divergence_ready(request)
            and not request.reclaimed_open
            and not request.above_vwap
        ):
            unconfirmed_cap = self.config.unconfirmed_intraday_entry_cap_pct
            signal_order_cap_pct = min(
                float(signal_order_cap_pct)
                if signal_order_cap_pct is not None
                else float(unconfirmed_cap),
                float(unconfirmed_cap),
            )
        if open_state["regime"] == "flat_open":
            return self._buy_decision(
                request,
                reason=(
                    "range_add_tranche"
                    if staged_add and range_buy_confirmed
                    else f"{theme or 'theme'}_flat_open_staged_add_confirmed"
                    if staged_add
                    else "range_low_buy"
                    if range_buy_confirmed
                    else f"{theme or 'theme'}_core_leader_divergence_flat_open_staged_entry_confirmed"
                    if self._core_leader_divergence_ready(request)
                    else f"{theme or 'theme'}_flat_open_staged_entry_confirmed"
                ),
                order_cap_pct=signal_order_cap_pct,
            )
        return self._buy_decision(
            request,
            reason=(
                "range_add_tranche"
                if staged_add and range_buy_confirmed
                else "range_low_buy"
                if range_buy_confirmed
                else f"{theme}_domestic_rotation_entry_confirmed"
                if theme in DOMESTIC_ROTATION_THEMES
                else f"{theme or 'theme'}_premarket_close_intraday_dip_confirmed"
                if entry_phase == "intraday_dip"
                else f"{theme or 'theme'}_us_close_opening_entry_confirmed"
            ),
            order_cap_pct=signal_order_cap_pct,
        )

    @staticmethod
    def _nasdaq_futures_entry_block_reason(
        request: StrategyDecisionInput,
    ) -> Optional[str]:
        theme = str(request.theme or "").strip().lower()
        if theme not in NASDAQ_FUTURES_LINKED_THEMES:
            return None
        signal = dict(request.nasdaq_futures_signal or {})
        if signal.get("available") is not True:
            return str(
                signal.get("reason") or "nasdaq_futures_signal_unavailable"
            )
        if signal.get("confirmed") is not True:
            return str(
                signal.get("reason") or "nasdaq_futures_trend_unconfirmed"
            )
        if signal.get("buy_allowed") is not True:
            return str(
                signal.get("reason") or "nasdaq_futures_downtrend_blocks_entry"
            )
        return None

    def _us_tech_entry_block_reason(
        self,
        request: StrategyDecisionInput,
    ) -> Optional[str]:
        theme = str(request.theme or "").strip().lower()
        if theme not in KR_LINKED_THEMES:
            return None
        score = _finite_float(request.us_tech_score)
        if score is None:
            return "us_tech_signal_unavailable"
        if score < self.config.us_buy_score:
            return "us_tech_score_too_weak"
        return None

    @staticmethod
    def _asia_market_entry_block_reason(
        request: StrategyDecisionInput,
    ) -> Optional[str]:
        theme = str(request.theme or "").strip().lower()
        if theme not in TECHNOLOGY_WEIGHTED_THEMES:
            return None
        gate = dict(request.asia_market_gate or {})
        if gate.get("buy_allowed") is True:
            return None
        return str(gate.get("reason") or "asia_market_buy_signal_unconfirmed")

    def is_us_close_theme_signal_ready(
        self,
        signal: Mapping[str, object],
    ) -> bool:
        signal = dict(signal or {})
        score = _finite_float(signal.get("score"))
        sector_change = _finite_float(signal.get("sector_change_pct"))
        advancing_ratio = _finite_float(signal.get("advancing_ratio"))
        leader_change = _finite_float(signal.get("leader_change_pct"))
        if None in {score, sector_change, advancing_ratio, leader_change}:
            return False
        return bool(
            signal.get("available") is True
            and signal.get("strong") is True
            and score >= self.config.us_close_strong_score
            and sector_change >= self.config.us_close_min_sector_change_pct
            and advancing_ratio >= self.config.us_close_min_advancing_ratio
            and leader_change >= self.config.us_close_min_leader_change_pct
        )

    def is_us_premarket_theme_signal_ready(
        self,
        signal: Mapping[str, object],
    ) -> bool:
        signal = dict(signal or {})
        score = _finite_float(signal.get("score"))
        sector_change = _finite_float(signal.get("sector_change_pct"))
        advancing_ratio = _finite_float(signal.get("advancing_ratio"))
        leader_change = _finite_float(signal.get("leader_change_pct"))
        if None in {score, sector_change, advancing_ratio, leader_change}:
            return False
        return bool(
            signal.get("available") is True
            and signal.get("strong") is True
            and score >= self.config.us_premarket_strong_score
            and sector_change >= self.config.us_premarket_min_sector_change_pct
            and advancing_ratio >= self.config.us_premarket_min_advancing_ratio
            and leader_change >= self.config.us_premarket_min_leader_change_pct
        )

    @staticmethod
    def is_asia_supply_chain_signal_ready(
        theme: str,
        signal: Mapping[str, object],
    ) -> bool:
        if str(theme or "").strip().lower() not in {"mlcc", "ccl"}:
            return False
        signal = dict(signal or {})
        score = _finite_float(signal.get("score"))
        sector_change = _finite_float(signal.get("sector_change_pct"))
        advancing_ratio = _finite_float(signal.get("advancing_ratio"))
        return bool(
            signal.get("available") is True
            and signal.get("strong") is True
            and score is not None
            and score >= 40.0
            and sector_change is not None
            and sector_change >= 0.3
            and advancing_ratio is not None
            and advancing_ratio >= 0.6
        )

    @staticmethod
    def _us_theme_allows_asia_supplement(
        *,
        theme: str,
        signal: Mapping[str, object],
        session_stage: str,
    ) -> bool:
        """Revalidate the exact US coverage shortfall before substitution."""

        normalized_theme = str(theme or "").strip().lower()
        normalized_stage = str(session_stage or "").strip().lower()
        expected_reason = {
            "premarket": "premarket_theme_coverage_insufficient",
            "close": "us_close_theme_coverage_insufficient",
        }.get(normalized_stage)
        signal = dict(signal or {})
        snapshot = signal.get("snapshot")
        if (
            normalized_theme not in {"mlcc", "ccl"}
            or expected_reason is None
            or signal.get("asia_supplement_eligible") is not True
            or signal.get("available") is not False
            or signal.get("reason") != expected_reason
            or not isinstance(snapshot, Mapping)
        ):
            return False
        session_date = str(signal.get("session_date") or "").strip()
        signal_theme = str(signal.get("signal_theme") or "").strip()
        if (
            not session_date
            or not signal_theme
            or str(snapshot.get("session_date") or "").strip() != session_date
            or str(snapshot.get("session_stage") or "").strip().lower()
            != normalized_stage
            or str(snapshot.get("observed_at") or "").strip()
            != str(signal.get("observed_at") or "").strip()
        ):
            return False
        theme_signals = snapshot.get("theme_signals")
        if not isinstance(theme_signals, Mapping):
            return False
        stored_signal = theme_signals.get(signal_theme)
        return bool(
            isinstance(stored_signal, Mapping)
            and stored_signal.get("available") is False
            and stored_signal.get("reason") == expected_reason
        )

    def is_asia_supplemented_us_theme_signal_ready(
        self,
        *,
        theme: str,
        us_signal: Mapping[str, object],
        asia_signal: Mapping[str, object],
        session_stage: str,
    ) -> bool:
        return bool(
            self._us_theme_allows_asia_supplement(
                theme=theme,
                signal=us_signal,
                session_stage=session_stage,
            )
            and self.is_asia_supply_chain_signal_ready(theme, asia_signal)
        )

    def _us_close_theme_ready(
        self,
        request: StrategyDecisionInput,
    ) -> bool:
        close_signal = dict(request.us_close_theme_signal or {})
        if self.is_us_close_theme_signal_ready(close_signal):
            return True
        return self.is_asia_supplemented_us_theme_signal_ready(
            theme=request.theme,
            us_signal=close_signal,
            asia_signal=dict(request.asia_supply_chain_signal or {}),
            session_stage="close",
        )

    def _premarket_intraday_dip_ready(
        self,
        request: StrategyDecisionInput,
    ) -> bool:
        signal = dict(request.us_premarket_signal or {})
        close_signal = dict(request.us_close_theme_signal or {})
        pullback = dict(request.intraday_pullback_signal or {})
        board = dict(request.board_technical_signal or {})
        us_premarket_ready = self.is_us_premarket_theme_signal_ready(signal)
        asia_supply_ready = self.is_asia_supply_chain_signal_ready(
            request.theme,
            dict(request.asia_supply_chain_signal or {}),
        )
        asia_premarket_ready = bool(
            asia_supply_ready
            and self.is_asia_supplemented_us_theme_signal_ready(
                theme=request.theme,
                us_signal=signal,
                asia_signal=dict(request.asia_supply_chain_signal or {}),
                session_stage="premarket",
            )
        )
        if not us_premarket_ready and not asia_premarket_ready:
            return False
        if not self._us_close_theme_ready(request):
            return False
        if close_signal.get("premarket_reversal_invalidated") is True:
            return False
        if pullback.get("available") is not True or pullback.get("confirmed") is not True:
            return False
        if board.get("available") is not True:
            return False
        pullback_pct = _finite_float(pullback.get("pullback_from_high_pct"))
        if pullback_pct is None:
            return False
        return bool(
            (us_premarket_ready or asia_premarket_ready)
            and pullback_pct >= self.config.intraday_pullback_min_pct
        )

    def _core_leader_divergence_ready(
        self,
        request: StrategyDecisionInput,
    ) -> bool:
        """Allow one small intraday entry when a verified leader outruns a flat board."""

        signal = dict(request.core_leader_signal or {})
        return bool(
            signal.get("available") is True
            and signal.get("confirmed") is True
            and not request.has_position
            and str(request.entry_phase or "").strip().lower() == "intraday_dip"
            and self.classify_cn_open(request.cn_gap_pct)["regime"] == "flat_open"
            and str(request.theme or "").strip().lower()
            in (GLOBAL_MARKET_LINKED_THEMES - {"cpo"})
        )

    def _short_resistance_core_leader_ready(
        self,
        request: StrategyDecisionInput,
        *,
        board_technical: Mapping[str, object],
    ) -> bool:
        """Allow a first tranche through only a 5/10-day resistance warning."""

        return bool(
            board_technical.get("near_resistance") is True
            and board_technical.get("short_resistance_only") is True
            and not list(board_technical.get("medium_long_pressure_boards") or [])
            and self._core_leader_divergence_ready(request)
        )

    def _buy_decision(
        self,
        request: StrategyDecisionInput,
        *,
        reason: str,
        order_cap_pct: Optional[float] = None,
    ) -> StrategyDecision:
        entry_score = _finite_float(request.entry_score)
        entry_grade: Optional[str] = None
        if entry_score is not None:
            if entry_score >= self.config.entry_score_a:
                entry_grade = "A"
            elif entry_score >= self.config.entry_score_b:
                entry_grade = "B"
            elif entry_score >= self.config.entry_score_late_probe:
                if str(request.analysis_slot or "").strip() not in {
                    "10:40",
                    "14:30",
                }:
                    return StrategyDecision(
                        action="blocked",
                        reason="entry_score_late_probe_only",
                        entry_score=round(entry_score, 6),
                        entry_grade="C",
                    )
                entry_grade = "C"
            else:
                return StrategyDecision(
                    action="blocked",
                    reason="entry_score_below_threshold",
                    entry_score=round(entry_score, 6),
                )
        if not self.has_sufficient_net_edge(
            expected_gross_edge_pct=request.expected_gross_edge_pct,
            estimated_round_trip_cost_pct=request.estimated_round_trip_cost_pct,
        ):
            return StrategyDecision(
                action="blocked",
                reason="insufficient_net_edge",
                entry_score=entry_score,
                entry_grade=entry_grade,
            )
        staged_cap = self.config.staged_entry_tranche_pct
        if entry_grade == "C":
            staged_cap = min(staged_cap, self.config.reduced_entry_tranche_pct)
        if order_cap_pct is not None:
            staged_cap = min(staged_cap, max(0.0, float(order_cap_pct)))
        target_pct = self.target_entry_position_pct(
            request.risk,
            order_cap_pct=staged_cap,
        )
        if target_pct <= 0:
            return StrategyDecision(
                action="blocked",
                reason="position_capacity_exhausted",
                entry_score=entry_score,
                entry_grade=entry_grade,
            )
        return StrategyDecision(
            action="buy",
            reason=reason,
            buy_allowed=True,
            target_tranche_delta=1,
            target_position_pct=target_pct,
            entry_score=(round(entry_score, 6) if entry_score is not None else None),
            entry_grade=entry_grade,
        )

    @staticmethod
    def _sell_decision(
        request: StrategyDecisionInput,
        *,
        fraction: float,
        reason: str,
    ) -> StrategyDecision:
        sellable = max(0.0, min(1.0, float(request.sellable_fraction)))
        executable = min(max(0.0, min(1.0, fraction)), sellable)
        if executable <= 0:
            return StrategyDecision(
                action="hold",
                reason="t_plus_one_position_not_sellable",
                deferred_action="exit" if fraction >= 1.0 else "reduce",
            )
        return StrategyDecision(
            action="exit" if executable >= 1.0 else "reduce",
            reason=reason,
            sell_fraction=round(executable, 6),
            target_tranche_delta=-3 if executable >= 1.0 else -1,
        )


class RealisticMinuteExecutionModel:
    """Minute-bar execution with costs, dynamic slippage and bounded liquidity."""

    def __init__(
        self,
        *,
        fee_schedule: Optional[TradeFeeSchedule] = None,
        max_minute_participation: float = 0.05,
        minimum_slippage_bps: float = 2.0,
        maximum_slippage_bps: float = 50.0,
    ) -> None:
        if not 0 < max_minute_participation <= 1:
            raise ValueError("max_minute_participation must be in (0, 1]")
        self.fee_schedule = fee_schedule or TradeFeeSchedule()
        self.max_minute_participation = float(max_minute_participation)
        self.minimum_slippage_bps = float(minimum_slippage_bps)
        self.maximum_slippage_bps = float(maximum_slippage_bps)

    def execute(self, order: PaperOrder, bar: MinuteBar) -> PaperFill:
        side = str(order.side or "").strip().lower()
        if side not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")
        requested = _finite_float(order.quantity)
        if requested is None or requested <= 0:
            raise ValueError("quantity must be positive")
        if side == "buy" and str(order.market or "").strip().lower() == "cn":
            declaration_reason = self._cn_buy_declaration_reason(order, requested)
            if declaration_reason:
                return self._empty_fill(declaration_reason, requested)
        signal_at = _as_utc(order.signal_at)
        bar_at = _as_utc(bar.timestamp)
        if order.cancel_requested:
            return self._empty_fill("order_cancelled_before_execution", requested, status="cancelled")
        if bar_at <= signal_at:
            return self._empty_fill("bar_not_after_signal", requested)
        if bar.suspended or bar.volume <= 0:
            return self._empty_fill("security_not_tradable", requested)
        if not self._valid_bar(bar):
            return self._empty_fill("invalid_minute_bar", requested)
        effective_bar = self._with_cn_price_limits(order, bar)
        if self._outside_price_limits(effective_bar):
            return self._empty_fill("minute_bar_outside_price_limit", requested)
        if self._blocked_by_one_price_limit(side, effective_bar):
            return self._empty_fill("one_price_limit_no_liquidity", requested)

        limit = _finite_float(order.limit_price)
        if limit is not None and not self._limit_touched(side, limit, effective_bar):
            return self._empty_fill("limit_not_touched", requested)

        executable = min(requested, float(effective_bar.volume) * self.max_minute_participation)
        if side == "sell":
            sellable = _finite_float(order.sellable_quantity)
            if sellable is None:
                return self._empty_fill("sellable_quantity_required", requested)
            executable = min(executable, max(0.0, sellable))
            if executable <= 0:
                return self._empty_fill("t_plus_one_no_sellable_quantity", requested)
        if str(order.market or "").strip().lower() == "cn":
            executable = math.floor(executable)
            if executable <= 0:
                return self._empty_fill("below_cn_minimum_fill", requested)

        participation = executable / float(effective_bar.volume)
        slippage_bps = self._slippage_bps(bar=effective_bar, participation=participation)
        reference = float(effective_bar.vwap)
        impacted = reference * (1.0 + slippage_bps / 10000.0 if side == "buy" else 1.0 - slippage_bps / 10000.0)
        fill_price = min(max(impacted, float(effective_bar.low)), float(effective_bar.high))
        if limit is not None:
            fill_price = min(fill_price, limit) if side == "buy" else max(fill_price, limit)
        if not (float(effective_bar.low) <= fill_price <= float(effective_bar.high)):
            return self._empty_fill("limit_not_executable", requested)

        notional = executable * fill_price
        fees = self.fee_schedule.calculate(
            side=side,
            notional=notional,
            instrument_type=order.instrument_type,
        )
        net_cash_change = -(notional + fees["total"]) if side == "buy" else notional - fees["total"]
        unfilled = max(0.0, requested - executable)
        return PaperFill(
            status="filled" if unfilled <= 1e-9 else "part_filled",
            reason=None if unfilled <= 1e-9 else "minute_participation_cap",
            requested_quantity=requested,
            filled_quantity=round(executable, 8),
            unfilled_quantity=round(unfilled, 8),
            reference_price=round(reference, 8),
            fill_price=round(fill_price, 8),
            slippage_bps=round(slippage_bps, 6),
            notional=round(notional, 6),
            fees=fees,
            net_cash_change=round(net_cash_change, 6),
        )

    def _slippage_bps(self, *, bar: MinuteBar, participation: float) -> float:
        spread_value = _finite_float(bar.bid_ask_spread_bps)
        spread = 4.0 if spread_value is None else max(0.0, spread_value)
        volatility = max(0.0, _finite_float(bar.atr_1m_pct) or 0.0)
        raw = 0.5 * spread + min(20.0, 15.0 * volatility) + 20.0 * math.sqrt(max(0.0, participation))
        return max(self.minimum_slippage_bps, min(self.maximum_slippage_bps, raw))

    @staticmethod
    def _cn_buy_declaration_reason(order: PaperOrder, requested: float) -> Optional[str]:
        declared = _finite_float(order.declared_quantity)
        declared = requested if declared is None else declared
        if declared <= 0 or abs(declared - round(declared)) > 1e-9:
            return "invalid_cn_buy_declaration_quantity"
        code = str(order.symbol or "").strip().upper().split(".", 1)[0]
        if code.startswith(("688", "689")):
            return None if declared >= 200 else "below_cn_board_lot"
        if code.startswith("920"):
            return None if declared >= 100 else "below_cn_board_lot"
        if declared < 100 or abs(declared % 100.0) > 1e-9:
            return "below_cn_board_lot"
        return None

    @classmethod
    def _with_cn_price_limits(cls, order: PaperOrder, bar: MinuteBar) -> MinuteBar:
        if str(order.market or "").strip().lower() != "cn":
            return bar
        previous_close = _finite_float(order.previous_close)
        if previous_close is None or previous_close <= 0:
            return bar
        limit_pct = _finite_float(order.price_limit_pct)
        if limit_pct is None:
            limit_pct = cls._cn_price_limit_pct(order.symbol)
        if limit_pct is None or limit_pct <= 0:
            return bar
        limit_up = cls._round_price(previous_close * (1.0 + limit_pct / 100.0))
        limit_down = cls._round_price(previous_close * (1.0 - limit_pct / 100.0))
        return MinuteBar(
            **{
                **bar.__dict__,
                "limit_up_price": bar.limit_up_price or limit_up,
                "limit_down_price": bar.limit_down_price or limit_down,
            }
        )

    @staticmethod
    def _cn_price_limit_pct(symbol: str) -> Optional[float]:
        code = str(symbol or "").strip().upper().split(".", 1)[0]
        if code.startswith(("688", "689", "300", "301")):
            return 20.0
        if code.startswith(("4", "8", "920")):
            return 30.0
        if len(code) == 6 and code.isdigit():
            return 10.0
        return None

    @staticmethod
    def _round_price(value: float) -> float:
        return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

    @staticmethod
    def _valid_bar(bar: MinuteBar) -> bool:
        values = [_finite_float(value) for value in (bar.open, bar.high, bar.low, bar.close, bar.volume)]
        if any(value is None for value in values):
            return False
        return bar.low > 0 and bar.low <= bar.open <= bar.high and bar.low <= bar.close <= bar.high

    @staticmethod
    def _blocked_by_one_price_limit(side: str, bar: MinuteBar) -> bool:
        one_price = abs(float(bar.high) - float(bar.low)) <= 1e-12
        if not one_price:
            return False
        limit_up = _finite_float(bar.limit_up_price)
        limit_down = _finite_float(bar.limit_down_price)
        if side == "buy" and limit_up is not None and abs(float(bar.high) - limit_up) <= 1e-8:
            return True
        return side == "sell" and limit_down is not None and abs(float(bar.low) - limit_down) <= 1e-8

    @staticmethod
    def _outside_price_limits(bar: MinuteBar) -> bool:
        limit_up = _finite_float(bar.limit_up_price)
        limit_down = _finite_float(bar.limit_down_price)
        if limit_up is not None and float(bar.high) > limit_up + 1e-8:
            return True
        return limit_down is not None and float(bar.low) < limit_down - 1e-8

    @staticmethod
    def _limit_touched(side: str, limit: float, bar: MinuteBar) -> bool:
        return float(bar.low) <= limit if side == "buy" else float(bar.high) >= limit

    @staticmethod
    def _empty_fill(reason: str, requested: float, *, status: str = "unfilled") -> PaperFill:
        return PaperFill(
            status=status,
            reason=reason,
            requested_quantity=requested,
            filled_quantity=0.0,
            unfilled_quantity=requested,
            reference_price=None,
            fill_price=None,
            slippage_bps=None,
            notional=0.0,
            fees={"commission": 0.0, "stamp_tax": 0.0, "transfer_fee": 0.0, "total": 0.0},
            net_cash_change=0.0,
        )
