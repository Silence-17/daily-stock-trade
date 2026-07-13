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
const generateAgentRunRecap = vi.hoisted(() => vi.fn());

vi.mock('../../api/vnpyPaperTrading', () => ({
  vnpyPaperTradingApi: {
    listAgentRuns,
    exportAgentRuns,
    getAgentRun,
    getAgentDailySummary,
    getAgentDataQualityTrends,
    generateAgentRunRecap,
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
    dataQuality: { status: 'partial' },
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
      llmDynamicPlan: {
        status: 'accepted',
        promptVersion: 'vnpy_paper_dynamic_agent_plan_v1',
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
    orderResult: {
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
  timeline: [{
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

describe('AgentConsolePage', () => {
  beforeEach(() => {
    listAgentRuns.mockReset();
    exportAgentRuns.mockReset();
    getAgentRun.mockReset();
    getAgentDailySummary.mockReset();
    getAgentDataQualityTrends.mockReset();
    generateAgentRunRecap.mockReset();
    listAgentRuns.mockResolvedValue({
      items: [runSummary, failedRunSummary],
      limit: 25,
      offset: 0,
      total: 2,
    });
    getAgentDailySummary.mockResolvedValue(dailySummary);
    getAgentDataQualityTrends.mockResolvedValue(dataQualityTrends);
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
  });

  it('renders Agent run history and selected run details', async () => {
    renderPage();

    await waitFor(() => expect(listAgentRuns).toHaveBeenCalledWith(25, 0, undefined));
    await waitFor(() => expect(getAgentDailySummary).toHaveBeenCalledWith(undefined, undefined));
    await waitFor(() => expect(getAgentDataQualityTrends).toHaveBeenCalledWith(30, undefined));
    await waitFor(() => expect(getAgentRun).toHaveBeenCalledWith('ss-agent-test'));
    expect(screen.getByText('今日 Agent 总结')).toBeInTheDocument();
    expect(screen.getByText('2026-07-01 · 2/2 runs scanned')).toBeInTheDocument();
    expect(screen.getByTestId('agent-data-quality-trends')).toHaveTextContent('跨 run 数据质量趋势');
    expect(screen.getByTestId('agent-data-quality-trends')).toHaveTextContent('33.33%');
    expect(screen.getByTestId('agent-data-quality-trends')).toHaveTextContent('1 / 1 / 0 / 0 / 1');
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
    expect(screen.getByTestId('agent-workflow')).toHaveTextContent('execution');
    expect(screen.getByTestId('agent-workflow')).toHaveTextContent('monitor_positions_and_events');
    expect(screen.getByText('Candidate review warning')).toBeInTheDocument();
    expect(screen.getByText('low valuation and improving momentum')).toBeInTheDocument();
    expect(screen.getByText('local_paper_execution')).toBeInTheDocument();
    expect(screen.getAllByText('guarded').length).toBeGreaterThan(0);
    expect(screen.getByText('LLM 动态计划')).toBeInTheDocument();
    expect(screen.getAllByText('score 85.0').length).toBeGreaterThan(0);
    expect(screen.getByText('建议人工确认')).toBeInTheDocument();
    expect(screen.getByText('100.0%')).toBeInTheDocument();
    expect(screen.getByText('50.0%')).toBeInTheDocument();
    expect(screen.getByText('agent_review_blocked')).toBeInTheDocument();
    expect(screen.getByText(/autoStrategy=capital_heat/)).toBeInTheDocument();
    expect(screen.getByText(/autoMaxResults=1/)).toBeInTheDocument();
    expect(screen.getByText('Prefer a narrower heat strategy today.')).toBeInTheDocument();
    expect(screen.getByText('prompt=vnpy_paper_dynamic_agent_plan_v1 / eval=dynamic_plan_guardrails_v1')).toBeInTheDocument();
    expect(screen.getByTestId('agent-llm-recap')).toHaveTextContent('LLM recap generated');
    expect(screen.getByTestId('agent-review-1')).toHaveTextContent('Agent 复核 passed');
    expect(screen.getByText('Pre-trade Agent review passed')).toBeInTheDocument();
    expect(screen.getByTestId('llm-review-1')).toHaveTextContent('LLM 复核 passed');
    expect(screen.getByText('LLM review passed')).toBeInTheDocument();
    expect(screen.getByText('prompt=vnpy_paper_pre_trade_review_v1 / eval=pre_trade_fail_closed_v1')).toBeInTheDocument();
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
