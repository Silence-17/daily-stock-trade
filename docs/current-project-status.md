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
- The Web paper trading page now surfaces the unified backend `diagnostics.system_health` view, readiness diagnostics, AlphaSift availability, scheduler loop status, task health, task event logs, 7/30/90-day terminal-run stability metrics, trading window status, scheduling-window alignment, and valuation degradation hints. The Agent console adds 7/30/90-day cross-run data-quality trends.

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
  - `diagnostics.system_health.components=paper_ledger,selection_source,automation_loop,scheduling_window,trading_window,valuation,vnpy_bridge`
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

The final post-change runtime smoke used process PID `23708`; the built-in `DSA_SIM` gateway and vn.py event bridge were connected while the saved execution mode remained `paper`.

The readiness and system health status are `warning` because the current time is outside the A-share trading session and the next action is to wait for the next session. There are no required blockers; the scheduling alignment itself is ready.

## Recent Validation

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

## Unfinished Goals

- Real vn.py gateway validation is still not complete: the isolated runtime and event lifecycle are verified, but no concrete gateway plugin/account has been configured, so gateway connection, broker/paper acknowledgements, reconnect behavior, and long-running subscriptions remain unverified.
- Basic cross-currency valuation is complete for supported paper-trading markets. Full portfolio optimization, target replenishment, and optimization-driven rebalancing are still incomplete.
- The Agent workflow is usable but not a complete autonomous research loop: cross-market dynamic objectives, long-horizon quality evaluation, and human feedback loops still need more work.
- Background-task long-window metrics and cross-run Agent data-quality trends are available for 7/30/90-day windows. Fine-grained per-source weighting and long-term availability trends are still pending.
- Backtest-grade performance evaluation is not complete; current paper performance is useful for operational review, not a full historical market-data backtest.
- Recovery now reconciles active MainEngine snapshots before timeout, survives service reconstruction in repository-backed tests, accepts late fills, and fails closed on query errors; real gateway reconnect behavior, gateway cache retention, and long-running lifecycle validation remain incomplete.

See [online-stock-selection-agent-goals.md](online-stock-selection-agent-goals.md) for the fuller goal breakdown.
