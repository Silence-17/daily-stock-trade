import apiClient from './index';
import { systemConfigApi } from './systemConfig';
import { toCamelCase } from './utils';

const ALPHASIFT_SCREEN_TIMEOUT_MS = 180000;
const ALPHASIFT_INSTALL_TIMEOUT_MS = 300000;
export const ALPHASIFT_CONFIG_CHANGED_EVENT = 'alphasift-config-changed';
export const SYSTEM_CONFIG_CHANGED_EVENT = 'dsa-system-config-changed';

export type AlphaSiftStatus = {
  enabled: boolean;
  available: boolean;
  installSpecIsDefault: boolean;
  contractVersion?: string | null;
  version?: string | null;
  strategyCount?: number | null;
  sourceHealth?: Record<string, Record<string, Record<string, unknown>>>;
  diagnostics?: Record<string, string>;
};

export type AlphaSiftInstallResponse = {
  installed: boolean;
  alreadyInstalled: boolean;
  installSpecIsDefault: boolean;
};

export type AlphaSiftCandidate = {
  rank: number;
  code: string;
  name: string;
  score?: number | null;
  screenScore?: number | null;
  reason: string;
  riskLevel?: string;
  riskFlags?: string[];
  llmScore?: number | null;
  llmConfidence?: number | null;
  llmSector?: string;
  llmTheme?: string;
  llmTags?: string[];
  llmThesis?: string;
  llmCatalysts?: string[];
  llmRisks?: string[];
  llmWatchItems?: string[];
  llmInvalidators?: string[];
  llmStyleFit?: string;
  price?: number | null;
  changePct?: number | null;
  amount?: number | null;
  industry?: string;
  factorScores?: Record<string, number>;
  postAnalysisSummaries?: Record<string, string>;
  postAnalysisTags?: string[];
  dsaContext?: {
    enriched?: boolean;
    quote?: Record<string, unknown>;
    fundamentals?: Record<string, unknown>;
    news?: {
      success?: boolean;
      query?: string;
      provider?: string;
      results?: Array<Record<string, unknown>>;
      error?: string | null;
    };
    warnings?: string[];
  };
  dsaNews?: Array<{
    title?: string;
    snippet?: string;
    url?: string;
    source?: string;
    publishedDate?: string | null;
  }>;
  dsaAnalysisSummary?: string;
  dataQuality?: string;
  missingFields?: string[];
  dataSources?: string[];
  qualityNotes?: string[];
  cacheUsed?: boolean;
  cachedAt?: string | null;
  stale?: boolean;
  staleAgeHours?: number | null;
  raw: Record<string, unknown>;
};

export type AlphaSiftStrategy = {
  id: string;
  name: string;
  title?: string;
  description: string;
  version?: string;
  category?: string;
  tag?: string;
  tags?: string[];
  marketScope?: string[];
  market?: string;
};

export type AlphaSiftStrategiesResponse = {
  enabled: boolean;
  strategies: AlphaSiftStrategy[];
  strategyCount: number;
};

export type AlphaSiftHotspot = {
  topic: string;
  name?: string;
  source?: string;
  rank?: number | null;
  changePct?: number | null;
  heatScore?: number | null;
  trendScore?: number | null;
  persistenceScore?: number | null;
  coolingScore?: number | null;
  observations?: number | null;
  state?: string;
  stage?: string;
  sampleStockCount?: number | null;
  leaders?: string[];
  providerUsed?: string;
  fallbackUsed?: boolean;
  cacheUsed?: boolean;
  cachedAt?: string | null;
  sourceErrors?: string[];
  stale?: boolean;
  staleAgeHours?: number | null;
};

export type AlphaSiftHotspotRouteItem = {
  title: string;
  description: string;
  source?: string;
  date?: string;
  time?: string;
  publishedAt?: string;
  url?: string;
};

export type AlphaSiftHotspotStock = {
  code?: string;
  name?: string;
  changePct?: number | null;
  amount?: number | null;
  turnoverRate?: number | null;
  volumeRatio?: number | null;
  role?: string;
  hotStockScore?: number | null;
  source?: string;
  sourceConfidence?: number | null;
  fallbackUsed?: boolean;
};

