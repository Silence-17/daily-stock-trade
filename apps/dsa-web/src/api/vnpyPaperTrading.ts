import apiClient from './index';
import { toCamelCase } from './utils';

export type VnpyPaperMarket = 'cn' | 'hk' | 'us' | 'jp' | 'kr' | 'tw';
export type VnpyPaperSide = 'buy' | 'sell';
export type VnpyPaperExecutionMode = 'paper' | 'vnpy_paper' | 'dry_run' | 'manual_approval';
export type VnpyPaperExecutionRoute = 'local_paper' | 'vnpy_bridge';
export type VnpyPaperAllocationMethod =
  | 'score_weighted'
  | 'score_inverse_volatility_20d'
  | 'score_inverse_volatility_20d_correlation_capped'
  | 'target_tracking_min_variance_20d';

export type VnpyPaperSettings = {
  enabled: boolean;
  accountId?: number | null;
  initialCash: number;
  autoTradeEnabled: boolean;
  autoStrategy: string;
  autoMarket: VnpyPaperMarket | string;
  autoMaxResults: number;
  autoCashPerOrder: number;
  autoScoreWeightedAllocationEnabled: boolean;
  autoAllocationBudget?: number | null;
  autoAllocationMethod: VnpyPaperAllocationMethod | string;
  autoRiskVolatilityFloorPct: number;
  autoCorrelationLookbackDays: number;
  autoCorrelationMinObservations: number;
  autoMaxPairwiseCorrelation: number;
  autoCovarianceRiskPenalty: number;
  autoIntervalMinutes: number;
  autoMinScore?: number | null;
  autoSkipExistingPositions: boolean;
  autoExecutionMode: VnpyPaperExecutionMode | string;
  autoMaxPositions: number;
  autoMaxSinglePositionValue?: number | null;
  autoMaxTotalPositionValue?: number | null;
  autoMaxTotalPositionPct?: number | null;
  autoMaxIndustryPositionValue?: number | null;
  autoMaxIndustryPositionPct?: number | null;
  autoDailyMaxOrders?: number | null;
  autoDailyBudget?: number | null;
  autoTradeTimeGateEnabled: boolean;
  autoSymbolBlacklist: string[];
  autoExcludeSt: boolean;
  autoExcludeSuspended: boolean;
  autoExcludePriceLimit: boolean;
  autoMinTurnover?: number | null;
  autoMinDataQualityScore?: number | null;
  autoCrossRunQualityGateEnabled: boolean;
  autoCrossRunHorizonDays: number;
  autoCrossRunMinMatureSamples: number;
  autoCrossRunMinWinRatePct: number;
  autoCrossRunMaxDecisions: number;
  autoMinCashBalance?: number | null;
  autoMaxDrawdownPct?: number | null;
  autoDrawdownRecoveryHysteresisPct: number;
  autoConsecutiveLossLimit?: number | null;
  autoConsecutiveLossCooldownMinutes: number;
  autoMarketLightGateEnabled: boolean;
  autoMarketLightBlockStatuses: string[];
  autoMarketContextMaxAgeDays: number;
  autoMarketBreadthGateEnabled: boolean;
  autoMarketBreadthMinScore: number;
  autoHotspotRetreatGateEnabled: boolean;
  autoHotspotRetreatMinDrop: number;
  autoFailureFuseEnabled: boolean;
  autoFailureFuseThreshold: number;
  autoFailureFuseAutoRecoveryEnabled: boolean;
  autoFailureFuseCooldownMinutes: number;
  autoSellEnabled: boolean;
  autoStopLossPct?: number | null;
  autoTakeProfitPct?: number | null;
  autoTrailingStopPct?: number | null;
  autoMaxHoldingDays?: number | null;
  autoSellPositionPct?: number | null;
  autoSignalExitEnabled: boolean;
  autoNoProgressDays?: number | null;
  autoNoProgressMinReturnPct?: number | null;
  autoRebalanceEnabled: boolean;
  autoTargetPositionWeights: Record<string, number>;
  autoTargetIndustryWeights: Record<string, number>;
  autoLlmPlanEnabled: boolean;
  autoLlmReviewEnabled: boolean;
  vnpyGatewayName?: string | null;
};

export type VnpyPaperAccount = {
  id: number;
  name: string;
  broker?: string | null;
  market?: string;
  baseCurrency?: string;
  isActive?: boolean;
  isCurrent?: boolean;
  archived?: boolean;
  cleanupHidden?: boolean;
  createdAt?: string | null;
  updatedAt?: string | null;
  [key: string]: unknown;
};

export type VnpyPaperAccountListResponse = {
  items: VnpyPaperAccount[];
  count: number;
  currentAccountId?: number | null;
  hiddenCount?: number;
};

export type VnpyPaperArchivedAccountCleanupResponse = {
  cleanedAccountIds: number[];
  skipped: Array<{ accountId?: number; reason?: string; [key: string]: unknown }>;
  dryRun: boolean;
  hiddenCountBefore: number;
  hiddenCountAfter: number;
  remainingCount: number;
  accounts: VnpyPaperAccountListResponse;
};

export type VnpyPaperPosition = {
  symbol: string;
  market?: string;
  quantity: number;
  avgCost?: number;
  lastPrice?: number;
  marketValueBase?: number;
  unrealizedPnlBase?: number;
  unrealizedPnlPct?: number | null;
  priceSource?: string | null;
  priceProvider?: string | null;
  priceDate?: string | null;
  priceStale?: boolean;
  priceAvailable?: boolean;
};

export type VnpyPaperSnapshotAccount = {
  accountId: number;
  accountName: string;
  baseCurrency: string;
  totalCash: number;
  totalMarketValue: number;
  totalEquity: number;
  unrealizedPnl: number;
  realizedPnl: number;
  positions: VnpyPaperPosition[];
  dataQuality?: string | null;
  limitations?: Record<string, unknown>[];
};

export type VnpyPaperSnapshot = {
  totalCash: number;
  totalMarketValue: number;
  totalEquity: number;
  unrealizedPnl: number;
  realizedPnl: number;
  accounts: VnpyPaperSnapshotAccount[];
  dataQuality?: string | null;
  limitations?: Record<string, unknown>[];
};

export type VnpyPaperTrade = {
  id: number;
  symbol: string;
  tradeDate: string;
  side: VnpyPaperSide | string;
  quantity: number;
  price: number;
  market?: string;
  currency?: string;
  note?: string | null;
};

export type VnpyPaperOrderResult = {
  accepted: boolean;
  status: string;
  tradeId?: number | null;
  accountId?: number | null;
  symbol?: string | null;
  side?: string | null;
  quantity?: number | null;
  price?: number | null;
  cashAmount?: number | null;
  cashAmountBase?: number | null;
  cashAmountQuote?: number | null;
  baseCurrency?: string | null;
  quoteCurrency?: string | null;
  source: string;
  message: string;
  reason?: string | null;
  raw?: Record<string, unknown>;
};

export type VnpyPaperAutoRunResponse = {
  accepted: boolean;
  skipped: boolean;
  reason?: string | null;
  agentRunUid?: string | null;
  agentRunId?: number | null;
  strategy: string;
  market: string;
  candidateCount: number;
  plannedCount: number;
  submittedCount: number;
  skippedCount: number;
  orders: VnpyPaperOrderResult[];
  messages: string[];
};

export type VnpyPaperAutoRunRequest = {
  executionMode?: VnpyPaperExecutionMode;
  ignoreAutoTradeEnabled?: boolean;
};

export type VnpyPaperSchedulerTaskStatus = {
  name: string;
  intervalSeconds?: number | null;
  initialDelaySeconds?: number | null;
  running: boolean;
  overlapGuarded?: boolean;
  previousGenerationRunning?: boolean;
  lastRun?: number | string | null;
  nextRunAt?: string | null;
};

export type VnpyPaperSchedulerTaskEvent = {
  name: string;
  status: string;
  message: string;
  timestamp?: string | null;
  durationSeconds?: number | null;
  details?: Record<string, unknown>;
};

