import { beforeEach, describe, expect, it, vi } from 'vitest';
import { alphasiftApi } from '../alphasift';

const { get, post, getConfig, updateConfig } = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  getConfig: vi.fn(),
  updateConfig: vi.fn(),
}));

vi.mock('../index', () => ({
  default: {
    get,
    post,
  },
}));

vi.mock('../systemConfig', () => ({
  systemConfigApi: {
    getConfig: (...args: unknown[]) => getConfig(...args),
    update: (...args: unknown[]) => updateConfig(...args),
  },
}));

describe('alphasiftApi', () => {
  beforeEach(() => {
    get.mockReset();
    post.mockReset();
    getConfig.mockReset();
    updateConfig.mockReset();
  });

  it('enables the config and checks bundled AlphaSift availability', async () => {
    getConfig.mockResolvedValueOnce({ configVersion: 'v1', maskToken: '******' });
    updateConfig.mockResolvedValueOnce({ success: true });
    get.mockResolvedValueOnce({
      data: {
        enabled: true,
        available: true,
        install_spec_is_default: true,
      },
    });

    await alphasiftApi.enable();

    expect(updateConfig).toHaveBeenCalledWith({
      configVersion: 'v1',
      maskToken: '******',
      reloadNow: true,
      items: [{ key: 'ALPHASIFT_ENABLED', value: 'true' }],
    });
    expect(get).toHaveBeenCalledWith('/api/v1/alphasift/status');
    expect(updateConfig).toHaveBeenCalledTimes(1);
    expect(post).not.toHaveBeenCalled();
  });

  it('keeps enable behavior when called without object binding', async () => {
    getConfig.mockResolvedValueOnce({ configVersion: 'v1', maskToken: '******' });
    updateConfig.mockResolvedValueOnce({ success: true });
    get.mockResolvedValueOnce({
      data: {
        enabled: true,
        available: true,
        install_spec_is_default: true,
      },
    });

    const enable = alphasiftApi.enable;
    await enable();

    expect(updateConfig).toHaveBeenCalledTimes(1);
    expect(post).not.toHaveBeenCalled();
  });

  it('rolls back ALPHASIFT_ENABLED when bundled AlphaSift is unavailable', async () => {
    getConfig
      .mockResolvedValueOnce({ configVersion: 'v1', maskToken: '******' })
      .mockResolvedValueOnce({ configVersion: 'v2', maskToken: '******' });
    updateConfig.mockResolvedValue({ success: true });
    get.mockResolvedValueOnce({
      data: {
        enabled: true,
        available: false,
        install_spec_is_default: true,
        diagnostics: { reason: 'missing_module' },
      },
    });

    await expect(alphasiftApi.enable()).rejects.toThrow('pip install -r requirements.txt');

    expect(updateConfig).toHaveBeenNthCalledWith(1, {
      configVersion: 'v1',
      maskToken: '******',
      reloadNow: true,
      items: [{ key: 'ALPHASIFT_ENABLED', value: 'true' }],
    });
    expect(updateConfig).toHaveBeenNthCalledWith(2, {
      configVersion: 'v2',
      maskToken: '******',
      reloadNow: true,
      items: [{ key: 'ALPHASIFT_ENABLED', value: 'false' }],
    });
    expect(post).not.toHaveBeenCalled();
  });

  it('loads strategies from the AlphaSift API', async () => {
    get.mockResolvedValueOnce({
      data: {
        enabled: true,
        strategies: [
          {
            id: 'dual_low',
            name: 'Dual Low',
            description: 'value',
            category: 'value',
            market_scope: ['cn'],
          },
        ],
        strategy_count: 1,
      },
    });

    const result = await alphasiftApi.getStrategies();

    expect(get).toHaveBeenCalledWith('/api/v1/alphasift/strategies', { timeout: 300000 });
    expect(result.enabled).toBe(true);
    expect(result.strategyCount).toBe(1);
    expect(result.strategies[0].id).toBe('dual_low');
    expect(result.strategies[0].marketScope).toEqual(['cn']);
  });

  it('loads hotspot themes from the AlphaSift API', async () => {
    get.mockResolvedValueOnce({
      data: {
        enabled: true,
        provider: 'akshare',
        provider_used: 'akshare',
        hotspots: [
          {
            topic: 'AI算力',
            heat_score: 88,
            trend_score: 12,
            sample_stock_count: 8,
            leaders: ['中际旭创'],
          },
        ],
        hotspot_count: 1,
        details: {
          AI绠楀姏: {
            enabled: true,
            provider: 'akshare',
            topic: 'AI绠楀姏',
            route: [{ title: '盘中发酵', description: '事件摘要' }],
            stocks: [],
            stock_count: 0,
          },
        },
      },
    });

    const result = await alphasiftApi.getHotspots({ provider: 'akshare', top: 12, refresh: true });

    expect(get).toHaveBeenCalledWith('/api/v1/alphasift/hotspots', {
      params: { provider: 'akshare', top: 12, refresh: true, include_details: true },
      timeout: 300000,
    });
    expect(result.providerUsed).toBe('akshare');
    expect(result.hotspots[0].heatScore).toBe(88);
    expect(result.hotspots[0].sampleStockCount).toBe(8);
    expect(Object.values(result.details || {})[0]?.stockCount).toBe(0);
  });

  it('keeps prefetched hotspot details addressable by the original topic', async () => {
    get.mockResolvedValueOnce({
      data: {
        enabled: true,
        provider: 'akshare',
        provider_used: 'akshare',
        hotspots: [{ topic: 'Moly Theme', heat_score: 96 }],
        hotspot_count: 1,
        details: {
          moly_theme: {
            enabled: true,
            provider: 'akshare',
            topic: 'Moly Theme',
            route: [{ title: 'catalyst', description: 'summary' }],
            stocks: [],
            stock_count: 0,
          },
        },
      },
    });

    const result = await alphasiftApi.getHotspots({ provider: 'akshare', top: 12, refresh: false });

    expect(result.details?.['Moly Theme']?.stockCount).toBe(0);
  });

  it('loads hotspot detail for a concrete topic', async () => {
    get.mockResolvedValueOnce({
      data: {
        enabled: true,
        provider: 'akshare',
        topic: '玻璃基板',
        summary: '玻璃基板盘中发酵',
        route: [{ title: '盘中发酵', description: '出现大笔买入' }],
        stocks: [{ code: '920438', name: '戈碧迦', role: '异动核心' }],
        leader_stocks: [{ code: '920438', name: '戈碧迦', role: '异动核心' }],
        stock_count: 1,
      },
    });

    const result = await alphasiftApi.getHotspotDetail({ topic: '玻璃基板', provider: 'akshare' });

    expect(get).toHaveBeenCalledWith('/api/v1/alphasift/hotspots/%E7%8E%BB%E7%92%83%E5%9F%BA%E6%9D%BF', {
      params: { provider: 'akshare', refresh: false },
      timeout: 300000,
    });
    expect(result.topic).toBe('玻璃基板');
    expect(result.stockCount).toBe(1);
    expect(result.stocks[0].name).toBe('戈碧迦');
    expect(result.leaderStocks?.[0].name).toBe('戈碧迦');
  });

  it('uses a long timeout for LLM-backed screening', async () => {
    post.mockResolvedValueOnce({
      data: {
        enabled: true,
        candidates: [],
        candidate_count: 0,
        llm_ranked: true,
      },
    });

    await alphasiftApi.screen({ market: 'cn', strategy: 'dual_low', maxResults: 3 });

    expect(post).toHaveBeenCalledWith(
      '/api/v1/alphasift/screen',
      { market: 'cn', strategy: 'dual_low', max_results: 3 },
      { timeout: 180000 }
    );
  });

  it('starts an async screening task', async () => {
    post.mockResolvedValueOnce({
      data: {
        task_id: 'screen-task-1',
        trace_id: 'screen-task-1',
        status: 'pending',
        message: 'AlphaSift 选股任务已提交',
        strategy: 'dual_low',
        market: 'cn',
        max_results: 3,
      },
    });

    const result = await alphasiftApi.startScreen({ market: 'cn', strategy: 'dual_low', maxResults: 3 });

    expect(post).toHaveBeenCalledWith(
      '/api/v1/alphasift/screen/tasks',
      { market: 'cn', strategy: 'dual_low', max_results: 3 }
    );
    expect(result.taskId).toBe('screen-task-1');
    expect(result.maxResults).toBe(3);
  });

  it('loads async screening task status', async () => {
    get.mockResolvedValueOnce({
      data: {
        task_id: 'screen-task-1',
        trace_id: 'screen-task-1',
        status: 'completed',
        progress: 100,
        message: '任务执行完成',
        result: {
          enabled: true,
          candidates: [],
          candidate_count: 0,
          daily_enriched: true,
          daily_enrich_count: 4,
          post_analyzers: ['scorecard'],
        },
      },
    });

    const result = await alphasiftApi.getScreenTask('screen-task-1');

    expect(get).toHaveBeenCalledWith('/api/v1/alphasift/screen/tasks/screen-task-1');
    expect(result.taskId).toBe('screen-task-1');
    expect(result.result?.candidateCount).toBe(0);
    expect(result.result?.dailyEnriched).toBe(true);
    expect(result.result?.dailyEnrichCount).toBe(4);
    expect(result.result?.postAnalyzers).toEqual(['scorecard']);
  });

  it('loads point-in-time replay compatibility', async () => {
    get.mockResolvedValueOnce({
      data: {
        strategy: 'dual_low',
        market: 'cn',
        snapshot_date: '2024-01-05',
        universe_count: 100,
        hard_coverage_ratio: 0.98,
        score_coverage_ratio: 0.9,
      },
    });

    const result = await alphasiftApi.getReplayCompatibility({
      strategy: 'dual_low',
      market: 'cn',
      snapshotDate: '2024-01-05',
    });

    expect(get).toHaveBeenCalledWith('/api/v1/alphasift/replay/compatibility', {
      params: { strategy: 'dual_low', market: 'cn', snapshot_date: '2024-01-05' },
    });
    expect(result.hardCoverageRatio).toBe(0.98);
  });

  it('runs a point-in-time strategy replay', async () => {
    post.mockResolvedValueOnce({
      data: {
        strategy: 'dual_low',
        market: 'cn',
        snapshot_date: '2024-01-05',
        candidate_count: 1,
        candidates: [{ symbol: '600519', screen_score: 88.5 }],
      },
    });

    const result = await alphasiftApi.runReplay({
      strategy: 'dual_low',
      market: 'cn',
      snapshotDate: '2024-01-05',
      maxResults: 10,
    });

    expect(post).toHaveBeenCalledWith('/api/v1/alphasift/replay/run', {
      strategy: 'dual_low',
      market: 'cn',
      snapshot_date: '2024-01-05',
      max_results: 10,
    });
    expect(result.candidates[0].screenScore).toBe(88.5);
  });

  it('runs a cost-aware point-in-time portfolio backtest', async () => {
    post.mockResolvedValueOnce({
      data: {
        strategy: 'dual_low',
        market: 'cn',
        snapshot_count: 2,
        metrics: { total_return_pct: 8, excess_return_pct: 5 },
        periods: [],
      },
    });

    const result = await alphasiftApi.runPortfolioBacktest({
      strategy: 'dual_low',
      market: 'cn',
      dateFrom: '2024-01-01',
      dateTo: '2024-02-01',
      topK: 5,
      benchmarkSymbol: '000300',
      targetWeights: { '600519': 60, '000001': 30 },
      minimumCommission: 5,
      sellTaxBps: 5,
      corporateActions: [{
        symbol: '600519',
        effectiveDate: '2024-01-15',
        actionType: 'cash_dividend',
        cashDividendPerShare: 1.5,
      }],
      includePersistedCorporateActions: false,
    });

    expect(post).toHaveBeenCalledWith('/api/v1/alphasift/replay/portfolio-backtest', {
      strategy: 'dual_low',
      market: 'cn',
      date_from: '2024-01-01',
      date_to: '2024-02-01',
      top_k: 5,
      final_holding_bars: 20,
      initial_capital: 100000,
      commission_bps: 3,
      minimum_commission: 5,
      sell_tax_bps: 5,
      slippage_bps: 5,
      benchmark_symbol: '000300',
      enforce_tradeability: true,
      accounting_mode: 'cash_ledger',
      target_weights: { '600519': 60, '000001': 30 },
      corporate_actions: [{
        symbol: '600519',
        effective_date: '2024-01-15',
        action_type: 'cash_dividend',
        cash_dividend_per_share: 1.5,
        split_ratio: null,
      }],
      include_persisted_corporate_actions: false,
    });
    expect(result.metrics.excessReturnPct).toBe(5);
  });

  it('resolves a dated A-share universe without current-list fallback', async () => {
    get.mockResolvedValueOnce({
      data: {
        market: 'cn',
        snapshot_date: '2024-01-05',
        total_count: 5100,
        returned_count: 5100,
        truncated: false,
        items: [],
        methodology: { uses_current_universe_fallback: false },
      },
    });

    const result = await alphasiftApi.resolveHistoricalUniverse({
      market: 'cn',
      snapshotDate: '2024-01-05',
    });

    expect(get).toHaveBeenCalledWith('/api/v1/alphasift/replay/universe', {
      params: { market: 'cn', snapshot_date: '2024-01-05', limit: 6000 },
    });
    expect(result.totalCount).toBe(5100);
    expect(result.methodology.usesCurrentUniverseFallback).toBe(false);
  });

  it('creates and resumes a checkpointed full-market ingestion job', async () => {
    post
      .mockResolvedValueOnce({ data: { job_id: 'full-job-1', status: 'pending', total_work_items: 5000 } })
      .mockResolvedValueOnce({ data: { job_id: 'full-job-1', status: 'pending', next_offset: 50 } });

    const created = await alphasiftApi.startFullMarketIngestion({
      market: 'cn', snapshotDates: ['2024-01-05'], batchSize: 25,
    });
    const resumed = await alphasiftApi.resumeFullMarketIngestion('full-job-1');

    expect(post).toHaveBeenNthCalledWith(1, '/api/v1/alphasift/replay/full-market-ingestion/jobs', {
      market: 'cn', snapshot_dates: ['2024-01-05'], batch_size: 25,
    });
    expect(post).toHaveBeenNthCalledWith(
      2,
      '/api/v1/alphasift/replay/full-market-ingestion/jobs/full-job-1/resume',
      undefined,
      { params: { force: false } },
    );
    expect(created.totalWorkItems).toBe(5000);
    expect(resumed.nextOffset).toBe(50);
  });

  it('lists persisted full-market ingestion jobs', async () => {
    get.mockResolvedValueOnce({
      data: { items: [{ job_id: 'full-job-1', status: 'failed' }], limit: 1 },
    });

    const result = await alphasiftApi.listFullMarketIngestions(1);

    expect(get).toHaveBeenCalledWith(
      '/api/v1/alphasift/replay/full-market-ingestion/jobs',
      { params: { limit: 1 } },
    );
    expect(result.items[0].jobId).toBe('full-job-1');
  });

  it('starts and checks a historical factor ingestion task', async () => {
    post.mockResolvedValueOnce({
      data: {
        task_id: 'factor-task-1',
        trace_id: 'factor-task-1',
        status: 'pending',
        snapshot_count: 1,
        symbol_count: 1,
      },
    });
    get.mockResolvedValueOnce({
      data: {
        task_id: 'factor-task-1',
        status: 'completed',
        result: { row_count: 1, error_count: 0 },
      },
    });

    const accepted = await alphasiftApi.startFactorIngestion({
      market: 'cn',
      snapshotDates: ['2024-01-05'],
      universe: [{ symbol: '600519', name: '贵州茅台' }],
    });
    const task = await alphasiftApi.getFactorIngestionTask(accepted.taskId);

    expect(post).toHaveBeenCalledWith('/api/v1/alphasift/replay/ingestion/tasks', {
      market: 'cn',
      snapshot_dates: ['2024-01-05'],
      universe: [{ symbol: '600519', name: '贵州茅台' }],
    });
    expect(get).toHaveBeenCalledWith('/api/v1/alphasift/replay/ingestion/tasks/factor-task-1');
    expect(task.result?.rowCount).toBe(1);
  });
});
