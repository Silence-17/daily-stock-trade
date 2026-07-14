# Current Project Status

Snapshot date: 2026-07-14

## Current State

- The project has a Web/API implementation for an online stock-selection Agent, AlphaSift screening, industry board data integration, and a vn.py-style paper trading page.
- The paper trading path works without a real vn.py installation by using the local Portfolio ledger; `vnpy_available=false` does not block local paper trading.
- Automatic paper trading can run from AlphaSift candidates through risk checks into local paper orders, with dry-run, manual approval, `paper`, and optional `vnpy_paper` execution modes.
- Runtime scheduler background tasks are registered independently from the daily analysis job, so `serve-only` / `webui-only` can still run paper auto-trading tasks.
- The API-owned vn.py MainEngine/EventEngine is now injected into Runtime scheduler paper-trading services; the recovery task runs once at registration and then every one to five minutes, so background execution no longer has a separate bridge capability from Web requests.
- A complete isolated Python 3.13.14 environment now contains AlphaSift 0.2.0, LiteLLM 1.91.3, vn.py 4.4.0, and the project dependencies. Its smoke check constructs a real vn.py `OrderRequest`, starts and closes the real EventEngine/MainEngine, and uses the repository-local ignored `.vntrader/` runtime directory.
- Active vn.py plans become eligible for `MainEngine.get_order` / `get_all_trades` reconciliation after a 60-second grace period. Pausing new automatic buys keeps order recovery scheduled, and missed, cancel-race, or post-timeout late fills are still accumulated idempotently into the Portfolio ledger.
- The latest scheduler alignment fix delays the first `vnpy_paper_auto_trade` run to the next trading window when the time gate is enforced and the service starts outside market hours.
- Paper-trading budgets, cash checks, position exposure, and daily usage now use the account base currency. Foreign-market orders convert through the Portfolio FX table, fail closed for new buys when rates are missing or more than seven calendar days old, preserve sell-side risk reduction, and record both base and quote amounts for audit.
- Explicit stock and industry target weights now cap each automatic buy to the remaining target gap instead of rejecting the whole candidate. Existing positions can be replenished toward an explicit stock target even when the maximum position count is reached or existing positions are normally skipped; the final amount is reduced to an executable market lot and audited in `target_weight_sizing`. Arbitrary-weight, multi-constraint portfolio optimization remains incomplete.
- Automatic buys now have an optional score-weighted portfolio allocation stage. A configured round budget is distributed by candidate score and reallocated when a candidate reaches its per-order, single-position, industry, or explicit target cap; cash reserve, remaining daily budget/order slots, maximum positions, and total exposure constrain the whole round. Agent diagnostics distinguish intended allocation from executable A-share-lot budget. This is a deterministic constrained allocator, not yet a covariance/risk-model or arbitrary-objective optimizer.
- The Agent console now runs an on-demand 1/5/10/20-trading-day forward evaluation for persisted buy candidates. It anchors on the recorded decision price, reads only local daily bars strictly later than the decision date, reports coverage and return/excursion metrics, and never reruns a strategy or places an order by default.
- AlphaSift now has a persisted point-in-time factor snapshot table and single-date replay APIs. Hard-filter and scoring-field coverage are audited separately, incomplete datasets fail closed, and the Agent console can inspect coverage and run deterministic hard-filter plus `screen_score` replay without current-data fallback or LLM ranking.
- For an explicitly supplied historical universe, a bounded background task now collects AKShare daily/turnover and historical valuation factors with strict as-of dates. The Agent console can then run a cross-date equal-weight portfolio backtest with per-side costs, benchmark/excess return, drawdown, coverage, and period equity; it never places orders.
- Cross-date backtests now retain symbols selected in adjacent snapshots, charge costs only on actual entries/exits, expose period and aggregate turnover, and apply A-share suspension/price-limit gates only when a trade is required.
- A sell blocked by suspension or a limit-down session is now marked to market without fake sell costs and carried into the next rebalance attempt. Period output separates selected coverage from actual/forced holdings, and summary metrics disclose any ending open positions.
- A Tushare-backed historical-universe endpoint can reconstruct A-share membership for a requested date from listing/delisting lifecycle rows, including symbols that later delisted. It fails closed when `stock_basic` permission is unavailable and discloses that current names/industries are not point-in-time fields.
- Persistent full-market ingestion jobs now resolve each snapshot date independently from one lifecycle load, freeze date/symbol work items, checkpoint every completed factor batch, and retain cursor, heartbeat, write counts, and source errors across restarts. Lease-protected force takeover prevents an old executor from advancing a replaced job, and the Agent console restores the latest job after refresh.
- Portfolio replay now defaults through API/Web to a cash ledger: equal target values are converted to whole A-share lots, overweight positions sell before underweight buys, buys are capped by cash, and final blocked exits remain marked open positions. Period output audits cash, market value, quantities, trades, blocks, and traded-notional turnover; the prior mean-return approximation remains opt-in.
- The Web paper trading page now surfaces the unified backend `diagnostics.system_health` view, readiness diagnostics, AlphaSift availability, scheduler loop status, task health, task event logs, 7/30/90-day terminal-run stability metrics, trading window status, scheduling-window alignment, valuation degradation hints, and position-industry coverage. Full status reports resolved/missing positions, coverage percentage, missing symbols, and industry market values; configured industry limits fail closed on incomplete coverage. The Agent console adds 7/30/90-day cross-run overall quality and per-source degradation trends.
- Configured account drawdown now uses an observed equity peak persisted per paper account. A profitable account that falls from its peak can block new buys even while remaining above initial cash; full status, system health, alerts, and Agent run diagnostics expose the peak, current equity, drawdown, threshold, and calculation basis.
- Every Agent plan now persists a deterministic summary of the five latest runs for the same trigger, strategy, and market. The optional LLM dynamic planner v2 consumes that exact context, while the Agent console displays recent-run count, historical submission rate, latest status, and failure streak even when LLM planning is disabled.