export type AlphaSiftHotspotDetail = {
  enabled: boolean;
  provider: string;
  topic: string;
  name?: string;
  canonicalTopic?: string;
  aliases?: string[];
  summary?: string;
  summaryDetail?: Record<string, unknown>;
  route: AlphaSiftHotspotRouteItem[];
  timeline?: AlphaSiftHotspotRouteItem[];
  stocks: AlphaSiftHotspotStock[];
  leaderStocks?: AlphaSiftHotspotStock[];
  stockCount: number;
  sourceErrors?: string[];
  qualityStatus?: 'available' | 'partial' | 'stale' | 'failed' | string;
  missingFields?: string[];
  fallbackUsed?: boolean;
  stale?: boolean;
  staleAgeHours?: number | null;
  cacheUsed?: boolean;
  cachedAt?: string | null;
  resolverCandidates?: Record<string, unknown>[];
};

export type AlphaSiftHotspotsResponse = {
  enabled: boolean;
  provider: string;
  providerUsed?: string;
  fallbackUsed?: boolean;
  cacheUsed?: boolean;
  cachedAt?: string | null;
  sourceErrors?: string[];
  stale?: boolean;
  staleAgeHours?: number | null;
  message?: string | null;
  hotspots: AlphaSiftHotspot[];
  hotspotCount: number;
  details?: Record<string, AlphaSiftHotspotDetail>;
};

export type AlphaSiftScreenResponse = {
  enabled: boolean;
  candidates: AlphaSiftCandidate[];
  candidateCount: number;
  runId?: string;
  strategy?: string;
  market?: string;
  snapshotCount?: number;
  afterFilterCount?: number;
  llmRanked?: boolean;
  llmMarketView?: string;
  llmSelectionLogic?: string;
  llmPortfolioRisk?: string;
  llmCoverage?: number | null;
  llmParseErrors?: string[];
  warnings?: string[];
  sourceErrors?: string[];
  qualityStatus?: string;
  fallbackUsed?: boolean;
  cacheUsed?: boolean;
  cachedAt?: string | null;
  stale?: boolean;
  staleAgeHours?: number | null;
  dsaEnrichment?: {
    enabled?: boolean;
    maxCandidates?: number;
    requestedCount?: number;
    enrichedCount?: number;
    warnings?: string[];
  };
  deepAnalysisRequested?: boolean | null;
  postAnalyzers?: string[];
  dailyEnriched?: boolean | null;
  dailyEnrichCount?: number | null;
  riskEnabled?: boolean | null;
  portfolioDiversityEnabled?: boolean | null;
  portfolioConcentrationNotes?: string[];
};

export type AlphaSiftScreenAccepted = {
  taskId: string;
  traceId?: string | null;
  status: 'pending' | 'processing' | 'completed' | 'failed' | string;
  message: string;
  strategy: string;
  market: string;
  maxResults: number;
};

export type AlphaSiftScreenTaskStatus = {
  taskId: string;
  traceId?: string | null;
  status: 'pending' | 'processing' | 'completed' | 'failed' | string;
  progress?: number | null;
  message?: string | null;
  error?: string | null;
  result?: AlphaSiftScreenResponse | null;
};

export type AlphaSiftReplayCompatibility = {
  strategy: string;
  market: string;
  snapshotDate: string;
  universeCount: number;
  requiredHardFields: string[];
  requiredScoreFields: string[];
  hardCompleteRows: number;
  scoreCompleteRows: number;
  hardCoverageRatio: number;
  scoreCoverageRatio: number;
  hardMissingCounts: Record<string, number>;
  scoreMissingCounts: Record<string, number>;
};

export type AlphaSiftReplayCandidate = {
  symbol: string;
  name?: string | null;
  industry?: string | null;
  price?: number | null;
  changePct?: number | null;
  screenScore: number;
  [key: string]: unknown;
};

export type AlphaSiftReplayResponse = {
  strategy: string;
  market: string;
  snapshotDate: string;
  universeCount: number;
  completeRowCount: number;
  filteredCount: number;
  candidateCount: number;
  candidates: AlphaSiftReplayCandidate[];
  compatibility: AlphaSiftReplayCompatibility;
  methodology: {
    pointInTime: boolean;
    lookaheadProtection: boolean;
    usesCurrentSnapshotFallback: boolean;
    llmRankingEnabled: boolean;
    ranking: string;
  };
};