export type VnpyPaperSchedulerStatus = {
  enabled: boolean;
  running: boolean;
  loopRunning?: boolean;
  scheduleTimes: string[];
  nextRunAt?: string | null;
  lastRunAt?: string | null;
  lastSuccessAt?: string | null;
  lastError?: string | null;
  lastSkippedAt?: string | null;
  lastSkipReason?: string | null;
  backgroundTasks: VnpyPaperSchedulerTaskStatus[];
  taskEvents?: VnpyPaperSchedulerTaskEvent[];
};

export type VnpyPaperTaskHealthItem = {
  name: string;
  label: string;
  health: 'healthy' | 'warning' | 'error' | 'disabled' | string;
  reason?: string | null;
  required: boolean;
  registered: boolean;
  running: boolean;
  intervalSeconds?: number | null;
  lastRun?: number | string | null;
  nextRunAt?: string | null;
  lastEventStatus?: string | null;
  lastEventAt?: string | null;
  lastEventMessage?: string | null;
  durationSeconds?: number | null;
  details?: Record<string, unknown>;
};

export type VnpyPaperTaskHealthResponse = {
  generatedAt?: string | null;
  overallHealth: 'healthy' | 'warning' | 'error' | 'disabled' | string;
  schedulerEnabled: boolean;
  schedulerRunning: boolean;
  schedulerLoopRunning?: boolean;
  paperTradingEnabled: boolean;
  autoTradeEnabled: boolean;
  autoExecutionMode?: string | null;
  summary: Record<string, number>;
  items: VnpyPaperTaskHealthItem[];
};

export type VnpyPaperTaskEventFilters = {
  name?: string;
  status?: string;
};

export type VnpyPaperTaskEventListResponse = {
  generatedAt?: string | null;
  limit: number;
  name?: string | null;
  status?: string | null;
  count: number;
  items: VnpyPaperSchedulerTaskEvent[];
};

export type VnpyPaperTaskEventSummaryItem = {
  name: string;
  label?: string | null;
  total: number;
  startedCount: number;
  completedCount: number;
  skippedCount: number;
  failedCount: number;
  failureRatePct: number;
  avgDurationSeconds?: number | null;
  lastEventStatus?: string | null;
  lastEventAt?: string | null;
  lastEventMessage?: string | null;
  lastFailedAt?: string | null;
  lastSkippedAt?: string | null;
};

export type VnpyPaperTaskEventSummaryResponse = {
  generatedAt?: string | null;
  limit: number;
  count: number;
  statusCounts: Record<string, number>;
  items: VnpyPaperTaskEventSummaryItem[];
};

export type VnpyPaperTaskMetricsItem = {
  name: string;
  label?: string | null;
  runCount: number;
  completedCount: number;
  skippedCount: number;
  failedCount: number;
  successRatePct: number;
  skipRatePct: number;
  failureRatePct: number;
  avgDurationSeconds?: number | null;
  p95DurationSeconds?: number | null;
  lastRunAt?: string | null;
  lastFailureAt?: string | null;
};

export type VnpyPaperTaskMetricsDailyItem = {
  date: string;
  runCount: number;
  completedCount: number;
  skippedCount: number;
  failedCount: number;
  successRatePct: number;
  failureRatePct: number;
  avgDurationSeconds?: number | null;
};

export type VnpyPaperTaskMetricsResponse = {
  generatedAt?: string | null;
  windowDays: number;
  windowStartedAt?: string | null;
  windowEndedAt?: string | null;
  eventCount: number;
  runCount: number;
  startedCount: number;
  completedCount: number;
  skippedCount: number;
  failedCount: number;
  successRatePct: number;
  skipRatePct: number;
  failureRatePct: number;
  avgDurationSeconds?: number | null;
  p95DurationSeconds?: number | null;
  currentFailureStreak: number;
  truncated: boolean;
  items: VnpyPaperTaskMetricsItem[];
  daily: VnpyPaperTaskMetricsDailyItem[];
};

export type VnpyPaperStatusResponse = {
  available: boolean;
  enabled: boolean;
  vnpyAvailable: boolean;
  engine: string;
  mode: string;
  settings: VnpyPaperSettings;
  account?: VnpyPaperAccount | null;
  snapshot?: VnpyPaperSnapshot | null;
  recentTrades: VnpyPaperTrade[];
  lastAutoRun?: Record<string, unknown> | null;
  scheduler?: VnpyPaperSchedulerStatus | null;
  diagnostics?: Record<string, unknown>;
};

export type VnpyPaperPerformanceResponse = {
  account?: VnpyPaperAccount | null;
  initialCash: number;
  totalCash?: number | null;
  totalMarketValue?: number | null;
  totalEquity?: number | null;
  realizedPnl?: number | null;
  unrealizedPnl?: number | null;
  totalPnl?: number | null;
  returnPct?: number | null;
  runWindow: Record<string, unknown>;
  agent: {
    candidateCount?: number;
    plannedCount?: number;
    submittedCount?: number;
    skippedCount?: number;
    fillRatePct?: number | null;
    [key: string]: unknown;
  };
  tradeMetrics: {
    tradeCount?: number;
    buyCount?: number;
    sellCount?: number;
    grossTurnover?: number;
    buyTurnover?: number;
    sellTurnover?: number;
    closedTradeCount?: number;
    sellWinCount?: number;
    sellLossCount?: number;
    sellFlatCount?: number;
    winRatePct?: number | null;
    averageSellReturnPct?: number | null;
    realizedTradePnl?: number;
    [key: string]: unknown;
  };
  riskMetrics?: {
    maxDrawdownPct?: number | null;
    maxDrawdownValue?: number | null;
    peakEquity?: number | null;
    turnoverPct?: number | null;
    currentExposurePct?: number | null;
    curvePointCount?: number;
    [key: string]: unknown;
  };
  equityCurve?: Array<{
    date?: string | null;
    equity?: number;
    realizedPnl?: number;
    tradeId?: number | null;
    symbol?: string | null;
    side?: string | null;
    source?: string;
    [key: string]: unknown;
  }>;
  dailyReturns?: Array<{
    date: string;
    equity?: number;
    realizedPnl?: number;
    dailyPnl?: number;
    dailyReturnPct?: number | null;
    cumulativeReturnPct?: number | null;
    drawdownPct?: number | null;
    tradeCount?: number;
    source?: string;
    [key: string]: unknown;
  }>;
  monthlyReturns?: Array<{
    month: string;
    startDate?: string;
    endDate?: string;
    startEquity?: number;
    endEquity?: number;
    realizedPnl?: number;
    monthlyPnl?: number;
    monthlyReturnPct?: number | null;
    cumulativeReturnPct?: number | null;
    drawdownPct?: number | null;
    tradeCount?: number;
    positiveDays?: number;
    negativeDays?: number;
    [key: string]: unknown;
  }>;
  strategyAttribution?: Array<{
    key: string;
    runCount?: number;
    candidateCount?: number;
    plannedCount?: number;
    submittedCount?: number;
    skippedCount?: number;
    filledPlanCount?: number;
    plannedCashAmount?: number;
    filledCashAmount?: number;
    fillRatePct?: number | null;
    [key: string]: unknown;
  }>;
  industryAttribution?: Array<{
    key: string;
    decisionCount?: number;
    filledCount?: number;
    skippedCount?: number;
    plannedCashAmount?: number;
    filledCashAmount?: number;
    fillRatePct?: number | null;
    [key: string]: unknown;
  }>;
  statusCounts: Record<string, number>;
  executionModeCounts: Record<string, number>;
  dataQualityCounts: Record<string, number>;
  tradePlanStatusCounts: Record<string, number>;
  skipReasonCounts: Record<string, number>;
  topSkipReasons: Array<{ key: string; count: number }>;
  tradedSymbols: Array<{ key: string; count: number }>;
  diagnostics?: Record<string, unknown>;
};

export type VnpyPaperTradePlanRecoveryItem = {
  planUid?: string | null;
  runId?: number | null;
  decisionId?: number | null;
  symbol?: string | null;
  name?: string | null;
  market?: string | null;
  side?: string | null;
  status?: string | null;
  executionMode?: string | null;
  skipReason?: string | null;
  plannedCashAmount?: number | null;
  plannedQuantity?: number | null;
  plannedPrice?: number | null;
  submittedQuantity?: number | null;
  submittedPrice?: number | null;
  tradeId?: number | null;
  createdAt?: string | null;
  updatedAt?: string | null;
  ageSeconds?: number | null;
  ageMinutes?: number | null;
  isActive?: boolean;
  staleActive?: boolean;
  staleReason?: string | null;
  cancellable?: boolean;
  retryable?: boolean;
  retryDue?: boolean;
  retryBlockReason?: string | null;
  recoveryState?: string | null;
  retry?: {
    attemptCount?: number;
    maxAttempts?: number;
    nextRetryAfter?: string | null;
    cooldownSeconds?: number;
    [key: string]: unknown;
  };
};

