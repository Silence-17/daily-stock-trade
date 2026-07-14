# -*- coding: utf-8 -*-
"""Schemas for vn.py-style paper trading endpoints."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class VnpyPaperSettings(BaseModel):
    enabled: bool = True
    account_id: Optional[int] = None
    initial_cash: float = Field(100000.0, gt=0)
    auto_trade_enabled: bool = False
    auto_strategy: str = Field("dual_low", min_length=1, max_length=64)
    auto_market: str = Field("cn", min_length=1, max_length=16)
    auto_max_results: int = Field(3, ge=1, le=50)
    auto_cash_per_order: float = Field(10000.0, gt=0)
    auto_score_weighted_allocation_enabled: bool = False
    auto_allocation_budget: Optional[float] = Field(None, gt=0)
    auto_allocation_method: Literal["score_weighted", "score_inverse_volatility_20d", "score_inverse_volatility_20d_correlation_capped"] = "score_weighted"
    auto_risk_volatility_floor_pct: float = Field(5.0, gt=0, le=1000)
    auto_correlation_lookback_days: int = Field(60, ge=20, le=252)
    auto_correlation_min_observations: int = Field(20, ge=5, le=120)
    auto_max_pairwise_correlation: float = Field(0.85, ge=-1, le=1)
    auto_interval_minutes: int = Field(1440, ge=1, le=10080)
    auto_min_score: Optional[float] = None
    auto_skip_existing_positions: bool = True
    auto_execution_mode: Literal["paper", "vnpy_paper", "dry_run", "manual_approval"] = "paper"
    auto_max_positions: int = Field(10, ge=1, le=200)
    auto_max_single_position_value: Optional[float] = Field(None, gt=0)
    auto_max_total_position_value: Optional[float] = Field(None, gt=0)
    auto_max_total_position_pct: Optional[float] = Field(None, gt=0, le=100)
    auto_max_industry_position_value: Optional[float] = Field(None, gt=0)
    auto_max_industry_position_pct: Optional[float] = Field(None, gt=0, le=100)
    auto_daily_max_orders: Optional[int] = Field(None, ge=1, le=200)
    auto_daily_budget: Optional[float] = Field(None, gt=0)
    auto_trade_time_gate_enabled: bool = True
    auto_symbol_blacklist: List[str] = Field(default_factory=list)
    auto_exclude_st: bool = True
    auto_exclude_suspended: bool = True
    auto_exclude_price_limit: bool = True
    auto_min_turnover: Optional[float] = Field(None, gt=0)
    auto_min_data_quality_score: Optional[float] = Field(None, ge=0, le=100)
    auto_cross_run_quality_gate_enabled: bool = False
    auto_cross_run_horizon_days: int = Field(5, ge=1, le=60)
    auto_cross_run_min_mature_samples: int = Field(10, ge=1, le=500)
    auto_cross_run_min_win_rate_pct: float = Field(45.0, ge=0, le=100)
    auto_cross_run_max_decisions: int = Field(200, ge=1, le=2000)
    auto_min_cash_balance: Optional[float] = Field(None, gt=0)
    auto_max_drawdown_pct: Optional[float] = Field(None, gt=0, le=100)
    auto_market_light_gate_enabled: bool = False
    auto_market_light_block_statuses: List[Literal["red", "yellow"]] = Field(default_factory=lambda: ["red"])
    auto_failure_fuse_enabled: bool = False
    auto_failure_fuse_threshold: int = Field(3, ge=2, le=20)
    auto_sell_enabled: bool = False
    auto_stop_loss_pct: Optional[float] = Field(None, gt=0, le=1000)
    auto_take_profit_pct: Optional[float] = Field(None, gt=0, le=1000)
    auto_trailing_stop_pct: Optional[float] = Field(None, gt=0, le=100)
    auto_max_holding_days: Optional[int] = Field(None, ge=1, le=3650)
    auto_sell_position_pct: Optional[float] = Field(None, gt=0, le=100)
    auto_signal_exit_enabled: bool = False
    auto_no_progress_days: Optional[int] = Field(None, ge=1, le=3650)
    auto_no_progress_min_return_pct: Optional[float] = Field(None, ge=-100, le=1000)
    auto_rebalance_enabled: bool = False
    auto_target_position_weights: Dict[str, float] = Field(default_factory=dict)
    auto_target_industry_weights: Dict[str, float] = Field(default_factory=dict)
    auto_llm_plan_enabled: bool = False
    auto_llm_review_enabled: bool = False
    vnpy_gateway_name: Optional[str] = Field(None, max_length=64)


class VnpyPaperSettingsUpdate(BaseModel):
    enabled: Optional[bool] = None
    account_id: Optional[int] = None
    initial_cash: Optional[float] = Field(None, gt=0)
    auto_trade_enabled: Optional[bool] = None
    auto_strategy: Optional[str] = Field(None, min_length=1, max_length=64)
    auto_market: Optional[str] = Field(None, min_length=1, max_length=16)
    auto_max_results: Optional[int] = Field(None, ge=1, le=50)
    auto_cash_per_order: Optional[float] = Field(None, gt=0)
    auto_score_weighted_allocation_enabled: Optional[bool] = None
    auto_allocation_budget: Optional[float] = Field(None, gt=0)
    auto_allocation_method: Optional[Literal["score_weighted", "score_inverse_volatility_20d", "score_inverse_volatility_20d_correlation_capped"]] = None
    auto_risk_volatility_floor_pct: Optional[float] = Field(None, gt=0, le=1000)
    auto_correlation_lookback_days: Optional[int] = Field(None, ge=20, le=252)
    auto_correlation_min_observations: Optional[int] = Field(None, ge=5, le=120)
    auto_max_pairwise_correlation: Optional[float] = Field(None, ge=-1, le=1)
    auto_interval_minutes: Optional[int] = Field(None, ge=1, le=10080)
    auto_min_score: Optional[float] = None
    auto_skip_existing_positions: Optional[bool] = None
    auto_execution_mode: Optional[Literal["paper", "vnpy_paper", "dry_run", "manual_approval"]] = None
    auto_max_positions: Optional[int] = Field(None, ge=1, le=200)
    auto_max_single_position_value: Optional[float] = Field(None, gt=0)
    auto_max_total_position_value: Optional[float] = Field(None, gt=0)
    auto_max_total_position_pct: Optional[float] = Field(None, gt=0, le=100)
    auto_max_industry_position_value: Optional[float] = Field(None, gt=0)
    auto_max_industry_position_pct: Optional[float] = Field(None, gt=0, le=100)
    auto_daily_max_orders: Optional[int] = Field(None, ge=1, le=200)
    auto_daily_budget: Optional[float] = Field(None, gt=0)
    auto_trade_time_gate_enabled: Optional[bool] = None
    auto_symbol_blacklist: Optional[List[str]] = None
    auto_exclude_st: Optional[bool] = None
    auto_exclude_suspended: Optional[bool] = None
    auto_exclude_price_limit: Optional[bool] = None
    auto_min_turnover: Optional[float] = Field(None, gt=0)
    auto_min_data_quality_score: Optional[float] = Field(None, ge=0, le=100)
    auto_cross_run_quality_gate_enabled: Optional[bool] = None
    auto_cross_run_horizon_days: Optional[int] = Field(None, ge=1, le=60)
    auto_cross_run_min_mature_samples: Optional[int] = Field(None, ge=1, le=500)
    auto_cross_run_min_win_rate_pct: Optional[float] = Field(None, ge=0, le=100)
    auto_cross_run_max_decisions: Optional[int] = Field(None, ge=1, le=2000)
    auto_min_cash_balance: Optional[float] = Field(None, gt=0)
    auto_max_drawdown_pct: Optional[float] = Field(None, gt=0, le=100)
    auto_market_light_gate_enabled: Optional[bool] = None
    auto_market_light_block_statuses: Optional[List[Literal["red", "yellow"]]] = None
    auto_failure_fuse_enabled: Optional[bool] = None
    auto_failure_fuse_threshold: Optional[int] = Field(None, ge=2, le=20)
    auto_sell_enabled: Optional[bool] = None
    auto_stop_loss_pct: Optional[float] = Field(None, gt=0, le=1000)
    auto_take_profit_pct: Optional[float] = Field(None, gt=0, le=1000)
    auto_trailing_stop_pct: Optional[float] = Field(None, gt=0, le=100)
    auto_max_holding_days: Optional[int] = Field(None, ge=1, le=3650)
    auto_sell_position_pct: Optional[float] = Field(None, gt=0, le=100)
    auto_signal_exit_enabled: Optional[bool] = None
    auto_no_progress_days: Optional[int] = Field(None, ge=1, le=3650)
    auto_no_progress_min_return_pct: Optional[float] = Field(None, ge=-100, le=1000)
    auto_rebalance_enabled: Optional[bool] = None
    auto_target_position_weights: Optional[Dict[str, float]] = None
    auto_target_industry_weights: Optional[Dict[str, float]] = None
    auto_llm_plan_enabled: Optional[bool] = None
    auto_llm_review_enabled: Optional[bool] = None
    vnpy_gateway_name: Optional[str] = Field(None, max_length=64)


class VnpyPaperOrderRequest(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=16)
    side: Literal["buy", "sell"] = "buy"
    market: Literal["cn", "hk", "us", "jp", "kr", "tw"] = "cn"
    quantity: Optional[float] = Field(None, gt=0)
    cash_amount: Optional[float] = Field(None, gt=0)
    price: Optional[float] = Field(None, gt=0)
    note: Optional[str] = Field(None, max_length=160)
    execution_route: Literal["local_paper", "vnpy_bridge"] = "local_paper"


class VnpyPaperVnpyTradeCallbackRequest(BaseModel):
    vt_orderid: str = Field(..., min_length=1, max_length=128)
    vt_tradeid: Optional[str] = Field(None, max_length=128)
    symbol: Optional[str] = Field(None, min_length=1, max_length=16)
    side: Optional[Literal["buy", "sell"]] = None
    market: Optional[Literal["cn", "hk", "us", "jp", "kr", "tw"]] = None
    quantity: float = Field(..., gt=0)
    price: float = Field(..., gt=0)
    trade_date: Optional[date] = None
    fee: float = Field(0.0, ge=0)
    tax: float = Field(0.0, ge=0)
    currency: Optional[str] = Field(None, max_length=8)
    raw: Dict[str, Any] = Field(default_factory=dict)


class VnpyPaperVnpyOrderCallbackRequest(BaseModel):
    vt_orderid: str = Field(..., min_length=1, max_length=128)
    status: str = Field(..., min_length=1, max_length=32)
    symbol: Optional[str] = Field(None, min_length=1, max_length=16)
    side: Optional[Literal["buy", "sell"]] = None
    market: Optional[Literal["cn", "hk", "us", "jp", "kr", "tw"]] = None
    volume: Optional[float] = Field(None, gt=0)
    traded: Optional[float] = Field(None, ge=0)
    price: Optional[float] = Field(None, gt=0)
    rejected_reason: Optional[str] = Field(None, max_length=256)
    raw: Dict[str, Any] = Field(default_factory=dict)


class VnpyPaperVnpyAccountCallbackRequest(BaseModel):
    account_id: str = Field(..., min_length=1, max_length=128)
    balance: Optional[float] = Field(None, ge=0)
    available: Optional[float] = Field(None, ge=0)
    frozen: Optional[float] = Field(None, ge=0)
    margin: Optional[float] = Field(None, ge=0)
    close_profit: Optional[float] = None
    holding_profit: Optional[float] = None
    currency: Optional[str] = Field(None, max_length=8)
    raw: Dict[str, Any] = Field(default_factory=dict)


class VnpyPaperVnpyPositionSnapshot(BaseModel):
    symbol: Optional[str] = Field(None, min_length=1, max_length=16)
    vt_symbol: Optional[str] = Field(None, max_length=64)
    market: Optional[Literal["cn", "hk", "us", "jp", "kr", "tw"]] = None
    direction: Optional[str] = Field(None, max_length=24)
    volume: Optional[float] = Field(None, ge=0)
    quantity: Optional[float] = Field(None, ge=0)
    yd_volume: Optional[float] = Field(None, ge=0)
    frozen: Optional[float] = Field(None, ge=0)
    price: Optional[float] = Field(None, ge=0)
    avg_price: Optional[float] = Field(None, ge=0)
    pnl: Optional[float] = None
    raw: Dict[str, Any] = Field(default_factory=dict)


class VnpyPaperVnpyPositionsCallbackRequest(BaseModel):
    positions: List[VnpyPaperVnpyPositionSnapshot] = Field(default_factory=list)
    raw: Dict[str, Any] = Field(default_factory=dict)


class VnpyPaperVnpySyncResult(BaseModel):
    accepted: bool
    status: str
    message: str = ""
    reason: Optional[str] = None
    raw: Dict[str, Any] = Field(default_factory=dict)


class VnpyPaperOrderResult(BaseModel):
    accepted: bool
    status: str
    trade_id: Optional[int] = None
    account_id: Optional[int] = None
    symbol: Optional[str] = None
    side: Optional[str] = None
    quantity: Optional[float] = None
    price: Optional[float] = None
    cash_amount: Optional[float] = None
    cash_amount_base: Optional[float] = None
    cash_amount_quote: Optional[float] = None
    base_currency: Optional[str] = None
    quote_currency: Optional[str] = None
    source: str = "dsa_paper"
    message: str = ""
    reason: Optional[str] = None
    raw: Dict[str, Any] = Field(default_factory=dict)


class VnpyPaperSchedulerTaskStatus(BaseModel):
    name: str
    interval_seconds: Optional[int] = None
    initial_delay_seconds: Optional[int] = None
    running: bool = False
    last_run: Optional[Any] = None
    next_run_at: Optional[Any] = None


class VnpyPaperSchedulerTaskEvent(BaseModel):
    name: str
    status: str
    message: str = ""
    timestamp: Optional[Any] = None
    duration_seconds: Optional[float] = None
    details: Dict[str, Any] = Field(default_factory=dict)


class VnpyPaperSchedulerStatus(BaseModel):
    enabled: bool = False
    running: bool = False
    loop_running: bool = False
    schedule_times: List[str] = Field(default_factory=list)
    next_run_at: Optional[Any] = None
    last_run_at: Optional[Any] = None
    last_success_at: Optional[Any] = None
    last_error: Optional[str] = None
    last_skipped_at: Optional[Any] = None
    last_skip_reason: Optional[str] = None
    background_tasks: List[VnpyPaperSchedulerTaskStatus] = Field(default_factory=list)
    task_events: List[VnpyPaperSchedulerTaskEvent] = Field(default_factory=list)


class VnpyPaperTaskHealthItem(BaseModel):
    name: str
    label: str
    health: Literal["healthy", "warning", "error", "disabled"]
    reason: Optional[str] = None
    required: bool = False
    registered: bool = False
    running: bool = False
    interval_seconds: Optional[int] = None
    last_run: Optional[Any] = None
    next_run_at: Optional[Any] = None
    last_event_status: Optional[str] = None
    last_event_at: Optional[Any] = None
    last_event_message: Optional[str] = None
    duration_seconds: Optional[float] = None
    details: Dict[str, Any] = Field(default_factory=dict)


class VnpyPaperTaskHealthResponse(BaseModel):
    generated_at: Optional[Any] = None
    overall_health: Literal["healthy", "warning", "error", "disabled"] = "disabled"
    scheduler_enabled: bool = False
    scheduler_running: bool = False
    scheduler_loop_running: bool = False
    paper_trading_enabled: bool = False
    auto_trade_enabled: bool = False
    auto_execution_mode: Optional[str] = None
    summary: Dict[str, int] = Field(default_factory=dict)
    items: List[VnpyPaperTaskHealthItem] = Field(default_factory=list)


class VnpyPaperTaskEventListResponse(BaseModel):
    generated_at: Optional[Any] = None
    limit: int = 50
    name: Optional[str] = None
    status: Optional[str] = None
    count: int = 0
    items: List[VnpyPaperSchedulerTaskEvent] = Field(default_factory=list)


class VnpyPaperTaskEventSummaryItem(BaseModel):
    name: str
    label: Optional[str] = None
    total: int = 0
    started_count: int = 0
    completed_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    failure_rate_pct: float = 0.0
    avg_duration_seconds: Optional[float] = None
    last_event_status: Optional[str] = None
    last_event_at: Optional[Any] = None
    last_event_message: Optional[str] = None
    last_failed_at: Optional[Any] = None
    last_skipped_at: Optional[Any] = None


class VnpyPaperTaskEventSummaryResponse(BaseModel):
    generated_at: Optional[Any] = None
    limit: int = 100
    count: int = 0
    status_counts: Dict[str, int] = Field(default_factory=dict)
    items: List[VnpyPaperTaskEventSummaryItem] = Field(default_factory=list)


class VnpyPaperTaskMetricsItem(BaseModel):
    name: str
    label: Optional[str] = None
    run_count: int = 0
    completed_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    success_rate_pct: float = 0.0
    skip_rate_pct: float = 0.0
    failure_rate_pct: float = 0.0
    avg_duration_seconds: Optional[float] = None
    p95_duration_seconds: Optional[float] = None
    last_run_at: Optional[Any] = None
    last_failure_at: Optional[Any] = None


class VnpyPaperTaskMetricsDailyItem(BaseModel):
    date: str
    run_count: int = 0
    completed_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    success_rate_pct: float = 0.0
    failure_rate_pct: float = 0.0
    avg_duration_seconds: Optional[float] = None


class VnpyPaperTaskMetricsResponse(BaseModel):
    generated_at: Optional[Any] = None
    window_days: int = 30
    window_started_at: Optional[Any] = None
    window_ended_at: Optional[Any] = None
    event_count: int = 0
    run_count: int = 0
    started_count: int = 0
    completed_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    success_rate_pct: float = 0.0
    skip_rate_pct: float = 0.0
    failure_rate_pct: float = 0.0
    avg_duration_seconds: Optional[float] = None
    p95_duration_seconds: Optional[float] = None
    current_failure_streak: int = 0
    truncated: bool = False
    items: List[VnpyPaperTaskMetricsItem] = Field(default_factory=list)
    daily: List[VnpyPaperTaskMetricsDailyItem] = Field(default_factory=list)


class VnpyPaperAccountListResponse(BaseModel):
    items: List[Dict[str, Any]] = Field(default_factory=list)
    count: int = 0
    current_account_id: Optional[int] = None
    hidden_count: int = 0


class VnpyPaperArchivedAccountCleanupRequest(BaseModel):
    account_ids: List[int] = Field(default_factory=list)
    dry_run: bool = False
    include_hidden: bool = False


class VnpyPaperArchivedAccountCleanupResponse(BaseModel):
    cleaned_account_ids: List[int] = Field(default_factory=list)
    skipped: List[Dict[str, Any]] = Field(default_factory=list)
    dry_run: bool = False
    hidden_count_before: int = 0
    hidden_count_after: int = 0
    remaining_count: int = 0
    accounts: VnpyPaperAccountListResponse = Field(default_factory=VnpyPaperAccountListResponse)


class VnpyPaperStatusResponse(BaseModel):
    available: bool = True
    enabled: bool
    vnpy_available: bool
    engine: str
    mode: str = "local_paper"
    settings: VnpyPaperSettings
    account: Optional[Dict[str, Any]] = None
    snapshot: Optional[Dict[str, Any]] = None
    recent_trades: List[Dict[str, Any]] = Field(default_factory=list)
    last_auto_run: Optional[Dict[str, Any]] = None
    scheduler: VnpyPaperSchedulerStatus = Field(default_factory=VnpyPaperSchedulerStatus)
    diagnostics: Dict[str, Any] = Field(default_factory=dict)


class VnpyPaperPerformanceResponse(BaseModel):
    account: Optional[Dict[str, Any]] = None
    initial_cash: float = 0.0
    total_cash: Optional[float] = None
    total_market_value: Optional[float] = None
    total_equity: Optional[float] = None
    realized_pnl: Optional[float] = None
    unrealized_pnl: Optional[float] = None
    total_pnl: Optional[float] = None
    return_pct: Optional[float] = None
    run_window: Dict[str, Any] = Field(default_factory=dict)
    agent: Dict[str, Any] = Field(default_factory=dict)
    trade_metrics: Dict[str, Any] = Field(default_factory=dict)
    risk_metrics: Dict[str, Any] = Field(default_factory=dict)
    equity_curve: List[Dict[str, Any]] = Field(default_factory=list)
    daily_returns: List[Dict[str, Any]] = Field(default_factory=list)
    monthly_returns: List[Dict[str, Any]] = Field(default_factory=list)
    strategy_attribution: List[Dict[str, Any]] = Field(default_factory=list)
    industry_attribution: List[Dict[str, Any]] = Field(default_factory=list)
    status_counts: Dict[str, int] = Field(default_factory=dict)
    execution_mode_counts: Dict[str, int] = Field(default_factory=dict)
    data_quality_counts: Dict[str, int] = Field(default_factory=dict)
    trade_plan_status_counts: Dict[str, int] = Field(default_factory=dict)
    skip_reason_counts: Dict[str, int] = Field(default_factory=dict)
    top_skip_reasons: List[Dict[str, Any]] = Field(default_factory=list)
    traded_symbols: List[Dict[str, Any]] = Field(default_factory=list)
    diagnostics: Dict[str, Any] = Field(default_factory=dict)


class VnpyPaperTradePlanRecoverySummaryResponse(BaseModel):
    generated_at: Optional[Any] = None
    limit: int = 100
    total: int = 0
    scanned_count: int = 0
    include_terminal: bool = False
    timeout_seconds: int = 0
    retry_cooldown_seconds: int = 0
    retry_max_attempts: int = 0
    active_statuses: List[str] = Field(default_factory=list)
    status_counts: Dict[str, int] = Field(default_factory=dict)
    execution_mode_counts: Dict[str, int] = Field(default_factory=dict)
    side_counts: Dict[str, int] = Field(default_factory=dict)
    recovery_counts: Dict[str, int] = Field(default_factory=dict)
    stale_active_count: int = 0
    cancellable_count: int = 0
    retry_due_count: int = 0
    retry_cooldown_count: int = 0
    retry_limit_count: int = 0
    not_retryable_count: int = 0
    items: List[Dict[str, Any]] = Field(default_factory=list)


class VnpyPaperTradePlanRecoveryRunResponse(BaseModel):
    accepted: bool = True
    skipped: bool = False
    reason: Optional[str] = None
    expired_count: int = 0
    reconciled_count: int = 0
    protected_count: int = 0
    reconciliation_failed_count: int = 0
    scanned_count: int = 0
    attempted_count: int = 0
    submitted_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    orders: List[VnpyPaperOrderResult] = Field(default_factory=list)
    messages: List[str] = Field(default_factory=list)


class VnpyPaperAutoRunRequest(BaseModel):
    execution_mode: Optional[Literal["paper", "vnpy_paper", "dry_run", "manual_approval"]] = None
    ignore_auto_trade_enabled: bool = False


class VnpyPaperAutoRunResponse(BaseModel):
    accepted: bool
    skipped: bool = False
    reason: Optional[str] = None
    agent_run_uid: Optional[str] = None
    agent_run_id: Optional[int] = None
    strategy: str
    market: str
    candidate_count: int = 0
    planned_count: int = 0
    submitted_count: int = 0
    skipped_count: int = 0
    orders: List[VnpyPaperOrderResult] = Field(default_factory=list)
    messages: List[str] = Field(default_factory=list)


class VnpyPaperAgentRunSummary(BaseModel):
    id: int
    run_uid: str
    trigger_source: str
    status: str
    strategy: str
    market: str
    max_results: Optional[int] = None
    cash_per_order: Optional[float] = None
    min_score: Optional[float] = None
    skip_existing_positions: bool = True
    candidate_count: int = 0
    planned_count: int = 0
    submitted_count: int = 0
    skipped_count: int = 0
    message_count: int = 0
    error: Optional[str] = None
    settings: Dict[str, Any] = Field(default_factory=dict)
    diagnostics: Dict[str, Any] = Field(default_factory=dict)
    started_at: Optional[Any] = None
    completed_at: Optional[Any] = None
    created_at: Optional[Any] = None
    updated_at: Optional[Any] = None


class VnpyPaperAgentDecision(BaseModel):
    id: int
    run_id: int
    sequence: int = 0
    symbol: Optional[str] = None
    name: Optional[str] = None
    market: str
    action: str
    status: str
    reason: Optional[str] = None
    score: Optional[float] = None
    confidence: Optional[float] = None
    cash_amount: Optional[float] = None
    quantity: Optional[float] = None
    price: Optional[float] = None
    trade_id: Optional[int] = None
    rationale: Optional[str] = None
    risk_flags: List[str] = Field(default_factory=list)
    order_result: Dict[str, Any] = Field(default_factory=dict)
    raw_candidate: Dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[Any] = None


class VnpyPaperAgentTradePlan(BaseModel):
    id: int
    plan_uid: str
    run_id: int
    decision_id: Optional[int] = None
    symbol: Optional[str] = None
    name: Optional[str] = None
    market: str
    side: str
    status: str
    execution_mode: str
    planned_cash_amount: Optional[float] = None
    planned_quantity: Optional[float] = None
    planned_price: Optional[float] = None
    submitted_quantity: Optional[float] = None
    submitted_price: Optional[float] = None
    trade_id: Optional[int] = None
    skip_reason: Optional[str] = None
    risk_flags: List[str] = Field(default_factory=list)
    order_result: Dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[Any] = None
    updated_at: Optional[Any] = None


class VnpyPaperAgentTimelineEvent(BaseModel):
    stage: str
    status: str
    message: str
    timestamp: Optional[Any] = None
    details: Dict[str, Any] = Field(default_factory=dict)


class VnpyPaperAgentRunDetail(VnpyPaperAgentRunSummary):
    decisions: List[VnpyPaperAgentDecision] = Field(default_factory=list)
    trade_plans: List[VnpyPaperAgentTradePlan] = Field(default_factory=list)
    timeline: List[VnpyPaperAgentTimelineEvent] = Field(default_factory=list)


class VnpyPaperAgentRunListResponse(BaseModel):
    items: List[VnpyPaperAgentRunSummary] = Field(default_factory=list)
    limit: int
    offset: int
    total: int = 0


class VnpyPaperAgentRunRecapRequest(BaseModel):
    max_output_tokens: int = Field(800, ge=200, le=2000)


class VnpyPaperAgentRunRecapResponse(BaseModel):
    accepted: bool
    status: str
    reason: Optional[str] = None
    run_uid: str
    llm_recap: Dict[str, Any] = Field(default_factory=dict)
    run_detail: Optional[VnpyPaperAgentRunDetail] = None


class VnpyPaperAgentBacktestRequest(BaseModel):
    strategy: Optional[str] = Field(None, max_length=64)
    market: Optional[str] = Field(None, max_length=16)
    created_from: Optional[datetime] = None
    created_to: Optional[datetime] = None
    eval_windows: List[int] = Field(default_factory=lambda: [1, 5, 10, 20], min_length=1, max_length=6)
    include_skipped: bool = True
    max_decisions: int = Field(500, ge=1, le=2000)
    refresh_missing: bool = False
    neutral_band_pct: float = Field(2.0, ge=0, le=25)


class VnpyPaperAgentBacktestResponse(BaseModel):
    generated_at: datetime
    methodology: Dict[str, Any] = Field(default_factory=dict)
    filters: Dict[str, Any] = Field(default_factory=dict)
    total: int = 0
    scanned_count: int = 0
    truncated: bool = False
    refresh_attempted_count: int = 0
    status_counts: Dict[str, int] = Field(default_factory=dict)
    matrix: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    strategy_matrix: Dict[str, Dict[str, Dict[str, Any]]] = Field(default_factory=dict)
    items: List[Dict[str, Any]] = Field(default_factory=list)


class VnpyPaperAgentCrossRunQualityResponse(BaseModel):
    schema_version: int = 1
    generated_at: Optional[Any] = None
    state: str
    reason: str
    previous_state: Optional[str] = None
    transition: Optional[str] = None
    changed: bool = False
    strategy: str
    market: str
    horizon_days: int
    min_mature_samples: int
    min_win_rate_pct: float
    max_decisions: int
    sample_count: int = 0
    mature_sample_count: int = 0
    coverage_pct: Optional[float] = None
    win_rate_pct: Optional[float] = None
    average_return_pct: Optional[float] = None
    median_return_pct: Optional[float] = None
    average_max_adverse_excursion_pct: Optional[float] = None
    unable_reason_counts: Dict[str, int] = Field(default_factory=dict)
    lookahead_protection: bool = True
    source: str
    truncated: bool = False
    gate_enabled: bool = False
    gate_blocked: bool = False
    insufficient_evidence_blocks: bool = False
    error: Optional[str] = None


class VnpyPaperAgentDailySummaryResponse(BaseModel):
    generated_at: Optional[Any] = None
    date: str
    created_from: Optional[Any] = None
    created_to: Optional[Any] = None
    limit: int = 100
    total: int = 0
    scanned_count: int = 0
    run_count: int = 0
    health: str = "idle"
    candidate_count: int = 0
    planned_count: int = 0
    submitted_count: int = 0
    skipped_count: int = 0
    message_count: int = 0
    status_counts: Dict[str, int] = Field(default_factory=dict)
    strategy_counts: Dict[str, int] = Field(default_factory=dict)
    market_counts: Dict[str, int] = Field(default_factory=dict)
    execution_mode_counts: Dict[str, int] = Field(default_factory=dict)
    data_quality_counts: Dict[str, int] = Field(default_factory=dict)
    agent_review_counts: Dict[str, int] = Field(default_factory=dict)
    llm_review_counts: Dict[str, int] = Field(default_factory=dict)
    review_quality_counts: Dict[str, int] = Field(default_factory=dict)
    review_quality_flag_counts: Dict[str, int] = Field(default_factory=dict)
    review_quality_score_avg: Optional[float] = None
    workflow_status_counts: Dict[str, int] = Field(default_factory=dict)
    workflow_stage_counts: Dict[str, int] = Field(default_factory=dict)
    trade_plan_status_counts: Dict[str, int] = Field(default_factory=dict)
    side_counts: Dict[str, int] = Field(default_factory=dict)
    top_skip_reasons: List[Dict[str, Any]] = Field(default_factory=list)
    top_symbols: List[Dict[str, Any]] = Field(default_factory=list)
    latest_run: Optional[VnpyPaperAgentRunSummary] = None
    filters: Dict[str, Any] = Field(default_factory=dict)


class VnpyPaperAgentDataQualityDailyItem(BaseModel):
    date: str
    run_count: int = 0
    quality_counts: Dict[str, int] = Field(default_factory=dict)
    degraded_count: int = 0
    degraded_rate_pct: float = 0.0


class VnpyPaperAgentDataQualityTrendsResponse(BaseModel):
    generated_at: Optional[Any] = None
    window_days: int = 30
    window_started_at: Optional[Any] = None
    window_ended_at: Optional[Any] = None
    total: int = 0
    scanned_count: int = 0
    known_count: int = 0
    quality_counts: Dict[str, int] = Field(default_factory=dict)
    degraded_count: int = 0
    degraded_rate_pct: float = 0.0
    health: str = "idle"
    latest_quality: Optional[str] = None
    warning_counts: Dict[str, int] = Field(default_factory=dict)
    source_error_counts: Dict[str, int] = Field(default_factory=dict)
    source_health_items: List[Dict[str, Any]] = Field(default_factory=list)
    truncated: bool = False
    daily: List[VnpyPaperAgentDataQualityDailyItem] = Field(default_factory=list)
    filters: Dict[str, Any] = Field(default_factory=dict)


class VnpyPaperAgentRunExportResponse(BaseModel):
    generated_at: Optional[Any] = None
    limit: int
    include_details: bool = True
    count: int = 0
    items: List[Dict[str, Any]] = Field(default_factory=list)
