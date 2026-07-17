# A2 Single Accounting Truth Implementation Plan

**Goal:** Establish one versioned, engine-neutral accounting snapshot so orders,
fills, positions, trade lifecycles, P&L, fees, funding, margin, and equity have
the same meaning across Legacy paper, Nautilus paper, production history, and
read-only broker observations.

**Architecture:** Immutable execution or broker facts remain the source records.
Pure projectors map those facts into `accounting-snapshot-v1`; no projector may
place an order, rewrite a fill, or invent missing venue evidence. The current
execution and Strategy Console response shapes stay backward compatible while
receiving the canonical snapshot as an additive field. Production-history
totals move to the canonical projector only after exact parity tests pass.

**Tech Stack:** Python 3.13, frozen dataclasses, existing DualTrack execution
contract, Nautilus 1.230.0 normalized reports, pytest.

---

## User outcome

“Current orders”, “positions”, “fills”, and “trades” stop using overlapping
counts. One opening lifecycle counts as one trade; closing it does not create a
second trade. Only a fully closed lifecycle counts as a completed trade and can
enter strategy-performance sample sizes.

## Observable success criteria

1. `trade_count` counts unique started trade lifecycles; `completed_trade_count`
   counts only fully closed lifecycles; entry and exit fills remain separately
   visible in `fill_count`, `entry_fill_count`, and `exit_fill_count`.
2. Legacy and Nautilus `dualtrack-execution-v1` snapshots project into the same
   `accounting-snapshot-v1` contract without leaking engine objects.
3. Binance and Tiger read-only reconciliation receipts project into the same
   contract while explicitly marking unavailable order/fill/round-trip evidence
   as partial rather than zero.
4. Net realized P&L reconciles as gross realized P&L minus fees plus funding;
   ending cash and equity satisfy their accounting identities whenever all
   required inputs are observed.
5. Duplicate fill identities, orphan exits, negative remaining quantities,
   account drift, and ambiguous broker trade classification are visible in a
   deterministic reconciliation receipt.
6. Reprojecting the same immutable inputs produces the same snapshot ID and
   never duplicates a trade or P&L amount after restart.
7. Existing order submission, protection, risk guardrails, Nautilus approval
   gates, and browser response fields remain unchanged.

## Scope

- In: canonical accounting contract, pure projectors, deterministic identities,
  execution/API projection, production-history total cutover, read-only broker
  projection, conformance and regression tests.
- Out: real broker writes, order-routing changes, risk-limit changes, automatic
  engine cutover, full Dashboard redesign, historical source-fill rewriting.

## Gotchas

- Binance `userTrades` does not currently carry enough normalized evidence to
  classify every zero-PnL fill as entry versus breakeven exit. Broker round-trip
  counts must remain unknown/partial instead of being guessed.
- Tiger prime-assets accounting is aggregate account evidence, not a fill
  ledger. It can prove balance/P&L fields but not order, fill, or trade counts.
- Entry fees are already represented as negative realized P&L in Legacy fills.
  A projector must not subtract them twice.
- Nautilus position reports and Legacy trade rows use slightly different field
  names. Normalize at the adapter boundary; do not teach the Dashboard about
  engine-specific aliases.
- Recovery-replay and execution-shadow records are evidence, not production
  P&L. The existing exclusion rules remain intact.
- The primary worktree contains unrelated Debug and grid-range changes. A2 is
  implemented only in this isolated worktree and must not overwrite them.

## Baseline

- Focused accounting, execution, broker reconciliation, and Strategy Console
  suite before A2: `115 passed, 5 skipped`.

### Task 1: Freeze `AccountingSnapshot v1`

**Files:**

- Create: `schemas/accounting.py`
- Modify: `schemas/__init__.py`
- Create: `services/accounting_projection.py`
- Create: `tests/test_accounting_snapshot.py`
- Create: `tests/test_accounting_projection.py`

**Steps:**

1. Write failing contract tests for one open lifecycle, one closed lifecycle,
   scale-in, partial close, exact counts, deterministic ID, and immutable
   serialization.
2. Add a frozen snapshot envelope with source/scope/currency, canonical rows,
   counts, P&L, account values, completeness, and reconciliation evidence.
3. Normalize orders, fills, positions, and trade lifecycles from a
   `dualtrack-execution-v1` input.
4. Derive all counts from canonical rows; never accept caller-supplied counts.
5. Enforce exact identity and P&L/account invariants without display rounding
   tolerances.