export type VnpyPaperTradePlanRecoverySummary = {
  generatedAt?: string | null;
  limit: number;
  total: number;
  scannedCount: number;
  includeTerminal: boolean;
  timeoutSeconds: number;
  retryCooldownSeconds: number;
  retryMaxAttempts: number;
  activeStatuses: string[];
  statusCounts: Record<string, number>;
  executionModeCounts: Record<string, number>;
  sideCounts: Record<string, number>;
  recoveryCounts: Record<string, number>;
  staleActiveCount: number;
  cancellableCount: number;
  retryDueCount: number;
  retryCooldownCount: number;
  retryLimitCount: number;
  notRetryableCount: number;
  items: VnpyPaperTradePlanRecoveryItem[];
};

export type VnpyPaperTradePlanRecoveryRunResponse = {
  accepted: boolean;
  skipped: boolean;
  reason?: string | null;
  expiredCount: number;
  reconciledCount: number;
  protectedCount: number;
  reconciliationFailedCount: number;
  scannedCount: number;
  attemptedCount: number;
  submittedCount: number;
  skippedCount: number;
  failedCount: number;
  orders: VnpyPaperOrderResult[];
  messages: string[];
};

export type VnpyPaperAgentRunSummary = {
  id: number;
  runUid: string;
  triggerSource: string;
  status: string;
  strategy: string;
  market: string;
  maxResults?: number | null;
  cashPerOrder?: number | null;
  minScore?: number | null;
  skipExistingPositions: boolean;
  candidateCount: number;
  plannedCount: number;
  submittedCount: number;
  skippedCount: number;
  messageCount: number;
  error?: string | null;
  settings?: Record<string, unknown>;
  diagnostics?: Record<string, unknown>;
  humanFeedback?: VnpyPaperAgentRunFeedback | null;
  startedAt?: string | null;
  completedAt?: string | null;
  createdAt?: string | null;
  updatedAt?: string | null;
};

export type VnpyPaperAgentRunFeedbackVerdict = 'approved' | 'needs_changes' | 'rejected';

export type VnpyPaperAgentRunFeedback = {
  id: number;
  runId: number;
  verdict: VnpyPaperAgentRunFeedbackVerdict;
  note?: string | null;
  reviewer?: string | null;
  source: string;
  createdAt?: string | null;
  updatedAt?: string | null;
};

export type VnpyPaperAgentRunFeedbackResponse = {
  accepted: boolean;
  runUid: string;
  humanFeedback: VnpyPaperAgentRunFeedback;
  runDetail: VnpyPaperAgentRunDetail;
};

export type VnpyPaperAgentDecision = {
  id: number;
  runId: number;
  sequence: number;
  symbol?: string | null;
  name?: string | null;
  market: string;
  action: string;
  status: string;
  reason?: string | null;
  score?: number | null;
  confidence?: number | null;
  cashAmount?: number | null;
  quantity?: number | null;
  price?: number | null;
  tradeId?: number | null;
  rationale?: string | null;
  riskFlags: string[];
  orderResult?: Record<string, unknown>;
  rawCandidate?: Record<string, unknown>;
  createdAt?: string | null;
};

export type VnpyPaperAgentRunDetail = VnpyPaperAgentRunSummary & {
  decisions: VnpyPaperAgentDecision[];
  tradePlans: VnpyPaperAgentTradePlan[];
  timeline: VnpyPaperAgentTimelineEvent[];
};

export type VnpyPaperAgentTimelineEvent = {
  stage: string;
  status: string;
  message: string;
  timestamp?: string | null;
  details?: Record<string, unknown>;
};

export type VnpyPaperAgentTradePlan = {
  id: number;
  planUid: string;
  runId: number;
  decisionId?: number | null;
  symbol?: string | null;
  name?: string | null;
  market: string;
  side: string;
  status: string;
  executionMode: string;
  plannedCashAmount?: number | null;
  plannedQuantity?: number | null;
  plannedPrice?: number | null;
  submittedQuantity?: number | null;
  submittedPrice?: number | null;
  tradeId?: number | null;
  skipReason?: string | null;
  riskFlags: string[];
  orderResult?: Record<string, unknown>;
  createdAt?: string | null;
  updatedAt?: string | null;
};

export type VnpyPaperAgentRunListResponse = {
  items: VnpyPaperAgentRunSummary[];
  limit: number;
  offset: number;
  total: number;
};

export type VnpyPaperAgentRunRecapResponse = {
  accepted: boolean;
  status: string;
  reason?: string | null;
  runUid: string;
  llmRecap: Record<string, unknown>;
  runDetail?: VnpyPaperAgentRunDetail | null;
};

export type VnpyPaperAgentDailySummary = {
  generatedAt?: string | null;
  date: string;
  createdFrom?: string | null;
  createdTo?: string | null;
  limit: number;
  total: number;
  scannedCount: number;
  runCount: number;
  health: string;
  candidateCount: number;
  plannedCount: number;
  submittedCount: number;
  skippedCount: number;
  messageCount: number;
  statusCounts: Record<string, number>;
  strategyCounts: Record<string, number>;
  marketCounts: Record<string, number>;
  executionModeCounts: Record<string, number>;
  dataQualityCounts: Record<string, number>;
  agentReviewCounts: Record<string, number>;
  llmReviewCounts: Record<string, number>;
  reviewQualityCounts: Record<string, number>;
  reviewQualityFlagCounts: Record<string, number>;
  reviewQualityScoreAvg?: number | null;
  humanFeedbackCounts: Record<string, number>;
  humanFeedbackReviewedCount: number;
  workflowStatusCounts: Record<string, number>;
  workflowStageCounts: Record<string, number>;
  tradePlanStatusCounts: Record<string, number>;
  sideCounts: Record<string, number>;
  topSkipReasons: Array<{ reason?: string; count: number; [key: string]: unknown }>;
  topSymbols: Array<{ symbol?: string; count: number; [key: string]: unknown }>;
  latestRun?: VnpyPaperAgentRunSummary | null;
  filters?: Record<string, unknown>;
};

export type VnpyPaperAgentDataQualityDailyItem = {
  date: string;
  runCount: number;
  qualityCounts: Record<string, number>;
  degradedCount: number;
  degradedRatePct: number;
};

export type VnpyPaperAgentDataQualityTrends = {
  generatedAt?: string | null;
  windowDays: number;
  windowStartedAt?: string | null;
  windowEndedAt?: string | null;
  total: number;
  scannedCount: number;
  knownCount: number;
  qualityCounts: Record<string, number>;
  degradedCount: number;
  degradedRatePct: number;
  health: string;
  latestQuality?: string | null;
  warningCounts: Record<string, number>;
  sourceErrorCounts: Record<string, number>;
  sourceHealthItems?: Array<{
    key: string;
    group: string;
    source: string;
    observationCount: number;
    degradedObservationCount: number;
    degradedRatePct: number;
    maxFailures: number;
    latestStatus: string;
    latestFailures: number;
    lastObservedAt?: string | null;
  }>;
  truncated: boolean;
  daily: VnpyPaperAgentDataQualityDailyItem[];
  filters?: Record<string, unknown>;
};

export type VnpyPaperGatewayReconnectResponse = {
  attempted: boolean;
  connected: boolean;
  status: string;
  result: string;
  reason?: string | null;
  connect: Record<string, unknown>;
  reconnect: Record<string, unknown>;
};

export type VnpyPaperAgentReturnRiskCalibrationDailyItem = {
  date: string;
  runSnapshotCount: number;
  stateCounts: Record<string, number>;
  averageUtilityPct?: number | null;
};