## Verified Local Status

Local API: `http://127.0.0.1:8000`

- `GET /api/v1/vnpy-paper/status?include_snapshot=false&include_recent_trades=false`
  - `available=true`
  - `vnpy_available=true`
  - `diagnostics.vnpy_runtime.available=true`
  - `diagnostics.vnpy_runtime.gateway.added=true`
  - `diagnostics.vnpy_runtime.connect.connected=true`
  - `diagnostics.vnpy_runtime.event_bridge.registered=true`
  - `settings.auto_execution_mode=paper`
  - `scheduler.enabled=true`
  - `scheduler.loop_running=true`
  - `settings.auto_trade_enabled=true`
  - `diagnostics.alphasift.available=true`
  - `diagnostics.alphasift.strategy_count=8`
  - `diagnostics.system_health.status=warning`
  - `diagnostics.system_health.next_action=next_session`
  - `diagnostics.system_health.required_blockers=[]`
  - `diagnostics.system_health.warnings=["next_session"]`
  - `diagnostics.system_health.components=paper_ledger,selection_source,automation_loop,scheduling_window,trading_window,valuation,industry_exposure,account_drawdown,vnpy_bridge`
  - `diagnostics.auto_trade_readiness.status=warning`
  - `diagnostics.auto_trade_readiness.next_action=next_session`
  - `diagnostics.auto_trade_readiness.timing_alignment.status=ready`
  - `diagnostics.auto_trade_readiness.timing_alignment.reason=next_auto_run_in_window`
  - next action: `next_session`
- `GET /api/v1/vnpy-paper/task-metrics?days=30`
  - route present in OpenAPI
  - `event_count=708`
  - `run_count=354`
  - `started_count=354`
  - `completed_count=353`
  - `skipped_count=1`
  - `failed_count=0`
  - `success_rate_pct=99.72`
  - `failure_rate_pct=0.0`
  - `skip_rate_pct=0.28`
  - `current_failure_streak=0`
  - `truncated=false`
  - 2 task series and 4 daily buckets
- `GET /api/v1/vnpy-paper/agent-runs/data-quality-trends?days=30`
  - `total=1`
  - `scanned_count=1`
  - `known_count=0`
  - `quality_counts={"unknown":1}` because the historical run predates quality snapshots
  - `degraded_rate_pct=0.0`
  - `health=warning`
  - `truncated=false`
- `GET /paper-trading` returned HTTP 200.

The latest post-change runtime smoke uses process PID `23396`; vn.py runtime, built-in `DSA_SIM` gateway, bridge, and event callbacks are available while the saved execution mode remains `paper`. The paper-trading and Agent-console pages both return HTTP 200. Score-weighted allocation remains disabled by default with no saved round budget, and no orders were triggered during verification. Full status previously completed in 6.14 seconds for six positions without per-symbol industry lookups.

