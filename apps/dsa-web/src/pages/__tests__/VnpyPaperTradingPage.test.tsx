import type React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { UiLanguageProvider } from '../../contexts/UiLanguageContext';
import VnpyPaperTradingPage from '../VnpyPaperTradingPage';

const getStatus = vi.hoisted(() => vi.fn());
const reconnectGateway = vi.hoisted(() => vi.fn());
const getTaskHealth = vi.hoisted(() => vi.fn());
const getTaskEvents = vi.hoisted(() => vi.fn());
const getTaskEventSummary = vi.hoisted(() => vi.fn());
const getTaskMetrics = vi.hoisted(() => vi.fn());
const ensureAccount = vi.hoisted(() => vi.fn());
const resetAccount = vi.hoisted(() => vi.fn());
const listAccounts = vi.hoisted(() => vi.fn());
const restoreAccount = vi.hoisted(() => vi.fn());
const cleanupArchivedAccounts = vi.hoisted(() => vi.fn());
const getPerformance = vi.hoisted(() => vi.fn());
const getTradePlanRecoverySummary = vi.hoisted(() => vi.fn());
const runTradePlanRecovery = vi.hoisted(() => vi.fn());
const updateSettings = vi.hoisted(() => vi.fn());
const resetFailureFuse = vi.hoisted(() => vi.fn());
const submitOrder = vi.hoisted(() => vi.fn());
const runAutoOnce = vi.hoisted(() => vi.fn());
const listAgentRuns = vi.hoisted(() => vi.fn());
const getAgentReturnRiskCalibrationTrends = vi.hoisted(() => vi.fn());
const exportAgentRuns = vi.hoisted(() => vi.fn());
const getAgentRun = vi.hoisted(() => vi.fn());
const approveTradePlan = vi.hoisted(() => vi.fn());
const retryTradePlan = vi.hoisted(() => vi.fn());
const cancelTradePlan = vi.hoisted(() => vi.fn());
const listAlertTriggers = vi.hoisted(() => vi.fn());

vi.mock('../../api/vnpyPaperTrading', () => ({
  vnpyPaperTradingApi: {
    getStatus,
    reconnectGateway,
    getTaskHealth,
    getTaskEvents,
    getTaskEventSummary,
    getTaskMetrics,
    ensureAccount,
    resetAccount,
    listAccounts,
    restoreAccount,
    cleanupArchivedAccounts,
    getPerformance,
    getTradePlanRecoverySummary,
    runTradePlanRecovery,
    updateSettings,
    resetFailureFuse,
    submitOrder,
    runAutoOnce,
    listAgentRuns,
    getAgentReturnRiskCalibrationTrends,
    exportAgentRuns,
    getAgentRun,
    approveTradePlan,
    retryTradePlan,
    cancelTradePlan,
  },
}));

vi.mock('../../api/alerts', () => ({
  alertsApi: {
    listTriggers: listAlertTriggers,
  },
}));

vi.mock('recharts', () => ({
  ResponsiveContainer: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="mock-responsive-container">{children}</div>
  ),
  LineChart: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="mock-equity-chart">{children}</div>
  ),
  CartesianGrid: () => null,
  XAxis: () => null,
  YAxis: () => null,
  Tooltip: () => null,
  Line: () => null,
}));

const statusResponse = {
  available: true,
  enabled: true,
  vnpyAvailable: false,
  engine: 'vnpy_local_paper_ledger',
  mode: 'local_paper',
  settings: {
    enabled: true,
    accountId: 1,
    initialCash: 100000,
    autoTradeEnabled: false,
    autoStrategy: 'dual_low',
    autoMarket: 'cn',
    autoMaxResults: 3,
    autoCashPerOrder: 10000,
    autoScoreWeightedAllocationEnabled: false,
    autoAllocationBudget: null,
    autoAllocationMethod: 'score_weighted',
    autoRiskVolatilityFloorPct: 5,
    autoIntervalMinutes: 1440,
    autoMinScore: null,
    autoSkipExistingPositions: true,
    autoExecutionMode: 'paper',
    autoMaxPositions: 10,
    autoMaxSinglePositionValue: null,
    autoMaxTotalPositionValue: null,
    autoMaxTotalPositionPct: null,
    autoMaxIndustryPositionValue: null,
    autoMaxIndustryPositionPct: null,
    autoDailyMaxOrders: null,
    autoDailyBudget: null,
    autoTradeTimeGateEnabled: true,
    autoSymbolBlacklist: [],
    autoExcludeSt: true,
    autoExcludeSuspended: true,
    autoExcludePriceLimit: true,
    autoMinTurnover: null,
    autoMinDataQualityScore: 60,
    autoMinCashBalance: null,
    autoMaxDrawdownPct: null,
    autoDrawdownRecoveryHysteresisPct: 0,
    autoConsecutiveLossLimit: null,
    autoConsecutiveLossCooldownMinutes: 1440,
    autoMarketLightGateEnabled: false,
    autoMarketLightBlockStatuses: ['red'],
    autoMarketContextMaxAgeDays: 7,
    autoMarketBreadthGateEnabled: false,
    autoMarketBreadthMinScore: 35,
    autoHotspotRetreatGateEnabled: false,
    autoHotspotRetreatMinDrop: 25,
    autoFailureFuseEnabled: false,
    autoFailureFuseThreshold: 3,
    autoFailureFuseAutoRecoveryEnabled: false,
    autoFailureFuseCooldownMinutes: 1440,
    autoSellEnabled: false,
    autoStopLossPct: null,
    autoTakeProfitPct: null,
    autoTrailingStopPct: null,
    autoMaxHoldingDays: null,
    autoSellPositionPct: null,
    autoSignalExitEnabled: false,
    autoNoProgressDays: null,
    autoNoProgressMinReturnPct: null,
    autoRebalanceEnabled: false,
    autoTargetPositionWeights: {},
    autoTargetIndustryWeights: {},
    autoLlmPlanEnabled: false,
    autoLlmReviewEnabled: false,
    vnpyGatewayName: null,
  },
  account: { id: 1, name: 'vn.py 模拟交易', broker: 'vnpy_paper', baseCurrency: 'CNY' },
  snapshot: {
    totalCash: 99000,
    totalMarketValue: 1000,
    totalEquity: 100000,
    unrealizedPnl: 0,
    realizedPnl: 0,
    accounts: [{
      accountId: 1,
      accountName: 'vn.py 模拟交易',
      baseCurrency: 'CNY',
      totalCash: 99000,
      totalMarketValue: 1000,
      totalEquity: 100000,
      unrealizedPnl: 0,
      realizedPnl: 0,
      positions: [{
        symbol: 'SH600519',
        market: 'cn',
        quantity: 100,
        avgCost: 10,
        lastPrice: 10,
        marketValueBase: 1000,
        unrealizedPnlPct: 0,
        priceSource: 'realtime_quote',
        priceProvider: 'unit-test',
        priceAvailable: true,
        priceStale: false,
      }],
    }],
  },
  recentTrades: [{
    id: 1,
    symbol: 'SH600519',
    tradeDate: '2026-07-01',
    side: 'buy',
    quantity: 100,
    price: 10,
    note: 'unit',
  }],
  scheduler: {
    enabled: true,
    running: false,
    loopRunning: true,
    scheduleTimes: ['18:00'],
    nextRunAt: '2026-07-02T09:30:00',
    lastRunAt: null,
    lastSuccessAt: null,
    lastError: null,
    lastSkippedAt: null,
    lastSkipReason: null,
    backgroundTasks: [{
      name: 'vnpy_paper_auto_trade',
      intervalSeconds: 300,
      initialDelaySeconds: 120,
      running: false,
      lastRun: null,
      nextRunAt: '2026-07-02T09:35:00',
    }, {
      name: 'vnpy_paper_auto_retry',
      intervalSeconds: 300,
      running: true,
      overlapGuarded: true,
      previousGenerationRunning: true,
      lastRun: null,
      nextRunAt: '2026-07-02T09:36:00',
    }],
    taskEvents: [{
      name: 'vnpy_paper_auto_trade',
      status: 'skipped',
      message: 'Background task skipped: auto_trade_disabled',
      timestamp: '2026-07-02T09:31:00',
      durationSeconds: 0.12,
      details: {
        reason: 'auto_trade_disabled',
        submittedCount: 0,
        skippedCount: 0,
      },
    }],
  },
  diagnostics: {
    backend: {
      apiVersion: '1.0.0',
      vnpyPaperContractVersion: 3,
      buildId: 'test-build-abcdef123456',
      buildSource: 'DSA_BUILD_ID',
      pythonVersion: '3.13.14',
      processStartedAt: '2026-07-15T09:00:00Z',
    },
    vnpyAdapter: {
      orderRequestSupported: false,
      cancelRequestSupported: false,
      mode: 'local_paper_fallback',
    },
    vnpyBridge: {
      available: false,
      mode: 'not_configured',
      reason: 'main_engine_not_configured',
      cancelOrderSupported: false,
    },
    vnpyRuntime: {
      enabled: false,
      available: false,
      mode: 'disabled',
      reason: 'disabled',
    },
    tradingWindow: {
      available: true,
      market: 'cn',
      phase: 'postmarket',
      isMarketOpenNow: false,
      nextWindowStatus: 'next_session',
      nextSessionDate: '2026-07-03',
      nextOpenAt: '2026-07-03T09:30:00+08:00',
      nextCloseAt: '2026-07-03T15:00:00+08:00',
      timeGateEnabled: true,
      timeGateEnforced: true,
      executionMode: 'paper',
      gateReason: null,
    },
    failureFuse: {
      enabled: false,
      open: false,
      threshold: 3,
      consecutiveFailureCount: 0,
    },
    alphasift: {
      enabled: true,
      available: true,
      version: '0.2.0',
      contractVersion: '1',
      strategyCount: 8,
    },
    systemHealth: {
      schemaVersion: 1,
      generatedAt: '2026-07-02T09:31:00+08:00',
      status: 'disabled',
      ready: false,
      nextAction: 'enable_auto_trade',
      healthScore: 72.22,
      requiredBlockers: [],
      warnings: [],
      disabled: ['auto_trade_disabled', 'snapshot_not_requested'],
      components: [{
        key: 'paper_ledger',
        label: '本地账本',
        status: 'ready',
        reason: 'paper_ledger_ready',
        detail: '本地 paper 账本可写入 Portfolio',
        required: true,
        tone: 'success',
      }, {
        key: 'selection_source',
        label: '选股来源',
        status: 'disabled',
        reason: 'auto_trade_disabled',
        detail: '自动买入关闭',
        required: false,
        tone: 'info',
      }, {
        key: 'automation_loop',
        label: '自动化调度',
        status: 'disabled',
        reason: 'auto_trade_disabled',
        detail: '自动买入关闭',
        required: false,
        tone: 'info',
      }, {
        key: 'scheduling_window',
        label: '调度窗口',
        status: 'disabled',
        reason: 'auto_trade_disabled',
        detail: '自动买入关闭',
        required: false,
        tone: 'info',
      }, {
        key: 'trading_window',
        label: '交易窗口',
        status: 'disabled',
        reason: 'auto_trade_disabled',
        detail: '自动买入关闭',
        required: false,
        tone: 'info',
      }, {
        key: 'valuation',
        label: '持仓估值',
        status: 'disabled',
        reason: 'snapshot_not_requested',
        detail: '轻量状态未拉取持仓估值',
        required: false,
        tone: 'info',
      }, {
        key: 'industry_exposure',
        label: '行业归属',
        status: 'disabled',
        reason: 'industry_coverage_not_required',
        detail: '行业风控未启用；快照字段已解析 1/2 笔（50%），缺失 1 笔：000001',
        required: false,
        tone: 'info',
        positionCount: 2,
        resolvedPositionCount: 1,
        missingPositionCount: 1,
        coveragePct: 50,
        missingSymbols: ['000001'],
      }, {
        key: 'account_drawdown',
        label: '账户回撤',
        status: 'ready',
        reason: 'account_drawdown_ready',
        detail: '当前回撤 4.5% / 上限 10%',
        required: true,
        tone: 'success',
        basis: 'observed_equity_peak',
        equity: 114600,
        peakEquity: 120000,
        drawdownPct: 4.5,
        thresholdPct: 10,
      }, {
        key: 'consecutive_losses',
        label: '连续亏损',
        status: 'ready',
        reason: 'consecutive_loss_guard_ready',
        detail: '当前连续已平仓亏损 1 / 3 笔',
        required: true,
        tone: 'success',
        currentStreak: 1,
        limit: 3,
      }, {
        key: 'vnpy_bridge',
        label: 'vn.py bridge',
        status: 'ready',
        reason: 'vnpy_bridge_not_required',
        detail: 'paper 模式不要求 vn.py bridge',
        required: false,
        tone: 'success',
      }],
    },
    autoTradeReadiness: {
      schemaVersion: 1,
      status: 'disabled',
      ready: false,
      nextAction: 'enable_auto_trade',
      autoTradeEnabled: false,
      autoExecutionMode: 'paper',
      timingAlignment: {
        nextRunAt: '2026-07-02T09:35:00',
        windowOpenAt: '2026-07-03T09:30:00+08:00',
        windowCloseAt: '2026-07-03T15:00:00+08:00',
        status: 'disabled',
        reason: 'auto_trade_disabled',
        detail: '自动买入关闭',
      },
      blockers: [],
      warnings: ['next_session'],
      disabled: ['auto_trade_disabled'],
      components: [{
        key: 'local_paper',
        label: '本地账本',
        status: 'ready',
        reason: 'local_paper_available',
        detail: '本地 paper 账本可写入 Portfolio',
        tone: 'success',
      }, {
        key: 'auto_trade',
        label: '自动交易',
        status: 'disabled',
        reason: 'auto_trade_disabled',
        detail: '后端 readiness: 自动买入关闭',
        tone: 'info',
      }, {
        key: 'alphasift',
        label: 'AlphaSift',
        status: 'ready',
        reason: 'alphasift_available',
        detail: '策略 8 个',
        tone: 'success',
      }, {
        key: 'timing_alignment',
        label: '调度窗口',
        status: 'disabled',
        reason: 'auto_trade_disabled',
        detail: '自动买入关闭',
        tone: 'info',
      }, {
        key: 'trading_window',
        label: '交易窗口',
        status: 'warning',
        reason: 'next_session',
        detail: '等待 2026-07-03T09:30:00+08:00',
        tone: 'warning',
      }, {
        key: 'vnpy_bridge',
        label: 'vn.py bridge',
        status: 'ready',
        reason: 'vnpy_bridge_not_required',
        detail: 'paper 模式不需要 vn.py bridge',
        tone: 'success',
      }],
    },
  },
  lastAutoRun: null,
};