6. Run the new contract/projection tests and commit.

### Task 2: Project both execution engines and expose the first read seam

**Files:**

- Modify: `pipelines/dashboard_server.py`
- Modify: `tests/test_dualtrack_execution_engine_adapter.py`
- Modify: `tests/test_dualtrack_nautilus_execution_adapter.py`
- Modify: `tests/test_dashboard_server.py`

**Steps:**

1. Add conformance tests proving equivalent Legacy and Nautilus snapshots map
   to equivalent canonical accounting values.
2. Add `accounting_snapshot` to the read-only execution response after the
   authoritative adapter snapshot and reconciliation are obtained.
3. Preserve every existing top-level execution field and all attended Nautilus
   cutover gates.
4. Prove GET remains read-only and projection failure is surfaced rather than
   silently falling back to a second P&L calculation.
5. Run focused execution and API tests and commit.

### Task 3: Move production-history totals to canonical accounting

**Files:**

- Create: `services/production_accounting.py`
- Modify: `pipelines/dashboard_server.py`
- Modify: `tests/test_dashboard_server.py`

**Steps:**

1. Keep the existing immutable source selection: versioned StrategyPlan Legacy
   fills plus Nautilus authoritative snapshots only; shadow/recovery records
   stay excluded.
2. Build the canonical snapshot from those selected facts and derive summary,
   P&L, and account totals from it.
3. Preserve the existing `trades`, `fills`, `summary`, `pnl`, `account`, and
   `history_contract` response fields; add the canonical snapshot and an exact
   parity receipt.
4. Test open, fully closed, partial close, multiple cycles, Legacy archive plus
   Nautilus authority, fee handling, and unknown mark behavior.
5. Run focused Strategy Console tests and commit.

### Task 4: Add honest broker projections

**Files:**

- Modify: `services/live_reconciliation.py`
- Modify: `services/tiger_openapi_account_sync.py`
- Modify: `tests/test_live_reconciliation.py`
- Modify: `tests/test_tiger_openapi_account_sync.py`
- Modify: `decision-log.md`

**Steps:**

1. Add a Binance projector for read-only open orders, user fills, positions,
   balance, commission, funding, and UTC-day accounting.
2. Add a Tiger aggregate projector that preserves balance/realized/unrealized
   evidence and marks unavailable lifecycle fields partial.
3. Attach the snapshot additively to reconciliation reports without changing
   live-money guardrail inputs or decisions.
4. Prove missing/failed venue observations cannot become zero-valued complete
   accounting.
5. Run the focused baseline, all accounting/reconciliation consumers, then the
   full repository suite.
6. Run an Opus adversarial review focused on double-counted costs, false trade
   counts, partial evidence presented as zero, duplicate-event idempotence, and
   accidental production mutation. Fix all valid P0/P1 findings and record the
   review plus final evidence in `decision-log.md`.

## Completion boundary

A2 is complete when all four source classes emit the same versioned accounting
contract, the execution API and Strategy Console use it for read-side totals,
broker gaps are fail-honest, and focused/full regression plus Opus review are
clean. A2 does not authorize a real order, an engine switch, or a UI redesign.

## Execution record

- Task 1 complete: immutable `accounting-snapshot-v1`, deterministic identity,
  lifecycle counts, duplicate/orphan/drift checks, and unknown-value semantics.
- Task 2 complete: Legacy and Nautilus execution snapshots expose the canonical
  contract without changing their source ledgers or attended cutover gates.
- Task 3 complete: Strategy Console production history derives compatibility
  totals from versioned Legacy plus explicitly authoritative Nautilus facts;
  shadow and recovery artifacts remain excluded.
- Task 4 complete: Binance and Tiger reconciliation receipts project honestly;
  a projector or serializer failure cannot suppress authoritative persistence.
- Accepted Opus fixes: fail-honest broker persistence, unknown slippage, a
  versioned emergency projection receipt, and consistent broker observed-field
  vocabulary. No P0/P1 remained after two reviews.
- Empirical Nautilus gate: the pinned 1.230.0 runtime proved realized P&L is net
  of observed commission and funding in the normalized adapter contract.
- Final verification: `124` focused accounting/reconciliation/guardrail tests,
  `5` pinned Nautilus runtime tests, Ruff, and the full repository suite
  (`1638 passed, 6 skipped`) all passed.
- No production order, strategy state, engine selection, external venue state,
  or visible surface was changed by A2.