The readiness and system health status are `warning` because the current time is outside the A-share trading session and the next action is to wait for the next session. There are no required blockers; the scheduling alignment itself is ready.

## Recent Validation

- `python -m pytest tests/test_vnpy_paper_trading_service.py tests/test_vnpy_paper_trading_api.py -q -p no:cacheprovider`
  - 133 passed and 1 optional test skipped after adding score-weighted constrained portfolio allocation, lot residual audit, settings/API contracts, and Agent position-plan coverage.
- `npm.cmd test -- --run src/api/__tests__/vnpyPaperTrading.test.ts src/pages/__tests__/VnpyPaperTradingPage.test.tsx src/pages/__tests__/AgentConsolePage.test.tsx`
  - 66 passed; `npm.cmd run lint` completed with zero errors and the pre-existing `SettingsPage.tsx:553` warning, and `npm.cmd run build` passed.
- `python -m flake8 . --count --select=E9,F63,F7,F82 --show-source --statistics --exclude=.git,.tools,.venv-vnpy,.uv-cache-vnpy,.vntrader,node_modules,static`
  - 0 critical findings in repository-controlled source. The unmodified `scripts/ci_gate.sh` cannot complete locally because `flake8 .` and pytest collect ignored `.tools` runtimes; its deterministic shell check also hits a Windows GBK emoji encoding error. The broad local offline run still completed 4551 passes and 3 skips with 31 failures outside the changed vn.py paths, while the focused suite above is green.
- `python -m pytest tests/test_vnpy_paper_trading_service.py tests/test_vnpy_paper_trading_api.py -q -p no:cacheprovider`
  - 119 passed and 1 optional test skipped before the final strict-industry health assertion was added; the final focused service run passed 91 tests with 1 optional skip.
- `npm.cmd run test -- --run src/pages/__tests__/VnpyPaperTradingPage.test.tsx src/api/__tests__/vnpyPaperTrading.test.ts`
  - 53 passed; the final page-only run passed 23 tests after the non-blocking snapshot-only display wording was refined.
- `npm.cmd run lint` and `npm.cmd run build`
  - Both passed; lint retains the pre-existing `SettingsPage.tsx:553` exhaustive-deps warning.
- `python -m pytest tests/test_vnpy_paper_trading_service.py tests/test_vnpy_paper_trading_api.py -q -p no:cacheprovider`
  - 123 passed and 1 optional test skipped after adding persisted observed-equity-peak drawdown and system-health coverage.
- `npm.cmd run test -- --run src/pages/__tests__/AgentConsolePage.test.tsx src/api/__tests__/vnpyPaperTrading.test.ts`
  - 43 passed after adding per-source health trend mapping and display coverage.
- `python -m pytest tests/test_vnpy_paper_trading_service.py -q -p no:cacheprovider`
  - 93 passed and 1 optional test skipped after adding deterministic recent-run context and dynamic-plan v2 prompt reuse.
- `npm.cmd run test -- --run src/pages/__tests__/AgentConsolePage.test.tsx`
  - 13 passed with recent-run context display coverage.

- `python -m py_compile api\v1\endpoints\vnpy_paper_trading.py api\v1\schemas\vnpy_paper_trading.py src\repositories\runtime_scheduler_repo.py src\services\runtime_scheduler.py`
- `python -m pytest tests/test_runtime_scheduler_repo.py tests/test_runtime_scheduler_service.py tests/test_vnpy_paper_trading_api.py -q`
  - 50 passed
- `npm.cmd run test -- --run src/api/__tests__/vnpyPaperTrading.test.ts src/pages/__tests__/VnpyPaperTradingPage.test.tsx`
  - 51 passed
- `python -m pytest tests/test_vnpy_paper_trading_api.py tests/test_vnpy_paper_trading_service.py -q`
  - 95 passed
- `python -m pytest tests/test_vnpy_adapter.py tests/test_vnpy_paper_trading_service.py tests/test_vnpy_paper_trading_api.py -q`
  - 117 passed
  - Covers MainEngine order/trade snapshots, cumulative multi-fill callbacks, partial-fill cancellation summaries, proactive missed-callback reconciliation, paused-buy recovery, cancel races, late fills, query-failure protection, and recovery API counters.
- `python -m pytest tests/test_vnpy_paper_trading_service.py -q -p no:cacheprovider`
  - 80 passed.
  - Covers service reconstruction recovery, pre-timeout no-evidence waiting, the 60-second grace period, paused-buy recovery scheduling, cancel-request fill races, and post-timeout late fills.
