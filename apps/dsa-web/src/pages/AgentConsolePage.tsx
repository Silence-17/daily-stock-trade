import type React from 'react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useParams, useSearchParams } from 'react-router-dom';
import {
  Bot,
  Clock3,
  Download,
  ListChecks,
  RefreshCw,
  Search,
  ShieldAlert,
  Sparkles,
} from 'lucide-react';
import {
  vnpyPaperTradingApi,
  type VnpyPaperAgentDailySummary,
  type VnpyPaperAgentDataQualityTrends,
  type VnpyPaperAgentDecision,
  type VnpyPaperAgentRunDetail,
  type VnpyPaperAgentRunFilters,
  type VnpyPaperAgentRunSummary,
  type VnpyPaperAgentTimelineEvent,
  type VnpyPaperAgentTradePlan,
} from '../api/vnpyPaperTrading';
import { toApiErrorMessage } from '../api/error';
import { AppPage, Button, InlineAlert } from '../components/common';

const INPUT_CLASS =
  'h-10 w-full rounded-xl border border-border bg-surface px-3 text-sm text-foreground outline-none transition-colors focus:border-cyan disabled:cursor-not-allowed disabled:opacity-60';
const SELECT_CLASS = `${INPUT_CLASS} appearance-none`;
const AGENT_RUN_PAGE_SIZE = 25;

type AgentRunFilterForm = {
  strategy: string;
  market: string;
  status: string;
  createdFrom: string;
  createdTo: string;
};

const defaultFilters: AgentRunFilterForm = {
  strategy: '',
  market: '',
  status: '',
  createdFrom: '',
  createdTo: '',
};

const statusOptions = ['completed', 'failed', 'skipped', 'running'];
const marketOptions = [
  { value: 'cn', label: 'A 股' },
  { value: 'hk', label: '港股' },
  { value: 'us', label: '美股' },
  { value: 'tw', label: '台股' },
  { value: 'jp', label: '日股' },
  { value: 'kr', label: '韩股' },
];

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function asRecordList(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value)
    ? value.filter((item): item is Record<string, unknown> => Boolean(asRecord(item)))
    : [];
}

function buildFilters(filters: AgentRunFilterForm): VnpyPaperAgentRunFilters | undefined {
  const payload: VnpyPaperAgentRunFilters = {};
  const strategy = filters.strategy.trim();
  if (strategy) payload.strategy = strategy;
  if (filters.market) payload.market = filters.market;
  if (filters.status) payload.status = filters.status;
  if (filters.createdFrom) payload.createdFrom = filters.createdFrom;
  if (filters.createdTo) payload.createdTo = filters.createdTo;
  return Object.keys(payload).length > 0 ? payload : undefined;
}

function formatDateTime(value: unknown): string {
  const text = String(value || '');
  if (!text) return '-';
  const parsed = new Date(text);
  if (Number.isNaN(parsed.getTime())) return text;
  return parsed.toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  });
}

function formatNumber(value: unknown, digits = 2): string {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return '-';
  return numeric.toFixed(digits);
}

function formatPercent(value: unknown, digits = 1): string {
  const formatted = formatNumber(value, digits);
  return formatted === '-' ? '-' : `${formatted}%`;
}

function formatMoney(value: unknown, currency = 'CNY'): string {
  const amount = Number(value);
  if (!Number.isFinite(amount)) return '-';
  return new Intl.NumberFormat('zh-CN', {
    style: 'currency',
    currency,
    maximumFractionDigits: 2,
  }).format(amount);
}

function runDataQuality(run: VnpyPaperAgentRunSummary | VnpyPaperAgentRunDetail | null): string {
  const diagnostics = asRecord(run?.diagnostics);
  const quality = asRecord(diagnostics?.dataQuality) ?? asRecord(diagnostics?.data_quality);
  const status = quality?.status;
  return typeof status === 'string' ? status : '';
}

function runAgentSummary(run: VnpyPaperAgentRunDetail | null): Record<string, unknown> | null {
  const diagnostics = asRecord(run?.diagnostics);
  return asRecord(diagnostics?.agentSummary) ?? asRecord(diagnostics?.agent_summary);
}

function runReviewQuality(run: VnpyPaperAgentRunDetail | null): Record<string, unknown> | null {
  const summary = runAgentSummary(run);
  return asRecord(summary?.reviewQuality) ?? asRecord(summary?.review_quality);
}

function runAgentWorkflow(run: VnpyPaperAgentRunDetail | null): Record<string, unknown> | null {
  const diagnostics = asRecord(run?.diagnostics);
  return asRecord(diagnostics?.agentWorkflow) ?? asRecord(diagnostics?.agent_workflow);
}

function runAgentPlan(run: VnpyPaperAgentRunDetail | null): Record<string, unknown> | null {
  const diagnostics = asRecord(run?.diagnostics);
  return asRecord(diagnostics?.agentPlan) ?? asRecord(diagnostics?.agent_plan);
}

function runLlmRecap(run: VnpyPaperAgentRunDetail | null): Record<string, unknown> | null {
  const diagnostics = asRecord(run?.diagnostics);
  return asRecord(diagnostics?.llmRecap) ?? asRecord(diagnostics?.llm_recap);
}

function orderAgentReview(decision: VnpyPaperAgentDecision): Record<string, unknown> | null {
  const orderResult = asRecord(decision.orderResult);
  return asRecord(orderResult?.agentReview) ?? asRecord(orderResult?.agent_review);
}

function orderLlmReview(decision: VnpyPaperAgentDecision): Record<string, unknown> | null {
  const orderResult = asRecord(decision.orderResult);
  return asRecord(orderResult?.llmReview) ?? asRecord(orderResult?.llm_review);
}

function versionHint(record: Record<string, unknown> | null): string {
  const promptVersion = record?.promptVersion ?? record?.prompt_version;
  const evaluatorVersion = record?.evaluatorVersion ?? record?.evaluator_version;
  return [
    promptVersion ? `prompt=${String(promptVersion)}` : '',
    evaluatorVersion ? `eval=${String(evaluatorVersion)}` : '',
  ].filter(Boolean).join(' / ');
}

function statusTone(status: string): string {
  const normalizedStatus = status.replace(/([A-Z])/g, '_$1').toLowerCase();
  if (['completed', 'filled', 'executed', 'passed', 'audited'].includes(normalizedStatus)) {
    return 'border-success/30 bg-success/10 text-success';
  }
  if (['failed', 'unavailable', 'blocked', 'failed_execution', 'needs_review'].includes(normalizedStatus)) {
    return 'border-danger/30 bg-danger/10 text-danger';
  }
  if ([
    'skipped',
    'partial',
    'stale',
    'planned',
    'running',
    'submitted',
    'part_filled',
    'warning',
    'guarded',
  ].includes(normalizedStatus)) {
    return 'border-warning/30 bg-warning/10 text-warning';
  }
  return 'border-border bg-surface text-secondary-text';
}

