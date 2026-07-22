import { beforeEach, describe, expect, it, vi } from 'vitest';
import { vnpyPaperTradingApi } from '../vnpyPaperTrading';

const get = vi.hoisted(() => vi.fn());
const post = vi.hoisted(() => vi.fn());
const put = vi.hoisted(() => vi.fn());

vi.mock('../index', () => ({
  default: { get, post, put },
}));

describe('vnpyPaperTradingApi', () => {
  beforeEach(() => {
    get.mockReset();
    post.mockReset();
    put.mockReset();
  });

  it('loads status and camelCases nested fields', async () => {
    get.mockResolvedValueOnce({
      data: {
        available: true,
        enabled: true,
        vnpy_available: false,
        engine: 'vnpy_local_paper_ledger',
        mode: 'local_paper',
        settings: {
          enabled: true,
          account_id: 1,
          initial_cash: 100000,
          auto_trade_enabled: true,
          auto_strategy: 'dual_low',
          auto_market: 'cn',
          auto_max_results: 3,
          auto_cash_per_order: 10000,
          auto_score_weighted_allocation_enabled: true,
          auto_allocation_budget: 25000,
          auto_allocation_method: 'score_inverse_volatility_20d',
          auto_risk_volatility_floor_pct: 7.5,
          auto_correlation_lookback_days: 90,
          auto_correlation_min_observations: 30,
          auto_max_pairwise_correlation: 0.75,
          auto_covariance_risk_penalty: 0.4,
          auto_interval_minutes: 1440,
          auto_min_score: null,
          auto_skip_existing_positions: true,
          auto_execution_mode: 'paper',
          auto_max_positions: 10,
          auto_max_single_position_value: 20000,
          auto_max_total_position_value: 80000,
          auto_max_total_position_pct: 80,
          auto_max_industry_position_value: 40000,
          auto_max_industry_position_pct: 40,
          auto_target_position_weights: { '600519': 2.5 },
          auto_target_industry_weights: { 白酒: 8 },
          auto_daily_max_orders: null,
          auto_daily_budget: null,
          auto_trade_time_gate_enabled: true,
          auto_symbol_blacklist: ['600519'],
          auto_exclude_st: true,
          auto_exclude_suspended: true,
          auto_exclude_price_limit: true,
          auto_min_turnover: 100000000,
          auto_min_data_quality_score: 72.5,
          auto_cross_run_quality_gate_enabled: true,
          auto_cross_run_horizon_days: 10,
          auto_cross_run_min_mature_samples: 20,
          auto_cross_run_min_win_rate_pct: 48,
          auto_cross_run_max_decisions: 300,
          auto_min_cash_balance: 5000,
          auto_max_drawdown_pct: 12,
          auto_drawdown_recovery_hysteresis_pct: 2,
          auto_consecutive_loss_limit: 3,
          auto_consecutive_loss_cooldown_minutes: 120,
          auto_market_light_gate_enabled: true,
          auto_market_light_block_statuses: ['red', 'yellow'],
          auto_market_context_max_age_days: 5,
          auto_market_breadth_gate_enabled: true,
          auto_market_breadth_min_score: 42,
          auto_hotspot_retreat_gate_enabled: true,
          auto_hotspot_retreat_min_drop: 30,
          auto_intraday_market_gate_enabled: true,
          auto_intraday_require_provider_timestamp: true,
          auto_intraday_index_min_change_pct: -1.5,
          auto_intraday_breadth_min_score: 45,
          auto_cross_market_gate_enabled: true,
          auto_cross_market_min_change_pct: -2.5,
          auto_failure_fuse_enabled: true,
          auto_failure_fuse_threshold: 2,
          auto_failure_fuse_auto_recovery_enabled: true,
          auto_failure_fuse_cooldown_minutes: 60,
          auto_sell_enabled: false,
          auto_stop_loss_pct: null,
          auto_take_profit_pct: null,
          auto_trailing_stop_pct: null,
          auto_max_holding_days: null,
          auto_sell_position_pct: 50,
          auto_signal_exit_enabled: true,
          auto_no_progress_days: 5,
          auto_no_progress_min_return_pct: 1,
          auto_rebalance_enabled: true,
          auto_llm_plan_enabled: true,
          auto_llm_review_enabled: true,
          vnpy_gateway_name: 'SIM',
        },
        account: { id: 1, name: 'vn.py 模拟交易', base_currency: 'CNY' },
        snapshot: { total_cash: 99000, accounts: [{ account_id: 1, total_cash: 99000 }] },
        recent_trades: [{ id: 1, trade_date: '2026-07-01', symbol: 'SH600519' }],
        diagnostics: {
          vnpy_adapter: {
            order_request_supported: false,
            mode: 'local_paper_fallback',
          },
          vnpy_bridge: {
            available: false,
            mode: 'not_configured',
            reason: 'main_engine_not_configured',
          },
        },
        scheduler: {
          enabled: true,
          running: false,
          schedule_times: ['18:00'],
          next_run_at: '2026-07-02T09:30:00',
          last_error: null,
          background_tasks: [{
            name: 'vnpy_paper_auto_trade',
            interval_seconds: 300,
            initial_delay_seconds: 120,
            running: false,
            last_run: null,
            next_run_at: '2026-07-02T09:35:00',
          }],
        },
      },
    });

    const result = await vnpyPaperTradingApi.getStatus();

    expect(get).toHaveBeenCalledWith('/api/v1/vnpy-paper/status');
    expect(result.available).toBe(true);
    expect(result.vnpyAvailable).toBe(false);
    expect(result.mode).toBe('local_paper');
    expect(result.settings.autoTradeEnabled).toBe(true);
    expect(result.settings.autoCashPerOrder).toBe(10000);
    expect(result.settings.autoScoreWeightedAllocationEnabled).toBe(true);
    expect(result.settings.autoAllocationBudget).toBe(25000);
    expect(result.settings.autoAllocationMethod).toBe('score_inverse_volatility_20d');
    expect(result.settings.autoRiskVolatilityFloorPct).toBe(7.5);
    expect(result.settings.autoCorrelationLookbackDays).toBe(90);
    expect(result.settings.autoCorrelationMinObservations).toBe(30);
    expect(result.settings.autoMaxPairwiseCorrelation).toBe(0.75);
    expect(result.settings.autoCovarianceRiskPenalty).toBe(0.4);
    expect(result.settings.autoExecutionMode).toBe('paper');
    expect(result.settings.autoMaxSinglePositionValue).toBe(20000);
    expect(result.settings.autoMaxTotalPositionValue).toBe(80000);
    expect(result.settings.autoMaxTotalPositionPct).toBe(80);
    expect(result.settings.autoMaxIndustryPositionValue).toBe(40000);
    expect(result.settings.autoMaxIndustryPositionPct).toBe(40);
    expect(result.settings.autoMinDataQualityScore).toBe(72.5);
    expect(result.settings.autoCrossRunQualityGateEnabled).toBe(true);
    expect(result.settings.autoCrossRunHorizonDays).toBe(10);
    expect(result.settings.autoCrossRunMinMatureSamples).toBe(20);
    expect(result.settings.autoCrossRunMinWinRatePct).toBe(48);
    expect(result.settings.autoCrossRunMaxDecisions).toBe(300);
    expect(result.settings.autoTargetPositionWeights).toEqual({ '600519': 2.5 });
    expect(result.settings.autoTargetIndustryWeights).toEqual({ 白酒: 8 });
    expect(result.settings.autoSymbolBlacklist).toEqual(['600519']);
    expect(result.settings.autoExcludeSt).toBe(true);
    expect(result.settings.autoMinTurnover).toBe(100000000);
    expect(result.settings.autoMinCashBalance).toBe(5000);
    expect(result.settings.autoMaxDrawdownPct).toBe(12);
    expect(result.settings.autoDrawdownRecoveryHysteresisPct).toBe(2);
    expect(result.settings.autoConsecutiveLossLimit).toBe(3);
    expect(result.settings.autoConsecutiveLossCooldownMinutes).toBe(120);
    expect(result.settings.autoMarketLightGateEnabled).toBe(true);
    expect(result.settings.autoMarketLightBlockStatuses).toEqual(['red', 'yellow']);
    expect(result.settings.autoMarketContextMaxAgeDays).toBe(5);
    expect(result.settings.autoMarketBreadthGateEnabled).toBe(true);
    expect(result.settings.autoMarketBreadthMinScore).toBe(42);
    expect(result.settings.autoHotspotRetreatGateEnabled).toBe(true);
    expect(result.settings.autoHotspotRetreatMinDrop).toBe(30);
    expect(result.settings.autoIntradayMarketGateEnabled).toBe(true);
    expect(result.settings.autoIntradayRequireProviderTimestamp).toBe(true);
    expect(result.settings.autoIntradayIndexMinChangePct).toBe(-1.5);
    expect(result.settings.autoIntradayBreadthMinScore).toBe(45);
    expect(result.settings.autoCrossMarketGateEnabled).toBe(true);
    expect(result.settings.autoCrossMarketMinChangePct).toBe(-2.5);
    expect(result.settings.autoFailureFuseEnabled).toBe(true);
    expect(result.settings.autoFailureFuseThreshold).toBe(2);
    expect(result.settings.autoFailureFuseAutoRecoveryEnabled).toBe(true);
    expect(result.settings.autoFailureFuseCooldownMinutes).toBe(60);
    expect(result.settings.autoSellPositionPct).toBe(50);
    expect(result.settings.autoSignalExitEnabled).toBe(true);
    expect(result.settings.autoNoProgressDays).toBe(5);
    expect(result.settings.autoNoProgressMinReturnPct).toBe(1);
    expect(result.settings.autoRebalanceEnabled).toBe(true);
    expect(result.settings.autoLlmPlanEnabled).toBe(true);
    expect(result.settings.autoLlmReviewEnabled).toBe(true);
    expect(result.settings.vnpyGatewayName).toBe('SIM');
    expect(result.account?.baseCurrency).toBe('CNY');
    expect(result.snapshot?.totalCash).toBe(99000);
    expect(result.recentTrades[0].tradeDate).toBe('2026-07-01');
    expect(result.diagnostics?.vnpyAdapter).toMatchObject({
      orderRequestSupported: false,
      mode: 'local_paper_fallback',
    });
    expect(result.diagnostics?.vnpyBridge).toMatchObject({
      available: false,
      mode: 'not_configured',
      reason: 'main_engine_not_configured',
    });
    expect(result.scheduler?.nextRunAt).toBe('2026-07-02T09:30:00');
    expect(result.scheduler?.backgroundTasks[0].intervalSeconds).toBe(300);
    expect(result.scheduler?.backgroundTasks[0].initialDelaySeconds).toBe(120);
    expect(result.scheduler?.backgroundTasks[0].nextRunAt).toBe('2026-07-02T09:35:00');
  });

  it('passes lightweight status options as query params', async () => {
    get.mockResolvedValueOnce({
      data: {
        available: true,
        enabled: true,
        vnpy_available: false,
        engine: 'vnpy_local_paper_ledger',
        mode: 'local_paper',
        settings: { enabled: true },
        recent_trades: [],
        diagnostics: {
          detail_level: 'summary',
          snapshot_requested: false,
          recent_trades_requested: false,
        },
      },
    });

    const result = await vnpyPaperTradingApi.getStatus({
      includeSnapshot: false,
      includeRecentTrades: false,
    });

    expect(get).toHaveBeenCalledWith('/api/v1/vnpy-paper/status', {
      params: {
        include_snapshot: false,
        include_recent_trades: false,
      },
    });
    expect(result.diagnostics?.detailLevel).toBe('summary');
  });

  it('loads local paper accounts including archived ledgers', async () => {
    get.mockResolvedValueOnce({
      data: {
        items: [{
          id: 2,
          name: 'vn.py paper',
          broker: 'vnpy_paper',
          market: 'cn',
          base_currency: 'CNY',
          is_active: true,
          is_current: true,
          archived: false,
          created_at: '2026-07-01T09:00:00',
          updated_at: '2026-07-02T09:00:00',
        }, {
          id: 1,
          name: 'vn.py paper old',
          broker: 'vnpy_paper',
          market: 'cn',
          base_currency: 'CNY',
          is_active: false,
          is_current: false,
          archived: true,
          created_at: '2026-06-30T09:00:00',
          updated_at: '2026-07-01T09:00:00',
        }],
        count: 2,
        current_account_id: 2,
        hidden_count: 1,
      },
    });

    const result = await vnpyPaperTradingApi.listAccounts(true, true);

    expect(get).toHaveBeenCalledWith('/api/v1/vnpy-paper/accounts', {
      params: { include_inactive: true, include_hidden: true },
    });
    expect(result.currentAccountId).toBe(2);
    expect(result.hiddenCount).toBe(1);
    expect(result.items[0]).toMatchObject({
      id: 2,
      baseCurrency: 'CNY',
      isActive: true,
      isCurrent: true,
      archived: false,
    });
    expect(result.items[1]).toMatchObject({
      id: 1,
      isActive: false,
      isCurrent: false,
      archived: true,
    });
  });

  it('cleans up archived paper accounts without deleting ledgers', async () => {
    post.mockResolvedValueOnce({
      data: {
        cleaned_account_ids: [1],
        skipped: [],
        dry_run: false,
        hidden_count_before: 0,
        hidden_count_after: 1,
        remaining_count: 1,
        accounts: {
          items: [{ id: 2, broker: 'vnpy_paper', is_current: true, archived: false }],
          count: 1,
          current_account_id: 2,
          hidden_count: 1,
        },
      },
    });

    const result = await vnpyPaperTradingApi.cleanupArchivedAccounts([1], { includeHidden: false });

    expect(post).toHaveBeenCalledWith('/api/v1/vnpy-paper/accounts/archived/cleanup', {
      account_ids: [1],
      dry_run: false,
      include_hidden: false,
    });
    expect(result.cleanedAccountIds).toEqual([1]);
    expect(result.accounts.hiddenCount).toBe(1);
    expect(result.accounts.items).toHaveLength(1);
  });

  it('restores an archived paper account with status query params', async () => {
    post.mockResolvedValueOnce({
      data: {
        available: true,
        enabled: true,
        vnpy_available: false,
        engine: 'vnpy_local_paper_ledger',
        mode: 'local_paper',
        settings: { enabled: true, account_id: 1 },
        account: { id: 1, broker: 'vnpy_paper' },
        recent_trades: [],
        diagnostics: {
          account_restore: {
            restored_account_id: 1,
            previous_account_id: 2,
            deactivated_account_ids: [2],
          },
        },
      },
    });

    const result = await vnpyPaperTradingApi.restoreAccount(1, {
      includeSnapshot: false,
      includeRecentTrades: false,
    });

    expect(post).toHaveBeenCalledWith('/api/v1/vnpy-paper/accounts/1/restore', undefined, {
      params: {
        include_snapshot: false,
        include_recent_trades: false,
      },
    });
    expect(result.account?.id).toBe(1);
    expect(result.diagnostics?.accountRestore).toMatchObject({
      restoredAccountId: 1,
      previousAccountId: 2,
      deactivatedAccountIds: [2],
    });
  });

  it('resets paper account with status query params', async () => {
    post.mockResolvedValueOnce({
      data: {
        available: true,
        enabled: true,
        vnpy_available: false,
        engine: 'vnpy_local_paper_ledger',
        mode: 'local_paper',
        settings: { enabled: true, account_id: 2 },
        account: { id: 2, broker: 'vnpy_paper' },
        recent_trades: [],
        diagnostics: {
          account_reset: {
            archived_account_id: 1,
            new_account_id: 2,
          },
        },
      },
    });

    const result = await vnpyPaperTradingApi.resetAccount({
      includeSnapshot: false,
      includeRecentTrades: false,
    });

    expect(post).toHaveBeenCalledWith('/api/v1/vnpy-paper/account/reset', undefined, {
      params: {
        include_snapshot: false,
        include_recent_trades: false,
      },
    });
    expect(result.account?.id).toBe(2);
    expect(result.diagnostics?.accountReset).toMatchObject({
      archivedAccountId: 1,
      newAccountId: 2,
    });
  });

  it('loads paper trading performance summary', async () => {
    get.mockResolvedValueOnce({
      data: {
        account: { id: 1, broker: 'vnpy_paper' },
        initial_cash: 100000,
        total_equity: 101000,
        total_pnl: 1000,
        return_pct: 1,
        run_window: { run_count: 2 },
        agent: {
          candidate_count: 3,
          submitted_count: 1,
          fill_rate_pct: 50,
        },
        trade_metrics: {
          trade_count: 2,
          buy_count: 1,
          sell_count: 1,
          gross_turnover: 2200,
          win_rate_pct: 100,
          average_sell_return_pct: 20,
        },
        risk_metrics: {
          max_drawdown_pct: 3.5,
          turnover_pct: 2.2,
          current_exposure_pct: 20,
        },
        equity_curve: [{ source: 'snapshot', equity: 101000 }],
        daily_returns: [{
          date: '2026-07-02',
          equity: 101000,
          daily_pnl: 1000,
          daily_return_pct: 1,
          cumulative_return_pct: 1,
          drawdown_pct: 0,
          trade_count: 2,
        }],
        monthly_returns: [{
          month: '2026-07',
          start_equity: 100000,
          end_equity: 101000,
          monthly_pnl: 1000,
          monthly_return_pct: 1,
          cumulative_return_pct: 1,
          drawdown_pct: 0,
          trade_count: 2,
          positive_days: 1,
          negative_days: 0,
        }],
        strategy_attribution: [{
          key: 'dual_low',
          run_count: 2,
          filled_plan_count: 1,
          filled_cash_amount: 1000,
          fill_rate_pct: 100,
        }],
        industry_attribution: [{
          key: '白酒',
          decision_count: 1,
          filled_count: 1,
          filled_cash_amount: 1000,
          fill_rate_pct: 100,
        }],
        status_counts: { completed: 2 },
        execution_mode_counts: { paper: 2 },
        data_quality_counts: { ok: 2 },
        trade_plan_status_counts: { filled: 1, skipped: 1 },
        skip_reason_counts: { position_exists: 1 },
        top_skip_reasons: [{ key: 'position_exists', count: 1 }],
        traded_symbols: [{ key: '600519', count: 1 }],
      },
    });

    const result = await vnpyPaperTradingApi.getPerformance(10, {
      createdFrom: '2026-07-01T09:00',
      createdTo: '2026-07-02T15:00',
    });

    expect(get).toHaveBeenCalledWith('/api/v1/vnpy-paper/performance', {
      params: {
        run_limit: 10,
        created_from: '2026-07-01T09:00',
        created_to: '2026-07-02T15:00',
      },
    });
    expect(result.totalEquity).toBe(101000);
    expect(result.returnPct).toBe(1);
    expect(result.agent.candidateCount).toBe(3);
    expect(result.tradeMetrics.grossTurnover).toBe(2200);
    expect(result.tradeMetrics.winRatePct).toBe(100);
    expect(result.riskMetrics?.maxDrawdownPct).toBe(3.5);
    expect(result.riskMetrics?.turnoverPct).toBe(2.2);
    expect(result.equityCurve?.[0].source).toBe('snapshot');
    expect(result.dailyReturns?.[0].dailyReturnPct).toBe(1);
    expect(result.dailyReturns?.[0].tradeCount).toBe(2);
    expect(result.monthlyReturns?.[0].monthlyReturnPct).toBe(1);
    expect(result.monthlyReturns?.[0].positiveDays).toBe(1);
    expect(result.strategyAttribution?.[0].key).toBe('dual_low');
    expect(result.industryAttribution?.[0].key).toBe('白酒');
    expect(result.tradePlanStatusCounts.filled).toBe(1);
    expect(result.topSkipReasons[0]).toEqual({ key: 'position_exists', count: 1 });
  });

  it('loads trade plan recovery summary', async () => {
    get.mockResolvedValueOnce({
      data: {
        generated_at: '2026-07-02T00:00:00Z',
        limit: 100,
        total: 2,
        scanned_count: 2,
        include_terminal: false,
        timeout_seconds: 1800,
        retry_cooldown_seconds: 60,
        retry_max_attempts: 3,
        active_statuses: ['cancel_requested', 'part_filled', 'submitted'],
        status_counts: { submitted: 1, skipped: 1 },
        execution_mode_counts: { vnpy_paper: 1, paper: 1 },
        side_counts: { buy: 2 },
        recovery_counts: { stale_active: 1, retry_due: 1 },
        stale_active_count: 1,
        cancellable_count: 1,
        retry_due_count: 1,
        retry_cooldown_count: 0,
        retry_limit_count: 0,
        not_retryable_count: 0,
        items: [{
          plan_uid: 'plan-1',
          symbol: '600519',
          status: 'submitted',
          execution_mode: 'vnpy_paper',
          stale_active: true,
          recovery_state: 'stale_active',
          age_minutes: 45,
        }],
      },
    });

    const result = await vnpyPaperTradingApi.getTradePlanRecoverySummary(25, true);

    expect(get).toHaveBeenCalledWith('/api/v1/vnpy-paper/trade-plans/recovery-summary', {
      params: { limit: 25, include_terminal: true },
    });
    expect(result.scannedCount).toBe(2);
    expect(result.staleActiveCount).toBe(1);
    expect(result.items[0].planUid).toBe('plan-1');
    expect(result.items[0].recoveryState).toBe('stale_active');
  });

  it('loads task health summary', async () => {
    get.mockResolvedValueOnce({
      data: {
        generated_at: '2026-07-02T09:32:00Z',
        overall_health: 'warning',
        scheduler_enabled: true,
        scheduler_running: false,
        scheduler_loop_running: true,
        paper_trading_enabled: true,
        auto_trade_enabled: true,
        auto_execution_mode: 'paper',
        summary: { healthy: 1, warning: 1, error: 0, disabled: 0, total: 2 },
        items: [{
          name: 'vnpy_paper_auto_trade',
          label: '定时自动买入',
          health: 'warning',
          reason: 'auto_trade_disabled',
          registered: true,
          running: false,
          required: true,
          interval_seconds: 300,
          next_run_at: '2026-07-02T09:35:00',
          last_event_status: 'skipped',
          last_event_at: '2026-07-02T09:31:00',
          duration_seconds: 0.12,
          details: { reason: 'auto_trade_disabled' },
        }],
      },
    });

    const result = await vnpyPaperTradingApi.getTaskHealth();

    expect(get).toHaveBeenCalledWith('/api/v1/vnpy-paper/task-health');
    expect(result.overallHealth).toBe('warning');
    expect(result.schedulerEnabled).toBe(true);
    expect(result.schedulerLoopRunning).toBe(true);
    expect(result.summary.total).toBe(2);
    expect(result.items[0]).toMatchObject({
      name: 'vnpy_paper_auto_trade',
      intervalSeconds: 300,
      lastEventStatus: 'skipped',
    });
    expect(result.items[0].details?.reason).toBe('auto_trade_disabled');
  });

  it('loads filtered task events', async () => {
    get.mockResolvedValueOnce({
      data: {
        generated_at: '2026-07-02T09:33:00Z',
        limit: 10,
        name: 'vnpy_paper_auto_retry',
        status: 'failed',
        count: 1,
        items: [{
          name: 'vnpy_paper_auto_retry',
          status: 'failed',
          message: 'boom',
          timestamp: '2026-07-02T09:32:00',
          duration_seconds: 0.1,
          details: { error: 'boom' },
        }],
      },
    });

    const result = await vnpyPaperTradingApi.getTaskEvents(10, {
      name: 'vnpy_paper_auto_retry',
      status: 'failed',
    });

    expect(get).toHaveBeenCalledWith('/api/v1/vnpy-paper/task-events', {
      params: {
        limit: 10,
        name: 'vnpy_paper_auto_retry',
        status: 'failed',
      },
    });
    expect(result.count).toBe(1);
    expect(result.items[0].durationSeconds).toBe(0.1);
    expect(result.items[0].details?.error).toBe('boom');
  });

  it('loads task event summary', async () => {
    get.mockResolvedValueOnce({
      data: {
        generated_at: '2026-07-02T09:34:00Z',
        limit: 100,
        count: 3,
        status_counts: {
          completed: 1,
          skipped: 1,
          failed: 1,
        },
        items: [{
          name: 'vnpy_paper_auto_retry',
          label: '自动恢复扫描',
          total: 1,
          started_count: 0,
          completed_count: 0,
          skipped_count: 0,
          failed_count: 1,
          failure_rate_pct: 100,
          avg_duration_seconds: 0.3,
          last_event_status: 'failed',
          last_event_at: '2026-07-02T09:33:00',
          last_event_message: 'boom',
          last_failed_at: '2026-07-02T09:33:00',
          last_skipped_at: null,
        }],
      },
    });

    const result = await vnpyPaperTradingApi.getTaskEventSummary(100);

    expect(get).toHaveBeenCalledWith('/api/v1/vnpy-paper/task-event-summary', {
      params: { limit: 100 },
    });
    expect(result.count).toBe(3);
    expect(result.statusCounts.failed).toBe(1);
    expect(result.items[0]).toMatchObject({
      name: 'vnpy_paper_auto_retry',
      failedCount: 1,
      failureRatePct: 100,
      avgDurationSeconds: 0.3,
      lastFailedAt: '2026-07-02T09:33:00',
    });
  });

  it('loads long-window task metrics and camelCases daily results', async () => {
    get.mockResolvedValueOnce({
      data: {
        generated_at: '2026-07-13T10:00:00Z',
        window_days: 30,
        window_started_at: '2026-06-13T10:00:00',
        window_ended_at: '2026-07-13T10:00:00',
        event_count: 6,
        run_count: 3,
        started_count: 3,
        completed_count: 2,
        skipped_count: 0,
        failed_count: 1,
        success_rate_pct: 66.67,
        skip_rate_pct: 0,
        failure_rate_pct: 33.33,
        avg_duration_seconds: 0.2,
        p95_duration_seconds: 0.3,
        current_failure_streak: 1,
        truncated: false,
        items: [{
          name: 'vnpy_paper_auto_trade',
          run_count: 3,
          completed_count: 2,
          skipped_count: 0,
          failed_count: 1,
          success_rate_pct: 66.67,
          skip_rate_pct: 0,
          failure_rate_pct: 33.33,
        }],
        daily: [{
          date: '2026-07-13',
          run_count: 1,
          completed_count: 0,
          skipped_count: 0,
          failed_count: 1,
          success_rate_pct: 0,
          failure_rate_pct: 100,
        }],
      },
    });

    const result = await vnpyPaperTradingApi.getTaskMetrics(30);

    expect(get).toHaveBeenCalledWith('/api/v1/vnpy-paper/task-metrics', {
      params: { days: 30 },
    });
    expect(result).toMatchObject({
      windowDays: 30,
      runCount: 3,
      successRatePct: 66.67,
      currentFailureStreak: 1,
    });
    expect(result.daily[0]).toMatchObject({ date: '2026-07-13', failureRatePct: 100 });
  });

  it('runs trade plan recovery scans', async () => {
    post.mockResolvedValueOnce({
      data: {
        accepted: true,
        skipped: false,
        expired_count: 1,
        reconciled_count: 2,
        protected_count: 3,
        reconciliation_failed_count: 1,
        scanned_count: 3,
        attempted_count: 2,
        submitted_count: 1,
        skipped_count: 1,
        failed_count: 0,
        orders: [],
        messages: ['vnpy_order_timeout:plan-1'],
      },
    });

    const result = await vnpyPaperTradingApi.runTradePlanRecovery(4, 80);

    expect(post).toHaveBeenCalledWith(
      '/api/v1/vnpy-paper/trade-plans/recovery/run',
      undefined,
      {
        params: { max_plans: 4, scan_limit: 80 },
      },
    );
    expect(result.expiredCount).toBe(1);
    expect(result.reconciledCount).toBe(2);
    expect(result.protectedCount).toBe(3);
    expect(result.reconciliationFailedCount).toBe(1);
    expect(result.attemptedCount).toBe(2);
    expect(result.messages).toEqual(['vnpy_order_timeout:plan-1']);
  });

  it('exports recent agent runs with detail flag', async () => {
    get.mockResolvedValueOnce({
      data: {
        generated_at: '2026-07-02T00:00:00Z',
        limit: 50,
        include_details: true,
        count: 1,
        items: [{
          run_uid: 'ss-agent-test',
          trigger_source: 'vnpy_paper_auto',
          status: 'completed',
          strategy: 'dual_low',
          market: 'cn',
          decisions: [],
          trade_plans: [],
          timeline: [],
        }],
      },
    });

    const result = await vnpyPaperTradingApi.exportAgentRuns(50, true);

    expect(get).toHaveBeenCalledWith('/api/v1/vnpy-paper/agent-runs/export', {
      params: {
        limit: 50,
        include_details: true,
      },
    });
    expect(result.generatedAt).toBe('2026-07-02T00:00:00Z');
    expect(result.includeDetails).toBe(true);
    expect(result.items[0]).toMatchObject({ runUid: 'ss-agent-test' });
  });

  it('passes agent run filters to list and export endpoints', async () => {
    get
      .mockResolvedValueOnce({ data: { items: [], limit: 10, offset: 0, total: 0 } })
      .mockResolvedValueOnce({
        data: {
          generated_at: '2026-07-02T00:00:00Z',
          limit: 50,
          include_details: false,
          count: 0,
          items: [],
        },
      });

    await vnpyPaperTradingApi.listAgentRuns(10, 0, {
      triggerSource: 'vnpy_paper_auto',
      strategy: 'dual_low',
      market: 'cn',
      status: 'completed',
      createdFrom: '2026-07-01T09:00',
      createdTo: '2026-07-01T15:00',
    });
    await vnpyPaperTradingApi.exportAgentRuns(50, false, {
      strategy: 'dual_low',
      market: 'cn',
      status: 'completed',
      createdFrom: '2026-07-01T09:00',
      createdTo: '2026-07-01T15:00',
    });

    expect(get).toHaveBeenNthCalledWith(1, '/api/v1/vnpy-paper/agent-runs', {
      params: {
        limit: 10,
        offset: 0,
        trigger_source: 'vnpy_paper_auto',
        strategy: 'dual_low',
        market: 'cn',
        status: 'completed',
        created_from: '2026-07-01T09:00',
        created_to: '2026-07-01T15:00',
      },
    });
    expect(get).toHaveBeenNthCalledWith(2, '/api/v1/vnpy-paper/agent-runs/export', {
      params: {
        limit: 50,
        strategy: 'dual_low',
        market: 'cn',
        status: 'completed',
        created_from: '2026-07-01T09:00',
        created_to: '2026-07-01T15:00',
        include_details: false,
      },
    });
  });

  it('loads Agent daily summary with snake_case filters', async () => {
    get.mockResolvedValueOnce({
      data: {
        date: '2026-07-01',
        generated_at: '2026-07-02T00:00:00Z',
        limit: 100,
        total: 2,
        scanned_count: 2,
        run_count: 2,
        health: 'warning',
        candidate_count: 3,
        planned_count: 2,
        submitted_count: 1,
        skipped_count: 1,
        message_count: 0,
        status_counts: { completed: 1, failed: 1 },
        strategy_counts: { dual_low: 2 },
        market_counts: { cn: 2 },
        execution_mode_counts: { paper: 2 },
        data_quality_counts: { partial: 1, stale: 1 },
        agent_review_counts: { passed: 1, warning: 1 },
        llm_review_counts: { passed: 1, blocked: 1 },
        review_quality_counts: { audited: 1, guarded: 1 },
        review_quality_flag_counts: { llm_review_blocked: 1 },
        review_quality_score_avg: 92.5,
        workflow_status_counts: { executed: 1, skipped: 1 },
        workflow_stage_counts: { execution: 1, candidate_review: 1 },
        trade_plan_status_counts: { filled: 1 },
        side_counts: { buy: 1 },
        top_skip_reasons: [{ reason: 'position_exists', count: 1 }],
        top_symbols: [{ symbol: '600519', count: 1 }],
        latest_run: null,
        filters: { strategy: 'dual_low' },
      },
    });

    const result = await vnpyPaperTradingApi.getAgentDailySummary('2026-07-01', {
      strategy: 'dual_low',
      market: 'cn',
      status: 'completed',
    });

    expect(get).toHaveBeenCalledWith('/api/v1/vnpy-paper/agent-runs/daily-summary', {
      params: {
        limit: 100,
        strategy: 'dual_low',
        market: 'cn',
        status: 'completed',
        date: '2026-07-01',
      },
    });
    expect(result.scannedCount).toBe(2);
    expect(result.agentReviewCounts).toEqual({ passed: 1, warning: 1 });
    expect(result.llmReviewCounts).toEqual({ passed: 1, blocked: 1 });
    expect(result.reviewQualityCounts).toEqual({ audited: 1, guarded: 1 });
    expect(result.reviewQualityFlagCounts).toEqual({ llmReviewBlocked: 1 });
    expect(result.reviewQualityScoreAvg).toBe(92.5);
    expect(result.workflowStatusCounts).toEqual({ executed: 1, skipped: 1 });
    expect(result.workflowStageCounts).toEqual({ execution: 1, candidateReview: 1 });
    expect(result.topSkipReasons[0].reason).toBe('position_exists');
    expect(result.topSymbols[0].symbol).toBe('600519');
  });

  it('loads cross-run Agent data quality trends', async () => {
    get.mockResolvedValueOnce({
      data: {
        generated_at: '2026-07-13T16:00:00',
        window_days: 30,
        total: 3,
        scanned_count: 3,
        known_count: 2,
        quality_counts: { ok: 1, partial: 1, unknown: 1 },
        degraded_count: 1,
        degraded_rate_pct: 33.33,
        health: 'warning',
        latest_quality: 'partial',
        warning_counts: { daily_source_fallback: 1 },
        source_error_counts: { snapshot_timeout: 1 },
        source_health_items: [{
          key: 'snapshot/sina',
          group: 'snapshot',
          source: 'sina',
          observation_count: 3,
          degraded_observation_count: 2,
          degraded_rate_pct: 66.67,
          max_failures: 2,
          latest_status: 'degraded',
          latest_failures: 1,
          last_observed_at: '2026-07-13T16:00:00',
        }],
        truncated: false,
        daily: [{
          date: '2026-07-13',
          run_count: 3,
          quality_counts: { ok: 1, partial: 1, unknown: 1 },
          degraded_count: 1,
          degraded_rate_pct: 33.33,
        }],
        filters: { strategy: 'dual_low' },
      },
    });

    const result = await vnpyPaperTradingApi.getAgentDataQualityTrends(30, {
      strategy: 'dual_low',
      market: 'cn',
      status: 'completed',
      createdFrom: '2026-07-01T09:00',
    });

    expect(get).toHaveBeenCalledWith('/api/v1/vnpy-paper/agent-runs/data-quality-trends', {
      params: { days: 30, strategy: 'dual_low', market: 'cn', status: 'completed' },
    });
    expect(result.scannedCount).toBe(3);
    expect(result.degradedRatePct).toBe(33.33);
    expect(result.daily[0].runCount).toBe(3);
    expect(result.sourceErrorCounts).toEqual({ snapshotTimeout: 1 });
    expect(result.sourceHealthItems?.[0]).toMatchObject({
      key: 'snapshot/sina',
      observationCount: 3,
      degradedObservationCount: 2,
      degradedRatePct: 66.67,
    });
  });

  it('requests one gateway reconnect and camelCases diagnostics', async () => {
    post.mockResolvedValueOnce({
      data: {
        attempted: true,
        connected: true,
        status: 'connected',
        result: 'reconnected',
        reason: null,
        connect: { confirmation_source: 'get_state_snapshot' },
        reconnect: { last_trigger: 'manual', attempt_count: 1 },
      },
    });

    const result = await vnpyPaperTradingApi.reconnectGateway();

    expect(post).toHaveBeenCalledWith('/api/v1/vnpy-paper/gateway/reconnect');
    expect(result.connected).toBe(true);
    expect(result.connect.confirmationSource).toBe('get_state_snapshot');
    expect(result.reconnect.lastTrigger).toBe('manual');
    expect(result.reconnect.attemptCount).toBe(1);
  });

  it('runs a zero-connect gateway preflight and camelCases the contract', async () => {
    get.mockResolvedValueOnce({
      data: {
        schema_version: 1,
        ok: false,
        failures: ['builtin_gateway_not_external'],
        runtime_available: true,
        gateway_registered: true,
        external_gateway: false,
        gateway_name: 'DSA_SIM',
        production_preflight_enabled: false,
        settings_provided: false,
        settings_valid: true,
        settings_source: 'gateway_defaults',
        settings_inside_repository: false,
        default_setting_key_count: 5,
        provided_key_count: 0,
        missing_default_keys: ['initial_balance'],
        connect_attempted: false,
        subscriptions_created: false,
        orders_created: false,
        settings_path_exposed: false,
        settings_values_exposed: false,
      },
    });

    const result = await vnpyPaperTradingApi.preflightGateway();

    expect(get).toHaveBeenCalledWith('/api/v1/vnpy-paper/gateway/preflight');
    expect(result.ok).toBe(false);
    expect(result.gatewayName).toBe('DSA_SIM');
    expect(result.missingDefaultKeys).toEqual(['initial_balance']);
    expect(result.connectAttempted).toBe(false);
  });

  it('loads Agent return-risk calibration trends with filters', async () => {
    get.mockResolvedValueOnce({
      data: {
        schema_version: 1,
        window_days: 30,
        total: 2,
        scanned_count: 2,
        observed_count: 2,
        unknown_count: 0,
        observation_rate_pct: 100,
        health: 'ok',
        state_counts: { healthy: 2 },
        version_counts: { 'candidate-return-risk-v1': 2 },
        market_counts: { cn: 2 },
        strategy_counts: { dual_low: 2 },
        transition_counts: {},
        applied_count: 2,
        applied_rate_pct: 100,
        gate_blocked_count: 0,
        utility_observation_count: 2,
        average_utility_pct: 0.3,
        minimum_utility_pct: 0.1,
        maximum_utility_pct: 0.5,
        latest_mature_sample_count: 8,
        max_mature_sample_count: 9,
        groups: [{
          key: 'cn/dual_low/candidate-return-risk-v1',
          market: 'cn',
          strategy: 'dual_low',
          version: 'candidate-return-risk-v1',
          run_snapshot_count: 2,
          state_counts: { healthy: 2 },
          utility_observation_count: 2,
          average_utility_pct: 0.3,
          latest_state: 'healthy',
        }],
        daily: [{
          date: '2026-07-15',
          run_snapshot_count: 2,
          state_counts: { healthy: 2 },
          average_utility_pct: 0.3,
        }],
        truncated: false,
        methodology: { overlapping_rolling_samples: true },
      },
    });

    const result = await vnpyPaperTradingApi.getAgentReturnRiskCalibrationTrends(30, {
      strategy: 'dual_low',
      market: 'cn',
    });

    expect(get).toHaveBeenCalledWith(
      '/api/v1/vnpy-paper/agent-runs/return-risk-calibration-trends',
      { params: { days: 30, strategy: 'dual_low', market: 'cn' } },
    );
    expect(result.observedCount).toBe(2);
    expect(result.groups[0].runSnapshotCount).toBe(2);
    expect(result.daily[0].averageUtilityPct).toBe(0.3);
  });

  it('loads read-only production calibration evidence', async () => {
    get.mockResolvedValueOnce({
      data: {
        schema_version: 1,
        generated_at: '2026-07-21T04:30:00Z',
        window_days: 90,
        filters: { trigger_source: 'agent_calibration_shadow', status: 'completed' },
        evaluation: {
          ok: false,
          failures: ['cn:runs_below_threshold'],
          required_markets: ['cn', 'hk', 'us'],
          required_versions: [],
          thresholds: {
            min_runs_per_market: 20,
            min_observed_per_market: 10,
            min_observation_rate_pct: 80,
            min_mature_samples: 20,
            min_observation_days: 5,
            max_latest_age_hours: 72,
          },
          markets: {
            cn: {
              ok: false,
              failures: ['runs_below_threshold'],
              total_runs: 9,
              observed_runs: 9,
              observation_rate_pct: 100,
              latest_mature_sample_count: 0,
              observation_days: 2,
            },
          },
        },
        methodology: {
          read_only: true,
          creates_agent_runs: false,
          places_orders: false,
        },
        alert_delivery: {
          status: 'failed',
          trigger: {
            id: 28,
            status: 'degraded',
            reason: 'calibration_evidence_pending',
            triggered_at: '2026-07-21T04:52:11Z',
          },
          attempt_count: 1,
          successful_count: 0,
          failed_count: 1,
          retryable_failure_count: 1,
          attempts: [{
            attempt: 1,
            channel: 'feishu',
            success: false,
            error_code: 'send_failed',
            retryable: true,
          }],
          retry_policy: {
            status: 'waiting',
            attempt: 1,
            max_attempts: 3,
            interval_seconds: 300,
            next_retry_at: '2026-07-21T04:57:11Z',
          },
        },
      },
    });

    const result = await vnpyPaperTradingApi.getAgentCalibrationEvidence();

    expect(get).toHaveBeenCalledWith(
      '/api/v1/vnpy-paper/agent-runs/calibration-evidence',
    );
    expect(result.evaluation.requiredMarkets).toEqual(['cn', 'hk', 'us']);
    expect(result.evaluation.markets.cn.totalRuns).toBe(9);
    expect(result.methodology.createsAgentRuns).toBe(false);
    expect(result.alertDelivery?.status).toBe('failed');
    expect(result.alertDelivery?.attempts[0].channel).toBe('feishu');
    expect(result.alertDelivery?.attempts[0].attempt).toBe(1);
    expect(result.alertDelivery?.retryPolicy?.status).toBe('waiting');
    expect(result.alertDelivery?.retryPolicy?.nextRetryAt).toBe('2026-07-21T04:57:11Z');
  });

  it('generates an Agent run LLM recap', async () => {
    post.mockResolvedValueOnce({
      data: {
        accepted: true,
        status: 'completed',
        reason: null,
        run_uid: 'ss-agent-test',
        llm_recap: {
          status: 'completed',
          content: 'ok',
        },
        run_detail: null,
      },
    });

    const result = await vnpyPaperTradingApi.generateAgentRunRecap('ss-agent-test', 600);

    expect(post).toHaveBeenCalledWith('/api/v1/vnpy-paper/agent-runs/ss-agent-test/llm-recap', {
      max_output_tokens: 600,
    });
    expect(result.accepted).toBe(true);
    expect(result.runUid).toBe('ss-agent-test');
    expect(result.llmRecap.content).toBe('ok');
  });

  it('upserts structured human feedback for an Agent run', async () => {
    put.mockResolvedValueOnce({
      data: {
        accepted: true,
        run_uid: 'ss-agent-test',
        human_feedback: {
          id: 7,
          run_id: 1,
          verdict: 'needs_changes',
          note: 'Reduce concentration.',
          reviewer: 'risk-owner',
          source: 'web',
        },
        run_detail: {
          id: 1,
          run_uid: 'ss-agent-test',
          trigger_source: 'vnpy_paper_auto',
          status: 'completed',
          strategy: 'dual_low',
          market: 'cn',
          skip_existing_positions: true,
          human_feedback: { id: 7, run_id: 1, verdict: 'needs_changes', source: 'web' },
          decisions: [],
          trade_plans: [],
          timeline: [],
        },
      },
    });

    const result = await vnpyPaperTradingApi.updateAgentRunFeedback('ss-agent-test', {
      verdict: 'needs_changes',
      note: '  Reduce concentration.  ',
      reviewer: ' risk-owner ',
    });

    expect(put).toHaveBeenCalledWith('/api/v1/vnpy-paper/agent-runs/ss-agent-test/feedback', {
      verdict: 'needs_changes',
      note: 'Reduce concentration.',
      reviewer: 'risk-owner',
    });
    expect(result.humanFeedback.verdict).toBe('needs_changes');
    expect(result.humanFeedback.runId).toBe(1);
    expect(result.runDetail.humanFeedback?.verdict).toBe('needs_changes');
  });

  it('sends settings updates as snake_case and preserves explicit null thresholds', async () => {
    put.mockResolvedValueOnce({ data: { enabled: true, settings: {}, recent_trades: [] } });

    await vnpyPaperTradingApi.updateSettings({
      enabled: true,
      autoTradeEnabled: true,
      autoCashPerOrder: 12000,
      autoScoreWeightedAllocationEnabled: true,
      autoAllocationBudget: 25000,
      autoAllocationMethod: 'score_inverse_volatility_20d',
      autoRiskVolatilityFloorPct: 7.5,
      autoCorrelationLookbackDays: 90,
      autoCorrelationMinObservations: 30,
      autoMaxPairwiseCorrelation: 0.75,
      autoCovarianceRiskPenalty: 0.4,
      autoIntervalMinutes: 5,
      autoMinScore: null,
      autoSkipExistingPositions: false,
      autoExecutionMode: 'manual_approval',
      autoMaxPositions: 8,
      autoMaxSinglePositionValue: 20000,
      autoMaxTotalPositionValue: 80000,
      autoMaxTotalPositionPct: 80,
      autoMaxIndustryPositionValue: 40000,
      autoMaxIndustryPositionPct: 40,
      autoTargetPositionWeights: { '600519': 2.5 },
      autoTargetIndustryWeights: { 白酒: 8 },
      autoDailyMaxOrders: 2,
      autoDailyBudget: null,
      autoTradeTimeGateEnabled: false,
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
      autoIntradayMarketGateEnabled: true,
      autoIntradayRequireProviderTimestamp: true,
      autoIntradayIndexMinChangePct: -1.5,
      autoIntradayBreadthMinScore: 45,
      autoCrossMarketGateEnabled: true,
      autoCrossMarketMinChangePct: -2.5,
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
    });

    expect(put).toHaveBeenCalledWith('/api/v1/vnpy-paper/settings', {
      enabled: true,
      auto_trade_enabled: true,
      auto_cash_per_order: 12000,
      auto_score_weighted_allocation_enabled: true,
      auto_allocation_budget: 25000,
      auto_allocation_method: 'score_inverse_volatility_20d',
      auto_risk_volatility_floor_pct: 7.5,
      auto_correlation_lookback_days: 90,
      auto_correlation_min_observations: 30,
      auto_max_pairwise_correlation: 0.75,
      auto_covariance_risk_penalty: 0.4,
      auto_interval_minutes: 5,
      auto_min_score: null,
      auto_skip_existing_positions: false,
      auto_execution_mode: 'manual_approval',
      auto_max_positions: 8,
      auto_max_single_position_value: 20000,
      auto_max_total_position_value: 80000,
      auto_max_total_position_pct: 80,
      auto_max_industry_position_value: 40000,
      auto_max_industry_position_pct: 40,
      auto_target_position_weights: { '600519': 2.5 },
      auto_target_industry_weights: { 白酒: 8 },
      auto_daily_max_orders: 2,
      auto_daily_budget: null,
      auto_trade_time_gate_enabled: false,
      auto_symbol_blacklist: ['600519', '000001'],
      auto_exclude_st: true,
      auto_exclude_suspended: true,
      auto_exclude_price_limit: true,
      auto_min_turnover: 100000000,
      auto_min_data_quality_score: 72.5,
      auto_cross_run_quality_gate_enabled: true,
      auto_cross_run_horizon_days: 10,
      auto_cross_run_min_mature_samples: 20,
      auto_cross_run_min_win_rate_pct: 48,
      auto_cross_run_max_decisions: 300,
      auto_min_cash_balance: 5000,
      auto_max_drawdown_pct: 12,
      auto_drawdown_recovery_hysteresis_pct: 2,
      auto_consecutive_loss_limit: 3,
      auto_consecutive_loss_cooldown_minutes: 120,
      auto_market_light_gate_enabled: true,
      auto_market_light_block_statuses: ['red', 'yellow'],
      auto_market_context_max_age_days: 5,
      auto_market_breadth_gate_enabled: true,
      auto_market_breadth_min_score: 42,
      auto_hotspot_retreat_gate_enabled: true,
      auto_hotspot_retreat_min_drop: 30,
      auto_intraday_market_gate_enabled: true,
      auto_intraday_require_provider_timestamp: true,
      auto_intraday_index_min_change_pct: -1.5,
      auto_intraday_breadth_min_score: 45,
      auto_cross_market_gate_enabled: true,
      auto_cross_market_min_change_pct: -2.5,
      auto_failure_fuse_enabled: true,
      auto_failure_fuse_threshold: 2,
      auto_failure_fuse_auto_recovery_enabled: true,
      auto_failure_fuse_cooldown_minutes: 60,
      auto_sell_enabled: true,
      auto_stop_loss_pct: 8,
      auto_take_profit_pct: 18,
      auto_trailing_stop_pct: 12,
      auto_max_holding_days: 20,
      auto_sell_position_pct: 50,
      auto_signal_exit_enabled: true,
      auto_no_progress_days: 5,
      auto_no_progress_min_return_pct: 1,
      auto_rebalance_enabled: true,
      auto_llm_plan_enabled: true,
      auto_llm_review_enabled: true,
      vnpy_gateway_name: 'SIM',
    });
  });

  it('submits manual paper orders with snake_case cash amount', async () => {
    post.mockResolvedValueOnce({
      data: {
        accepted: true,
        cash_amount: 1100,
        cash_amount_base: 1000,
        cash_amount_quote: 1100,
        base_currency: 'CNY',
        quote_currency: 'HKD',
        trade_id: 1,
      },
    });

    const result = await vnpyPaperTradingApi.submitOrder({
      symbol: '00700',
      side: 'buy',
      market: 'hk',
      cashAmount: 1000,
      price: 10,
      executionRoute: 'vnpy_bridge',
    });

    expect(post).toHaveBeenCalledWith('/api/v1/vnpy-paper/orders', {
      symbol: '00700',
      side: 'buy',
      market: 'hk',
      quantity: undefined,
      cash_amount: 1000,
      price: 10,
      note: undefined,
      execution_route: 'vnpy_bridge',
    });
    expect(result.cashAmount).toBe(1100);
    expect(result.cashAmountBase).toBe(1000);
    expect(result.cashAmountQuote).toBe(1100);
    expect(result.baseCurrency).toBe('CNY');
    expect(result.quoteCurrency).toBe('HKD');
    expect(result.tradeId).toBe(1);
  });

  it('syncs vnpy trade callbacks with snake_case payload', async () => {
    post.mockResolvedValueOnce({
      data: {
        accepted: true,
        status: 'filled',
        trade_id: 2,
        raw: { vt_orderid: 'SIM.1', vt_tradeid: 'SIM.T1' },
      },
    });

    const result = await vnpyPaperTradingApi.syncVnpyTradeCallback({
      vtOrderid: 'SIM.1',
      vtTradeid: 'SIM.T1',
      symbol: '600519',
      side: 'buy',
      market: 'cn',
      quantity: 100,
      price: 10.2,
      tradeDate: '2026-07-03',
      raw: { gatewayName: 'SIM' },
    });

    expect(post).toHaveBeenCalledWith('/api/v1/vnpy-paper/vnpy-events/trades', {
      vt_orderid: 'SIM.1',
      vt_tradeid: 'SIM.T1',
      symbol: '600519',
      side: 'buy',
      market: 'cn',
      quantity: 100,
      price: 10.2,
      trade_date: '2026-07-03',
      fee: undefined,
      tax: undefined,
      currency: undefined,
      raw: { gatewayName: 'SIM' },
    });
    expect(result.tradeId).toBe(2);
    expect(result.raw?.vtOrderid).toBe('SIM.1');
  });

  it('syncs vnpy order callbacks with snake_case payload', async () => {
    post.mockResolvedValueOnce({
      data: {
        accepted: false,
        status: 'failed',
        reason: 'vnpy_order_rejected',
        raw: { vt_orderid: 'SIM.1', vnpy_order_status: 'rejected' },
      },
    });

    const result = await vnpyPaperTradingApi.syncVnpyOrderCallback({
      vtOrderid: 'SIM.1',
      status: 'rejected',
      symbol: '600519',
      side: 'buy',
      market: 'cn',
      volume: 100,
      traded: 0,
      price: 10,
      rejectedReason: 'unit rejected',
      raw: { gatewayName: 'SIM' },
    });

    expect(post).toHaveBeenCalledWith('/api/v1/vnpy-paper/vnpy-events/orders', {
      vt_orderid: 'SIM.1',
      status: 'rejected',
      symbol: '600519',
      side: 'buy',
      market: 'cn',
      volume: 100,
      traded: 0,
      price: 10,
      rejected_reason: 'unit rejected',
      raw: { gatewayName: 'SIM' },
    });
    expect(result.reason).toBe('vnpy_order_rejected');
    expect(result.raw?.vtOrderid).toBe('SIM.1');
  });

  it('syncs vnpy account and position callbacks with snake_case payload', async () => {
    post
      .mockResolvedValueOnce({
        data: {
          accepted: true,
          status: 'synced',
          raw: { account_id: 'SIM.ACC', available: 99000 },
        },
      })
      .mockResolvedValueOnce({
        data: {
          accepted: true,
          status: 'synced',
          raw: { count: 1 },
        },
      });

    const account = await vnpyPaperTradingApi.syncVnpyAccountCallback({
      accountId: 'SIM.ACC',
      balance: 100000,
      available: 99000,
      frozen: 1000,
      margin: 0,
      closeProfit: 0,
      holdingProfit: 120,
      currency: 'CNY',
      raw: { gatewayName: 'SIM' },
    });
    const positions = await vnpyPaperTradingApi.syncVnpyPositionsCallback({
      positions: [{
        vtSymbol: '600519.SSE',
        market: 'cn',
        direction: 'net',
        volume: 100,
        ydVolume: 100,
        frozen: 0,
        price: 10,
        avgPrice: 9,
        pnl: 120,
      }],
      raw: { gatewayName: 'SIM' },
    });

    expect(post).toHaveBeenNthCalledWith(1, '/api/v1/vnpy-paper/vnpy-events/account', {
      account_id: 'SIM.ACC',
      balance: 100000,
      available: 99000,
      frozen: 1000,
      margin: 0,
      close_profit: 0,
      holding_profit: 120,
      currency: 'CNY',
      raw: { gatewayName: 'SIM' },
    });
    expect(post).toHaveBeenNthCalledWith(2, '/api/v1/vnpy-paper/vnpy-events/positions', {
      positions: [{
        symbol: undefined,
        vt_symbol: '600519.SSE',
        market: 'cn',
        direction: 'net',
        volume: 100,
        quantity: undefined,
        yd_volume: 100,
        frozen: 0,
        price: 10,
        avg_price: 9,
        pnl: 120,
        raw: {},
      }],
      raw: { gatewayName: 'SIM' },
    });
    expect(account.raw?.accountId).toBe('SIM.ACC');
    expect(positions.raw?.count).toBe(1);
  });

  it('attaches injected vnpy event engine callbacks', async () => {
    post.mockResolvedValueOnce({
      data: {
        accepted: true,
        status: 'attached',
        raw: { registered_count: 4, event_types: ['eOrder.'] },
      },
    });

    const result = await vnpyPaperTradingApi.attachVnpyEventEngine();

    expect(post).toHaveBeenCalledWith('/api/v1/vnpy-paper/vnpy-events/attach');
    expect(result.status).toBe('attached');
    expect(result.raw?.registeredCount).toBe(4);
  });

  it('runs auto trading with temporary dry-run overrides', async () => {
    post.mockResolvedValueOnce({
      data: {
        accepted: true,
        skipped: false,
        strategy: 'dual_low',
        market: 'cn',
        candidate_count: 1,
        planned_count: 1,
        submitted_count: 0,
        skipped_count: 0,
        orders: [],
        messages: [],
      },
    });

    const result = await vnpyPaperTradingApi.runAutoOnce({
      executionMode: 'dry_run',
      ignoreAutoTradeEnabled: true,
    });

    expect(post).toHaveBeenCalledWith('/api/v1/vnpy-paper/auto/run', {
      execution_mode: 'dry_run',
      ignore_auto_trade_enabled: true,
    });
    expect(result.plannedCount).toBe(1);
    expect(result.submittedCount).toBe(0);
  });

  it('loads agent run list and details', async () => {
    get
      .mockResolvedValueOnce({
        data: {
          items: [{
            id: 1,
            run_uid: 'ss-agent-test',
            trigger_source: 'vnpy_paper_auto',
            status: 'completed',
            strategy: 'dual_low',
            market: 'cn',
            candidate_count: 1,
            planned_count: 0,
            submitted_count: 1,
            skipped_count: 0,
            message_count: 0,
            skip_existing_positions: true,
          }],
          limit: 10,
          offset: 0,
          total: 1,
        },
      })
      .mockResolvedValueOnce({
        data: {
          id: 1,
          run_uid: 'ss-agent-test',
          trigger_source: 'vnpy_paper_auto',
          status: 'completed',
          strategy: 'dual_low',
          market: 'cn',
          candidate_count: 1,
          submitted_count: 1,
          skipped_count: 0,
          message_count: 0,
          skip_existing_positions: true,
          decisions: [{
            id: 2,
            run_id: 1,
            sequence: 1,
            symbol: '600519',
            market: 'cn',
            action: 'buy',
            status: 'filled',
            cash_amount: 1000,
            trade_id: 3,
            risk_flags: [],
            strategy_evidence: { status: 'detailed', strategy: 'dual_low' },
            position_plan: { symbol: '600519', planned_cash_amount: 1000 },
            risk_review: { status: 'passed' },
            agent_review: { status: 'passed', reviewer: 'rule_agent_v1' },
            llm_review: { status: 'warning', model: 'unit-test' },
          }],
          trade_plans: [{
            id: 3,
            plan_uid: 'plan-test',
            run_id: 1,
            decision_id: 2,
            symbol: '600519',
            market: 'cn',
            side: 'buy',
            status: 'filled',
            execution_mode: 'paper',
            planned_cash_amount: 1000,
            submitted_quantity: 100,
            trade_id: 3,
            risk_flags: [],
          }],
          portfolio_change: {
            schema_version: 1,
            basis: 'persisted_portfolio_trade_ids',
            status: 'changed',
            booked_plan_count: 1,
            pending_plan_count: 0,
            planned_plan_count: 0,
            symbol_count: 1,
            items: [{
              symbol: '600519',
              market: 'cn',
              buy_quantity: 100,
              sell_quantity: 0,
              net_quantity: 100,
              buy_notional: 1000,
              sell_notional: 0,
              net_cash_flow: -1000,
              plan_count: 1,
              trade_ids: [3],
            }],
          },
          timeline: [{
            stage: 'completed',
            status: 'completed',
            message: 'Run completed: planned=0, submitted=1, skipped=0',
            timestamp: '2026-07-01T09:30:01Z',
            details: { status_counts: { filled: 1 } },
          }],
        },
      });

    const list = await vnpyPaperTradingApi.listAgentRuns(10, 0);
    const detail = await vnpyPaperTradingApi.getAgentRun('ss-agent-test');

    expect(get).toHaveBeenNthCalledWith(1, '/api/v1/vnpy-paper/agent-runs', {
      params: { limit: 10, offset: 0 },
    });
    expect(get).toHaveBeenNthCalledWith(2, '/api/v1/vnpy-paper/agent-runs/ss-agent-test');
    expect(list.items[0].runUid).toBe('ss-agent-test');
    expect(list.total).toBe(1);
    expect(detail.decisions[0].cashAmount).toBe(1000);
    expect(detail.decisions[0].tradeId).toBe(3);
    expect(detail.decisions[0].strategyEvidence).toEqual({ status: 'detailed', strategy: 'dual_low' });
    expect(detail.decisions[0].positionPlan).toEqual({ symbol: '600519', plannedCashAmount: 1000 });
    expect(detail.decisions[0].riskReview).toEqual({ status: 'passed' });
    expect(detail.decisions[0].agentReview).toEqual({ status: 'passed', reviewer: 'rule_agent_v1' });
    expect(detail.decisions[0].llmReview).toEqual({ status: 'warning', model: 'unit-test' });
    expect(detail.tradePlans[0].plannedCashAmount).toBe(1000);
    expect(detail.tradePlans[0].executionMode).toBe('paper');
    expect(detail.portfolioChange).toBeDefined();
    expect(detail.portfolioChange?.status).toBe('changed');
    expect(detail.portfolioChange?.items[0].netQuantity).toBe(100);
    expect(detail.portfolioChange?.items[0].tradeIds).toEqual([3]);
    expect(detail.timeline[0].stage).toBe('completed');
    expect(detail.timeline[0].details?.statusCounts).toEqual({ filled: 1 });
  });

  it('approves manual trade plans through the plan endpoint', async () => {
    post.mockResolvedValueOnce({
      data: {
        accepted: true,
        status: 'filled',
        symbol: '600519',
        trade_id: 9,
        cash_amount: 1000,
      },
    });

    const result = await vnpyPaperTradingApi.approveTradePlan('plan/manual 1');

    expect(post).toHaveBeenCalledWith('/api/v1/vnpy-paper/trade-plans/plan%2Fmanual%201/approve');
    expect(result.accepted).toBe(true);
    expect(result.tradeId).toBe(9);
    expect(result.cashAmount).toBe(1000);
  });

  it('retries manual trade plans through the retry endpoint', async () => {
    post.mockResolvedValueOnce({
      data: {
        accepted: true,
        status: 'filled',
        symbol: '600519',
        trade_id: 10,
        cash_amount: 1000,
      },
    });

    const result = await vnpyPaperTradingApi.retryTradePlan('plan/manual 1');

    expect(post).toHaveBeenCalledWith('/api/v1/vnpy-paper/trade-plans/plan%2Fmanual%201/retry');
    expect(result.accepted).toBe(true);
    expect(result.tradeId).toBe(10);
  });

  it('cancels submitted trade plans through the cancel endpoint', async () => {
    post.mockResolvedValueOnce({
      data: {
        accepted: true,
        status: 'cancel_requested',
        symbol: '600519',
        reason: 'vnpy_order_cancel_requested',
      },
    });

    const result = await vnpyPaperTradingApi.cancelTradePlan('plan/manual 1');

    expect(post).toHaveBeenCalledWith('/api/v1/vnpy-paper/trade-plans/plan%2Fmanual%201/cancel');
    expect(result.accepted).toBe(true);
    expect(result.status).toBe('cancel_requested');
    expect(result.reason).toBe('vnpy_order_cancel_requested');
  });

  it('runs Agent forward evaluation with snake-case filters and camelCases the matrix', async () => {
    post.mockResolvedValueOnce({
      data: {
        generated_at: '2026-07-14T10:00:00',
        methodology: { lookahead_protection: true },
        filters: { eval_windows: [1, 5] },
        total: 2,
        scanned_count: 2,
        truncated: false,
        refresh_attempted_count: 0,
        status_counts: { filled: 2 },
        matrix: {
          1: {
            eval_window_days: 1,
            sample_count: 2,
            completed_count: 2,
            insufficient_count: 0,
            coverage_pct: 100,
            win_count: 1,
            loss_count: 1,
            neutral_count: 0,
            win_rate_pct: 50,
            direction_accuracy_pct: 50,
            average_return_pct: 1.25,
            median_return_pct: 1.25,
            average_max_favorable_excursion_pct: 3,
            average_max_adverse_excursion_pct: -2,
            daily_observation_count: 2,
            expected_daily_observation_count: 2,
            daily_return_coverage_pct: 100,
            average_daily_return_pct: 0.62,
            daily_return_volatility_pct: 1.4,
            downside_deviation_pct: 0.8,
            horizon_downside_deviation_pct: 0.8,
            daily_expected_shortfall_20_pct: -1.1,
            return_risk_utility_pct: 0.35,
            return_risk_objective_version: 'candidate-return-risk-v1',
            neutral_band_pct: 2,
            unable_reason_counts: {},
          },
        },
        strategy_matrix: {},
        review_quality_matrix: [
          {
            key: 'llm:openai/model-a:prompt-v2/eval-v1',
            source: 'llm',
            reviewer: 'llm_reviewer_v1',
            model: 'openai/model-a',
            version: 'prompt-v2/eval-v1',
            sample_count: 2,
            status_counts: { passed: 1, blocked: 1 },
            horizons: {
              1: {
                eval_window_days: 1,
                sample_count: 2,
                completed_count: 2,
                passed_completed_count: 1,
                blocked_completed_count: 1,
                passed_precision_pct: 100,
                blocked_avoidance_rate_pct: 100,
                return_spread_pct: 6,
                unable_reason_counts: {},
              },
            },
          },
        ],
        review_policy_quality: {
          policy: 'llm_review_then_rule_agent',
          source_counts: { llm: 2 },
          horizons: { 1: { passed_precision_pct: 100 } },
        },
        items: [],
      },
    });

    const result = await vnpyPaperTradingApi.runAgentBacktest({
      strategy: 'dual_low',
      market: 'cn',
      createdFrom: '2026-07-01T00:00:00',
      createdTo: '2026-07-14T23:59:59',
      evalWindows: [1, 5],
      includeSkipped: false,
      maxDecisions: 200,
    });

    expect(post).toHaveBeenCalledWith('/api/v1/vnpy-paper/agent-runs/backtest', {
      strategy: 'dual_low',
      market: 'cn',
      created_from: '2026-07-01T00:00:00',
      created_to: '2026-07-14T23:59:59',
      eval_windows: [1, 5],
      include_skipped: false,
      max_decisions: 200,
      refresh_missing: false,
      neutral_band_pct: 2,
    });
    expect(result.scannedCount).toBe(2);
    expect(result.methodology.lookaheadProtection).toBe(true);
    expect(result.matrix['1'].averageReturnPct).toBe(1.25);
    expect(result.matrix['1'].returnRiskUtilityPct).toBe(0.35);
    expect(result.matrix['1'].dailyExpectedShortfall20Pct).toBe(-1.1);
    expect(result.reviewQualityMatrix[0].model).toBe('openai/model-a');
    expect(result.reviewQualityMatrix[0].horizons['1'].blockedAvoidanceRatePct).toBe(100);
    expect(result.reviewPolicyQuality?.policy).toBe('llm_review_then_rule_agent');
  });

  it('loads the current cross-run quality gate state', async () => {
    get.mockResolvedValueOnce({
      data: {
        schema_version: 3,
        generated_at: '2026-07-14T10:00:00',
        state: 'blocked',
        reason: 'forward_win_rate_below_threshold',
        previous_state: 'healthy',
        transition: 'healthy->blocked',
        changed: true,
        strategy: 'dual_low',
        market: 'cn',
        selection_quality_state: 'healthy',
        selection_quality_reason: 'forward_quality_thresholds_met',
        return_risk_objective_state: 'blocked',
        return_risk_objective_reason: 'return_risk_negative_return_and_utility',
        return_risk_objective_applied: true,
        return_risk_objective: {
          version: 'candidate-return-risk-v1',
          state: 'blocked',
          metrics: { return_risk_utility_pct: -1.5 },
        },
        review_quality_state: 'blocked',
        review_quality_reason: 'review_passed_precision_below_threshold',
        review_quality_applied: true,
        review_policy_quality: {
          policy: 'llm_review_then_rule_agent',
          horizons: { 5: { passed_precision_pct: 40 } },
        },
        horizon_days: 5,
        min_mature_samples: 10,
        min_win_rate_pct: 45,
        max_decisions: 200,
        sample_count: 20,
        mature_sample_count: 18,
        coverage_pct: 90,
        win_rate_pct: 40,
        average_return_pct: -0.5,
        median_return_pct: -0.2,
        average_max_adverse_excursion_pct: -4,
        average_daily_return_pct: -0.1,
        daily_return_coverage_pct: 100,
        daily_return_volatility_pct: 2.1,
        downside_deviation_pct: 1.8,
        daily_expected_shortfall_20_pct: -3.2,
        return_risk_utility_pct: -1.5,
        unable_reason_counts: { insufficient_forward_bars: 2 },
        lookahead_protection: true,
        source: 'persisted_agent_decisions_and_stock_daily',
        truncated: false,
        gate_enabled: true,
        gate_blocked: true,
        insufficient_evidence_blocks: false,
      },
    });

    const result = await vnpyPaperTradingApi.getAgentCrossRunQuality();

    expect(get).toHaveBeenCalledWith('/api/v1/vnpy-paper/agent-runs/cross-run-quality');
    expect(result.matureSampleCount).toBe(18);
    expect(result.gateBlocked).toBe(true);
    expect(result.transition).toBe('healthy->blocked');
    expect(result.reviewQualityState).toBe('blocked');
    expect(result.reviewQualityApplied).toBe(true);
    expect(result.returnRiskObjectiveState).toBe('blocked');
    expect(result.returnRiskObjectiveApplied).toBe(true);
    expect(result.returnRiskUtilityPct).toBe(-1.5);
    expect(result.dailyReturnCoveragePct).toBe(100);
    expect(result.returnRiskObjective?.version).toBe('candidate-return-risk-v1');
    expect(result.reviewPolicyQuality?.horizons).toEqual({ 5: { passedPrecisionPct: 40 } });
  });
});
