import type React from 'react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Activity,
  BarChart3,
  Ban,
  CircleDollarSign,
  ClipboardList,
  Download,
  Layers3,
  LineChart,
  PauseCircle,
  Play,
  RefreshCw,
  RotateCcw,
  Save,
  SendHorizontal,
  ShieldCheck,
  WalletCards,
} from 'lucide-react';
import {
  CartesianGrid,
  Line,
  LineChart as RechartsLineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import {
  vnpyPaperTradingApi,
  type VnpyPaperAgentRunFilters,
  type VnpyPaperAgentReturnRiskCalibrationTrends,
  type VnpyPaperAgentTradePlan,
  type VnpyPaperAgentRunDetail,
  type VnpyPaperAgentRunSummary,
  type VnpyPaperAllocationMethod,
  type VnpyPaperAccount,
  type VnpyPaperAutoRunResponse,
  type VnpyPaperExecutionMode,
  type VnpyPaperGatewayPreflightResponse,
  type VnpyPaperMarket,
  type VnpyPaperOrderResult,
  type VnpyPaperPerformanceResponse,
  type VnpyPaperSchedulerTaskEvent,
  type VnpyPaperSide,
  type VnpyPaperSettingsUpdate,
  type VnpyPaperStatusResponse,
  type VnpyPaperTaskEventSummaryResponse,
  type VnpyPaperTaskHealthResponse,
  type VnpyPaperTaskMetricsResponse,
  type VnpyPaperTradePlanRecoverySummary,
} from '../api/vnpyPaperTrading';
import { alertsApi } from '../api/alerts';
import { getParsedApiError, toApiErrorMessage } from '../api/error';
import { AppPage, Button, InlineAlert } from '../components/common';
import { PortfolioChangePanel } from '../components/agent';
import type { AlertTriggerItem } from '../types/alerts';

const INPUT_CLASS =
  'h-10 w-full rounded-xl border border-border bg-surface px-3 text-sm text-foreground outline-none transition-colors focus:border-cyan disabled:cursor-not-allowed disabled:opacity-60';
const SELECT_CLASS = `${INPUT_CLASS} appearance-none`;
const TEXTAREA_CLASS =
  'min-h-20 w-full rounded-xl border border-border bg-surface px-3 py-2 text-sm text-foreground outline-none transition-colors focus:border-cyan disabled:cursor-not-allowed disabled:opacity-60';
const CHECKBOX_CLASS = 'h-4 w-4 rounded border-border bg-surface text-cyan focus:ring-cyan/30';
const EXPECTED_VNPY_PAPER_CONTRACT_VERSION = 3;

type AvailabilityDiagnostic = {
  key: string;
  label: string;
  status: string;
  detail: string;
  tone: 'success' | 'warning' | 'danger' | 'info';
};

type SettingsForm = {
  enabled: boolean;
  initialCash: string;
  autoTradeEnabled: boolean;
  autoStrategy: string;
  autoMarket: VnpyPaperMarket;
  autoMaxResults: string;
  autoCashPerOrder: string;
  autoScoreWeightedAllocationEnabled: boolean;
  autoAllocationBudget: string;
  autoAllocationMethod: VnpyPaperAllocationMethod;
  autoRiskVolatilityFloorPct: string;
  autoCorrelationLookbackDays: string;
  autoCorrelationMinObservations: string;
  autoMaxPairwiseCorrelation: string;
  autoCovarianceRiskPenalty: string;
  autoIntervalMinutes: string;
  autoMinScore: string;
  autoSkipExistingPositions: boolean;
  autoExecutionMode: VnpyPaperExecutionMode;
  autoMaxPositions: string;
  autoMaxSinglePositionValue: string;
  autoMaxTotalPositionValue: string;
  autoMaxTotalPositionPct: string;
  autoMaxIndustryPositionValue: string;
  autoMaxIndustryPositionPct: string;
  autoDailyMaxOrders: string;
  autoDailyBudget: string;
  autoTradeTimeGateEnabled: boolean;
  autoSymbolBlacklist: string;
  autoExcludeSt: boolean;
  autoExcludeSuspended: boolean;
  autoExcludePriceLimit: boolean;
  autoMinTurnover: string;
  autoMinDataQualityScore: string;
  autoCrossRunQualityGateEnabled: boolean;
  autoCrossRunHorizonDays: string;
  autoCrossRunMinMatureSamples: string;
  autoCrossRunMinWinRatePct: string;
  autoCrossRunMaxDecisions: string;
  autoMinCashBalance: string;
  autoMaxDrawdownPct: string;
  autoDrawdownRecoveryHysteresisPct: string;
  autoConsecutiveLossLimit: string;
  autoConsecutiveLossCooldownMinutes: string;
  autoMarketLightGateEnabled: boolean;
  autoMarketLightBlockMode: 'red' | 'red_yellow';
  autoMarketContextMaxAgeDays: string;
  autoMarketBreadthGateEnabled: boolean;
  autoMarketBreadthMinScore: string;
  autoHotspotRetreatGateEnabled: boolean;
  autoHotspotRetreatMinDrop: string;
  autoIntradayMarketGateEnabled: boolean;
  autoIntradayRequireProviderTimestamp: boolean;
  autoIntradayIndexMinChangePct: string;
  autoIntradayBreadthMinScore: string;
  autoCrossMarketGateEnabled: boolean;
  autoCrossMarketMinChangePct: string;
  autoFailureFuseEnabled: boolean;
  autoFailureFuseThreshold: string;
  autoFailureFuseAutoRecoveryEnabled: boolean;
  autoFailureFuseCooldownMinutes: string;
  autoSellEnabled: boolean;
  autoStopLossPct: string;
  autoTakeProfitPct: string;
  autoTrailingStopPct: string;
  autoMaxHoldingDays: string;
  autoSellPositionPct: string;
  autoSignalExitEnabled: boolean;
  autoNoProgressDays: string;
  autoNoProgressMinReturnPct: string;
  autoRebalanceEnabled: boolean;
  autoTargetPositionWeights: string;
  autoTargetIndustryWeights: string;
  autoLlmPlanEnabled: boolean;
  autoLlmReviewEnabled: boolean;
  vnpyGatewayName: string;
};

type OrderForm = {
  symbol: string;
  side: VnpyPaperSide;
  market: VnpyPaperMarket;
  quantity: string;
  cashAmount: string;
  price: string;
  note: string;
  executionRoute: 'local_paper' | 'vnpy_bridge';
};

type AgentRunFilterForm = {
  strategy: string;
  market: string;
  status: string;
  createdFrom: string;
  createdTo: string;
};

type PerformanceFilterForm = {
  createdFrom: string;
  createdTo: string;
};

type TaskEventFilterForm = {
  name: string;
  status: string;
};

type PaperAccountHistoryFilter = 'all' | 'current' | 'archived' | 'active';

const DEFAULT_AUTO_MIN_DATA_QUALITY_SCORE = 60;

const defaultSettingsForm: SettingsForm = {
  enabled: true,
  initialCash: '100000',
  autoTradeEnabled: false,
  autoStrategy: 'dual_low',
  autoMarket: 'cn',
  autoMaxResults: '3',
  autoCashPerOrder: '10000',
  autoScoreWeightedAllocationEnabled: false,
  autoAllocationBudget: '',
  autoAllocationMethod: 'score_weighted',
  autoRiskVolatilityFloorPct: '5',
  autoCorrelationLookbackDays: '60',
  autoCorrelationMinObservations: '20',
  autoMaxPairwiseCorrelation: '0.85',
  autoCovarianceRiskPenalty: '0.25',
  autoIntervalMinutes: '1440',
  autoMinScore: '',
  autoSkipExistingPositions: true,
  autoExecutionMode: 'paper',
  autoMaxPositions: '10',
  autoMaxSinglePositionValue: '',
  autoMaxTotalPositionValue: '',
  autoMaxTotalPositionPct: '',
  autoMaxIndustryPositionValue: '',
  autoMaxIndustryPositionPct: '',
  autoDailyMaxOrders: '',
  autoDailyBudget: '',
  autoTradeTimeGateEnabled: true,
  autoSymbolBlacklist: '',
  autoExcludeSt: true,
  autoExcludeSuspended: true,
  autoExcludePriceLimit: true,
  autoMinTurnover: '',
  autoMinDataQualityScore: String(DEFAULT_AUTO_MIN_DATA_QUALITY_SCORE),
  autoCrossRunQualityGateEnabled: false,
  autoCrossRunHorizonDays: '5',
  autoCrossRunMinMatureSamples: '10',
  autoCrossRunMinWinRatePct: '45',
  autoCrossRunMaxDecisions: '200',
  autoMinCashBalance: '',
  autoMaxDrawdownPct: '',
  autoDrawdownRecoveryHysteresisPct: '0',
  autoConsecutiveLossLimit: '',
  autoConsecutiveLossCooldownMinutes: '1440',
  autoMarketLightGateEnabled: false,
  autoMarketLightBlockMode: 'red',
  autoMarketContextMaxAgeDays: '7',
  autoMarketBreadthGateEnabled: false,
  autoMarketBreadthMinScore: '35',
  autoHotspotRetreatGateEnabled: false,
  autoHotspotRetreatMinDrop: '25',
  autoIntradayMarketGateEnabled: false,
  autoIntradayRequireProviderTimestamp: true,
  autoIntradayIndexMinChangePct: '-2',
  autoIntradayBreadthMinScore: '35',
  autoCrossMarketGateEnabled: false,
  autoCrossMarketMinChangePct: '-2',
  autoFailureFuseEnabled: false,
  autoFailureFuseThreshold: '3',
  autoFailureFuseAutoRecoveryEnabled: false,
  autoFailureFuseCooldownMinutes: '1440',
  autoSellEnabled: false,
  autoStopLossPct: '',
  autoTakeProfitPct: '',
  autoTrailingStopPct: '',
  autoMaxHoldingDays: '',
  autoSellPositionPct: '',
  autoSignalExitEnabled: false,
  autoNoProgressDays: '',
  autoNoProgressMinReturnPct: '',
  autoRebalanceEnabled: false,
  autoTargetPositionWeights: '',
  autoTargetIndustryWeights: '',
  autoLlmPlanEnabled: false,
  autoLlmReviewEnabled: false,
  vnpyGatewayName: '',
};

const defaultOrderForm: OrderForm = {
  symbol: '000001',
  side: 'buy',
  market: 'cn',
  quantity: '100',
  cashAmount: '',
  price: '',
  note: '',
  executionRoute: 'local_paper',
};

const defaultAgentRunFilterForm: AgentRunFilterForm = {
  strategy: '',
  market: '',
  status: '',
  createdFrom: '',
  createdTo: '',
};

const defaultPerformanceFilterForm: PerformanceFilterForm = {
  createdFrom: '',
  createdTo: '',
};

const defaultTaskEventFilterForm: TaskEventFilterForm = {
  name: '',
  status: '',
};

const markets: Array<{ value: VnpyPaperMarket; label: string }> = [
  { value: 'cn', label: 'A 股' },
  { value: 'hk', label: '港股' },
  { value: 'us', label: '美股' },
  { value: 'tw', label: '台股' },
  { value: 'jp', label: '日股' },
  { value: 'kr', label: '韩股' },
];

const agentRunStatuses = ['completed', 'failed', 'skipped', 'running'];

function parseNumber(value: string): number | undefined {
  const text = value.trim();
  if (!text) return undefined;
  const parsed = Number(text);
  return Number.isFinite(parsed) ? parsed : undefined;
}

function parseSymbolList(value: string): string[] {
  const seen = new Set<string>();
  return value
    .replace(/，|；|;/g, ',')
    .split(',')
    .map((item) => item.trim())
    .filter((item) => {
      if (!item || seen.has(item)) return false;
      seen.add(item);
      return true;
    });
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

function formatNumber(value: unknown, digits = 2): string {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return '-';
  return numeric.toFixed(digits);
}

function finiteNumberOrNull(value: unknown): number | null {
  if (value === null || value === undefined || value === '') return null;
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : null;
}

function formatPercent(value: unknown): string {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return '-';
  return `${numeric.toFixed(2)}%`;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function formatWeightMap(value: Record<string, number> | null | undefined): string {
  if (!value) return '';
  return Object.entries(value)
    .filter(([key, weight]) => key && Number.isFinite(Number(weight)))
    .map(([key, weight]) => `${key}:${Number(weight)}`)
    .join('\n');
}

function parseWeightMap(value: string, label: string): Record<string, number> {
  const text = value.trim();
  if (!text) return {};
  let entries: Array<[string, unknown]> = [];
  if (text.startsWith('{')) {
    let parsed: unknown;
    try {
      parsed = JSON.parse(text);
    } catch {
      throw new Error(`${label}格式错误：请输入 JSON 对象或 key:percent 列表`);
    }
    const record = asRecord(parsed);
    if (!record) {
      throw new Error(`${label}格式错误：请输入 JSON 对象或 key:percent 列表`);
    }
    entries = Object.entries(record);
  } else {
    entries = text
      .replace(/；/g, ';')
      .replace(/，/g, ',')
      .split(/[\n,;]+/)
      .map((chunk) => chunk.trim())
      .filter(Boolean)
      .map((chunk) => {
        const separator = chunk.includes(':') ? ':' : chunk.includes('=') ? '=' : '';
        if (!separator) return ['', undefined];
        const [key, weight] = chunk.split(separator, 2);
        return [key.trim(), weight.trim()];
      });
  }

  const weights: Record<string, number> = {};
  for (const [rawKey, rawWeight] of entries) {
    const key = String(rawKey || '').trim();
    const weight = Number(rawWeight);
    if (!key) continue;
    if (!Number.isFinite(weight) || weight <= 0 || weight > 100) {
      throw new Error(`${label}格式错误：${key} 的权重必须在 0 到 100 之间`);
    }
    weights[key] = weight;
  }
  return weights;
}

function asRecordList(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value)
    ? value.filter((item): item is Record<string, unknown> => Boolean(asRecord(item)))
    : [];
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

function formatCurveLabel(value: unknown, fallback: string): string {
  const text = String(value || '');
  if (!text) return fallback;
  const parsed = new Date(text);
  if (Number.isNaN(parsed.getTime())) return text;
  return parsed.toLocaleDateString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
  });
}

function formatTaskEventDetails(details: Record<string, unknown> | undefined): string {
  if (!details) return '-';
  const parts = [
    details.reason ? `reason=${String(details.reason)}` : '',
    details.agentRunUid || details.agent_run_uid ? `run=${String(details.agentRunUid || details.agent_run_uid)}` : '',
    Number.isFinite(Number(details.submittedCount ?? details.submitted_count))
      ? `submitted=${formatNumber(details.submittedCount ?? details.submitted_count, 0)}`
      : '',
    Number.isFinite(Number(details.skippedCount ?? details.skipped_count))
      ? `skipped=${formatNumber(details.skippedCount ?? details.skipped_count, 0)}`
      : '',
    Number.isFinite(Number(details.attemptedCount ?? details.attempted_count))
      ? `attempted=${formatNumber(details.attemptedCount ?? details.attempted_count, 0)}`
      : '',
    Number.isFinite(Number(details.failedCount ?? details.failed_count))
      ? `failed=${formatNumber(details.failedCount ?? details.failed_count, 0)}`
      : '',
  ].filter(Boolean);
  return parts.join(' / ') || '-';
}

function retryValue(payload: Record<string, unknown>, snakeKey: string, camelKey: string): unknown {
  return payload[snakeKey] ?? payload[camelKey];
}

function tradePlanRetryInfo(plan: VnpyPaperAgentTradePlan): Record<string, unknown> | null {
  const orderResult = asRecord(plan.orderResult);
  const raw = asRecord(orderResult?.raw);
  return asRecord(orderResult?.retry) ?? asRecord(raw?.retry);
}

function formatTradePlanRetry(plan: VnpyPaperAgentTradePlan): string | null {
  const retry = tradePlanRetryInfo(plan);
  if (!retry) return null;
  const attempts = Number(retryValue(retry, 'attempt_count', 'attemptCount'));
  const maxAttempts = Number(retryValue(retry, 'max_attempts', 'maxAttempts'));
  const parts: string[] = [];
  if (Number.isFinite(attempts) && Number.isFinite(maxAttempts)) {
    parts.push(`重试 ${attempts}/${maxAttempts}`);
  }
  const nextRetryAfter = retryValue(retry, 'next_retry_after', 'nextRetryAfter');
  if (nextRetryAfter) {
    parts.push(`下次 ${formatDateTime(nextRetryAfter)}`);
  }
  return parts.length > 0 ? parts.join(' · ') : null;
}

function canRetryTradePlan(plan: VnpyPaperAgentTradePlan): boolean {
  return ['manual_approval', 'paper', 'vnpy_paper'].includes(plan.executionMode)
    && ['failed', 'skipped'].includes(plan.status);
}

function canCancelTradePlan(plan: VnpyPaperAgentTradePlan): boolean {
  return plan.executionMode === 'vnpy_paper' && ['submitted', 'part_filled'].includes(plan.status);
}

function formatTradingWindowStatus(value: unknown): string {
  const status = String(value || '');
  const labels: Record<string, string> = {
    open_now: '开市中',
    opens_later_today: '今日稍后',
    next_session: '下一交易日',
    unknown: '未知',
    calendar_unavailable: '交易日历不可用',
    unknown_market: '市场未知',
    no_session_in_lookahead: '近期无交易日',
    calendar_error: '交易日历异常',
    time_gate_disabled: '时间窗关闭',
    execution_mode_not_gated: '当前模式不拦截',
  };
  return labels[status] || status || '-';
}

function settingsToForm(status: VnpyPaperStatusResponse): SettingsForm {
  const settings = status.settings;
  return {
    enabled: Boolean(settings.enabled),
    initialCash: String(settings.initialCash ?? 100000),
    autoTradeEnabled: Boolean(settings.autoTradeEnabled),
    autoStrategy: settings.autoStrategy || 'dual_low',
    autoMarket: (settings.autoMarket || 'cn') as VnpyPaperMarket,
    autoMaxResults: String(settings.autoMaxResults ?? 3),
    autoCashPerOrder: String(settings.autoCashPerOrder ?? 10000),
    autoScoreWeightedAllocationEnabled: Boolean(settings.autoScoreWeightedAllocationEnabled),
    autoAllocationBudget: settings.autoAllocationBudget == null
      ? ''
      : String(settings.autoAllocationBudget),
    autoAllocationMethod: (
      settings.autoAllocationMethod === 'score_inverse_volatility_20d'
      || settings.autoAllocationMethod === 'score_inverse_volatility_20d_correlation_capped'
      || settings.autoAllocationMethod === 'target_tracking_min_variance_20d'
    ) ? settings.autoAllocationMethod : 'score_weighted',
    autoRiskVolatilityFloorPct: String(settings.autoRiskVolatilityFloorPct ?? 5),
    autoCorrelationLookbackDays: String(settings.autoCorrelationLookbackDays ?? 60),
    autoCorrelationMinObservations: String(settings.autoCorrelationMinObservations ?? 20),
    autoMaxPairwiseCorrelation: String(settings.autoMaxPairwiseCorrelation ?? 0.85),
    autoCovarianceRiskPenalty: String(settings.autoCovarianceRiskPenalty ?? 0.25),
    autoIntervalMinutes: String(settings.autoIntervalMinutes ?? 1440),
    autoMinScore: settings.autoMinScore == null ? '' : String(settings.autoMinScore),
    autoSkipExistingPositions: Boolean(settings.autoSkipExistingPositions),
    autoExecutionMode: (settings.autoExecutionMode || 'paper') as VnpyPaperExecutionMode,
    autoMaxPositions: String(settings.autoMaxPositions ?? 10),
    autoMaxSinglePositionValue: settings.autoMaxSinglePositionValue == null
      ? ''
      : String(settings.autoMaxSinglePositionValue),
    autoMaxTotalPositionValue: settings.autoMaxTotalPositionValue == null
      ? ''
      : String(settings.autoMaxTotalPositionValue),
    autoMaxTotalPositionPct: settings.autoMaxTotalPositionPct == null
      ? ''
      : String(settings.autoMaxTotalPositionPct),
    autoMaxIndustryPositionValue: settings.autoMaxIndustryPositionValue == null
      ? ''
      : String(settings.autoMaxIndustryPositionValue),
    autoMaxIndustryPositionPct: settings.autoMaxIndustryPositionPct == null
      ? ''
      : String(settings.autoMaxIndustryPositionPct),
    autoDailyMaxOrders: settings.autoDailyMaxOrders == null ? '' : String(settings.autoDailyMaxOrders),
    autoDailyBudget: settings.autoDailyBudget == null ? '' : String(settings.autoDailyBudget),
    autoTradeTimeGateEnabled: Boolean(settings.autoTradeTimeGateEnabled ?? true),
    autoSymbolBlacklist: (settings.autoSymbolBlacklist || []).join(','),
    autoExcludeSt: Boolean(settings.autoExcludeSt ?? true),
    autoExcludeSuspended: Boolean(settings.autoExcludeSuspended ?? true),
    autoExcludePriceLimit: Boolean(settings.autoExcludePriceLimit ?? true),
    autoMinTurnover: settings.autoMinTurnover == null ? '' : String(settings.autoMinTurnover),
    autoMinDataQualityScore: settings.autoMinDataQualityScore == null
      ? String(DEFAULT_AUTO_MIN_DATA_QUALITY_SCORE)
      : String(settings.autoMinDataQualityScore),
    autoCrossRunQualityGateEnabled: Boolean(settings.autoCrossRunQualityGateEnabled),
    autoCrossRunHorizonDays: String(settings.autoCrossRunHorizonDays ?? 5),
    autoCrossRunMinMatureSamples: String(settings.autoCrossRunMinMatureSamples ?? 10),
    autoCrossRunMinWinRatePct: String(settings.autoCrossRunMinWinRatePct ?? 45),
    autoCrossRunMaxDecisions: String(settings.autoCrossRunMaxDecisions ?? 200),
    autoMinCashBalance: settings.autoMinCashBalance == null ? '' : String(settings.autoMinCashBalance),
    autoMaxDrawdownPct: settings.autoMaxDrawdownPct == null ? '' : String(settings.autoMaxDrawdownPct),
    autoDrawdownRecoveryHysteresisPct: String(settings.autoDrawdownRecoveryHysteresisPct ?? 0),
    autoConsecutiveLossLimit: settings.autoConsecutiveLossLimit == null
      ? ''
      : String(settings.autoConsecutiveLossLimit),
    autoConsecutiveLossCooldownMinutes: String(
      settings.autoConsecutiveLossCooldownMinutes ?? 1440,
    ),
    autoMarketLightGateEnabled: Boolean(settings.autoMarketLightGateEnabled),
    autoMarketLightBlockMode: (settings.autoMarketLightBlockStatuses || []).includes('yellow')
      ? 'red_yellow'
      : 'red',
    autoMarketContextMaxAgeDays: String(settings.autoMarketContextMaxAgeDays ?? 7),
    autoMarketBreadthGateEnabled: Boolean(settings.autoMarketBreadthGateEnabled),
    autoMarketBreadthMinScore: String(settings.autoMarketBreadthMinScore ?? 35),
    autoHotspotRetreatGateEnabled: Boolean(settings.autoHotspotRetreatGateEnabled),
    autoHotspotRetreatMinDrop: String(settings.autoHotspotRetreatMinDrop ?? 25),
    autoIntradayMarketGateEnabled: Boolean(settings.autoIntradayMarketGateEnabled),
    autoIntradayRequireProviderTimestamp: settings.autoIntradayRequireProviderTimestamp ?? true,
    autoIntradayIndexMinChangePct: String(settings.autoIntradayIndexMinChangePct ?? -2),
    autoIntradayBreadthMinScore: String(settings.autoIntradayBreadthMinScore ?? 35),
    autoCrossMarketGateEnabled: Boolean(settings.autoCrossMarketGateEnabled),
    autoCrossMarketMinChangePct: String(settings.autoCrossMarketMinChangePct ?? -2),
    autoFailureFuseEnabled: Boolean(settings.autoFailureFuseEnabled),
    autoFailureFuseThreshold: String(settings.autoFailureFuseThreshold ?? 3),
    autoFailureFuseAutoRecoveryEnabled: Boolean(settings.autoFailureFuseAutoRecoveryEnabled),
    autoFailureFuseCooldownMinutes: String(settings.autoFailureFuseCooldownMinutes ?? 1440),
    autoSellEnabled: Boolean(settings.autoSellEnabled),
    autoStopLossPct: settings.autoStopLossPct == null ? '' : String(settings.autoStopLossPct),
    autoTakeProfitPct: settings.autoTakeProfitPct == null ? '' : String(settings.autoTakeProfitPct),
    autoTrailingStopPct: settings.autoTrailingStopPct == null ? '' : String(settings.autoTrailingStopPct),
    autoMaxHoldingDays: settings.autoMaxHoldingDays == null ? '' : String(settings.autoMaxHoldingDays),
    autoSellPositionPct: settings.autoSellPositionPct == null ? '' : String(settings.autoSellPositionPct),
    autoSignalExitEnabled: Boolean(settings.autoSignalExitEnabled),
    autoNoProgressDays: settings.autoNoProgressDays == null ? '' : String(settings.autoNoProgressDays),
    autoNoProgressMinReturnPct: settings.autoNoProgressMinReturnPct == null
      ? ''
      : String(settings.autoNoProgressMinReturnPct),
    autoRebalanceEnabled: Boolean(settings.autoRebalanceEnabled),
    autoTargetPositionWeights: formatWeightMap(settings.autoTargetPositionWeights),
    autoTargetIndustryWeights: formatWeightMap(settings.autoTargetIndustryWeights),
    autoLlmPlanEnabled: Boolean(settings.autoLlmPlanEnabled),
    autoLlmReviewEnabled: Boolean(settings.autoLlmReviewEnabled),
    vnpyGatewayName: settings.vnpyGatewayName || '',
  };
}

function buildSettingsUpdate(settingsForm: SettingsForm): VnpyPaperSettingsUpdate {
  return {
    enabled: settingsForm.enabled,
    initialCash: parseNumber(settingsForm.initialCash) ?? 100000,
    autoTradeEnabled: settingsForm.autoTradeEnabled,
    autoStrategy: settingsForm.autoStrategy.trim() || 'dual_low',
    autoMarket: settingsForm.autoMarket,
    autoMaxResults: parseNumber(settingsForm.autoMaxResults) ?? 3,
    autoCashPerOrder: parseNumber(settingsForm.autoCashPerOrder) ?? 10000,
    autoScoreWeightedAllocationEnabled: settingsForm.autoScoreWeightedAllocationEnabled,
    autoAllocationBudget: settingsForm.autoAllocationBudget.trim()
      ? parseNumber(settingsForm.autoAllocationBudget)
      : null,
    autoAllocationMethod: settingsForm.autoAllocationMethod,
    autoRiskVolatilityFloorPct: parseNumber(settingsForm.autoRiskVolatilityFloorPct) ?? 5,
    autoCorrelationLookbackDays: parseNumber(settingsForm.autoCorrelationLookbackDays) ?? 60,
    autoCorrelationMinObservations: parseNumber(settingsForm.autoCorrelationMinObservations) ?? 20,
    autoMaxPairwiseCorrelation: parseNumber(settingsForm.autoMaxPairwiseCorrelation) ?? 0.85,
    autoCovarianceRiskPenalty: parseNumber(settingsForm.autoCovarianceRiskPenalty) ?? 0.25,
    autoIntervalMinutes: parseNumber(settingsForm.autoIntervalMinutes) ?? 1440,
    autoMinScore: settingsForm.autoMinScore.trim() ? parseNumber(settingsForm.autoMinScore) : null,
    autoSkipExistingPositions: settingsForm.autoSkipExistingPositions,
    autoExecutionMode: settingsForm.autoExecutionMode,
    autoMaxPositions: parseNumber(settingsForm.autoMaxPositions) ?? 10,
    autoMaxSinglePositionValue: settingsForm.autoMaxSinglePositionValue.trim()
      ? parseNumber(settingsForm.autoMaxSinglePositionValue)
      : null,
    autoMaxTotalPositionValue: settingsForm.autoMaxTotalPositionValue.trim()
      ? parseNumber(settingsForm.autoMaxTotalPositionValue)
      : null,
    autoMaxTotalPositionPct: settingsForm.autoMaxTotalPositionPct.trim()
      ? parseNumber(settingsForm.autoMaxTotalPositionPct)
      : null,
    autoMaxIndustryPositionValue: settingsForm.autoMaxIndustryPositionValue.trim()
      ? parseNumber(settingsForm.autoMaxIndustryPositionValue)
      : null,
    autoMaxIndustryPositionPct: settingsForm.autoMaxIndustryPositionPct.trim()
      ? parseNumber(settingsForm.autoMaxIndustryPositionPct)
      : null,
    autoDailyMaxOrders: settingsForm.autoDailyMaxOrders.trim()
      ? parseNumber(settingsForm.autoDailyMaxOrders)
      : null,
    autoDailyBudget: settingsForm.autoDailyBudget.trim()
      ? parseNumber(settingsForm.autoDailyBudget)
      : null,
    autoTradeTimeGateEnabled: settingsForm.autoTradeTimeGateEnabled,
    autoSymbolBlacklist: parseSymbolList(settingsForm.autoSymbolBlacklist),
    autoExcludeSt: settingsForm.autoExcludeSt,
    autoExcludeSuspended: settingsForm.autoExcludeSuspended,
    autoExcludePriceLimit: settingsForm.autoExcludePriceLimit,
    autoMinTurnover: settingsForm.autoMinTurnover.trim()
      ? parseNumber(settingsForm.autoMinTurnover)
      : null,
    autoMinDataQualityScore: settingsForm.autoMinDataQualityScore.trim()
      ? parseNumber(settingsForm.autoMinDataQualityScore)
      : DEFAULT_AUTO_MIN_DATA_QUALITY_SCORE,
    autoCrossRunQualityGateEnabled: settingsForm.autoCrossRunQualityGateEnabled,
    autoCrossRunHorizonDays: parseNumber(settingsForm.autoCrossRunHorizonDays) ?? 5,
    autoCrossRunMinMatureSamples: parseNumber(settingsForm.autoCrossRunMinMatureSamples) ?? 10,
    autoCrossRunMinWinRatePct: parseNumber(settingsForm.autoCrossRunMinWinRatePct) ?? 45,
    autoCrossRunMaxDecisions: parseNumber(settingsForm.autoCrossRunMaxDecisions) ?? 200,
    autoMinCashBalance: settingsForm.autoMinCashBalance.trim()
      ? parseNumber(settingsForm.autoMinCashBalance)
      : null,
    autoMaxDrawdownPct: settingsForm.autoMaxDrawdownPct.trim()
      ? parseNumber(settingsForm.autoMaxDrawdownPct)
      : null,
    autoDrawdownRecoveryHysteresisPct:
      parseNumber(settingsForm.autoDrawdownRecoveryHysteresisPct) ?? 0,
    autoConsecutiveLossLimit: settingsForm.autoConsecutiveLossLimit.trim()
      ? parseNumber(settingsForm.autoConsecutiveLossLimit)
      : null,
    autoConsecutiveLossCooldownMinutes:
      parseNumber(settingsForm.autoConsecutiveLossCooldownMinutes) ?? 1440,
    autoMarketLightGateEnabled: settingsForm.autoMarketLightGateEnabled,
    autoMarketLightBlockStatuses: settingsForm.autoMarketLightBlockMode === 'red_yellow'
      ? ['red', 'yellow']
      : ['red'],
    autoMarketContextMaxAgeDays: parseNumber(settingsForm.autoMarketContextMaxAgeDays) ?? 7,
    autoMarketBreadthGateEnabled: settingsForm.autoMarketBreadthGateEnabled,
    autoMarketBreadthMinScore: parseNumber(settingsForm.autoMarketBreadthMinScore) ?? 35,
    autoHotspotRetreatGateEnabled: settingsForm.autoHotspotRetreatGateEnabled,
    autoHotspotRetreatMinDrop: parseNumber(settingsForm.autoHotspotRetreatMinDrop) ?? 25,
    autoIntradayMarketGateEnabled: settingsForm.autoIntradayMarketGateEnabled,
    autoIntradayRequireProviderTimestamp: settingsForm.autoIntradayRequireProviderTimestamp,
    autoIntradayIndexMinChangePct: parseNumber(settingsForm.autoIntradayIndexMinChangePct) ?? -2,
    autoIntradayBreadthMinScore: parseNumber(settingsForm.autoIntradayBreadthMinScore) ?? 35,
    autoCrossMarketGateEnabled: settingsForm.autoCrossMarketGateEnabled,
    autoCrossMarketMinChangePct: parseNumber(settingsForm.autoCrossMarketMinChangePct) ?? -2,
    autoFailureFuseEnabled: settingsForm.autoFailureFuseEnabled,
    autoFailureFuseThreshold: parseNumber(settingsForm.autoFailureFuseThreshold) ?? 3,
    autoFailureFuseAutoRecoveryEnabled: settingsForm.autoFailureFuseAutoRecoveryEnabled,
    autoFailureFuseCooldownMinutes: parseNumber(settingsForm.autoFailureFuseCooldownMinutes) ?? 1440,
    autoSellEnabled: settingsForm.autoSellEnabled,
    autoStopLossPct: settingsForm.autoStopLossPct.trim()
      ? parseNumber(settingsForm.autoStopLossPct)
      : null,
    autoTakeProfitPct: settingsForm.autoTakeProfitPct.trim()
      ? parseNumber(settingsForm.autoTakeProfitPct)
      : null,
    autoTrailingStopPct: settingsForm.autoTrailingStopPct.trim()
      ? parseNumber(settingsForm.autoTrailingStopPct)
      : null,
    autoMaxHoldingDays: settingsForm.autoMaxHoldingDays.trim()
      ? parseNumber(settingsForm.autoMaxHoldingDays)
      : null,
    autoSellPositionPct: settingsForm.autoSellPositionPct.trim()
      ? parseNumber(settingsForm.autoSellPositionPct)
      : null,
    autoSignalExitEnabled: settingsForm.autoSignalExitEnabled,
    autoNoProgressDays: settingsForm.autoNoProgressDays.trim()
      ? parseNumber(settingsForm.autoNoProgressDays)
      : null,
    autoNoProgressMinReturnPct: settingsForm.autoNoProgressMinReturnPct.trim()
      ? parseNumber(settingsForm.autoNoProgressMinReturnPct)
      : null,
    autoRebalanceEnabled: settingsForm.autoRebalanceEnabled,
    autoTargetPositionWeights: parseWeightMap(settingsForm.autoTargetPositionWeights, '目标持仓权重'),
    autoTargetIndustryWeights: parseWeightMap(settingsForm.autoTargetIndustryWeights, '目标行业权重'),
    autoLlmPlanEnabled: settingsForm.autoLlmPlanEnabled,
    autoLlmReviewEnabled: settingsForm.autoLlmReviewEnabled,
    vnpyGatewayName: settingsForm.vnpyGatewayName.trim() || null,
  };
}

function buildAgentRunFilters(filters: AgentRunFilterForm): VnpyPaperAgentRunFilters | undefined {
  const payload: VnpyPaperAgentRunFilters = {};
  const strategy = filters.strategy.trim();
  if (strategy) payload.strategy = strategy;
  if (filters.market) payload.market = filters.market;
  if (filters.status) payload.status = filters.status;
  if (filters.createdFrom) payload.createdFrom = filters.createdFrom;
  if (filters.createdTo) payload.createdTo = filters.createdTo;
  return Object.keys(payload).length > 0 ? payload : undefined;
}

function buildPerformanceFilters(
  filters: PerformanceFilterForm,
): { createdFrom?: string; createdTo?: string } | undefined {
  const payload: { createdFrom?: string; createdTo?: string } = {};
  if (filters.createdFrom) payload.createdFrom = filters.createdFrom;
  if (filters.createdTo) payload.createdTo = filters.createdTo;
  return Object.keys(payload).length > 0 ? payload : undefined;
}

function buildTaskEventFilters(filters: TaskEventFilterForm): { name?: string; status?: string } | undefined {
  const payload: { name?: string; status?: string } = {};
  if (filters.name) payload.name = filters.name;
  if (filters.status) payload.status = filters.status;
  return Object.keys(payload).length > 0 ? payload : undefined;
}

function orderTone(result: VnpyPaperOrderResult): 'success' | 'warning' {
  return result.accepted ? 'success' : 'warning';
}

function statusBadge(enabled: boolean) {
  return enabled ? 'border-success/30 bg-success/10 text-success' : 'border-warning/30 bg-warning/10 text-warning';
}

function diagnosticTone(tone: 'success' | 'warning' | 'danger' | 'info'): string {
  if (tone === 'success') return 'border-success/30 bg-success/10 text-success';
  if (tone === 'danger') return 'border-danger/30 bg-danger/10 text-danger';
  if (tone === 'warning') return 'border-warning/30 bg-warning/10 text-warning';
  return 'border-cyan/30 bg-cyan/10 text-cyan';
}

function diagnosticStatusLabel(status: unknown): string {
  const value = String(status || '');
  const labels: Record<string, string> = {
    ready: '可用',
    warning: '需关注',
    blocked: '不可用',
    disabled: '已停用',
  };
  return labels[value] || value || '-';
}

function diagnosticToneFromStatus(status: unknown): 'success' | 'warning' | 'danger' | 'info' {
  const value = String(status || '');
  if (value === 'ready') return 'success';
  if (value === 'blocked') return 'danger';
  if (value === 'warning') return 'warning';
  return 'info';
}

function decisionTone(status: string): string {
  if (status === 'filled') return 'text-success';
  if (status === 'failed') return 'text-danger';
  return 'text-warning';
}

function alertStatusTone(status: string): string {
  if (status === 'triggered') return 'border-warning/30 bg-warning/10 text-warning';
  if (status === 'failed') return 'border-danger/30 bg-danger/10 text-danger';
  if (status === 'completed') return 'border-success/30 bg-success/10 text-success';
  if (status === 'skipped') return 'border-warning/30 bg-warning/10 text-warning';
  if (status === 'started') return 'border-cyan/30 bg-cyan/10 text-cyan';
  if (status === 'degraded') return 'border-amber-400/30 bg-amber-400/10 text-amber-300';
  return 'border-border bg-surface text-secondary-text';
}

function taskHealthLabel(value?: string | null): string {
  const labels: Record<string, string> = {
    healthy: '健康',
    warning: '需关注',
    error: '异常',
    disabled: '已停用',
  };
  return labels[String(value || '')] || String(value || '-');
}

function taskHealthClass(value?: string | null): string {
  if (value === 'healthy') return 'text-success';
  if (value === 'warning') return 'text-warning';
  if (value === 'error') return 'text-danger';
  return 'text-secondary-text';
}

function formatIntervalSeconds(value?: number | null): string {
  const seconds = Number(value);
  if (!Number.isFinite(seconds) || seconds <= 0) return '-';
  if (seconds < 60) return `${formatNumber(seconds, 0)} 秒`;
  if (seconds < 3600) return `${formatNumber(seconds / 60, 0)} 分钟`;
  return `${formatNumber(seconds / 3600, 1)} 小时`;
}

function dataQualityStatus(run: VnpyPaperAgentRunSummary | VnpyPaperAgentRunDetail | null): string {
  const quality = run?.diagnostics?.dataQuality ?? run?.diagnostics?.data_quality;
  if (!quality || typeof quality !== 'object') return '';
  const status = (quality as { status?: unknown }).status;
  return typeof status === 'string' ? status : '';
}

function riskFlagSummary(run: VnpyPaperAgentRunDetail | null): Array<{ reason: string; count: number }> {
  if (!run) return [];
  const counts = new Map<string, number>();
  run.decisions.forEach((decision) => {
    const flags = decision.riskFlags.length > 0
      ? decision.riskFlags
      : (decision.reason ? [decision.reason] : []);
    flags.forEach((flag) => {
      const reason = String(flag || '').trim();
      if (!reason) return;
      counts.set(reason, (counts.get(reason) || 0) + 1);
    });
  });
  return Array.from(counts.entries())
    .map(([reason, count]) => ({ reason, count }))
    .sort((left, right) => right.count - left.count || left.reason.localeCompare(right.reason));
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

function toStatusLoadErrorMessage(error: unknown): string {
  const parsed = getParsedApiError(error);
  if (parsed.status === 404) {
    return '后端未加载 vn.py 模拟交易 API。请确认当前 Web/API 进程已包含 /api/v1/vnpy-paper 路由，并重启服务后刷新页面。';
  }
  if (parsed.category === 'local_connection_failed') {
    return '无法连接到本地 Web/API 服务。请确认项目已启动、端口可访问，并检查前端 API_BASE_URL 配置。';
  }
  if (parsed.category === 'upstream_timeout') {
    return 'vn.py 模拟交易状态接口超时。请稍后刷新；如果持续出现，请先使用轻量状态接口或检查行情/估值数据源是否卡住。';
  }
  return toApiErrorMessage(error, 'vn.py 模拟交易状态加载失败');
}

const VnpyPaperTradingPage: React.FC = () => {
  const [status, setStatus] = useState<VnpyPaperStatusResponse | null>(null);
  const [settingsForm, setSettingsForm] = useState<SettingsForm>(defaultSettingsForm);
  const [orderForm, setOrderForm] = useState<OrderForm>(defaultOrderForm);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [ensuring, setEnsuring] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [runningAuto, setRunningAuto] = useState(false);
  const [runningDryRun, setRunningDryRun] = useState(false);
  const [pausingAuto, setPausingAuto] = useState(false);
  const [resumingAuto, setResumingAuto] = useState(false);
  const [resettingAccount, setResettingAccount] = useState(false);
  const [restoringAccountId, setRestoringAccountId] = useState<number | null>(null);
  const [cleaningArchivedAccounts, setCleaningArchivedAccounts] = useState(false);
  const [resettingFailureFuse, setResettingFailureFuse] = useState(false);
  const [reconnectingGateway, setReconnectingGateway] = useState(false);
  const [preflightingGateway, setPreflightingGateway] = useState(false);
  const [gatewayPreflight, setGatewayPreflight] = useState<VnpyPaperGatewayPreflightResponse | null>(null);
  const [snapshotLoading, setSnapshotLoading] = useState(false);
  const [snapshotError, setSnapshotError] = useState('');
  const [error, setError] = useState('');
  const [success, setSuccess] = useState('');
  const [orderResult, setOrderResult] = useState<VnpyPaperOrderResult | null>(null);
  const [autoResult, setAutoResult] = useState<VnpyPaperAutoRunResponse | null>(null);
  const [agentRuns, setAgentRuns] = useState<VnpyPaperAgentRunSummary[]>([]);
  const [agentRunFilters, setAgentRunFilters] = useState<AgentRunFilterForm>(defaultAgentRunFilterForm);
  const agentRunFiltersRef = useRef<AgentRunFilterForm>(defaultAgentRunFilterForm);
  const [selectedAgentRun, setSelectedAgentRun] = useState<VnpyPaperAgentRunDetail | null>(null);
  const [agentRunsLoading, setAgentRunsLoading] = useState(false);
  const [agentRunsError, setAgentRunsError] = useState('');
  const [returnRiskCalibrationTrends, setReturnRiskCalibrationTrends] = useState<
    VnpyPaperAgentReturnRiskCalibrationTrends | null
  >(null);
  const [returnRiskCalibrationDays, setReturnRiskCalibrationDays] = useState<7 | 30 | 90>(30);
  const returnRiskCalibrationDaysRef = useRef<7 | 30 | 90>(30);
  const [returnRiskCalibrationLoading, setReturnRiskCalibrationLoading] = useState(false);
  const [returnRiskCalibrationError, setReturnRiskCalibrationError] = useState('');
  const [exportingAgentRuns, setExportingAgentRuns] = useState(false);
  const [approvingPlanUid, setApprovingPlanUid] = useState<string | null>(null);
  const [retryingPlanUid, setRetryingPlanUid] = useState<string | null>(null);
  const [cancellingPlanUid, setCancellingPlanUid] = useState<string | null>(null);
  const [performance, setPerformance] = useState<VnpyPaperPerformanceResponse | null>(null);
  const [performanceLoading, setPerformanceLoading] = useState(false);
  const [performanceError, setPerformanceError] = useState('');
  const [performanceFilters, setPerformanceFilters] = useState<PerformanceFilterForm>(defaultPerformanceFilterForm);
  const [performanceFilterForm, setPerformanceFilterForm] = useState<PerformanceFilterForm>(
    defaultPerformanceFilterForm,
  );
  const performanceFiltersRef = useRef<PerformanceFilterForm>(defaultPerformanceFilterForm);
  const [tradePlanRecovery, setTradePlanRecovery] = useState<VnpyPaperTradePlanRecoverySummary | null>(null);
  const [tradePlanRecoveryLoading, setTradePlanRecoveryLoading] = useState(false);
  const [tradePlanRecoveryRunning, setTradePlanRecoveryRunning] = useState(false);
  const [tradePlanRecoveryError, setTradePlanRecoveryError] = useState('');
  const [taskHealth, setTaskHealth] = useState<VnpyPaperTaskHealthResponse | null>(null);
  const [taskHealthLoading, setTaskHealthLoading] = useState(false);
  const [taskHealthError, setTaskHealthError] = useState('');
  const [taskEvents, setTaskEvents] = useState<VnpyPaperSchedulerTaskEvent[]>([]);
  const [taskEventsLoading, setTaskEventsLoading] = useState(false);
  const [taskEventsError, setTaskEventsError] = useState('');
  const [taskEventSummary, setTaskEventSummary] = useState<VnpyPaperTaskEventSummaryResponse | null>(null);
  const [taskEventSummaryLoading, setTaskEventSummaryLoading] = useState(false);
  const [taskEventSummaryError, setTaskEventSummaryError] = useState('');
  const [taskMetrics, setTaskMetrics] = useState<VnpyPaperTaskMetricsResponse | null>(null);
  const [taskMetricsDays, setTaskMetricsDays] = useState<7 | 30 | 90>(30);
  const taskMetricsDaysRef = useRef<7 | 30 | 90>(30);
  const [taskMetricsLoading, setTaskMetricsLoading] = useState(false);
  const [taskMetricsError, setTaskMetricsError] = useState('');
  const [taskEventFilters, setTaskEventFilters] = useState<TaskEventFilterForm>(defaultTaskEventFilterForm);
  const taskEventFiltersRef = useRef<TaskEventFilterForm>(defaultTaskEventFilterForm);
  const [autoAlertTriggers, setAutoAlertTriggers] = useState<AlertTriggerItem[]>([]);
  const [autoAlertTriggersLoading, setAutoAlertTriggersLoading] = useState(false);
  const [autoAlertTriggersError, setAutoAlertTriggersError] = useState('');
  const [paperAccounts, setPaperAccounts] = useState<VnpyPaperAccount[]>([]);
  const [paperAccountsError, setPaperAccountsError] = useState('');
  const [paperAccountsHiddenCount, setPaperAccountsHiddenCount] = useState(0);
  const [paperAccountFilter, setPaperAccountFilter] = useState<PaperAccountHistoryFilter>('all');
  const selectedRiskSummary = useMemo(() => riskFlagSummary(selectedAgentRun), [selectedAgentRun]);
  const selectedAgentPlan = asRecord(selectedAgentRun?.diagnostics?.agentPlan);
  const selectedAgentSummary = asRecord(selectedAgentRun?.diagnostics?.agentSummary);
  const selectedAgentSummarySkips = asRecordList(selectedAgentSummary?.topSkipReasons);
  const selectedAgentRiskBudget = asRecord(selectedAgentPlan?.riskBudget);
  const selectedAgentGates = asRecord(selectedAgentPlan?.gates);

  useEffect(() => {
    agentRunFiltersRef.current = agentRunFilters;
  }, [agentRunFilters]);

  useEffect(() => {
    returnRiskCalibrationDaysRef.current = returnRiskCalibrationDays;
  }, [returnRiskCalibrationDays]);

  useEffect(() => {
    performanceFiltersRef.current = performanceFilters;
  }, [performanceFilters]);

  useEffect(() => {
    taskEventFiltersRef.current = taskEventFilters;
  }, [taskEventFilters]);

  useEffect(() => {
    taskMetricsDaysRef.current = taskMetricsDays;
  }, [taskMetricsDays]);

  useEffect(() => {
    document.title = 'vn.py 模拟交易 - DSA';
  }, []);

  const applyStatus = useCallback((
    next: VnpyPaperStatusResponse,
    options: { preserveDetails?: boolean } = {},
  ) => {
    setStatus((prev) => {
      if (!options.preserveDetails || !prev) return next;
      return {
        ...next,
        snapshot: next.snapshot ?? prev.snapshot,
        recentTrades: next.recentTrades.length > 0 ? next.recentTrades : prev.recentTrades,
      };
    });
    setSettingsForm(settingsToForm(next));
  }, []);

  const loadFullStatus = useCallback(async () => {
    setSnapshotLoading(true);
    setSnapshotError('');
    try {
      applyStatus(await vnpyPaperTradingApi.getStatus({
        includeSnapshot: true,
        includeRecentTrades: true,
      }));
    } catch (err) {
      setSnapshotError(toApiErrorMessage(err, '持仓快照加载失败'));
    } finally {
      setSnapshotLoading(false);
    }
  }, [applyStatus]);

  const loadStatus = useCallback(async () => {
    setLoading(true);
    setError('');
    setSnapshotError('');
    try {
      applyStatus(await vnpyPaperTradingApi.getStatus({
        includeSnapshot: false,
        includeRecentTrades: false,
      }), { preserveDetails: true });
      setLoading(false);
      await loadFullStatus();
    } catch (err) {
      setError(toStatusLoadErrorMessage(err));
      setLoading(false);
    } finally {
      setLoading(false);
    }
  }, [applyStatus, loadFullStatus]);

  const loadAgentRunDetail = useCallback(async (runUid: string) => {
    setAgentRunsError('');
    try {
      setSelectedAgentRun(await vnpyPaperTradingApi.getAgentRun(runUid));
    } catch (err) {
      setAgentRunsError(toApiErrorMessage(err, 'Agent 运行详情加载失败'));
    }
  }, []);

  const loadReturnRiskCalibrationTrends = useCallback(async (
    daysOverride?: 7 | 30 | 90,
    filtersOverride?: AgentRunFilterForm,
  ) => {
    setReturnRiskCalibrationLoading(true);
    setReturnRiskCalibrationError('');
    try {
      const filters = buildAgentRunFilters(filtersOverride ?? agentRunFiltersRef.current);
      setReturnRiskCalibrationTrends(await vnpyPaperTradingApi.getAgentReturnRiskCalibrationTrends(
        daysOverride ?? returnRiskCalibrationDaysRef.current,
        filters,
      ));
    } catch (err) {
      setReturnRiskCalibrationTrends(null);
      setReturnRiskCalibrationError(toApiErrorMessage(err, '收益/风险长期校准加载失败'));
    } finally {
      setReturnRiskCalibrationLoading(false);
    }
  }, []);

  const loadAgentRuns = useCallback(async (filtersOverride?: AgentRunFilterForm) => {
    setAgentRunsLoading(true);
    setAgentRunsError('');
    void loadReturnRiskCalibrationTrends(undefined, filtersOverride);
    try {
      const filters = buildAgentRunFilters(filtersOverride ?? agentRunFiltersRef.current);
      const result = filters
        ? await vnpyPaperTradingApi.listAgentRuns(10, 0, filters)
        : await vnpyPaperTradingApi.listAgentRuns(10, 0);
      setAgentRuns(result.items);
      if (result.items.length > 0) {
        await loadAgentRunDetail(result.items[0].runUid);
      } else {
        setSelectedAgentRun(null);
      }
    } catch (err) {
      setAgentRunsError(toApiErrorMessage(err, 'Agent 运行记录加载失败'));
    } finally {
      setAgentRunsLoading(false);
    }
  }, [loadAgentRunDetail, loadReturnRiskCalibrationTrends]);

  const handleReturnRiskCalibrationWindow = useCallback((days: 7 | 30 | 90) => {
    returnRiskCalibrationDaysRef.current = days;
    setReturnRiskCalibrationDays(days);
    void loadReturnRiskCalibrationTrends(days);
  }, [loadReturnRiskCalibrationTrends]);

  const loadPaperAccounts = useCallback(async () => {
    setPaperAccountsError('');
    try {
      const result = await vnpyPaperTradingApi.listAccounts(true);
      setPaperAccounts(result.items);
      setPaperAccountsHiddenCount(Number(result.hiddenCount ?? 0));
    } catch (err) {
      setPaperAccountsError(toApiErrorMessage(err, '归档账户加载失败'));
    }
  }, []);

  const loadPerformance = useCallback(async (filtersOverride?: PerformanceFilterForm) => {
    setPerformanceLoading(true);
    setPerformanceError('');
    try {
      const filters = buildPerformanceFilters(filtersOverride ?? performanceFiltersRef.current);
      setPerformance(filters
        ? await vnpyPaperTradingApi.getPerformance(50, filters)
        : await vnpyPaperTradingApi.getPerformance(50));
    } catch (err) {
      setPerformanceError(toApiErrorMessage(err, '绩效摘要加载失败'));
    } finally {
      setPerformanceLoading(false);
    }
  }, []);

  const loadTradePlanRecovery = useCallback(async () => {
    setTradePlanRecoveryLoading(true);
    setTradePlanRecoveryError('');
    try {
      setTradePlanRecovery(await vnpyPaperTradingApi.getTradePlanRecoverySummary(100));
    } catch (err) {
      setTradePlanRecoveryError(toApiErrorMessage(err, '交易计划恢复矩阵加载失败'));
    } finally {
      setTradePlanRecoveryLoading(false);
    }
  }, []);

  const loadTaskHealth = useCallback(async () => {
    setTaskHealthLoading(true);
    setTaskHealthError('');
    try {
      setTaskHealth(await vnpyPaperTradingApi.getTaskHealth());
    } catch (err) {
      setTaskHealthError(toApiErrorMessage(err, '任务健康检查加载失败'));
    } finally {
      setTaskHealthLoading(false);
    }
  }, []);

  const loadTaskEventSummary = useCallback(async () => {
    setTaskEventSummaryLoading(true);
    setTaskEventSummaryError('');
    try {
      setTaskEventSummary(await vnpyPaperTradingApi.getTaskEventSummary(100));
    } catch (err) {
      setTaskEventSummaryError(toApiErrorMessage(err, '后台任务趋势加载失败'));
    } finally {
      setTaskEventSummaryLoading(false);
    }
  }, []);

  const loadTaskMetrics = useCallback(async (days: 7 | 30 | 90 = 30) => {
    setTaskMetricsLoading(true);
    setTaskMetricsError('');
    try {
      setTaskMetrics(await vnpyPaperTradingApi.getTaskMetrics(days));
    } catch (err) {
      setTaskMetricsError(toApiErrorMessage(err, '后台任务长期指标加载失败'));
    } finally {
      setTaskMetricsLoading(false);
    }
  }, []);

  const loadTaskEvents = useCallback(async (filtersOverride?: TaskEventFilterForm) => {
    setTaskEventsLoading(true);
    setTaskEventsError('');
    try {
      const filters = buildTaskEventFilters(filtersOverride ?? taskEventFiltersRef.current);
      const result = filters
        ? await vnpyPaperTradingApi.getTaskEvents(50, filters)
        : await vnpyPaperTradingApi.getTaskEvents(50);
      setTaskEvents(result.items);
      void loadTaskEventSummary();
      void loadTaskMetrics(taskMetricsDaysRef.current);
    } catch (err) {
      setTaskEventsError(toApiErrorMessage(err, '后台任务日志加载失败'));
    } finally {
      setTaskEventsLoading(false);
    }
  }, [loadTaskEventSummary, loadTaskMetrics]);

  const loadAutoAlertTriggers = useCallback(async () => {
    setAutoAlertTriggersLoading(true);
    setAutoAlertTriggersError('');
    try {
      const result = await alertsApi.listTriggers({
        target: 'vnpy_paper',
        pageSize: 5,
      });
      setAutoAlertTriggers(result.items);
    } catch (err) {
      setAutoAlertTriggersError(toApiErrorMessage(err, '自动交易告警历史加载失败'));
    } finally {
      setAutoAlertTriggersLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadStatus();
    void loadPaperAccounts();
    void loadAgentRuns();
    void loadPerformance();
    void loadTradePlanRecovery();
    void loadTaskHealth();
    void loadTaskEvents();
    void loadAutoAlertTriggers();
  }, [
    loadStatus,
    loadPaperAccounts,
    loadAgentRuns,
    loadPerformance,
    loadTradePlanRecovery,
    loadTaskHealth,
    loadTaskEvents,
    loadAutoAlertTriggers,
  ]);

  const snapshotAccount = status?.snapshot?.accounts?.[0];
  const currency = snapshotAccount?.baseCurrency || 'CNY';
  const performanceCurrency = performance?.account?.baseCurrency || currency;
  const performanceRunCount = Number(performance?.runWindow?.runCount ?? 0);
  const performanceSubmitted = Number(performance?.agent?.submittedCount ?? 0);
  const performancePlanned = Number(performance?.agent?.plannedCount ?? 0);
  const performanceTradeMetrics = performance?.tradeMetrics;
  const performanceRiskMetrics = performance?.riskMetrics;
  const performanceWindowLabel = performanceFilters.createdFrom || performanceFilters.createdTo
    ? `${performanceFilters.createdFrom || '起始不限'} 至 ${performanceFilters.createdTo || '结束不限'}`
    : `最近 ${formatNumber(performanceRunCount, 0)} 次 Agent run`;
  const strategyAttribution = useMemo(() => performance?.strategyAttribution ?? [], [performance?.strategyAttribution]);
  const industryAttribution = useMemo(() => performance?.industryAttribution ?? [], [performance?.industryAttribution]);
  const equityCurveRows = useMemo(() => {
    const initialCash = Number(performance?.initialCash ?? 0);
    return (performance?.equityCurve ?? []).reduce<Array<{
      name: string;
      equity: number;
      returnPct: number | null;
      realizedPnl: number | null;
      source: string;
      symbol: string;
      side: string;
    }>>((rows, point, index) => {
      const equity = Number(point.equity);
      if (!Number.isFinite(equity)) return rows;
      const realizedPnl = Number(point.realizedPnl);
      rows.push({
        name: formatCurveLabel(point.date ?? point.source, String(index + 1)),
        equity,
        returnPct: initialCash > 0 ? ((equity - initialCash) / initialCash) * 100 : null,
        realizedPnl: Number.isFinite(realizedPnl) ? realizedPnl : null,
        source: point.source || '-',
        symbol: point.symbol || '',
        side: point.side || '',
      });
      return rows;
    }, []);
  }, [performance?.equityCurve, performance?.initialCash]);
  const latestEquityPoint = equityCurveRows[equityCurveRows.length - 1];
  const peakEquityPoint = equityCurveRows.reduce<typeof latestEquityPoint | undefined>((peak, point) => (
    !peak || point.equity > peak.equity ? point : peak
  ), undefined);
  const dailyReturnRows = useMemo(() => (performance?.dailyReturns ?? [])
    .map((item) => ({
      date: item.date,
      equity: finiteNumberOrNull(item.equity),
      dailyPnl: finiteNumberOrNull(item.dailyPnl),
      dailyReturnPct: finiteNumberOrNull(item.dailyReturnPct),
      cumulativeReturnPct: finiteNumberOrNull(item.cumulativeReturnPct),
      drawdownPct: finiteNumberOrNull(item.drawdownPct),
      tradeCount: finiteNumberOrNull(item.tradeCount) ?? 0,
      source: item.source || '-',
    }))
    .filter((item) => item.date && item.equity !== null)
    .slice(-8)
    .reverse(), [performance?.dailyReturns]);
  const monthlyReturnRows = useMemo(() => (performance?.monthlyReturns ?? [])
    .map((item) => ({
      month: item.month,
      startEquity: finiteNumberOrNull(item.startEquity),
      endEquity: finiteNumberOrNull(item.endEquity),
      monthlyPnl: finiteNumberOrNull(item.monthlyPnl),
      monthlyReturnPct: finiteNumberOrNull(item.monthlyReturnPct),
      cumulativeReturnPct: finiteNumberOrNull(item.cumulativeReturnPct),
      drawdownPct: finiteNumberOrNull(item.drawdownPct),
      tradeCount: finiteNumberOrNull(item.tradeCount) ?? 0,
      positiveDays: finiteNumberOrNull(item.positiveDays) ?? 0,
      negativeDays: finiteNumberOrNull(item.negativeDays) ?? 0,
    }))
    .filter((item) => item.month && item.endEquity !== null)
    .slice(-6)
    .reverse(), [performance?.monthlyReturns]);
  const performanceBuyCount = Number(performanceTradeMetrics?.buyCount ?? 0);
  const performanceSellCount = Number(performanceTradeMetrics?.sellCount ?? 0);
  const topSkipReason = performance?.topSkipReasons?.[0];
  const topStrategy = strategyAttribution[0];
  const topIndustry = industryAttribution[0];
  const performanceMatrixRows = useMemo(() => ([
    ...strategyAttribution.map((item) => ({
      id: `strategy-${item.key}`,
      dimension: '策略',
      name: item.key,
      sampleCount: item.runCount,
      plannedCount: item.plannedCount,
      filledCount: item.filledPlanCount,
      skippedCount: item.skippedCount,
      filledCashAmount: item.filledCashAmount,
      fillRatePct: item.fillRatePct,
    })),
    ...industryAttribution.map((item) => ({
      id: `industry-${item.key}`,
      dimension: '行业',
      name: item.key,
      sampleCount: item.decisionCount,
      plannedCount: null,
      filledCount: item.filledCount,
      skippedCount: item.skippedCount,
      filledCashAmount: item.filledCashAmount,
      fillRatePct: item.fillRatePct,
    })),
  ]), [industryAttribution, strategyAttribution]);
  const tradePlanRecoveryItems = useMemo(() => tradePlanRecovery?.items ?? [], [tradePlanRecovery?.items]);
  const tradePlanRecoveryActiveCount = tradePlanRecoveryItems.filter((item) => item.isActive).length;
  const tradePlanRecoveryFocusRows = tradePlanRecoveryItems
    .filter((item) => (
      item.staleActive
      || item.retryDue
      || item.recoveryState === 'retry_cooldown'
      || item.recoveryState === 'retry_limit_reached'
      || item.recoveryState === 'manual_approval_pending'
      || item.isActive
    ))
    .slice(0, 8);
  const taskHealthItems = useMemo(() => taskHealth?.items ?? [], [taskHealth?.items]);
  const taskHealthUnhealthyCount = taskHealthItems.filter((item) => (
    item.health === 'warning' || item.health === 'error'
  )).length;
  const positionRows = useMemo(() => snapshotAccount?.positions || [], [snapshotAccount?.positions]);
  const snapshotDiagnostics = asRecord(status?.diagnostics);
  const snapshotErrorDiagnostic = String(snapshotDiagnostics?.snapshotError || '').trim();
  const snapshotLimitations = useMemo(() => ([
    ...asRecordList(status?.snapshot?.limitations),
    ...asRecordList(snapshotAccount?.limitations),
  ]), [snapshotAccount?.limitations, status?.snapshot?.limitations]);
  const valuationWarning = useMemo(() => {
    if (snapshotErrorDiagnostic) {
      return `持仓快照生成失败：${snapshotErrorDiagnostic}`;
    }
    const missingPricePositions = positionRows.filter((item) => item.priceAvailable === false);
    if (missingPricePositions.length > 0) {
      const symbols = missingPricePositions
        .slice(0, 3)
        .map((item) => item.symbol)
        .filter(Boolean)
        .join('、');
      return `${symbols || '部分持仓'} 缺少可用价格，市值和浮盈可能偏低或显示为 0。`;
    }
    const limitationMessages = snapshotLimitations
      .map((item) => String(item.message || item.reason || item.code || '').trim())
      .filter(Boolean);
    if (limitationMessages.length > 0) {
      return `Portfolio 快照存在估值限制：${limitationMessages.slice(0, 2).join('；')}`;
    }
    const stalePricePositions = positionRows.filter((item) => item.priceStale === true);
    if (stalePricePositions.length > 0) {
      const symbols = stalePricePositions
        .slice(0, 3)
        .map((item) => item.symbol)
        .filter(Boolean)
        .join('、');
      return `${symbols || '部分持仓'} 使用了陈旧价格，市值和浮盈仅供参考。`;
    }
    return '';
  }, [positionRows, snapshotErrorDiagnostic, snapshotLimitations]);
  const paperAvailable = status?.available ?? status?.enabled ?? false;
  const schedulerStatus = status?.scheduler;
  const autoTradeTask = schedulerStatus?.backgroundTasks?.find((task) => task.name === 'vnpy_paper_auto_trade');
  const autoRetryTask = schedulerStatus?.backgroundTasks?.find((task) => task.name === 'vnpy_paper_auto_retry');
  const schedulerTaskEvents = useMemo(
    () => (taskEvents.length > 0 ? taskEvents : (schedulerStatus?.taskEvents ?? [])),
    [schedulerStatus?.taskEvents, taskEvents],
  );
  const taskEventNameOptions = useMemo(() => {
    const names = new Set<string>([
      'vnpy_paper_auto_trade',
      'vnpy_paper_auto_retry',
      ...((schedulerStatus?.backgroundTasks ?? [])
        .map((task) => task.name)
        .filter((name): name is string => Boolean(name))),
      ...schedulerTaskEvents
        .map((event) => event.name)
        .filter((name): name is string => Boolean(name)),
    ]);
    return Array.from(names).sort();
  }, [schedulerStatus?.backgroundTasks, schedulerTaskEvents]);
  const taskEventSummaryItems = useMemo(() => taskEventSummary?.items ?? [], [taskEventSummary?.items]);
  const taskEventStatusCounts = taskEventSummary?.statusCounts ?? {};
  const taskMetricsDaily = useMemo(() => taskMetrics?.daily ?? [], [taskMetrics?.daily]);
  const archivedPaperAccountCount = paperAccounts.filter((account) => account.archived).length;
  const currentPaperAccountCount = paperAccounts.filter((account) => account.isCurrent).length;
  const activeNonCurrentPaperAccountCount = paperAccounts.filter((account) => !account.archived && !account.isCurrent).length;
  const filteredPaperAccounts = useMemo(() => {
    if (paperAccountFilter === 'current') {
      return paperAccounts.filter((account) => account.isCurrent);
    }
    if (paperAccountFilter === 'archived') {
      return paperAccounts.filter((account) => account.archived);
    }
    if (paperAccountFilter === 'active') {
      return paperAccounts.filter((account) => !account.archived && !account.isCurrent);
    }
    return paperAccounts;
  }, [paperAccountFilter, paperAccounts]);
  const paperAccountFilterOptions: Array<{
    value: PaperAccountHistoryFilter;
    label: string;
    count: number;
  }> = [
    { value: 'all', label: '全部', count: paperAccounts.length },
    { value: 'current', label: '当前', count: currentPaperAccountCount },
    { value: 'archived', label: '已归档', count: archivedPaperAccountCount },
    { value: 'active', label: '活跃非当前', count: activeNonCurrentPaperAccountCount },
  ];
  const nextAutoRunAt = autoTradeTask?.nextRunAt || schedulerStatus?.nextRunAt;
  const vnpyAdapter = asRecord(status?.diagnostics?.vnpyAdapter);
  const vnpyBridge = asRecord(status?.diagnostics?.vnpyBridge);
  const vnpyRuntime = asRecord(status?.diagnostics?.vnpyRuntime);
  const vnpyRuntimeConnect = asRecord(vnpyRuntime?.connect);
  const tradingWindow = asRecord(status?.diagnostics?.tradingWindow);
  const failureFuse = asRecord(status?.diagnostics?.failureFuse);
  const systemHealth = asRecord(status?.diagnostics?.systemHealth);
  const systemHealthComponents = useMemo(
    () => asRecordList(systemHealth?.components),
    [systemHealth?.components],
  );
  const valuationHealth = systemHealthComponents.find((item) => item.key === 'valuation');
  const valuationHealthTrends = asRecord(valuationHealth?.trends);
  const valuationTrendWindows = asRecordList(valuationHealthTrends?.windows);
  const valuationTrendLatestObservedAt = valuationTrendWindows
    .map((item) => item.latestObservedAt)
    .find(Boolean);
  const autoTradeReadiness = asRecord(status?.diagnostics?.autoTradeReadiness);
  const backendRuntime = asRecord(status?.diagnostics?.backend);
  const backendApiVersion = String(backendRuntime?.apiVersion || '').trim();
  const backendBuildId = String(backendRuntime?.buildId || '').trim();
  const backendContractVersion = Number(backendRuntime?.vnpyPaperContractVersion || 0);
  const failureFuseEnabled = Boolean(failureFuse?.enabled);
  const failureFuseOpen = Boolean(failureFuse?.open);
  const failureFuseCount = Number(failureFuse?.consecutiveFailureCount ?? 0);
  const failureFuseThreshold = Number(failureFuse?.threshold ?? settingsForm.autoFailureFuseThreshold);
  const failureFuseAutoRecoveryEnabled = Boolean(failureFuse?.autoRecoveryEnabled);
  const failureFuseRecoverAt = failureFuse?.recoverAt;
  const failureFuseDisplay = failureFuseEnabled
    ? `${failureFuseOpen ? '已熔断' : '正常'} ${formatNumber(failureFuseCount, 0)}/${formatNumber(failureFuseThreshold, 0)}`
    : '未启用';
  const vnpyRuntimeMode = String(vnpyRuntime?.mode || (vnpyRuntime?.enabled ? 'enabled' : 'disabled'));
  const vnpyRuntimeReason = String(vnpyRuntime?.reason || '');
  const vnpyRuntimeLabel = vnpyRuntimeReason && vnpyRuntimeReason !== vnpyRuntimeMode
    ? `${vnpyRuntimeMode}: ${vnpyRuntimeReason}`
    : vnpyRuntimeMode;
  const vnpyGatewayConnectionStatus = String(vnpyRuntimeConnect?.status || 'unavailable');
  const vnpyGatewayConnected = vnpyRuntimeConnect?.connected === true;
  const canReconnectGateway = Boolean(
    vnpyRuntime?.available
    && vnpyRuntime?.gatewayName
    && !vnpyGatewayConnected
    && !['connect_requested', 'connection_unconfirmed'].includes(vnpyGatewayConnectionStatus),
  );
  const vnpyAdapterMode = String(vnpyAdapter?.mode || (status?.vnpyAvailable ? 'vnpy_order_request' : 'local_paper_fallback'));
  const vnpyOrderRequestSupported = Boolean(vnpyAdapter?.orderRequestSupported);
  const vnpyCancelRequestSupported = Boolean(vnpyAdapter?.cancelRequestSupported);
  const vnpyBridgeMode = String(vnpyBridge?.mode || 'not_configured');
  const vnpyBridgeAvailable = Boolean(vnpyBridge?.available);
  const vnpyCancelOrderSupported = Boolean(vnpyBridge?.cancelOrderSupported);
  const tradingWindowOpenNow = tradingWindow?.isMarketOpenNow === true;
  const tradingWindowStatus = tradingWindow?.nextWindowStatus || tradingWindow?.reason;
  const tradingWindowGateReason = tradingWindow?.gateReason;
  const tradingWindowTime = tradingWindowOpenNow
    ? tradingWindow?.currentCloseAt || tradingWindow?.nextCloseAt
    : tradingWindow?.nextOpenAt;
  const tradingWindowDisplay = tradingWindowOpenNow
    ? `至 ${formatDateTime(tradingWindowTime)}`
    : (tradingWindow?.available ? formatDateTime(tradingWindowTime) : formatTradingWindowStatus(tradingWindow?.reason));
  const tradingWindowMeta = [
    formatTradingWindowStatus(tradingWindowStatus),
    tradingWindowGateReason ? formatTradingWindowStatus(tradingWindowGateReason) : '',
  ].filter(Boolean).join(' · ');
  const autoExecutionMode = settingsForm.autoExecutionMode;
  const tradingWindowGatesExecution = settingsForm.autoTradeTimeGateEnabled
    && ['paper', 'vnpy_paper'].includes(autoExecutionMode);
  const availabilityDiagnostics = useMemo(() => {
    const withBackendVersion = (items: AvailabilityDiagnostic[]): AvailabilityDiagnostic[] => {
      const compatible = backendContractVersion >= EXPECTED_VNPY_PAPER_CONTRACT_VERSION;
      const detail = backendContractVersion > 0
        ? `API ${backendApiVersion || '-'} · contract ${backendContractVersion} · build ${backendBuildId.slice(0, 16) || 'local'}`
        : `未报告版本，可能仍是旧后端进程；页面需要 contract ${EXPECTED_VNPY_PAPER_CONTRACT_VERSION}`;
      const backendItem: AvailabilityDiagnostic = {
        key: 'backend_version',
        label: '后端版本',
        status: compatible ? '兼容' : '需更新',
        detail,
        tone: compatible ? 'success' : 'warning',
      };
      return [backendItem, ...items.filter((item) => item.key !== 'backend_version')];
    };
    if (systemHealthComponents.length > 0) {
      return withBackendVersion(systemHealthComponents.map((item, index) => {
        const itemStatus = item.status;
        const itemTone = String(item.tone || '') as 'success' | 'warning' | 'danger' | 'info';
        return {
          key: String(item.key || `system-health-${index}`),
          label: String(item.label || item.key || '-'),
          status: diagnosticStatusLabel(itemStatus),
          detail: String(item.detail || item.reason || '-'),
          tone: ['success', 'warning', 'danger', 'info'].includes(itemTone)
            ? itemTone
            : diagnosticToneFromStatus(itemStatus),
        };
      }));
    }

    const readinessComponents = asRecordList(autoTradeReadiness?.components);
    if (readinessComponents.length > 0) {
      return withBackendVersion(readinessComponents.map((item, index) => {
        const itemStatus = item.status;
        const itemTone = String(item.tone || '') as 'success' | 'warning' | 'danger' | 'info';
        return {
          key: String(item.key || `readiness-${index}`),
          label: String(item.label || item.key || '-'),
          status: diagnosticStatusLabel(itemStatus),
          detail: String(item.detail || item.reason || '-'),
          tone: ['success', 'warning', 'danger', 'info'].includes(itemTone)
            ? itemTone
            : diagnosticToneFromStatus(itemStatus),
        };
      }));
    }

    const items: AvailabilityDiagnostic[] = [];

    items.push({
      key: 'local-paper',
      label: '本地账本',
      status: paperAvailable ? '可用' : '不可用',
      detail: paperAvailable
        ? '本地 paper 委托可写入 Portfolio 账本'
        : (!status?.enabled ? '模拟交易开关关闭' : '账户未初始化或状态未返回'),
      tone: paperAvailable ? 'success' : 'danger',
    });

    if (settingsForm.autoTradeEnabled) {
      const schedulerLoopRunning = Boolean(schedulerStatus?.loopRunning ?? schedulerStatus?.enabled);
      const taskRegistered = Boolean(autoTradeTask);
      items.push({
        key: 'auto-task',
        label: '自动任务',
        status: schedulerLoopRunning && taskRegistered ? '已注册' : '需关注',
        detail: schedulerLoopRunning
          ? (taskRegistered ? `下次 ${formatDateTime(autoTradeTask?.nextRunAt || schedulerStatus?.nextRunAt)}` : '自动买入任务未注册')
          : 'Runtime scheduler 未运行',
        tone: schedulerLoopRunning && taskRegistered ? 'success' : 'warning',
      });
    } else {
      items.push({
        key: 'auto-task',
        label: '自动任务',
        status: '已停用',
        detail: '自动买入关闭',
        tone: 'info',
      });
    }

    items.push({
      key: 'trading-window',
      label: '交易窗口',
      status: tradingWindowGatesExecution
        ? (tradingWindowOpenNow ? '开市中' : '等待窗口')
        : '不拦截',
      detail: tradingWindowGatesExecution
        ? (tradingWindowOpenNow
          ? `当前窗口 ${formatDateTime(tradingWindowTime)}`
          : (tradingWindowMeta || '当前不在可交易窗口'))
        : `${autoExecutionMode} 模式不强制拦截`,
      tone: tradingWindowGatesExecution
        ? (tradingWindowOpenNow ? 'success' : 'warning')
        : 'info',
    });

    items.push({
      key: 'failure-fuse',
      label: '连续失败熔断',
      status: failureFuseOpen ? '已熔断' : (failureFuseEnabled ? '正常' : '未启用'),
      detail: failureFuseEnabled
        ? `${formatNumber(failureFuseCount, 0)}/${formatNumber(failureFuseThreshold, 0)}`
        : '未启用连续失败熔断',
      tone: failureFuseOpen ? 'danger' : (failureFuseEnabled ? 'success' : 'info'),
    });

    items.push({
      key: 'vnpy-bridge',
      label: 'vn.py bridge',
      status: autoExecutionMode === 'vnpy_paper'
        ? (vnpyBridgeAvailable ? '可提交' : '不可用')
        : '非必需',
      detail: autoExecutionMode === 'vnpy_paper'
        ? (vnpyBridgeAvailable ? 'MainEngine 已可提交委托' : `${vnpyBridgeMode}${vnpyBridge?.reason ? `: ${String(vnpyBridge.reason)}` : ''}`)
        : `${autoExecutionMode} 模式使用本地或审计流程`,
      tone: autoExecutionMode === 'vnpy_paper'
        ? (vnpyBridgeAvailable ? 'success' : 'danger')
        : 'info',
    });

    return withBackendVersion(items);
  }, [
    autoTradeReadiness,
    autoExecutionMode,
    autoTradeTask,
    backendApiVersion,
    backendBuildId,
    backendContractVersion,
    failureFuseCount,
    failureFuseEnabled,
    failureFuseOpen,
    failureFuseThreshold,
    paperAvailable,
    schedulerStatus?.enabled,
    schedulerStatus?.loopRunning,
    schedulerStatus?.nextRunAt,
    settingsForm.autoTradeEnabled,
    systemHealthComponents,
    status?.enabled,
    tradingWindowGatesExecution,
    tradingWindowMeta,
    tradingWindowOpenNow,
    tradingWindowTime,
    vnpyBridge?.reason,
    vnpyBridgeAvailable,
    vnpyBridgeMode,
  ]);
  const canSubmitOrder = paperAvailable
    && Boolean(orderForm.symbol.trim())
    && (Boolean(parseNumber(orderForm.quantity)) || Boolean(parseNumber(orderForm.cashAmount)));

  const handleEnsureAccount = async () => {
    setEnsuring(true);
    setError('');
    setSuccess('');
    try {
      applyStatus(await vnpyPaperTradingApi.ensureAccount({
        includeSnapshot: false,
        includeRecentTrades: false,
      }), { preserveDetails: true });
      void loadFullStatus();
      void loadTaskHealth();
      void loadTaskEvents();
      setSuccess('模拟账户已就绪');
    } catch (err) {
      setError(toApiErrorMessage(err, '模拟账户初始化失败'));
    } finally {
      setEnsuring(false);
    }
  };

  const handleReconnectGateway = async () => {
    setReconnectingGateway(true);
    setError('');
    setSuccess('');
    try {
      const result = await vnpyPaperTradingApi.reconnectGateway();
      if (result.connected) {
        setSuccess('vn.py gateway 已重新连接');
      } else if (result.attempted) {
        setError(`vn.py gateway 重连后仍未就绪：${result.reason || result.status}`);
      } else {
        setSuccess(`未执行重复重连：${result.reason || result.result}`);
      }
      await loadStatus();
      await loadAutoAlertTriggers();
    } catch (err) {
      setError(toApiErrorMessage(err, 'vn.py gateway 重连失败'));
    } finally {
      setReconnectingGateway(false);
    }
  };

  const handlePreflightGateway = async () => {
    setPreflightingGateway(true);
    setError('');
    setSuccess('');
    try {
      const result = await vnpyPaperTradingApi.preflightGateway();
      setGatewayPreflight(result);
      if (result.ok) {
        setSuccess('外部 vn.py gateway 生产预检通过');
      }
    } catch (err) {
      setError(toApiErrorMessage(err, 'vn.py gateway 生产预检失败'));
    } finally {
      setPreflightingGateway(false);
    }
  };

  const handleSaveSettings = async (event: React.FormEvent) => {
    event.preventDefault();
    setSaving(true);
    setError('');
    setSuccess('');
    try {
      applyStatus(await vnpyPaperTradingApi.updateSettings(buildSettingsUpdate(settingsForm), {
        includeSnapshot: false,
        includeRecentTrades: false,
      }), { preserveDetails: true });
      void loadFullStatus();
      void loadTaskHealth();
      void loadTaskEvents();
      setSuccess('模拟交易设置已保存');
    } catch (err) {
      setError(toApiErrorMessage(err, '模拟交易设置保存失败'));
    } finally {
      setSaving(false);
    }
  };

  const handleSubmitOrder = async (event: React.FormEvent) => {
    event.preventDefault();
    setSubmitting(true);
    setError('');
    setSuccess('');
    setOrderResult(null);
    try {
      const result = await vnpyPaperTradingApi.submitOrder({
        symbol: orderForm.symbol.trim(),
        side: orderForm.side,
        market: orderForm.market,
        quantity: parseNumber(orderForm.quantity),
        cashAmount: parseNumber(orderForm.cashAmount),
        price: parseNumber(orderForm.price),
        note: orderForm.note.trim() || undefined,
        executionRoute: orderForm.executionRoute,
      });
      setOrderResult(result);
      if (result.accepted) {
        setSuccess(`模拟成交：${result.symbol} ${result.side} ${formatNumber(result.quantity, 0)} 股`);
        await loadStatus();
        await loadPerformance();
        await loadTradePlanRecovery();
        await loadTaskHealth();
        await loadTaskEvents();
      }
    } catch (err) {
      setError(toApiErrorMessage(err, '模拟委托提交失败'));
    } finally {
      setSubmitting(false);
    }
  };

  const handleRunAuto = async () => {
    setRunningAuto(true);
    setError('');
    setSuccess('');
    setAutoResult(null);
    try {
      applyStatus(await vnpyPaperTradingApi.updateSettings(buildSettingsUpdate(settingsForm), {
        includeSnapshot: false,
        includeRecentTrades: false,
      }), { preserveDetails: true });
      const result = await vnpyPaperTradingApi.runAutoOnce();
      setAutoResult(result);
      if (result.accepted) {
        setSuccess(`自动模拟交易完成：计划 ${result.plannedCount} 笔，成交 ${result.submittedCount} 笔，跳过 ${result.skippedCount} 笔`);
      }
      await loadAgentRuns();
      await loadStatus();
      await loadPerformance();
      await loadTradePlanRecovery();
      await loadTaskHealth();
      await loadTaskEvents();
      await loadAutoAlertTriggers();
    } catch (err) {
      setError(toApiErrorMessage(err, '自动模拟交易运行失败'));
    } finally {
      setRunningAuto(false);
    }
  };

  const handleRunDryRun = async () => {
    setRunningDryRun(true);
    setError('');
    setSuccess('');
    setAutoResult(null);
    try {
      const result = await vnpyPaperTradingApi.runAutoOnce({
        executionMode: 'dry_run',
        ignoreAutoTradeEnabled: true,
      });
      setAutoResult(result);
      if (result.accepted) {
        setSuccess(`dry-run 完成：计划 ${result.plannedCount} 笔，成交 ${result.submittedCount} 笔，跳过 ${result.skippedCount} 笔`);
      }
      await loadAgentRuns();
      await loadStatus();
      await loadPerformance();
      await loadTradePlanRecovery();
      await loadTaskHealth();
      await loadTaskEvents();
      await loadAutoAlertTriggers();
    } catch (err) {
      setError(toApiErrorMessage(err, '自动模拟交易 dry-run 失败'));
    } finally {
      setRunningDryRun(false);
    }
  };

  const handlePauseAutoTrade = async () => {
    const confirmed = window.confirm('确认暂停定时自动买入吗？暂停后后台 scheduler 会停止新增自动买入任务。');
    if (!confirmed) return;
    setPausingAuto(true);
    setError('');
    setSuccess('');
    try {
      applyStatus(await vnpyPaperTradingApi.updateSettings({ autoTradeEnabled: false }, {
        includeSnapshot: false,
        includeRecentTrades: false,
      }), { preserveDetails: true });
      setSettingsForm((prev) => ({ ...prev, autoTradeEnabled: false }));
      await loadTaskHealth();
      await loadTaskEvents();
      setSuccess('定时自动买入已暂停');
    } catch (err) {
      setError(toApiErrorMessage(err, '暂停自动买入失败'));
    } finally {
      setPausingAuto(false);
    }
  };

  const handleResumeAutoTrade = async () => {
    setResumingAuto(true);
    setError('');
    setSuccess('');
    try {
      applyStatus(await vnpyPaperTradingApi.updateSettings({ autoTradeEnabled: true }, {
        includeSnapshot: false,
        includeRecentTrades: false,
      }), { preserveDetails: true });
      setSettingsForm((prev) => ({ ...prev, autoTradeEnabled: true }));
      await loadTaskHealth();
      await loadTaskEvents();
      setSuccess('定时自动买入已恢复');
    } catch (err) {
      setError(toApiErrorMessage(err, '恢复自动买入失败'));
    } finally {
      setResumingAuto(false);
    }
  };

  const handleResetFailureFuse = async () => {
    setResettingFailureFuse(true);
    setError('');
    setSuccess('');
    try {
      applyStatus(await vnpyPaperTradingApi.resetFailureFuse(), { preserveDetails: true });
      await loadAgentRuns();
      await loadTaskHealth();
      await loadTaskEvents();
      await loadAutoAlertTriggers();
      setSuccess('连续失败熔断基线已重置');
    } catch (err) {
      setError(toApiErrorMessage(err, '连续失败熔断恢复失败'));
    } finally {
      setResettingFailureFuse(false);
    }
  };

  const handleResetAccount = async () => {
    const confirmed = window.confirm('确定要重置模拟账户？旧 vnpy_paper 账户会被归档，新账户会按初始资金重新创建。');
    if (!confirmed) return;
    setResettingAccount(true);
    setError('');
    setSuccess('');
    setOrderResult(null);
    setAutoResult(null);
    try {
      applyStatus(await vnpyPaperTradingApi.resetAccount({
        includeSnapshot: false,
        includeRecentTrades: false,
      }));
      await loadFullStatus();
      await loadPaperAccounts();
      await loadPerformance();
      await loadTradePlanRecovery();
      await loadTaskHealth();
      await loadTaskEvents();
      setSuccess('模拟账户已重置，旧账户已归档');
    } catch (err) {
      setError(toApiErrorMessage(err, '模拟账户重置失败'));
    } finally {
      setResettingAccount(false);
    }
  };

  const handleRestoreAccount = async (account: VnpyPaperAccount) => {
    const accountId = Number(account.id);
    if (!Number.isFinite(accountId) || accountId <= 0) return;
    const actionLabel = account.archived ? '恢复' : '切换到';
    const confirmed = window.confirm(`确认${actionLabel}模拟账户 #${accountId} 吗？当前账户会被归档，自动交易后续将使用该账户。`);
    if (!confirmed) return;
    setRestoringAccountId(accountId);
    setError('');
    setSuccess('');
    setOrderResult(null);
    setAutoResult(null);
    try {
      applyStatus(await vnpyPaperTradingApi.restoreAccount(accountId, {
        includeSnapshot: false,
        includeRecentTrades: false,
      }));
      await loadFullStatus();
      await loadPaperAccounts();
      await loadPerformance();
      await loadTradePlanRecovery();
      await loadTaskHealth();
      await loadTaskEvents();
      setSuccess(`模拟账户 #${accountId} 已设为当前账户`);
    } catch (err) {
      setError(toApiErrorMessage(err, '模拟账户恢复失败'));
    } finally {
      setRestoringAccountId(null);
    }
  };

  const handleCleanupArchivedAccounts = async () => {
    const archivedAccountIds = paperAccounts
      .filter((account) => account.archived && !account.isCurrent)
      .map((account) => Number(account.id))
      .filter((accountId) => Number.isFinite(accountId) && accountId > 0);
    if (archivedAccountIds.length === 0) return;
    const confirmed = window.confirm(`确认清理 ${archivedAccountIds.length} 个已归档模拟账户吗？该操作只会从模拟账户历史中隐藏旧账户，不会删除流水。`);
    if (!confirmed) return;
    setCleaningArchivedAccounts(true);
    setError('');
    setSuccess('');
    try {
      const result = await vnpyPaperTradingApi.cleanupArchivedAccounts(archivedAccountIds);
      setPaperAccounts(result.accounts.items);
      setPaperAccountsHiddenCount(Number(result.accounts.hiddenCount ?? result.hiddenCountAfter ?? 0));
      setSuccess(`已清理 ${result.cleanedAccountIds.length} 个已归档模拟账户，历史流水仍保留`);
    } catch (err) {
      setError(toApiErrorMessage(err, '已归档模拟账户清理失败'));
    } finally {
      setCleaningArchivedAccounts(false);
    }
  };

  const handleApproveTradePlan = async (planUid: string) => {
    setApprovingPlanUid(planUid);
    setError('');
    setSuccess('');
    setOrderResult(null);
    try {
      const result = await vnpyPaperTradingApi.approveTradePlan(planUid);
      setOrderResult(result);
      if (result.accepted) {
        setSuccess(`审批成交：${result.symbol} ${result.side} ${formatNumber(result.quantity, 0)} 股`);
      }
      await loadFullStatus();
      await loadAgentRuns();
      await loadPerformance();
      await loadTradePlanRecovery();
      await loadTaskHealth();
      await loadTaskEvents();
    } catch (err) {
      setError(toApiErrorMessage(err, '交易计划审批失败'));
    } finally {
      setApprovingPlanUid(null);
    }
  };

  const handleRetryTradePlan = async (planUid: string) => {
    setRetryingPlanUid(planUid);
    setError('');
    setSuccess('');
    setOrderResult(null);
    try {
      const result = await vnpyPaperTradingApi.retryTradePlan(planUid);
      setOrderResult(result);
      if (result.accepted) {
        setSuccess(`重试成交：${result.symbol} ${result.side} ${formatNumber(result.quantity, 0)} 股`);
      } else {
        setSuccess(`重试完成：${result.reason || result.status}`);
      }
      await loadFullStatus();
      await loadAgentRuns();
      await loadPerformance();
      await loadTradePlanRecovery();
      await loadTaskHealth();
      await loadTaskEvents();
      await loadAutoAlertTriggers();
    } catch (err) {
      setError(toApiErrorMessage(err, '交易计划重试失败'));
    } finally {
      setRetryingPlanUid(null);
    }
  };

  const handleCancelTradePlan = async (planUid: string) => {
    setCancellingPlanUid(planUid);
    setError('');
    setSuccess('');
    setOrderResult(null);
    try {
      const result = await vnpyPaperTradingApi.cancelTradePlan(planUid);
      setOrderResult(result);
      if (result.accepted) {
        setSuccess(`撤单请求已发送：${result.symbol || planUid}`);
      } else {
        setSuccess(`撤单未发送：${result.reason || result.status}`);
      }
      await loadFullStatus();
      await loadAgentRuns();
      await loadPerformance();
      await loadTradePlanRecovery();
      await loadTaskHealth();
      await loadTaskEvents();
      await loadAutoAlertTriggers();
    } catch (err) {
      setError(toApiErrorMessage(err, '交易计划撤单失败'));
    } finally {
      setCancellingPlanUid(null);
    }
  };

  const handleRunTradePlanRecovery = async () => {
    if (!window.confirm('运行一次交易计划恢复扫描？超时的 vn.py 计划会先对账，确认可恢复的失败计划可能会被重新提交。')) {
      return;
    }
    setTradePlanRecoveryRunning(true);
    setError('');
    setSuccess('');
    setOrderResult(null);
    try {
      const result = await vnpyPaperTradingApi.runTradePlanRecovery(5, 200);
      setSuccess(
        `恢复扫描完成：对账 ${formatNumber(result.reconciledCount, 0)}，保护 ${formatNumber(result.protectedCount, 0)}，对账异常 ${formatNumber(result.reconciliationFailedCount, 0)}，归档 ${formatNumber(result.expiredCount, 0)}，尝试 ${formatNumber(result.attemptedCount, 0)}，提交 ${formatNumber(result.submittedCount, 0)}`
      );
      await loadFullStatus();
      await loadPerformance();
      await loadTradePlanRecovery();
      await loadTaskHealth();
      await loadTaskEvents();
      await loadAutoAlertTriggers();
    } catch (err) {
      setError(toApiErrorMessage(err, '交易计划恢复扫描失败'));
    } finally {
      setTradePlanRecoveryRunning(false);
    }
  };

  const handleExportSelectedAgentRun = () => {
    if (!selectedAgentRun) return;
    exportJsonFile(`stock-selection-agent-${selectedAgentRun.runUid}.json`, selectedAgentRun);
    setSuccess(`已导出 Agent 运行记录：${selectedAgentRun.runUid}`);
  };

  const handleExportAgentRuns = async () => {
    setExportingAgentRuns(true);
    setError('');
    setSuccess('');
    try {
      const filters = buildAgentRunFilters(agentRunFilters);
      const payload = filters
        ? await vnpyPaperTradingApi.exportAgentRuns(50, true, filters)
        : await vnpyPaperTradingApi.exportAgentRuns(50, true);
      exportJsonFile('stock-selection-agent-runs.json', payload);
      setSuccess(`已导出最近 ${payload.count} 条 Agent 运行记录`);
    } catch (err) {
      setError(toApiErrorMessage(err, 'Agent 运行记录导出失败'));
    } finally {
      setExportingAgentRuns(false);
    }
  };

  const handleApplyPerformanceFilters = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setPerformanceFilters(performanceFilterForm);
    await loadPerformance(performanceFilterForm);
  };

  const handleResetPerformanceFilters = async () => {
    setPerformanceFilterForm(defaultPerformanceFilterForm);
    setPerformanceFilters(defaultPerformanceFilterForm);
    await loadPerformance(defaultPerformanceFilterForm);
  };

  const handleApplyTaskEventFilters = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    await loadTaskEvents(taskEventFilters);
  };

  const handleResetTaskEventFilters = async () => {
    setTaskEventFilters(defaultTaskEventFilterForm);
    taskEventFiltersRef.current = defaultTaskEventFilterForm;
    await loadTaskEvents(defaultTaskEventFilterForm);
  };

  return (
    <AppPage className="space-y-4">
      <section className="flex flex-col gap-4 border-b border-border/70 pb-4 lg:flex-row lg:items-end lg:justify-between">
        <div>
          <h1 className="text-2xl font-semibold text-foreground">vn.py 模拟交易</h1>
          <p className="mt-2 text-sm text-secondary-text">
            {status?.account ? `账户 ${status.account.name} · ${status.account.broker || 'vnpy_paper'}` : '本地 paper 账户待初始化'}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <span className={`rounded-full border px-3 py-1 text-xs font-semibold ${statusBadge(Boolean(status?.enabled))}`}>
            {status?.enabled ? '模拟开启' : '模拟关闭'}
          </span>
          <span className={`rounded-full border px-3 py-1 text-xs font-semibold ${statusBadge(paperAvailable)}`}>
            {paperAvailable ? '本地模拟可用' : '本地模拟不可用'}
          </span>
          <span className={`rounded-full border px-3 py-1 text-xs font-semibold ${statusBadge(Boolean(status?.vnpyAvailable))}`}>
            {status?.vnpyAvailable ? 'vn.py 环境已安装' : 'vn.py 环境未安装'}
          </span>
          <Button variant="secondary" isLoading={loading} loadingText="刷新中..." onClick={() => void loadStatus()}>
            <RefreshCw className="h-4 w-4" />
            刷新
          </Button>
          <Button
            variant="outline"
            isLoading={reconnectingGateway}
            loadingText="重连中..."
            disabled={!canReconnectGateway}
            onClick={() => void handleReconnectGateway()}
            title={canReconnectGateway ? '重新读取连接参数并尝试连接 gateway' : `当前连接状态：${vnpyGatewayConnectionStatus}`}
          >
            <RefreshCw className="h-4 w-4" />
            重连网关
          </Button>
          <Button
            variant="outline"
            isLoading={preflightingGateway}
            loadingText="预检中..."
            onClick={() => void handlePreflightGateway()}
            title="零连接检查外部 gateway 与连接参数合同"
          >
            <ShieldCheck className="h-4 w-4" />
            生产预检
          </Button>
          <Button variant="outline" isLoading={ensuring} loadingText="初始化中..." onClick={() => void handleEnsureAccount()}>
            <WalletCards className="h-4 w-4" />
            初始化账户
          </Button>
          <Button variant="outline" isLoading={resettingAccount} loadingText="重置中..." onClick={() => void handleResetAccount()}>
            <RotateCcw className="h-4 w-4" />
            重置账户
          </Button>
        </div>
      </section>

      {error ? <InlineAlert variant="danger" title="操作失败" message={error} /> : null}
      {snapshotError ? <InlineAlert variant="warning" title="快照加载失败" message={snapshotError} /> : null}
      {valuationWarning ? <InlineAlert variant="warning" title="持仓估值降级" message={valuationWarning} /> : null}
      {performanceError ? <InlineAlert variant="warning" title="绩效摘要加载失败" message={performanceError} /> : null}
      {tradePlanRecoveryError ? <InlineAlert variant="warning" title="交易计划恢复矩阵加载失败" message={tradePlanRecoveryError} /> : null}
      {taskHealthError ? <InlineAlert variant="warning" title="任务健康检查加载失败" message={taskHealthError} /> : null}
      {taskEventsError ? <InlineAlert variant="warning" title="后台任务日志加载失败" message={taskEventsError} /> : null}
      {taskEventSummaryError ? <InlineAlert variant="warning" title="后台任务趋势加载失败" message={taskEventSummaryError} /> : null}
      {taskMetricsError ? <InlineAlert variant="warning" title="后台任务长期指标加载失败" message={taskMetricsError} /> : null}
      {paperAccountsError ? <InlineAlert variant="warning" title="账户历史加载失败" message={paperAccountsError} /> : null}
      {success ? <InlineAlert variant="success" message={success} /> : null}
      {gatewayPreflight ? (
        <InlineAlert
          variant={gatewayPreflight.ok ? 'success' : 'warning'}
          title="外部 gateway 生产预检"
          message={gatewayPreflight.ok
            ? `已通过 · ${gatewayPreflight.gatewayName || '-'} · 配置键 ${gatewayPreflight.providedKeyCount}/${gatewayPreflight.defaultSettingKeyCount}`
            : `未通过：${gatewayPreflight.failures.join('、') || 'unknown'} · 配置键 ${gatewayPreflight.providedKeyCount}/${gatewayPreflight.defaultSettingKeyCount}`}
        />
      ) : null}
      <InlineAlert
        variant="warning"
        title="模拟交易"
        message="当前可用的是本地模拟账本；vn.py 环境未安装时仍可模拟成交，不会连接实盘券商或真实网关。"
      />
      {snapshotLoading ? (
        <InlineAlert
          variant="info"
          message="账户状态已加载，持仓估值和近期成交正在后台刷新。"
        />
      ) : null}
      {schedulerStatus?.lastError ? (
        <InlineAlert
          variant="warning"
          title="调度器最近错误"
          message={schedulerStatus.lastError}
        />
      ) : null}

      <section className="rounded-xl border border-border bg-card/95 px-4 py-3" data-testid="paper-availability-diagnostics">
        <div className="flex flex-col gap-1 md:flex-row md:items-center md:justify-between">
          <div>
            <h2 className="text-sm font-semibold text-foreground">可用性诊断</h2>
            <p className="mt-1 text-xs text-secondary-text">
              本地账本、自动任务、交易窗口、熔断和 vn.py bridge 的当前执行条件。
            </p>
          </div>
          <span className={`w-fit rounded-full border px-2 py-1 text-xs ${diagnosticTone(paperAvailable ? 'success' : 'danger')}`}>
            {paperAvailable ? '本地可交易' : '需处理'}
          </span>
        </div>
        <div className="mt-3 grid gap-2 md:grid-cols-2 xl:grid-cols-5">
          {availabilityDiagnostics.map((item) => (
            <div key={item.key} className="min-w-0 border-t border-border pt-2 first:border-t-0 md:border-t-0 md:border-l md:pl-3 md:first:border-l-0">
              <div className="flex items-center justify-between gap-2">
                <span className="text-xs text-secondary-text">{item.label}</span>
                <span className={`shrink-0 rounded-full border px-2 py-0.5 text-[11px] ${diagnosticTone(item.tone)}`}>
                  {item.status}
                </span>
              </div>
              <p className="mt-2 truncate text-xs text-foreground" title={item.detail}>
                {item.detail}
              </p>
            </div>
          ))}
        </div>
      </section>

      {valuationTrendWindows.length > 0 ? (
        <section className="border-y border-border py-3" data-testid="valuation-health-trends">
          <div className="flex flex-col gap-1 md:flex-row md:items-end md:justify-between">
            <div>
              <h2 className="text-sm font-semibold text-foreground">估值价格源趋势</h2>
              <p className="mt-1 text-xs text-secondary-text">
                每 15 分钟最多记录一次完整持仓快照；无历史样本时不推断为健康。
              </p>
            </div>
            <span className="text-xs text-secondary-text">
              最近观测 {formatDateTime(valuationTrendLatestObservedAt)}
            </span>
          </div>
          <div className="mt-3 grid gap-3 md:grid-cols-3">
            {valuationTrendWindows.map((item) => {
              const days = Number(item.windowDays || 0);
              const observations = Number(item.observationCount || 0);
              const healthObservations = finiteNumberOrNull(item.healthObservationCount) ?? observations;
              const emptyObservations = Number(item.emptyPositionObservationCount || 0);
              const degradedPct = finiteNumberOrNull(item.degradedPct);
              const averageCoverage = finiteNumberOrNull(item.averageCoveragePct);
              const averageFreshCoverage = finiteNumberOrNull(item.averageFreshCoveragePct);
              const weightedCoverage = finiteNumberOrNull(item.positionWeightedCoveragePct) ?? averageCoverage;
              const weightedFreshCoverage = finiteNumberOrNull(item.positionWeightedFreshCoveragePct) ?? averageFreshCoverage;
              const minimumCoverage = finiteNumberOrNull(item.minimumCoveragePct);
              const minimumFreshCoverage = finiteNumberOrNull(item.minimumFreshCoveragePct);
              const providerUsage = asRecordList(item.providerUsage)
                .slice(0, 3)
                .map((provider) => {
                  const share = finiteNumberOrNull(provider.sharePct);
                  return `${String(provider.provider || 'unknown')} ${share === null ? '-' : `${formatNumber(share, 2)}%`}`;
                })
                .join(' · ');
              return (
                <div key={days} className="min-w-0 border-l-2 border-cyan/50 pl-3">
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-sm font-semibold text-foreground">{days} 天</span>
                    <span className="text-xs text-secondary-text">
                      {formatNumber(healthObservations, 0)} 有效 / {formatNumber(observations, 0)} 总计
                    </span>
                  </div>
                  <p className="mt-2 text-xs text-secondary-text">
                    持仓加权覆盖 {weightedCoverage === null ? '-' : `${formatNumber(weightedCoverage, 2)}%`}
                    {' · '}新鲜 {weightedFreshCoverage === null ? '-' : `${formatNumber(weightedFreshCoverage, 2)}%`}
                  </p>
                  <p className="mt-1 text-xs text-secondary-text">
                    时间均值覆盖 {averageCoverage === null ? '-' : `${formatNumber(averageCoverage, 2)}%`}
                    {' · '}新鲜 {averageFreshCoverage === null ? '-' : `${formatNumber(averageFreshCoverage, 2)}%`}
                  </p>
                  <p className="mt-1 text-xs text-secondary-text">
                    最低覆盖 {minimumCoverage === null ? '-' : `${formatNumber(minimumCoverage, 2)}%`}
                    {' · '}新鲜 {minimumFreshCoverage === null ? '-' : `${formatNumber(minimumFreshCoverage, 2)}%`}
                  </p>
                  <p className={`mt-1 text-xs ${degradedPct && degradedPct > 0 ? 'text-warning' : 'text-secondary-text'}`}>
                    降级占比 {degradedPct === null ? '-' : `${formatNumber(degradedPct, 2)}%`}
                    {emptyObservations > 0 ? ` · 空仓 ${formatNumber(emptyObservations, 0)}` : ''}
                  </p>
                  <p className="mt-1 truncate text-xs text-secondary-text" title={providerUsage || '无 provider 观测'}>
                    Provider {providerUsage || '-'}
                  </p>
                </div>
              );
            })}
          </div>
        </section>
      ) : null}

      <section className="grid gap-3 md:grid-cols-3 xl:grid-cols-6">
        <div className="rounded-xl border border-border bg-card/95 px-4 py-3">
          <div className="flex items-center gap-2 text-xs text-secondary-text">
            <Activity className="h-4 w-4 text-cyan" />
            执行引擎
          </div>
          <p className="mt-2 break-all text-sm font-semibold text-foreground">{status?.engine || '-'}</p>
        </div>
        <div className="rounded-xl border border-border bg-card/95 px-4 py-3">
          <div className="flex items-center gap-2 text-xs text-secondary-text">
            <ShieldCheck className="h-4 w-4 text-cyan" />
            Runtime
          </div>
          <p className="mt-2 break-all text-sm font-semibold text-foreground">{vnpyRuntimeLabel}</p>
        </div>
        <div className="rounded-xl border border-border bg-card/95 px-4 py-3">
          <div className="flex items-center gap-2 text-xs text-secondary-text">
            <Layers3 className="h-4 w-4 text-cyan" />
            vn.py adapter
          </div>
          <p className="mt-2 break-all text-sm font-semibold text-foreground">{vnpyAdapterMode}</p>
        </div>
        <div className="rounded-xl border border-border bg-card/95 px-4 py-3">
          <div className="flex items-center gap-2 text-xs text-secondary-text">
            <ClipboardList className="h-4 w-4 text-cyan" />
            OrderRequest
          </div>
          <p className="mt-2 text-sm font-semibold text-foreground">
            {vnpyOrderRequestSupported ? '可构造' : '本地 fallback'}
          </p>
        </div>
        <div className="rounded-xl border border-border bg-card/95 px-4 py-3">
          <div className="flex items-center gap-2 text-xs text-secondary-text">
            <Ban className="h-4 w-4 text-cyan" />
            CancelRequest
          </div>
          <p className="mt-2 text-sm font-semibold text-foreground">
            {vnpyCancelRequestSupported && vnpyCancelOrderSupported ? '可撤单' : '未就绪'}
          </p>
        </div>
        <div className="rounded-xl border border-border bg-card/95 px-4 py-3">
          <div className="flex items-center gap-2 text-xs text-secondary-text">
            <SendHorizontal className="h-4 w-4 text-cyan" />
            MainEngine
          </div>
          <p className="mt-2 break-all text-sm font-semibold text-foreground">
            {vnpyBridgeAvailable ? '可提交' : vnpyBridgeMode}
          </p>
        </div>
      </section>

      {paperAccounts.length > 0 ? (
        <section className="rounded-xl border border-border bg-card/95" data-testid="paper-account-history">
          <div className="flex flex-col gap-3 border-b border-border px-4 py-3 lg:flex-row lg:items-center lg:justify-between">
            <div>
              <h2 className="text-sm font-semibold text-foreground">模拟账户历史</h2>
              <p className="mt-1 text-xs text-secondary-text">
                共 {paperAccounts.length} 个本地 paper 账户，当前 {currentPaperAccountCount} 个，已归档 {archivedPaperAccountCount} 个，已隐藏 {paperAccountsHiddenCount} 个，显示 {filteredPaperAccounts.length} 个
              </p>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <Button
                size="xsm"
                variant="danger-subtle"
                isLoading={cleaningArchivedAccounts}
                loadingText="清理中..."
                disabled={archivedPaperAccountCount === 0}
                onClick={() => void handleCleanupArchivedAccounts()}
              >
                清理已归档
              </Button>
              <div className="flex flex-wrap items-center gap-1.5" role="group" aria-label="模拟账户历史筛选">
                {paperAccountFilterOptions.map((option) => (
                <Button
                  key={option.value}
                  data-testid={`paper-account-filter-${option.value}`}
                  size="xsm"
                  variant={paperAccountFilter === option.value ? 'outline' : 'ghost'}
                  aria-pressed={paperAccountFilter === option.value}
                  onClick={() => setPaperAccountFilter(option.value)}
                >
                  {option.label} {option.count}
                </Button>
                ))}
              </div>
            </div>
          </div>
          <div className="overflow-x-auto">
            <table className="min-w-full divide-y divide-border text-left text-xs">
              <thead className="text-secondary-text">
                <tr>
                  <th className="px-4 py-2 font-medium">账户</th>
                  <th className="px-4 py-2 font-medium">状态</th>
                  <th className="px-4 py-2 font-medium">市场 / 币种</th>
                  <th className="px-4 py-2 font-medium">更新时间</th>
                  <th className="px-4 py-2 text-right font-medium">操作</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {filteredPaperAccounts.length === 0 ? (
                  <tr>
                    <td className="px-4 py-6 text-center text-secondary-text" colSpan={5}>
                      当前筛选下没有模拟账户
                    </td>
                  </tr>
                ) : filteredPaperAccounts.map((account) => (
                  <tr key={account.id} data-testid={`paper-account-row-${account.id}`}>
                    <td className="px-4 py-3">
                      <div className="font-semibold text-foreground">#{account.id} {account.name || '-'}</div>
                      <div className="mt-1 text-secondary-text">{account.broker || 'vnpy_paper'}</div>
                    </td>
                    <td className="px-4 py-3">
                      <div className="flex flex-wrap gap-1.5">
                        {account.isCurrent ? (
                          <span className="rounded-full border border-success/40 bg-success/10 px-2 py-0.5 text-success">当前</span>
                        ) : null}
                        <span className={`rounded-full border px-2 py-0.5 ${account.archived ? 'border-secondary-text/30 bg-surface text-secondary-text' : 'border-cyan/40 bg-cyan/10 text-cyan'}`}>
                          {account.archived ? '已归档' : '活跃'}
                        </span>
                      </div>
                    </td>
                    <td className="px-4 py-3 text-secondary-text">
                      {(account.market || '-').toUpperCase()} / {account.baseCurrency || '-'}
                    </td>
                    <td className="px-4 py-3 text-secondary-text">
                      {formatDateTime(account.updatedAt || account.createdAt)}
                    </td>
                    <td className="px-4 py-3 text-right">
                      {!account.isCurrent ? (
                        <Button
                          data-testid={`paper-account-restore-${account.id}`}
                          size="xsm"
                          variant="outline"
                          isLoading={restoringAccountId === Number(account.id)}
                          loadingText="处理中..."
                          onClick={() => void handleRestoreAccount(account)}
                        >
                          {account.archived ? '恢复' : '切换'}
                        </Button>
                      ) : (
                        <span className="text-xs text-secondary-text">-</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      ) : null}

      {schedulerStatus ? (
        <section className="grid gap-3 md:grid-cols-3 xl:grid-cols-6">
          <div className="rounded-xl border border-border bg-card/95 px-4 py-3">
            <p className="text-xs text-secondary-text">后台调度</p>
            <p className="mt-2 text-sm font-semibold text-foreground">
              {schedulerStatus.enabled ? '调度已启动' : '调度未启动'}
            </p>
          </div>
          <div className="rounded-xl border border-border bg-card/95 px-4 py-3">
            <p className="text-xs text-secondary-text">自动任务</p>
            <p className="mt-2 text-sm font-semibold text-foreground">
              {autoTradeTask ? (autoTradeTask.running ? '自动任务运行中' : '自动任务已注册') : '自动任务未注册'}
            </p>
            {autoTradeTask?.previousGenerationRunning ? (
              <p className="mt-1 text-xs text-secondary-text" data-testid="auto-trade-previous-generation-running">
                配置重载前任务仍在收尾
              </p>
            ) : null}
          </div>
          <div className="rounded-xl border border-border bg-card/95 px-4 py-3">
            <p className="text-xs text-secondary-text">自动重试</p>
            <p className="mt-2 text-sm font-semibold text-foreground">
              {autoRetryTask ? (autoRetryTask.running ? '重试任务运行中' : '重试任务已注册') : '重试任务未注册'}
            </p>
            {autoRetryTask?.previousGenerationRunning ? (
              <p className="mt-1 text-xs text-secondary-text" data-testid="auto-retry-previous-generation-running">
                配置重载前任务仍在收尾
              </p>
            ) : null}
          </div>
          <div className="rounded-xl border border-border bg-card/95 px-4 py-3">
            <p className="text-xs text-secondary-text">下次运行</p>
            <p className="mt-2 text-sm font-semibold text-foreground">
              {formatDateTime(nextAutoRunAt)}
            </p>
          </div>
          <div className="rounded-xl border border-border bg-card/95 px-4 py-3">
            <p className="text-xs text-secondary-text">可交易窗口</p>
            <p className="mt-2 break-words text-sm font-semibold text-foreground">
              {tradingWindowDisplay}
            </p>
            <p className="mt-1 break-words text-xs text-secondary-text">{tradingWindowMeta || '-'}</p>
          </div>
          <div className="rounded-xl border border-border bg-card/95 px-4 py-3">
            <p className="text-xs text-secondary-text">连续失败熔断</p>
            <p className={`mt-2 text-sm font-semibold ${failureFuseOpen ? 'text-danger' : 'text-foreground'}`}>
              {failureFuseDisplay}
            </p>
            {failureFuseOpen && failureFuseAutoRecoveryEnabled ? (
              <p className="mt-1 break-words text-xs text-secondary-text">
                自动恢复探测：{formatDateTime(failureFuseRecoverAt)}
              </p>
            ) : null}
            {failureFuseOpen ? (
              <Button
                className="mt-2"
                size="xsm"
                variant="outline"
                isLoading={resettingFailureFuse}
                loadingText="恢复中..."
                onClick={() => void handleResetFailureFuse()}
              >
                <RotateCcw className="h-3.5 w-3.5" />
                恢复熔断
              </Button>
            ) : null}
          </div>
          <div className="rounded-xl border border-border bg-card/95 px-4 py-3">
            <p className="text-xs text-secondary-text">最近跳过</p>
            <p className="mt-2 text-sm font-semibold text-foreground">
              {schedulerStatus.lastSkipReason || '-'}
            </p>
          </div>
        </section>
      ) : null}

      {taskHealth ? (
        <section
          className="rounded-xl border border-border bg-card/95"
          data-testid="task-health-panel"
        >
          <div className="flex flex-col gap-3 border-b border-border px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
            <div>
              <h2 className="flex items-center gap-2 text-sm font-semibold text-foreground">
                <ShieldCheck className="h-4 w-4 text-cyan" />
                任务健康检查
              </h2>
              <p className="mt-1 text-xs text-secondary-text">
                {formatDateTime(taskHealth.generatedAt)} 生成 · {taskHealth.autoExecutionMode || '-'}
              </p>
            </div>
            <Button
              size="xsm"
              variant="outline"
              isLoading={taskHealthLoading}
              loadingText="刷新中..."
              onClick={() => void loadTaskHealth()}
            >
              <RefreshCw className="h-3.5 w-3.5" />
              刷新健康
            </Button>
          </div>
          <div className="grid divide-y divide-border sm:grid-cols-2 sm:divide-x sm:divide-y-0 lg:grid-cols-4">
            <div className="px-4 py-3">
              <p className="text-xs text-secondary-text">整体状态</p>
              <p className={`mt-2 text-sm font-semibold ${taskHealthClass(taskHealth.overallHealth)}`}>
                {taskHealthLabel(taskHealth.overallHealth)}
              </p>
            </div>
            <div className="px-4 py-3">
              <p className="text-xs text-secondary-text">调度器</p>
              <p className="mt-2 text-sm font-semibold text-foreground">
                {taskHealth.schedulerEnabled
                  ? (taskHealth.schedulerLoopRunning ?? taskHealth.schedulerEnabled)
                    ? (taskHealth.schedulerRunning ? '任务执行中' : '循环运行')
                    : '已启用未运行'
                  : '未启用'}
              </p>
            </div>
            <div className="px-4 py-3">
              <p className="text-xs text-secondary-text">定时自动买入</p>
              <p className="mt-2 text-sm font-semibold text-foreground">
                {taskHealth.paperTradingEnabled && taskHealth.autoTradeEnabled ? '已启用' : '未启用'}
              </p>
            </div>
            <div className="px-4 py-3">
              <p className="text-xs text-secondary-text">需关注任务</p>
              <p className={taskHealthUnhealthyCount > 0 ? 'mt-2 text-sm font-semibold text-warning' : 'mt-2 text-sm font-semibold text-foreground'}>
                {formatNumber(taskHealthUnhealthyCount, 0)} / {formatNumber(taskHealth.summary.total ?? taskHealthItems.length, 0)}
              </p>
            </div>
          </div>
          <div className="overflow-x-auto border-t border-border">
            <table className="min-w-full divide-y divide-border text-left text-xs">
              <thead className="text-secondary-text">
                <tr>
                  <th className="px-4 py-2 font-medium">任务</th>
                  <th className="px-4 py-2 font-medium">健康</th>
                  <th className="px-4 py-2 font-medium">注册 / 运行</th>
                  <th className="px-4 py-2 font-medium">间隔 / 下次</th>
                  <th className="px-4 py-2 font-medium">最近事件</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {taskHealthItems.length === 0 ? (
                  <tr>
                    <td className="px-4 py-6 text-center text-secondary-text" colSpan={5}>
                      暂无后台任务健康信息
                    </td>
                  </tr>
                ) : taskHealthItems.map((item) => (
                  <tr key={item.name}>
                    <td className="px-4 py-3">
                      <div className="font-semibold text-foreground">{item.label || item.name}</div>
                      <div className="mt-1 font-mono text-secondary-text">{item.name}</div>
                    </td>
                    <td className="px-4 py-3">
                      <div className={`font-semibold ${taskHealthClass(item.health)}`}>
                        {taskHealthLabel(item.health)}
                      </div>
                      <div className="mt-1 break-words text-secondary-text">{item.reason || '-'}</div>
                    </td>
                    <td className="px-4 py-3 text-secondary-text">
                      <div>{item.registered ? '已注册' : '未注册'} / {item.running ? '运行中' : '空闲'}</div>
                      <div className="mt-1">{item.required ? '必需任务' : '非必需'}</div>
                    </td>
                    <td className="px-4 py-3 text-secondary-text">
                      <div>{formatIntervalSeconds(item.intervalSeconds)}</div>
                      <div className="mt-1">{formatDateTime(item.nextRunAt)}</div>
                    </td>
                    <td className="px-4 py-3">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className={`inline-flex rounded-full border px-2 py-1 ${alertStatusTone(item.lastEventStatus || '')}`}>
                          {item.lastEventStatus || '-'}
                        </span>
                        <span className="text-secondary-text">
                          {Number.isFinite(Number(item.durationSeconds))
                            ? `${formatNumber(item.durationSeconds, 2)}s`
                            : '-'}
                        </span>
                      </div>
                      <div className="mt-1 text-secondary-text">{formatDateTime(item.lastEventAt)}</div>
                      <div className="mt-1 break-words text-foreground">{item.lastEventMessage || '-'}</div>
                      <div className="mt-1 break-words text-secondary-text">
                        {formatTaskEventDetails(item.details)}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      ) : null}

      {schedulerStatus || schedulerTaskEvents.length > 0 || taskEventsLoading ? (
        <section
          className="rounded-xl border border-border bg-card/95"
          data-testid="scheduler-task-events"
        >
          <div className="flex flex-col gap-3 border-b border-border px-4 py-3 lg:flex-row lg:items-end lg:justify-between">
            <div>
              <h2 className="flex items-center gap-2 text-sm font-semibold text-foreground">
                <Activity className="h-4 w-4 text-cyan" />
                后台任务日志
              </h2>
              <p className="mt-1 text-xs text-secondary-text">
                最近 {formatNumber(schedulerTaskEvents.length, 0)} 条自动买入、自动恢复和事件监控执行结果
              </p>
            </div>
            <form
              className="grid w-full gap-2 sm:grid-cols-2 lg:max-w-3xl lg:grid-cols-[1fr_1fr_auto_auto]"
              onSubmit={handleApplyTaskEventFilters}
            >
              <label className="text-xs text-secondary-text">
                任务
                <select
                  className={`${SELECT_CLASS} mt-1`}
                  data-testid="task-event-name-filter"
                  value={taskEventFilters.name}
                  onChange={(event) => setTaskEventFilters((prev) => ({
                    ...prev,
                    name: event.target.value,
                  }))}
                >
                  <option value="">全部任务</option>
                  {taskEventNameOptions.map((name) => (
                    <option key={name} value={name}>{name}</option>
                  ))}
                </select>
              </label>
              <label className="text-xs text-secondary-text">
                状态
                <select
                  className={`${SELECT_CLASS} mt-1`}
                  data-testid="task-event-status-filter"
                  value={taskEventFilters.status}
                  onChange={(event) => setTaskEventFilters((prev) => ({
                    ...prev,
                    status: event.target.value,
                  }))}
                >
                  <option value="">全部状态</option>
                  <option value="started">started</option>
                  <option value="completed">completed</option>
                  <option value="skipped">skipped</option>
                  <option value="failed">failed</option>
                </select>
              </label>
              <Button
                className="self-end"
                data-testid="task-event-filter-apply"
                size="xsm"
                type="submit"
                variant="outline"
                isLoading={taskEventsLoading}
                loadingText="筛选中..."
              >
                <RefreshCw className="h-3.5 w-3.5" />
                应用
              </Button>
              <Button
                className="self-end"
                data-testid="task-event-filter-reset"
                size="xsm"
                type="button"
                variant="ghost"
                onClick={() => void handleResetTaskEventFilters()}
              >
                <RotateCcw className="h-3.5 w-3.5" />
                重置
              </Button>
            </form>
          </div>
          {taskEventSummary || taskEventSummaryLoading ? (
            <div
              className="border-b border-border px-4 py-3"
              data-testid="task-event-summary"
            >
              <div className="flex flex-col gap-1 sm:flex-row sm:items-center sm:justify-between">
                <h3 className="text-xs font-semibold uppercase tracking-wide text-secondary-text">
                  任务趋势
                </h3>
                <p className="text-xs text-secondary-text">
                  最近 {formatNumber(taskEventSummary?.count ?? 0, 0)} / {formatNumber(taskEventSummary?.limit ?? 100, 0)} 条持久化事件
                </p>
              </div>
              <div className="mt-3 grid gap-2 sm:grid-cols-4">
                {[
                  ['completed', taskEventStatusCounts.completed ?? 0],
                  ['skipped', taskEventStatusCounts.skipped ?? 0],
                  ['failed', taskEventStatusCounts.failed ?? 0],
                  ['started', taskEventStatusCounts.started ?? 0],
                ].map(([status, count]) => (
                  <div key={status} className="border-l border-border pl-3">
                    <div className={`text-sm font-semibold ${alertStatusTone(String(status))}`}>
                      {formatNumber(Number(count), 0)}
                    </div>
                    <div className="mt-1 text-xs text-secondary-text">{status}</div>
                  </div>
                ))}
              </div>
              {taskEventSummaryLoading ? (
                <div className="mt-3 text-xs text-secondary-text">加载任务趋势...</div>
              ) : taskEventSummaryItems.length > 0 ? (
                <div className="mt-3 overflow-x-auto">
                  <table className="min-w-full text-left text-xs">
                    <thead className="text-secondary-text">
                      <tr>
                        <th className="py-2 pr-3 font-medium">任务</th>
                        <th className="py-2 pr-3 font-medium">总数</th>
                        <th className="py-2 pr-3 font-medium">失败率</th>
                        <th className="py-2 pr-3 font-medium">失败 / 跳过</th>
                        <th className="py-2 pr-3 font-medium">平均耗时</th>
                        <th className="py-2 pr-3 font-medium">最近事件</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-border">
                      {taskEventSummaryItems.map((item) => (
                        <tr key={item.name}>
                          <td className="py-2 pr-3">
                            <div className="font-semibold text-foreground">{item.label || item.name}</div>
                            <div className="mt-1 font-mono text-secondary-text">{item.name}</div>
                          </td>
                          <td className="py-2 pr-3 text-foreground">{formatNumber(item.total, 0)}</td>
                          <td className={`py-2 pr-3 font-semibold ${item.failureRatePct > 0 ? 'text-danger' : 'text-success'}`}>
                            {formatNumber(item.failureRatePct, 2)}%
                          </td>
                          <td className="py-2 pr-3 text-secondary-text">
                            {formatNumber(item.failedCount, 0)} / {formatNumber(item.skippedCount, 0)}
                          </td>
                          <td className="py-2 pr-3 text-secondary-text">
                            {Number.isFinite(Number(item.avgDurationSeconds))
                              ? `${formatNumber(item.avgDurationSeconds, 2)}s`
                              : '-'}
                          </td>
                          <td className="py-2 pr-3">
                            <div className="flex flex-wrap items-center gap-2">
                              <span className={`inline-flex rounded-full border px-2 py-1 ${alertStatusTone(item.lastEventStatus || '')}`}>
                                {item.lastEventStatus || '-'}
                              </span>
                              <span className="text-secondary-text">{formatDateTime(item.lastEventAt)}</span>
                            </div>
                            <div className="mt-1 break-words text-secondary-text">
                              {item.lastEventMessage || '-'}
                            </div>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <div className="mt-3 text-xs text-secondary-text">暂无任务趋势数据</div>
              )}
            </div>
          ) : null}
          {taskMetrics || taskMetricsLoading ? (
            <div className="border-b border-border px-4 py-3" data-testid="task-metrics">
              <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
                <div>
                  <h3 className="text-xs font-semibold uppercase tracking-wide text-secondary-text">
                    长期稳定性
                  </h3>
                  <p className="mt-1 text-xs text-secondary-text">
                    按终态运行统计，started 事件不计入成功率分母
                  </p>
                </div>
                <div className="inline-flex w-fit border border-border bg-surface" aria-label="长期指标时间窗口">
                  {([7, 30, 90] as const).map((days) => (
                    <button
                      key={days}
                      type="button"
                      className={`h-8 min-w-12 border-r border-border px-3 text-xs font-semibold last:border-r-0 ${
                        taskMetricsDays === days ? 'bg-cyan text-background' : 'text-secondary-text hover:text-foreground'
                      }`}
                      aria-pressed={taskMetricsDays === days}
                      onClick={() => {
                        setTaskMetricsDays(days);
                        taskMetricsDaysRef.current = days;
                        void loadTaskMetrics(days);
                      }}
                    >
                      {days}天
                    </button>
                  ))}
                </div>
              </div>
              {taskMetricsLoading && !taskMetrics ? (
                <div className="mt-3 text-xs text-secondary-text">加载长期指标...</div>
              ) : taskMetrics ? (
                <>
                  {taskMetrics.truncated ? (
                    <div className="mt-3 text-xs text-warning">事件超过 5000 条，当前窗口指标为截断结果。</div>
                  ) : null}
                  <div className="mt-3 grid gap-2 sm:grid-cols-3 lg:grid-cols-6">
                    {[
                      ['终态运行', formatNumber(taskMetrics.runCount, 0), 'text-foreground'],
                      ['成功率', `${formatNumber(taskMetrics.successRatePct, 2)}%`, 'text-success'],
                      ['失败率', `${formatNumber(taskMetrics.failureRatePct, 2)}%`, taskMetrics.failedCount > 0 ? 'text-danger' : 'text-success'],
                      ['跳过率', `${formatNumber(taskMetrics.skipRatePct, 2)}%`, 'text-warning'],
                      ['平均 / P95', `${formatNumber(taskMetrics.avgDurationSeconds, 2)}s / ${formatNumber(taskMetrics.p95DurationSeconds, 2)}s`, 'text-foreground'],
                      ['连续失败', formatNumber(taskMetrics.currentFailureStreak, 0), taskMetrics.currentFailureStreak > 0 ? 'text-danger' : 'text-success'],
                    ].map(([label, value, tone]) => (
                      <div key={label} className="border-l border-border pl-3">
                        <div className={`text-sm font-semibold ${tone}`}>{value}</div>
                        <div className="mt-1 text-xs text-secondary-text">{label}</div>
                      </div>
                    ))}
                  </div>
                  {taskMetrics.items.length > 0 ? (
                    <div className="mt-3 overflow-x-auto">
                      <table className="min-w-full text-left text-xs">
                        <thead className="text-secondary-text">
                          <tr>
                            <th className="py-2 pr-3 font-medium">任务</th>
                            <th className="py-2 pr-3 font-medium">运行</th>
                            <th className="py-2 pr-3 font-medium">成功 / 失败 / 跳过</th>
                            <th className="py-2 pr-3 font-medium">平均 / P95</th>
                            <th className="py-2 pr-3 font-medium">最近运行</th>
                          </tr>
                        </thead>
                        <tbody className="divide-y divide-border">
                          {taskMetrics.items.map((item) => (
                            <tr key={item.name}>
                              <td className="py-2 pr-3">
                                <div className="font-semibold text-foreground">{item.label || item.name}</div>
                                <div className="mt-1 font-mono text-secondary-text">{item.name}</div>
                              </td>
                              <td className="py-2 pr-3 text-foreground">{formatNumber(item.runCount, 0)}</td>
                              <td className="py-2 pr-3 text-secondary-text">
                                <span className="text-success">{formatNumber(item.successRatePct, 2)}%</span>
                                {' / '}
                                <span className={item.failedCount > 0 ? 'text-danger' : 'text-success'}>{formatNumber(item.failureRatePct, 2)}%</span>
                                {' / '}{formatNumber(item.skipRatePct, 2)}%
                              </td>
                              <td className="py-2 pr-3 text-secondary-text">
                                {formatNumber(item.avgDurationSeconds, 2)}s / {formatNumber(item.p95DurationSeconds, 2)}s
                              </td>
                              <td className="py-2 pr-3 text-secondary-text">{formatDateTime(item.lastRunAt)}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  ) : (
                    <div className="mt-3 text-xs text-secondary-text">当前窗口暂无终态运行</div>
                  )}
                  {taskMetricsDaily.length > 0 ? (
                    <div className="mt-3 overflow-x-auto">
                      <div className="mb-1 text-xs font-semibold text-secondary-text">最近逐日结果</div>
                      <table className="min-w-full text-left text-xs">
                        <thead className="text-secondary-text">
                          <tr>
                            <th className="py-2 pr-3 font-medium">日期</th>
                            <th className="py-2 pr-3 font-medium">运行</th>
                            <th className="py-2 pr-3 font-medium">成功</th>
                            <th className="py-2 pr-3 font-medium">失败</th>
                            <th className="py-2 pr-3 font-medium">平均耗时</th>
                          </tr>
                        </thead>
                        <tbody className="divide-y divide-border">
                          {taskMetricsDaily.slice(-14).reverse().map((item) => (
                            <tr key={item.date}>
                              <td className="py-2 pr-3 text-foreground">{item.date}</td>
                              <td className="py-2 pr-3 text-secondary-text">{formatNumber(item.runCount, 0)}</td>
                              <td className="py-2 pr-3 text-success">{formatNumber(item.successRatePct, 2)}%</td>
                              <td className={`py-2 pr-3 ${item.failedCount > 0 ? 'text-danger' : 'text-success'}`}>
                                {formatNumber(item.failureRatePct, 2)}%
                              </td>
                              <td className="py-2 pr-3 text-secondary-text">{formatNumber(item.avgDurationSeconds, 2)}s</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  ) : null}
                </>
              ) : null}
            </div>
          ) : null}
          <div className="divide-y divide-border" data-testid="scheduler-task-event-list">
            {schedulerTaskEvents.length === 0 ? (
              <div className="px-4 py-6 text-center text-sm text-secondary-text">
                {taskEventsLoading ? '加载中...' : '暂无后台任务日志'}
              </div>
            ) : schedulerTaskEvents.slice().reverse().map((event, index) => (
              <div
                key={`${event.name}-${event.timestamp || index}-${index}`}
                className="grid gap-2 px-4 py-3 text-xs md:grid-cols-[180px_110px_130px_1fr]"
              >
                <div className="min-w-0">
                  <div className="truncate font-mono font-semibold text-foreground">{event.name}</div>
                  <div className="mt-1 text-secondary-text">{formatDateTime(event.timestamp)}</div>
                </div>
                <div>
                  <span className={`inline-flex rounded-full border px-2 py-1 ${alertStatusTone(event.status)}`}>
                    {event.status}
                  </span>
                </div>
                <div className="text-secondary-text">
                  {Number.isFinite(Number(event.durationSeconds))
                    ? `${formatNumber(event.durationSeconds, 2)}s`
                    : '-'}
                </div>
                <div className="min-w-0">
                  <div className="break-words text-foreground">{event.message || '-'}</div>
                  <div className="mt-1 break-words text-secondary-text">
                    {formatTaskEventDetails(event.details)}
                  </div>
                </div>
              </div>
            ))}
          </div>
        </section>
      ) : null}

      <section className="grid gap-3 md:grid-cols-4">
        <div className="rounded-xl border border-border bg-card/95 px-4 py-3">
          <div className="flex items-center gap-2 text-xs text-secondary-text">
            <CircleDollarSign className="h-4 w-4 text-cyan" />
            可用现金
          </div>
          <p className="mt-2 text-lg font-semibold text-foreground">
            {formatMoney(snapshotAccount?.totalCash ?? status?.snapshot?.totalCash, currency)}
          </p>
        </div>
        <div className="rounded-xl border border-border bg-card/95 px-4 py-3">
          <p className="text-xs text-secondary-text">总权益</p>
          <p className="mt-2 text-lg font-semibold text-foreground">
            {formatMoney(snapshotAccount?.totalEquity ?? status?.snapshot?.totalEquity, currency)}
          </p>
        </div>
        <div className="rounded-xl border border-border bg-card/95 px-4 py-3">
          <p className="text-xs text-secondary-text">持仓市值</p>
          <p className="mt-2 text-lg font-semibold text-foreground">
            {formatMoney(snapshotAccount?.totalMarketValue ?? status?.snapshot?.totalMarketValue, currency)}
          </p>
        </div>
        <div className="rounded-xl border border-border bg-card/95 px-4 py-3">
          <p className="text-xs text-secondary-text">浮动盈亏</p>
          <p className="mt-2 text-lg font-semibold text-foreground">
            {formatMoney(snapshotAccount?.unrealizedPnl ?? status?.snapshot?.unrealizedPnl, currency)}
          </p>
        </div>
      </section>

      <form
        className="rounded-xl border border-border bg-card/95 px-4 py-3"
        data-testid="performance-filters"
        onSubmit={handleApplyPerformanceFilters}
      >
        <div className="flex flex-col gap-3 lg:flex-row lg:items-end lg:justify-between">
          <div>
            <h2 className="text-sm font-semibold text-foreground">绩效窗口</h2>
            <p className="mt-1 text-xs text-secondary-text">{performanceWindowLabel}</p>
          </div>
          <div className="grid w-full gap-2 sm:grid-cols-2 lg:max-w-3xl lg:grid-cols-[1fr_1fr_auto_auto]">
            <label className="text-xs text-secondary-text">
              开始时间
              <input
                className={`${INPUT_CLASS} mt-1`}
                data-testid="performance-created-from"
                type="datetime-local"
                value={performanceFilterForm.createdFrom}
                onChange={(event) => setPerformanceFilterForm((prev) => ({
                  ...prev,
                  createdFrom: event.target.value,
                }))}
              />
            </label>
            <label className="text-xs text-secondary-text">
              结束时间
              <input
                className={`${INPUT_CLASS} mt-1`}
                data-testid="performance-created-to"
                type="datetime-local"
                value={performanceFilterForm.createdTo}
                onChange={(event) => setPerformanceFilterForm((prev) => ({
                  ...prev,
                  createdTo: event.target.value,
                }))}
              />
            </label>
            <Button
              className="self-end"
              type="submit"
              variant="outline"
              isLoading={performanceLoading}
              loadingText="筛选中..."
            >
              <RefreshCw className="h-4 w-4" />
              应用
            </Button>
            <Button
              className="self-end"
              type="button"
              variant="ghost"
              onClick={() => void handleResetPerformanceFilters()}
            >
              <RotateCcw className="h-4 w-4" />
              重置
            </Button>
          </div>
        </div>
      </form>

      <section className="grid gap-3 md:grid-cols-4 xl:grid-cols-6">
        <div className="rounded-xl border border-border bg-card/95 px-4 py-3">
          <p className="text-xs text-secondary-text">累计收益</p>
          <p className="mt-2 text-lg font-semibold text-foreground">
            {performanceLoading ? '加载中...' : formatMoney(performance?.totalPnl, performanceCurrency)}
          </p>
        </div>
        <div className="rounded-xl border border-border bg-card/95 px-4 py-3">
          <p className="text-xs text-secondary-text">收益率</p>
          <p className="mt-2 text-lg font-semibold text-foreground">
            {performanceLoading ? '加载中...' : formatPercent(performance?.returnPct)}
          </p>
        </div>
        <div className="rounded-xl border border-border bg-card/95 px-4 py-3">
          <p className="text-xs text-secondary-text">最大回撤</p>
          <p className="mt-2 text-lg font-semibold text-foreground">
            {performanceLoading ? '加载中...' : formatPercent(performanceRiskMetrics?.maxDrawdownPct)}
          </p>
        </div>
        <div className="rounded-xl border border-border bg-card/95 px-4 py-3">
          <p className="text-xs text-secondary-text">换手率</p>
          <p className="mt-2 text-lg font-semibold text-foreground">
            {performanceLoading ? '加载中...' : formatPercent(performanceRiskMetrics?.turnoverPct)}
          </p>
        </div>
        <div className="rounded-xl border border-border bg-card/95 px-4 py-3">
          <p className="text-xs text-secondary-text">当前仓位</p>
          <p className="mt-2 text-lg font-semibold text-foreground">
            {performanceLoading ? '加载中...' : formatPercent(performanceRiskMetrics?.currentExposurePct)}
          </p>
        </div>
        <div className="rounded-xl border border-border bg-card/95 px-4 py-3">
          <p className="text-xs text-secondary-text">成交额</p>
          <p className="mt-2 text-lg font-semibold text-foreground">
            {performanceLoading ? '加载中...' : formatMoney(performanceTradeMetrics?.grossTurnover, performanceCurrency)}
          </p>
        </div>
        <div className="rounded-xl border border-border bg-card/95 px-4 py-3">
          <p className="text-xs text-secondary-text">买 / 卖</p>
          <p className="mt-2 text-lg font-semibold text-foreground">
            {performanceLoading
              ? '加载中...'
              : `${formatNumber(performanceBuyCount, 0)} / ${formatNumber(performanceSellCount, 0)}`}
          </p>
        </div>
        <div className="rounded-xl border border-border bg-card/95 px-4 py-3">
          <p className="text-xs text-secondary-text">卖出胜率</p>
          <p className="mt-2 text-lg font-semibold text-foreground">
            {performanceLoading ? '加载中...' : formatPercent(performanceTradeMetrics?.winRatePct)}
          </p>
        </div>
        <div className="rounded-xl border border-border bg-card/95 px-4 py-3">
          <p className="text-xs text-secondary-text">Agent 运行</p>
          <p className="mt-2 text-lg font-semibold text-foreground">
            {performanceLoading ? '加载中...' : `${formatNumber(performanceRunCount, 0)} 次`}
          </p>
        </div>
        <div className="rounded-xl border border-border bg-card/95 px-4 py-3">
          <p className="text-xs text-secondary-text">成交 / 计划</p>
          <p className="mt-2 text-lg font-semibold text-foreground">
            {performanceLoading
              ? '加载中...'
              : `${formatNumber(performanceSubmitted, 0)} / ${formatNumber(performancePlanned, 0)}`}
          </p>
        </div>
        <div className="rounded-xl border border-border bg-card/95 px-4 py-3">
          <p className="text-xs text-secondary-text">主要跳过</p>
          <p className="mt-2 text-sm font-semibold text-foreground">
            {performanceLoading
              ? '加载中...'
              : topSkipReason ? `${topSkipReason.key} ${topSkipReason.count}次` : '-'}
          </p>
        </div>
        <div className="rounded-xl border border-border bg-card/95 px-4 py-3">
          <p className="text-xs text-secondary-text">主策略</p>
          <p className="mt-2 text-sm font-semibold text-foreground">
            {performanceLoading
              ? '加载中...'
              : topStrategy ? `${topStrategy.key} ${formatMoney(topStrategy.filledCashAmount, performanceCurrency)}` : '-'}
          </p>
        </div>
        <div className="rounded-xl border border-border bg-card/95 px-4 py-3">
          <p className="text-xs text-secondary-text">主行业</p>
          <p className="mt-2 text-sm font-semibold text-foreground">
            {performanceLoading
              ? '加载中...'
              : topIndustry ? `${topIndustry.key} ${formatMoney(topIndustry.filledCashAmount, performanceCurrency)}` : '-'}
          </p>
        </div>
      </section>

      {equityCurveRows.length > 0 ? (
        <section className="rounded-xl border border-border bg-card/95" data-testid="paper-equity-curve">
          <div className="flex flex-col gap-3 border-b border-border px-4 py-3 lg:flex-row lg:items-center lg:justify-between">
            <div>
              <h2 className="flex items-center gap-2 text-sm font-semibold text-foreground">
                <LineChart className="h-4 w-4 text-cyan" />
                权益曲线
              </h2>
              <p className="mt-1 text-xs text-secondary-text">
                基于当前 paper 账户快照和本地成交流水估算，用于模拟复盘
              </p>
            </div>
            <div className="grid gap-2 text-xs sm:grid-cols-3">
              <div>
                <p className="text-secondary-text">最新权益</p>
                <p className="mt-1 font-semibold text-foreground">
                  {formatMoney(latestEquityPoint?.equity, performanceCurrency)}
                </p>
              </div>
              <div>
                <p className="text-secondary-text">峰值权益</p>
                <p className="mt-1 font-semibold text-foreground">
                  {formatMoney(peakEquityPoint?.equity, performanceCurrency)}
                </p>
              </div>
              <div>
                <p className="text-secondary-text">最新收益率</p>
                <p className="mt-1 font-semibold text-foreground">
                  {formatPercent(latestEquityPoint?.returnPct)}
                </p>
              </div>
            </div>
          </div>
          <div className="h-64 min-h-[240px] px-2 py-4">
            <ResponsiveContainer width="100%" height="100%">
              <RechartsLineChart data={equityCurveRows} margin={{ top: 8, right: 24, bottom: 8, left: 8 }}>
                <CartesianGrid stroke="rgba(148, 163, 184, 0.22)" vertical={false} />
                <XAxis dataKey="name" tickLine={false} axisLine={false} tickMargin={8} />
                <YAxis
                  width={88}
                  tickLine={false}
                  axisLine={false}
                  tickFormatter={(value) => formatMoney(value, performanceCurrency)}
                />
                <Tooltip
                  formatter={(value) => [formatMoney(value, performanceCurrency), '权益']}
                  labelFormatter={(label) => `日期 ${label}`}
                />
                <Line
                  type="monotone"
                  dataKey="equity"
                  name="权益"
                  stroke="#22d3ee"
                  strokeWidth={2}
                  dot={{ r: 2 }}
                  activeDot={{ r: 4 }}
                  isAnimationActive={false}
                />
              </RechartsLineChart>
            </ResponsiveContainer>
          </div>
        </section>
      ) : null}

      {dailyReturnRows.length > 0 ? (
        <section className="rounded-xl border border-border bg-card/95" data-testid="paper-daily-returns">
          <div className="flex flex-col gap-1 border-b border-border px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
            <div>
              <h2 className="text-sm font-semibold text-foreground">日度收益</h2>
              <p className="mt-1 text-xs text-secondary-text">按每日最后权益聚合，最近 {formatNumber(dailyReturnRows.length, 0)} 条</p>
            </div>
            <span className="text-xs text-secondary-text">非完整历史回测</span>
          </div>
          <div className="overflow-x-auto">
            <table className="min-w-full divide-y divide-border text-left text-xs">
              <thead className="text-secondary-text">
                <tr>
                  <th className="px-4 py-2 font-medium">日期</th>
                  <th className="px-4 py-2 text-right font-medium">权益</th>
                  <th className="px-4 py-2 text-right font-medium">日盈亏</th>
                  <th className="px-4 py-2 text-right font-medium">日收益率</th>
                  <th className="px-4 py-2 text-right font-medium">累计收益率</th>
                  <th className="px-4 py-2 text-right font-medium">回撤</th>
                  <th className="px-4 py-2 text-right font-medium">交易</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {dailyReturnRows.map((row) => (
                  <tr key={row.date}>
                    <td className="px-4 py-3 font-mono text-secondary-text">{row.date}</td>
                    <td className="px-4 py-3 text-right font-semibold text-foreground">
                      {row.equity === null ? '-' : formatMoney(row.equity, performanceCurrency)}
                    </td>
                    <td className="px-4 py-3 text-right text-secondary-text">
                      {row.dailyPnl === null ? '-' : formatMoney(row.dailyPnl, performanceCurrency)}
                    </td>
                    <td className="px-4 py-3 text-right text-secondary-text">
                      {row.dailyReturnPct === null ? '-' : formatPercent(row.dailyReturnPct)}
                    </td>
                    <td className="px-4 py-3 text-right text-secondary-text">
                      {row.cumulativeReturnPct === null ? '-' : formatPercent(row.cumulativeReturnPct)}
                    </td>
                    <td className="px-4 py-3 text-right text-secondary-text">
                      {row.drawdownPct === null ? '-' : formatPercent(row.drawdownPct)}
                    </td>
                    <td className="px-4 py-3 text-right text-secondary-text">{formatNumber(row.tradeCount, 0)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      ) : null}

      {monthlyReturnRows.length > 0 ? (
        <section className="rounded-xl border border-border bg-card/95" data-testid="paper-monthly-returns">
          <div className="flex flex-col gap-1 border-b border-border px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
            <div>
              <h2 className="text-sm font-semibold text-foreground">月度收益</h2>
              <p className="mt-1 text-xs text-secondary-text">按月末权益聚合，最近 {formatNumber(monthlyReturnRows.length, 0)} 个月</p>
            </div>
            <span className="text-xs text-secondary-text">paper 账本复盘</span>
          </div>
          <div className="overflow-x-auto">
            <table className="min-w-full divide-y divide-border text-left text-xs">
              <thead className="text-secondary-text">
                <tr>
                  <th className="px-4 py-2 font-medium">月份</th>
                  <th className="px-4 py-2 text-right font-medium">月初权益</th>
                  <th className="px-4 py-2 text-right font-medium">月末权益</th>
                  <th className="px-4 py-2 text-right font-medium">月盈亏</th>
                  <th className="px-4 py-2 text-right font-medium">月收益率</th>
                  <th className="px-4 py-2 text-right font-medium">累计收益率</th>
                  <th className="px-4 py-2 text-right font-medium">回撤</th>
                  <th className="px-4 py-2 text-right font-medium">交易</th>
                  <th className="px-4 py-2 text-right font-medium">涨/跌日</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {monthlyReturnRows.map((row) => (
                  <tr key={row.month}>
                    <td className="px-4 py-3 font-mono text-secondary-text">{row.month}</td>
                    <td className="px-4 py-3 text-right text-secondary-text">
                      {row.startEquity === null ? '-' : formatMoney(row.startEquity, performanceCurrency)}
                    </td>
                    <td className="px-4 py-3 text-right font-semibold text-foreground">
                      {row.endEquity === null ? '-' : formatMoney(row.endEquity, performanceCurrency)}
                    </td>
                    <td className="px-4 py-3 text-right text-secondary-text">
                      {row.monthlyPnl === null ? '-' : formatMoney(row.monthlyPnl, performanceCurrency)}
                    </td>
                    <td className="px-4 py-3 text-right text-secondary-text">
                      {row.monthlyReturnPct === null ? '-' : formatPercent(row.monthlyReturnPct)}
                    </td>
                    <td className="px-4 py-3 text-right text-secondary-text">
                      {row.cumulativeReturnPct === null ? '-' : formatPercent(row.cumulativeReturnPct)}
                    </td>
                    <td className="px-4 py-3 text-right text-secondary-text">
                      {row.drawdownPct === null ? '-' : formatPercent(row.drawdownPct)}
                    </td>
                    <td className="px-4 py-3 text-right text-secondary-text">{formatNumber(row.tradeCount, 0)}</td>
                    <td className="px-4 py-3 text-right text-secondary-text">
                      {formatNumber(row.positiveDays, 0)} / {formatNumber(row.negativeDays, 0)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      ) : null}

      <section className="grid gap-4 lg:grid-cols-2">
        <div className="rounded-xl border border-border bg-card/95 p-4">
          <div className="mb-3 flex items-center gap-2 text-sm font-semibold text-foreground">
            <BarChart3 className="h-4 w-4 text-cyan" />
            策略归因
          </div>
          <div className="divide-y divide-border/70">
            {strategyAttribution.length > 0 ? strategyAttribution.slice(0, 5).map((item) => (
              <div key={item.key} className="grid grid-cols-[1fr_auto] gap-3 py-2 text-sm">
                <div>
                  <p className="font-semibold text-foreground">{item.key}</p>
                  <p className="text-xs text-secondary-text">
                    运行 {formatNumber(item.runCount, 0)} 次 · 成交 {formatNumber(item.filledPlanCount, 0)} 笔
                  </p>
                </div>
                <div className="text-right">
                  <p className="font-semibold text-foreground">{formatMoney(item.filledCashAmount, performanceCurrency)}</p>
                  <p className="text-xs text-secondary-text">{formatPercent(item.fillRatePct)}</p>
                </div>
              </div>
            )) : (
              <p className="py-2 text-sm text-secondary-text">-</p>
            )}
          </div>
        </div>
        <div className="rounded-xl border border-border bg-card/95 p-4">
          <div className="mb-3 flex items-center gap-2 text-sm font-semibold text-foreground">
            <LineChart className="h-4 w-4 text-cyan" />
            行业归因
          </div>
          <div className="divide-y divide-border/70">
            {industryAttribution.length > 0 ? industryAttribution.slice(0, 5).map((item) => (
              <div key={item.key} className="grid grid-cols-[1fr_auto] gap-3 py-2 text-sm">
                <div>
                  <p className="font-semibold text-foreground">{item.key}</p>
                  <p className="text-xs text-secondary-text">
                    决策 {formatNumber(item.decisionCount, 0)} 条 · 成交 {formatNumber(item.filledCount, 0)} 笔
                  </p>
                </div>
                <div className="text-right">
                  <p className="font-semibold text-foreground">{formatMoney(item.filledCashAmount, performanceCurrency)}</p>
                  <p className="text-xs text-secondary-text">{formatPercent(item.fillRatePct)}</p>
                </div>
              </div>
            )) : (
              <p className="py-2 text-sm text-secondary-text">-</p>
            )}
          </div>
        </div>
      </section>

      {performanceMatrixRows.length > 0 ? (
        <section className="rounded-xl border border-border bg-card/95" data-testid="paper-performance-matrix">
          <div className="flex flex-col gap-1 border-b border-border px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
            <div>
              <h2 className="text-sm font-semibold text-foreground">绩效矩阵</h2>
              <p className="mt-1 text-xs text-secondary-text">
                {performanceWindowLabel} 的策略与行业归因
              </p>
            </div>
            <span className="text-xs text-secondary-text">run_limit={formatNumber(performance?.runWindow?.limit ?? 0, 0)}</span>
          </div>
          <div className="overflow-x-auto">
            <table className="min-w-full divide-y divide-border text-left text-xs">
              <thead className="text-secondary-text">
                <tr>
                  <th className="px-4 py-2 font-medium">维度</th>
                  <th className="px-4 py-2 font-medium">名称</th>
                  <th className="px-4 py-2 text-right font-medium">样本</th>
                  <th className="px-4 py-2 text-right font-medium">计划</th>
                  <th className="px-4 py-2 text-right font-medium">成交</th>
                  <th className="px-4 py-2 text-right font-medium">跳过</th>
                  <th className="px-4 py-2 text-right font-medium">成交金额</th>
                  <th className="px-4 py-2 text-right font-medium">填充率</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {performanceMatrixRows.map((row) => (
                  <tr key={row.id}>
                    <td className="px-4 py-3 text-secondary-text">{row.dimension}</td>
                    <td className="px-4 py-3 font-semibold text-foreground">{row.name}</td>
                    <td className="px-4 py-3 text-right text-secondary-text">{formatNumber(row.sampleCount, 0)}</td>
                    <td className="px-4 py-3 text-right text-secondary-text">
                      {row.plannedCount == null ? '-' : formatNumber(row.plannedCount, 0)}
                    </td>
                    <td className="px-4 py-3 text-right text-secondary-text">{formatNumber(row.filledCount, 0)}</td>
                    <td className="px-4 py-3 text-right text-secondary-text">{formatNumber(row.skippedCount, 0)}</td>
                    <td className="px-4 py-3 text-right font-semibold text-foreground">
                      {formatMoney(row.filledCashAmount, performanceCurrency)}
                    </td>
                    <td className="px-4 py-3 text-right text-secondary-text">{formatPercent(row.fillRatePct)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      ) : null}

      <section className="rounded-xl border border-border bg-card/95" data-testid="trade-plan-recovery-matrix">
        <div className="flex flex-col gap-3 border-b border-border px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <h2 className="flex items-center gap-2 text-sm font-semibold text-foreground">
              <ClipboardList className="h-4 w-4 text-cyan" />
              交易计划恢复矩阵
            </h2>
            <p className="mt-1 text-xs text-secondary-text">
              最近 {formatNumber(tradePlanRecovery?.scannedCount ?? 0, 0)} 条非终态计划
            </p>
          </div>
          <div className="flex flex-wrap gap-2">
            <Button
              size="xsm"
              variant="outline"
              isLoading={tradePlanRecoveryRunning}
              loadingText="扫描中..."
              onClick={() => void handleRunTradePlanRecovery()}
            >
              <RefreshCw className="h-3.5 w-3.5" />
              运行恢复扫描
            </Button>
            <Button
              size="xsm"
              variant="outline"
              isLoading={tradePlanRecoveryLoading}
              loadingText="刷新中..."
              onClick={() => void loadTradePlanRecovery()}
            >
              <RefreshCw className="h-3.5 w-3.5" />
              刷新矩阵
            </Button>
          </div>
        </div>
        <div className="grid gap-3 p-4 md:grid-cols-3 xl:grid-cols-6">
          <div className="rounded-lg border border-border bg-surface px-3 py-2">
            <p className="text-xs text-secondary-text">活跃计划</p>
            <p className="mt-1 text-lg font-semibold text-foreground">
              {tradePlanRecoveryLoading ? '加载中...' : formatNumber(tradePlanRecoveryActiveCount, 0)}
            </p>
          </div>
          <div className="rounded-lg border border-border bg-surface px-3 py-2">
            <p className="text-xs text-secondary-text">疑似卡住</p>
            <p className="mt-1 text-lg font-semibold text-warning">
              {tradePlanRecoveryLoading ? '加载中...' : formatNumber(tradePlanRecovery?.staleActiveCount ?? 0, 0)}
            </p>
          </div>
          <div className="rounded-lg border border-border bg-surface px-3 py-2">
            <p className="text-xs text-secondary-text">可撤单</p>
            <p className="mt-1 text-lg font-semibold text-foreground">
              {tradePlanRecoveryLoading ? '加载中...' : formatNumber(tradePlanRecovery?.cancellableCount ?? 0, 0)}
            </p>
          </div>
          <div className="rounded-lg border border-border bg-surface px-3 py-2">
            <p className="text-xs text-secondary-text">可重试</p>
            <p className="mt-1 text-lg font-semibold text-foreground">
              {tradePlanRecoveryLoading ? '加载中...' : formatNumber(tradePlanRecovery?.retryDueCount ?? 0, 0)}
            </p>
          </div>
          <div className="rounded-lg border border-border bg-surface px-3 py-2">
            <p className="text-xs text-secondary-text">冷却中</p>
            <p className="mt-1 text-lg font-semibold text-foreground">
              {tradePlanRecoveryLoading ? '加载中...' : formatNumber(tradePlanRecovery?.retryCooldownCount ?? 0, 0)}
            </p>
          </div>
          <div className="rounded-lg border border-border bg-surface px-3 py-2">
            <p className="text-xs text-secondary-text">重试超限</p>
            <p className="mt-1 text-lg font-semibold text-foreground">
              {tradePlanRecoveryLoading ? '加载中...' : formatNumber(tradePlanRecovery?.retryLimitCount ?? 0, 0)}
            </p>
          </div>
        </div>
        {tradePlanRecoveryFocusRows.length > 0 ? (
          <div className="overflow-x-auto border-t border-border">
            <table className="min-w-full divide-y divide-border text-left text-xs">
              <thead className="text-secondary-text">
                <tr>
                  <th className="px-4 py-2 font-medium">计划</th>
                  <th className="px-4 py-2 font-medium">股票</th>
                  <th className="px-4 py-2 font-medium">状态</th>
                  <th className="px-4 py-2 font-medium">恢复状态</th>
                  <th className="px-4 py-2 text-right font-medium">年龄</th>
                  <th className="px-4 py-2 text-right font-medium">计划金额</th>
                  <th className="px-4 py-2 font-medium">更新时间</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {tradePlanRecoveryFocusRows.map((item) => (
                  <tr key={item.planUid || `${item.symbol}-${item.updatedAt}`}>
                    <td className="px-4 py-3 font-mono text-secondary-text">{item.planUid || '-'}</td>
                    <td className="px-4 py-3">
                      <div className="font-semibold text-foreground">{item.symbol || '-'}</div>
                      <div className="mt-1 text-secondary-text">{item.side || '-'} / {item.executionMode || '-'}</div>
                    </td>
                    <td className="px-4 py-3 text-secondary-text">{item.status || '-'}</td>
                    <td className="px-4 py-3">
                      <div className={item.staleActive || item.retryDue ? 'font-semibold text-warning' : 'text-secondary-text'}>
                        {item.recoveryState || '-'}
                      </div>
                      <div className="mt-1 text-secondary-text">
                        {item.staleReason || item.retryBlockReason || item.skipReason || '-'}
                      </div>
                    </td>
                    <td className="px-4 py-3 text-right text-secondary-text">
                      {formatNumber(item.ageMinutes, 1)} 分钟
                    </td>
                    <td className="px-4 py-3 text-right text-secondary-text">
                      {formatMoney(item.plannedCashAmount, performanceCurrency)}
                    </td>
                    <td className="px-4 py-3 text-secondary-text">{formatDateTime(item.updatedAt)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="border-t border-border px-4 py-6 text-center text-sm text-secondary-text">
            暂无需要关注的非终态交易计划
          </div>
        )}
      </section>

      <section className="grid gap-4 xl:grid-cols-[1.15fr_0.85fr]">
        <form className="rounded-xl border border-border bg-card/95 p-4" onSubmit={handleSaveSettings}>
          <div className="mb-4 flex items-center gap-2 text-sm font-semibold text-foreground">
            <ShieldCheck className="h-4 w-4 text-cyan" />
            自动模拟交易
          </div>
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
            <label className="flex items-center gap-2 text-sm text-foreground">
              <input
                type="checkbox"
                className={CHECKBOX_CLASS}
                checked={settingsForm.enabled}
                onChange={(event) => setSettingsForm((prev) => ({ ...prev, enabled: event.target.checked }))}
              />
              启用模拟交易
            </label>
            <label className="flex items-center gap-2 text-sm text-foreground">
              <input
                type="checkbox"
                className={CHECKBOX_CLASS}
                checked={settingsForm.autoTradeEnabled}
                onChange={(event) => setSettingsForm((prev) => ({ ...prev, autoTradeEnabled: event.target.checked }))}
              />
              定时自动买入
            </label>
            <label className="flex items-center gap-2 text-sm text-foreground">
              <input
                type="checkbox"
                className={CHECKBOX_CLASS}
                checked={settingsForm.autoSkipExistingPositions}
                onChange={(event) => setSettingsForm((prev) => ({
                  ...prev,
                  autoSkipExistingPositions: event.target.checked,
                }))}
              />
              跳过已有持仓
            </label>
            <label className="flex items-center gap-2 text-sm text-foreground">
              <input
                type="checkbox"
                className={CHECKBOX_CLASS}
                checked={settingsForm.autoTradeTimeGateEnabled}
                onChange={(event) => setSettingsForm((prev) => ({
                  ...prev,
                  autoTradeTimeGateEnabled: event.target.checked,
                }))}
              />
              限制交易时段
            </label>
            <label className="flex items-center gap-2 text-sm text-foreground">
              <input
                type="checkbox"
                className={CHECKBOX_CLASS}
                checked={settingsForm.autoSellEnabled}
                onChange={(event) => setSettingsForm((prev) => ({
                  ...prev,
                  autoSellEnabled: event.target.checked,
                }))}
              />
              自动卖出风控
            </label>
            <label className="flex items-center gap-2 text-sm text-foreground">
              <input
                type="checkbox"
                className={CHECKBOX_CLASS}
                checked={settingsForm.autoSignalExitEnabled}
                onChange={(event) => setSettingsForm((prev) => ({
                  ...prev,
                  autoSignalExitEnabled: event.target.checked,
                }))}
              />
              信号失效卖出
            </label>
            <label className="flex items-center gap-2 text-sm text-foreground">
              <input
                type="checkbox"
                className={CHECKBOX_CLASS}
                checked={settingsForm.autoRebalanceEnabled}
                onChange={(event) => setSettingsForm((prev) => ({
                  ...prev,
                  autoRebalanceEnabled: event.target.checked,
                }))}
              />
              组合再平衡
            </label>
            <label className="flex items-center gap-2 text-sm text-foreground">
              <input
                type="checkbox"
                className={CHECKBOX_CLASS}
                checked={settingsForm.autoScoreWeightedAllocationEnabled}
                onChange={(event) => setSettingsForm((prev) => ({
                  ...prev,
                  autoScoreWeightedAllocationEnabled: event.target.checked,
                }))}
              />
              评分加权分配
            </label>
            <label className="flex items-center gap-2 text-sm text-foreground">
              <input
                type="checkbox"
                className={CHECKBOX_CLASS}
                checked={settingsForm.autoLlmPlanEnabled}
                onChange={(event) => setSettingsForm((prev) => ({
                  ...prev,
                  autoLlmPlanEnabled: event.target.checked,
                }))}
              />
              LLM 动态计划
            </label>
            <label className="flex items-center gap-2 text-sm text-foreground">
              <input
                type="checkbox"
                className={CHECKBOX_CLASS}
                checked={settingsForm.autoLlmReviewEnabled}
                onChange={(event) => setSettingsForm((prev) => ({
                  ...prev,
                  autoLlmReviewEnabled: event.target.checked,
                }))}
              />
              LLM 买入复核
            </label>
            <label className="flex items-center gap-2 text-sm text-foreground">
              <input
                type="checkbox"
                className={CHECKBOX_CLASS}
                checked={settingsForm.autoExcludeSt}
                onChange={(event) => setSettingsForm((prev) => ({
                  ...prev,
                  autoExcludeSt: event.target.checked,
                }))}
              />
              过滤 ST/退市
            </label>
            <label className="flex items-center gap-2 text-sm text-foreground">
              <input
                type="checkbox"
                className={CHECKBOX_CLASS}
                checked={settingsForm.autoExcludeSuspended}
                onChange={(event) => setSettingsForm((prev) => ({
                  ...prev,
                  autoExcludeSuspended: event.target.checked,
                }))}
              />
              过滤停牌
            </label>
            <label className="flex items-center gap-2 text-sm text-foreground">
              <input
                type="checkbox"
                className={CHECKBOX_CLASS}
                checked={settingsForm.autoExcludePriceLimit}
                onChange={(event) => setSettingsForm((prev) => ({
                  ...prev,
                  autoExcludePriceLimit: event.target.checked,
                }))}
              />
              过滤涨跌停
            </label>
            <label className="flex items-center gap-2 text-sm text-foreground">
              <input
                type="checkbox"
                className={CHECKBOX_CLASS}
                checked={settingsForm.autoMarketLightGateEnabled}
                onChange={(event) => setSettingsForm((prev) => ({
                  ...prev,
                  autoMarketLightGateEnabled: event.target.checked,
                }))}
              />
              大盘红绿灯风控
            </label>
            <label className="flex items-center gap-2 text-sm text-foreground">
              <input
                type="checkbox"
                className={CHECKBOX_CLASS}
                checked={settingsForm.autoMarketBreadthGateEnabled}
                onChange={(event) => setSettingsForm((prev) => ({
                  ...prev,
                  autoMarketBreadthGateEnabled: event.target.checked,
                }))}
              />
              市场宽度风控
            </label>
            <label className="flex items-center gap-2 text-sm text-foreground">
              <input
                type="checkbox"
                className={CHECKBOX_CLASS}
                checked={settingsForm.autoHotspotRetreatGateEnabled}
                onChange={(event) => setSettingsForm((prev) => ({
                  ...prev,
                  autoHotspotRetreatGateEnabled: event.target.checked,
                }))}
              />
              热点退潮风控
            </label>
            <label className="flex items-center gap-2 text-sm text-foreground">
              <input
                type="checkbox"
                className={CHECKBOX_CLASS}
                checked={settingsForm.autoIntradayMarketGateEnabled}
                onChange={(event) => setSettingsForm((prev) => ({
                  ...prev,
                  autoIntradayMarketGateEnabled: event.target.checked,
                }))}
              />
              盘中指数与实时宽度风控
            </label>
            <label className="flex items-center gap-2 text-sm text-foreground">
              <input
                type="checkbox"
                className={CHECKBOX_CLASS}
                disabled={!settingsForm.autoIntradayMarketGateEnabled}
                checked={settingsForm.autoIntradayRequireProviderTimestamp}
                onChange={(event) => setSettingsForm((prev) => ({
                  ...prev,
                  autoIntradayRequireProviderTimestamp: event.target.checked,
                }))}
              />
              要求可验证行情时间
            </label>
            <label className="flex items-center gap-2 text-sm text-foreground">
              <input
                type="checkbox"
                className={CHECKBOX_CLASS}
                checked={settingsForm.autoCrossMarketGateEnabled}
                onChange={(event) => setSettingsForm((prev) => ({
                  ...prev,
                  autoCrossMarketGateEnabled: event.target.checked,
                }))}
              />
              跨市场联动风控
            </label>
            <label className="flex items-center gap-2 text-sm text-foreground">
              <input
                type="checkbox"
                className={CHECKBOX_CLASS}
                checked={settingsForm.autoFailureFuseEnabled}
                onChange={(event) => setSettingsForm((prev) => ({
                  ...prev,
                  autoFailureFuseEnabled: event.target.checked,
                }))}
              />
              连续失败熔断
            </label>
            <label className="flex items-center gap-2 text-sm text-foreground">
              <input
                type="checkbox"
                className={CHECKBOX_CLASS}
                checked={settingsForm.autoFailureFuseAutoRecoveryEnabled}
                disabled={!settingsForm.autoFailureFuseEnabled}
                onChange={(event) => setSettingsForm((prev) => ({
                  ...prev,
                  autoFailureFuseAutoRecoveryEnabled: event.target.checked,
                }))}
              />
              熔断冷却后自动恢复
            </label>
            <label className="flex items-center gap-2 text-sm text-foreground">
              <input
                type="checkbox"
                className={CHECKBOX_CLASS}
                checked={settingsForm.autoCrossRunQualityGateEnabled}
                onChange={(event) => setSettingsForm((prev) => ({
                  ...prev,
                  autoCrossRunQualityGateEnabled: event.target.checked,
                }))}
              />
              跨运行前瞻门禁
            </label>
            <label className="space-y-1 text-xs text-secondary-text">
              初始资金
              <input
                className={INPUT_CLASS}
                type="number"
                min={1}
                step="1000"
                value={settingsForm.initialCash}
                onChange={(event) => setSettingsForm((prev) => ({ ...prev, initialCash: event.target.value }))}
              />
            </label>
            <label className="space-y-1 text-xs text-secondary-text">
              选股策略
              <input
                className={INPUT_CLASS}
                value={settingsForm.autoStrategy}
                onChange={(event) => setSettingsForm((prev) => ({ ...prev, autoStrategy: event.target.value }))}
              />
            </label>
            <label className="space-y-1 text-xs text-secondary-text">
              市场
              <select
                className={SELECT_CLASS}
                value={settingsForm.autoMarket}
                onChange={(event) => setSettingsForm((prev) => ({
                  ...prev,
                  autoMarket: event.target.value as VnpyPaperMarket,
                }))}
              >
                {markets.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
              </select>
            </label>
            <label className="space-y-1 text-xs text-secondary-text">
              执行模式
              <select
                className={SELECT_CLASS}
                value={settingsForm.autoExecutionMode}
                onChange={(event) => setSettingsForm((prev) => ({
                  ...prev,
                  autoExecutionMode: event.target.value as VnpyPaperExecutionMode,
                }))}
              >
                <option value="paper">模拟成交</option>
                <option value="vnpy_paper">vn.py 提交</option>
                <option value="manual_approval">生成计划待审批</option>
                <option value="dry_run">只生成计划</option>
              </select>
            </label>
            <label className="space-y-1 text-xs text-secondary-text">
              vn.py gateway
              <input
                className={INPUT_CLASS}
                placeholder="例如 SIM"
                value={settingsForm.vnpyGatewayName}
                onChange={(event) => setSettingsForm((prev) => ({ ...prev, vnpyGatewayName: event.target.value }))}
              />
            </label>
            <label className="space-y-1 text-xs text-secondary-text">
              候选数
              <input
                className={INPUT_CLASS}
                type="number"
                min={1}
                max={50}
                value={settingsForm.autoMaxResults}
                onChange={(event) => setSettingsForm((prev) => ({ ...prev, autoMaxResults: event.target.value }))}
              />
            </label>
            <label className="space-y-1 text-xs text-secondary-text">
              最大持仓数
              <input
                className={INPUT_CLASS}
                type="number"
                min={1}
                max={200}
                value={settingsForm.autoMaxPositions}
                onChange={(event) => setSettingsForm((prev) => ({ ...prev, autoMaxPositions: event.target.value }))}
              />
            </label>
            <label className="space-y-1 text-xs text-secondary-text">
              单票金额上限（{currency}）
              <input
                className={INPUT_CLASS}
                type="number"
                min={1}
                step="100"
                value={settingsForm.autoMaxSinglePositionValue}
                onChange={(event) => setSettingsForm((prev) => ({
                  ...prev,
                  autoMaxSinglePositionValue: event.target.value,
                }))}
                placeholder="可留空"
              />
            </label>
            <label className="space-y-1 text-xs text-secondary-text">
              总持仓金额上限（{currency}）
              <input
                className={INPUT_CLASS}
                type="number"
                min={1}
                step="100"
                value={settingsForm.autoMaxTotalPositionValue}
                onChange={(event) => setSettingsForm((prev) => ({
                  ...prev,
                  autoMaxTotalPositionValue: event.target.value,
                }))}
                placeholder="可留空"
              />
            </label>
            <label className="space-y-1 text-xs text-secondary-text">
              总仓位%
              <input
                className={INPUT_CLASS}
                type="number"
                min={0}
                max={100}
                step="0.1"
                value={settingsForm.autoMaxTotalPositionPct}
                onChange={(event) => setSettingsForm((prev) => ({
                  ...prev,
                  autoMaxTotalPositionPct: event.target.value,
                }))}
                placeholder="可留空"
              />
            </label>
            <label className="space-y-1 text-xs text-secondary-text">
              行业金额上限（{currency}）
              <input
                className={INPUT_CLASS}
                type="number"
                min={1}
                step="100"
                value={settingsForm.autoMaxIndustryPositionValue}
                onChange={(event) => setSettingsForm((prev) => ({
                  ...prev,
                  autoMaxIndustryPositionValue: event.target.value,
                }))}
                placeholder="可留空"
              />
            </label>
            <label className="space-y-1 text-xs text-secondary-text">
              行业仓位%
              <input
                className={INPUT_CLASS}
                type="number"
                min={0}
                max={100}
                step="0.1"
                value={settingsForm.autoMaxIndustryPositionPct}
                onChange={(event) => setSettingsForm((prev) => ({
                  ...prev,
                  autoMaxIndustryPositionPct: event.target.value,
                }))}
                placeholder="可留空"
              />
            </label>
            <label className="space-y-1 text-xs text-secondary-text">
              每日买入上限
              <input
                className={INPUT_CLASS}
                type="number"
                min={1}
                max={200}
                value={settingsForm.autoDailyMaxOrders}
                onChange={(event) => setSettingsForm((prev) => ({ ...prev, autoDailyMaxOrders: event.target.value }))}
                placeholder="可留空"
              />
            </label>
            <label className="space-y-1 text-xs text-secondary-text">
              止损%
              <input
                className={INPUT_CLASS}
                type="number"
                min={0}
                max={1000}
                step="0.1"
                value={settingsForm.autoStopLossPct}
                onChange={(event) => setSettingsForm((prev) => ({ ...prev, autoStopLossPct: event.target.value }))}
                placeholder="可留空"
              />
            </label>
            <label className="space-y-1 text-xs text-secondary-text">
              止盈%
              <input
                className={INPUT_CLASS}
                type="number"
                min={0}
                max={1000}
                step="0.1"
                value={settingsForm.autoTakeProfitPct}
                onChange={(event) => setSettingsForm((prev) => ({ ...prev, autoTakeProfitPct: event.target.value }))}
                placeholder="可留空"
              />
            </label>
            <label className="space-y-1 text-xs text-secondary-text">
              移动止损%
              <input
                className={INPUT_CLASS}
                type="number"
                min={0}
                max={100}
                step="0.1"
                value={settingsForm.autoTrailingStopPct}
                onChange={(event) => setSettingsForm((prev) => ({
                  ...prev,
                  autoTrailingStopPct: event.target.value,
                }))}
                placeholder="可留空"
              />
            </label>
            <label className="space-y-1 text-xs text-secondary-text">
              最大持仓天数
              <input
                className={INPUT_CLASS}
                type="number"
                min={1}
                max={3650}
                value={settingsForm.autoMaxHoldingDays}
                onChange={(event) => setSettingsForm((prev) => ({ ...prev, autoMaxHoldingDays: event.target.value }))}
                placeholder="可留空"
              />
            </label>
            <label className="space-y-1 text-xs text-secondary-text">
              未走强天数
              <input
                className={INPUT_CLASS}
                type="number"
                min={1}
                max={3650}
                value={settingsForm.autoNoProgressDays}
                onChange={(event) => setSettingsForm((prev) => ({
                  ...prev,
                  autoNoProgressDays: event.target.value,
                }))}
                placeholder="可留空"
              />
            </label>
            <label className="space-y-1 text-xs text-secondary-text">
              未走强收益%
              <input
                className={INPUT_CLASS}
                type="number"
                min={-100}
                max={1000}
                step="0.1"
                value={settingsForm.autoNoProgressMinReturnPct}
                onChange={(event) => setSettingsForm((prev) => ({
                  ...prev,
                  autoNoProgressMinReturnPct: event.target.value,
                }))}
                placeholder="留空=0"
              />
            </label>
            <label className="space-y-1 text-xs text-secondary-text">
              卖出比例%
              <input
                className={INPUT_CLASS}
                type="number"
                min={1}
                max={100}
                step="1"
                value={settingsForm.autoSellPositionPct}
                onChange={(event) => setSettingsForm((prev) => ({
                  ...prev,
                  autoSellPositionPct: event.target.value,
                }))}
                placeholder="留空=100"
              />
            </label>
            <label className="space-y-1 text-xs text-secondary-text">
              每日预算（{currency}）
              <input
                className={INPUT_CLASS}
                type="number"
                min={1}
                step="100"
                value={settingsForm.autoDailyBudget}
                onChange={(event) => setSettingsForm((prev) => ({ ...prev, autoDailyBudget: event.target.value }))}
                placeholder="可留空"
              />
            </label>
            <label className="space-y-1 text-xs text-secondary-text">
              最低现金余额（{currency}）
              <input
                className={INPUT_CLASS}
                type="number"
                min={1}
                step="100"
                value={settingsForm.autoMinCashBalance}
                onChange={(event) => setSettingsForm((prev) => ({ ...prev, autoMinCashBalance: event.target.value }))}
                placeholder="可留空"
              />
            </label>
            <label className="space-y-1 text-xs text-secondary-text">
              最大回撤%
              <input
                className={INPUT_CLASS}
                type="number"
                min={0}
                max={100}
                step="0.1"
                value={settingsForm.autoMaxDrawdownPct}
                onChange={(event) => setSettingsForm((prev) => ({ ...prev, autoMaxDrawdownPct: event.target.value }))}
                placeholder="可留空"
              />
            </label>
            <label className="space-y-1 text-xs text-secondary-text">
              回撤恢复缓冲%
              <input
                className={INPUT_CLASS}
                type="number"
                min={0}
                max={100}
                step="0.1"
                value={settingsForm.autoDrawdownRecoveryHysteresisPct}
                onChange={(event) => setSettingsForm((prev) => ({
                  ...prev,
                  autoDrawdownRecoveryHysteresisPct: event.target.value,
                }))}
              />
            </label>
            <label className="space-y-1 text-xs text-secondary-text">
              连续亏损上限（笔）
              <input
                className={INPUT_CLASS}
                type="number"
                min={1}
                max={100}
                step={1}
                value={settingsForm.autoConsecutiveLossLimit}
                onChange={(event) => setSettingsForm((prev) => ({
                  ...prev,
                  autoConsecutiveLossLimit: event.target.value,
                }))}
                placeholder="可留空"
              />
            </label>
            <label className="space-y-1 text-xs text-secondary-text">
              连续亏损冷却（分钟）
              <input
                className={INPUT_CLASS}
                type="number"
                min={1}
                max={10080}
                step={1}
                value={settingsForm.autoConsecutiveLossCooldownMinutes}
                onChange={(event) => setSettingsForm((prev) => ({
                  ...prev,
                  autoConsecutiveLossCooldownMinutes: event.target.value,
                }))}
              />
            </label>
            <label className="space-y-1 text-xs text-secondary-text">
              每票金额（{currency}）
              <input
                className={INPUT_CLASS}
                type="number"
                min={1}
                step="100"
                value={settingsForm.autoCashPerOrder}
                onChange={(event) => setSettingsForm((prev) => ({ ...prev, autoCashPerOrder: event.target.value }))}
              />
            </label>
            <label className="space-y-1 text-xs text-secondary-text">
              每轮组合预算（{currency}）
              <input
                className={INPUT_CLASS}
                type="number"
                min={1}
                step="100"
                value={settingsForm.autoAllocationBudget}
                disabled={!settingsForm.autoScoreWeightedAllocationEnabled}
                onChange={(event) => setSettingsForm((prev) => ({
                  ...prev,
                  autoAllocationBudget: event.target.value,
                }))}
                placeholder={settingsForm.autoCashPerOrder || '10000'}
              />
            </label>
            <label className="space-y-1 text-xs text-secondary-text">
              组合分配方法
              <select
                className={SELECT_CLASS}
                value={settingsForm.autoAllocationMethod}
                disabled={!settingsForm.autoScoreWeightedAllocationEnabled}
                onChange={(event) => setSettingsForm((prev) => ({
                  ...prev,
                  autoAllocationMethod: event.target.value as VnpyPaperAllocationMethod,
                }))}
              >
                <option value="score_weighted">评分加权</option>
                <option value="score_inverse_volatility_20d">评分 / 20日波动率</option>
                <option value="score_inverse_volatility_20d_correlation_capped">
                  评分 / 波动率 + 相关性上限
                </option>
                <option value="target_tracking_min_variance_20d">
                  目标缺口 + 20日最小方差
                </option>
              </select>
            </label>
            <label className="space-y-1 text-xs text-secondary-text">
              波动率下限（%）
              <input
                className={INPUT_CLASS}
                type="number"
                min={0.01}
                max={1000}
                step="0.1"
                value={settingsForm.autoRiskVolatilityFloorPct}
                disabled={
                  !settingsForm.autoScoreWeightedAllocationEnabled
                  || ![
                    'score_inverse_volatility_20d',
                    'score_inverse_volatility_20d_correlation_capped',
                  ].includes(settingsForm.autoAllocationMethod)
                }
                onChange={(event) => setSettingsForm((prev) => ({
                  ...prev,
                  autoRiskVolatilityFloorPct: event.target.value,
                }))}
              />
            </label>
            <label className="space-y-1 text-xs text-secondary-text">
              相关性回看日数
              <input
                className={INPUT_CLASS}
                type="number"
                min={20}
                max={252}
                value={settingsForm.autoCorrelationLookbackDays}
                disabled={![
                  'score_inverse_volatility_20d_correlation_capped',
                  'target_tracking_min_variance_20d',
                ].includes(settingsForm.autoAllocationMethod)}
                onChange={(event) => setSettingsForm((prev) => ({
                  ...prev,
                  autoCorrelationLookbackDays: event.target.value,
                }))}
              />
            </label>
            <label className="space-y-1 text-xs text-secondary-text">
              最少重叠收益数
              <input
                className={INPUT_CLASS}
                type="number"
                min={5}
                max={120}
                value={settingsForm.autoCorrelationMinObservations}
                disabled={![
                  'score_inverse_volatility_20d_correlation_capped',
                  'target_tracking_min_variance_20d',
                ].includes(settingsForm.autoAllocationMethod)}
                onChange={(event) => setSettingsForm((prev) => ({
                  ...prev,
                  autoCorrelationMinObservations: event.target.value,
                }))}
              />
            </label>
            <label className="space-y-1 text-xs text-secondary-text">
              最大两两相关性
              <input
                className={INPUT_CLASS}
                type="number"
                min={-1}
                max={1}
                step="0.01"
                value={settingsForm.autoMaxPairwiseCorrelation}
                disabled={settingsForm.autoAllocationMethod !== 'score_inverse_volatility_20d_correlation_capped'}
                onChange={(event) => setSettingsForm((prev) => ({
                  ...prev,
                  autoMaxPairwiseCorrelation: event.target.value,
                }))}
              />
            </label>
            <label className="space-y-1 text-xs text-secondary-text">
              协方差风险惩罚
              <input
                className={INPUT_CLASS}
                type="number"
                min={0}
                max={10}
                step="0.05"
                value={settingsForm.autoCovarianceRiskPenalty}
                disabled={settingsForm.autoAllocationMethod !== 'target_tracking_min_variance_20d'}
                onChange={(event) => setSettingsForm((prev) => ({
                  ...prev,
                  autoCovarianceRiskPenalty: event.target.value,
                }))}
              />
            </label>
            <label className="space-y-1 text-xs text-secondary-text">
              间隔分钟
              <input
                className={INPUT_CLASS}
                type="number"
                min={1}
                max={10080}
                value={settingsForm.autoIntervalMinutes}
                onChange={(event) => setSettingsForm((prev) => ({ ...prev, autoIntervalMinutes: event.target.value }))}
              />
            </label>
            <label className="space-y-1 text-xs text-secondary-text">
              最低评分
              <input
                className={INPUT_CLASS}
                type="number"
                step="0.01"
                value={settingsForm.autoMinScore}
                onChange={(event) => setSettingsForm((prev) => ({ ...prev, autoMinScore: event.target.value }))}
                placeholder="可留空"
              />
            </label>
            <label className="space-y-1 text-xs text-secondary-text">
              红绿灯拦截
              <select
                className={SELECT_CLASS}
                value={settingsForm.autoMarketLightBlockMode}
                onChange={(event) => setSettingsForm((prev) => ({
                  ...prev,
                  autoMarketLightBlockMode: event.target.value as SettingsForm['autoMarketLightBlockMode'],
                }))}
              >
                <option value="red">仅红灯</option>
                <option value="red_yellow">红灯和黄灯</option>
              </select>
            </label>
            <label className="space-y-1 text-xs text-secondary-text">
              市场快照最长年龄（天）
              <input
                className={INPUT_CLASS}
                type="number"
                min={1}
                max={30}
                value={settingsForm.autoMarketContextMaxAgeDays}
                onChange={(event) => setSettingsForm((prev) => ({
                  ...prev,
                  autoMarketContextMaxAgeDays: event.target.value,
                }))}
              />
            </label>
            <label className="space-y-1 text-xs text-secondary-text">
              最低市场宽度分
              <input
                className={INPUT_CLASS}
                type="number"
                min={0}
                max={100}
                disabled={!settingsForm.autoMarketBreadthGateEnabled}
                value={settingsForm.autoMarketBreadthMinScore}
                onChange={(event) => setSettingsForm((prev) => ({
                  ...prev,
                  autoMarketBreadthMinScore: event.target.value,
                }))}
              />
            </label>
            <label className="space-y-1 text-xs text-secondary-text">
              热点强度最小回落分
              <input
                className={INPUT_CLASS}
                type="number"
                min={1}
                max={100}
                disabled={!settingsForm.autoHotspotRetreatGateEnabled}
                value={settingsForm.autoHotspotRetreatMinDrop}
                onChange={(event) => setSettingsForm((prev) => ({
                  ...prev,
                  autoHotspotRetreatMinDrop: event.target.value,
                }))}
              />
            </label>
            <label className="space-y-1 text-xs text-secondary-text">
              盘中指数最低涨跌幅（%）
              <input
                className={INPUT_CLASS}
                type="number"
                min={-20}
                max={20}
                step="0.1"
                disabled={!settingsForm.autoIntradayMarketGateEnabled}
                value={settingsForm.autoIntradayIndexMinChangePct}
                onChange={(event) => setSettingsForm((prev) => ({
                  ...prev,
                  autoIntradayIndexMinChangePct: event.target.value,
                }))}
              />
            </label>
            <label className="space-y-1 text-xs text-secondary-text">
              A 股盘中最低宽度分
              <input
                className={INPUT_CLASS}
                type="number"
                min={0}
                max={100}
                disabled={!settingsForm.autoIntradayMarketGateEnabled}
                value={settingsForm.autoIntradayBreadthMinScore}
                onChange={(event) => setSettingsForm((prev) => ({
                  ...prev,
                  autoIntradayBreadthMinScore: event.target.value,
                }))}
              />
            </label>
            <label className="space-y-1 text-xs text-secondary-text">
              关联市场最低涨跌幅（%）
              <input
                className={INPUT_CLASS}
                type="number"
                min={-20}
                max={20}
                step="0.1"
                disabled={!settingsForm.autoCrossMarketGateEnabled}
                value={settingsForm.autoCrossMarketMinChangePct}
                onChange={(event) => setSettingsForm((prev) => ({
                  ...prev,
                  autoCrossMarketMinChangePct: event.target.value,
                }))}
              />
            </label>
            <label className="space-y-1 text-xs text-secondary-text">
              熔断阈值
              <input
                className={INPUT_CLASS}
                type="number"
                min={2}
                max={20}
                value={settingsForm.autoFailureFuseThreshold}
                onChange={(event) => setSettingsForm((prev) => ({
                  ...prev,
                  autoFailureFuseThreshold: event.target.value,
                }))}
              />
            </label>
            <label className="space-y-1 text-xs text-secondary-text">
              熔断冷却（分钟）
              <input
                className={INPUT_CLASS}
                type="number"
                min={1}
                max={10080}
                disabled={!settingsForm.autoFailureFuseAutoRecoveryEnabled}
                value={settingsForm.autoFailureFuseCooldownMinutes}
                onChange={(event) => setSettingsForm((prev) => ({
                  ...prev,
                  autoFailureFuseCooldownMinutes: event.target.value,
                }))}
              />
            </label>
            <label className="space-y-1 text-xs text-secondary-text">
              最低数据质量分
              <input
                className={INPUT_CLASS}
                type="number"
                min={0}
                max={100}
                step="0.1"
                value={settingsForm.autoMinDataQualityScore}
                onChange={(event) => setSettingsForm((prev) => ({
                  ...prev,
                  autoMinDataQualityScore: event.target.value,
                }))}
                placeholder="0 表示仅审计"
              />
            </label>
            <label className="space-y-1 text-xs text-secondary-text">
              前瞻周期（交易日）
              <input
                className={INPUT_CLASS}
                type="number"
                min={1}
                max={60}
                value={settingsForm.autoCrossRunHorizonDays}
                onChange={(event) => setSettingsForm((prev) => ({
                  ...prev,
                  autoCrossRunHorizonDays: event.target.value,
                }))}
              />
            </label>
            <label className="space-y-1 text-xs text-secondary-text">
              最少成熟样本
              <input
                className={INPUT_CLASS}
                type="number"
                min={1}
                max={500}
                value={settingsForm.autoCrossRunMinMatureSamples}
                onChange={(event) => setSettingsForm((prev) => ({
                  ...prev,
                  autoCrossRunMinMatureSamples: event.target.value,
                }))}
              />
            </label>
            <label className="space-y-1 text-xs text-secondary-text">
              最低前瞻胜率%
              <input
                className={INPUT_CLASS}
                type="number"
                min={0}
                max={100}
                step="0.1"
                value={settingsForm.autoCrossRunMinWinRatePct}
                onChange={(event) => setSettingsForm((prev) => ({
                  ...prev,
                  autoCrossRunMinWinRatePct: event.target.value,
                }))}
              />
            </label>
            <label className="space-y-1 text-xs text-secondary-text">
              前瞻扫描上限
              <input
                className={INPUT_CLASS}
                type="number"
                min={1}
                max={2000}
                value={settingsForm.autoCrossRunMaxDecisions}
                onChange={(event) => setSettingsForm((prev) => ({
                  ...prev,
                  autoCrossRunMaxDecisions: event.target.value,
                }))}
              />
            </label>
            <label className="space-y-1 text-xs text-secondary-text">
              最低成交额
              <input
                className={INPUT_CLASS}
                type="number"
                min={1}
                step="10000"
                value={settingsForm.autoMinTurnover}
                onChange={(event) => setSettingsForm((prev) => ({ ...prev, autoMinTurnover: event.target.value }))}
                placeholder="可留空"
              />
            </label>
            <label className="space-y-1 text-xs text-secondary-text md:col-span-2 xl:col-span-3">
              股票黑名单
              <input
                className={INPUT_CLASS}
                value={settingsForm.autoSymbolBlacklist}
                onChange={(event) => setSettingsForm((prev) => ({
                  ...prev,
                  autoSymbolBlacklist: event.target.value,
                }))}
                placeholder="逗号分隔，如 600519,000001"
              />
            </label>
            <label className="space-y-1 text-xs text-secondary-text md:col-span-2 xl:col-span-3">
              目标持仓权重%
              <textarea
                className={TEXTAREA_CLASS}
                value={settingsForm.autoTargetPositionWeights}
                onChange={(event) => setSettingsForm((prev) => ({
                  ...prev,
                  autoTargetPositionWeights: event.target.value,
                }))}
                placeholder="600519:2.5&#10;000001:1.5"
              />
            </label>
            <label className="space-y-1 text-xs text-secondary-text md:col-span-2 xl:col-span-3">
              目标行业权重%
              <textarea
                className={TEXTAREA_CLASS}
                value={settingsForm.autoTargetIndustryWeights}
                onChange={(event) => setSettingsForm((prev) => ({
                  ...prev,
                  autoTargetIndustryWeights: event.target.value,
                }))}
                placeholder="白酒:8&#10;银行:12"
              />
            </label>
          </div>
          <div className="mt-4 flex flex-wrap gap-2">
            <Button type="submit" isLoading={saving} loadingText="保存中...">
              <Save className="h-4 w-4" />
              保存设置
            </Button>
            <Button
              variant="secondary"
              isLoading={runningAuto}
              loadingText="运行中..."
              disabled={!settingsForm.autoTradeEnabled}
              onClick={() => void handleRunAuto()}
            >
              <Play className="h-4 w-4" />
              立即运行一次
            </Button>
            <Button
              variant="outline"
              isLoading={runningDryRun}
              loadingText="演练中..."
              disabled={!paperAvailable}
              onClick={() => void handleRunDryRun()}
            >
              <ClipboardList className="h-4 w-4" />
              立即 dry-run
            </Button>
            {settingsForm.autoTradeEnabled || status?.settings?.autoTradeEnabled ? (
              <Button
                variant="outline"
                isLoading={pausingAuto}
                loadingText="暂停中..."
                disabled={!status}
                onClick={() => void handlePauseAutoTrade()}
              >
                <PauseCircle className="h-4 w-4" />
                暂停自动买入
              </Button>
            ) : (
              <Button
                variant="outline"
                isLoading={resumingAuto}
                loadingText="恢复中..."
                disabled={!status}
                onClick={() => void handleResumeAutoTrade()}
              >
                <Play className="h-4 w-4" />
                恢复自动买入
              </Button>
            )}
          </div>
        </form>

        <form className="rounded-xl border border-border bg-card/95 p-4" onSubmit={handleSubmitOrder}>
          <div className="mb-4 flex items-center gap-2 text-sm font-semibold text-foreground">
            <SendHorizontal className="h-4 w-4 text-cyan" />
            手动模拟委托
          </div>
          <div className="grid gap-3">
            <input
              className={INPUT_CLASS}
              placeholder="股票代码，例如 000001"
              value={orderForm.symbol}
              onChange={(event) => setOrderForm((prev) => ({ ...prev, symbol: event.target.value }))}
            />
            <div className="grid gap-2 md:grid-cols-3">
              <select
                className={SELECT_CLASS}
                value={orderForm.side}
                onChange={(event) => setOrderForm((prev) => ({ ...prev, side: event.target.value as VnpyPaperSide }))}
              >
                <option value="buy">买入</option>
                <option value="sell">卖出</option>
              </select>
              <select
                className={SELECT_CLASS}
                value={orderForm.market}
                onChange={(event) => setOrderForm((prev) => ({ ...prev, market: event.target.value as VnpyPaperMarket }))}
              >
                {markets.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
              </select>
              <select
                className={SELECT_CLASS}
                value={orderForm.executionRoute}
                onChange={(event) => setOrderForm((prev) => ({
                  ...prev,
                  executionRoute: event.target.value as 'local_paper' | 'vnpy_bridge',
                }))}
              >
                <option value="local_paper">本地成交</option>
                <option value="vnpy_bridge">vn.py bridge</option>
              </select>
            </div>
            <div className="grid grid-cols-2 gap-2">
              <input
                className={INPUT_CLASS}
                type="number"
                min={0}
                step="1"
                placeholder="数量"
                value={orderForm.quantity}
                onChange={(event) => setOrderForm((prev) => ({ ...prev, quantity: event.target.value }))}
              />
              <input
                className={INPUT_CLASS}
                type="number"
                min={0}
                step="100"
                placeholder="金额"
                value={orderForm.cashAmount}
                onChange={(event) => setOrderForm((prev) => ({ ...prev, cashAmount: event.target.value }))}
              />
            </div>
            <input
              className={INPUT_CLASS}
              type="number"
              min={0}
              step="0.001"
              placeholder="成交价，留空则取行情"
              value={orderForm.price}
              onChange={(event) => setOrderForm((prev) => ({ ...prev, price: event.target.value }))}
            />
            <input
              className={INPUT_CLASS}
              placeholder="备注"
              value={orderForm.note}
              onChange={(event) => setOrderForm((prev) => ({ ...prev, note: event.target.value }))}
            />
          </div>
          <Button className="mt-4 w-full" type="submit" isLoading={submitting} loadingText="提交中..." disabled={!canSubmitOrder}>
            <SendHorizontal className="h-4 w-4" />
            提交模拟委托
          </Button>
        </form>
      </section>

      {orderResult ? (
        <InlineAlert
          variant={orderTone(orderResult)}
          title={orderResult.status === 'submitted' ? '委托已提交' : orderResult.accepted ? '模拟成交' : '委托跳过'}
          message={`${orderResult.symbol || '-'} · ${orderResult.reason || orderResult.message} · 数量 ${formatNumber(orderResult.quantity, 0)} · 价格 ${formatNumber(orderResult.price)}`}
        />
      ) : null}

      {autoResult ? (
        <section className="rounded-xl border border-border bg-card/95 p-4">
          <div className="mb-3 flex items-center justify-between gap-3">
            <h2 className="text-sm font-semibold text-foreground">自动交易结果</h2>
            <span className="text-xs text-secondary-text">
              候选 {autoResult.candidateCount} · 计划 {autoResult.plannedCount} · 成交 {autoResult.submittedCount} · 跳过 {autoResult.skippedCount}
            </span>
          </div>
          <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-3">
            {autoResult.orders.map((item, index) => (
              <div key={`${item.symbol || 'order'}-${index}`} className="rounded-lg border border-border bg-surface px-3 py-2 text-xs">
                <div className="font-semibold text-foreground">{item.symbol || '-'}</div>
                <div className={item.accepted ? 'mt-1 text-success' : 'mt-1 text-warning'}>
                  {item.accepted ? 'filled' : item.reason || 'skipped'}
                </div>
                <div className="mt-1 text-secondary-text">
                  {formatNumber(item.quantity, 0)} 股 · {formatNumber(item.price)}
                </div>
              </div>
            ))}
          </div>
          {autoResult.messages.length > 0 ? (
            <p className="mt-3 text-xs text-secondary-text">{autoResult.messages.join('；')}</p>
          ) : null}
        </section>
      ) : null}

      <section className="rounded-xl border border-border bg-card/95 p-4">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-2 text-sm font-semibold text-foreground">
            <Activity className="h-4 w-4 text-warning" />
            自动交易告警历史
          </div>
          <Button
            variant="outline"
            isLoading={autoAlertTriggersLoading}
            loadingText="刷新中..."
            onClick={() => void loadAutoAlertTriggers()}
          >
            <RefreshCw className="h-4 w-4" />
            刷新告警
          </Button>
        </div>
        {autoAlertTriggersError ? <InlineAlert variant="warning" message={autoAlertTriggersError} /> : null}
        {autoAlertTriggers.length === 0 ? (
          <div className="rounded-lg border border-dashed border-border bg-surface/70 px-4 py-6 text-center text-sm text-secondary-text">
            暂无自动交易告警历史
          </div>
        ) : (
          <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-3">
            {autoAlertTriggers.map((item) => (
              <div key={item.id} className="rounded-lg border border-border bg-surface px-3 py-2 text-xs">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="break-words font-semibold text-foreground">{item.reason || item.status}</div>
                    <div className="mt-1 text-secondary-text">{formatDateTime(item.triggeredAt)}</div>
                  </div>
                  <span className={`shrink-0 rounded-full border px-2 py-0.5 ${alertStatusTone(String(item.status || ''))}`}>
                    {item.status}
                  </span>
                </div>
                <div className="mt-2 flex flex-wrap gap-2 text-secondary-text">
                  <span>{item.dataSource || 'system'}</span>
                  {item.observedValue !== null && item.observedValue !== undefined ? (
                    <span>观测 {formatNumber(item.observedValue)}</span>
                  ) : null}
                  {item.threshold !== null && item.threshold !== undefined ? (
                    <span>阈值 {formatNumber(item.threshold)}</span>
                  ) : null}
                </div>
              </div>
            ))}
          </div>
        )}
      </section>

      <section className="rounded-xl border border-border bg-card/95 p-4">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-2 text-sm font-semibold text-foreground">
            <ClipboardList className="h-4 w-4 text-cyan" />
            自动选股 Agent 记录
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Button
              variant="outline"
              isLoading={exportingAgentRuns}
              loadingText="导出中..."
              onClick={() => void handleExportAgentRuns()}
            >
              <Download className="h-4 w-4" />
              导出最近记录
            </Button>
            <Button
              variant="outline"
              isLoading={agentRunsLoading}
              loadingText="刷新中..."
              onClick={() => void loadAgentRuns()}
            >
              <RefreshCw className="h-4 w-4" />
              刷新记录
            </Button>
          </div>
        </div>
        <div className="mb-3 grid gap-2 md:grid-cols-2 xl:grid-cols-[1.1fr_0.8fr_0.8fr_1fr_1fr_auto_auto]">
          <input
            className={INPUT_CLASS}
            aria-label="Agent 策略筛选"
            placeholder="策略"
            value={agentRunFilters.strategy}
            onChange={(event) => setAgentRunFilters((prev) => ({ ...prev, strategy: event.target.value }))}
          />
          <select
            className={SELECT_CLASS}
            aria-label="Agent 市场筛选"
            value={agentRunFilters.market}
            onChange={(event) => setAgentRunFilters((prev) => ({ ...prev, market: event.target.value }))}
          >
            <option value="">全部市场</option>
            {markets.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
          </select>
          <select
            className={SELECT_CLASS}
            aria-label="Agent 状态筛选"
            value={agentRunFilters.status}
            onChange={(event) => setAgentRunFilters((prev) => ({ ...prev, status: event.target.value }))}
          >
            <option value="">全部状态</option>
            {agentRunStatuses.map((item) => <option key={item} value={item}>{item}</option>)}
          </select>
          <input
            className={INPUT_CLASS}
            type="datetime-local"
            aria-label="Agent 开始时间筛选"
            value={agentRunFilters.createdFrom}
            onChange={(event) => setAgentRunFilters((prev) => ({ ...prev, createdFrom: event.target.value }))}
          />
          <input
            className={INPUT_CLASS}
            type="datetime-local"
            aria-label="Agent 结束时间筛选"
            value={agentRunFilters.createdTo}
            onChange={(event) => setAgentRunFilters((prev) => ({ ...prev, createdTo: event.target.value }))}
          />
          <Button
            variant="outline"
            isLoading={agentRunsLoading}
            loadingText="筛选中..."
            onClick={() => void loadAgentRuns()}
          >
            <RefreshCw className="h-4 w-4" />
            应用筛选
          </Button>
          <Button
            variant="ghost"
            onClick={() => {
              setAgentRunFilters(defaultAgentRunFilterForm);
              void loadAgentRuns(defaultAgentRunFilterForm);
            }}
          >
            清空
          </Button>
        </div>
        {agentRunsError ? <InlineAlert variant="warning" message={agentRunsError} /> : null}
        <div
          className="mb-4 border-y border-border bg-surface/40 py-4"
          data-testid="agent-return-risk-calibration-trends"
        >
          <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
            <div className="flex items-center gap-2 text-sm font-semibold text-foreground">
              <LineChart className="h-4 w-4 text-cyan" />
              收益/风险长期校准
            </div>
            <div className="flex items-center gap-1" aria-label="收益风险校准窗口">
              {([7, 30, 90] as const).map((days) => (
                <Button
                  key={days}
                  size="xsm"
                  variant={returnRiskCalibrationDays === days ? 'primary' : 'outline'}
                  disabled={returnRiskCalibrationLoading}
                  onClick={() => handleReturnRiskCalibrationWindow(days)}
                >
                  {days} 天
                </Button>
              ))}
            </div>
          </div>
          {returnRiskCalibrationError ? (
            <InlineAlert variant="warning" message={returnRiskCalibrationError} />
          ) : null}
          {returnRiskCalibrationTrends ? (
            <>
              <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-5">
                <div className="border-l-2 border-cyan px-3 py-2">
                  <div className="text-xs text-secondary-text">已观测快照</div>
                  <div className="mt-1 text-lg font-semibold text-foreground">
                    {returnRiskCalibrationTrends.observedCount} / {returnRiskCalibrationTrends.scannedCount}
                  </div>
                </div>
                <div className="border-l-2 border-success px-3 py-2">
                  <div className="text-xs text-secondary-text">观测覆盖率</div>
                  <div className="mt-1 text-lg font-semibold text-foreground">
                    {formatNumber(returnRiskCalibrationTrends.observationRatePct)}%
                  </div>
                </div>
                <div className="border-l-2 border-warning px-3 py-2">
                  <div className="text-xs text-secondary-text">平均效用</div>
                  <div className="mt-1 text-lg font-semibold text-foreground">
                    {returnRiskCalibrationTrends.averageUtilityPct == null
                      ? '-'
                      : `${formatNumber(returnRiskCalibrationTrends.averageUtilityPct)}%`}
                  </div>
                </div>
                <div className="border-l-2 border-border px-3 py-2">
                  <div className="text-xs text-secondary-text">最新成熟样本</div>
                  <div className="mt-1 text-lg font-semibold text-foreground">
                    {returnRiskCalibrationTrends.latestMatureSampleCount}
                  </div>
                </div>
                <div className="border-l-2 border-border px-3 py-2">
                  <div className="text-xs text-secondary-text">目标已应用</div>
                  <div className="mt-1 text-lg font-semibold text-foreground">
                    {formatNumber(returnRiskCalibrationTrends.appliedRatePct)}%
                  </div>
                </div>
              </div>
              <div className="mt-3 overflow-x-auto border border-border">
                <table className="w-full min-w-[780px] border-collapse text-xs">
                  <thead className="bg-surface text-left text-secondary-text">
                    <tr>
                      <th className="px-3 py-2 font-semibold">市场 / 策略</th>
                      <th className="px-3 py-2 font-semibold">目标版本</th>
                      <th className="px-3 py-2 font-semibold">快照</th>
                      <th className="px-3 py-2 font-semibold">状态分布</th>
                      <th className="px-3 py-2 font-semibold">平均效用</th>
                      <th className="px-3 py-2 font-semibold">最新状态</th>
                    </tr>
                  </thead>
                  <tbody>
                    {returnRiskCalibrationTrends.groups.length > 0 ? (
                      returnRiskCalibrationTrends.groups.map((item) => (
                        <tr key={item.key} className="border-t border-border">
                          <td className="px-3 py-2 font-semibold text-foreground">
                            {item.market} / {item.strategy}
                          </td>
                          <td className="px-3 py-2 font-mono text-secondary-text">{item.version}</td>
                          <td className="px-3 py-2 text-secondary-text">{item.runSnapshotCount}</td>
                          <td className="px-3 py-2 text-secondary-text">
                            {Object.entries(item.stateCounts).map(([state, count]) => `${state} ${count}`).join(' · ') || '-'}
                          </td>
                          <td className="px-3 py-2 text-secondary-text">
                            {item.averageUtilityPct == null ? '-' : `${formatNumber(item.averageUtilityPct)}%`}
                          </td>
                          <td className="px-3 py-2 text-secondary-text">{item.latestState || '-'}</td>
                        </tr>
                      ))
                    ) : (
                      <tr className="border-t border-border">
                        <td className="px-3 py-4 text-center text-secondary-text" colSpan={6}>
                          暂无校准快照
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            </>
          ) : returnRiskCalibrationLoading ? (
            <div className="py-5 text-center text-sm text-secondary-text">加载中...</div>
          ) : null}
        </div>
        {agentRuns.length === 0 ? (
          <div className="rounded-lg border border-dashed border-border bg-surface/70 px-4 py-8 text-center text-sm text-secondary-text">
            暂无自动选股 Agent 运行记录
          </div>
        ) : (
          <div className="grid gap-4 xl:grid-cols-[0.85fr_1.15fr]">
            <div className="space-y-2">
              {agentRuns.map((item) => (
                <button
                  key={item.runUid}
                  type="button"
                  className={`w-full rounded-lg border px-3 py-2 text-left text-xs transition-colors ${
                    selectedAgentRun?.runUid === item.runUid
                      ? 'border-cyan bg-cyan/10'
                      : 'border-border bg-surface hover:border-cyan/60'
                  }`}
                  onClick={() => void loadAgentRunDetail(item.runUid)}
                >
                  <div className="flex items-center justify-between gap-3">
                    <span className="font-semibold text-foreground">{item.strategy}</span>
                    <span className="text-secondary-text">{formatDateTime(item.startedAt || item.createdAt)}</span>
                  </div>
                  <div className="mt-1 text-secondary-text">
                    {item.market} · 候选 {item.candidateCount} · 计划 {item.plannedCount} · 成交 {item.submittedCount} · 跳过 {item.skippedCount}
                  </div>
                  <div className={item.status === 'completed' ? 'mt-1 text-success' : 'mt-1 text-warning'}>
                    {item.status}{item.error ? ` · ${item.error}` : ''}
                  </div>
                  {dataQualityStatus(item) ? (
                    <div className="mt-1 text-secondary-text">数据质量 {dataQualityStatus(item)}</div>
                  ) : null}
                </button>
              ))}
            </div>

            <div className="min-w-0">
              {selectedAgentRun ? (
                <div className="space-y-3">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div className="text-xs text-secondary-text">
                      {selectedAgentRun.runUid} · {formatDateTime(selectedAgentRun.startedAt || selectedAgentRun.createdAt)}
                    </div>
                    <Button size="xsm" variant="outline" onClick={handleExportSelectedAgentRun}>
                      <Download className="h-3.5 w-3.5" />
                      导出 JSON
                    </Button>
                  </div>
                  {selectedAgentPlan ? (
                    <div className="rounded-lg border border-border bg-surface px-3 py-2">
                      <div className="mb-2 text-xs font-semibold text-foreground">Agent 计划</div>
                      <div className="grid gap-2 text-xs md:grid-cols-2 xl:grid-cols-4">
                        <div>
                          <div className="text-secondary-text">策略 / 市场</div>
                          <div className="mt-1 font-semibold text-foreground">
                            {String(selectedAgentPlan.strategy || selectedAgentRun.strategy || '-')} · {String(selectedAgentPlan.market || selectedAgentRun.market || '-')}
                          </div>
                        </div>
                        <div>
                          <div className="text-secondary-text">执行模式</div>
                          <div className="mt-1 font-semibold text-foreground">
                            {String(selectedAgentPlan.executionMode || selectedAgentRun.diagnostics?.executionMode || '-')}
                          </div>
                        </div>
                        <div>
                          <div className="text-secondary-text">候选 / 每票</div>
                          <div className="mt-1 font-semibold text-foreground">
                            {String(selectedAgentPlan.maxResults ?? selectedAgentRun.maxResults ?? '-')} · {formatMoney(selectedAgentPlan.cashPerOrder ?? selectedAgentRun.cashPerOrder, currency)}
                          </div>
                        </div>
                        <div>
                          <div className="text-secondary-text">风控 gate</div>
                          <div className="mt-1 font-semibold text-foreground">
                            {selectedAgentGates?.timeGateEnforced ? '时间窗' : '时间窗未强制'} · 持仓≤{String(selectedAgentRiskBudget?.maxPositions ?? '-')}
                          </div>
                        </div>
                      </div>
                    </div>
                  ) : null}
                  {selectedAgentSummary ? (
                    <div className="rounded-lg border border-border bg-surface px-3 py-2">
                      <div className="mb-2 text-xs font-semibold text-foreground">运行总结</div>
                      <div className="text-sm font-semibold text-foreground">
                        {String(selectedAgentSummary.headline || selectedAgentSummary.outcome || '-')}
                      </div>
                      <div className="mt-2 grid gap-2 text-xs md:grid-cols-4">
                        <div>
                          <div className="text-secondary-text">结果</div>
                          <div className="mt-1 text-foreground">{String(selectedAgentSummary.outcome || selectedAgentRun.status || '-')}</div>
                        </div>
                        <div>
                          <div className="text-secondary-text">候选 / 计划</div>
                          <div className="mt-1 text-foreground">
                            {String(selectedAgentSummary.candidateCount ?? selectedAgentRun.candidateCount)} / {String(selectedAgentSummary.plannedCount ?? selectedAgentRun.plannedCount)}
                          </div>
                        </div>
                        <div>
                          <div className="text-secondary-text">成交 / 跳过</div>
                          <div className="mt-1 text-foreground">
                            {String(selectedAgentSummary.submittedCount ?? selectedAgentRun.submittedCount)} / {String(selectedAgentSummary.skippedCount ?? selectedAgentRun.skippedCount)}
                          </div>
                        </div>
                        <div>
                          <div className="text-secondary-text">数据质量</div>
                          <div className="mt-1 text-foreground">{String(selectedAgentSummary.dataQualityStatus || '-')}</div>
                        </div>
                      </div>
                      {selectedAgentSummarySkips.length > 0 ? (
                        <div className="mt-2 flex flex-wrap gap-2">
                          {selectedAgentSummarySkips.map((item) => (
                            <span
                              key={String(item.reason || item.key || '')}
                              className="rounded-full border border-warning/30 bg-warning/10 px-2 py-1 text-xs text-warning"
                            >
                              {String(item.reason || item.key || '-')} ×{String(item.count || 0)}
                            </span>
                          ))}
                        </div>
                      ) : null}
                    </div>
                  ) : null}
                  {selectedRiskSummary.length > 0 ? (
                    <div className="rounded-lg border border-border bg-surface px-3 py-2">
                      <div className="mb-2 text-xs font-semibold text-foreground">风控统计</div>
                      <div className="flex flex-wrap gap-2">
                        {selectedRiskSummary.map((item) => (
                          <span
                            key={item.reason}
                            className="rounded-full border border-warning/30 bg-warning/10 px-2 py-1 text-xs text-warning"
                          >
                            {item.reason} ×{item.count}
                          </span>
                        ))}
                      </div>
                    </div>
                  ) : null}
                  <PortfolioChangePanel
                    change={selectedAgentRun.portfolioChange}
                    currency={currency}
                  />
                  {(selectedAgentRun.timeline || []).length > 0 ? (
                    <div className="rounded-lg border border-border bg-surface px-3 py-2">
                      <div className="mb-2 text-xs font-semibold text-foreground">运行时间线</div>
                      <div className="space-y-2">
                        {(selectedAgentRun.timeline || []).map((event, index) => (
                          <div
                            key={`${event.stage}-${index}`}
                            className="grid gap-2 rounded-md border border-border bg-card/80 px-3 py-2 text-xs md:grid-cols-[120px_1fr]"
                          >
                            <div className="text-secondary-text">
                              <div className="font-mono">{formatDateTime(event.timestamp)}</div>
                              <div className={decisionTone(event.status)}>{event.stage} · {event.status}</div>
                            </div>
                            <div className="text-foreground">{event.message}</div>
                          </div>
                        ))}
                      </div>
                    </div>
                  ) : null}
                  <div className="overflow-x-auto rounded-lg border border-border">
                    <table className="w-full min-w-[760px] border-collapse text-xs">
                      <thead className="bg-surface text-left text-secondary-text">
                        <tr>
                          <th className="px-3 py-2 font-semibold">候选</th>
                          <th className="px-3 py-2 font-semibold">分数</th>
                          <th className="px-3 py-2 font-semibold">动作</th>
                          <th className="px-3 py-2 font-semibold">状态</th>
                          <th className="px-3 py-2 font-semibold">计划/成交</th>
                          <th className="px-3 py-2 font-semibold">解释</th>
                        </tr>
                      </thead>
                      <tbody>
                        {selectedAgentRun.decisions.map((item) => (
                          <tr key={item.id} className="border-t border-border align-top">
                            <td className="px-3 py-2">
                              <div className="font-mono font-semibold text-foreground">{item.symbol || '-'}</div>
                              <div className="mt-1 text-secondary-text">{item.name || item.market}</div>
                            </td>
                            <td className="px-3 py-2 text-secondary-text">{formatNumber(item.score)}</td>
                            <td className="px-3 py-2 text-secondary-text">{item.action}</td>
                            <td className={`px-3 py-2 ${decisionTone(item.status)}`}>
                              {item.status}
                              {item.reason ? <div className="mt-1">{item.reason}</div> : null}
                            </td>
                            <td className="px-3 py-2 text-secondary-text">
                              <div>{formatMoney(item.cashAmount, currency)}</div>
                              <div>{formatNumber(item.quantity, 0)} 股 · {formatNumber(item.price)}</div>
                              {item.tradeId ? <div>trade #{item.tradeId}</div> : null}
                            </td>
                            <td className="max-w-[260px] px-3 py-2 text-secondary-text">
                              <div className="line-clamp-3">{item.rationale || '-'}</div>
                              {item.riskFlags.length > 0 ? (
                                <div className="mt-1 text-warning">{item.riskFlags.join(' / ')}</div>
                              ) : null}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                    {selectedAgentRun.decisions.length === 0 ? (
                      <div className="border-t border-border px-4 py-6 text-center text-sm text-secondary-text">
                        本轮没有候选决策记录
                      </div>
                    ) : null}
                  </div>

                  {(selectedAgentRun.tradePlans || []).length > 0 ? (
                    <div className="rounded-lg border border-border bg-surface px-3 py-2">
                      <div className="mb-2 text-xs font-semibold text-foreground">交易计划</div>
                      <div className="grid gap-2 md:grid-cols-2">
                        {(selectedAgentRun.tradePlans || []).map((plan) => {
                          const retryText = formatTradePlanRetry(plan);
                          return (
                          <div key={plan.planUid} className="rounded-md border border-border bg-card/80 px-3 py-2 text-xs">
                            <div className="flex items-center justify-between gap-2">
                              <span className="font-mono font-semibold text-foreground">{plan.symbol || '-'}</span>
                              <span className={decisionTone(plan.status)}>{plan.status}</span>
                            </div>
                            <div className="mt-1 text-secondary-text">
                              {plan.executionMode} · {formatMoney(plan.plannedCashAmount, currency)} · {formatNumber(plan.plannedQuantity, 0)} 股
                            </div>
                            {plan.skipReason ? <div className="mt-1 text-warning">{plan.skipReason}</div> : null}
                            {retryText ? <div className="mt-1 text-secondary-text">{retryText}</div> : null}
                            {plan.executionMode === 'manual_approval' && plan.status === 'planned' ? (
                              <Button
                                className="mt-2"
                                size="xsm"
                                variant="outline"
                                isLoading={approvingPlanUid === plan.planUid}
                                loadingText="审批中..."
                                disabled={!paperAvailable}
                                onClick={() => void handleApproveTradePlan(plan.planUid)}
                              >
                                <SendHorizontal className="h-3.5 w-3.5" />
                                审批成交
                              </Button>
                            ) : null}
                            {canRetryTradePlan(plan) ? (
                              <Button
                                className="mt-2"
                                size="xsm"
                                variant="outline"
                                isLoading={retryingPlanUid === plan.planUid}
                                loadingText="重试中..."
                                disabled={!paperAvailable}
                                onClick={() => void handleRetryTradePlan(plan.planUid)}
                              >
                                <RefreshCw className="h-3.5 w-3.5" />
                                重试提交
                              </Button>
                            ) : null}
                            {canCancelTradePlan(plan) ? (
                              <Button
                                className="mt-2"
                                size="xsm"
                                variant="outline"
                                isLoading={cancellingPlanUid === plan.planUid}
                                loadingText="撤单中..."
                                disabled={!paperAvailable}
                                onClick={() => void handleCancelTradePlan(plan.planUid)}
                              >
                                <Ban className="h-3.5 w-3.5" />
                                撤单
                              </Button>
                            ) : null}
                          </div>
                          );
                        })}
                      </div>
                    </div>
                  ) : null}
                </div>
              ) : (
                <div className="rounded-lg border border-dashed border-border bg-surface/70 px-4 py-8 text-center text-sm text-secondary-text">
                  选择一条运行记录查看候选决策
                </div>
              )}
            </div>
          </div>
        )}
      </section>

      <section className="grid gap-4 xl:grid-cols-[1fr_1fr]">
        <div className="rounded-xl border border-border bg-card/95 p-4">
          <div className="mb-3 flex items-center justify-between gap-3">
            <h2 className="text-sm font-semibold text-foreground">当前持仓</h2>
            <span className="text-xs text-secondary-text">{positionRows.length} 只</span>
          </div>
          {positionRows.length === 0 ? (
            <div className="rounded-lg border border-dashed border-border bg-surface/70 px-4 py-8 text-center text-sm text-secondary-text">
              暂无模拟持仓
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[700px] border-collapse text-sm">
                <thead className="bg-surface text-left text-xs text-secondary-text">
                  <tr>
                    <th className="px-3 py-2 font-semibold">代码</th>
                    <th className="px-3 py-2 font-semibold">数量</th>
                    <th className="px-3 py-2 font-semibold">成本</th>
                    <th className="px-3 py-2 font-semibold">现价</th>
                    <th className="px-3 py-2 font-semibold">价格源</th>
                    <th className="px-3 py-2 font-semibold">市值</th>
                    <th className="px-3 py-2 font-semibold">盈亏</th>
                  </tr>
                </thead>
                <tbody>
                  {positionRows.map((item) => (
                    <tr key={`${item.market || 'cn'}-${item.symbol}`} className="border-t border-border">
                      <td className="px-3 py-2 font-mono font-semibold text-foreground">{item.symbol}</td>
                      <td className="px-3 py-2 text-secondary-text">{formatNumber(item.quantity, 0)}</td>
                      <td className="px-3 py-2 text-secondary-text">{formatNumber(item.avgCost)}</td>
                      <td className="px-3 py-2 text-secondary-text">{formatNumber(item.lastPrice)}</td>
                      <td className="px-3 py-2 text-secondary-text">
                        {item.priceAvailable === false
                          ? '缺价'
                          : `${item.priceSource || item.priceProvider || '-'}${item.priceStale ? ' · 陈旧' : ''}`}
                      </td>
                      <td className="px-3 py-2 text-secondary-text">{formatMoney(item.marketValueBase, currency)}</td>
                      <td className="px-3 py-2 text-secondary-text">{formatPercent(item.unrealizedPnlPct)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>

        <div className="rounded-xl border border-border bg-card/95 p-4">
          <div className="mb-3 flex items-center justify-between gap-3">
            <h2 className="text-sm font-semibold text-foreground">近期成交</h2>
            <span className="text-xs text-secondary-text">{status?.recentTrades?.length || 0} 条</span>
          </div>
          {status?.recentTrades?.length ? (
            <div className="max-h-[360px] overflow-auto rounded-lg border border-border">
              {status.recentTrades.map((item) => (
                <div key={item.id} className="grid grid-cols-[1fr_auto] gap-3 border-b border-border px-3 py-2 text-xs last:border-b-0">
                  <div className="min-w-0">
                    <div className="font-mono font-semibold text-foreground">{item.symbol}</div>
                    <div className="mt-1 truncate text-secondary-text">
                      {formatDateTime(item.tradeDate)} · {item.side} · {item.note || '-'}
                    </div>
                  </div>
                  <div className="text-right text-secondary-text">
                    <div>{formatNumber(item.quantity, 0)} 股</div>
                    <div>{formatNumber(item.price)}</div>
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <div className="rounded-lg border border-dashed border-border bg-surface/70 px-4 py-8 text-center text-sm text-secondary-text">
              暂无模拟成交
            </div>
          )}
        </div>
      </section>
    </AppPage>
  );
};

export default VnpyPaperTradingPage;