const paperAccountListResponse = {
  items: [{
    id: 1,
    name: 'vn.py 妯℃嫙浜ゆ槗',
    broker: 'vnpy_paper',
    market: 'cn',
    baseCurrency: 'CNY',
    isActive: true,
    isCurrent: true,
    archived: false,
    createdAt: '2026-07-01T09:00:00Z',
    updatedAt: '2026-07-02T09:00:00Z',
  }, {
    id: 2,
    name: 'vn.py 妯℃嫙浜ゆ槗 old',
    broker: 'vnpy_paper',
    market: 'cn',
    baseCurrency: 'CNY',
    isActive: false,
    isCurrent: false,
    archived: true,
    createdAt: '2026-06-30T09:00:00Z',
    updatedAt: '2026-07-01T09:00:00Z',
  }],
  count: 2,
  currentAccountId: 1,
};

const agentRunSummary = {
  id: 1,
  runUid: 'ss-agent-test',
  triggerSource: 'vnpy_paper_auto',
  status: 'completed',
  strategy: 'dual_low',
  market: 'cn',
  maxResults: 3,
  cashPerOrder: 10000,
  minScore: null,
  skipExistingPositions: true,
  candidateCount: 1,
  plannedCount: 0,
  submittedCount: 1,
  skippedCount: 0,
  messageCount: 0,
  error: null,
  settings: {},
  diagnostics: {
    dataQuality: { status: 'partial' },
    agentPlan: {
      schemaVersion: 1,
      strategy: 'dual_low',
      market: 'cn',
      executionMode: 'paper',
      maxResults: 3,
      cashPerOrder: 10000,
      riskBudget: { maxPositions: 10 },
      gates: { timeGateEnforced: true },
    },
    agentSummary: {
      schemaVersion: 1,
      outcome: 'executed',
      headline: 'Agent summary: outcome=executed, candidates=1, planned=0, submitted=1, skipped=0',
      candidateCount: 1,
      plannedCount: 0,
      submittedCount: 1,
      skippedCount: 0,
      dataQualityStatus: 'partial',
      topSkipReasons: [{ reason: 'position_exists', count: 1 }],
    },
  },
  startedAt: '2026-07-01T09:30:00Z',
  completedAt: '2026-07-01T09:30:01Z',
  createdAt: '2026-07-01T09:30:00Z',
  updatedAt: '2026-07-01T09:30:01Z',
};

const agentRunDetail = {
  ...agentRunSummary,
  decisions: [{
    id: 1,
    runId: 1,
    sequence: 1,
    symbol: '600519',
    name: '贵州茅台',
    market: 'cn',
    action: 'buy',
    status: 'filled',
    reason: null,
    score: 80,
    confidence: null,
    cashAmount: 1000,
    quantity: 100,
    price: 10,
    tradeId: 2,
    rationale: '低估值且动量改善',
    riskFlags: [],
    orderResult: {},
    rawCandidate: {},
    createdAt: '2026-07-01T09:30:01Z',
  }, {
    id: 3,
    runId: 1,
    sequence: 2,
    symbol: '000001',
    name: '平安银行',
    market: 'cn',
    action: 'skip',
    status: 'skipped',
    reason: 'position_exists',
    score: 70,
    confidence: null,
    cashAmount: 1000,
    quantity: null,
    price: 10,
    tradeId: null,
    rationale: '已有持仓',
    riskFlags: ['position_exists'],
    orderResult: {},
    rawCandidate: {},
    createdAt: '2026-07-01T09:30:02Z',
  }],
  tradePlans: [{
    id: 2,
    planUid: 'plan-test',
    runId: 1,
    decisionId: 1,
    symbol: '600519',
    name: '贵州茅台',
    market: 'cn',
    side: 'buy',
    status: 'filled',
    executionMode: 'paper',
    plannedCashAmount: 1000,
    plannedQuantity: 100,
    plannedPrice: 10,
    submittedQuantity: 100,
    submittedPrice: 10,
    tradeId: 2,
    skipReason: null,
    riskFlags: [],
    orderResult: {},
    createdAt: '2026-07-01T09:30:01Z',
    updatedAt: '2026-07-01T09:30:01Z',
  }],
  timeline: [{
    stage: 'candidate_decisions',
    status: 'completed',
    message: '2 candidate decisions: buy=1, skip=1',
    timestamp: '2026-07-01T09:30:01Z',
    details: {},
  }, {
    stage: 'completed',
    status: 'completed',
    message: 'Run completed: planned=0, submitted=1, skipped=1',
    timestamp: '2026-07-01T09:30:02Z',
    details: {},
  }],
};

const pendingAgentRunDetail = {
  ...agentRunSummary,
  plannedCount: 1,
  submittedCount: 0,
  decisions: [{
    id: 1,
    runId: 1,
    sequence: 1,
    symbol: '600519',
    name: '贵州茅台',
    market: 'cn',
    action: 'buy',
    status: 'planned',
    reason: 'pending_approval',
    score: 80,
    confidence: null,
    cashAmount: 1000,
    quantity: 100,
    price: 10,
    tradeId: null,
    rationale: '低估值且动量改善',
    riskFlags: [],
    orderResult: {},
    rawCandidate: {},
    createdAt: '2026-07-01T09:30:01Z',
  }],
  tradePlans: [{
    id: 2,
    planUid: 'plan-approval',
    runId: 1,
    decisionId: 1,
    symbol: '600519',
    name: '贵州茅台',
    market: 'cn',
    side: 'buy',
    status: 'planned',
    executionMode: 'manual_approval',
    plannedCashAmount: 1000,
    plannedQuantity: 100,
    plannedPrice: 10,
    submittedQuantity: null,
    submittedPrice: null,
    tradeId: null,
    skipReason: 'pending_approval',
    riskFlags: [],
    orderResult: {},
    createdAt: '2026-07-01T09:30:01Z',
    updatedAt: '2026-07-01T09:30:01Z',
  }],
  timeline: [{
    stage: 'trade_plans',
    status: 'completed',
    message: '1 trade plans: planned=1',
    timestamp: '2026-07-01T09:30:01Z',
    details: {},
  }],
};

const skippedManualAgentRunDetail = {
  ...pendingAgentRunDetail,
  plannedCount: 0,
  skippedCount: 1,
  decisions: pendingAgentRunDetail.decisions.map((decision) => ({
    ...decision,
    status: 'skipped',
    reason: 'price_unavailable',
    riskFlags: ['price_unavailable'],
  })),
  tradePlans: pendingAgentRunDetail.tradePlans.map((plan) => ({
    ...plan,
    status: 'skipped',
    skipReason: 'price_unavailable',
  })),
  timeline: [{
    stage: 'trade_plans',
    status: 'completed',
    message: '1 trade plans: skipped=1',
    timestamp: '2026-07-01T09:30:01Z',
    details: {},
  }],
};

const skippedPaperAgentRunDetail = {
  ...skippedManualAgentRunDetail,
  tradePlans: skippedManualAgentRunDetail.tradePlans.map((plan) => ({
    ...plan,
    planUid: 'plan-paper-retry',
    executionMode: 'paper',
    orderResult: {
      retry: {
        attemptCount: 1,
        maxAttempts: 3,
        nextRetryAfter: null,
      },
    },
  })),
};