export type VnpyPaperAgentReturnRiskCalibrationGroup = {
  key: string;
  market: string;
  strategy: string;
  version: string;
  runSnapshotCount: number;
  stateCounts: Record<string, number>;
  utilityObservationCount: number;
  averageUtilityPct?: number | null;
  minimumUtilityPct?: number | null;
  maximumUtilityPct?: number | null;
  latestState?: string | null;
  latestUtilityPct?: number | null;
  latestRunUid?: string | null;
  latestAt?: string | null;
};

export type VnpyPaperAgentReturnRiskCalibrationTrends = {
  schemaVersion: number;
  generatedAt?: string | null;
  windowDays: number;
  windowStartedAt?: string | null;
  windowEndedAt?: string | null;
  total: number;
  scannedCount: number;
  observedCount: number;
  unknownCount: number;
  observationRatePct: number;
  health: string;
  stateCounts: Record<string, number>;
  versionCounts: Record<string, number>;
  marketCounts: Record<string, number>;
  strategyCounts: Record<string, number>;
  transitionCounts: Record<string, number>;
  appliedCount: number;
  appliedRatePct: number;
  gateBlockedCount: number;
  utilityObservationCount: number;
  averageUtilityPct?: number | null;
  minimumUtilityPct?: number | null;
  maximumUtilityPct?: number | null;
  latestMatureSampleCount: number;
  maxMatureSampleCount: number;
  latest?: Record<string, unknown> | null;
  groups: VnpyPaperAgentReturnRiskCalibrationGroup[];
  daily: VnpyPaperAgentReturnRiskCalibrationDailyItem[];
  truncated: boolean;
  methodology: Record<string, unknown>;
  filters?: Record<string, unknown>;
};

export type VnpyPaperAgentRunFilters = {
  triggerSource?: string;
  strategy?: string;
  market?: string;
  status?: string;
  createdFrom?: string;
  createdTo?: string;
};

export type VnpyPaperAgentBacktestRequest = {
  strategy?: string;
  market?: string;
  createdFrom?: string;
  createdTo?: string;
  evalWindows?: number[];
  includeSkipped?: boolean;
  maxDecisions?: number;
  refreshMissing?: boolean;
  neutralBandPct?: number;
};

export type VnpyPaperAgentBacktestMetric = {
  evalWindowDays: number;
  sampleCount: number;
  completedCount: number;
  insufficientCount: number;
  coveragePct?: number | null;
  winCount: number;
  lossCount: number;
  neutralCount: number;
  winRatePct?: number | null;
  directionAccuracyPct?: number | null;
  averageReturnPct?: number | null;
  medianReturnPct?: number | null;
  averageMaxFavorableExcursionPct?: number | null;
  averageMaxAdverseExcursionPct?: number | null;
  dailyObservationCount?: number;
  expectedDailyObservationCount?: number;
  dailyReturnCoveragePct?: number | null;
  averageDailyReturnPct?: number | null;
  dailyReturnVolatilityPct?: number | null;
  downsideDeviationPct?: number | null;
  horizonDownsideDeviationPct?: number | null;
  dailyExpectedShortfall20Pct?: number | null;
  returnRiskUtilityPct?: number | null;
  returnRiskObjectiveVersion?: string;
  neutralBandPct: number;
  unableReasonCounts: Record<string, number>;
};

export type VnpyPaperAgentReviewQualityMetric = {
  evalWindowDays: number;
  sampleCount: number;
  completedCount: number;
  coveragePct?: number | null;
  passedCompletedCount: number;
  blockedCompletedCount: number;
  passedPrecisionPct?: number | null;
  blockedAvoidanceRatePct?: number | null;
  passedAverageReturnPct?: number | null;
  blockedAverageReturnPct?: number | null;
  returnSpreadPct?: number | null;
  unableReasonCounts: Record<string, number>;
};

export type VnpyPaperAgentReviewQualityGroup = {
  key: string;
  source: string;
  reviewer: string;
  model?: string | null;
  promptVersion?: string | null;
  evaluatorVersion?: string | null;
  version: string;
  sampleCount: number;
  statusCounts: Record<string, number>;
  horizons: Record<string, VnpyPaperAgentReviewQualityMetric>;
};

export type VnpyPaperAgentBacktestResponse = {
  generatedAt: string;
  methodology: Record<string, unknown>;
  filters: Record<string, unknown>;
  total: number;
  scannedCount: number;
  truncated: boolean;
  refreshAttemptedCount: number;
  statusCounts: Record<string, number>;
  matrix: Record<string, VnpyPaperAgentBacktestMetric>;
  strategyMatrix: Record<string, Record<string, VnpyPaperAgentBacktestMetric>>;
  reviewQualityMatrix: VnpyPaperAgentReviewQualityGroup[];
  reviewPolicyQuality?: Record<string, unknown>;
  items: Array<Record<string, unknown>>;
};

export type VnpyPaperAgentCrossRunQuality = {
  schemaVersion: number;
  generatedAt?: string | null;
  state: string;
  reason: string;
  previousState?: string | null;
  transition?: string | null;
  changed: boolean;
  strategy: string;
  market: string;
  selectionQualityState?: string | null;
  selectionQualityReason?: string | null;
  returnRiskObjectiveState?: string | null;
  returnRiskObjectiveReason?: string | null;
  returnRiskObjectiveApplied?: boolean;
  returnRiskObjective?: Record<string, unknown>;
  reviewQualityState?: string | null;
  reviewQualityReason?: string | null;
  reviewQualityApplied?: boolean;
  reviewPolicyQuality?: Record<string, unknown>;
  horizonDays: number;
  minMatureSamples: number;
  minWinRatePct: number;
  maxDecisions: number;
  sampleCount: number;
  matureSampleCount: number;
  coveragePct?: number | null;
  winRatePct?: number | null;
  averageReturnPct?: number | null;
  medianReturnPct?: number | null;
  averageMaxAdverseExcursionPct?: number | null;
  averageDailyReturnPct?: number | null;
  dailyReturnCoveragePct?: number | null;
  dailyReturnVolatilityPct?: number | null;
  downsideDeviationPct?: number | null;
  dailyExpectedShortfall20Pct?: number | null;
  returnRiskUtilityPct?: number | null;
  unableReasonCounts: Record<string, number>;
  lookaheadProtection: boolean;
  source: string;
  truncated: boolean;
  gateEnabled: boolean;
  gateBlocked: boolean;
  insufficientEvidenceBlocks: boolean;
  error?: string | null;
};

export type VnpyPaperAgentRunExportResponse = {
  generatedAt?: string | null;
  limit: number;
  includeDetails: boolean;
  count: number;
  items: Array<VnpyPaperAgentRunSummary | VnpyPaperAgentRunDetail | Record<string, unknown>>;
};

export type VnpyPaperSettingsUpdate = Partial<Omit<VnpyPaperSettings, 'autoMarket'>> & {
  autoMarket?: string;
};

export type VnpyPaperStatusOptions = {
  includeSnapshot?: boolean;
  includeRecentTrades?: boolean;
};

export type VnpyPaperOrderRequest = {
  symbol: string;
  side: VnpyPaperSide;
  market: VnpyPaperMarket;
  quantity?: number;
  cashAmount?: number;
  price?: number;
  note?: string;
  executionRoute?: VnpyPaperExecutionRoute;
};

export type VnpyPaperVnpyTradeCallbackRequest = {
  vtOrderid: string;
  vtTradeid?: string | null;
  symbol?: string | null;
  side?: VnpyPaperSide | null;
  market?: VnpyPaperMarket | null;
  quantity: number;
  price: number;
  tradeDate?: string | null;
  fee?: number;
  tax?: number;
  currency?: string | null;
  raw?: Record<string, unknown>;
};

export type VnpyPaperVnpyOrderCallbackRequest = {
  vtOrderid: string;
  status: string;
  symbol?: string | null;
  side?: VnpyPaperSide | null;
  market?: VnpyPaperMarket | null;
  volume?: number | null;
  traded?: number | null;
  price?: number | null;
  rejectedReason?: string | null;
  raw?: Record<string, unknown>;
};

export type VnpyPaperVnpyAccountCallbackRequest = {
  accountId: string;
  balance?: number | null;
  available?: number | null;
  frozen?: number | null;
  margin?: number | null;
  closeProfit?: number | null;
  holdingProfit?: number | null;
  currency?: string | null;
  raw?: Record<string, unknown>;
};

