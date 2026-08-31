# Hotspot Dual-Account 30-Session Paper Campaign

This experiment runs two isolated CNY 100,000 local Portfolio accounts:

- `hotspot_oversold_reversal_0945_v1` uses AlphaSift `oversold_reversal` candidates and targets 25% of current equity per position.
- `hotspot_trend_reacceleration_0945_v1` uses AlphaSift `momentum_quality` candidates and targets 25% of current equity per position.

Both use `broker=paper_0945`, so they never switch or contaminate the active `vnpy_paper` gateway account. At 09:28 on weekdays the runner prepares and freezes both candidate pools. From 09:30 until 09:35 it refreshes provider-timestamped quotes every 15 seconds. When a candidate first passes, a second fresh quote provides the simulated fill with 5 bp slippage. A fill quote that returns at or after 09:35 is rejected, and no buy is backfilled later. Quote collection skips providers without a provider timestamp and records each candidate's availability or rejection reason. Quotes older than 120 seconds or with incomplete fields fail closed.

Each strategy may add at most one position during the window. There is no forced buy that bypasses its hard entry gates.

On the next sellable session, the exit process reads the completed 09:25 call-auction price at 09:27 and compares it with the previous close. An auction gain below 5% is sold from a fresh provider-timestamped 09:30 quote. A gain of at least 5% enters a 15-second polling loop from 09:30 through 09:45, comparing the cumulative session high with the provider's security-specific `limit_up_price`.

Touching the limit-up price during those first 15 minutes protects the position from selling for the rest of that day. If limit-up was not touched and the limit-up-price evidence is complete, a trailing stop is armed after 09:45 and monitored through 14:54: 2% below the current-day peak for oversold reversal and 3% for trend reacceleration. The 14:55 close phase performs one final trailing check and persists closing equity. Every sell observes T+1 and includes slippage, commission, transfer fee, and stamp duty.

Missing auction price, previous close, limit-up price, or live quotes fails closed and keeps the position, with the reason recorded in the `exit-watch` audit. The former 3% hard stop, breakeven floor, open/VWAP strength-loss checks, and maximum holding periods are disabled. Administrative liquidation after 30 qualified sessions remains. A session advances only when at least one candidate exists and both the morning decision and closing valuation evidence are complete; an empty candidate pool is failed evidence and does not consume campaign progress.

## Dashboard and manual factors

The Web paper-trading page can switch its read-only view among both hotspot accounts and the cross-market account without changing the actual execution account. Each hotspot account exposes all account-level runtime factors: target equity per symbol, maximum positions, opening-return/rebound/range gates, the VWAP requirement, five final-score weights (opening return, rebound, AlphaSift upstream score, resilience, and VWAP), candidate and eligible-rank limits, next-day auction threshold, and trailing-stop pullback.

Overrides are isolated by strategy ID in `data/hotspot_0945_paper/factor_overrides.json` and reloaded before each strategy phase. The backend rejects invalid types, out-of-range values, a zero total score weight, or an eligible-rank limit larger than the candidate pool. Saved values affect subsequent decisions only; restoring defaults removes that strategy's manual overrides and never rewrites historical evidence.

Create the accounts with `python scripts/run_hotspot_0945_paper_30d.py --setup`, then install `scripts/install_hotspot_0945_paper_30d_task.ps1` from an elevated PowerShell. The installer registers `DailyStockAnalysis-Hotspot0927ExitWatch30D` for the 09:27 exit watch and keeps the compatibility-named `DailyStockAnalysis-Hotspot0945Paper30D` at 09:28/14:55 for the 09:30-09:35 entry watch and closing valuation. Existing strategy IDs retain `0945` only for account/progress compatibility. Inspect progress with `python scripts/run_hotspot_0945_paper_30d.py --phase status`.

This is paper trading only and is not investment advice.