- `python -m pytest tests/test_vnpy_paper_trading_service.py tests/test_runtime_scheduler_service.py tests/test_vnpy_paper_trading_api.py tests/test_vnpy_runtime.py tests/test_vnpy_adapter.py tests/test_vnpy_simulated_gateway.py -q -p no:cacheprovider`
  - 148 passed and 2 optional-runtime tests skipped under system Python 3.14.
  - Covers API lifespan runtime binding, scheduler dependency forwarding, immediate recovery registration, runtime bootstrap, bridge adapter, built-in gateway contracts, task health, and order lifecycle recovery. The two skipped tests passed separately under Python 3.13 with vn.py installed.
- `.venv-vnpy\Scripts\python.exe scripts\check_vnpy_adapter.py --require-vnpy`
  - Python 3.13.14, vn.py 4.4.0, real `vnpy.trader.object.OrderRequest`, real `vnpy.event.engine.EventEngine`, and real `vnpy.trader.engine.MainEngine` all passed.
  - Bridge smoke returned `accepted=true`; the built-in `DsaSimulatedGateway` returned `vt_orderid=DSA_SIM.1`, `order_status=ALLTRADED`, and one real vn.py trade event; `pip check` reported no broken requirements.
- `.venv-vnpy\Scripts\python.exe -m unittest` built-in gateway integration smoke
  - A fixed AlphaSift candidate produced an Agent `submitted` plan through real MainEngine, then EventEngine callbacks changed the plan and decision to `filled` and wrote one Portfolio trade.
  - A direct gateway test also verified order, trade, account, and position snapshots through vn.py's OMS engine.
- Isolated API process smoke on `http://127.0.0.1:8011` with `VNPY_RUNTIME_ENABLED=true`
  - `diagnostics.vnpy_runtime.available=true`, `event_bridge.registered=true`, and four vn.py event types were attached.
  - MainEngine exposed send, cancel, order-query, and trade-query capabilities, and the runtime scheduler loop was active.
  - Recovery-task registration is covered by the focused scheduler/API regression suite; the isolated status response was not used to assert a task-list entry.
  - `diagnostics.vnpy_bridge.available=false` remained expected because no gateway name or gateway plugin was configured. The temporary process and port were stopped after validation.
- `npm.cmd run test -- --run src/api/__tests__/vnpyPaperTrading.test.ts src/pages/__tests__/VnpyPaperTradingPage.test.tsx`
  - 52 passed, including recovery counter mapping and the Web recovery result summary.
- Current local API process on `http://127.0.0.1:8000`
  - The API is running from the Python 3.13 vn.py environment with `runtime_available=true`, `gateway_added=true`, `gateway_connected=true`, and `event_bridge_registered=true`.
  - Settings inherit `gateway_name=DSA_SIM`; `diagnostics.vnpy_bridge.available=true` and the runtime scheduler loop is active.
  - The saved execution mode remains `paper`, so startup does not silently redirect scheduled orders. Selecting `vnpy_paper` in the Web settings explicitly activates the built-in gateway route.
- `python -m pytest tests -m "not network" -q --basetemp <workspace-temp> -p no:cacheprovider`
  - 4077 passed, 15 failed, 187 errors, 4 deselected.
  - The remaining suite failures are outside the changed vn.py paths and cluster around existing global configuration isolation, Windows subprocess handling, static bundle fixtures, and temporary-directory behavior. The focused vn.py suite is green with 148 passed and 2 optional-runtime skips under system Python.
- `npm.cmd run test -- --run src/api/__tests__/vnpyPaperTrading.test.ts src/pages/__tests__/VnpyPaperTradingPage.test.tsx src/pages/__tests__/AgentConsolePage.test.tsx`
  - 59 passed
- Browser verification at 1440x900 and 390x844
  - Agent quality-trend view rendered with live API data
  - no page-level horizontal overflow after constraining run history/detail cards
- `npm.cmd run lint -- --max-warnings=9999`
  - Passed with one existing warning in `apps/dsa-web/src/pages/SettingsPage.tsx`.
- `npm.cmd run build`
  - Passed.
- `git diff --check`
  - No whitespace errors; only Windows LF/CRLF warnings.