export type VnpyPaperVnpyPositionSnapshot = {
  symbol?: string | null;
  vtSymbol?: string | null;
  market?: VnpyPaperMarket | null;
  direction?: string | null;
  volume?: number | null;
  quantity?: number | null;
  ydVolume?: number | null;
  frozen?: number | null;
  price?: number | null;
  avgPrice?: number | null;
  pnl?: number | null;
  raw?: Record<string, unknown>;
};

export type VnpyPaperVnpyPositionsCallbackRequest = {
  positions: VnpyPaperVnpyPositionSnapshot[];
  raw?: Record<string, unknown>;
};

export type VnpyPaperVnpySyncResult = {
  accepted: boolean;
  status: string;
  message: string;
  reason?: string | null;
  raw?: Record<string, unknown>;
};

function hasOwn<T extends object>(value: T, key: keyof T): boolean {
  return Object.prototype.hasOwnProperty.call(value, key);
}

function buildSettingsPayload(payload: VnpyPaperSettingsUpdate): Record<string, unknown> {
  const body: Record<string, unknown> = {};
  if (hasOwn(payload, 'enabled')) body.enabled = payload.enabled;
  if (hasOwn(payload, 'accountId')) body.account_id = payload.accountId;
  if (hasOwn(payload, 'initialCash')) body.initial_cash = payload.initialCash;
  if (hasOwn(payload, 'autoTradeEnabled')) body.auto_trade_enabled = payload.autoTradeEnabled;
  if (hasOwn(payload, 'autoStrategy')) body.auto_strategy = payload.autoStrategy;
  if (hasOwn(payload, 'autoMarket')) body.auto_market = payload.autoMarket;
  if (hasOwn(payload, 'autoMaxResults')) body.auto_max_results = payload.autoMaxResults;
  if (hasOwn(payload, 'autoCashPerOrder')) body.auto_cash_per_order = payload.autoCashPerOrder;
  if (hasOwn(payload, 'autoScoreWeightedAllocationEnabled')) {
    body.auto_score_weighted_allocation_enabled = payload.autoScoreWeightedAllocationEnabled;
  }
  if (hasOwn(payload, 'autoAllocationBudget')) {
    body.auto_allocation_budget = payload.autoAllocationBudget;
  }
  if (hasOwn(payload, 'autoAllocationMethod')) {
    body.auto_allocation_method = payload.autoAllocationMethod;
  }
  if (hasOwn(payload, 'autoRiskVolatilityFloorPct')) {
    body.auto_risk_volatility_floor_pct = payload.autoRiskVolatilityFloorPct;
  }
  if (hasOwn(payload, 'autoCorrelationLookbackDays')) {
    body.auto_correlation_lookback_days = payload.autoCorrelationLookbackDays;
  }
  if (hasOwn(payload, 'autoCorrelationMinObservations')) {
    body.auto_correlation_min_observations = payload.autoCorrelationMinObservations;
  }
  if (hasOwn(payload, 'autoMaxPairwiseCorrelation')) {
    body.auto_max_pairwise_correlation = payload.autoMaxPairwiseCorrelation;
  }
  if (hasOwn(payload, 'autoCovarianceRiskPenalty')) {
    body.auto_covariance_risk_penalty = payload.autoCovarianceRiskPenalty;
  }
  if (hasOwn(payload, 'autoIntervalMinutes')) body.auto_interval_minutes = payload.autoIntervalMinutes;
  if (hasOwn(payload, 'autoMinScore')) body.auto_min_score = payload.autoMinScore ?? null;
  if (hasOwn(payload, 'autoSkipExistingPositions')) {
    body.auto_skip_existing_positions = payload.autoSkipExistingPositions;
  }
  if (hasOwn(payload, 'autoExecutionMode')) body.auto_execution_mode = payload.autoExecutionMode;
  if (hasOwn(payload, 'autoMaxPositions')) body.auto_max_positions = payload.autoMaxPositions;
  if (hasOwn(payload, 'autoMaxSinglePositionValue')) {
    body.auto_max_single_position_value = payload.autoMaxSinglePositionValue ?? null;
  }
  if (hasOwn(payload, 'autoMaxTotalPositionValue')) {
    body.auto_max_total_position_value = payload.autoMaxTotalPositionValue ?? null;
  }
  if (hasOwn(payload, 'autoMaxTotalPositionPct')) {
    body.auto_max_total_position_pct = payload.autoMaxTotalPositionPct ?? null;
  }
  if (hasOwn(payload, 'autoMaxIndustryPositionValue')) {
    body.auto_max_industry_position_value = payload.autoMaxIndustryPositionValue ?? null;
  }
  if (hasOwn(payload, 'autoMaxIndustryPositionPct')) {
    body.auto_max_industry_position_pct = payload.autoMaxIndustryPositionPct ?? null;
  }
  if (hasOwn(payload, 'autoDailyMaxOrders')) body.auto_daily_max_orders = payload.autoDailyMaxOrders ?? null;
  if (hasOwn(payload, 'autoDailyBudget')) body.auto_daily_budget = payload.autoDailyBudget ?? null;
  if (hasOwn(payload, 'autoTradeTimeGateEnabled')) {
    body.auto_trade_time_gate_enabled = payload.autoTradeTimeGateEnabled;
  }
  if (hasOwn(payload, 'autoSymbolBlacklist')) body.auto_symbol_blacklist = payload.autoSymbolBlacklist ?? [];
  if (hasOwn(payload, 'autoExcludeSt')) body.auto_exclude_st = payload.autoExcludeSt;
  if (hasOwn(payload, 'autoExcludeSuspended')) body.auto_exclude_suspended = payload.autoExcludeSuspended;
  if (hasOwn(payload, 'autoExcludePriceLimit')) body.auto_exclude_price_limit = payload.autoExcludePriceLimit;
  if (hasOwn(payload, 'autoMinTurnover')) body.auto_min_turnover = payload.autoMinTurnover ?? null;
  if (hasOwn(payload, 'autoMinDataQualityScore')) {
    body.auto_min_data_quality_score = payload.autoMinDataQualityScore ?? null;
  }
  if (hasOwn(payload, 'autoCrossRunQualityGateEnabled')) {
    body.auto_cross_run_quality_gate_enabled = payload.autoCrossRunQualityGateEnabled;
  }
  if (hasOwn(payload, 'autoCrossRunHorizonDays')) {
    body.auto_cross_run_horizon_days = payload.autoCrossRunHorizonDays;
  }
  if (hasOwn(payload, 'autoCrossRunMinMatureSamples')) {
    body.auto_cross_run_min_mature_samples = payload.autoCrossRunMinMatureSamples;
  }
  if (hasOwn(payload, 'autoCrossRunMinWinRatePct')) {
    body.auto_cross_run_min_win_rate_pct = payload.autoCrossRunMinWinRatePct;
  }
  if (hasOwn(payload, 'autoCrossRunMaxDecisions')) {
    body.auto_cross_run_max_decisions = payload.autoCrossRunMaxDecisions;
  }
  if (hasOwn(payload, 'autoMinCashBalance')) body.auto_min_cash_balance = payload.autoMinCashBalance ?? null;
  if (hasOwn(payload, 'autoMaxDrawdownPct')) body.auto_max_drawdown_pct = payload.autoMaxDrawdownPct ?? null;
  if (hasOwn(payload, 'autoDrawdownRecoveryHysteresisPct')) {
    body.auto_drawdown_recovery_hysteresis_pct = payload.autoDrawdownRecoveryHysteresisPct;
  }
  if (hasOwn(payload, 'autoConsecutiveLossLimit')) {
    body.auto_consecutive_loss_limit = payload.autoConsecutiveLossLimit ?? null;
  }
  if (hasOwn(payload, 'autoConsecutiveLossCooldownMinutes')) {
    body.auto_consecutive_loss_cooldown_minutes = payload.autoConsecutiveLossCooldownMinutes;
  }
  if (hasOwn(payload, 'autoMarketLightGateEnabled')) {
    body.auto_market_light_gate_enabled = payload.autoMarketLightGateEnabled;
  }
  if (hasOwn(payload, 'autoMarketLightBlockStatuses')) {
    body.auto_market_light_block_statuses = payload.autoMarketLightBlockStatuses ?? ['red'];
  }
  if (hasOwn(payload, 'autoMarketContextMaxAgeDays')) {
    body.auto_market_context_max_age_days = payload.autoMarketContextMaxAgeDays;
  }
  if (hasOwn(payload, 'autoMarketBreadthGateEnabled')) {
    body.auto_market_breadth_gate_enabled = payload.autoMarketBreadthGateEnabled;
  }
  if (hasOwn(payload, 'autoMarketBreadthMinScore')) {
    body.auto_market_breadth_min_score = payload.autoMarketBreadthMinScore;
  }
  if (hasOwn(payload, 'autoHotspotRetreatGateEnabled')) {
    body.auto_hotspot_retreat_gate_enabled = payload.autoHotspotRetreatGateEnabled;
  }
  if (hasOwn(payload, 'autoHotspotRetreatMinDrop')) {
    body.auto_hotspot_retreat_min_drop = payload.autoHotspotRetreatMinDrop;
  }
  if (hasOwn(payload, 'autoFailureFuseEnabled')) body.auto_failure_fuse_enabled = payload.autoFailureFuseEnabled;
  if (hasOwn(payload, 'autoFailureFuseThreshold')) {
    body.auto_failure_fuse_threshold = payload.autoFailureFuseThreshold;
  }
  if (hasOwn(payload, 'autoFailureFuseAutoRecoveryEnabled')) {
    body.auto_failure_fuse_auto_recovery_enabled = payload.autoFailureFuseAutoRecoveryEnabled;
  }
  if (hasOwn(payload, 'autoFailureFuseCooldownMinutes')) {
    body.auto_failure_fuse_cooldown_minutes = payload.autoFailureFuseCooldownMinutes;
  }
  if (hasOwn(payload, 'autoSellEnabled')) body.auto_sell_enabled = payload.autoSellEnabled;
  if (hasOwn(payload, 'autoStopLossPct')) body.auto_stop_loss_pct = payload.autoStopLossPct ?? null;
  if (hasOwn(payload, 'autoTakeProfitPct')) body.auto_take_profit_pct = payload.autoTakeProfitPct ?? null;
  if (hasOwn(payload, 'autoTrailingStopPct')) body.auto_trailing_stop_pct = payload.autoTrailingStopPct ?? null;
  if (hasOwn(payload, 'autoMaxHoldingDays')) body.auto_max_holding_days = payload.autoMaxHoldingDays ?? null;
  if (hasOwn(payload, 'autoSellPositionPct')) body.auto_sell_position_pct = payload.autoSellPositionPct ?? null;
  if (hasOwn(payload, 'autoSignalExitEnabled')) body.auto_signal_exit_enabled = payload.autoSignalExitEnabled;
  if (hasOwn(payload, 'autoNoProgressDays')) body.auto_no_progress_days = payload.autoNoProgressDays ?? null;
  if (hasOwn(payload, 'autoNoProgressMinReturnPct')) {
    body.auto_no_progress_min_return_pct = payload.autoNoProgressMinReturnPct ?? null;
  }
  if (hasOwn(payload, 'autoRebalanceEnabled')) {
    body.auto_rebalance_enabled = payload.autoRebalanceEnabled;
  }
  if (hasOwn(payload, 'autoTargetPositionWeights')) {
    body.auto_target_position_weights = payload.autoTargetPositionWeights ?? {};
  }
  if (hasOwn(payload, 'autoTargetIndustryWeights')) {
    body.auto_target_industry_weights = payload.autoTargetIndustryWeights ?? {};
  }
  if (hasOwn(payload, 'autoLlmPlanEnabled')) {
    body.auto_llm_plan_enabled = payload.autoLlmPlanEnabled;
  }
  if (hasOwn(payload, 'autoLlmReviewEnabled')) {
    body.auto_llm_review_enabled = payload.autoLlmReviewEnabled;
  }
  if (hasOwn(payload, 'vnpyGatewayName')) body.vnpy_gateway_name = payload.vnpyGatewayName || null;
  return body;
}

