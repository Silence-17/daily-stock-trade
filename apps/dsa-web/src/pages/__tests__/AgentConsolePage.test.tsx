import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { UiLanguageProvider } from '../../contexts/UiLanguageContext';
import AgentConsolePage from '../AgentConsolePage';

const listAgentRuns = vi.hoisted(() => vi.fn());
const exportAgentRuns = vi.hoisted(() => vi.fn());
const getAgentRun = vi.hoisted(() => vi.fn());
const getAgentDailySummary = vi.hoisted(() => vi.fn());
const getAgentDataQualityTrends = vi.hoisted(() => vi.fn());
const getAgentCalibrationEvidence = vi.hoisted(() => vi.fn());
const getAgentCrossRunQuality = vi.hoisted(() => vi.fn());
const runAgentBacktest = vi.hoisted(() => vi.fn());
const generateAgentRunRecap = vi.hoisted(() => vi.fn());
const updateAgentRunFeedback = vi.hoisted(() => vi.fn());
const getReplayCompatibility = vi.hoisted(() => vi.fn());
const runReplay = vi.hoisted(() => vi.fn());
const runPortfolioBacktest = vi.hoisted(() => vi.fn());
const resolveHistoricalUniverse = vi.hoisted(() => vi.fn());
const startFactorIngestion = vi.hoisted(() => vi.fn());
const getFactorIngestionTask = vi.hoisted(() => vi.fn());
const startFullMarketIngestion = vi.hoisted(() => vi.fn());
const getFullMarketIngestion = vi.hoisted(() => vi.fn());
const resumeFullMarketIngestion = vi.hoisted(() => vi.fn());
const listFullMarketIngestions = vi.hoisted(() => vi.fn());

vi.mock('../../api/vnpyPaperTrading', () => ({
  vnpyPaperTradingApi: {
    listAgentRuns,
    exportAgentRuns,
    getAgentRun,
    getAgentDailySummary,
    getAgentDataQualityTrends,
    getAgentCalibrationEvidence,
    getAgentCrossRunQuality,
    runAgentBacktest,
    generateAgentRunRecap,
    updateAgentRunFeedback,
  },
}));

vi.mock('../../api/alphasift', () => ({
  alphasiftApi: {
    getReplayCompatibility,
    runReplay,
    runPortfolioBacktest,
    resolveHistoricalUniverse,
    startFactorIngestion,
    getFactorIngestionTask,
    startFullMarketIngestion,
    getFullMarketIngestion,
    resumeFullMarketIngestion,
    listFullMarketIngestions,
  },
}));

function renderPage(initialEntry = '/agent-console') {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <UiLanguageProvider>
        <Routes>
          <Route path="/agent-console" element={<AgentConsolePage />} />
          <Route path="/agent-console/:runUid" element={<AgentConsolePage />} />
        </Routes>
      </UiLanguageProvider>
    </MemoryRouter>,
  );
}

const runSummary = {
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
  candidateCount: 2,
  plannedCount: 1,
  submittedCount: 1,
  skippedCount: 1,
  messageCount: 0,
  error: null,
  settings: {},
  diagnostics: {
    dataQuality: { status: 'partial', score: 82.5, grade: 'good' },
    portfolioAllocation: {
      enabled: true,
      method: 'score_inverse_volatility_20d_capped',
      configuredBudget: 10000,
      resolvedBudget: 10000,
      allocatedBudget: 10000,
      optimizer: {
        model: 'target_tracking_min_variance_20d_v1',
        observationCount: 20,
        riskPenalty: 0.25,
      },
    },
    sourceRouting: {
      mode: 'dynamic_health',
      basePriority: 'sina,efinance',
      effectivePriority: 'efinance,sina',
      adjusted: true,
      sources: [
        { source: 'efinance', weight: 1, effectiveRank: 1 },
        { source: 'sina', weight: 0.38, effectiveRank: 2 },
      ],
      candidateContext: {
        quote: {
          mode: 'circuit_breaker_failover',
          sources: {
            'cn/efinance': {
              state: 'open',
              failures: 3,
              disabled: true,
              cooldownRemainingSeconds: 240,
            },
            'cn/akshare_em': {
              state: 'closed',
              failures: 0,
              disabled: false,
              cooldownRemainingSeconds: 0,
            },
          },
        },
        fundFlow: {
          mode: 'circuit_breaker_failover',
          priority: ['tushare_ths', 'akshare'],
          sources: {
            'cn/tushare_ths': {
              state: 'open',
              failures: 3,
              disabled: true,
              cooldownRemainingSeconds: 180,
            },
            'cn/akshare': {
              state: 'closed',
              failures: 0,
              disabled: false,
              cooldownRemainingSeconds: 0,
            },
          },
        },
        news: {
          mode: 'cross_run_health_weighted_failover',
          basePriority: ['anspire', 'tavily'],
          effectivePriority: ['tavily', 'anspire'],
          adjusted: true,
          failureThreshold: 3,
          cooldownSeconds: 300,
          halfOpenMaxCalls: 1,
          crossProcessPersistence: true,
          nextRecommendedPriority: ['anspire', 'tavily'],
          sources: {
            anspire: {
              state: 'open',
              failures: 3,
              disabled: true,
              cooldownRemainingSeconds: 120,
            },
            tavily: {
              state: 'closed',
              failures: 0,
              disabled: false,
              cooldownRemainingSeconds: 0,
            },
          },
        },
      },
    },
    crossRunQuality: {
      state: 'healthy',
      reason: 'forward_quality_thresholds_met',
      previousState: 'blocked',
      transition: 'blocked->healthy',
      matureSampleCount: 20,
      winRatePct: 55,
      gateEnabled: true,
      gateBlocked: false,
    },
    agentPlan: {
      strategy: 'dual_low',
      market: 'cn',
      executionMode: 'paper',
      maxResults: 3,
      cashPerOrder: 10000,
      planProfile: {
        mode: 'local_paper_execution',
        riskLevel: 'guarded',
      },
      adaptiveControls: {
        riskLevel: 'guarded',
        configuredLayers: ['time_gate', 'candidate_filters', 'portfolio_limits'],
      },
      marketObjective: {
        primaryObjective: 'liquidity_and_momentum',
        mode: 'guarded',
        status: 'tightened',
        configured: { maxResults: 3, cashPerOrder: 10000 },
        effective: { maxResults: 2, cashPerOrder: 7500 },
        reasons: ['latest_run_failed'],
      },
      recentRunContext: {
        runCount: 5,
        submissionRatePct: 40,
        currentFailureStreak: 1,
        latestRunUid: 'ss-agent-previous',
        latestStatus: 'failed',
      },
      llmDynamicPlan: {
        status: 'accepted',
        promptVersion: 'vnpy_paper_dynamic_agent_plan_v2',
        evaluatorVersion: 'dynamic_plan_guardrails_v1',
        appliedOverrides: {
          autoStrategy: 'capital_heat',
          autoMaxResults: 1,
        },
        recommendation: {
          rationale: 'Prefer a narrower heat strategy today.',
        },
      },
    },
    agentSummary: {
      outcome: 'executed',
      headline: 'Agent summary: outcome=executed, candidates=2, planned=1, submitted=1, skipped=1',
      reviewQuality: {
        status: 'guarded',
        score: 85,
        decisionCount: 2,
        agentReviewCoveragePct: 100,
        llmReviewCoveragePct: 50,
        riskFlags: ['agent_review_blocked'],
        humanReviewRecommended: true,
      },
      topSkipReasons: [{ reason: 'position_exists', count: 1 }],
    },
    agentWorkflow: {
      status: 'executed',
      currentStage: 'execution',
      nextAction: 'monitor_positions_and_events',
      stages: [
        { key: 'agent_plan', label: 'Agent plan', status: 'completed' },
        { key: 'candidate_review', label: 'Candidate review', status: 'warning' },
        { key: 'execution', label: 'Execution', status: 'completed' },
      ],
    },
    llmRecap: {
      status: 'completed',
      content: 'LLM recap generated',
    },
  },
  humanFeedback: null,
  startedAt: '2026-07-01T09:30:00Z',
  completedAt: '2026-07-01T09:31:00Z',
  createdAt: '2026-07-01T09:30:00Z',
  updatedAt: '2026-07-01T09:31:00Z',
};