export type AlphaSiftPortfolioBacktestResponse = {
  strategy: string;
  market: string;
  dateFrom: string;
  dateTo: string;
  snapshotCount: number;
  selectedCount: number;
  evaluatedCount: number;
  coveragePct: number;
  initialCapital: number;
  finalEquity: number;
  benchmarkSymbol?: string | null;
  metrics: {
    totalReturnPct?: number | null;
    annualizedReturnPct?: number | null;
    benchmarkReturnPct?: number | null;
    excessReturnPct?: number | null;
    maxDrawdownPct?: number | null;
    periodWinRatePct?: number | null;
    periodCount: number;
    totalTurnoverPct?: number | null;
    averageTurnoverPct?: number | null;
    endingOpenPositionCount?: number;
    endingCash?: number;
    endingMarketValue?: number;
    totalFees?: number;
    totalTaxes?: number;
    totalSlippageCost?: number;
    processedCorporateActionCount?: number;
    appliedCorporateActionCount?: number;
    cashDividendsReceived?: number;
  };
  periods: Array<{
    signalDate: string;
    selectedCount: number;
    holdingCount?: number;
    evaluatedCount: number;
    coveragePct: number;
    turnoverPct?: number | null;
    retainedCount?: number;
    forcedRetainedCount?: number;
    entryTradeCount?: number;
    exitTradeCount?: number;
    netReturnPct?: number | null;
    equity: number;
    cash?: number;
    marketValue?: number;
    positionCount?: number;
    tradeCount?: number;
    blockedTradeCount?: number;
    fees?: number;
    taxes?: number;
    slippageCost?: number;
    replayCandidateCount?: number;
    targetWeights?: Record<string, number>;
    configuredTargetsNotSelected?: string[];
    processedCorporateActionCount?: number;
    appliedCorporateActionCount?: number;
    corporateActions?: Array<Record<string, unknown>>;
  }>;
  methodology: Record<string, unknown>;
};

export type AlphaSiftPortfolioCorporateActionInput = {
  symbol: string;
  effectiveDate: string;
  actionType: 'cash_dividend' | 'split_adjustment';
  cashDividendPerShare?: number;
  splitRatio?: number;
};

export type AlphaSiftHistoricalUniverseResponse = {
  market: string;
  snapshotDate: string;
  totalCount: number;
  returnedCount: number;
  truncated: boolean;
  items: Array<{
    symbol: string;
    name: string;
    industry?: string | null;
    listDate: string;
    delistDate?: string | null;
  }>;
  methodology: Record<string, unknown>;
};

export type AlphaSiftFactorIngestionAccepted = {
  taskId: string;
  traceId: string;
  status: string;
  message: string;
  market: string;
  snapshotCount: number;
  symbolCount: number;
};

export type AlphaSiftFactorIngestionTask = {
  taskId: string;
  traceId?: string | null;
  status: string;
  progress?: number | null;
  message?: string | null;
  error?: string | null;
  result?: {
    rowCount?: number;
    inserted?: number;
    updated?: number;
    errorCount?: number;
    errors?: Array<Record<string, string>>;
  } | null;
};

export type AlphaSiftFullMarketIngestionJob = {
  jobId: string;
  market: string;
  snapshotDates: string[];
  universeSource: string;
  status: string;
  batchSize: number;
  totalSymbols: number;
  totalWorkItems: number;
  nextOffset: number;
  remainingWorkItems: number;
  progressPct: number;
  completedBatches: number;
  rowCount: number;
  inserted: number;
  updated: number;
  sourceErrorCount: number;
  errors: Array<Record<string, unknown>>;
  taskId?: string | null;
  error?: string | null;
  heartbeatAt?: string | null;
  completedAt?: string | null;
};

export function notifyAlphaSiftConfigChanged(): void {
  window.dispatchEvent(new Event(ALPHASIFT_CONFIG_CHANGED_EVENT));
  notifySystemConfigChanged();
}

export function notifySystemConfigChanged(): void {
  window.dispatchEvent(new Event(SYSTEM_CONFIG_CHANGED_EVENT));
}

async function setAlphaSiftEnabled(value: 'true' | 'false'): Promise<void> {
  const config = await systemConfigApi.getConfig(false);
  await systemConfigApi.update({
    configVersion: config.configVersion,
    maskToken: config.maskToken,
    reloadNow: true,
    items: [{ key: 'ALPHASIFT_ENABLED', value }],
  });
  notifyAlphaSiftConfigChanged();
}