- `python -m pytest tests/test_vnpy_paper_trading_service.py tests/test_runtime_scheduler_service.py tests/test_vnpy_paper_trading_api.py tests/test_vnpy_runtime.py tests/test_vnpy_adapter.py tests/test_vnpy_simulated_gateway.py tests/test_portfolio_pr2.py -q -p no:cacheprovider`
  - 181 passed and 2 optional-runtime tests skipped under system Python 3.14.
  - Covers base/quote currency order audit, stale/missing FX fail-closed buys, cross-currency daily budget and exposure calculations, sell-side risk reduction, scheduler integration, and vn.py recovery paths.
- `python -m pytest tests/test_data_tools_portfolio_snapshot.py tests/test_portfolio_alerts.py tests/test_portfolio_api.py tests/test_portfolio_pr2.py tests/test_portfolio_service.py -q -p no:cacheprovider`
  - 93 passed across the shared Portfolio service and API surface.
- `.venv-vnpy\Scripts\python.exe -m unittest` built-in gateway integration smoke after the FX changes
  - 2 passed with real vn.py 4.4.0 MainEngine/EventEngine and the built-in `DSA_SIM` gateway.
- `.venv-vnpy\Scripts\python.exe -m pip check`
  - No broken requirements found.
- `python -m pytest tests/test_stock_selection_agent_backtest_service.py tests/test_backtest_engine.py tests/test_backtest_summary.py tests/test_backtest_service.py tests/test_vnpy_paper_trading_api.py -q -p no:cacheprovider`
  - 115 passed.
  - Covers strict post-decision daily-bar selection, multi-horizon metrics, strategy/date filters, skipped-candidate cohorts, missing-data coverage, the existing backtest engine/service, and the API contract.
- `npm.cmd run test -- --run src/api/__tests__/vnpyPaperTrading.test.ts src/pages/__tests__/AgentConsolePage.test.tsx`
  - 38 passed, including the Agent forward-evaluation request mapping and matrix rendering.
- `npm.cmd run lint` and `npm.cmd run build`
  - Passed; lint retains one pre-existing React hook dependency warning in `SettingsPage.tsx`.
- Live `POST /api/v1/vnpy-paper/agent-runs/backtest` smoke with local data
  - OpenAPI route present, 3 persisted candidates scanned, and 1/5/10/20-day coverage all returned `0.0%` with `insufficient_forward_bars=3`, as expected before any later trading-day bars exist.
  - `lookahead_protection=true`, `reruns_historical_strategy=false`, `/agent-console` returned HTTP 200, and the saved execution mode remained `paper`.
- `python -m pytest tests/test_alphasift_replay_api.py tests/test_stock_selection_strategy_replay_service.py tests/test_stock_selection_agent_backtest_service.py -q -p no:cacheprovider`
  - 12 passed, covering factor-snapshot idempotency, date isolation, missing-field fail-closed behavior, deterministic AlphaSift ranking, replay API contracts, and the existing persisted-candidate forward evaluation.
- `npm.cmd run test -- --run src/pages/__tests__/AgentConsolePage.test.tsx src/api/__tests__/alphasift.test.ts`
  - 21 passed, covering replay API request mapping, factor-coverage rendering, missing-field diagnostics, and candidate-table output.
- `npm.cmd run lint` and `npm.cmd run build`
  - Passed; lint retains one pre-existing React hook dependency warning in `SettingsPage.tsx`.
- Broader `tests/test_alphasift_api.py` run
  - 108 passed and 2 existing date-sensitive cache tests failed because their fixed `cached_at=2026-07-03` fixture is beyond the configured cache age on 2026-07-14; the new replay tests are unaffected.
- Live point-in-time replay smoke on `http://127.0.0.1:8000`
  - OpenAPI includes `/api/v1/alphasift/replay/snapshots`, `/replay/compatibility`, and `/replay/run`; an empty 2024-01-05 snapshot returned zero hard/score coverage plus the required-field lists, `/agent-console` returned HTTP 200, and no replay or trading order was submitted.
- `python -m pytest tests/test_stock_selection_factor_ingestion_service.py tests/test_stock_selection_portfolio_backtest_service.py tests/test_stock_selection_strategy_replay_service.py tests/test_stock_selection_agent_backtest_service.py tests/test_alphasift_replay_api.py -q -p no:cacheprovider`
  - 24 passed, covering valuation as-of protection, ingestion degradation, portfolio entry/exit timing, costs on actual entries/exits, overlap retention, aggregate and per-period turnover, suspension/price-limit trade gates, blocked-exit carry and later liquidation, benchmark/excess return, missing-bar coverage, replay gates, and background-task API contracts.
