# Issue #416 — production execution to daily-ledger root cause

## Root cause

`DualTrackScorer.rebuild_ledgers()` discovered cycles from
`dualtrack/fills/*_{machine,human}.json` and rebuilt machine P&L exclusively
from those legacy simulation fills. Production Paper execution is persisted in
`dualtrack/nautilus_authoritative/` and bound into the immutable
`dualtrack/strategy_cycle_packages/<cycle>.json` terminal journal. It does not
write the legacy machine-fill file.

The rollover order made the defect deterministic:

1. the legacy cycle scorer closed the market-review cycle and wrote a zero
   ledger from an empty legacy fill file;
2. the production control plane stopped and reconciled Nautilus Paper;
3. the terminal package captured the real fills, positions and P&L;
4. the same tick called `rebuild_ledgers()`, which ignored the new package and
   wrote zero again.

For `2026-07-28_DAY`, the verified terminal package contains six fills, three
closed positions and `execution.pnl.realized=-7.47319148`; the legacy
`fills/2026-07-28_DAY_machine.json` source is absent/empty. This is a source
selection defect, not missing execution evidence.

## Repair contract

The derived ledger now discovers terminal package journals as well as legacy
fills. When a package exists, its complete hash chain must verify, its status
must be `closed`, and its execution reconciliation must be `ok` without
issues. Only then are `execution.pnl.realized`, closed-position count and fill
count projected into the daily and weekly ledgers. A corrupt package fails the
ledger phase visibly; it cannot silently fall back to zero.

Legacy simulation and human-track behavior remain compatible. No source fill,
trade, Nautilus snapshot, cycle record or terminal package is rewritten.

## Downstream impact and rebuild

- **NAV curve:** affected. Historical NAV sourced from the daily/weekly machine
  ledger understated every Nautilus production cycle as zero. Rebuild all
  derived daily/weekly ledgers from verified terminal packages.
- **12-hour review:** the top-level production review inside each terminal
  package already used `execution.pnl.realized` and therefore retained the
  correct value. Its legacy nested review may still describe the old
  simulation track, but the immutable terminal review does not require
  rewriting. Regenerate read models/reports that cached ledger totals.
- **Strategy Shadows promotion threshold:** Shadow run evidence is immutable
  and remains independent of production-ledger mutation. Any cross-cycle
  promotion/read-model aggregation that consumed ledger trade counts must be
  regenerated so the 100-trade gate sees the corrected production sample.
  No candidate is automatically promoted by this rebuild.

## Acceptance evidence

Focused tests construct a hash-valid terminal package with six fills, three
closed positions and exact P&L `-7.47319148`, then prove that ledger rebuild
emits the same non-zero trade/fill/P&L values. Existing scoring, cycle-runner
and package-integrity tests remain green.