function buildOrderPayload(payload: VnpyPaperOrderRequest): Record<string, unknown> {
  return {
    symbol: payload.symbol,
    side: payload.side,
    market: payload.market,
    quantity: payload.quantity,
    cash_amount: payload.cashAmount,
    price: payload.price,
    note: payload.note,
    execution_route: payload.executionRoute,
  };
}

function buildVnpyTradeCallbackPayload(payload: VnpyPaperVnpyTradeCallbackRequest): Record<string, unknown> {
  return {
    vt_orderid: payload.vtOrderid,
    vt_tradeid: payload.vtTradeid,
    symbol: payload.symbol,
    side: payload.side,
    market: payload.market,
    quantity: payload.quantity,
    price: payload.price,
    trade_date: payload.tradeDate,
    fee: payload.fee,
    tax: payload.tax,
    currency: payload.currency,
    raw: payload.raw ?? {},
  };
}

function buildVnpyOrderCallbackPayload(payload: VnpyPaperVnpyOrderCallbackRequest): Record<string, unknown> {
  return {
    vt_orderid: payload.vtOrderid,
    status: payload.status,
    symbol: payload.symbol,
    side: payload.side,
    market: payload.market,
    volume: payload.volume,
    traded: payload.traded,
    price: payload.price,
    rejected_reason: payload.rejectedReason,
    raw: payload.raw ?? {},
  };
}

function buildVnpyAccountCallbackPayload(payload: VnpyPaperVnpyAccountCallbackRequest): Record<string, unknown> {
  return {
    account_id: payload.accountId,
    balance: payload.balance,
    available: payload.available,
    frozen: payload.frozen,
    margin: payload.margin,
    close_profit: payload.closeProfit,
    holding_profit: payload.holdingProfit,
    currency: payload.currency,
    raw: payload.raw ?? {},
  };
}

function buildVnpyPositionPayload(payload: VnpyPaperVnpyPositionSnapshot): Record<string, unknown> {
  return {
    symbol: payload.symbol,
    vt_symbol: payload.vtSymbol,
    market: payload.market,
    direction: payload.direction,
    volume: payload.volume,
    quantity: payload.quantity,
    yd_volume: payload.ydVolume,
    frozen: payload.frozen,
    price: payload.price,
    avg_price: payload.avgPrice,
    pnl: payload.pnl,
    raw: payload.raw ?? {},
  };
}

function buildVnpyPositionsCallbackPayload(payload: VnpyPaperVnpyPositionsCallbackRequest): Record<string, unknown> {
  return {
    positions: payload.positions.map(buildVnpyPositionPayload),
    raw: payload.raw ?? {},
  };
}

function buildAutoRunPayload(payload?: VnpyPaperAutoRunRequest): Record<string, unknown> | undefined {
  if (!payload) return undefined;
  const body: Record<string, unknown> = {};
  if (hasOwn(payload, 'executionMode')) body.execution_mode = payload.executionMode;
  if (hasOwn(payload, 'ignoreAutoTradeEnabled')) {
    body.ignore_auto_trade_enabled = payload.ignoreAutoTradeEnabled;
  }
  return Object.keys(body).length ? body : undefined;
}

function buildStatusParams(options?: VnpyPaperStatusOptions): Record<string, boolean> | undefined {
  if (!options) return undefined;
  const params: Record<string, boolean> = {};
  if (options.includeSnapshot !== undefined) params.include_snapshot = options.includeSnapshot;
  if (options.includeRecentTrades !== undefined) params.include_recent_trades = options.includeRecentTrades;
  return Object.keys(params).length ? params : undefined;
}

function buildAgentRunParams(
  limit: number,
  offset: number | null,
  filters?: VnpyPaperAgentRunFilters,
): Record<string, number | string> {
  const params: Record<string, number | string> = { limit };
  if (offset !== null) params.offset = offset;
  const triggerSource = filters?.triggerSource?.trim();
  const strategy = filters?.strategy?.trim();
  const market = filters?.market?.trim();
  const status = filters?.status?.trim();
  const createdFrom = filters?.createdFrom?.trim();
  const createdTo = filters?.createdTo?.trim();
  if (triggerSource) params.trigger_source = triggerSource;
  if (strategy) params.strategy = strategy;
  if (market) params.market = market;
  if (status) params.status = status;
  if (createdFrom) params.created_from = createdFrom;
  if (createdTo) params.created_to = createdTo;
  return params;
}