function exportJsonFile(filename: string, payload: unknown): void {
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
}

function StatTile({
  label,
  value,
  hint,
}: {
  label: string;
  value: string | number;
  hint?: string;
}) {
  return (
    <div className="rounded-xl border border-border bg-card/95 px-4 py-3">
      <div className="text-xs text-secondary-text">{label}</div>
      <div className="mt-2 text-2xl font-semibold text-foreground">{value}</div>
      {hint ? <div className="mt-1 truncate text-xs text-secondary-text">{hint}</div> : null}
    </div>
  );
}

const AgentConsolePage: React.FC = () => {
  const navigate = useNavigate();
  const routeParams = useParams<{ runUid?: string }>();
  const [searchParams] = useSearchParams();
  const routeRunUid = routeParams.runUid?.trim() || '';
  const queryRunUid = searchParams.get('runUid')?.trim() || '';
  const initialRunUid = routeRunUid || queryRunUid;
  const initialLoadRef = useRef(false);
  const lastUrlRunUidRef = useRef('');
  const [filters, setFilters] = useState<AgentRunFilterForm>(defaultFilters);
  const [runs, setRuns] = useState<VnpyPaperAgentRunSummary[]>([]);
  const [dailySummary, setDailySummary] = useState<VnpyPaperAgentDailySummary | null>(null);
  const [dataQualityTrends, setDataQualityTrends] = useState<VnpyPaperAgentDataQualityTrends | null>(null);
  const [dataQualityDays, setDataQualityDays] = useState<7 | 30 | 90>(30);
  const dataQualityDaysRef = useRef<7 | 30 | 90>(30);
  const [selectedRun, setSelectedRun] = useState<VnpyPaperAgentRunDetail | null>(null);
  const [runOffset, setRunOffset] = useState(0);
  const [runTotal, setRunTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [recapLoading, setRecapLoading] = useState(false);
  const [error, setError] = useState('');
  const [success, setSuccess] = useState('');

  const loadRunDetail = useCallback(async (runUid: string) => {
    if (!runUid) return;
    setDetailLoading(true);
    setError('');
    try {
      const detail = await vnpyPaperTradingApi.getAgentRun(runUid);
      setSelectedRun(detail);
    } catch (err) {
      setError(toApiErrorMessage(err, 'Agent run 详情加载失败'));
    } finally {
      setDetailLoading(false);
    }
  }, []);

  const loadRuns = useCallback(async (
    nextFilters: AgentRunFilterForm,
    preferredRunUid?: string,
    nextOffset = 0,
  ) => {
    setLoading(true);
    setError('');
    try {
      const filterPayload = buildFilters(nextFilters);
      const [payload, summary, qualityTrends] = await Promise.all([
        vnpyPaperTradingApi.listAgentRuns(
          AGENT_RUN_PAGE_SIZE,
          nextOffset,
          filterPayload,
        ),
        vnpyPaperTradingApi.getAgentDailySummary(undefined, filterPayload).catch(() => null),
        vnpyPaperTradingApi.getAgentDataQualityTrends(
          dataQualityDaysRef.current,
          filterPayload,
        ).catch(() => null),
      ]);
      setRuns(payload.items);
      setDailySummary(summary);
      setDataQualityTrends(qualityTrends);
      setRunOffset(payload.offset || nextOffset);
      setRunTotal(payload.total ?? payload.items.length);
      const preferred = String(preferredRunUid || '').trim();
      const nextRunUid = preferred || payload.items[0]?.runUid || '';
      if (nextRunUid) {
        await loadRunDetail(nextRunUid);
      } else {
        setSelectedRun(null);
      }
    } catch (err) {
      setRuns([]);
      setDailySummary(null);
      setDataQualityTrends(null);
      setSelectedRun(null);
      setRunOffset(0);
      setRunTotal(0);
      setError(toApiErrorMessage(err, 'Agent run 列表加载失败'));
    } finally {
      setLoading(false);
    }
  }, [loadRunDetail]);

  useEffect(() => {
    if (!initialLoadRef.current) {
      initialLoadRef.current = true;
      lastUrlRunUidRef.current = initialRunUid;
      void loadRuns(defaultFilters, initialRunUid);
      return;
    }
    if (initialRunUid && initialRunUid !== lastUrlRunUidRef.current) {
      lastUrlRunUidRef.current = initialRunUid;
      void loadRunDetail(initialRunUid);
    }
  }, [initialRunUid, loadRunDetail, loadRuns]);

  const stats = useMemo(() => {
    return runs.reduce(
      (acc, item) => {
        acc.candidates += item.candidateCount || 0;
        acc.planned += item.plannedCount || 0;
        acc.submitted += item.submittedCount || 0;
        acc.skipped += item.skippedCount || 0;
        acc.statuses[item.status] = (acc.statuses[item.status] || 0) + 1;
        return acc;
      },
      {
        candidates: 0,
        planned: 0,
        submitted: 0,
        skipped: 0,
        statuses: {} as Record<string, number>,
      },
    );
  }, [runs]);
  const currentPage = Math.floor(runOffset / AGENT_RUN_PAGE_SIZE) + 1;
  const hasPreviousPage = runOffset > 0;
  const hasNextPage = runOffset + runs.length < runTotal;
  const dailyStatusHint = Object.entries(dailySummary?.statusCounts || {})
    .map(([key, value]) => `${key} ${value}`)
    .join(' · ') || '-';
  const dailyTopSkips = dailySummary?.topSkipReasons || [];
  const dailyAgentReviewHint = Object.entries(dailySummary?.agentReviewCounts || {})
    .map(([key, value]) => `${key} ${value}`)
    .join(' / ') || '-';
  const dailyLlmReviewHint = Object.entries(dailySummary?.llmReviewCounts || {})
    .map(([key, value]) => `${key} ${value}`)
    .join(' / ') || '-';
  const dailyReviewQualityHint = Object.entries(dailySummary?.reviewQualityCounts || {})
    .map(([key, value]) => `${key} ${value}`)
    .join(' / ') || '-';
  const dailyReviewQualityFlags = Object.entries(dailySummary?.reviewQualityFlagCounts || {});
  const dailyReviewQualityScore = dailySummary?.reviewQualityScoreAvg;
  const dailyWorkflowStages = Object.entries(dailySummary?.workflowStageCounts || {});
  const dailyTopSymbols = dailySummary?.topSymbols || [];

  const selectedSummary = runAgentSummary(selectedRun);
  const selectedReviewQuality = runReviewQuality(selectedRun);
  const selectedReviewQualityStatus = String(selectedReviewQuality?.status || '');
  const selectedReviewQualityScore = selectedReviewQuality?.score;
  const selectedReviewFlagsValue = selectedReviewQuality?.riskFlags ?? selectedReviewQuality?.risk_flags;
  const selectedReviewFlags = Array.isArray(selectedReviewFlagsValue)
    ? selectedReviewFlagsValue.map((item) => String(item || '').trim()).filter(Boolean)
    : [];
  const selectedWorkflow = runAgentWorkflow(selectedRun);
  const selectedPlan = runAgentPlan(selectedRun);
  const selectedLlmRecap = runLlmRecap(selectedRun);
  const selectedPlanProfile = asRecord(selectedPlan?.planProfile) ?? asRecord(selectedPlan?.plan_profile);
  const selectedAdaptiveControls = (
    asRecord(selectedPlan?.adaptiveControls)
    ?? asRecord(selectedPlan?.adaptive_controls)
  );
  const selectedDynamicPlan = (
    asRecord(selectedPlan?.llmDynamicPlan)
    ?? asRecord(selectedPlan?.llm_dynamic_plan)
  );
  const selectedDynamicOverrides = (
    asRecord(selectedDynamicPlan?.appliedOverrides)
    ?? asRecord(selectedDynamicPlan?.applied_overrides)
  );
  const selectedDynamicRecommendation = asRecord(selectedDynamicPlan?.recommendation);
  const selectedDynamicVersionHint = versionHint(selectedDynamicPlan);
  const selectedTopSkips = asRecordList(
    selectedSummary?.topSkipReasons ?? selectedSummary?.top_skip_reasons,
  );
  const selectedWorkflowStages = asRecordList(selectedWorkflow?.stages);

  const handleApplyFilters = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    void loadRuns(filters, undefined, 0);
  };

  const handleResetFilters = () => {
    setFilters(defaultFilters);
    void loadRuns(defaultFilters, undefined, 0);
  };

  const handlePageChange = (nextOffset: number) => {
    void loadRuns(filters, undefined, Math.max(0, nextOffset));
  };

  const handleDataQualityWindow = async (days: 7 | 30 | 90) => {
    setDataQualityDays(days);
    dataQualityDaysRef.current = days;
    try {
      setDataQualityTrends(await vnpyPaperTradingApi.getAgentDataQualityTrends(days, buildFilters(filters)));
    } catch (err) {
      setDataQualityTrends(null);
      setError(toApiErrorMessage(err, '数据质量趋势加载失败'));
    }
  };

  const handleSelectRun = (runUid: string) => {
    const nextRunUid = runUid.trim();
    if (!nextRunUid) return;
    navigate(`/agent-console/${encodeURIComponent(nextRunUid)}`);
  };

  const handleExportRuns = async () => {
    setExporting(true);
    setError('');
    setSuccess('');
    try {
      const payload = await vnpyPaperTradingApi.exportAgentRuns(50, true, buildFilters(filters));
      exportJsonFile('stock-selection-agent-console-runs.json', payload);
      setSuccess(`已导出 ${payload.count} 条 Agent 运行记录`);
    } catch (err) {
      setError(toApiErrorMessage(err, 'Agent run 导出失败'));
    } finally {
      setExporting(false);
    }
  };

  const handleExportSelected = () => {
    if (!selectedRun) return;
    exportJsonFile(`stock-selection-agent-${selectedRun.runUid}.json`, selectedRun);
    setSuccess(`已导出 ${selectedRun.runUid}`);
  };

  const handleGenerateRecap = async () => {
    if (!selectedRun) return;
    const runUid = selectedRun.runUid;
    setRecapLoading(true);
    setError('');
    setSuccess('');
    try {
      const payload = await vnpyPaperTradingApi.generateAgentRunRecap(runUid, 800);
      if (payload.runDetail) {
        setSelectedRun(payload.runDetail);
      } else {
        await loadRunDetail(runUid);
      }
      if (payload.accepted) {
        setSuccess(`已生成 ${runUid} LLM 复盘`);
      } else {
        setError(payload.reason || 'LLM 复盘生成失败');
      }
    } catch (err) {
      setError(toApiErrorMessage(err, 'LLM 复盘生成失败'));
    } finally {
      setRecapLoading(false);
    }
  };

  const renderStatusBadge = (status: string, label?: string) => (
    <span className={`inline-flex items-center rounded-full border px-2 py-1 text-xs ${statusTone(status)}`}>
      {label || status || '-'}
    </span>
  );

  return (
    <AppPage className="space-y-5">
      <header className="flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
        <div className="min-w-0">
          <div className="flex items-center gap-3">
            <div className="flex h-11 w-11 items-center justify-center rounded-xl border border-cyan/25 bg-cyan/10 text-cyan">
              <Bot className="h-5 w-5" />
            </div>
            <div className="min-w-0">
              <h1 className="truncate text-2xl font-semibold text-foreground">自动选股 Agent 控制台</h1>
              <p className="mt-1 text-sm text-secondary-text">
                集中查看自动选股 run、候选决策、交易计划、风控跳过和执行时间线。
              </p>
            </div>
          </div>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button
            variant="outline"
            onClick={() => void loadRuns(filters, selectedRun?.runUid, runOffset)}
            isLoading={loading}
            loadingText="刷新中..."
          >
            <RefreshCw className="h-4 w-4" />
            刷新
          </Button>
          <Button
            data-testid="agent-run-export-runs"
            variant="secondary"
            onClick={handleExportRuns}
            isLoading={exporting}
            loadingText="导出中..."
            disabled={runs.length === 0}
          >
            <Download className="h-4 w-4" />
            导出最近记录
          </Button>
        </div>
      </header>

      {error ? <InlineAlert variant="danger" title="Agent 控制台加载失败" message={error} /> : null}
      {success ? <InlineAlert variant="success" title="操作完成" message={success} /> : null}

      <section className="space-y-3" data-testid="agent-daily-summary">
        <div className="flex flex-col gap-2 md:flex-row md:items-center md:justify-between">
          <div>
            <h2 className="text-sm font-semibold text-foreground">今日 Agent 总结</h2>
            <p className="mt-1 text-xs text-secondary-text">
              {dailySummary?.date || '-'} · {dailySummary?.scannedCount ?? 0}/{dailySummary?.total ?? 0} runs scanned
            </p>
          </div>
          {renderStatusBadge(dailySummary?.health || 'idle', dailySummary?.health || 'idle')}
        </div>
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-7">
          <StatTile label="今日运行" value={dailySummary?.runCount ?? 0} hint={dailyStatusHint} />
          <StatTile
            label="候选 / 计划"
            value={`${dailySummary?.candidateCount ?? 0} / ${dailySummary?.plannedCount ?? 0}`}
          />
          <StatTile
            label="成交 / 跳过"
            value={`${dailySummary?.submittedCount ?? 0} / ${dailySummary?.skippedCount ?? 0}`}
          />
          <StatTile
            label="执行模式"
            value={Object.keys(dailySummary?.executionModeCounts || {}).length || 0}
            hint={Object.entries(dailySummary?.executionModeCounts || {})
              .map(([key, value]) => `${key} ${value}`)
              .join(' · ') || '-'}
          />
          <StatTile
            label="Agent 复核"
            value={Object.keys(dailySummary?.agentReviewCounts || {}).length || 0}
            hint={dailyAgentReviewHint}
          />
          <StatTile
            label="LLM 复核"
            value={Object.keys(dailySummary?.llmReviewCounts || {}).length || 0}
            hint={dailyLlmReviewHint}
          />
          <StatTile
            label="复核质量"
            value={dailyReviewQualityScore == null ? '-' : formatNumber(dailyReviewQualityScore, 1)}
            hint={dailyReviewQualityHint}
          />
        </div>
        <div className="grid gap-3 lg:grid-cols-4">
          <div className="rounded-xl border border-border bg-card/95 px-4 py-3">
            <div className="text-xs font-medium text-secondary-text">状态分布</div>
            <div className="mt-2 flex flex-wrap gap-2">
              {Object.entries(dailySummary?.statusCounts || {}).length > 0 ? (
                Object.entries(dailySummary?.statusCounts || {}).map(([key, value]) => (
                  <span key={key} className={`rounded-full border px-2 py-1 text-xs ${statusTone(key)}`}>
                    {key} {value}
                  </span>
                ))
              ) : (
                <span className="text-xs text-secondary-text">暂无运行</span>
              )}
            </div>
          </div>
          <div className="rounded-xl border border-border bg-card/95 px-4 py-3">
            <div className="text-xs font-medium text-secondary-text">工作流阶段</div>
            <div className="mt-2 flex flex-wrap gap-2">
              {dailyWorkflowStages.length > 0 ? dailyWorkflowStages.map(([key, value]) => (
                <span key={key} className={`rounded-full border px-2 py-1 text-xs ${statusTone(key)}`}>
                  {key} {value}
                </span>
              )) : <span className="text-xs text-secondary-text">暂无阶段</span>}
            </div>
          </div>
          <div className="rounded-xl border border-border bg-card/95 px-4 py-3">
            <div className="text-xs font-medium text-secondary-text">复核风险</div>
            <div className="mt-2 flex flex-wrap gap-2">
              {dailyReviewQualityFlags.length > 0 ? dailyReviewQualityFlags.map(([key, value]) => (
                <span key={key} className={`rounded-full border px-2 py-1 text-xs ${statusTone(key)}`}>
                  {key} {value}
                </span>
              )) : <span className="text-xs text-secondary-text">暂无风险</span>}
            </div>
          </div>
          <div className="rounded-xl border border-border bg-card/95 px-4 py-3">
            <div className="text-xs font-medium text-secondary-text">主要跳过原因</div>
            <div className="mt-2 flex flex-wrap gap-2">
              {dailyTopSkips.length > 0 ? dailyTopSkips.map((item) => (
                <span
                  key={String(item.reason || '')}
                  className="rounded-full border border-warning/30 bg-warning/10 px-2 py-1 text-xs text-warning"
                >
                  {item.reason || '-'} {item.count}
                </span>
              )) : <span className="text-xs text-secondary-text">暂无跳过</span>}
            </div>
          </div>
          <div className="rounded-xl border border-border bg-card/95 px-4 py-3">
            <div className="text-xs font-medium text-secondary-text">热门标的</div>
            <div className="mt-2 flex flex-wrap gap-2">
              {dailyTopSymbols.length > 0 ? dailyTopSymbols.map((item) => (
                <span
                  key={String(item.symbol || '')}
                  className="rounded-full border border-cyan/30 bg-cyan/10 px-2 py-1 font-mono text-xs text-cyan"
                >
                  {item.symbol || '-'} {item.count}
                </span>
              )) : <span className="text-xs text-secondary-text">暂无标的</span>}
            </div>
          </div>
        </div>
      </section>

      <section className="space-y-3" data-testid="agent-data-quality-trends">
        <div className="flex flex-col gap-2 md:flex-row md:items-center md:justify-between">
          <div>
            <h2 className="text-sm font-semibold text-foreground">跨 run 数据质量趋势</h2>
            <p className="mt-1 text-xs text-secondary-text">
              缺少质量快照的旧 run 计为 unknown，不会被当作健康数据。
            </p>
          </div>
          <div className="inline-flex w-fit border border-border bg-surface" aria-label="数据质量趋势时间窗口">
            {([7, 30, 90] as const).map((days) => (
              <button
                key={days}
                type="button"
                className={`h-8 min-w-12 border-r border-border px-3 text-xs font-semibold last:border-r-0 ${
                  dataQualityDays === days ? 'bg-cyan text-background' : 'text-secondary-text hover:text-foreground'
                }`}
                aria-pressed={dataQualityDays === days}
                onClick={() => void handleDataQualityWindow(days)}
              >
                {days}天
              </button>
            ))}
          </div>
        </div>
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <StatTile
            label="已扫描 run"
            value={`${dataQualityTrends?.scannedCount ?? 0} / ${dataQualityTrends?.total ?? 0}`}
            hint={dataQualityTrends?.truncated ? '结果已截断' : '窗口内完整结果'}
          />
          <StatTile
            label="已知质量"
            value={dataQualityTrends?.knownCount ?? 0}
            hint={`latest ${dataQualityTrends?.latestQuality || '-'}`}
          />
          <StatTile
            label="降级率"
            value={`${formatNumber(dataQualityTrends?.degradedRatePct ?? 0, 2)}%`}
            hint={`${dataQualityTrends?.degradedCount ?? 0} degraded runs`}
          />
          <StatTile
            label="趋势健康"
            value={dataQualityTrends?.health || 'idle'}
            hint={`${dataQualityTrends?.daily.length ?? 0} active days`}
          />
        </div>
        <div className="overflow-x-auto border-y border-border">
          <table className="min-w-full text-left text-xs">
            <thead className="text-secondary-text">
              <tr>
                <th className="py-2 pr-3 font-medium">日期</th>
                <th className="py-2 pr-3 font-medium">run</th>
                <th className="py-2 pr-3 font-medium">ok / partial / stale / unavailable / unknown</th>
                <th className="py-2 pr-3 font-medium">降级率</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {(dataQualityTrends?.daily || []).slice(-14).reverse().map((item) => (
                <tr key={item.date}>
                  <td className="py-2 pr-3 text-foreground">{item.date}</td>
                  <td className="py-2 pr-3 text-secondary-text">{item.runCount}</td>
                  <td className="py-2 pr-3 text-secondary-text">
                    {['ok', 'partial', 'stale', 'unavailable', 'unknown']
                      .map((key) => item.qualityCounts[key] || 0)
                      .join(' / ')}
                  </td>
                  <td className="py-2 pr-3 text-secondary-text">{formatNumber(item.degradedRatePct, 2)}%</td>
                </tr>
              ))}
              {(dataQualityTrends?.daily.length ?? 0) === 0 ? (
                <tr><td className="py-4 text-secondary-text" colSpan={4}>当前窗口暂无 Agent run</td></tr>
              ) : null}
            </tbody>
          </table>
        </div>
      </section>

      <section className="grid gap-3 md:grid-cols-4">
        <StatTile label="运行记录" value={runs.length} hint="当前筛选窗口" />
        <StatTile label="候选 / 计划" value={`${stats.candidates} / ${stats.planned}`} />
        <StatTile label="成交 / 跳过" value={`${stats.submitted} / ${stats.skipped}`} />
        <StatTile
          label="状态分布"
          value={Object.keys(stats.statuses).length || 0}
          hint={Object.entries(stats.statuses).map(([key, value]) => `${key} ${value}`).join(' · ') || '-'}
        />
      </section>

      <form
        data-testid="agent-run-filters"
        className="grid gap-3 rounded-xl border border-border bg-card/95 p-4 lg:grid-cols-[1fr_160px_160px_190px_190px_auto]"
        onSubmit={handleApplyFilters}
      >
        <label className="text-xs font-medium text-secondary-text">
          策略
          <input
            data-testid="agent-run-strategy-filter"
            className={`${INPUT_CLASS} mt-1`}
            value={filters.strategy}
            onChange={(event) => setFilters((prev) => ({ ...prev, strategy: event.target.value }))}
            placeholder="dual_low"
            aria-label="Agent 控制台策略筛选"
          />
        </label>
        <label className="text-xs font-medium text-secondary-text">
          市场
          <select
            data-testid="agent-run-market-filter"
            className={`${SELECT_CLASS} mt-1`}
            value={filters.market}
            onChange={(event) => setFilters((prev) => ({ ...prev, market: event.target.value }))}
            aria-label="Agent 控制台市场筛选"
          >
            <option value="">全部市场</option>
            {marketOptions.map((item) => (
              <option key={item.value} value={item.value}>{item.label}</option>
            ))}
          </select>
        </label>
        <label className="text-xs font-medium text-secondary-text">
          状态
          <select
            data-testid="agent-run-status-filter"
            className={`${SELECT_CLASS} mt-1`}
            value={filters.status}
            onChange={(event) => setFilters((prev) => ({ ...prev, status: event.target.value }))}
            aria-label="Agent 控制台状态筛选"
          >
            <option value="">全部状态</option>
            {statusOptions.map((item) => (
              <option key={item} value={item}>{item}</option>
            ))}
          </select>
        </label>
        <label className="text-xs font-medium text-secondary-text">
          开始时间
          <input
            data-testid="agent-run-created-from-filter"
            className={`${INPUT_CLASS} mt-1`}
            type="datetime-local"
            value={filters.createdFrom}
            onChange={(event) => setFilters((prev) => ({ ...prev, createdFrom: event.target.value }))}
            aria-label="Agent 控制台开始时间筛选"
          />
        </label>
        <label className="text-xs font-medium text-secondary-text">
          结束时间
          <input
            data-testid="agent-run-created-to-filter"
            className={`${INPUT_CLASS} mt-1`}
            type="datetime-local"
            value={filters.createdTo}
            onChange={(event) => setFilters((prev) => ({ ...prev, createdTo: event.target.value }))}
            aria-label="Agent 控制台结束时间筛选"
          />
        </label>
        <div className="flex items-end gap-2">
          <Button type="submit" variant="primary" className="h-10" isLoading={loading} loadingText="筛选中...">
            <Search className="h-4 w-4" />
            应用筛选
          </Button>
          <Button type="button" variant="ghost" className="h-10" onClick={handleResetFilters}>
            重置
          </Button>
        </div>
      </form>

      <section className="grid gap-4 xl:grid-cols-[minmax(360px,0.9fr)_minmax(0,1.35fr)]">
        <div className="min-w-0 rounded-xl border border-border bg-card/95">
          <div className="flex items-center justify-between border-b border-border px-4 py-3">
            <div>
              <h2 className="text-sm font-semibold text-foreground">运行历史</h2>
              <p className="mt-1 text-xs text-secondary-text">
                第 {currentPage} 页 · 共 {runTotal} 条 · 每页 {AGENT_RUN_PAGE_SIZE} 条
              </p>
            </div>
            <div className="flex items-center gap-2">
              <Button
                data-testid="agent-run-prev-page"
                size="xsm"
                variant="ghost"
                disabled={!hasPreviousPage || loading}
                onClick={() => handlePageChange(runOffset - AGENT_RUN_PAGE_SIZE)}
              >
                上一页
              </Button>
              <Button
                data-testid="agent-run-next-page"
                size="xsm"
                variant="ghost"
                disabled={!hasNextPage || loading}
                onClick={() => handlePageChange(runOffset + AGENT_RUN_PAGE_SIZE)}
              >
                下一页
              </Button>
              <ListChecks className="h-4 w-4 text-secondary-text" />
            </div>
          </div>
          {loading ? (
            <div className="px-4 py-10 text-center text-sm text-secondary-text">正在加载 Agent 运行记录...</div>
          ) : runs.length === 0 ? (
            <div className="px-4 py-10 text-center text-sm text-secondary-text">暂无符合条件的 Agent 运行记录</div>
          ) : (
            <div className="max-h-[680px] overflow-auto">
              {runs.map((item) => {
                const active = selectedRun?.runUid === item.runUid;
                const quality = runDataQuality(item);
                return (
                  <button
                    key={item.runUid}
                    type="button"
                    data-testid={`agent-run-row-${item.runUid}`}
                    className={`block w-full border-b border-border px-4 py-3 text-left transition-colors last:border-b-0 ${
                      active ? 'bg-cyan/10' : 'hover:bg-hover'
                    }`}
                    onClick={() => handleSelectRun(item.runUid)}
                    aria-label={`查看 Agent run ${item.runUid}`}
                  >
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <div className="truncate font-mono text-sm font-semibold text-foreground">{item.runUid}</div>
                        <div className="mt-1 text-xs text-secondary-text">
                          {item.strategy} · {item.market} · {formatDateTime(item.startedAt || item.createdAt)}
                        </div>
                      </div>
                      {renderStatusBadge(item.status)}
                    </div>
                    <div className="mt-2 grid grid-cols-4 gap-2 text-xs text-secondary-text">
                      <span>候选 {item.candidateCount}</span>
                      <span>计划 {item.plannedCount}</span>
                      <span>成交 {item.submittedCount}</span>
                      <span>跳过 {item.skippedCount}</span>
                    </div>
                    <div className="mt-2 flex flex-wrap gap-2">
                      {quality ? renderStatusBadge(quality, `数据 ${quality}`) : null}
                      {item.error ? renderStatusBadge('failed', item.error) : null}
                    </div>
                  </button>
                );
              })}
            </div>
          )}
        </div>

        <div className="min-w-0 rounded-xl border border-border bg-card/95">
          <div className="flex items-center justify-between gap-3 border-b border-border px-4 py-3">
            <div className="min-w-0">
              <h2 className="text-sm font-semibold text-foreground">运行详情</h2>
              <p className="mt-1 truncate text-xs text-secondary-text">
                {selectedRun ? selectedRun.runUid : '选择一条运行记录查看详情'}
              </p>
            </div>
            <div className="flex shrink-0 items-center gap-2">
              <Button
                size="xsm"
                variant="outline"
                onClick={() => void handleGenerateRecap()}
                disabled={!selectedRun || recapLoading}
                isLoading={recapLoading}
                loadingText="复盘中..."
              >
                <Sparkles className="h-3.5 w-3.5" />
                生成复盘
              </Button>
              <Button
                size="xsm"
                variant="outline"
                onClick={handleExportSelected}
                disabled={!selectedRun}
              >
                <Download className="h-3.5 w-3.5" />
                导出 JSON
              </Button>
            </div>
          </div>

          {detailLoading ? (
            <div className="px-4 py-10 text-center text-sm text-secondary-text">正在加载运行详情...</div>
          ) : selectedRun ? (
            <div className="space-y-4 p-4">
              <div className="grid gap-3 md:grid-cols-5">
                <StatTile label="策略 / 市场" value={selectedRun.strategy} hint={selectedRun.market} />
                <StatTile label="候选 / 计划" value={`${selectedRun.candidateCount} / ${selectedRun.plannedCount}`} />
                <StatTile label="成交 / 跳过" value={`${selectedRun.submittedCount} / ${selectedRun.skippedCount}`} />
                <StatTile label="数据质量" value={runDataQuality(selectedRun) || '-'} />
                <StatTile
                  label="复核质量"
                  value={selectedReviewQualityStatus || '-'}
                  hint={selectedReviewQualityScore == null ? undefined : `score ${formatNumber(selectedReviewQualityScore, 1)}`}
                />
              </div>

              {selectedPlan || selectedSummary || selectedWorkflow || selectedLlmRecap ? (
                <div className="grid gap-3 lg:grid-cols-2">
                  {selectedPlan ? (
                    <section className="rounded-lg border border-border bg-surface px-3 py-3">
                      <div className="mb-2 flex items-center gap-2 text-sm font-semibold text-foreground">
                        <Clock3 className="h-4 w-4 text-cyan" />
                        Agent 计划
                      </div>
                      <dl className="grid gap-2 text-xs text-secondary-text sm:grid-cols-2">
                        <div>
                          <dt>执行模式</dt>
                          <dd className="mt-1 font-semibold text-foreground">
                            {String(selectedPlan.executionMode || selectedPlan.execution_mode || '-')}
                          </dd>
                        </div>
                        <div>
                          <dt>每票预算</dt>
                          <dd className="mt-1 font-semibold text-foreground">
                            {formatMoney(selectedPlan.cashPerOrder ?? selectedPlan.cash_per_order ?? selectedRun.cashPerOrder)}
                          </dd>
                        </div>
                        <div>
                          <dt>最大候选</dt>
                          <dd className="mt-1 font-semibold text-foreground">
                            {String(selectedPlan.maxResults ?? selectedPlan.max_results ?? selectedRun.maxResults ?? '-')}
                          </dd>
                        </div>
                        <div>
                          <dt>最低分</dt>
                          <dd className="mt-1 font-semibold text-foreground">
                            {String(selectedRun.minScore ?? '-')}
                          </dd>
                        </div>
                        <div>
                          <dt>计划档位</dt>
                          <dd className="mt-1 font-semibold text-foreground">
                            {String(selectedPlanProfile?.mode || selectedPlanProfile?.profile || '-')}
                          </dd>
                        </div>
                        <div>
                          <dt>风控档位</dt>
                          <dd className="mt-1 font-semibold text-foreground">
                            {String(
                              selectedAdaptiveControls?.riskLevel
                              || selectedAdaptiveControls?.risk_level
                              || selectedPlanProfile?.riskLevel
                              || selectedPlanProfile?.risk_level
                              || '-',
                            )}
                          </dd>
                        </div>
                        {selectedDynamicPlan ? (
                          <div className="sm:col-span-2">
                            <dt>LLM 动态计划</dt>
                            <dd className="mt-1 space-y-1">
                              <span className={`inline-flex rounded-full border px-2 py-1 ${statusTone(String(selectedDynamicPlan.status || ''))}`}>
                                {String(selectedDynamicPlan.status || '-')}
                              </span>
                              <div className="text-foreground">
                                {Object.keys(selectedDynamicOverrides || {}).length > 0
                                  ? Object.entries(selectedDynamicOverrides || {})
                                    .map(([key, value]) => `${key}=${String(value)}`)
                                    .join(' / ')
                                  : '使用保存配置'}
                              </div>
                              {selectedDynamicRecommendation?.rationale ? (
                                <div className="line-clamp-2">
                                  {String(selectedDynamicRecommendation.rationale)}
                                </div>
                              ) : null}
                              {selectedDynamicVersionHint ? (
                                <div className="text-xs text-secondary-text">{selectedDynamicVersionHint}</div>
                              ) : null}
                            </dd>
                          </div>
                        ) : null}
                      </dl>
                    </section>
                  ) : null}

                  {selectedSummary ? (
                    <section className="rounded-lg border border-border bg-surface px-3 py-3">
                      <div className="mb-2 flex items-center gap-2 text-sm font-semibold text-foreground">
                        <ShieldAlert className="h-4 w-4 text-warning" />
                        运行总结
                      </div>
                      <div className="text-sm font-semibold text-foreground">
                        {String(selectedSummary.headline || selectedSummary.outcome || '-')}
                      </div>
                      {selectedReviewQuality ? (
                        <div className="mt-3 border-t border-border pt-3">
                          <div className="flex flex-wrap items-center gap-2">
                            <span className="text-xs font-medium text-secondary-text">复核质量</span>
                            {selectedReviewQualityStatus
                              ? renderStatusBadge(selectedReviewQualityStatus)
                              : null}
                            {selectedReviewQualityScore == null ? null : (
                              <span className="text-xs text-secondary-text">
                                score {formatNumber(selectedReviewQualityScore, 1)}
                              </span>
                            )}
                            {selectedReviewQuality.humanReviewRecommended
                              || selectedReviewQuality.human_review_recommended ? (
                                <span className="rounded-full border border-warning/30 bg-warning/10 px-2 py-1 text-xs text-warning">
                                  建议人工确认
                                </span>
                              ) : null}
                          </div>
                          <dl className="mt-2 grid gap-2 text-xs text-secondary-text sm:grid-cols-3">
                            <div>
                              <dt>Agent 覆盖</dt>
                              <dd className="mt-1 font-semibold text-foreground">
                                {formatPercent(
                                  selectedReviewQuality.agentReviewCoveragePct
                                  ?? selectedReviewQuality.agent_review_coverage_pct,
                                  1,
                                )}
                              </dd>
                            </div>
                            <div>
                              <dt>LLM 覆盖</dt>
                              <dd className="mt-1 font-semibold text-foreground">
                                {formatPercent(
                                  selectedReviewQuality.llmReviewCoveragePct
                                  ?? selectedReviewQuality.llm_review_coverage_pct,
                                  1,
                                )}
                              </dd>
                            </div>
                            <div>
                              <dt>决策数</dt>
                              <dd className="mt-1 font-semibold text-foreground">
                                {String(selectedReviewQuality.decisionCount ?? selectedReviewQuality.decision_count ?? '-')}
                              </dd>
                            </div>
                          </dl>
                          {selectedReviewFlags.length > 0 ? (
                            <div className="mt-2 flex flex-wrap gap-2">
                              {selectedReviewFlags.map((flag) => (
                                <span key={flag} className={`rounded-full border px-2 py-1 text-xs ${statusTone(flag)}`}>
                                  {flag}
                                </span>
                              ))}
                            </div>
                          ) : null}
                        </div>
                      ) : null}
                      {selectedTopSkips.length > 0 ? (
                        <div className="mt-2 flex flex-wrap gap-2">
                          {selectedTopSkips.map((item) => (
                            <span
                              key={String(item.reason || item.key || '')}
                              className="rounded-full border border-warning/30 bg-warning/10 px-2 py-1 text-xs text-warning"
                            >
                              {String(item.reason || item.key || '-')} ×{String(item.count || 0)}
                            </span>
                          ))}
                        </div>
                      ) : null}
                    </section>
                  ) : null}

                  {selectedWorkflow ? (
                    <section data-testid="agent-workflow" className="rounded-lg border border-border bg-surface px-3 py-3">
                      <div className="mb-2 flex items-center gap-2 text-sm font-semibold text-foreground">
                        <ListChecks className="h-4 w-4 text-cyan" />
                        Agent 工作流
                        {selectedWorkflow.status ? renderStatusBadge(String(selectedWorkflow.status)) : null}
                      </div>
                      <dl className="grid gap-2 text-xs text-secondary-text sm:grid-cols-2">
                        <div>
                          <dt>当前阶段</dt>
                          <dd className="mt-1 font-semibold text-foreground">
                            {String(selectedWorkflow.currentStage || selectedWorkflow.current_stage || '-')}
                          </dd>
                        </div>
                        <div>
                          <dt>下一步</dt>
                          <dd className="mt-1 font-semibold text-foreground">
                            {String(selectedWorkflow.nextAction || selectedWorkflow.next_action || '-')}
                          </dd>
                        </div>
                      </dl>
                      {selectedWorkflowStages.length > 0 ? (
                        <div className="mt-3 flex flex-wrap gap-2">
                          {selectedWorkflowStages.map((stage) => {
                            const key = String(stage.key || stage.label || '');
                            const status = String(stage.status || '');
                            return (
                              <span key={key} className={`rounded-full border px-2 py-1 text-xs ${statusTone(status)}`}>
                                {String(stage.label || key || '-')} {status || '-'}
                              </span>
                            );
                          })}
                        </div>
                      ) : null}
                    </section>
                  ) : null}

                  {selectedLlmRecap ? (
                    <section
                      data-testid="agent-llm-recap"
                      className="rounded-lg border border-border bg-surface px-3 py-3 lg:col-span-2"
                    >
                      <div className="mb-2 flex items-center gap-2 text-sm font-semibold text-foreground">
                        <Sparkles className="h-4 w-4 text-cyan" />
                        LLM 复盘
                        {selectedLlmRecap.status ? renderStatusBadge(String(selectedLlmRecap.status)) : null}
                      </div>
                      <div className="whitespace-pre-wrap break-words text-sm leading-6 text-secondary-text">
                        {String(
                          selectedLlmRecap.content
                          || selectedLlmRecap.error
                          || selectedLlmRecap.reason
                          || selectedLlmRecap.status
                          || '-',
                        )}
                      </div>
                    </section>
                  ) : null}
                </div>
              ) : null}

              <Timeline events={selectedRun.timeline || []} />
              <DecisionTable decisions={selectedRun.decisions || []} />
              <TradePlanList plans={selectedRun.tradePlans || []} />
            </div>
          ) : (
            <div className="px-4 py-10 text-center text-sm text-secondary-text">暂无运行详情</div>
          )}
        </div>
      </section>
    </AppPage>
  );
};

