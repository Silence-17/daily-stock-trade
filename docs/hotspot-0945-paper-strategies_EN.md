# 09:45 Hotspot Dual-Account 30-Session Paper Campaign

This experiment runs two isolated CNY 100,000 local Portfolio accounts:

- `hotspot_oversold_reversal_0945_v1` uses AlphaSift `oversold_reversal` candidates and targets 25% of current equity per position.
- `hotspot_trend_reacceleration_0945_v1` uses AlphaSift `momentum_quality` candidates and targets 30% of current equity per position.

Both use `broker=paper_0945`, so they never switch or contaminate the active `vnpy_paper` gateway account. At 09:38 on weekdays the runner freezes both candidate pools. Provider-timestamped 09:45 quotes confirm the signal, and a second 09:46 quote provides the simulated fill with 5 bp slippage. Quotes older than 120 seconds, incomplete fields, or a missed signal window fail closed.

The close phase runs at 14:55, applies T+1 and exit risk rules, and persists closing equity. A session advances only when both the morning decision and closing valuation evidence are complete. New buys stop after 30 qualified A-share sessions; any remaining position is liquidated on the next sellable session.

Create the accounts with `python scripts/run_hotspot_0945_paper_30d.py --setup`, then install `scripts/install_hotspot_0945_paper_30d_task.ps1` from an elevated PowerShell. Inspect progress with `python scripts/run_hotspot_0945_paper_30d.py --phase status`.

This is paper trading only and is not investment advice.