export const alphasiftApi = {
  async getStatus(): Promise<AlphaSiftStatus> {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/alphasift/status');
    return toCamelCase<AlphaSiftStatus>(response.data);
  },

  async screen(payload: { market: string; strategy: string; maxResults: number }): Promise<AlphaSiftScreenResponse> {
    const response = await apiClient.post<Record<string, unknown>>('/api/v1/alphasift/screen', {
      market: payload.market,
      strategy: payload.strategy,
      max_results: payload.maxResults,
    }, { timeout: ALPHASIFT_SCREEN_TIMEOUT_MS });
    return toCamelCase<AlphaSiftScreenResponse>(response.data);
  },

  async startScreen(payload: { market: string; strategy: string; maxResults: number }): Promise<AlphaSiftScreenAccepted> {
    const response = await apiClient.post<Record<string, unknown>>('/api/v1/alphasift/screen/tasks', {
      market: payload.market,
      strategy: payload.strategy,
      max_results: payload.maxResults,
    });
    return toCamelCase<AlphaSiftScreenAccepted>(response.data);
  },

  async getScreenTask(taskId: string): Promise<AlphaSiftScreenTaskStatus> {
    const response = await apiClient.get<Record<string, unknown>>(`/api/v1/alphasift/screen/tasks/${encodeURIComponent(taskId)}`);
    return toCamelCase<AlphaSiftScreenTaskStatus>(response.data);
  },

  async getStrategies(): Promise<AlphaSiftStrategiesResponse> {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/alphasift/strategies', { timeout: ALPHASIFT_INSTALL_TIMEOUT_MS });
    return toCamelCase<AlphaSiftStrategiesResponse>(response.data);
  },

  async getReplayCompatibility(payload: {
    strategy: string;
    market: string;
    snapshotDate: string;
  }): Promise<AlphaSiftReplayCompatibility> {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/alphasift/replay/compatibility', {
      params: {
        strategy: payload.strategy,
        market: payload.market,
        snapshot_date: payload.snapshotDate,
      },
    });
    return toCamelCase<AlphaSiftReplayCompatibility>(response.data);
  },

  async runReplay(payload: {
    strategy: string;
    market: string;
    snapshotDate: string;
    maxResults?: number;
  }): Promise<AlphaSiftReplayResponse> {
    const response = await apiClient.post<Record<string, unknown>>('/api/v1/alphasift/replay/run', {
      strategy: payload.strategy,
      market: payload.market,
      snapshot_date: payload.snapshotDate,
      max_results: payload.maxResults ?? 20,
    });
    return toCamelCase<AlphaSiftReplayResponse>(response.data);
  },

  async resolveHistoricalUniverse(payload: {
    market: string;
    snapshotDate: string;
    limit?: number;
  }): Promise<AlphaSiftHistoricalUniverseResponse> {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/alphasift/replay/universe', {
      params: {
        market: payload.market,
        snapshot_date: payload.snapshotDate,
        limit: payload.limit ?? 6000,
      },
    });
    return toCamelCase<AlphaSiftHistoricalUniverseResponse>(response.data);
  },

  async startFullMarketIngestion(payload: {
    market: string;
    snapshotDates: string[];
    batchSize?: number;
  }): Promise<AlphaSiftFullMarketIngestionJob> {
    const response = await apiClient.post<Record<string, unknown>>(
      '/api/v1/alphasift/replay/full-market-ingestion/jobs',
      {
        market: payload.market,
        snapshot_dates: payload.snapshotDates,
        batch_size: payload.batchSize ?? 25,
      },
    );
    return toCamelCase<AlphaSiftFullMarketIngestionJob>(response.data);
  },

  async getFullMarketIngestion(jobId: string): Promise<AlphaSiftFullMarketIngestionJob> {
    const response = await apiClient.get<Record<string, unknown>>(
      `/api/v1/alphasift/replay/full-market-ingestion/jobs/${encodeURIComponent(jobId)}`,
    );
    return toCamelCase<AlphaSiftFullMarketIngestionJob>(response.data);
  },

  async listFullMarketIngestions(limit = 20): Promise<{ items: AlphaSiftFullMarketIngestionJob[]; limit: number }> {
    const response = await apiClient.get<Record<string, unknown>>(
      '/api/v1/alphasift/replay/full-market-ingestion/jobs',
      { params: { limit } },
    );
    return toCamelCase<{ items: AlphaSiftFullMarketIngestionJob[]; limit: number }>(response.data);
  },

  async resumeFullMarketIngestion(jobId: string, force = false): Promise<AlphaSiftFullMarketIngestionJob> {
    const response = await apiClient.post<Record<string, unknown>>(
      `/api/v1/alphasift/replay/full-market-ingestion/jobs/${encodeURIComponent(jobId)}/resume`,
      undefined,
      { params: { force } },
    );
    return toCamelCase<AlphaSiftFullMarketIngestionJob>(response.data);
  },

  async runPortfolioBacktest(payload: {
    strategy: string;
    market: string;
    dateFrom: string;
    dateTo: string;
    topK?: number;
    benchmarkSymbol?: string;
    targetWeights?: Record<string, number>;
    minimumCommission?: number;
    sellTaxBps?: number;
    corporateActions?: AlphaSiftPortfolioCorporateActionInput[];
  }): Promise<AlphaSiftPortfolioBacktestResponse> {
    const response = await apiClient.post<Record<string, unknown>>(
      '/api/v1/alphasift/replay/portfolio-backtest',
      {
        strategy: payload.strategy,
        market: payload.market,
        date_from: payload.dateFrom,
        date_to: payload.dateTo,
        top_k: payload.topK ?? 5,
        final_holding_bars: 20,
        initial_capital: 100000,
        commission_bps: 3,
        minimum_commission: payload.minimumCommission ?? 0,
        sell_tax_bps: payload.sellTaxBps ?? 0,
        slippage_bps: 5,
        benchmark_symbol: payload.benchmarkSymbol || null,
        enforce_tradeability: true,
        accounting_mode: 'cash_ledger',
        target_weights: payload.targetWeights ?? {},
        corporate_actions: (payload.corporateActions ?? []).map((item) => ({
          symbol: item.symbol,
          effective_date: item.effectiveDate,
          action_type: item.actionType,
          cash_dividend_per_share: item.cashDividendPerShare ?? null,
          split_ratio: item.splitRatio ?? null,
        })),
      },
    );
    return toCamelCase<AlphaSiftPortfolioBacktestResponse>(response.data);
  },

  async startFactorIngestion(payload: {
    market: string;
    snapshotDates: string[];
    universe: Array<{ symbol: string; name: string; industry?: string }>;
  }): Promise<AlphaSiftFactorIngestionAccepted> {
    const response = await apiClient.post<Record<string, unknown>>(
      '/api/v1/alphasift/replay/ingestion/tasks',
      {
        market: payload.market,
        snapshot_dates: payload.snapshotDates,
        universe: payload.universe,
      },
    );
    return toCamelCase<AlphaSiftFactorIngestionAccepted>(response.data);
  },

  async getFactorIngestionTask(taskId: string): Promise<AlphaSiftFactorIngestionTask> {
    const response = await apiClient.get<Record<string, unknown>>(
      `/api/v1/alphasift/replay/ingestion/tasks/${encodeURIComponent(taskId)}`,
    );
    return toCamelCase<AlphaSiftFactorIngestionTask>(response.data);
  },

  async getHotspots(payload: { provider?: string; top?: number; refresh?: boolean; includeDetails?: boolean } = {}): Promise<AlphaSiftHotspotsResponse> {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/alphasift/hotspots', {
      params: {
        provider: payload.provider || 'akshare',
        top: payload.top ?? 12,
        refresh: payload.refresh ?? false,
        include_details: payload.includeDetails ?? true,
      },
      timeout: ALPHASIFT_INSTALL_TIMEOUT_MS,
    });
    const normalized = toCamelCase<AlphaSiftHotspotsResponse>(response.data);
    if (normalized.details) {
      const detailsByTopic: Record<string, AlphaSiftHotspotDetail> = {};
      Object.values(normalized.details).forEach((detail) => {
        if (detail?.topic) {
          detailsByTopic[detail.topic] = detail;
        }
      });
      normalized.details = { ...normalized.details, ...detailsByTopic };
    }
    return normalized;
  },

  async getHotspotDetail(payload: { topic: string; provider?: string; refresh?: boolean }): Promise<AlphaSiftHotspotDetail> {
    const response = await apiClient.get<Record<string, unknown>>(
      `/api/v1/alphasift/hotspots/${encodeURIComponent(payload.topic)}`,
      {
        params: { provider: payload.provider || 'akshare', refresh: payload.refresh ?? false },
        timeout: ALPHASIFT_INSTALL_TIMEOUT_MS,
      },
    );
    return toCamelCase<AlphaSiftHotspotDetail>(response.data);
  },

  async install(): Promise<AlphaSiftInstallResponse> {
    const response = await apiClient.post<Record<string, unknown>>('/api/v1/alphasift/install', {}, { timeout: ALPHASIFT_INSTALL_TIMEOUT_MS });
    return toCamelCase<AlphaSiftInstallResponse>(response.data);
  },

  async enable(): Promise<void> {
    await setAlphaSiftEnabled('true');
    try {
      const status = await alphasiftApi.getStatus();
      if (!status.available) {
        const reason = status.diagnostics?.reason ? `（${status.diagnostics.reason}）` : '';
        throw new Error(`AlphaSift 适配层不可用${reason}。请确认后端已安装项目依赖，必要时执行 pip install -r requirements.txt 或重建 Docker/桌面后端。`);
      }
    } catch (error) {
      try {
        await setAlphaSiftEnabled('false');
      } catch {
        // Preserve the original install/status failure for the caller.
      }
      throw error;
    }
  },
};