function Timeline({ events }: { events: VnpyPaperAgentTimelineEvent[] }) {
  if (events.length === 0) {
    return (
      <section className="rounded-lg border border-dashed border-border bg-surface/70 px-4 py-6 text-center text-sm text-secondary-text">
        暂无运行时间线
      </section>
    );
  }
  return (
    <section className="rounded-lg border border-border bg-surface px-3 py-3">
      <div className="mb-3 text-sm font-semibold text-foreground">运行时间线</div>
      <div className="space-y-2">
        {events.map((event, index) => (
          <div
            key={`${event.stage}-${index}`}
            className="grid gap-2 rounded-md border border-border bg-card/80 px-3 py-2 text-xs md:grid-cols-[140px_1fr]"
          >
            <div className="text-secondary-text">
              <div className="font-mono">{formatDateTime(event.timestamp)}</div>
              <div className="mt-1">{renderTimelineBadge(event.stage, event.status)}</div>
            </div>
            <div className="text-foreground">{event.message}</div>
          </div>
        ))}
      </div>
    </section>
  );
}

function renderTimelineBadge(stage: string, status: string) {
  return (
    <span className={`inline-flex rounded-full border px-2 py-1 ${statusTone(status)}`}>
      {stage} · {status}
    </span>
  );
}

function DecisionTable({ decisions }: { decisions: VnpyPaperAgentDecision[] }) {
  return (
    <section className="rounded-lg border border-border bg-surface">
      <div className="flex items-center justify-between border-b border-border px-3 py-2">
        <h3 className="text-sm font-semibold text-foreground">候选决策</h3>
        <span className="text-xs text-secondary-text">{decisions.length} 条</span>
      </div>
      {decisions.length === 0 ? (
        <div className="px-4 py-6 text-center text-sm text-secondary-text">本轮没有候选决策记录</div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[760px] border-collapse text-xs">
            <thead className="bg-card/70 text-left text-secondary-text">
              <tr>
                <th className="px-3 py-2 font-semibold">候选</th>
                <th className="px-3 py-2 font-semibold">分数</th>
                <th className="px-3 py-2 font-semibold">动作</th>
                <th className="px-3 py-2 font-semibold">状态</th>
                <th className="px-3 py-2 font-semibold">计划金额</th>
                <th className="px-3 py-2 font-semibold">解释 / 风险</th>
              </tr>
            </thead>
            <tbody>
              {decisions.map((item) => {
                const agentReview = orderAgentReview(item);
                const reviewStatus = String(agentReview?.status || '');
                const reviewSummary = String(agentReview?.summary || agentReview?.reason || '');
                const llmReview = orderLlmReview(item);
                const llmReviewStatus = String(llmReview?.status || '');
                const llmReviewSummary = String(llmReview?.summary || llmReview?.reason || '');
                const llmReviewVersionHint = versionHint(llmReview);
                return (
                  <tr key={item.id} className="border-t border-border align-top">
                    <td className="px-3 py-2">
                      <div className="font-mono font-semibold text-foreground">{item.symbol || '-'}</div>
                      <div className="mt-1 text-secondary-text">{item.name || item.market}</div>
                    </td>
                    <td className="px-3 py-2 text-secondary-text">{formatNumber(item.score)}</td>
                    <td className="px-3 py-2 text-secondary-text">{item.action}</td>
                    <td className="px-3 py-2">
                      <span className={`inline-flex rounded-full border px-2 py-1 ${statusTone(item.status)}`}>
                        {item.status}
                      </span>
                      {item.reason ? <div className="mt-1 text-warning">{item.reason}</div> : null}
                    </td>
                    <td className="px-3 py-2 text-secondary-text">
                      <div>{formatMoney(item.cashAmount)}</div>
                      <div>{formatNumber(item.quantity, 0)} 股 · {formatNumber(item.price)}</div>
                    </td>
                    <td className="max-w-[320px] px-3 py-2 text-secondary-text">
                      <div className="line-clamp-3">{item.rationale || '-'}</div>
                      {item.riskFlags.length > 0 ? (
                        <div className="mt-1 text-warning">{item.riskFlags.join(' / ')}</div>
                      ) : null}
                      {agentReview ? (
                        <div className="mt-2 space-y-1" data-testid={`agent-review-${item.id}`}>
                          <span className={`inline-flex rounded-full border px-2 py-1 ${statusTone(reviewStatus)}`}>
                            Agent 复核 {reviewStatus || '-'}
                          </span>
                          {reviewSummary ? <div className="line-clamp-2">{reviewSummary}</div> : null}
                        </div>
                      ) : null}
                      {llmReview ? (
                        <div className="mt-2 space-y-1" data-testid={`llm-review-${item.id}`}>
                          <span className={`inline-flex rounded-full border px-2 py-1 ${statusTone(llmReviewStatus)}`}>
                            LLM 复核 {llmReviewStatus || '-'}
                          </span>
                          {llmReviewSummary ? <div className="line-clamp-2">{llmReviewSummary}</div> : null}
                          {llmReviewVersionHint ? (
                            <div className="text-xs text-secondary-text">{llmReviewVersionHint}</div>
                          ) : null}
                        </div>
                      ) : null}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function TradePlanList({ plans }: { plans: VnpyPaperAgentTradePlan[] }) {
  return (
    <section className="rounded-lg border border-border bg-surface">
      <div className="flex items-center justify-between border-b border-border px-3 py-2">
        <h3 className="text-sm font-semibold text-foreground">交易计划</h3>
        <span className="text-xs text-secondary-text">{plans.length} 条</span>
      </div>
      {plans.length === 0 ? (
        <div className="px-4 py-6 text-center text-sm text-secondary-text">本轮没有交易计划</div>
      ) : (
        <div className="grid gap-2 p-3 md:grid-cols-2">
          {plans.map((plan) => (
            <div key={plan.planUid} className="rounded-md border border-border bg-card/80 px-3 py-2 text-xs">
              <div className="flex items-center justify-between gap-2">
                <span className="font-mono font-semibold text-foreground">{plan.symbol || '-'}</span>
                <span className={`inline-flex rounded-full border px-2 py-1 ${statusTone(plan.status)}`}>
                  {plan.status}
                </span>
              </div>
              <div className="mt-2 text-secondary-text">
                {plan.executionMode} · {plan.side} · {formatMoney(plan.plannedCashAmount)}
              </div>
              <div className="mt-1 text-secondary-text">
                {formatNumber(plan.plannedQuantity, 0)} 股 · {formatNumber(plan.plannedPrice)}
              </div>
              {plan.skipReason ? <div className="mt-1 text-warning">{plan.skipReason}</div> : null}
              {plan.tradeId ? <div className="mt-1 text-secondary-text">trade #{plan.tradeId}</div> : null}
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

export default AgentConsolePage;