const failedRunSummary = {
  ...runSummary,
  id: 2,
  runUid: 'ss-agent-failed',
  status: 'failed',
  strategy: 'capital_heat',
  candidateCount: 0,
  plannedCount: 0,
  submittedCount: 0,
  skippedCount: 0,
  error: 'data_quality_stale',
  diagnostics: {
    dataQuality: { status: 'stale' },
    agentSummary: {
      outcome: 'failed',
      headline: 'Agent summary: outcome=failed, error=data_quality_stale',
      reviewQuality: {
        status: 'idle',
        score: 100,
        decisionCount: 0,
        agentReviewCoveragePct: null,
        llmReviewCoveragePct: null,
        riskFlags: ['data_quality_stale'],
        humanReviewRecommended: false,
      },
      topSkipReasons: [{ reason: 'data_quality_stale', count: 1 }],
    },
  },
};

const runDetail = {
  ...runSummary,
  decisions: [{
    id: 1,
    runId: 1,
    sequence: 1,
    symbol: '600519',
    name: 'Kweichow Moutai',
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
    rationale: 'low valuation and improving momentum',
    riskFlags: [],
    agentReview: {
      status: 'passed',
      summary: 'Top-level Agent review passed',
    },
    llmReview: {
      status: 'passed',
      promptVersion: 'vnpy_paper_pre_trade_review_v2',
      evaluatorVersion: 'candidate_audit_columns_v1',
      summary: 'Top-level LLM review passed',
    },
    orderResult: {
      strategyEvidence: {
        schemaVersion: 1,
        strategy: 'dual_low',
        status: 'detailed',
        rank: 1,
        screenScore: 78,
        finalScore: 80,
        matches: [
          { key: 'pe_below_limit', status: 'passed', value: 18.2 },
          { key: 'momentum_positive', matched: true },
        ],
        factorScores: { value: 88, momentum: 72.5 },
        evidenceFields: ['rule_matches', 'factor_scores', 'screen_score', 'final_score'],
      },
      positionPlan: {
        sizingMethod: 'score_inverse_volatility_20d_allocation',
        portfolioAllocation: {
          enabled: true,
          method: 'score_inverse_volatility_20d_capped',
          scoreWeight: 0.8,
          allocationWeight: 0.75,
          optimizerTargetWeight: 0.8,
          candidateCap: 10000,
          allocatedBaseAmount: 8000,
        riskInput: {
            status: 'available',
            volatility20dPct: 12.5,
            effectiveVolatilityPct: 12.5,
          riskAdjustedWeight: 6.4,
          correlation: {
            status: 'available',
            observationCount: 60,
            maxPairwiseCorrelation: 0.85,
            pairwise: [{ symbol: '000001', correlation: 0.42 }],
          },
          covarianceHistory: {
            status: 'available',
            observationCount: 20,
          },
          },
        },
      },
      agentReview: {
        status: 'passed',
        summary: 'Pre-trade Agent review passed',
      },
      llmReview: {
        status: 'passed',
        promptVersion: 'vnpy_paper_pre_trade_review_v1',
        evaluatorVersion: 'pre_trade_fail_closed_v1',
        summary: 'LLM review passed',
      },
    },
    rawCandidate: {},
    createdAt: '2026-07-01T09:30:01Z',
  }, {
    id: 2,
    runId: 1,
    sequence: 2,
    symbol: '000001',
    name: 'Ping An Bank',
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
    rationale: 'position already exists',
    riskFlags: ['position_exists'],
    orderResult: {
      agentReview: {
        status: 'blocked',
        summary: 'Pre-trade Agent review blocked: position_exists',
      },
    },
    rawCandidate: {},
    createdAt: '2026-07-01T09:30:02Z',
  }],
  tradePlans: [{
    id: 1,
    planUid: 'plan-test',
    runId: 1,
    decisionId: 1,
    symbol: '600519',
    name: 'Kweichow Moutai',
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
  portfolioChange: {
    schemaVersion: 1,
    basis: 'persisted_portfolio_trade_ids',
    status: 'changed',
    bookedPlanCount: 1,
    pendingPlanCount: 0,
    plannedPlanCount: 0,
    symbolCount: 1,
    items: [{
      symbol: '600519',
      name: 'Kweichow Moutai',
      market: 'cn',
      buyQuantity: 100,
      sellQuantity: 0,
      netQuantity: 100,
      buyNotional: 1000,
      sellNotional: 0,
      netCashFlow: -1000,
      planCount: 1,
      tradeIds: [2],
    }],
    updatedAt: '2026-07-01T09:30:01Z',
  },
  timeline: [{
    stage: 'llm_ranking_health',
    status: 'skipped',
    message: 'AlphaSift LLM ranking: decision=circuit_open, result=skipped, state=open',
    timestamp: '2026-07-01T09:30:00Z',
    details: {},
  }, {
    stage: 'market_context_risk',
    status: 'blocked',
    message: 'Market context risk blocked: market_context_stale, breadth=28, hotspot_drop=null, age_days=8',
    timestamp: '2026-07-01T09:30:00Z',
    details: {},
  }, {
    stage: 'candidate_decisions',
    status: 'completed',
    message: '2 candidate decisions: buy=1, skip=1',
    timestamp: '2026-07-01T09:30:01Z',
    details: {},
  }, {
    stage: 'completed',
    status: 'completed',
    message: 'Run completed: planned=1, submitted=1, skipped=1',
    timestamp: '2026-07-01T09:31:00Z',
    details: {},
  }],
};

const failedRunDetail = {
  ...failedRunSummary,
  decisions: [],
  tradePlans: [],
  timeline: [{
    stage: 'data_quality',
    status: 'stale',
    message: 'Data quality stale: cached screen result',
    timestamp: '2026-07-01T09:30:01Z',
    details: {},
  }],
};

const dailySummary = {
  generatedAt: '2026-07-02T00:00:00Z',
  date: '2026-07-01',
  createdFrom: '2026-07-01T00:00:00',
  createdTo: '2026-07-02T00:00:00',
  limit: 100,
  total: 2,
  scannedCount: 2,
  runCount: 2,
  health: 'warning',
  candidateCount: 2,
  plannedCount: 1,
  submittedCount: 1,
  skippedCount: 1,
  messageCount: 0,
  statusCounts: { completed: 1, failed: 1 },
  strategyCounts: { dual_low: 1, capital_heat: 1 },
  marketCounts: { cn: 2 },
  executionModeCounts: { paper: 1, unknown: 1 },
  dataQualityCounts: { partial: 1, stale: 1 },
  agentReviewCounts: { passed: 1, blocked: 1 },
  llmReviewCounts: { passed: 1 },
  reviewQualityCounts: { guarded: 1, idle: 1 },
  reviewQualityFlagCounts: { agent_review_blocked: 1, data_quality_stale: 1 },
  reviewQualityScoreAvg: 92.5,
  humanFeedbackCounts: { approved: 1 },
  humanFeedbackReviewedCount: 1,
  workflowStatusCounts: { executed: 1, skipped: 1 },
  workflowStageCounts: { execution: 1, candidateReview: 1 },
  tradePlanStatusCounts: { filled: 1 },
  sideCounts: { buy: 1 },
  topSkipReasons: [{ reason: 'position_exists', count: 1 }],
  topSymbols: [{ symbol: '600519', count: 1 }],
  latestRun: runSummary,
  filters: {},
};

const dataQualityTrends = {
  generatedAt: '2026-07-13T16:00:00',
  windowDays: 30,
  total: 3,
  scannedCount: 3,
  knownCount: 2,
  qualityCounts: { ok: 1, partial: 1, unknown: 1 },
  degradedCount: 1,
  degradedRatePct: 33.33,
  health: 'warning',
  latestQuality: 'partial',
  warningCounts: { dailySourceFallback: 1 },
  sourceErrorCounts: { snapshotTimeout: 1 },
  sourceHealthItems: [{
    key: 'snapshot/sina',
    group: 'snapshot',
    source: 'sina',
    observationCount: 3,
    degradedObservationCount: 2,
    degradedRatePct: 66.67,
    maxFailures: 2,
    latestStatus: 'degraded',
    latestFailures: 1,
    lastObservedAt: '2026-07-13T16:00:00',
  }],
  truncated: false,
  daily: [{
    date: '2026-07-13',
    runCount: 3,
    qualityCounts: { ok: 1, partial: 1, unknown: 1 },
    degradedCount: 1,
    degradedRatePct: 33.33,
  }],
  filters: {},
};

const calibrationEvidence = {
  schemaVersion: 1,
  generatedAt: '2026-07-21T04:30:00Z',
  windowDays: 90,
  filters: { triggerSource: 'agent_calibration_shadow', status: 'completed' },
  evaluation: {
    ok: false,
    failures: ['cn:runs_below_threshold', 'cn:mature_samples_below_threshold'],
    requiredMarkets: ['cn', 'hk', 'us'],
    requiredVersions: [],
    thresholds: {
      minRunsPerMarket: 20,
      minObservedPerMarket: 10,
      minObservationRatePct: 80,
      minMatureSamples: 20,
      minObservationDays: 5,
      maxLatestAgeHours: 72,
    },
    markets: {
      cn: {
        ok: false,
        failures: ['runs_below_threshold', 'mature_samples_below_threshold'],
        totalRuns: 9,
        observedRuns: 0,
        unscopedSnapshotCount: 9,
        observationRatePct: 100,
        latestMatureSampleCount: 3,
        effectiveMatureSampleCount: 3,
        persistedLatestMatureSampleCount: 0,
        currentMatureSampleCount: 3,
        matureSampleSource: 'current_shadow_decisions_and_local_daily_bars',
        observationDays: 2,
      },
      hk: {
        ok: false,
        failures: ['runs_below_threshold', 'observation_days_below_threshold'],
        totalRuns: 8,
        observedRuns: 8,
        observationRatePct: 100,
        latestMatureSampleCount: 0,
        observationDays: 1,
      },
      us: {
        ok: false,
        failures: ['runs_below_threshold', 'observation_days_below_threshold'],
        totalRuns: 10,
        observedRuns: 10,
        observationRatePct: 100,
        latestMatureSampleCount: 0,
        observationDays: 1,
      },
    },
  },
  methodology: {
    readOnly: true,
    createsAgentRuns: false,
    placesOrders: false,
    overlappingRollingSamples: true,
    independentSampleCountClaimed: false,
  },
  samplingSchedule: {
    schemaVersion: 1,
    generatedAt: '2026-07-22T05:16:23Z',
    enabled: true,
    intervalMinutes: 1440,
    configuredCount: 3,
    eligibleCount: 0,
    nextEligibleAt: '2026-07-23T02:57:05Z',
    items: [
      {
        market: 'cn',
        strategy: 'dual_low',
        eligible: false,
        nextEligibleAt: '2026-07-23T02:57:05Z',
      },
      {
        market: 'hk',
        strategy: 'hk_liquid_momentum',
        eligible: false,
        nextEligibleAt: '2026-07-23T03:05:45Z',
      },
      {
        market: 'us',
        strategy: 'us_large_cap_momentum',
        eligible: false,
        nextEligibleAt: '2026-07-23T02:58:21Z',
      },
    ],
    readOnly: true,
    createsAgentRuns: false,
    placesOrders: false,
  },
  alertDelivery: {
    status: 'failed' as const,
    trigger: {
      id: 28,
      status: 'degraded',
      reason: 'calibration_evidence_pending',
      triggeredAt: '2026-07-21T04:52:11Z',
    },
    attemptCount: 1,
    successfulCount: 0,
    failedCount: 1,
    retryableFailureCount: 1,
    attempts: [{
      attempt: 1,
      channel: 'feishu',
      success: false,
      errorCode: 'send_failed',
      retryable: true,
    }],
    retryPolicy: {
      status: 'waiting' as const,
      attempt: 1,
      maxAttempts: 3,
      intervalSeconds: 300,
      nextRetryAt: '2026-07-21T04:57:11Z',
    },
  },
};

describe('AgentConsolePage', () => {
  beforeEach(() => {
    listAgentRuns.mockReset();
    exportAgentRuns.mockReset();
    getAgentRun.mockReset();
    getAgentDailySummary.mockReset();
    getAgentDataQualityTrends.mockReset();
    getAgentCalibrationEvidence.mockReset();
    getAgentCrossRunQuality.mockReset();
    runAgentBacktest.mockReset();
    generateAgentRunRecap.mockReset();
    updateAgentRunFeedback.mockReset();
    getReplayCompatibility.mockReset();
    runReplay.mockReset();
    runPortfolioBacktest.mockReset();
    resolveHistoricalUniverse.mockReset();
    startFactorIngestion.mockReset();
    getFactorIngestionTask.mockReset();
    startFullMarketIngestion.mockReset();
    getFullMarketIngestion.mockReset();
    resumeFullMarketIngestion.mockReset();
    listFullMarketIngestions.mockReset();
    listFullMarketIngestions.mockResolvedValue({ items: [], limit: 1 });
    listAgentRuns.mockResolvedValue({
      items: [runSummary, failedRunSummary],
      limit: 25,
      offset: 0,
      total: 2,
    });
    getAgentDailySummary.mockResolvedValue(dailySummary);
    getAgentDataQualityTrends.mockResolvedValue(dataQualityTrends);
    getAgentCalibrationEvidence.mockResolvedValue(calibrationEvidence);
    getAgentCrossRunQuality.mockResolvedValue({
      schemaVersion: 3,
      generatedAt: '2026-07-14T10:00:00',
      state: 'insufficient_evidence',
      reason: 'mature_sample_count_below_threshold',
      previousState: null,
      transition: null,
      changed: false,
      strategy: 'dual_low',
      market: 'cn',
      selectionQualityState: 'insufficient_evidence',
      selectionQualityReason: 'mature_sample_count_below_threshold',
      returnRiskObjectiveState: 'insufficient_evidence',
      returnRiskObjectiveReason: 'return_risk_mature_sample_count_below_threshold',
      returnRiskObjectiveApplied: false,
      returnRiskObjective: {
        version: 'candidate-return-risk-v1',
        metrics: {
          returnRiskUtilityPct: null,
        },
      },
      reviewQualityState: 'insufficient_evidence',
      reviewQualityReason: 'review_mature_sample_count_below_threshold',
      reviewQualityApplied: false,
      reviewPolicyQuality: {
        policy: 'llm_review_then_rule_agent',
        horizons: {
          5: {
            passedPrecisionPct: null,
            blockedAvoidanceRatePct: null,
          },
        },
      },
      horizonDays: 5,
      minMatureSamples: 10,
      minWinRatePct: 45,
      maxDecisions: 200,
      sampleCount: 3,
      matureSampleCount: 0,
      coveragePct: 0,
      winRatePct: null,
      averageReturnPct: null,
      medianReturnPct: null,
      averageMaxAdverseExcursionPct: null,
      averageDailyReturnPct: null,
      dailyReturnCoveragePct: 0,
      dailyReturnVolatilityPct: null,
      downsideDeviationPct: null,
      dailyExpectedShortfall20Pct: null,
      returnRiskUtilityPct: null,
      unableReasonCounts: { insufficient_forward_bars: 3 },
      lookaheadProtection: true,
      source: 'persisted_agent_decisions_and_stock_daily',
      truncated: false,
      gateEnabled: false,
      gateBlocked: false,
      insufficientEvidenceBlocks: false,
    });
    runAgentBacktest.mockResolvedValue({
      generatedAt: '2026-07-14T10:00:00',
      methodology: {
        engineVersion: 'agent-forward-v1',
        lookaheadProtection: true,
      },
      filters: {},
      total: 2,
      scannedCount: 2,
      truncated: false,
      refreshAttemptedCount: 0,
      refreshSucceededCount: 0,
      refreshFailedCount: 0,
      refreshSkippedNotDueCount: 0,
      refreshSourceCounts: {},
      refreshSavedRowCount: 0,
      refreshResolvedAnchorCount: 0,
      refreshUnresolvedAnchorCount: 0,
      statusCounts: { filled: 1, skipped: 1 },
      matrix: {
        1: {
          evalWindowDays: 1,
          sampleCount: 2,
          completedCount: 1,
          insufficientCount: 1,
          coveragePct: 50,
          winCount: 1,
          lossCount: 0,
          neutralCount: 0,
          winRatePct: 100,
          directionAccuracyPct: 100,
          averageReturnPct: 3.5,
          medianReturnPct: 3.5,
          averageMaxFavorableExcursionPct: 5,
          averageMaxAdverseExcursionPct: -1.2,
          dailyObservationCount: 1,
          expectedDailyObservationCount: 1,
          dailyReturnCoveragePct: 100,
          averageDailyReturnPct: 3.5,
          dailyReturnVolatilityPct: 0,
          downsideDeviationPct: 0,
          horizonDownsideDeviationPct: 0,
          dailyExpectedShortfall20Pct: 3.5,
          returnRiskUtilityPct: 3.2,
          returnRiskObjectiveVersion: 'candidate-return-risk-v1',
          neutralBandPct: 2,
          unableReasonCounts: { insufficientForwardBars: 1 },
        },
      },
      strategyMatrix: {},
      reviewQualityMatrix: [
        {
          key: 'llm:openai/model-a:prompt-v2/eval-v1',
          source: 'llm',
          reviewer: 'llm_reviewer_v1',
          model: 'openai/model-a',
          promptVersion: 'prompt-v2',
          evaluatorVersion: 'eval-v1',
          version: 'prompt-v2/eval-v1',
          sampleCount: 2,
          statusCounts: { blocked: 1, passed: 1 },
          horizons: {
            1: {
              evalWindowDays: 1,
              sampleCount: 2,
              completedCount: 2,
              coveragePct: 100,
              passedCompletedCount: 1,
              blockedCompletedCount: 1,
              passedPrecisionPct: 100,
              blockedAvoidanceRatePct: 100,
              passedAverageReturnPct: 3.5,
              blockedAverageReturnPct: -2.5,
              returnSpreadPct: 6,
              unableReasonCounts: {},
            },
          },
        },
      ],
      items: [],
    });
    const replayCompatibility = {
      strategy: 'dual_low',
      market: 'cn',
      snapshotDate: '2024-01-05',
      universeCount: 100,
      requiredHardFields: ['amount', 'pe_ratio'],
      requiredScoreFields: ['rsi14'],
      hardCompleteRows: 98,
      scoreCompleteRows: 90,
      hardCoverageRatio: 0.98,
      scoreCoverageRatio: 0.9,
      hardMissingCounts: { peRatio: 2 },
      scoreMissingCounts: { rsi14: 10 },
    };
    getReplayCompatibility.mockResolvedValue(replayCompatibility);
    runReplay.mockResolvedValue({
      strategy: 'dual_low',
      market: 'cn',
      snapshotDate: '2024-01-05',
      universeCount: 100,
      completeRowCount: 90,
      filteredCount: 4,
      candidateCount: 1,
      candidates: [{
        symbol: '600519',
        name: '贵州茅台',
        industry: '白酒',
        price: 1600,
        screenScore: 88.5,
      }],
      compatibility: replayCompatibility,
      methodology: {
        pointInTime: true,
        lookaheadProtection: true,
        usesCurrentSnapshotFallback: false,
        llmRankingEnabled: false,
        ranking: 'alphasift_hard_filters_and_screen_score',
      },
    });
    runPortfolioBacktest.mockResolvedValue({
      strategy: 'dual_low',
      market: 'cn',
      dateFrom: '2024-01-01',
      dateTo: '2024-02-01',
      snapshotCount: 2,
      selectedCount: 10,
      evaluatedCount: 9,
      coveragePct: 90,
      initialCapital: 100000,
      finalEquity: 108000,
      benchmarkSymbol: '000300',
      metrics: {
        totalReturnPct: 8,
        benchmarkReturnPct: 3,
        excessReturnPct: 5,
        maxDrawdownPct: -2,
        periodWinRatePct: 50,
        periodCount: 2,
        totalTurnoverPct: 250,
        averageTurnoverPct: 125,
        endingOpenPositionCount: 1,
        endingCash: 12000,
        totalFees: 20,
        totalTaxes: 10,
        cashInLieuReceived: 4,
        totalSlippageCost: 30,
      },
      periods: [{
        signalDate: '2024-01-05',
        selectedCount: 5,
        evaluatedCount: 5,
        coveragePct: 100,
        netReturnPct: 4,
        turnoverPct: 100,
        retainedCount: 2,
        forcedRetainedCount: 1,
        entryTradeCount: 3,
        exitTradeCount: 3,
        cash: 12000,
        positionCount: 4,
        equity: 104000,
        processedCorporateActionCount: 1,
        appliedCorporateActionCount: 1,
        corporateActions: [{
          symbol: '600519',
          effectiveDate: '2024-01-15',
          actionType: 'split_adjustment',
          status: 'applied',
          fractionalQuantity: 0.5,
          cashInLieuEffect: 4,
          cashEffect: 4,
        }],
      }],
      methodology: {
        lookaheadProtection: true,
        configuredTargetWeights: { '600519': 60, '000001': 30 },
        costProfile: 'cn_retail_reference',
        costProfileVersion: 'dsa-cost-profile-v1',
        effectiveCostAssumptions: {
          commissionBps: 3,
          minimumCommission: 5,
          sellTaxBps: 0,
          sellTaxMode: 'cn_historical_stamp_duty',
          slippageBps: 5,
        },
        historicalTaxCoverageFrom: '2005-01-24',
        sellTaxRegimes: [{
          effectiveFrom: '2007-05-30',
          buyTaxBps: 30,
          sellTaxBps: 30,
          source: 'mof_tax_2007_84_bilateral',
        }, {
          effectiveFrom: '2008-09-19',
          buyTaxBps: 0,
          sellTaxBps: 10,
          source: 'mof_2008_09_19_single_sided',
        }],
      },
    });
    resolveHistoricalUniverse.mockResolvedValue({
      market: 'cn',
      snapshotDate: '2024-01-05',
      totalCount: 5100,
      returnedCount: 5100,
      truncated: false,
      items: [],
      methodology: { usesCurrentUniverseFallback: false },
    });
    startFullMarketIngestion.mockResolvedValue({
      jobId: 'full-job-1', market: 'cn', snapshotDates: ['2024-01-05'], universeSource: 'tushare_stock_basic',
      status: 'failed', batchSize: 25, totalSymbols: 5000, totalWorkItems: 5000, nextOffset: 50,
      remainingWorkItems: 4950, progressPct: 1, completedBatches: 2, rowCount: 50, inserted: 50,
      updated: 0, sourceErrorCount: 1, errors: [], error: 'source timeout',
      recoveryState: 'retryable', resumeAllowed: true, forceTakeoverRequired: false,
    });
    resumeFullMarketIngestion.mockResolvedValue({
      jobId: 'full-job-1', market: 'cn', snapshotDates: ['2024-01-05'], universeSource: 'tushare_stock_basic',
      status: 'pending', batchSize: 25, totalSymbols: 5000, totalWorkItems: 5000, nextOffset: 50,
      remainingWorkItems: 4950, progressPct: 1, completedBatches: 2, rowCount: 50, inserted: 50,
      updated: 0, sourceErrorCount: 1, errors: [], recoveryState: 'active', resumeAllowed: false,
    });
    startFactorIngestion.mockResolvedValue({
      taskId: 'factor-task-1',
      traceId: 'factor-task-1',
      status: 'pending',
      message: 'submitted',
      market: 'cn',
      snapshotCount: 1,
      symbolCount: 1,
    });
    getFactorIngestionTask.mockResolvedValue({
      taskId: 'factor-task-1',
      status: 'completed',
      progress: 100,
      result: { rowCount: 1, inserted: 1, updated: 0, errorCount: 0, errors: [] },
    });
    exportAgentRuns.mockResolvedValue({
      generatedAt: '2026-07-02T00:00:00Z',
      limit: 50,
      includeDetails: true,
      count: 2,
      items: [runDetail, failedRunDetail],
    });
    getAgentRun.mockImplementation((runUid: string) => (
      runUid === 'ss-agent-failed'
        ? Promise.resolve(failedRunDetail)
        : Promise.resolve(runDetail)
    ));
    generateAgentRunRecap.mockResolvedValue({
      accepted: true,
      status: 'completed',
      reason: null,
      runUid: 'ss-agent-test',
      llmRecap: {
        status: 'completed',
        content: 'LLM recap generated',
      },
      runDetail,
    });
    updateAgentRunFeedback.mockImplementation(async (_runUid, payload) => ({
      accepted: true,
      runUid: 'ss-agent-test',
      humanFeedback: {
        id: 7,
        runId: 1,
        verdict: payload.verdict,
        note: payload.note,
        reviewer: payload.reviewer,
        source: 'web',
      },
      runDetail: {
        ...runDetail,
        humanFeedback: {
          id: 7,
          runId: 1,
          verdict: payload.verdict,
          note: payload.note,
          reviewer: payload.reviewer,
          source: 'web',
        },
      },
    }));
  });

  it('renders Agent run history and selected run details', async () => {
    renderPage();

    await waitFor(() => expect(listAgentRuns).toHaveBeenCalledWith(25, 0, undefined));
    await waitFor(() => expect(getAgentDailySummary).toHaveBeenCalledWith(undefined, undefined));
    await waitFor(() => expect(getAgentDataQualityTrends).toHaveBeenCalledWith(30, undefined));
    await waitFor(() => expect(getAgentCalibrationEvidence).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(getAgentRun).toHaveBeenCalledWith('ss-agent-test'));
    expect(screen.getByText('今日 Agent 总结')).toBeInTheDocument();
    expect(screen.getByTestId('agent-calibration-evidence')).toHaveTextContent('生产校准证据');
    expect(screen.getByTestId('agent-calibration-evidence')).toHaveTextContent('9 / 0');
    expect(screen.getByTestId('agent-calibration-evidence')).toHaveTextContent('8 / 8');
    expect(screen.getByTestId('agent-calibration-evidence')).toHaveTextContent('10 / 10');
    expect(screen.getByTestId('agent-calibration-evidence')).toHaveTextContent('3（快照 0）');
    expect(screen.getByTestId('agent-calibration-evidence')).toHaveTextContent('旧快照 9');
    expect(screen.getByTestId('agent-calibration-evidence')).toHaveTextContent('下一次采样');
    expect(screen.getByTestId('agent-calibration-evidence')).toHaveTextContent('dual_low');
    expect(screen.getByTestId('agent-calibration-evidence')).toHaveTextContent('hk_liquid_momentum');
    expect(screen.getByTestId('agent-calibration-evidence')).toHaveTextContent('成熟前瞻样本不足');
    expect(screen.getByTestId('agent-calibration-alert-delivery')).toHaveTextContent('通知失败');
    expect(screen.getByTestId('agent-calibration-alert-delivery')).toHaveTextContent('feishu #1: 失败 (send_failed)');
    expect(screen.getByTestId('agent-calibration-alert-delivery')).toHaveTextContent('可重试 1');
    expect(screen.getByTestId('agent-calibration-alert-retry')).toHaveTextContent('等待重试窗口');
    expect(screen.getByTestId('agent-calibration-alert-retry')).toHaveTextContent('1/3');
    expect(screen.getByTestId('agent-current-cross-run-quality')).toHaveTextContent('insufficient_evidence');
    expect(screen.getByTestId('agent-current-cross-run-quality')).toHaveTextContent('0/3');
    expect(screen.getByTestId('agent-current-cross-run-quality')).toHaveTextContent('仅审计');
    expect(screen.getByTestId('agent-review-quality-gate-state')).toHaveTextContent('最终复核质量 insufficient_evidence');
    expect(screen.getByTestId('agent-review-quality-gate-metrics')).toHaveTextContent('复核通过精度');
    expect(screen.getByTestId('agent-return-risk-objective-state')).toHaveTextContent(
      '收益风险目标 insufficient_evidence',
    );
    expect(screen.getByTestId('agent-return-risk-objective-state')).toHaveTextContent(
      'candidate-return-risk-v1',
    );
    expect(screen.getByTestId('agent-return-risk-objective-metrics')).toHaveTextContent('收益风险效用');
    expect(screen.getByText('2026-07-01 · 2/2 runs scanned')).toBeInTheDocument();
    expect(screen.getByTestId('agent-data-quality-trends')).toHaveTextContent('跨 run 数据质量趋势');
    expect(screen.getByTestId('agent-data-quality-trends')).toHaveTextContent('33.33%');
    expect(screen.getByTestId('agent-data-quality-trends')).toHaveTextContent('1 / 1 / 0 / 0 / 1');
    expect(screen.getByTestId('agent-source-health-trends')).toHaveTextContent('snapshot / sina');
    expect(screen.getByTestId('agent-source-health-trends')).toHaveTextContent('2 / 3');
    expect(screen.getByTestId('agent-source-health-trends')).toHaveTextContent('66.67%');
    fireEvent.click(screen.getByRole('button', { name: '7天' }));
    await waitFor(() => expect(getAgentDataQualityTrends).toHaveBeenCalledWith(7, undefined));
    expect(screen.getByText('LLM 复核')).toBeInTheDocument();
    expect(screen.getByText('passed 1')).toBeInTheDocument();
    expect(screen.getAllByText('复核质量').length).toBeGreaterThan(0);
    expect(screen.getByText('92.5')).toBeInTheDocument();
    expect(screen.getByText('guarded 1 / idle 1')).toBeInTheDocument();
    expect(screen.getByText('复核风险')).toBeInTheDocument();
    expect(screen.getByText('agent_review_blocked 1')).toBeInTheDocument();
    expect(screen.getByText('工作流阶段')).toBeInTheDocument();
    expect(screen.getByText('execution 1')).toBeInTheDocument();
    expect(screen.getAllByText('ss-agent-test').length).toBeGreaterThan(0);
    expect(screen.getByText('2 candidate decisions: buy=1, skip=1')).toBeInTheDocument();
    expect(screen.getByText('llm_ranking_health · skipped')).toBeInTheDocument();
    expect(screen.getByText(/decision=circuit_open, result=skipped, state=open/)).toBeInTheDocument();
    expect(screen.getByText('market_context_risk · blocked')).toBeInTheDocument();
    expect(screen.getByText(/market_context_stale, breadth=28, hotspot_drop=null, age_days=8/)).toBeInTheDocument();
    expect(screen.getByTestId('agent-workflow')).toHaveTextContent('execution');
    expect(screen.getByTestId('agent-workflow')).toHaveTextContent('monitor_positions_and_events');
    expect(screen.getByText('Candidate review warning')).toBeInTheDocument();
    expect(screen.getByText('low valuation and improving momentum')).toBeInTheDocument();
    expect(screen.getByTestId('portfolio-change-panel')).toHaveTextContent('本轮持仓变化');
    expect(screen.getByTestId('portfolio-change-panel')).toHaveTextContent('已入账');
    expect(screen.getByTestId('portfolio-change-panel')).toHaveTextContent('+100');
    expect(screen.getByTestId('portfolio-change-panel')).toHaveTextContent('¥1,000.00');
    expect(screen.getByText('local_paper_execution')).toBeInTheDocument();
    expect(screen.getAllByText('guarded').length).toBeGreaterThan(0);
    expect(screen.getByText('LLM 动态计划')).toBeInTheDocument();
    expect(screen.getByText('最近运行')).toBeInTheDocument();
    expect(screen.getByText('5 次')).toBeInTheDocument();
    expect(screen.getByText('历史成交率')).toBeInTheDocument();
    expect(screen.getByText('40.0%')).toBeInTheDocument();
    expect(screen.getByText(/failed · ss-agent-previous · 连续失败 1/)).toBeInTheDocument();
    expect(screen.getByTestId('cross-run-quality-state')).toHaveTextContent('healthy');
    expect(screen.getByTestId('cross-run-quality-state')).toHaveTextContent('20 个成熟样本');
    expect(screen.getByTestId('cross-run-quality-state')).toHaveTextContent('blocked->healthy');
    expect(screen.getByTestId('cross-market-objective')).toHaveTextContent('liquidity_and_momentum');
    expect(screen.getByTestId('cross-market-objective')).toHaveTextContent('3 → 2');
    expect(screen.getByTestId('cross-market-objective')).toHaveTextContent('latest_run_failed');
    expect(screen.getByTestId('agent-source-routing')).toHaveTextContent('dynamic_health');
    expect(screen.getByTestId('agent-source-routing')).toHaveTextContent('efinance,sina');
    expect(screen.getByTestId('agent-source-routing')).toHaveTextContent('已按健康权重调整');
    expect(screen.getByTestId('agent-source-routing')).toHaveTextContent('cn/efinance open');
    expect(screen.getByTestId('agent-source-routing')).toHaveTextContent('失败 3，冷却 240s');
    expect(screen.getByTestId('agent-source-routing')).toHaveTextContent('cn/akshare_em closed');
    expect(screen.getByTestId('agent-source-routing')).toHaveTextContent('资金流');
    expect(screen.getByTestId('agent-source-routing')).toHaveTextContent('cn/tushare_ths open');
    expect(screen.getByTestId('agent-source-routing')).toHaveTextContent('失败 3，冷却 180s');
    expect(screen.getByTestId('agent-source-routing')).toHaveTextContent('cn/akshare closed');
    expect(screen.getByTestId('agent-news-source-routing')).toHaveTextContent('新闻：tavily → anspire');
    expect(screen.getByTestId('agent-news-source-routing')).toHaveTextContent('anspire open');
    expect(screen.getByTestId('agent-news-source-routing')).toHaveTextContent('失败 3，冷却 120s');
    expect(screen.getByTestId('agent-news-source-routing')).toHaveTextContent('阈值 3 · 半开 1');
    expect(screen.getByTestId('agent-news-source-routing')).toHaveTextContent('跨重启恢复');
    expect(screen.getByTestId('agent-news-source-routing')).toHaveTextContent('下轮 anspire → tavily');
    expect(screen.getAllByText('score 85.0').length).toBeGreaterThan(0);
    expect(screen.getByText('建议人工确认')).toBeInTheDocument();
    expect(screen.getAllByText('100.0%').length).toBeGreaterThan(0);
    expect(screen.getByText('50.0%')).toBeInTheDocument();
    expect(screen.getByText('agent_review_blocked')).toBeInTheDocument();
    expect(screen.getByText(/autoStrategy=capital_heat/)).toBeInTheDocument();
    expect(screen.getByText(/autoMaxResults=1/)).toBeInTheDocument();
    expect(screen.getByText('Prefer a narrower heat strategy today.')).toBeInTheDocument();
    expect(screen.getByText('prompt=vnpy_paper_dynamic_agent_plan_v2 / eval=dynamic_plan_guardrails_v1')).toBeInTheDocument();
    expect(screen.getByTestId('portfolio-allocation-summary')).toHaveTextContent('score_inverse_volatility_20d_capped');
    expect(screen.getByTestId('portfolio-allocation-summary')).toHaveTextContent('10,000');
    expect(screen.getByTestId('portfolio-optimizer-summary')).toHaveTextContent('target_tracking_min_variance_20d_v1');
    expect(screen.getByTestId('portfolio-optimizer-summary')).toHaveTextContent('样本 20');
    expect(screen.getByText('partial / 82.5')).toBeInTheDocument();
    expect(screen.getByTestId('portfolio-allocation-1')).toHaveTextContent('80.0%');
    expect(screen.getByTestId('portfolio-allocation-1')).toHaveTextContent('10,000');
    expect(screen.getByTestId('portfolio-risk-input-1')).toHaveTextContent('12.5%');
    expect(screen.getByTestId('portfolio-risk-input-1')).toHaveTextContent('6.4000');
    expect(screen.getByTestId('portfolio-correlation-input-1')).toHaveTextContent('60');
    expect(screen.getByTestId('portfolio-correlation-input-1')).toHaveTextContent('000001=0.42');
    expect(screen.getByTestId('portfolio-optimizer-input-1')).toHaveTextContent('目标权重 80.0%');
    expect(screen.getByTestId('portfolio-optimizer-input-1')).toHaveTextContent('优化权重 75.0%');
    expect(screen.getByTestId('portfolio-covariance-input-1')).toHaveTextContent('协方差样本 20');
    expect(screen.getByTestId('strategy-evidence-1')).toHaveTextContent('策略证据 detailed');
    expect(screen.getByTestId('strategy-evidence-1')).toHaveTextContent('dual_low · 排名 1.00 · 筛选分 78.00');
    expect(screen.getByTestId('strategy-matches-1')).toHaveTextContent('pe_below_limit=passed');
    expect(screen.getByTestId('strategy-matches-1')).toHaveTextContent('momentum_positive=是');
    expect(screen.getByTestId('strategy-factors-1')).toHaveTextContent('value=88.00');
    expect(screen.getByTestId('strategy-factors-1')).toHaveTextContent('momentum=72.50');
    expect(screen.getByTestId('agent-llm-recap')).toHaveTextContent('LLM recap generated');
    expect(screen.getByTestId('agent-review-1')).toHaveTextContent('Agent 复核 passed');
    expect(screen.getByText('Top-level Agent review passed')).toBeInTheDocument();
    expect(screen.getByTestId('llm-review-1')).toHaveTextContent('LLM 复核 passed');
    expect(screen.getByText('Top-level LLM review passed')).toBeInTheDocument();
    expect(screen.getByText('prompt=vnpy_paper_pre_trade_review_v2 / eval=candidate_audit_columns_v1')).toBeInTheDocument();
    expect(screen.getAllByText('600519').length).toBeGreaterThan(0);
    expect(screen.getAllByText('position_exists').length).toBeGreaterThan(0);
  });

  it('generates an optional LLM recap for the selected Agent run', async () => {
    renderPage();

    await waitFor(() => expect(getAgentRun).toHaveBeenCalledWith('ss-agent-test'));
    fireEvent.click(await screen.findByText('生成复盘'));

    await waitFor(() => expect(generateAgentRunRecap).toHaveBeenCalledWith('ss-agent-test', 800));
    expect(await screen.findByText('已生成 ss-agent-test LLM 复盘')).toBeInTheDocument();
  });

  it('saves human acceptance feedback for the selected Agent run', async () => {
    renderPage();

    await waitFor(() => expect(getAgentRun).toHaveBeenCalledWith('ss-agent-test'));
    fireEvent.click(await screen.findByRole('button', { name: '需修改' }));
    fireEvent.change(screen.getByTestId('agent-human-feedback-note'), {
      target: { value: 'Reduce concentration before the next run.' },
    });
    fireEvent.change(screen.getByTestId('agent-human-feedback-reviewer'), {
      target: { value: 'risk-owner' },
    });
    fireEvent.click(screen.getByRole('button', { name: '保存验收结论' }));

    await waitFor(() => expect(updateAgentRunFeedback).toHaveBeenCalledWith('ss-agent-test', {
      verdict: 'needs_changes',
      note: 'Reduce concentration before the next run.',
      reviewer: 'risk-owner',
    }));
    expect(await screen.findByText('已保存 ss-agent-test 人工验收结论')).toBeInTheDocument();
    expect(screen.getByTestId('agent-human-feedback')).toHaveTextContent('needs_changes');
  });

  it('applies filters to the Agent run list API', async () => {
    renderPage();

    await waitFor(() => expect(listAgentRuns).toHaveBeenCalledWith(25, 0, undefined));
    fireEvent.change(screen.getByTestId('agent-run-strategy-filter'), { target: { value: 'dual_low' } });
    fireEvent.change(screen.getByTestId('agent-run-market-filter'), { target: { value: 'cn' } });
    fireEvent.change(screen.getByTestId('agent-run-status-filter'), { target: { value: 'completed' } });
    fireEvent.change(screen.getByTestId('agent-run-created-from-filter'), { target: { value: '2026-07-01T09:00' } });
    fireEvent.change(screen.getByTestId('agent-run-created-to-filter'), { target: { value: '2026-07-01T15:00' } });
    fireEvent.submit(screen.getByTestId('agent-run-filters'));

    await waitFor(() => expect(listAgentRuns).toHaveBeenLastCalledWith(25, 0, {
      strategy: 'dual_low',
      market: 'cn',
      status: 'completed',
      createdFrom: '2026-07-01T09:00',
      createdTo: '2026-07-01T15:00',
    }));
    await waitFor(() => expect(getAgentDailySummary).toHaveBeenLastCalledWith(undefined, {
      strategy: 'dual_low',
      market: 'cn',
      status: 'completed',
      createdFrom: '2026-07-01T09:00',
      createdTo: '2026-07-01T15:00',
    }));
  });

  it('runs forward evaluation with the current strategy, market, and date filters', async () => {
    renderPage();

    await waitFor(() => expect(listAgentRuns).toHaveBeenCalled());
    fireEvent.change(screen.getByTestId('agent-run-strategy-filter'), { target: { value: 'dual_low' } });
    fireEvent.change(screen.getByTestId('agent-run-market-filter'), { target: { value: 'cn' } });
    fireEvent.change(screen.getByTestId('agent-run-created-from-filter'), { target: { value: '2026-07-01T09:00' } });
    fireEvent.change(screen.getByTestId('agent-run-created-to-filter'), { target: { value: '2026-07-10T15:00' } });
    fireEvent.click(screen.getByTestId('agent-backtest-include-skipped'));
    fireEvent.click(screen.getByTestId('agent-backtest-run'));

    await waitFor(() => expect(runAgentBacktest).toHaveBeenCalledWith({
      strategy: 'dual_low',
      market: 'cn',
      createdFrom: '2026-07-01T09:00',
      createdTo: '2026-07-10T15:00',
      evalWindows: [1, 5, 10, 20],
      includeSkipped: false,
      maxDecisions: 500,
      refreshMissing: false,
    }));
    expect(await screen.findByTestId('agent-backtest-matrix')).toHaveTextContent('3.5%');
    expect(screen.getByTestId('agent-backtest-matrix')).toHaveTextContent('风险效用 3.2%');
    expect(screen.getByTestId('agent-backtest-matrix')).toHaveTextContent('尾部收益 3.5%');
    expect(screen.getByTestId('agent-backtest-panel')).toHaveTextContent('前视保护 已启用');
    expect(screen.getByTestId('agent-review-quality-matrix')).toHaveTextContent('openai/model-a');
    expect(screen.getByTestId('agent-review-quality-matrix')).toHaveTextContent('prompt-v2/eval-v1');
    expect(screen.getByTestId('agent-review-quality-matrix')).toHaveTextContent('阻断避损 100.0%');
    expect(screen.getByTestId('agent-review-quality-matrix')).toHaveTextContent('收益差 6.0%');
  });

  it('checks point-in-time factor coverage and runs strategy replay', async () => {
    renderPage();

    await waitFor(() => expect(listAgentRuns).toHaveBeenCalled());
    fireEvent.change(screen.getByTestId('agent-run-strategy-filter'), { target: { value: 'dual_low' } });
    fireEvent.change(screen.getByTestId('agent-run-market-filter'), { target: { value: 'cn' } });
    fireEvent.change(screen.getByTestId('agent-replay-date'), { target: { value: '2024-01-05' } });
    fireEvent.click(screen.getByTestId('agent-replay-check'));

    await waitFor(() => expect(getReplayCompatibility).toHaveBeenCalledWith({
      strategy: 'dual_low',
      market: 'cn',
      snapshotDate: '2024-01-05',
    }));
    expect(await screen.findByTestId('agent-replay-compatibility')).toHaveTextContent('98.0%');
    expect(screen.getByTestId('agent-replay-missing-fields')).toHaveTextContent('rsi14 缺失 10');

    fireEvent.click(screen.getByTestId('agent-replay-run'));
    await waitFor(() => expect(runReplay).toHaveBeenCalledWith({
      strategy: 'dual_low',
      market: 'cn',
      snapshotDate: '2024-01-05',
      maxResults: 20,
    }));
    expect(await screen.findByTestId('agent-replay-results')).toHaveTextContent('600519');
    expect(screen.getByTestId('agent-replay-results')).toHaveTextContent('88.50');
  });

  it('runs a cost-aware point-in-time portfolio backtest', async () => {
    renderPage();

    await waitFor(() => expect(listAgentRuns).toHaveBeenCalled());
    fireEvent.change(screen.getByTestId('agent-run-strategy-filter'), { target: { value: 'dual_low' } });
    fireEvent.change(screen.getByTestId('agent-run-market-filter'), { target: { value: 'cn' } });
    fireEvent.change(screen.getByTestId('agent-portfolio-date-from'), { target: { value: '2024-01-01' } });
    fireEvent.change(screen.getByTestId('agent-portfolio-date-to'), { target: { value: '2024-02-01' } });
    fireEvent.change(screen.getByTestId('agent-portfolio-target-weights'), {
      target: { value: '600519=60, 000001=30' },
    });
    fireEvent.change(screen.getByTestId('agent-portfolio-minimum-commission'), {
      target: { value: '5' },
    });
    fireEvent.change(screen.getByTestId('agent-portfolio-sell-tax-bps'), {
      target: { value: '5' },
    });
    fireEvent.change(screen.getByTestId('agent-portfolio-sell-tax-mode'), {
      target: { value: 'cn_historical_stamp_duty' },
    });
    fireEvent.change(screen.getByTestId('agent-portfolio-cost-profile'), {
      target: { value: 'cn_retail_reference' },
    });
    expect(screen.getByTestId('agent-portfolio-commission-bps')).toBeDisabled();
    expect(screen.getByTestId('agent-portfolio-minimum-commission')).toBeDisabled();
    expect(screen.getByTestId('agent-portfolio-sell-tax-mode')).toBeDisabled();
    expect(screen.getByTestId('agent-portfolio-slippage-bps')).toBeDisabled();
    fireEvent.change(screen.getByTestId('agent-portfolio-corporate-actions'), {
      target: {
        value: '[{"symbol":"600519","effective_date":"2024-01-15","action_type":"cash_dividend","cash_dividend_per_share":1.5},{"symbol":"000001","effective_date":"2024-01-20","action_type":"split_adjustment","split_ratio":1.5,"cash_in_lieu_price":9.8}]',
      },
    });
    fireEvent.click(screen.getByTestId('agent-portfolio-backtest-run'));

    await waitFor(() => expect(runPortfolioBacktest).toHaveBeenCalledWith({
      strategy: 'dual_low',
      market: 'cn',
      dateFrom: '2024-01-01',
      dateTo: '2024-02-01',
      topK: 5,
      benchmarkSymbol: '000300',
      targetWeights: { '600519': 60, '000001': 30 },
      costProfile: 'cn_retail_reference',
      commissionBps: 3,
      minimumCommission: 5,
      sellTaxBps: 0,
      sellTaxMode: 'cn_historical_stamp_duty',
      slippageBps: 5,
      corporateActions: [{
        symbol: '600519',
        effectiveDate: '2024-01-15',
        actionType: 'cash_dividend',
        cashDividendPerShare: 1.5,
      }, {
        symbol: '000001',
        effectiveDate: '2024-01-20',
        actionType: 'split_adjustment',
        splitRatio: 1.5,
        cashInLieuPrice: 9.8,
      }],
      includePersistedCorporateActions: true,
    }));
    const results = await screen.findByTestId('agent-portfolio-backtest-results');
    expect(results).toHaveTextContent('8.0%');
    expect(results).toHaveTextContent('5.0%');
    expect(results).toHaveTextContent('2024-01-05');
    expect(results).toHaveTextContent('125.0%');
    expect(results).toHaveTextContent('延续');
    expect(results).toHaveTextContent('被动');
    expect(results).toHaveTextContent('期末未平 1');
    expect(results).toHaveTextContent('零碎股补偿 ¥4.00');
    expect(screen.getByTestId('agent-portfolio-corporate-action-audit')).toHaveTextContent('补偿 ¥4.00');
    expect(screen.getByTestId('agent-portfolio-target-audit')).toHaveTextContent('600519 60.0%');
    expect(screen.getByTestId('agent-portfolio-target-audit')).toHaveTextContent('000001 30.0%');
    expect(screen.getByTestId('agent-portfolio-cost-profile-audit')).toHaveTextContent(
      '成本档位：cn_retail_reference · dsa-cost-profile-v1',
    );
    expect(screen.getByTestId('agent-portfolio-cost-profile-audit')).toHaveTextContent(
      '佣金 3.00 bps · 最低 ¥5.00 · 税制 cn_historical_stamp_duty · 滑点 5.00 bps',
    );
    expect(screen.getByTestId('agent-portfolio-tax-regimes')).toHaveTextContent('历史税制覆盖：2005-01-24');
    expect(screen.getByTestId('agent-portfolio-tax-regimes')).toHaveTextContent('2007-05-30 买 30.00 bps / 卖 30.00 bps');
    expect(screen.getByTestId('agent-portfolio-tax-regimes')).toHaveTextContent('2008-09-19 买 0.00 bps / 卖 10.00 bps');
    expect(results).toHaveTextContent('成本费用');
    expect(results).toHaveTextContent('¥60.00');
    expect(screen.getByTestId('agent-portfolio-corporate-action-audit')).toHaveTextContent(
      '600519 split_adjustment applied',
    );
  });

  it('rejects a cash-in-lieu price on a cash dividend before submitting replay', async () => {
    renderPage();

    await waitFor(() => expect(listAgentRuns).toHaveBeenCalled());
    fireEvent.change(screen.getByTestId('agent-portfolio-date-from'), { target: { value: '2024-01-01' } });
    fireEvent.change(screen.getByTestId('agent-portfolio-date-to'), { target: { value: '2024-02-01' } });
    fireEvent.change(screen.getByTestId('agent-portfolio-corporate-actions'), {
      target: {
        value: '[{"symbol":"600519","effective_date":"2024-01-15","action_type":"cash_dividend","cash_dividend_per_share":1.5,"cash_in_lieu_price":10}]',
      },
    });
    fireEvent.click(screen.getByTestId('agent-portfolio-backtest-run'));

    expect(await screen.findByRole('alert')).toHaveTextContent('第 1 个现金分红不能配置零碎股补偿价');
    expect(runPortfolioBacktest).not.toHaveBeenCalled();
  });

  it('submits and observes bounded historical factor ingestion', async () => {
    renderPage();

    await waitFor(() => expect(listAgentRuns).toHaveBeenCalled());
    fireEvent.change(screen.getByTestId('agent-run-market-filter'), { target: { value: 'cn' } });
    fireEvent.change(screen.getByTestId('agent-ingestion-dates'), { target: { value: '2024-01-05' } });
    fireEvent.change(screen.getByTestId('agent-ingestion-universe'), {
      target: { value: '[{"symbol":"600519","name":"贵州茅台","industry":"白酒"}]' },
    });
    fireEvent.click(screen.getByTestId('agent-ingestion-start'));

    await waitFor(() => expect(startFactorIngestion).toHaveBeenCalledWith({
      market: 'cn',
      snapshotDates: ['2024-01-05'],
      universe: [{ symbol: '600519', name: '贵州茅台', industry: '白酒' }],
    }));
    await waitFor(() => expect(getFactorIngestionTask).toHaveBeenCalledWith('factor-task-1'));
    expect(await screen.findByTestId('agent-ingestion-result')).toHaveTextContent('写入 1');
  });

  it('previews a survivor-bias-aware historical universe', async () => {
    renderPage();

    await waitFor(() => expect(listAgentRuns).toHaveBeenCalled());
    fireEvent.change(screen.getByTestId('agent-ingestion-dates'), { target: { value: '2024-01-05' } });
    fireEvent.click(screen.getByTestId('agent-historical-universe-resolve'));

    await waitFor(() => expect(resolveHistoricalUniverse).toHaveBeenCalledWith({
      market: 'cn',
      snapshotDate: '2024-01-05',
      limit: 6000,
    }));
    expect(await screen.findByTestId('agent-historical-universe-result')).toHaveTextContent('成员 5100');
  });

  it('starts and resumes a checkpointed full-market ingestion job', async () => {
    renderPage();
    await waitFor(() => expect(listAgentRuns).toHaveBeenCalled());
    fireEvent.change(screen.getByTestId('agent-ingestion-dates'), { target: { value: '2024-01-05' } });
    fireEvent.click(screen.getByTestId('agent-full-market-ingestion-start'));

    await waitFor(() => expect(startFullMarketIngestion).toHaveBeenCalledWith({
      market: 'cn', snapshotDates: ['2024-01-05'], batchSize: 25,
    }));
    expect(await screen.findByTestId('agent-full-market-ingestion-result')).toHaveTextContent('工作项 50/5000');
    fireEvent.click(screen.getByTestId('agent-full-market-ingestion-resume'));
    await waitFor(() => expect(resumeFullMarketIngestion).toHaveBeenCalledWith('full-job-1', false));
  });

  it('protects an active full-market ingestion job from duplicate takeover', async () => {
    listFullMarketIngestions.mockResolvedValue({
      items: [{
        jobId: 'full-job-active', market: 'cn', snapshotDates: ['2024-01-05'],
        universeSource: 'tushare_stock_basic', status: 'processing', taskStatus: 'running',
        batchSize: 25, totalSymbols: 5000, totalWorkItems: 5000, nextOffset: 50,
        remainingWorkItems: 4950, progressPct: 1, completedBatches: 2, rowCount: 50,
        inserted: 50, updated: 0, sourceErrorCount: 0, errors: [], recoveryState: 'active',
        resumeAllowed: false, forceTakeoverRequired: true,
      }],
      limit: 1,
    });

    renderPage();

    expect(await screen.findByText('后台任务仍在运行，不允许重复接管')).toBeInTheDocument();
    expect(screen.queryByTestId('agent-full-market-ingestion-resume')).not.toBeInTheDocument();
  });

  it('pages through Agent run history with offset', async () => {
    listAgentRuns
      .mockResolvedValueOnce({ items: [runSummary], limit: 25, offset: 0, total: 30 })
      .mockResolvedValueOnce({ items: [failedRunSummary], limit: 25, offset: 25, total: 30 });

    renderPage();

    await waitFor(() => expect(listAgentRuns).toHaveBeenCalledWith(25, 0, undefined));
    fireEvent.click(screen.getByTestId('agent-run-next-page'));

    await waitFor(() => expect(listAgentRuns).toHaveBeenLastCalledWith(25, 25, undefined));
    expect(await screen.findByText('Data quality stale: cached screen result')).toBeInTheDocument();
  });

  it('loads a different run detail and exports filtered runs', async () => {
    const originalCreateObjectURL = URL.createObjectURL;
    const originalRevokeObjectURL = URL.revokeObjectURL;
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
    Object.defineProperty(URL, 'createObjectURL', {
      configurable: true,
      value: vi.fn(() => 'blob:agent-console'),
    });
    Object.defineProperty(URL, 'revokeObjectURL', {
      configurable: true,
      value: vi.fn(),
    });

    try {
      renderPage();

      await waitFor(() => expect(getAgentRun).toHaveBeenCalledWith('ss-agent-test'));
      fireEvent.click(await screen.findByTestId('agent-run-row-ss-agent-failed'));

      await waitFor(() => expect(getAgentRun).toHaveBeenCalledWith('ss-agent-failed'));
      expect(await screen.findByText('Data quality stale: cached screen result')).toBeInTheDocument();

      fireEvent.click(screen.getByTestId('agent-run-export-runs'));

      await waitFor(() => expect(exportAgentRuns).toHaveBeenCalledWith(50, true, undefined));
      expect(clickSpy).toHaveBeenCalled();
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

  it('opens an Agent run detail from the dedicated route path', async () => {
    renderPage('/agent-console/ss-agent-failed');

    await waitFor(() => expect(listAgentRuns).toHaveBeenCalledWith(25, 0, undefined));
    await waitFor(() => expect(getAgentRun).toHaveBeenCalledWith('ss-agent-failed'));
    expect(await screen.findByText('Data quality stale: cached screen result')).toBeInTheDocument();
  });

  it('opens a deep-linked Agent run detail from the URL query', async () => {
    renderPage('/agent-console?runUid=ss-agent-failed');

    await waitFor(() => expect(getAgentRun).toHaveBeenCalledWith('ss-agent-failed'));
    expect(await screen.findByText('Data quality stale: cached screen result')).toBeInTheDocument();
  });
});