- `npm.cmd run test -- --run src/pages/__tests__/AgentConsolePage.test.tsx src/api/__tests__/alphasift.test.ts`
  - 30 passed after adding historical ingestion, portfolio-backtest, blocked-position audit, historical-universe preview, and persistent full-market job create/list/resume API/UI workflows.
- `python -m pytest tests/test_stock_selection_historical_universe_service.py tests/test_tushare_fetcher_get_stock_list.py tests/test_stock_selection_factor_ingestion_service.py tests/test_stock_selection_portfolio_backtest_service.py tests/test_stock_selection_strategy_replay_service.py tests/test_stock_selection_agent_backtest_service.py tests/test_alphasift_replay_api.py -q -p no:cacheprovider`
  - 45 passed across Tushare lifecycle aggregation, dated-universe filtering, bounded factor ingestion, replay, portfolio behavior, forward evaluation, and API contracts.
- `python -m pytest tests/test_stock_selection_historical_universe_service.py tests/test_tushare_fetcher_get_stock_list.py tests/test_stock_selection_factor_ingestion_service.py tests/test_stock_selection_full_market_ingestion_service.py tests/test_stock_selection_portfolio_backtest_service.py tests/test_stock_selection_strategy_replay_service.py tests/test_stock_selection_agent_backtest_service.py tests/test_alphasift_replay_api.py -q -p no:cacheprovider`
  - 55 passed, adding per-date universe isolation, persistent batch checkpoints, failed-batch resume, force-takeover lease protection, recent-job recovery, cash/whole-lot accounting, drift rebalancing, blocked final liquidation, and full-market/API contracts to the prior replay coverage.
- UI screenshot verification
  - The in-app Browser runtime still fails during initialization with `Cannot redefine property: process`, so no new screenshot could be captured. Component interaction tests, the production build, and live HTTP/OpenAPI smoke are used as replacement evidence; standalone Playwright was not substituted.
- Final live replay/backtest smoke on `http://127.0.0.1:8000`
  - OpenAPI exposes replay paths for historical-universe resolution, persistent full-market ingestion create/list/detail/resume, bounded ingestion status, and portfolio backtest. The portfolio request schema defaults `accounting_mode` to `cash_ledger` and `enforce_tradeability` to `true`. The persisted job list returned HTTP 200 with zero existing jobs, `/agent-console` returned HTTP 200, vn.py runtime/event bridge remained available under PID `19152`, execution mode remained `paper`, and no order was submitted.
  - A live dated-universe request returned HTTP 424 with `historical_universe_unavailable` because this machine does not currently have a Tushare token with `stock_basic` permission. This verifies the fail-closed path and that no current-universe fallback is used; an online successful universe fetch remains unverified until that external permission is configured.
  - A live full-market job creation request likewise returned HTTP 424 with `full_market_ingestion_unavailable`; it did not create a partial persisted job or submit a background factor task.

## Unfinished Goals

- Real vn.py gateway validation is still not complete: the isolated runtime and event lifecycle are verified, but no concrete gateway plugin/account has been configured, so gateway connection, broker/paper acknowledgements, reconnect behavior, and long-running subscriptions remain unverified.
- Basic cross-currency valuation is complete for supported paper-trading markets. Full portfolio optimization, target replenishment, and optimization-driven rebalancing are still incomplete.
- The Agent workflow is usable but not a complete autonomous research loop: cross-market dynamic objectives, long-horizon quality evaluation, and human feedback loops still need more work.
- Background-task long-window metrics and cross-run Agent data-quality trends are available for 7/30/90-day windows. Fine-grained per-source degradation rates are now visible; automatically changing source weights from those trends is still pending.
- Persisted Agent candidates have strict forward multi-horizon evaluation; Tushare lifecycle metadata reconstructs each dated A-share universe and feeds lease-protected resumable factor ingestion, while replay now executes equal targets with cash and whole-lot quantities. A successful live full-market run still requires external Tushare permission and stable long-running upstream access; corporate actions, market-specific fee minima/taxes, and arbitrary-weight constrained optimization remain incomplete.
- Recovery now reconciles active MainEngine snapshots before timeout, survives service reconstruction in repository-backed tests, accepts late fills, and fails closed on query errors; real gateway reconnect behavior, gateway cache retention, and long-running lifecycle validation remain incomplete.

See [online-stock-selection-agent-goals.md](online-stock-selection-agent-goals.md) for the fuller goal breakdown.