export const vnpyPaperTradingApi = {
  async getStatus(options?: VnpyPaperStatusOptions): Promise<VnpyPaperStatusResponse> {
    const params = buildStatusParams(options);
    const response = params
      ? await apiClient.get<Record<string, unknown>>('/api/v1/vnpy-paper/status', { params })
      : await apiClient.get<Record<string, unknown>>('/api/v1/vnpy-paper/status');
    return toCamelCase<VnpyPaperStatusResponse>(response.data);
  },

  async reconnectGateway(): Promise<VnpyPaperGatewayReconnectResponse> {
    const response = await apiClient.post<Record<string, unknown>>(
      '/api/v1/vnpy-paper/gateway/reconnect',
    );
    return toCamelCase<VnpyPaperGatewayReconnectResponse>(response.data);
  },

  async getTaskHealth(): Promise<VnpyPaperTaskHealthResponse> {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/vnpy-paper/task-health');
    return toCamelCase<VnpyPaperTaskHealthResponse>(response.data);
  },

  async getTaskEvents(
    limit = 50,
    filters?: VnpyPaperTaskEventFilters,
  ): Promise<VnpyPaperTaskEventListResponse> {
    const params: Record<string, number | string> = { limit };
    const name = filters?.name?.trim();
    const status = filters?.status?.trim();
    if (name) params.name = name;
    if (status) params.status = status;
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/vnpy-paper/task-events', { params });
    return toCamelCase<VnpyPaperTaskEventListResponse>(response.data);
  },

  async getTaskEventSummary(limit = 100): Promise<VnpyPaperTaskEventSummaryResponse> {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/vnpy-paper/task-event-summary', {
      params: { limit },
    });
    return toCamelCase<VnpyPaperTaskEventSummaryResponse>(response.data);
  },

  async getTaskMetrics(days = 30): Promise<VnpyPaperTaskMetricsResponse> {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/vnpy-paper/task-metrics', {
      params: { days },
    });
    return toCamelCase<VnpyPaperTaskMetricsResponse>(response.data);
  },

  async ensureAccount(options?: VnpyPaperStatusOptions): Promise<VnpyPaperStatusResponse> {
    const params = buildStatusParams(options);
    const response = params
      ? await apiClient.post<Record<string, unknown>>(
        '/api/v1/vnpy-paper/account/ensure',
        undefined,
        { params },
      )
      : await apiClient.post<Record<string, unknown>>('/api/v1/vnpy-paper/account/ensure');
    return toCamelCase<VnpyPaperStatusResponse>(response.data);
  },

  async listAccounts(includeInactive = true, includeHidden = false): Promise<VnpyPaperAccountListResponse> {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/vnpy-paper/accounts', {
      params: { include_inactive: includeInactive, include_hidden: includeHidden },
    });
    return toCamelCase<VnpyPaperAccountListResponse>(response.data);
  },

  async cleanupArchivedAccounts(
    accountIds: number[] = [],
    options?: { dryRun?: boolean; includeHidden?: boolean },
  ): Promise<VnpyPaperArchivedAccountCleanupResponse> {
    const response = await apiClient.post<Record<string, unknown>>(
      '/api/v1/vnpy-paper/accounts/archived/cleanup',
      {
        account_ids: accountIds,
        dry_run: Boolean(options?.dryRun),
        include_hidden: Boolean(options?.includeHidden),
      },
    );
    return toCamelCase<VnpyPaperArchivedAccountCleanupResponse>(response.data);
  },

  async restoreAccount(accountId: number, options?: VnpyPaperStatusOptions): Promise<VnpyPaperStatusResponse> {
    const params = buildStatusParams(options);
    const response = params
      ? await apiClient.post<Record<string, unknown>>(
        `/api/v1/vnpy-paper/accounts/${accountId}/restore`,
        undefined,
        { params },
      )
      : await apiClient.post<Record<string, unknown>>(
        `/api/v1/vnpy-paper/accounts/${accountId}/restore`,
      );
    return toCamelCase<VnpyPaperStatusResponse>(response.data);
  },

  async resetAccount(options?: VnpyPaperStatusOptions): Promise<VnpyPaperStatusResponse> {
    const params = buildStatusParams(options);
    const response = params
      ? await apiClient.post<Record<string, unknown>>(
        '/api/v1/vnpy-paper/account/reset',
        undefined,
        { params },
      )
      : await apiClient.post<Record<string, unknown>>('/api/v1/vnpy-paper/account/reset');
    return toCamelCase<VnpyPaperStatusResponse>(response.data);
  },

  async getPerformance(
    runLimit = 50,
    filters?: { createdFrom?: string; createdTo?: string },
  ): Promise<VnpyPaperPerformanceResponse> {
    const params: Record<string, unknown> = { run_limit: runLimit };
    if (filters?.createdFrom) params.created_from = filters.createdFrom;
    if (filters?.createdTo) params.created_to = filters.createdTo;
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/vnpy-paper/performance', {
      params,
    });
    return toCamelCase<VnpyPaperPerformanceResponse>(response.data);
  },

  async getTradePlanRecoverySummary(
    limit = 100,
    includeTerminal = false,
  ): Promise<VnpyPaperTradePlanRecoverySummary> {
    const response = await apiClient.get<Record<string, unknown>>(
      '/api/v1/vnpy-paper/trade-plans/recovery-summary',
      {
        params: {
          limit,
          include_terminal: includeTerminal,
        },
      },
    );
    return toCamelCase<VnpyPaperTradePlanRecoverySummary>(response.data);
  },

  async runTradePlanRecovery(maxPlans = 5, scanLimit = 200): Promise<VnpyPaperTradePlanRecoveryRunResponse> {
    const response = await apiClient.post<Record<string, unknown>>(
      '/api/v1/vnpy-paper/trade-plans/recovery/run',
      undefined,
      {
        params: {
          max_plans: maxPlans,
          scan_limit: scanLimit,
        },
      },
    );
    return toCamelCase<VnpyPaperTradePlanRecoveryRunResponse>(response.data);
  },

  async updateSettings(
    payload: VnpyPaperSettingsUpdate,
    options?: VnpyPaperStatusOptions,
  ): Promise<VnpyPaperStatusResponse> {
    const params = buildStatusParams(options);
    const response = params
      ? await apiClient.put<Record<string, unknown>>(
        '/api/v1/vnpy-paper/settings',
        buildSettingsPayload(payload),
        { params },
      )
      : await apiClient.put<Record<string, unknown>>(
        '/api/v1/vnpy-paper/settings',
        buildSettingsPayload(payload),
      );
    return toCamelCase<VnpyPaperStatusResponse>(response.data);
  },

  async resetFailureFuse(): Promise<VnpyPaperStatusResponse> {
    const response = await apiClient.post<Record<string, unknown>>('/api/v1/vnpy-paper/failure-fuse/reset');
    return toCamelCase<VnpyPaperStatusResponse>(response.data);
  },

  async submitOrder(payload: VnpyPaperOrderRequest): Promise<VnpyPaperOrderResult> {
    const response = await apiClient.post<Record<string, unknown>>(
      '/api/v1/vnpy-paper/orders',
      buildOrderPayload(payload),
    );
    return toCamelCase<VnpyPaperOrderResult>(response.data);
  },

  async syncVnpyTradeCallback(payload: VnpyPaperVnpyTradeCallbackRequest): Promise<VnpyPaperOrderResult> {
    const response = await apiClient.post<Record<string, unknown>>(
      '/api/v1/vnpy-paper/vnpy-events/trades',
      buildVnpyTradeCallbackPayload(payload),
    );
    return toCamelCase<VnpyPaperOrderResult>(response.data);
  },

  async syncVnpyOrderCallback(payload: VnpyPaperVnpyOrderCallbackRequest): Promise<VnpyPaperOrderResult> {
    const response = await apiClient.post<Record<string, unknown>>(
      '/api/v1/vnpy-paper/vnpy-events/orders',
      buildVnpyOrderCallbackPayload(payload),
    );
    return toCamelCase<VnpyPaperOrderResult>(response.data);
  },

  async syncVnpyAccountCallback(payload: VnpyPaperVnpyAccountCallbackRequest): Promise<VnpyPaperVnpySyncResult> {
    const response = await apiClient.post<Record<string, unknown>>(
      '/api/v1/vnpy-paper/vnpy-events/account',
      buildVnpyAccountCallbackPayload(payload),
    );
    return toCamelCase<VnpyPaperVnpySyncResult>(response.data);
  },

  async syncVnpyPositionsCallback(payload: VnpyPaperVnpyPositionsCallbackRequest): Promise<VnpyPaperVnpySyncResult> {
    const response = await apiClient.post<Record<string, unknown>>(
      '/api/v1/vnpy-paper/vnpy-events/positions',
      buildVnpyPositionsCallbackPayload(payload),
    );
    return toCamelCase<VnpyPaperVnpySyncResult>(response.data);
  },

  async attachVnpyEventEngine(): Promise<VnpyPaperVnpySyncResult> {
    const response = await apiClient.post<Record<string, unknown>>('/api/v1/vnpy-paper/vnpy-events/attach');
    return toCamelCase<VnpyPaperVnpySyncResult>(response.data);
  },

  async runAutoOnce(payload?: VnpyPaperAutoRunRequest): Promise<VnpyPaperAutoRunResponse> {
    const body = buildAutoRunPayload(payload);
    const response = body
      ? await apiClient.post<Record<string, unknown>>('/api/v1/vnpy-paper/auto/run', body)
      : await apiClient.post<Record<string, unknown>>('/api/v1/vnpy-paper/auto/run');
    return toCamelCase<VnpyPaperAutoRunResponse>(response.data);
  },

  async approveTradePlan(planUid: string): Promise<VnpyPaperOrderResult> {
    const response = await apiClient.post<Record<string, unknown>>(
      `/api/v1/vnpy-paper/trade-plans/${encodeURIComponent(planUid)}/approve`,
    );
    return toCamelCase<VnpyPaperOrderResult>(response.data);
  },

  async retryTradePlan(planUid: string): Promise<VnpyPaperOrderResult> {
    const response = await apiClient.post<Record<string, unknown>>(
      `/api/v1/vnpy-paper/trade-plans/${encodeURIComponent(planUid)}/retry`,
    );
    return toCamelCase<VnpyPaperOrderResult>(response.data);
  },

  async cancelTradePlan(planUid: string): Promise<VnpyPaperOrderResult> {
    const response = await apiClient.post<Record<string, unknown>>(
      `/api/v1/vnpy-paper/trade-plans/${encodeURIComponent(planUid)}/cancel`,
    );
    return toCamelCase<VnpyPaperOrderResult>(response.data);
  },

  async listAgentRuns(
    limit = 10,
    offset = 0,
    filters?: VnpyPaperAgentRunFilters,
  ): Promise<VnpyPaperAgentRunListResponse> {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/vnpy-paper/agent-runs', {
      params: buildAgentRunParams(limit, offset, filters),
    });
    return toCamelCase<VnpyPaperAgentRunListResponse>(response.data);
  },

  async exportAgentRuns(
    limit = 50,
    includeDetails = true,
    filters?: VnpyPaperAgentRunFilters,
  ): Promise<VnpyPaperAgentRunExportResponse> {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/vnpy-paper/agent-runs/export', {
      params: {
        ...buildAgentRunParams(limit, null, filters),
        include_details: includeDetails,
      },
    });
    return toCamelCase<VnpyPaperAgentRunExportResponse>(response.data);
  },

  async getAgentDailySummary(
    date?: string,
    filters?: VnpyPaperAgentRunFilters,
    limit = 100,
  ): Promise<VnpyPaperAgentDailySummary> {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/vnpy-paper/agent-runs/daily-summary', {
      params: {
        ...buildAgentRunParams(limit, null, filters),
        date: date || undefined,
      },
    });
    return toCamelCase<VnpyPaperAgentDailySummary>(response.data);
  },

  async getAgentDataQualityTrends(
    days = 30,
    filters?: VnpyPaperAgentRunFilters,
  ): Promise<VnpyPaperAgentDataQualityTrends> {
    const params: Record<string, number | string> = { days };
    const triggerSource = filters?.triggerSource?.trim();
    const strategy = filters?.strategy?.trim();
    const market = filters?.market?.trim();
    const status = filters?.status?.trim();
    if (triggerSource) params.trigger_source = triggerSource;
    if (strategy) params.strategy = strategy;
    if (market) params.market = market;
    if (status) params.status = status;
    const response = await apiClient.get<Record<string, unknown>>(
      '/api/v1/vnpy-paper/agent-runs/data-quality-trends',
      { params },
    );
    return toCamelCase<VnpyPaperAgentDataQualityTrends>(response.data);
  },

  async getAgentReturnRiskCalibrationTrends(
    days = 30,
    filters?: VnpyPaperAgentRunFilters,
  ): Promise<VnpyPaperAgentReturnRiskCalibrationTrends> {
    const params: Record<string, number | string> = { days };
    const triggerSource = filters?.triggerSource?.trim();
    const strategy = filters?.strategy?.trim();
    const market = filters?.market?.trim();
    const status = filters?.status?.trim();
    if (triggerSource) params.trigger_source = triggerSource;
    if (strategy) params.strategy = strategy;
    if (market) params.market = market;
    if (status) params.status = status;
    const response = await apiClient.get<Record<string, unknown>>(
      '/api/v1/vnpy-paper/agent-runs/return-risk-calibration-trends',
      { params },
    );
    return toCamelCase<VnpyPaperAgentReturnRiskCalibrationTrends>(response.data);
  },

  async runAgentBacktest(
    payload: VnpyPaperAgentBacktestRequest = {},
  ): Promise<VnpyPaperAgentBacktestResponse> {
    const response = await apiClient.post<Record<string, unknown>>(
      '/api/v1/vnpy-paper/agent-runs/backtest',
      {
        strategy: payload.strategy || null,
        market: payload.market || null,
        created_from: payload.createdFrom || null,
        created_to: payload.createdTo || null,
        eval_windows: payload.evalWindows ?? [1, 5, 10, 20],
        include_skipped: payload.includeSkipped ?? true,
        max_decisions: payload.maxDecisions ?? 500,
        refresh_missing: payload.refreshMissing ?? false,
        neutral_band_pct: payload.neutralBandPct ?? 2,
      },
    );
    return toCamelCase<VnpyPaperAgentBacktestResponse>(response.data);
  },

  async getAgentCrossRunQuality(): Promise<VnpyPaperAgentCrossRunQuality> {
    const response = await apiClient.get<Record<string, unknown>>(
      '/api/v1/vnpy-paper/agent-runs/cross-run-quality',
    );
    return toCamelCase<VnpyPaperAgentCrossRunQuality>(response.data);
  },

  async generateAgentRunRecap(
    runUid: string,
    maxOutputTokens = 800,
  ): Promise<VnpyPaperAgentRunRecapResponse> {
    const response = await apiClient.post<Record<string, unknown>>(
      `/api/v1/vnpy-paper/agent-runs/${runUid}/llm-recap`,
      { max_output_tokens: maxOutputTokens },
    );
    return toCamelCase<VnpyPaperAgentRunRecapResponse>(response.data);
  },

  async updateAgentRunFeedback(
    runUid: string,
    payload: {
      verdict: VnpyPaperAgentRunFeedbackVerdict;
      note?: string;
      reviewer?: string;
    },
  ): Promise<VnpyPaperAgentRunFeedbackResponse> {
    const response = await apiClient.put<Record<string, unknown>>(
      `/api/v1/vnpy-paper/agent-runs/${runUid}/feedback`,
      {
        verdict: payload.verdict,
        note: payload.note?.trim() || null,
        reviewer: payload.reviewer?.trim() || null,
      },
    );
    return toCamelCase<VnpyPaperAgentRunFeedbackResponse>(response.data);
  },

  async getAgentRun(runUid: string): Promise<VnpyPaperAgentRunDetail> {
    const response = await apiClient.get<Record<string, unknown>>(`/api/v1/vnpy-paper/agent-runs/${runUid}`);
    return toCamelCase<VnpyPaperAgentRunDetail>(response.data);
  },
};
