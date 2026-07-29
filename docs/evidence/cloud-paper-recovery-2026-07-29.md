# Cloud Paper recovery evidence — 2026-07-29

This is a documentation-only acceptance record for the five tasks requested on
2026-07-29. It does not authorize or record any live/real-money action.

## Task 1 — deploy the portable AI provider

- PR #410 supplied the portable provider in `main@68754a5`.
- The Cloud release subsequently advanced through the ledger, diagnostics,
  portability and late-event fixes to
  `fcb0df3597acc9d989d3923e1025eac91fefa606`.
- Cloud preflight at `2026-07-29T02:53:26Z` passed with the exact source SHA
  and tree, a clean tracked checkout, Paper-only execution and zero control
  actions.
- Dashboard, datafeed, access gateway and Cloudflare tunnel are active. All
  five Cloud timers are active. Dashboard full/trader/ops, strategy-console,
  trading-system read-model and public-access-health endpoints return 200.

## Task 2 — start a fresh AI-recommended Paper strategy

- Evaluation: `ai-eval-4272ba2b41ca4382`, status `success`.
- Proposal: `proposal-ai-eval-4272ba2b41ca4382`.
- StrategyPlan: `strategy-plan-2026-07-29_DAY-2-52d364d4`, version 2.
- Strategy: Grid, neutral, steady, arithmetic, 38 levels, range
  3871.39–4191.07, spacing 8.4126, per-grid notional 5265.97 USD, minimum
  planned net 10.04 USD and leverage cap 10x.
- Atomic start created and accepted 38/38 Paper orders; 38 lifecycle rows were
  armed.
- Current readback remains the same plan and reports `running`, one real entry
  fill, one protected open Paper short position and 37 remaining accepted/open
  orders. Tick and market are fresh, execution reconciliation is `ok`, and
  canonical accounting reconciliation is `pass`.
- The three trades from `2026-07-28_DAY` are not counted as evidence for this
  plan.

## Task 3 — repair execution-to-daily-ledger projection

- Issue #416 / PR #417 fixed the root cause: daily rebuild ignored verified
  terminal Nautilus StrategyCyclePackages and repeatedly overwrote production
  execution with legacy zero rows.
- Rebuilt `2026-07-28_DAY` evidence is 3 trades, 6 fills and
  `-7.47319148 USD`, sourced from
  `verified_strategy_cycle_package.execution`.
- Raw fills, trades and StrategyCyclePackages remain immutable. Only derived
  daily/weekly ledger data is rebuilt.
- NAV and all ledger-count consumers must use the rebuilt derived ledger.
  Terminal 12-hour execution review and immutable Shadow evidence were already
  execution-backed and are not rewritten. Promotion aggregates depending on
  ledger counts/PnL must be regenerated from the rebuilt ledger.

## Task 4 — clear known Cloud defects

- #406 / PR #418: Cloud dead-man exposure now uses the current matching
  Nautilus Paper snapshot. Flat is normal only when evidence is fresh and both
  reconciliation layers pass; unknown remains critical.
- #407 / PR #419: formal Dashboard diagnostics no longer mis-compose the Cloud
  datafeed database as a legacy market store. The supported full/trader/ops
  views and dedicated strategy-console endpoint return 200.
- #420 / PR #421: public health identifies the Cloudflare Access interstitial
  as `public_access_protected`; it no longer reports missing application
  fingerprints as a stale deployment.

## Task 5 — audit and gate hardcoded paths

- #422 / PR #423 records the complete classification in
  `docs/audits/environment-hardcoding-2026-07-29.md`.
- Active Cloud defaults no longer depend on `/opt/homebrew`, `/Users/wendy`,
  fixed macOS Python paths or Homebrew `codex`.
- The executable Linux portability check is mandatory in Cloud preflight. The
  deployed receipt scanned 29 active runtime files with zero violations.
- macOS launchd paths remain only in the explicitly isolated local/failback
  adapter and are documented as retained.

## Additional P0 found during release

#424 / PR #425 prevents a delayed candle from changing an already accepted
fill during full Nautilus replay. Raw events are retained and receive an
append-only `late_ignored` disposition when their execution timestamp is not
after the accepted execution watermark. The existing fill remains sell 1.303
at 4039.64 with timestamp `2026-07-29T02:41:00Z`.

## Remaining acceptance boundary

Cloud soak is not terminal yet. The first complete Cloud Beijing-day evidence
is `report_date=2026-07-29`, generated after 2026-07-30 01:10 Asia/Shanghai.
Until then the incomplete prior-day self-review remains a known degraded
evidence state, not a tick, market, execution or reconciliation failure.

# Gotchas

- A systemd timer being active does not mean its previous one-shot completed
  successfully. Final soak acceptance separately requires terminal daily
  report, complete self-review and verified backup receipts.
- `armed` and accepted orders are not fills. Only current-plan fill/trade,
  position, lifecycle and reconciliation evidence counts as execution.
- Cloudflare Access returning an HTTP 200 login document proves protected
  reachability, not authenticated application feature contents.
- The frozen legacy dual-engine comparison file is not the current
  authoritative execution reconciliation; use the current-plan read-model.
