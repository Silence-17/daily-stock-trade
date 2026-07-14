# -*- coding: utf-8 -*-
"""AlphaSift stock screening API routes."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from api.deps import get_config_dep
from api.v1.errors import api_error
from src.config import Config
from src.repositories.stock_selection_factor_snapshot_repo import (
    StockSelectionFactorSnapshotRepository,
)
from src.services.alphasift_service import AlphaSiftService
from src.services.stock_selection_strategy_replay_service import (
    StockSelectionStrategyReplayService,
)
from src.services.stock_selection_portfolio_backtest_service import (
    StockSelectionPortfolioBacktestService,
)
from src.services.stock_selection_factor_ingestion_service import (
    StockSelectionFactorIngestionService,
)
from src.services.stock_selection_historical_universe_service import (
    StockSelectionHistoricalUniverseService,
)
from src.services.stock_selection_full_market_ingestion_service import (
    StockSelectionFullMarketIngestionService,
)
from src.services.task_queue import TaskStatus as QueueTaskStatus
from src.services.task_queue import get_task_queue

router = APIRouter()


class AlphaSiftScreenRequest(BaseModel):
    market: str = Field("cn", min_length=1, max_length=16)
    strategy: str = Field("dual_low", min_length=1, max_length=64)
    max_results: int = Field(20, ge=1, le=100)


class AlphaSiftStrategyResponse(BaseModel):
    id: str
    name: str = ""
    title: str = ""
    description: str = ""
    category: str = ""
    tag: str = ""
    tags: List[str] = Field(default_factory=list)
    market_scope: List[str] = Field(default_factory=list)
    market: str = ""


class AlphaSiftScreenAccepted(BaseModel):
    task_id: str
    trace_id: str
    status: str = "pending"
    message: str
    strategy: str
    market: str
    max_results: int


class AlphaSiftScreenTaskStatus(BaseModel):
    task_id: str
    trace_id: Optional[str] = None
    status: str
    progress: int = 0
    message: Optional[str] = None
    error: Optional[str] = None
    result: Optional[Dict[str, Any]] = None


class AlphaSiftFactorSnapshotRow(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=16)
    name: Optional[str] = Field(None, max_length=100)
    industry: Optional[str] = Field(None, max_length=100)
    price: Optional[float] = None
    change_pct: Optional[float] = None
    amount: Optional[float] = None
    volume: Optional[float] = None
    turnover_rate: Optional[float] = None
    volume_ratio: Optional[float] = None
    pe_ratio: Optional[float] = None
    pb_ratio: Optional[float] = None
    total_mv: Optional[float] = None
    factors: Dict[str, Any] = Field(default_factory=dict)
    source: Dict[str, Any] = Field(default_factory=dict)
    quality_status: str = Field("unknown", min_length=1, max_length=32)
    missing_fields: List[str] = Field(default_factory=list)


class AlphaSiftFactorSnapshotImportRequest(BaseModel):
    market: str = Field("cn", min_length=1, max_length=16)
    snapshot_date: date
    rows: List[AlphaSiftFactorSnapshotRow] = Field(..., min_length=1, max_length=10000)


class AlphaSiftReplayRequest(BaseModel):
    strategy: str = Field("dual_low", min_length=1, max_length=64)
    market: str = Field("cn", min_length=1, max_length=16)
    snapshot_date: date
    max_results: int = Field(20, ge=1, le=100)
    min_hard_coverage: float = Field(0.95, gt=0, le=1)
    min_score_coverage: float = Field(0.80, gt=0, le=1)


class AlphaSiftPortfolioCorporateAction(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=16)
    effective_date: date
    action_type: str = Field(..., pattern="^(cash_dividend|split_adjustment)$")
    cash_dividend_per_share: Optional[float] = Field(None, gt=0)
    split_ratio: Optional[float] = Field(None, gt=0)


class AlphaSiftPortfolioBacktestRequest(BaseModel):
    strategy: str = Field("dual_low", min_length=1, max_length=64)
    market: str = Field("cn", min_length=1, max_length=16)
    date_from: date
    date_to: date
    top_k: int = Field(5, ge=1, le=100)
    final_holding_bars: int = Field(20, ge=1, le=250)
    initial_capital: float = Field(100000, gt=0)
    commission_bps: float = Field(3, ge=0, le=1000)
    minimum_commission: float = Field(0, ge=0)
    sell_tax_bps: float = Field(0, ge=0, le=1000)
    slippage_bps: float = Field(5, ge=0, le=1000)
    benchmark_symbol: Optional[str] = Field(None, min_length=1, max_length=16)
    enforce_tradeability: bool = True
    accounting_mode: str = Field("cash_ledger", pattern="^(cash_ledger|equal_weight_approximation)$")
    target_weights: Dict[str, float] = Field(default_factory=dict)
    corporate_actions: List[AlphaSiftPortfolioCorporateAction] = Field(default_factory=list, max_length=500)
    include_persisted_corporate_actions: bool = True
    min_hard_coverage: float = Field(0.95, gt=0, le=1)
    min_score_coverage: float = Field(0.80, gt=0, le=1)


class AlphaSiftHistoricalUniverseItem(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=16)
    name: str = Field(..., min_length=1, max_length=100)
    industry: Optional[str] = Field(None, max_length=100)


class AlphaSiftFactorIngestionRequest(BaseModel):
    market: str = Field("cn", min_length=1, max_length=16)
    snapshot_dates: List[date] = Field(..., min_length=1, max_length=50)
    universe: List[AlphaSiftHistoricalUniverseItem] = Field(..., min_length=1, max_length=50)


class AlphaSiftFactorIngestionAccepted(BaseModel):
    task_id: str
    trace_id: str
    status: str = "pending"
    message: str
    market: str
    snapshot_count: int
    symbol_count: int


class AlphaSiftFullMarketIngestionRequest(BaseModel):
    market: str = Field("cn", min_length=1, max_length=16)
    snapshot_dates: List[date] = Field(..., min_length=1, max_length=50)
    batch_size: int = Field(25, ge=1, le=50)


def _service(config: Config) -> AlphaSiftService:
    return AlphaSiftService(config=config)


def _screening_task_not_found(task_id: str) -> HTTPException:
    return api_error(
        404,
        "alphasift_screen_task_not_found",
        f"选股任务 {task_id} 不存在或已过期",
    )


@router.get("/status")
def alphasift_status(config: Config = Depends(get_config_dep)) -> Dict[str, Any]:
    return _service(config).status()


@router.get("/strategies")
def alphasift_strategies(
    request: Request,
    config: Config = Depends(get_config_dep),
) -> Dict[str, Any]:
    return _service(config).strategies()


@router.post("/replay/snapshots")
def alphasift_import_factor_snapshot(
    payload: AlphaSiftFactorSnapshotImportRequest,
) -> Dict[str, Any]:
    """Import normalized point-in-time rows without fetching current market data."""
    try:
        result = StockSelectionFactorSnapshotRepository().upsert_many(
            market=payload.market,
            snapshot_date=payload.snapshot_date,
            rows=[row.model_dump() for row in payload.rows],
        )
        return {
            **result,
            "market": payload.market.lower(),
            "snapshot_date": payload.snapshot_date.isoformat(),
        }
    except ValueError as exc:
        raise api_error(400, "invalid_factor_snapshot", str(exc)) from exc


@router.get("/replay/universe")
def alphasift_resolve_historical_universe(
    snapshot_date: date = Query(...),
    market: str = Query("cn", min_length=1, max_length=16),
    limit: int = Query(6000, ge=1, le=6000),
) -> Dict[str, Any]:
    try:
        return StockSelectionHistoricalUniverseService().resolve(
            market=market,
            snapshot_date=snapshot_date,
            limit=limit,
        )
    except ValueError as exc:
        raise api_error(400, "historical_universe_invalid", str(exc)) from exc
    except RuntimeError as exc:
        raise api_error(424, "historical_universe_unavailable", str(exc)) from exc


def _submit_full_market_ingestion_job(job_id: str, *, force: bool = False) -> str:
    task_id = uuid.uuid4().hex
    task_queue = get_task_queue()

    def run_ingestion() -> Dict[str, Any]:
        service = StockSelectionFullMarketIngestionService(
            progress_callback=lambda progress, message: task_queue.update_task_progress(
                task_id, progress, message
            )
        )
        return service.run(job_id, task_id=task_id, force=force)

    task_queue.submit_background_task(
        run_ingestion,
        stock_code=f"full-market-{job_id}",
        stock_name="AlphaSift full-market historical factors",
        report_type="alphasift_full",
        message="full-market historical factor ingestion submitted",
        task_id=task_id,
        trace_id=job_id,
    )
    return task_id


@router.post("/replay/full-market-ingestion/jobs", status_code=202)
def alphasift_create_full_market_ingestion_job(
    payload: AlphaSiftFullMarketIngestionRequest,
) -> Dict[str, Any]:
    try:
        job = StockSelectionFullMarketIngestionService().create(
            market=payload.market,
            snapshot_dates=payload.snapshot_dates,
            batch_size=payload.batch_size,
        )
        task_id = _submit_full_market_ingestion_job(job["job_id"])
        return {**job, "task_id": task_id}
    except ValueError as exc:
        raise api_error(400, "full_market_ingestion_invalid", str(exc)) from exc
    except RuntimeError as exc:
        raise api_error(424, "full_market_ingestion_unavailable", str(exc)) from exc


@router.get("/replay/full-market-ingestion/jobs")
def alphasift_list_full_market_ingestion_jobs(
    limit: int = Query(20, ge=1, le=100),
) -> Dict[str, Any]:
    return {
        "items": StockSelectionFullMarketIngestionService().list_recent(limit=limit),
        "limit": limit,
    }


@router.get("/replay/full-market-ingestion/jobs/{job_id}")
def alphasift_get_full_market_ingestion_job(job_id: str) -> Dict[str, Any]:
    try:
        return StockSelectionFullMarketIngestionService().get(job_id)
    except ValueError as exc:
        raise api_error(404, "full_market_ingestion_job_not_found", str(exc)) from exc


@router.post("/replay/full-market-ingestion/jobs/{job_id}/resume", status_code=202)
def alphasift_resume_full_market_ingestion_job(
    job_id: str,
    force: bool = Query(False),
) -> Dict[str, Any]:
    service = StockSelectionFullMarketIngestionService()
    try:
        job = service.get(job_id)
        if job["status"] == "completed":
            return job
        if job["status"] == "processing" and not force:
            raise api_error(409, "full_market_ingestion_job_active", "job is already processing")
        task_id = _submit_full_market_ingestion_job(job_id, force=force)
        return {**job, "task_id": task_id, "status": "pending"}
    except HTTPException:
        raise
    except ValueError as exc:
        raise api_error(404, "full_market_ingestion_job_not_found", str(exc)) from exc


@router.post(
    "/replay/ingestion/tasks",
    status_code=202,
    response_model=AlphaSiftFactorIngestionAccepted,
)
def alphasift_start_factor_ingestion_task(
    payload: AlphaSiftFactorIngestionRequest,
) -> AlphaSiftFactorIngestionAccepted:
    task_id = uuid.uuid4().hex
    task_queue = get_task_queue()

    def run_ingestion() -> Dict[str, Any]:
        task_queue.update_task_progress(task_id, 10, "正在抓取历史日线与估值因子")
        result = StockSelectionFactorIngestionService().ingest(
            market=payload.market,
            snapshot_dates=payload.snapshot_dates,
            universe=[item.model_dump() for item in payload.universe],
        )
        task_queue.update_task_progress(task_id, 95, "历史因子快照已写入")
        return result

    task = task_queue.submit_background_task(
        run_ingestion,
        stock_code="alphasift_factor_ingestion",
        stock_name=f"{payload.market} / {len(payload.universe)} symbols",
        report_type="alphasift_factor_ingestion",
        message="历史因子采集任务已提交",
        task_id=task_id,
        trace_id=task_id,
    )
    return AlphaSiftFactorIngestionAccepted(
        task_id=task.task_id,
        trace_id=task.trace_id or task.task_id,
        status=task.status.value if isinstance(task.status, QueueTaskStatus) else str(task.status),
        message=task.message or "历史因子采集任务已提交",
        market=payload.market.lower(),
        snapshot_count=len(payload.snapshot_dates),
        symbol_count=len(payload.universe),
    )


@router.get("/replay/ingestion/tasks/{task_id}", response_model=AlphaSiftScreenTaskStatus)
def alphasift_factor_ingestion_task_status(task_id: str) -> AlphaSiftScreenTaskStatus:
    task = get_task_queue().get_task(task_id)
    if task is None or task.report_type != "alphasift_factor_ingestion":
        raise api_error(404, "alphasift_ingestion_task_not_found", "历史因子采集任务不存在或已过期")
    result = task.result if task.status == QueueTaskStatus.COMPLETED and isinstance(task.result, dict) else None
    return AlphaSiftScreenTaskStatus(
        task_id=task.task_id,
        trace_id=task.trace_id,
        status=task.status.value if isinstance(task.status, QueueTaskStatus) else str(task.status),
        progress=task.progress,
        message=task.message,
        error=task.error,
        result=result,
    )


@router.get("/replay/compatibility")
def alphasift_replay_compatibility(
    strategy: str = Query("dual_low", min_length=1, max_length=64),
    market: str = Query("cn", min_length=1, max_length=16),
    snapshot_date: date = Query(...),
) -> Dict[str, Any]:
    try:
        return StockSelectionStrategyReplayService().compatibility(
            strategy=strategy,
            market=market,
            snapshot_date=snapshot_date,
        )
    except ValueError as exc:
        raise api_error(400, "strategy_replay_invalid", str(exc)) from exc
    except RuntimeError as exc:
        raise api_error(424, "alphasift_unavailable", str(exc)) from exc


@router.post("/replay/run")
def alphasift_run_strategy_replay(payload: AlphaSiftReplayRequest) -> Dict[str, Any]:
    try:
        return StockSelectionStrategyReplayService().replay(
            strategy=payload.strategy,
            market=payload.market,
            snapshot_date=payload.snapshot_date,
            max_results=payload.max_results,
            min_hard_coverage=payload.min_hard_coverage,
            min_score_coverage=payload.min_score_coverage,
        )
    except ValueError as exc:
        raise api_error(400, "strategy_replay_not_ready", str(exc)) from exc
    except RuntimeError as exc:
        raise api_error(424, "alphasift_unavailable", str(exc)) from exc


@router.post("/replay/portfolio-backtest")
def alphasift_run_portfolio_backtest(
    payload: AlphaSiftPortfolioBacktestRequest,
) -> Dict[str, Any]:
    try:
        return StockSelectionPortfolioBacktestService().run(
            strategy=payload.strategy,
            market=payload.market,
            date_from=payload.date_from,
            date_to=payload.date_to,
            top_k=payload.top_k,
            final_holding_bars=payload.final_holding_bars,
            initial_capital=payload.initial_capital,
            commission_bps=payload.commission_bps,
            minimum_commission=payload.minimum_commission,
            sell_tax_bps=payload.sell_tax_bps,
            slippage_bps=payload.slippage_bps,
            benchmark_symbol=payload.benchmark_symbol,
            enforce_tradeability=payload.enforce_tradeability,
            accounting_mode=payload.accounting_mode,
            target_weights=payload.target_weights,
            corporate_actions=[item.model_dump() for item in payload.corporate_actions],
            include_persisted_corporate_actions=payload.include_persisted_corporate_actions,
            min_hard_coverage=payload.min_hard_coverage,
            min_score_coverage=payload.min_score_coverage,
        )
    except ValueError as exc:
        raise api_error(400, "portfolio_backtest_not_ready", str(exc)) from exc
    except RuntimeError as exc:
        raise api_error(424, "alphasift_unavailable", str(exc)) from exc


@router.get("/hotspots")
def alphasift_hotspots(
    provider: str = Query("", max_length=32),
    top: int = Query(12, ge=1, le=50),
    refresh: bool = Query(False),
    include_details: bool = Query(False),
    config: Config = Depends(get_config_dep),
) -> Dict[str, Any]:
    refresh_value = refresh if isinstance(refresh, bool) else bool(getattr(refresh, "default", False))
    include_details_value = (
        include_details
        if isinstance(include_details, bool)
        else bool(getattr(include_details, "default", False))
    )
    return _service(config).hotspots(
        provider=provider,
        top=top,
        refresh=refresh_value,
        include_details=include_details_value,
    )


@router.get("/hotspots/{topic:path}")
def alphasift_hotspot_detail(
    topic: str,
    provider: str = Query("", max_length=32),
    refresh: bool = Query(False),
    config: Config = Depends(get_config_dep),
) -> Dict[str, Any]:
    refresh_value = refresh if isinstance(refresh, bool) else bool(getattr(refresh, "default", False))
    return _service(config).hotspot_detail(topic=topic, provider=provider, refresh=refresh_value)


@router.post("/install")
def alphasift_install(
    request: Request,
    config: Config = Depends(get_config_dep),
) -> Dict[str, Any]:
    return _service(config).install(request=request)


@router.post("/screen/tasks", status_code=202, response_model=AlphaSiftScreenAccepted)
def alphasift_start_screen_task(
    request: AlphaSiftScreenRequest,
    http_request: Request,
    config: Config = Depends(get_config_dep),
) -> AlphaSiftScreenAccepted:
    task_id = uuid.uuid4().hex
    task_queue = get_task_queue()

    def run_screen() -> Dict[str, Any]:
        task_queue.update_task_progress(
            task_id,
            20,
            "正在执行 AlphaSift 选股，外部数据源较慢时会持续后台运行",
        )
        result = _service(config).screen(
            strategy=request.strategy,
            market=request.market,
            max_results=request.max_results,
        )
        task_queue.update_task_progress(
            task_id,
            90,
            f"选股已完成，正在整理 {result.get('candidate_count', 0)} 条候选",
        )
        return result

    task = task_queue.submit_background_task(
        run_screen,
        stock_code="alphasift_screen",
        stock_name=f"{request.strategy} / {request.market}",
        report_type="alphasift_screen",
        message="AlphaSift 选股任务已提交",
        task_id=task_id,
        trace_id=task_id,
    )
    return AlphaSiftScreenAccepted(
        task_id=task.task_id,
        trace_id=task.trace_id or task.task_id,
        status=task.status.value if isinstance(task.status, QueueTaskStatus) else str(task.status),
        message=task.message or "AlphaSift 选股任务已提交",
        strategy=request.strategy,
        market=request.market,
        max_results=request.max_results,
    )


@router.get("/screen/tasks/{task_id}", response_model=AlphaSiftScreenTaskStatus)
def alphasift_screen_task_status(task_id: str) -> AlphaSiftScreenTaskStatus:
    task = get_task_queue().get_task(task_id)
    if task is None or task.report_type != "alphasift_screen":
        raise _screening_task_not_found(task_id)

    result = task.result if task.status == QueueTaskStatus.COMPLETED and isinstance(task.result, dict) else None
    return AlphaSiftScreenTaskStatus(
        task_id=task.task_id,
        trace_id=task.trace_id or task.task_id,
        status=task.status.value if isinstance(task.status, QueueTaskStatus) else str(task.status),
        progress=task.progress,
        message=task.message,
        error=task.error,
        result=result,
    )


@router.post("/screen")
def alphasift_screen(
    request: AlphaSiftScreenRequest,
    http_request: Request,
    config: Config = Depends(get_config_dep),
) -> Dict[str, Any]:
    return _service(config).screen(
        strategy=request.strategy,
        market=request.market,
        max_results=request.max_results,
    )
