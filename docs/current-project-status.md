# Current Project Status

Snapshot date: 2026-07-13

## Current State

- The project has a Web/API implementation for an online stock-selection Agent, AlphaSift screening, industry board data integration, and a vn.py-style paper trading page.
- The paper trading path works without a real vn.py installation by using the local Portfolio ledger; `vnpy_available=false` does not block local paper trading.
- Automatic paper trading can run from AlphaSift candidates through risk checks into local paper orders, with dry-run, manual approval, `paper`, and optional `vnpy_paper` execution modes.
- Runtime scheduler background tasks are registered independently from the daily analysis job, so `serve-only` / `webui-only` can still run paper auto-trading tasks.
- Stale vn.py plans now reconcile `MainEngine.get_order` / `get_all_trades` snapshots before timeout archival. Missed multi-fill trade callbacks are accumulated idempotently into the Portfolio ledger; gateway query failures protect the active plan instead of allowing an automatic duplicate submission.
- The latest scheduler alignment fix delays the first `vnpy_paper_auto_trade` run to the next trading window when the time gate is enforced and the service starts outside market hours.
- The Web paper trading page now surfaces the unified backend `diagnostics.system_health` view, readiness diagnostics, AlphaSift availability, scheduler loop status, task health, task event logs, 7/30/90-day terminal-run stability metrics, trading window status, scheduling-window alignment, and valuation degradation hints. The Agent console adds 7/30/90-day cross-run data-quality trends.

## Verified Local Status

Local API: `http://127.0.0.1:8000`

- `GET /api/v1/vnpy-paper/status?include_snapshot=false&include_recent_trades=false`
  - `available=true`
  - `vnpy_available=false`
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
  - next auto-trade run: `2026-07-14T09:30:00.819266`
  - next trading window: `2026-07-14T09:30:00+08:00` to `2026-07-14T15:00:00+08:00`
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
  - 110 passed
  - Covers MainEngine order/trade snapshots, cumulative multi-fill callbacks, partial-fill cancellation summaries, missed-callback reconciliation, query-failure protection, and recovery API counters.
- `npm.cmd run test -- --run src/api/__tests__/vnpyPaperTrading.test.ts src/pages/__tests__/VnpyPaperTradingPage.test.tsx`
  - 52 passed, including recovery counter mapping and the Web recovery result summary.
- Restarted live API smoke on `http://127.0.0.1:8000`
  - Latest-code process remains alive, `available=true`, `scheduler.loop_running=true`, and the recovery route is present.
  - OpenAPI exposes `reconciled_count`, `protected_count`, and `reconciliation_failed_count` on `VnpyPaperTradePlanRecoveryRunResponse`.
  - `diagnostics.vnpy_bridge.order_reconciliation_supported=false` is expected because this environment has no injected MainEngine.
- `python -m pytest tests -m "not network" -q --basetemp <workspace-temp> -p no:cacheprovider`
  - 4077 passed, 15 failed, 187 errors, 4 deselected.
  - The remaining suite failures are outside the changed vn.py paths and cluster around existing global configuration isolation, Windows subprocess handling, static bundle fixtures, and temporary-directory behavior. The focused 110-test vn.py suite is green.
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

## Unfinished Goals

- Real vn.py gateway validation is still not complete: the current environment does not have vn.py installed, so real gateway connection, long-running EventEngine subscription, and broker/paper gateway lifecycle behavior remain unverified.
- Cross-currency valuation and full portfolio optimization are still incomplete; current portfolio controls cover local paper valuation and rule-based exposure limits.
- The Agent workflow is usable but not a complete autonomous research loop: cross-market dynamic objectives, long-horizon quality evaluation, and human feedback loops still need more work.
- Background-task long-window metrics and cross-run Agent data-quality trends are available for 7/30/90-day windows. Fine-grained per-source weighting and long-term availability trends are still pending.
- Backtest-grade performance evaluation is not complete; current paper performance is useful for operational review, not a full historical market-data backtest.
- Recovery now reconciles MainEngine snapshots before timeout and fails closed on query errors, but real gateway reconnect behavior, cache retention, late callbacks after process restarts, and long-running lifecycle validation remain incomplete.

See [online-stock-selection-agent-goals.md](online-stock-selection-agent-goals.md) for the fuller goal breakdown.