const submittedVnpyAgentRunDetail = {
  ...agentRunDetail,
  submittedCount: 1,
  tradePlans: agentRunDetail.tradePlans.map((plan) => ({
    ...plan,
    planUid: 'plan-vnpy-cancel',
    status: 'submitted',
    executionMode: 'vnpy_paper',
    tradeId: null,
    skipReason: null,
    orderResult: {
      accepted: true,
      status: 'submitted',
      source: 'vnpy_main_engine',
      raw: {
        vtOrderid: 'SIM.1',
      },
    },
  })),
};

const taskHealthResponse = {
  generatedAt: '2026-07-02T09:32:00Z',
  overallHealth: 'warning',
  schedulerEnabled: true,
  schedulerRunning: false,
  schedulerLoopRunning: true,
  paperTradingEnabled: true,
  autoTradeEnabled: true,
  autoExecutionMode: 'paper',
  summary: { healthy: 1, warning: 1, error: 0, disabled: 0, total: 2 },
  items: [{
    name: 'vnpy_paper_auto_trade',
    label: '定时自动买入',
    health: 'healthy',
    reason: 'task_registered',
    required: true,
    registered: true,
    running: false,
    intervalSeconds: 300,
    lastRun: null,
    nextRunAt: '2026-07-02T09:35:00',
    lastEventStatus: 'completed',
    lastEventAt: '2026-07-02T09:31:00',
    lastEventMessage: 'ok',
    durationSeconds: 0.12,
    details: { submittedCount: 1 },
  }, {
    name: 'vnpy_paper_auto_retry',
    label: '自动恢复扫描',
    health: 'warning',
    reason: 'auto_trade_disabled',
    required: true,
    registered: true,
    running: false,
    intervalSeconds: 300,
    lastRun: null,
    nextRunAt: '2026-07-02T09:36:00',
    lastEventStatus: 'skipped',
    lastEventAt: '2026-07-02T09:32:00',
    lastEventMessage: 'skip',
    durationSeconds: 0.1,
    details: { reason: 'auto_trade_disabled' },
  }],
};

const taskEventListResponse = {
  generatedAt: '2026-07-02T09:33:00Z',
  limit: 50,
  name: null,
  status: null,
  count: 2,
  items: [{
    name: 'vnpy_paper_auto_trade',
    status: 'completed',
    message: 'Background task completed: vnpy_paper_auto_trade',
    timestamp: '2026-07-02T09:31:00',
    durationSeconds: 0.12,
    details: {
      submittedCount: 1,
      skippedCount: 0,
    },
  }, {
    name: 'vnpy_paper_auto_retry',
    status: 'failed',
    message: 'boom',
    timestamp: '2026-07-02T09:32:00',
    durationSeconds: 0.1,
    details: {
      error: 'boom',
    },
  }],
};

const taskEventSummaryResponse = {
  generatedAt: '2026-07-02T09:34:00Z',
  limit: 100,
  count: 2,
  statusCounts: {
    completed: 1,
    skipped: 0,
    failed: 1,
    started: 0,
  },
  items: [{
    name: 'vnpy_paper_auto_retry',
    label: '自动恢复扫描',
    total: 1,
    startedCount: 0,
    completedCount: 0,
    skippedCount: 0,
    failedCount: 1,
    failureRatePct: 100,
    avgDurationSeconds: 0.1,
    lastEventStatus: 'failed',
    lastEventAt: '2026-07-02T09:32:00',
    lastEventMessage: 'boom',
    lastFailedAt: '2026-07-02T09:32:00',
    lastSkippedAt: null,
  }, {
    name: 'vnpy_paper_auto_trade',
    label: '定时自动买入',
    total: 1,
    startedCount: 0,
    completedCount: 1,
    skippedCount: 0,
    failedCount: 0,
    failureRatePct: 0,
    avgDurationSeconds: 0.12,
    lastEventStatus: 'completed',
    lastEventAt: '2026-07-02T09:31:00',
    lastEventMessage: 'Background task completed: vnpy_paper_auto_trade',
    lastFailedAt: null,
    lastSkippedAt: null,
  }],
};

const taskMetricsResponse = {
  generatedAt: '2026-07-13T10:00:00Z',
  windowDays: 30,
  windowStartedAt: '2026-06-13T10:00:00',
  windowEndedAt: '2026-07-13T10:00:00',
  eventCount: 6,
  runCount: 3,
  startedCount: 3,
  completedCount: 2,
  skippedCount: 0,
  failedCount: 1,
  successRatePct: 66.67,
  skipRatePct: 0,
  failureRatePct: 33.33,
  avgDurationSeconds: 0.2,
  p95DurationSeconds: 0.3,
  currentFailureStreak: 1,
  truncated: false,
  items: [{
    name: 'vnpy_paper_auto_trade',
    label: '定时自动买入',
    runCount: 3,
    completedCount: 2,
    skippedCount: 0,
    failedCount: 1,
    successRatePct: 66.67,
    skipRatePct: 0,
    failureRatePct: 33.33,
    avgDurationSeconds: 0.2,
    p95DurationSeconds: 0.3,
    lastRunAt: '2026-07-13T09:31:00',
    lastFailureAt: '2026-07-13T09:31:00',
  }],
  daily: [{
    date: '2026-07-13',
    runCount: 1,
    completedCount: 0,
    skippedCount: 0,
    failedCount: 1,
    successRatePct: 0,
    failureRatePct: 100,
    avgDurationSeconds: 0.3,
  }],
};

const returnRiskCalibrationTrendsResponse = {
  schemaVersion: 1,
  generatedAt: '2026-07-15T10:00:00',
  windowDays: 30,
  windowStartedAt: '2026-06-15T10:00:00',
  windowEndedAt: '2026-07-15T10:00:00',
  total: 2,
  scannedCount: 2,
  observedCount: 2,
  unknownCount: 0,
  observationRatePct: 100,
  health: 'ok',
  stateCounts: { healthy: 1, guarded: 1 },
  versionCounts: { 'candidate-return-risk-v1': 2 },
  marketCounts: { cn: 2 },
  strategyCounts: { dual_low: 2 },
  transitionCounts: {},
  appliedCount: 1,
  appliedRatePct: 50,
  gateBlockedCount: 0,
  utilityObservationCount: 2,
  averageUtilityPct: 0.2,
  minimumUtilityPct: -0.1,
  maximumUtilityPct: 0.5,
  latestMatureSampleCount: 8,
  maxMatureSampleCount: 9,
  latest: { state: 'healthy' },
  groups: [{
    key: 'cn/dual_low/candidate-return-risk-v1',
    market: 'cn',
    strategy: 'dual_low',
    version: 'candidate-return-risk-v1',
    runSnapshotCount: 2,
    stateCounts: { healthy: 1, guarded: 1 },
    utilityObservationCount: 2,
    averageUtilityPct: 0.2,
    minimumUtilityPct: -0.1,
    maximumUtilityPct: 0.5,
    latestState: 'healthy',
    latestUtilityPct: 0.5,
    latestRunUid: 'ss-agent-test',
    latestAt: '2026-07-15T10:00:00',
  }],
  daily: [{
    date: '2026-07-15',
    runSnapshotCount: 2,
    stateCounts: { healthy: 1, guarded: 1 },
    averageUtilityPct: 0.2,
  }],
  truncated: false,
  methodology: { overlappingRollingSamples: true, independentSampleCountClaimed: false },
  filters: {},
};

