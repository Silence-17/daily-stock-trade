# Current Project Status

Snapshot date: 2026-07-10

## Current State

- The project has a Web/API implementation for an online stock-selection Agent, AlphaSift screening, industry board data integration, and a vn.py-style paper trading page.
- The paper trading path works without a real vn.py installation by using the local Portfolio ledger; `vnpy_available=false` does not block local paper trading.
- Automatic paper trading can run from AlphaSift candidates through risk checks into local paper orders, with dry-run, manual approval, `paper`, and optional `vnpy_paper` execution modes.
- Runtime scheduler background tasks are registered independently from the daily analysis job, so `serve-only` / `webui-only` can still run paper auto-trading tasks.
- The latest scheduler alignment fix delays the first `vnpy_paper_auto_trade` run to the next trading window when the time gate is enforced and the service starts outside market hours.
- The Web paper trading page now surfaces backend readiness diagnostics, AlphaSift availability, scheduler loop status, task health, task event logs, trading window status, scheduling-window alignment, and valuation degradation hints.

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
  - `diagnostics.auto_trade_readiness.status=warning`
  - `diagnostics.auto_trade_readiness.next_action=next_session`
  - `diagnostics.auto_trade_readiness.timing_alignment.status=ready`
  - `diagnostics.auto_trade_readiness.timing_alignment.reason=next_auto_run_in_window`
  - next auto-trade run: `2026-07-13T09:30:00.395532`
  - next trading window: `2026-07-13T09:30:00+08:00` to `2026-07-13T15:00:00+08:00`
- `GET /paper-trading` returned HTTP 200.

The readiness status is still `warning` because the current time is outside the A-share trading session and the next action is to wait for the next session. The scheduling alignment itself is ready.

## Recent Validation

- `python -m py_compile src\scheduler.py src\services\runtime_scheduler.py src\services\vnpy_paper_trading_service.py api\v1\endpoints\vnpy_paper_trading.py api\v1\schemas\vnpy_paper_trading.py tests\test_scheduler_background.py tests\test_runtime_scheduler_service.py tests\test_vnpy_paper_trading_service.py tests\test_vnpy_paper_trading_api.py`
- `python -m pytest tests\test_scheduler_background.py tests\test_runtime_scheduler_service.py tests\test_vnpy_paper_trading_service.py tests\test_vnpy_paper_trading_api.py -q`
  - 125 passed
- `npm.cmd run test -- --run src/api/__tests__/vnpyPaperTrading.test.ts src/pages/__tests__/VnpyPaperTradingPage.test.tsx`
  - 50 passed
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
- Data-source health is visible in several places, but a unified cross-module health view and longer-term metrics are still pending.
- Backtest-grade performance evaluation is not complete; current paper performance is useful for operational review, not a full historical market-data backtest.
- Recovery is safer than before, but a complete automatic recovery strategy for every real gateway order lifecycle edge case still needs long-running validation.

See [online-stock-selection-agent-goals.md](online-stock-selection-agent-goals.md) for the fuller goal breakdown.