describe('VnpyPaperTradingPage', () => {
  beforeEach(() => {
    getStatus.mockReset();
    reconnectGateway.mockReset();
    getTaskHealth.mockReset();
    getTaskEvents.mockReset();
    getTaskEventSummary.mockReset();
    getTaskMetrics.mockReset();
    ensureAccount.mockReset();
    resetAccount.mockReset();
    listAccounts.mockReset();
    restoreAccount.mockReset();
    cleanupArchivedAccounts.mockReset();
    getPerformance.mockReset();
    getTradePlanRecoverySummary.mockReset();
    runTradePlanRecovery.mockReset();
    updateSettings.mockReset();
    resetFailureFuse.mockReset();
    submitOrder.mockReset();
    runAutoOnce.mockReset();
    listAgentRuns.mockReset();
    getAgentReturnRiskCalibrationTrends.mockReset();
    exportAgentRuns.mockReset();
    getAgentRun.mockReset();
    approveTradePlan.mockReset();
    retryTradePlan.mockReset();
    cancelTradePlan.mockReset();
    listAlertTriggers.mockReset();
    getStatus.mockResolvedValue(statusResponse);
    reconnectGateway.mockResolvedValue({
      attempted: true,
      connected: true,
      status: 'connected',
      result: 'reconnected',
      reason: null,
      connect: { connected: true, status: 'connected' },
      reconnect: { lastTrigger: 'manual', lastResult: 'reconnected' },
    });
    getTaskHealth.mockResolvedValue(taskHealthResponse);
    getTaskEvents.mockResolvedValue(taskEventListResponse);
    getTaskEventSummary.mockResolvedValue(taskEventSummaryResponse);
    getTaskMetrics.mockResolvedValue(taskMetricsResponse);
    ensureAccount.mockResolvedValue(statusResponse);
    resetFailureFuse.mockResolvedValue(statusResponse);
    listAccounts.mockResolvedValue(paperAccountListResponse);
    cleanupArchivedAccounts.mockResolvedValue({
      cleanedAccountIds: [2],
      skipped: [],
      dryRun: false,
      hiddenCountBefore: 0,
      hiddenCountAfter: 1,
      remainingCount: 1,
      accounts: {
        items: paperAccountListResponse.items.filter((account) => account.id !== 2),
        count: 1,
        currentAccountId: 1,
        hiddenCount: 1,
      },
    });
    restoreAccount.mockResolvedValue({
      ...statusResponse,
      account: { id: 2, name: 'vn.py 妯℃嫙浜ゆ槗 old', broker: 'vnpy_paper', baseCurrency: 'CNY' },
      settings: { ...statusResponse.settings, accountId: 2 },
      diagnostics: {
        accountRestore: {
          restoredAccountId: 2,
          previousAccountId: 1,
          deactivatedAccountIds: [1],
        },
      },
    });
    resetAccount.mockResolvedValue({
      ...statusResponse,
      account: { id: 2, name: 'vn.py 模拟交易', broker: 'vnpy_paper', baseCurrency: 'CNY' },
      snapshot: {
        ...statusResponse.snapshot,
        totalCash: 100000,
        totalMarketValue: 0,
        totalEquity: 100000,
        accounts: [{
          ...statusResponse.snapshot.accounts[0],
          accountId: 2,
          totalCash: 100000,
          totalMarketValue: 0,
          totalEquity: 100000,
          positions: [],
        }],
      },
      recentTrades: [],
      settings: { ...statusResponse.settings, accountId: 2 },
      diagnostics: { accountReset: { archivedAccountId: 1, newAccountId: 2 } },
    });
    getPerformance.mockResolvedValue({
      account: { id: 1, broker: 'vnpy_paper', baseCurrency: 'CNY' },
      initialCash: 100000,
      totalCash: 99000,
      totalMarketValue: 1000,
      totalEquity: 100000,
      realizedPnl: 0,
      unrealizedPnl: 0,
      totalPnl: 0,
      returnPct: 0,
      runWindow: { limit: 50, runCount: 2 },
      agent: {
        candidateCount: 3,
        plannedCount: 1,
        submittedCount: 1,
        skippedCount: 1,
        fillRatePct: 33.33,
      },
      tradeMetrics: {
        tradeCount: 2,
        buyCount: 1,
        sellCount: 1,
        grossTurnover: 2200,
        buyTurnover: 1000,
        sellTurnover: 1200,
        closedTradeCount: 1,
        sellWinCount: 1,
        sellLossCount: 0,
        winRatePct: 100,
        averageSellReturnPct: 20,
        realizedTradePnl: 200,
      },
      riskMetrics: {
        maxDrawdownPct: 3.5,
        maxDrawdownValue: 3500,
        peakEquity: 103500,
        turnoverPct: 2.2,
        currentExposurePct: 1,
        curvePointCount: 4,
      },
      equityCurve: [
        { date: '2026-07-01', source: 'trade', equity: 99000, realizedPnl: 0 },
        { date: '2026-07-02', source: 'snapshot', equity: 100000, realizedPnl: 0 },
      ],
      dailyReturns: [
        {
          date: '2026-07-01',
          equity: 99000,
          dailyPnl: -1000,
          dailyReturnPct: -1,
          cumulativeReturnPct: -1,
          drawdownPct: 1,
          tradeCount: 1,
          source: 'trade',
        },
        {
          date: '2026-07-02',
          equity: 100000,
          dailyPnl: 1000,
          dailyReturnPct: 1.010101,
          cumulativeReturnPct: 0,
          drawdownPct: 0,
          tradeCount: 1,
          source: 'snapshot',
        },
      ],
      monthlyReturns: [
        {
          month: '2026-07',
          startEquity: 100000,
          endEquity: 100000,
          monthlyPnl: 0,
          monthlyReturnPct: 0,
          cumulativeReturnPct: 0,
          drawdownPct: 0,
          tradeCount: 2,
          positiveDays: 1,
          negativeDays: 1,
        },
      ],
      strategyAttribution: [{
        key: 'dual_low',
        runCount: 2,
        filledPlanCount: 1,
        plannedCount: 1,
        skippedCount: 1,
        filledCashAmount: 1000,
        fillRatePct: 100,
      }],
      industryAttribution: [{
        key: '白酒',
        decisionCount: 2,
        filledCount: 1,
        skippedCount: 1,
        filledCashAmount: 1000,
        fillRatePct: 50,
      }],
      statusCounts: { completed: 2 },
      executionModeCounts: { paper: 2 },
      dataQualityCounts: { partial: 1 },
      tradePlanStatusCounts: { filled: 1, skipped: 1 },
      skipReasonCounts: { position_exists: 1 },
      topSkipReasons: [{ key: 'position_exists', count: 1 }],
      tradedSymbols: [{ key: '600519', count: 1 }],
    });
    getTradePlanRecoverySummary.mockResolvedValue({
      generatedAt: '2026-07-02T00:00:00Z',
      limit: 100,
      total: 2,
      scannedCount: 2,
      includeTerminal: false,
      timeoutSeconds: 1800,
      retryCooldownSeconds: 60,
      retryMaxAttempts: 3,
      activeStatuses: ['cancel_requested', 'part_filled', 'submitted'],
      statusCounts: { submitted: 1, skipped: 1 },
      executionModeCounts: { vnpy_paper: 1, paper: 1 },
      sideCounts: { buy: 2 },
      recoveryCounts: { stale_active: 1, retry_due: 1 },
      staleActiveCount: 1,
      cancellableCount: 1,
      retryDueCount: 1,
      retryCooldownCount: 0,
      retryLimitCount: 0,
      notRetryableCount: 0,
      items: [
        {
          planUid: 'recovery-stale-submitted',
          symbol: '600519',
          market: 'cn',
          side: 'buy',
          status: 'submitted',
          executionMode: 'vnpy_paper',
          plannedCashAmount: 1000,
          updatedAt: '2026-07-02T09:30:00Z',
          ageMinutes: 45,
          isActive: true,
          staleActive: true,
          staleReason: 'vnpy_order_timeout_candidate',
          cancellable: true,
          recoveryState: 'stale_active',
        },
        {
          planUid: 'recovery-retryable-skipped',
          symbol: '000001',
          market: 'cn',
          side: 'buy',
          status: 'skipped',
          executionMode: 'paper',
          skipReason: 'price_unavailable',
          plannedCashAmount: 1000,
          updatedAt: '2026-07-02T09:35:00Z',
          ageMinutes: 5,
          retryable: true,
          retryDue: true,
          recoveryState: 'retry_due',
        },
      ],
    });
    runTradePlanRecovery.mockResolvedValue({
      accepted: true,
      skipped: false,
      expiredCount: 1,
      reconciledCount: 2,
      protectedCount: 3,
      reconciliationFailedCount: 1,
      scannedCount: 3,
      attemptedCount: 2,
      submittedCount: 1,
      skippedCount: 1,
      failedCount: 0,
      orders: [],
      messages: ['vnpy_order_timeout:recovery-stale-submitted'],
    });
    updateSettings.mockResolvedValue({
      ...statusResponse,
      settings: { ...statusResponse.settings, autoTradeEnabled: true, autoIntervalMinutes: 5 },
    });
    submitOrder.mockResolvedValue({
      accepted: true,
      status: 'filled',
      tradeId: 2,
      symbol: '600519',
      side: 'buy',
      quantity: 100,
      price: 10,
      cashAmount: 1000,
      source: 'vnpy_local_paper_ledger',
      message: 'Paper order filled.',
    });
    runAutoOnce.mockResolvedValue({
      accepted: true,
      skipped: false,
      strategy: 'dual_low',
      market: 'cn',
      candidateCount: 1,
      plannedCount: 0,
      submittedCount: 1,
      skippedCount: 0,
      orders: [],
      messages: [],
    });
    listAgentRuns.mockResolvedValue({ items: [agentRunSummary], limit: 10, offset: 0 });
    getAgentReturnRiskCalibrationTrends.mockResolvedValue(returnRiskCalibrationTrendsResponse);
    listAlertTriggers.mockResolvedValue({
      items: [{
        id: 1,
        ruleId: null,
        target: 'vnpy_paper',
        observedValue: 1,
        threshold: null,
        reason: 'data_quality_stale',
        dataSource: 'vnpy_paper_auto',
        dataTimestamp: null,
        triggeredAt: '2026-07-01T09:31:00Z',
        status: 'degraded',
        diagnostics: '{"event_type":"data_quality_stale"}',
      }],
      total: 1,
      page: 1,
      pageSize: 5,
    });
    exportAgentRuns.mockResolvedValue({
      generatedAt: '2026-07-02T00:00:00Z',
      limit: 50,
      includeDetails: true,
      count: 1,
      items: [agentRunDetail],
    });
    getAgentRun.mockResolvedValue(agentRunDetail);
    approveTradePlan.mockResolvedValue({
      accepted: true,
      status: 'filled',
      tradeId: 9,
      symbol: '600519',
      side: 'buy',
      quantity: 100,
      price: 10,
      cashAmount: 1000,
      source: 'vnpy_local_paper_ledger',
      message: 'Paper order filled.',
    });
    retryTradePlan.mockResolvedValue({
      accepted: true,
      status: 'filled',
      tradeId: 10,
      symbol: '600519',
      side: 'buy',
      quantity: 100,
      price: 10,
      cashAmount: 1000,
      source: 'vnpy_local_paper_ledger',
      message: 'Paper order filled.',
    });
    cancelTradePlan.mockResolvedValue({
      accepted: true,
      status: 'cancel_requested',
      tradeId: null,
      symbol: '600519',
      side: 'buy',
      quantity: 100,
      price: 10,
      cashAmount: 1000,
      source: 'vnpy_main_engine',
      message: 'vn.py cancel request submitted.',
      reason: 'vnpy_order_cancel_requested',
    });
  });

  it('explains when the paper trading status route is missing', async () => {
    getStatus.mockRejectedValueOnce({
      response: {
        status: 404,
        statusText: 'Not Found',
        data: 'Not Found',
      },
    });

    render(
      <UiLanguageProvider>
        <VnpyPaperTradingPage />
      </UiLanguageProvider>,
    );

    expect(await screen.findByText(/后端未加载 vn\.py 模拟交易 API/)).toBeInTheDocument();
    expect(screen.getByText(/重启服务后刷新页面/)).toBeInTheDocument();
  });

  it('explains when the paper trading status request times out', async () => {
    getStatus.mockRejectedValueOnce({
      code: 'ECONNABORTED',
      message: 'timeout of 30000ms exceeded',
    });

    render(
      <UiLanguageProvider>
        <VnpyPaperTradingPage />
      </UiLanguageProvider>,
    );

    expect(await screen.findByText(/vn\.py 模拟交易状态接口超时/)).toBeInTheDocument();
    expect(screen.getByText(/检查行情\/估值数据源是否卡住/)).toBeInTheDocument();
  });

  it('shows valuation degradation when position prices are unavailable', async () => {
    const degradedStatus = {
      ...statusResponse,
      snapshot: {
        ...statusResponse.snapshot,
        limitations: [{
          code: 'price_unavailable',
          message: '部分持仓缺少价格',
        }],
        accounts: [{
          ...statusResponse.snapshot.accounts[0],
          positions: [{
            ...statusResponse.snapshot.accounts[0].positions[0],
            lastPrice: 0,
            marketValueBase: 0,
            priceAvailable: false,
            priceSource: 'unavailable',
          }],
        }],
      },
    };
    getStatus
      .mockResolvedValueOnce({ ...statusResponse, snapshot: null, recentTrades: [] })
      .mockResolvedValueOnce(degradedStatus);

    render(
      <UiLanguageProvider>
        <VnpyPaperTradingPage />
      </UiLanguageProvider>,
    );

    expect(await screen.findByText('持仓估值降级')).toBeInTheDocument();
    expect(screen.getByText(/600519 缺少可用价格/)).toBeInTheDocument();
    expect(screen.getByText('缺价')).toBeInTheDocument();
  });

  it('shows structured valuation source coverage from system health', async () => {
    const valuationDetail = '覆盖 2/3（66.67%），新鲜 1/3（33.33%）；缺价 1 笔；陈旧价格 1 笔；来源 daily_close=1、realtime=1、unavailable=1';
    const sourceHealthStatus = {
      ...statusResponse,
      diagnostics: {
        ...statusResponse.diagnostics,
        systemHealth: {
          ...statusResponse.diagnostics.systemHealth,
          components: statusResponse.diagnostics.systemHealth.components.map((item) => (
            item.key === 'valuation'
              ? {
                ...item,
                status: 'warning',
                reason: 'valuation_degraded',
                detail: valuationDetail,
                tone: 'warning',
                coveragePct: 66.67,
                freshCoveragePct: 33.33,
                sourceCounts: { daily_close: 1, realtime: 1, unavailable: 1 },
                trends: {
                  schemaVersion: 1,
                  windows: [
                    {
                      windowDays: 7,
                      observationCount: 4,
                      healthObservationCount: 3,
                      emptyPositionObservationCount: 1,
                      degradedPct: 25,
                      positionWeightedCoveragePct: 90,
                      positionWeightedFreshCoveragePct: 85,
                      averageCoveragePct: 91.25,
                      averageFreshCoveragePct: 87.5,
                      minimumCoveragePct: 75,
                      minimumFreshCoveragePct: 50,
                      latestObservedAt: '2026-07-16T08:00:00',
                      providerUsage: [{
                        provider: 'tencent',
                        positionObservationCount: 12,
                        observationCount: 4,
                        sharePct: 60,
                      }],
                    },
                    {
                      windowDays: 30,
                      observationCount: 10,
                      degradedPct: 20,
                      averageCoveragePct: 94,
                      averageFreshCoveragePct: 90,
                    },
                    {
                      windowDays: 90,
                      observationCount: 22,
                      degradedPct: 18.18,
                      averageCoveragePct: 95,
                      averageFreshCoveragePct: 92,
                    },
                  ],
                },
              }
              : item
          )),
        },
      },
    };
    getStatus
      .mockResolvedValueOnce({ ...statusResponse, snapshot: null, recentTrades: [] })
      .mockResolvedValueOnce(sourceHealthStatus);

    render(
      <UiLanguageProvider>
        <VnpyPaperTradingPage />
      </UiLanguageProvider>,
    );

    expect(await screen.findByText(valuationDetail)).toBeInTheDocument();
    const trends = screen.getByTestId('valuation-health-trends');
    expect(trends).toHaveTextContent('估值价格源趋势');
    expect(trends).toHaveTextContent('7 天');
    expect(trends).toHaveTextContent('3 有效 / 4 总计');
    expect(trends).toHaveTextContent('持仓加权覆盖 90.00%');
    expect(trends).toHaveTextContent('时间均值覆盖 91.25%');
    expect(trends).toHaveTextContent('最低覆盖 75.00%');
    expect(trends).toHaveTextContent('降级占比 25.00%');
    expect(trends).toHaveTextContent('空仓 1');
    expect(trends).toHaveTextContent('Provider tencent 60.00%');
  });

  it('shows the persisted last auto-run skip reason in availability diagnostics', async () => {
    const lastRunDetail = '最近运行 2026-07-15T09:00:00Z 跳过：outside_trading_session（agent-last-skip）';
    const lastRunStatus = {
      ...statusResponse,
      diagnostics: {
        ...statusResponse.diagnostics,
        systemHealth: {
          ...statusResponse.diagnostics.systemHealth,
          components: [
            ...statusResponse.diagnostics.systemHealth.components,
            {
              key: 'last_auto_run',
              label: '最近运行结果',
              status: 'warning',
              reason: 'outside_trading_session',
              detail: lastRunDetail,
              required: false,
              tone: 'warning',
            },
          ],
        },
      },
    };
    getStatus
      .mockResolvedValueOnce({ ...lastRunStatus, snapshot: null, recentTrades: [] })
      .mockResolvedValueOnce(lastRunStatus);

    render(
      <UiLanguageProvider>
        <VnpyPaperTradingPage />
      </UiLanguageProvider>,
    );

    const availabilityDiagnostics = await screen.findByTestId('paper-availability-diagnostics');
    expect(availabilityDiagnostics).toHaveTextContent('最近运行结果');
    expect(availabilityDiagnostics).toHaveTextContent(lastRunDetail);
  });

  it('warns when an old backend does not report the paper contract version', async () => {
    const oldBackendStatus = {
      ...statusResponse,
      diagnostics: {
        ...statusResponse.diagnostics,
        backend: undefined,
      },
    };
    getStatus
      .mockResolvedValueOnce({ ...oldBackendStatus, snapshot: null, recentTrades: [] })
      .mockResolvedValueOnce(oldBackendStatus);

    render(
      <UiLanguageProvider>
        <VnpyPaperTradingPage />
      </UiLanguageProvider>,
    );

    const availabilityDiagnostics = await screen.findByTestId('paper-availability-diagnostics');
    expect(availabilityDiagnostics).toHaveTextContent('后端版本');
    expect(availabilityDiagnostics).toHaveTextContent('需更新');
    expect(availabilityDiagnostics).toHaveTextContent('未报告版本，可能仍是旧后端进程');
  });

  it('allows an operator to reconnect an explicitly disconnected gateway', async () => {
    const disconnectedStatus = {
      ...statusResponse,
      vnpyAvailable: true,
      diagnostics: {
        ...statusResponse.diagnostics,
        vnpyRuntime: {
          enabled: true,
          available: true,
          mode: 'vnpy_runtime',
          gatewayName: 'DSA_SIM',
          connect: {
            connected: false,
            status: 'disconnected',
            reason: 'gateway_reported_disconnected',
          },
        },
      },
    };
    getStatus.mockResolvedValue(disconnectedStatus);

    render(
      <UiLanguageProvider>
        <VnpyPaperTradingPage />
      </UiLanguageProvider>,
    );

    const reconnectButton = await screen.findByRole('button', { name: '重连网关' });
    expect(reconnectButton).toBeEnabled();
    await waitFor(() => expect(listAlertTriggers).toHaveBeenCalledTimes(1));
    fireEvent.click(reconnectButton);

    await waitFor(() => expect(reconnectGateway).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(listAlertTriggers).toHaveBeenCalledTimes(2));
    expect(await screen.findByText('vn.py gateway 已重新连接')).toBeInTheDocument();
  });

  it('renders paper account status, positions, and trades', async () => {
    render(
      <UiLanguageProvider>
        <VnpyPaperTradingPage />
      </UiLanguageProvider>,
    );

    expect(await screen.findByRole('heading', { name: 'vn.py 模拟交易' })).toBeInTheDocument();
    expect(screen.getByLabelText('最低数据质量分')).toHaveValue(60);
    await waitFor(() => expect(getStatus).toHaveBeenCalledWith({
      includeSnapshot: false,
      includeRecentTrades: false,
    }));
    await waitFor(() => expect(getStatus).toHaveBeenCalledWith({
      includeSnapshot: true,
      includeRecentTrades: true,
    }));
    expect(screen.getByText('本地模拟可用')).toBeInTheDocument();
    expect(screen.getByText('vn.py 环境未安装')).toBeInTheDocument();
    const availabilityDiagnostics = screen.getByTestId('paper-availability-diagnostics');
    expect(availabilityDiagnostics).toHaveTextContent('可用性诊断');
    expect(availabilityDiagnostics).toHaveTextContent('后端版本');
    expect(availabilityDiagnostics).toHaveTextContent('API 1.0.0 · contract 3 · build test-build-abcde');
    expect(availabilityDiagnostics).toHaveTextContent('本地账本');
    expect(availabilityDiagnostics).toHaveTextContent('选股来源');
    expect(availabilityDiagnostics).toHaveTextContent('自动化调度');
    expect(availabilityDiagnostics).toHaveTextContent('调度窗口');
    expect(availabilityDiagnostics).toHaveTextContent('交易窗口');
    expect(availabilityDiagnostics).toHaveTextContent('持仓估值');
    expect(availabilityDiagnostics).toHaveTextContent('轻量状态未拉取持仓估值');
    expect(availabilityDiagnostics).toHaveTextContent('行业归属');
    expect(availabilityDiagnostics).toHaveTextContent('行业风控未启用；快照字段已解析 1/2 笔（50%），缺失 1 笔：000001');
    expect(availabilityDiagnostics).toHaveTextContent('账户回撤');
    expect(availabilityDiagnostics).toHaveTextContent('当前回撤 4.5% / 上限 10%');
    expect(availabilityDiagnostics).toHaveTextContent('paper 模式不要求 vn.py bridge');
    expect(screen.getByText('执行引擎')).toBeInTheDocument();
    expect(screen.getByText('Runtime')).toBeInTheDocument();
    expect(screen.getByText('vn.py adapter')).toBeInTheDocument();
    expect(screen.getByText('OrderRequest')).toBeInTheDocument();
    expect(screen.getByText('CancelRequest')).toBeInTheDocument();
    expect(screen.getByText('MainEngine')).toBeInTheDocument();
    expect(screen.getByText('disabled')).toBeInTheDocument();
    expect(screen.getByText('not_configured')).toBeInTheDocument();
    await waitFor(() => expect(listAccounts).toHaveBeenCalledWith(true));
    expect(screen.getByTestId('paper-account-history')).toHaveTextContent('#1');
    expect(screen.getByTestId('paper-account-history')).toHaveTextContent('#2');
    expect(screen.getByTestId('paper-account-history')).toHaveTextContent('当前');
    expect(screen.getByTestId('paper-account-history')).toHaveTextContent('已归档');
    expect(screen.getByText('调度已启动')).toBeInTheDocument();
    expect(screen.getByText('自动任务已注册')).toBeInTheDocument();
    expect(screen.getByText('重试任务运行中')).toBeInTheDocument();
    expect(screen.getByTestId('auto-retry-previous-generation-running')).toHaveTextContent(
      '配置重载前任务仍在收尾',
    );
    expect(screen.getAllByText(/09:35/).length).toBeGreaterThan(0);
    expect(screen.getByText('可交易窗口')).toBeInTheDocument();
    expect(screen.getAllByText(/09:30/).length).toBeGreaterThan(0);
    await waitFor(() => expect(getTaskHealth).toHaveBeenCalled());
    expect(screen.getByTestId('task-health-panel')).toBeInTheDocument();
    expect(screen.getByText('任务健康检查')).toBeInTheDocument();
    expect(screen.getAllByText('自动恢复扫描').length).toBeGreaterThan(0);
    expect(screen.getAllByText('auto_trade_disabled').length).toBeGreaterThan(0);
    await waitFor(() => expect(getTaskEvents).toHaveBeenCalledWith(50));
    await waitFor(() => expect(getTaskEventSummary).toHaveBeenCalledWith(100));
    await waitFor(() => expect(getTaskMetrics).toHaveBeenCalledWith(30));
    expect(screen.getByTestId('scheduler-task-events')).toBeInTheDocument();
    expect(screen.getByTestId('task-event-summary')).toBeInTheDocument();
    expect(screen.getByTestId('task-metrics')).toBeInTheDocument();
    expect(screen.getByTestId('task-metrics')).toHaveTextContent('长期稳定性');
    expect(screen.getByTestId('task-metrics')).toHaveTextContent('66.67%');
    expect(screen.getByTestId('task-metrics')).toHaveTextContent('连续失败');
    expect(screen.getAllByText('vnpy_paper_auto_trade').length).toBeGreaterThan(0);
    expect(screen.getAllByText('vnpy_paper_auto_retry').length).toBeGreaterThan(0);
    expect(screen.getAllByText('boom').length).toBeGreaterThan(0);
    expect(screen.getByTestId('task-event-summary')).toHaveTextContent('100.00%');
    fireEvent.click(screen.getByRole('button', { name: '7天' }));
    await waitFor(() => expect(getTaskMetrics).toHaveBeenCalledWith(7));
    expect(screen.getByText('submitted=1 / skipped=0')).toBeInTheDocument();
    expect(screen.getAllByText('下一交易日').length).toBeGreaterThan(0);
    expect(screen.getByText('累计收益')).toBeInTheDocument();
    expect(screen.getByText('收益率')).toBeInTheDocument();
    expect(screen.getByText('最大回撤')).toBeInTheDocument();
    expect(screen.getByText('当前连续已平仓亏损 1 / 3 笔')).toBeInTheDocument();
    expect(screen.getByText('换手率')).toBeInTheDocument();
    expect(screen.getByText('当前仓位')).toBeInTheDocument();
    expect(screen.getByText('成交额')).toBeInTheDocument();
    expect(screen.getByText('买 / 卖')).toBeInTheDocument();
    expect(screen.getByText('卖出胜率')).toBeInTheDocument();
    expect(screen.getByText('Agent 运行')).toBeInTheDocument();
    expect(screen.getByText('成交 / 计划')).toBeInTheDocument();
    expect(screen.getByText('主要跳过')).toBeInTheDocument();
    expect(screen.getByText('策略归因')).toBeInTheDocument();
    expect(screen.getByText('行业归因')).toBeInTheDocument();
    expect(screen.getByTestId('paper-performance-matrix')).toBeInTheDocument();
    expect(screen.getAllByText('dual_low').length).toBeGreaterThan(0);
    expect(screen.getAllByText('白酒').length).toBeGreaterThan(0);
    expect(screen.getByTestId('paper-equity-curve')).toBeInTheDocument();
    expect(screen.getByTestId('mock-equity-chart')).toBeInTheDocument();
    expect(screen.getByTestId('paper-daily-returns')).toBeInTheDocument();
    expect(screen.getByTestId('paper-daily-returns')).toHaveTextContent('2026-07-02');
    expect(screen.getByTestId('paper-daily-returns')).toHaveTextContent('1.01%');
    expect(screen.getByTestId('paper-monthly-returns')).toBeInTheDocument();
    expect(screen.getByTestId('paper-monthly-returns')).toHaveTextContent('2026-07');
    expect(screen.getByTestId('paper-monthly-returns')).toHaveTextContent('0.00%');
    expect(getPerformance).toHaveBeenCalledWith(50);
    await waitFor(() => expect(getTradePlanRecoverySummary).toHaveBeenCalledWith(100));
    expect(screen.getByTestId('trade-plan-recovery-matrix')).toBeInTheDocument();
    expect(screen.getByText('交易计划恢复矩阵')).toBeInTheDocument();
    expect(screen.getByText('recovery-stale-submitted')).toBeInTheDocument();
    expect(screen.getByText('vnpy_order_timeout_candidate')).toBeInTheDocument();
    expect(screen.getByText('retry_due')).toBeInTheDocument();
    await waitFor(() => expect(listAlertTriggers).toHaveBeenCalledWith({
      target: 'vnpy_paper',
      pageSize: 5,
    }));
    expect(screen.getByText('自动交易告警历史')).toBeInTheDocument();
    expect(screen.getByText('data_quality_stale')).toBeInTheDocument();
    expect(screen.getAllByText('SH600519').length).toBeGreaterThan(0);
    expect(screen.getByText('近期成交')).toBeInTheDocument();
    expect(await screen.findByText('自动选股 Agent 记录')).toBeInTheDocument();
    expect(screen.getByTestId('agent-return-risk-calibration-trends')).toBeInTheDocument();
    expect(screen.getByText('收益/风险长期校准')).toBeInTheDocument();
    expect(screen.getByText('candidate-return-risk-v1')).toBeInTheDocument();
    expect(screen.getByText('healthy 1 · guarded 1')).toBeInTheDocument();
    expect(getAgentReturnRiskCalibrationTrends).toHaveBeenCalledWith(30, undefined);
    fireEvent.click(screen.getByRole('button', { name: '7 天' }));
    await waitFor(() => expect(getAgentReturnRiskCalibrationTrends).toHaveBeenCalledWith(7, undefined));
    expect(screen.getByText('贵州茅台')).toBeInTheDocument();
    expect(screen.getByText('交易计划')).toBeInTheDocument();
    expect(screen.getByText('Agent 计划')).toBeInTheDocument();
    expect(screen.getAllByText('执行模式').length).toBeGreaterThan(0);
    expect(screen.getAllByText('paper').length).toBeGreaterThan(0);
    expect(screen.getByText(/¥10,000.00/)).toBeInTheDocument();
    expect(screen.getByText('运行总结')).toBeInTheDocument();
    expect(screen.getByText(/outcome=executed/)).toBeInTheDocument();
    expect(screen.getAllByText('position_exists ×1').length).toBeGreaterThan(0);
    expect(screen.getByLabelText(/单票金额上限/)).toBeInTheDocument();
    expect(screen.getByLabelText(/总持仓金额上限/)).toBeInTheDocument();
    expect(screen.getByLabelText('总仓位%')).toBeInTheDocument();
    expect(screen.getByLabelText(/行业金额上限/)).toBeInTheDocument();
    expect(screen.getByLabelText('行业仓位%')).toBeInTheDocument();
    expect(screen.getByLabelText('移动止损%')).toBeInTheDocument();
    expect(screen.getByLabelText('未走强天数')).toBeInTheDocument();
    expect(screen.getByLabelText('未走强收益%')).toBeInTheDocument();
    expect(screen.getByLabelText('卖出比例%')).toBeInTheDocument();
    expect(screen.getByLabelText('信号失效卖出')).toBeInTheDocument();
    expect(screen.getByLabelText('vn.py gateway')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '导出最近记录' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '导出 JSON' })).toBeInTheDocument();
    expect(screen.getByText('数据质量 partial')).toBeInTheDocument();
    expect(screen.getByText('风控统计')).toBeInTheDocument();
    expect(screen.getByText('运行时间线')).toBeInTheDocument();
    expect(screen.getByText('2 candidate decisions: buy=1, skip=1')).toBeInTheDocument();
  });

  it('filters background task events by task name and status', async () => {
    getTaskEvents
      .mockResolvedValueOnce(taskEventListResponse)
      .mockResolvedValueOnce({
        ...taskEventListResponse,
        name: 'vnpy_paper_auto_retry',
        status: 'failed',
        count: 1,
        items: [taskEventListResponse.items[1]],
      });

    render(
      <UiLanguageProvider>
        <VnpyPaperTradingPage />
      </UiLanguageProvider>,
    );

    const events = await screen.findByTestId('scheduler-task-events');
    await waitFor(() => expect(getTaskEvents).toHaveBeenCalledWith(50));

    fireEvent.change(screen.getByTestId('task-event-name-filter'), {
      target: { value: 'vnpy_paper_auto_retry' },
    });
    fireEvent.change(screen.getByTestId('task-event-status-filter'), {
      target: { value: 'failed' },
    });
    fireEvent.click(within(events).getByTestId('task-event-filter-apply'));

    await waitFor(() => expect(getTaskEvents).toHaveBeenLastCalledWith(50, {
      name: 'vnpy_paper_auto_retry',
      status: 'failed',
    }));
    const eventList = within(events).getByTestId('scheduler-task-event-list');
    await waitFor(() => {
      expect(within(eventList).getByText('boom')).toBeInTheDocument();
      expect(within(eventList).queryByText('Background task completed: vnpy_paper_auto_trade')).not.toBeInTheDocument();
    });
  });

  it('runs a trade plan recovery scan from the recovery matrix', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    render(<VnpyPaperTradingPage />);

    const matrix = await screen.findByTestId('trade-plan-recovery-matrix');
    fireEvent.click(within(matrix).getByRole('button', { name: /运行恢复扫描/ }));

    await waitFor(() => expect(runTradePlanRecovery).toHaveBeenCalledWith(5, 200));
    expect(await screen.findByText(/恢复扫描完成：对账 2，保护 3，对账异常 1/)).toBeInTheDocument();
    await waitFor(() => expect(getTradePlanRecoverySummary).toHaveBeenCalledTimes(2));
    confirmSpy.mockRestore();
  });

  it('filters performance summary by agent run creation window', async () => {
    render(
      <UiLanguageProvider>
        <VnpyPaperTradingPage />
      </UiLanguageProvider>,
    );

    const performanceFilters = await screen.findByTestId('performance-filters');
    await waitFor(() => expect(getPerformance).toHaveBeenCalledWith(50));

    fireEvent.change(screen.getByTestId('performance-created-from'), {
      target: { value: '2026-07-01T09:00' },
    });
    fireEvent.change(screen.getByTestId('performance-created-to'), {
      target: { value: '2026-07-02T15:00' },
    });
    fireEvent.submit(performanceFilters);

    await waitFor(() => expect(getPerformance).toHaveBeenLastCalledWith(50, {
      createdFrom: '2026-07-01T09:00',
      createdTo: '2026-07-02T15:00',
    }));
    expect(performanceFilters).toHaveTextContent('2026-07-01T09:00');
    expect(performanceFilters).toHaveTextContent('2026-07-02T15:00');
  });

  it('filters paper account history rows by audit status', async () => {
    render(
      <UiLanguageProvider>
        <VnpyPaperTradingPage />
      </UiLanguageProvider>,
    );

    const accountHistory = await screen.findByTestId('paper-account-history');
    await waitFor(() => expect(within(accountHistory).getByTestId('paper-account-row-1')).toBeInTheDocument());
    expect(within(accountHistory).getByTestId('paper-account-row-2')).toBeInTheDocument();

    fireEvent.click(within(accountHistory).getByTestId('paper-account-filter-archived'));
    expect(within(accountHistory).queryByTestId('paper-account-row-1')).not.toBeInTheDocument();
    expect(within(accountHistory).getByTestId('paper-account-row-2')).toBeInTheDocument();

    fireEvent.click(within(accountHistory).getByTestId('paper-account-filter-current'));
    expect(within(accountHistory).getByTestId('paper-account-row-1')).toBeInTheDocument();
    expect(within(accountHistory).queryByTestId('paper-account-row-2')).not.toBeInTheDocument();

    fireEvent.click(within(accountHistory).getByTestId('paper-account-filter-active'));
    expect(within(accountHistory).queryByTestId('paper-account-row-1')).not.toBeInTheDocument();
    expect(within(accountHistory).queryByTestId('paper-account-row-2')).not.toBeInTheDocument();
  });

  it('cleans up archived paper accounts after confirmation', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    try {
      render(
        <UiLanguageProvider>
          <VnpyPaperTradingPage />
        </UiLanguageProvider>,
      );

      const accountHistory = await screen.findByTestId('paper-account-history');
      await waitFor(() => expect(within(accountHistory).getByTestId('paper-account-row-2')).toBeInTheDocument());
      fireEvent.click(within(accountHistory).getByRole('button', { name: '清理已归档' }));

      await waitFor(() => expect(cleanupArchivedAccounts).toHaveBeenCalledWith([2]));
      expect(confirmSpy).toHaveBeenCalled();
      expect(within(accountHistory).getByTestId('paper-account-row-1')).toBeInTheDocument();
      expect(within(accountHistory).queryByTestId('paper-account-row-2')).not.toBeInTheDocument();
    } finally {
      confirmSpy.mockRestore();
    }
  });

  it('shows failure fuse diagnostics from status', async () => {
    const fusedStatus = {
      ...statusResponse,
      diagnostics: {
        ...statusResponse.diagnostics,
        failureFuse: {
          enabled: true,
          open: true,
          threshold: 2,
          consecutiveFailureCount: 2,
          recentStatuses: ['failed', 'failed'],
          recentErrors: ['alphasift_unavailable', 'alphasift_unavailable'],
        },
      },
    };
    getStatus.mockResolvedValue(fusedStatus);
    resetFailureFuse.mockResolvedValue(statusResponse);

    render(
      <UiLanguageProvider>
        <VnpyPaperTradingPage />
      </UiLanguageProvider>,
    );

    expect(await screen.findByText('已熔断 2/2')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '恢复熔断' }));
    await waitFor(() => expect(resetFailureFuse).toHaveBeenCalledTimes(1));
    expect(await screen.findByText('连续失败熔断基线已重置')).toBeInTheDocument();
  });

  it('filters agent run records from the records toolbar', async () => {
    render(
      <UiLanguageProvider>
        <VnpyPaperTradingPage />
      </UiLanguageProvider>,
    );

    fireEvent.change(await screen.findByLabelText('Agent 策略筛选'), { target: { value: 'dual_low' } });
    fireEvent.change(screen.getByLabelText('Agent 市场筛选'), { target: { value: 'cn' } });
    fireEvent.change(screen.getByLabelText('Agent 状态筛选'), { target: { value: 'completed' } });
    fireEvent.change(screen.getByLabelText('Agent 开始时间筛选'), { target: { value: '2026-07-01T09:00' } });
    fireEvent.change(screen.getByLabelText('Agent 结束时间筛选'), { target: { value: '2026-07-01T15:00' } });
    fireEvent.click(screen.getByRole('button', { name: '应用筛选' }));

    await waitFor(() => expect(listAgentRuns).toHaveBeenLastCalledWith(10, 0, {
      strategy: 'dual_low',
      market: 'cn',
      status: 'completed',
      createdFrom: '2026-07-01T09:00',
      createdTo: '2026-07-01T15:00',
    }));
  });

  it('exports recent agent runs from the records toolbar', async () => {
    const originalCreateObjectURL = URL.createObjectURL;
    const originalRevokeObjectURL = URL.revokeObjectURL;
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
    Object.defineProperty(URL, 'createObjectURL', {
      configurable: true,
      value: vi.fn(() => 'blob:agent-runs'),
    });
    Object.defineProperty(URL, 'revokeObjectURL', {
      configurable: true,
      value: vi.fn(),
    });

    try {
      render(
        <UiLanguageProvider>
          <VnpyPaperTradingPage />
        </UiLanguageProvider>,
      );

      await screen.findByRole('heading', { name: 'vn.py 模拟交易' });
      fireEvent.click(screen.getByRole('button', { name: '导出最近记录' }));

      await waitFor(() => expect(exportAgentRuns).toHaveBeenCalledWith(50, true));
      expect(await screen.findByText('已导出最近 1 条 Agent 运行记录')).toBeInTheDocument();
    } finally {
      Object.defineProperty(URL, 'createObjectURL', {
        configurable: true,
        value: originalCreateObjectURL,
      });
      Object.defineProperty(URL, 'revokeObjectURL', {
        configurable: true,
        value: originalRevokeObjectURL,
      });
      clickSpy.mockRestore();
    }
  });

  it('resets the paper account after confirmation', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    try {
      render(
        <UiLanguageProvider>
          <VnpyPaperTradingPage />
        </UiLanguageProvider>,
      );

      await screen.findByRole('heading', { name: 'vn.py 模拟交易' });
      fireEvent.click(screen.getByRole('button', { name: '重置账户' }));

      await waitFor(() => expect(resetAccount).toHaveBeenCalledWith({
        includeSnapshot: false,
        includeRecentTrades: false,
      }));
      await waitFor(() => expect(listAccounts).toHaveBeenCalledTimes(2));
      expect(confirmSpy).toHaveBeenCalled();
      expect(await screen.findByText('模拟账户已重置，旧账户已归档')).toBeInTheDocument();
    } finally {
      confirmSpy.mockRestore();
    }
  });

  it('restores an archived paper account after confirmation', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    try {
      render(
        <UiLanguageProvider>
          <VnpyPaperTradingPage />
        </UiLanguageProvider>,
      );

      await screen.findByRole('heading', { name: /vn\.py/ });
      const accountHistory = await screen.findByTestId('paper-account-history');
      fireEvent.click(within(accountHistory).getByTestId('paper-account-restore-2'));

      await waitFor(() => expect(restoreAccount).toHaveBeenCalledWith(2, {
        includeSnapshot: false,
        includeRecentTrades: false,
      }));
      await waitFor(() => expect(listAccounts).toHaveBeenCalledTimes(2));
      expect(confirmSpy).toHaveBeenCalled();
    } finally {
      confirmSpy.mockRestore();
    }
  });

  it('saves auto trading settings and submits a manual paper order', async () => {
    render(
      <UiLanguageProvider>
        <VnpyPaperTradingPage />
      </UiLanguageProvider>,
    );

    await screen.findByRole('heading', { name: 'vn.py 模拟交易' });

    fireEvent.click(screen.getByLabelText('定时自动买入'));
    fireEvent.click(screen.getByLabelText('自动卖出风控'));
    fireEvent.click(screen.getByLabelText('信号失效卖出'));
    fireEvent.click(screen.getByLabelText('组合再平衡'));
    fireEvent.click(screen.getByLabelText('评分加权分配'));
    fireEvent.change(screen.getByLabelText('每轮组合预算（CNY）'), { target: { value: '25000' } });
    fireEvent.change(screen.getByLabelText('间隔分钟'), { target: { value: '5' } });
    fireEvent.change(screen.getByLabelText('止损%'), { target: { value: '8' } });
    fireEvent.change(screen.getByLabelText('止盈%'), { target: { value: '18' } });
    fireEvent.change(screen.getByLabelText('移动止损%'), { target: { value: '12' } });
    fireEvent.change(screen.getByLabelText('最大持仓天数'), { target: { value: '20' } });
    fireEvent.change(screen.getByLabelText('未走强天数'), { target: { value: '5' } });
    fireEvent.change(screen.getByLabelText('未走强收益%'), { target: { value: '1' } });
    fireEvent.change(screen.getByLabelText('卖出比例%'), { target: { value: '50' } });
    fireEvent.change(screen.getByLabelText('最低成交额'), { target: { value: '100000000' } });
    fireEvent.change(screen.getByLabelText(/单票金额上限/), { target: { value: '20000' } });
    fireEvent.change(screen.getByLabelText(/总持仓金额上限/), { target: { value: '80000' } });
    fireEvent.change(screen.getByLabelText('总仓位%'), { target: { value: '80' } });
    fireEvent.change(screen.getByLabelText(/行业金额上限/), { target: { value: '40000' } });
    fireEvent.change(screen.getByLabelText('行业仓位%'), { target: { value: '40' } });
    fireEvent.change(screen.getByLabelText('目标持仓权重%'), { target: { value: '600519:2.5\n000001:1.5' } });
    fireEvent.change(screen.getByLabelText('目标行业权重%'), { target: { value: '白酒:8\n银行:12' } });
    fireEvent.change(screen.getByLabelText(/最低现金余额/), { target: { value: '5000' } });
    fireEvent.change(screen.getByLabelText('最大回撤%'), { target: { value: '12' } });
    fireEvent.change(screen.getByLabelText('回撤恢复缓冲%'), { target: { value: '2' } });
    fireEvent.change(screen.getByLabelText('连续亏损上限（笔）'), { target: { value: '3' } });
    fireEvent.change(screen.getByLabelText('连续亏损冷却（分钟）'), { target: { value: '120' } });
    fireEvent.click(screen.getByLabelText('大盘红绿灯风控'));
    fireEvent.change(screen.getByLabelText('红绿灯拦截'), { target: { value: 'red_yellow' } });
    fireEvent.change(screen.getByLabelText('市场快照最长年龄（天）'), { target: { value: '5' } });
    fireEvent.click(screen.getByLabelText('市场宽度风控'));
    fireEvent.change(screen.getByLabelText('最低市场宽度分'), { target: { value: '42' } });
    fireEvent.click(screen.getByLabelText('热点退潮风控'));
    fireEvent.change(screen.getByLabelText('热点强度最小回落分'), { target: { value: '30' } });
    fireEvent.click(screen.getByLabelText('连续失败熔断'));
    fireEvent.change(screen.getByLabelText('熔断阈值'), { target: { value: '2' } });
    fireEvent.click(screen.getByLabelText('熔断冷却后自动恢复'));
    fireEvent.change(screen.getByLabelText('熔断冷却（分钟）'), { target: { value: '60' } });
    fireEvent.change(screen.getByLabelText('股票黑名单'), { target: { value: '600519, 000001' } });
    fireEvent.click(screen.getByLabelText('LLM 动态计划'));
    fireEvent.click(screen.getByLabelText('LLM 买入复核'));
    fireEvent.change(screen.getByLabelText('vn.py gateway'), { target: { value: 'SIM' } });
    const saveButton = screen.getByRole('button', { name: '保存设置' });
    fireEvent.change(screen.getByLabelText('组合分配方法'), {
      target: { value: 'score_inverse_volatility_20d_correlation_capped' },
    });
    fireEvent.change(screen.getByLabelText('波动率下限（%）'), { target: { value: '7.5' } });
    fireEvent.change(screen.getByLabelText('相关性回看日数'), { target: { value: '90' } });
    fireEvent.change(screen.getByLabelText('最少重叠收益数'), { target: { value: '30' } });
    fireEvent.change(screen.getByLabelText('最大两两相关性'), { target: { value: '0.75' } });
    fireEvent.change(screen.getByLabelText('最低数据质量分'), { target: { value: '72.5' } });
    fireEvent.click(screen.getByLabelText('跨运行前瞻门禁'));
    fireEvent.change(screen.getByLabelText('前瞻周期（交易日）'), { target: { value: '10' } });
    fireEvent.change(screen.getByLabelText('最少成熟样本'), { target: { value: '20' } });
    fireEvent.change(screen.getByLabelText('最低前瞻胜率%'), { target: { value: '48' } });
    fireEvent.change(screen.getByLabelText('前瞻扫描上限'), { target: { value: '300' } });
    fireEvent.submit(saveButton.closest('form') as HTMLFormElement);

    await waitFor(() => expect(updateSettings).toHaveBeenCalledWith(
      expect.objectContaining({
        autoTradeEnabled: true,
        autoScoreWeightedAllocationEnabled: true,
        autoAllocationBudget: 25000,
        autoAllocationMethod: 'score_inverse_volatility_20d_correlation_capped',
        autoRiskVolatilityFloorPct: 7.5,
        autoCorrelationLookbackDays: 90,
        autoCorrelationMinObservations: 30,
        autoMaxPairwiseCorrelation: 0.75,
        autoIntervalMinutes: 5,
        autoMaxPositions: 10,
        autoDailyMaxOrders: null,
        autoDailyBudget: null,
        autoTradeTimeGateEnabled: true,
        autoSymbolBlacklist: ['600519', '000001'],
        autoExcludeSt: true,
        autoExcludeSuspended: true,
        autoExcludePriceLimit: true,
        autoMinTurnover: 100000000,
        autoMinDataQualityScore: 72.5,
        autoCrossRunQualityGateEnabled: true,
        autoCrossRunHorizonDays: 10,
        autoCrossRunMinMatureSamples: 20,
        autoCrossRunMinWinRatePct: 48,
        autoCrossRunMaxDecisions: 300,
        autoMaxSinglePositionValue: 20000,
        autoMaxTotalPositionValue: 80000,
        autoMaxTotalPositionPct: 80,
        autoMaxIndustryPositionValue: 40000,
        autoMaxIndustryPositionPct: 40,
        autoTargetPositionWeights: { '600519': 2.5, '000001': 1.5 },
        autoTargetIndustryWeights: { 白酒: 8, 银行: 12 },
        autoMinCashBalance: 5000,
        autoMaxDrawdownPct: 12,
        autoDrawdownRecoveryHysteresisPct: 2,
        autoConsecutiveLossLimit: 3,
        autoConsecutiveLossCooldownMinutes: 120,
        autoMarketLightGateEnabled: true,
        autoMarketLightBlockStatuses: ['red', 'yellow'],
        autoMarketContextMaxAgeDays: 5,
        autoMarketBreadthGateEnabled: true,
        autoMarketBreadthMinScore: 42,
        autoHotspotRetreatGateEnabled: true,
        autoHotspotRetreatMinDrop: 30,
        autoFailureFuseEnabled: true,
        autoFailureFuseThreshold: 2,
        autoFailureFuseAutoRecoveryEnabled: true,
        autoFailureFuseCooldownMinutes: 60,
        autoSellEnabled: true,
        autoStopLossPct: 8,
        autoTakeProfitPct: 18,
        autoTrailingStopPct: 12,
        autoMaxHoldingDays: 20,
        autoSellPositionPct: 50,
        autoSignalExitEnabled: true,
        autoNoProgressDays: 5,
        autoNoProgressMinReturnPct: 1,
        autoRebalanceEnabled: true,
        autoLlmPlanEnabled: true,
        autoLlmReviewEnabled: true,
        vnpyGatewayName: 'SIM',
      }),
      {
        includeSnapshot: false,
        includeRecentTrades: false,
      },
    ));

    fireEvent.click(screen.getByRole('button', { name: '提交模拟委托' }));

    await waitFor(() => expect(submitOrder).toHaveBeenCalledWith(
      expect.objectContaining({
        symbol: '000001',
        side: 'buy',
        market: 'cn',
        quantity: 100,
        executionRoute: 'local_paper',
      }),
    ));
    await waitFor(() => expect(screen.getAllByText(/模拟成交/).length).toBeGreaterThan(0));
  }, 15000);

  it('persists current auto settings before running once', async () => {
    render(
      <UiLanguageProvider>
        <VnpyPaperTradingPage />
      </UiLanguageProvider>,
    );

    await screen.findByRole('heading', { name: 'vn.py 模拟交易' });

    fireEvent.click(screen.getByLabelText('定时自动买入'));
    fireEvent.change(screen.getByLabelText('间隔分钟'), { target: { value: '5' } });
    fireEvent.click(screen.getByRole('button', { name: '立即运行一次' }));

    await waitFor(() => expect(updateSettings).toHaveBeenCalledWith(
      expect.objectContaining({
        autoTradeEnabled: true,
        autoIntervalMinutes: 5,
        autoMaxPositions: 10,
        autoDailyMaxOrders: null,
        autoDailyBudget: null,
        autoTradeTimeGateEnabled: true,
        autoSymbolBlacklist: [],
        autoExcludeSt: true,
        autoExcludeSuspended: true,
        autoExcludePriceLimit: true,
        autoMinTurnover: null,
        autoMaxSinglePositionValue: null,
        autoMaxTotalPositionValue: null,
        autoMaxTotalPositionPct: null,
        autoMaxIndustryPositionValue: null,
        autoMaxIndustryPositionPct: null,
        autoMinCashBalance: null,
        autoMaxDrawdownPct: null,
        autoMarketLightGateEnabled: false,
        autoMarketLightBlockStatuses: ['red'],
        autoFailureFuseEnabled: false,
        autoFailureFuseThreshold: 3,
        autoFailureFuseAutoRecoveryEnabled: false,
        autoFailureFuseCooldownMinutes: 1440,
        autoSellEnabled: false,
        autoStopLossPct: null,
        autoTakeProfitPct: null,
        autoTrailingStopPct: null,
        autoMaxHoldingDays: null,
      }),
      {
        includeSnapshot: false,
        includeRecentTrades: false,
      },
    ));
    await waitFor(() => expect(runAutoOnce).toHaveBeenCalled());
    await waitFor(() => expect(listAgentRuns).toHaveBeenCalled());
  });

  it('runs immediate dry-run without saving auto settings first', async () => {
    runAutoOnce.mockResolvedValueOnce({
      accepted: true,
      skipped: false,
      strategy: 'dual_low',
      market: 'cn',
      candidateCount: 1,
      plannedCount: 1,
      submittedCount: 0,
      skippedCount: 0,
      orders: [],
      messages: [],
    });

    render(
      <UiLanguageProvider>
        <VnpyPaperTradingPage />
      </UiLanguageProvider>,
    );

    await screen.findByRole('heading', { name: 'vn.py 模拟交易' });

    fireEvent.click(screen.getByRole('button', { name: '立即 dry-run' }));

    await waitFor(() => expect(runAutoOnce).toHaveBeenCalledWith({
      executionMode: 'dry_run',
      ignoreAutoTradeEnabled: true,
    }));
    expect(updateSettings).not.toHaveBeenCalled();
    expect(await screen.findByText(/dry-run 完成：计划 1 笔/)).toBeInTheDocument();
  });

  it('pauses automatic buying with a single action', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    updateSettings.mockResolvedValueOnce({
      ...statusResponse,
      settings: { ...statusResponse.settings, autoTradeEnabled: false },
    });

    try {
      render(
        <UiLanguageProvider>
          <VnpyPaperTradingPage />
        </UiLanguageProvider>,
      );

      await screen.findByRole('heading', { name: 'vn.py 模拟交易' });

      fireEvent.click(screen.getByLabelText('定时自动买入'));
      fireEvent.click(screen.getByRole('button', { name: '暂停自动买入' }));

      expect(confirmSpy).toHaveBeenCalled();
      await waitFor(() => expect(updateSettings).toHaveBeenCalledWith(
        { autoTradeEnabled: false },
        {
          includeSnapshot: false,
          includeRecentTrades: false,
        },
      ));
      expect(await screen.findByText('定时自动买入已暂停')).toBeInTheDocument();
    } finally {
      confirmSpy.mockRestore();
    }
  });

  it('resumes automatic buying from the toolbar', async () => {
    updateSettings.mockResolvedValueOnce({
      ...statusResponse,
      settings: { ...statusResponse.settings, autoTradeEnabled: true },
    });

    render(
      <UiLanguageProvider>
        <VnpyPaperTradingPage />
      </UiLanguageProvider>,
    );

    await screen.findByRole('heading', { name: 'vn.py 模拟交易' });
    fireEvent.click(screen.getByRole('button', { name: '恢复自动买入' }));

    await waitFor(() => expect(updateSettings).toHaveBeenCalledWith(
      { autoTradeEnabled: true },
      {
        includeSnapshot: false,
        includeRecentTrades: false,
      },
    ));
    expect(await screen.findByText('定时自动买入已恢复')).toBeInTheDocument();
  });

  it('approves pending manual trade plans from agent details', async () => {
    listAgentRuns.mockResolvedValue({
      items: [{ ...agentRunSummary, plannedCount: 1, submittedCount: 0 }],
      limit: 10,
      offset: 0,
    });
    getAgentRun.mockResolvedValue(pendingAgentRunDetail);

    render(
      <UiLanguageProvider>
        <VnpyPaperTradingPage />
      </UiLanguageProvider>,
    );

    const approveButton = await screen.findByRole('button', { name: '审批成交' });
    fireEvent.click(approveButton);

    await waitFor(() => expect(approveTradePlan).toHaveBeenCalledWith('plan-approval'));
    expect(await screen.findByText(/审批成交：600519/)).toBeInTheDocument();
  });

  it('retries skipped manual trade plans from agent details', async () => {
    listAgentRuns.mockResolvedValue({
      items: [{ ...agentRunSummary, plannedCount: 0, submittedCount: 0, skippedCount: 1 }],
      limit: 10,
      offset: 0,
    });
    getAgentRun.mockResolvedValue(skippedManualAgentRunDetail);

    render(
      <UiLanguageProvider>
        <VnpyPaperTradingPage />
      </UiLanguageProvider>,
    );

    const retryButton = await screen.findByRole('button', { name: '重试提交' });
    fireEvent.click(retryButton);

    await waitFor(() => expect(retryTradePlan).toHaveBeenCalledWith('plan-approval'));
    expect(await screen.findByText(/重试成交：600519/)).toBeInTheDocument();
  });

  it('retries recoverable skipped paper trade plans from agent details', async () => {
    listAgentRuns.mockResolvedValue({
      items: [{ ...agentRunSummary, plannedCount: 0, submittedCount: 0, skippedCount: 1 }],
      limit: 10,
      offset: 0,
    });
    getAgentRun.mockResolvedValue(skippedPaperAgentRunDetail);

    render(
      <UiLanguageProvider>
        <VnpyPaperTradingPage />
      </UiLanguageProvider>,
    );

    expect(await screen.findByText('重试 1/3')).toBeInTheDocument();
    const retryButton = await screen.findByRole('button', { name: '重试提交' });
    fireEvent.click(retryButton);

    await waitFor(() => expect(retryTradePlan).toHaveBeenCalledWith('plan-paper-retry'));
  });

  it('cancels submitted vnpy paper trade plans from agent details', async () => {
    listAgentRuns.mockResolvedValue({
      items: [{ ...agentRunSummary, plannedCount: 0, submittedCount: 1, skippedCount: 0 }],
      limit: 10,
      offset: 0,
    });
    getAgentRun.mockResolvedValue(submittedVnpyAgentRunDetail);

    render(
      <UiLanguageProvider>
        <VnpyPaperTradingPage />
      </UiLanguageProvider>,
    );

    const cancelButton = await screen.findByRole('button', { name: '撤单' });
    fireEvent.click(cancelButton);

    await waitFor(() => expect(cancelTradePlan).toHaveBeenCalledWith('plan-vnpy-cancel'));
    expect(await screen.findByText(/撤单请求已发送：600519/)).toBeInTheDocument();
  });
});
