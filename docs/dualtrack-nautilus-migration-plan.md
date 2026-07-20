# DualTrack Nautilus Paper Engine Migration

Status: M0-M3 complete; M4 evidence accrual; M5 isolated rehearsal complete, live cutover gated
Date: 2026-07-16

## User-value objective

One paper execution truth: a trusted market event reaches one deterministic
order engine, and every order, fill, position, account balance, margin value,
and PnL number remains traceable to the active StrategyPlan across replay and
restart.

The dashboard, StrategyPlan control plane, AI recommendation service, and
canonical datafeed are not replaced. Nautilus owns execution and accounting
only.

## Non-negotiable success criteria

1. The configured authoritative engine is exactly one of `legacy_paper` or
   `nautilus_paper`; there is never more than one production paper ledger.
2. Persistent config cannot self-authorize a cutover. Selecting Nautilus also
   requires the exact attended service approval, an isolated runtime path, and
   a passed shadow gate.
3. Synthetic, stale, future, provider-mismatched, or source-ambiguous events
   never reach either engine.
4. Start, limit entry, fill, re-arm, TP/SL, cancel, regrid, and flatten preserve
   `strategy_plan_id` and `strategy_plan_version`.
5. Orders, fills, positions, realized/unrealized PnL, equity, margin, exposure,
   fees, and slippage reconcile from one normalized execution snapshot.
6. Duplicate commands/events and process restarts do not duplicate a fill or
   move an order backward in its lifecycle.
7. Nautilus shadow never mutates the authoritative legacy ledger.
8. Cutover requires all fixed parity fixtures plus seven consecutive
   command-bearing paper cycles with zero unexplained drift.
9. Cutover is paper-only, attended, visually verified, and reversible without
   rewriting historical fills.

## Milestones

### M0 - Contract freeze and migration boundary

Exit criteria:

- All production mutation/read entrypoints construct the authoritative adapter
  through `build_configured_execution_engine_adapter`.
- `configs/dualtrack.yaml` explicitly keeps `legacy_paper` authoritative and
  declares `nautilus_paper` as shadow.
- Config cannot grant attended approval or provide a trusted runtime path.
- Existing execution contract and factory regression stays green.

Current evidence: complete. The fixed Nautilus parity gate is `pass`; the live
cutover gate is correctly `blocked` with `0/7` qualifying cycles.

### M1 - Continuous Nautilus paper shadow runtime

Exit criteria:

- The isolated pinned Nautilus runtime is durable outside `/tmp`.
- Every accepted production command and canonical 1m market event is delivered
  once to the shadow journal without changing the legacy result.
- Shadow failures are visible and retryable; they never silently disappear and
  never stop or mutate the authoritative paper robot.
- Restart resumes from immutable commands/events and reconciles the last
  normalized snapshot.

Current evidence: complete. Nautilus 1.230.0 is installed at a stable isolated
path. A live 240-event sweep completes with one deferred replay instead of one
replay per event. The measured live tick fell from an aborted run exceeding 90
seconds to 4.41 seconds. The current cycle has 246/246 unique processed market
events and 51/51 unique processed commands; legacy remains authoritative and
reconciles `ok`.

### M2 - Complete grid order lifecycle

Exit criteria:

- Neutral, long, and short grid orders map to Nautilus limit orders.
- Start, fill, replacement/re-arm, regrid cancel/replace, TP/SL, and
  stop/cancel/flatten have tested terminal states.
- Partial fill and partial reduction behavior is explicit and deterministic.

Current evidence: complete. Real Nautilus integration covers neutral grid start,
same-timestamp batched cancel, two-phase regrid, manual market open/close,
cancel-all, protective cleanup, flatten, and stop. HEDGING position IDs are
preserved and partial reductions resize their remaining protection.

### M3 - Accounting and restart reconciliation

Exit criteria:

- One normalized snapshot drives dashboard orders, fills, positions, account,
  margin, exposure, fees, and PnL.
- Fill IDs are unique; exits are later than entries; account PnL equals fill
  PnL; persisted projections equal replayed projections.
- Restart and duplicate-event tests remain exact.

Current evidence: complete. Nautilus persists immutable commands/events plus
normalized orders, fills, positions, account and snapshot projections. Restart,
duplicate-event, account-identity, fee, margin and traceability tests pass. The
dashboard now names authoritative reconciliation separately from shadow drift.

### M4 - Shadow qualification

Exit criteria:

- The ten fixed parity classes pass.
- Seven consecutive command-bearing 12-hour paper cycles pass exact comparison.
- Empty cycles do not count and any drift resets the consecutive gate.
- An in-progress cycle never counts. Qualification requires the persisted cycle
  close artifact, at least one accepted command, trusted non-synthetic market
  events from the configured provider, the pinned replay version, and an exact
  match to the current account-observed paper fee contract.
- An in-progress observation also cannot reset an already-earned completed-cycle
  streak. Only a completed non-qualifying cycle resets the consecutive count.
  A live in-progress drift still blocks an attended switch, but preserves the
  completed streak so the evidence does not have to be re-earned.

Current evidence: in progress by design. All ten fixed parity classes pass. The
live gate is `0/7` because the latest completed cycle used an earlier paper fee
contract and is not rewritten. The currently open cycle remains visible as an
observation but cannot count or reset the completed-cycle streak. Only new
command-bearing 12-hour cycles under the unified account-observed fee contract
can advance this gate.
The live scheduler now performs an idempotent final shadow reconciliation when
each cycle closes, so the 7/7 record accrues automatically without an operator
backfill.

### M5 - Attended paper cutover and rollback

Exit criteria:

- Operator explicitly selects Nautilus after inspecting gate evidence.
- Start/stop/cancel/flatten and browser dashboard work against Nautilus.
- Full regression and desktop/mobile visual evidence pass.
- A documented rollback restores `legacy_paper` without changing historical
  source events. Real-money execution remains ineligible.

Current evidence: isolated rehearsal complete; live switch not authorized. The
real pinned runtime passed start, regrid, cancel-all, manual open/close and stop
against a dedicated `nautilus_authoritative` ledger. Historical
`nautilus_paper` shadow state was pre-seeded in the rehearsal and proved unable
to enter the production ledger. Desktop and 390px mobile browser checks pass;
the repository regression is green. Live authority stays `legacy_paper` until
M4 reaches 7/7 and an operator gives attended approval.
The same rehearsal then selected `legacy_paper` again and proved the Legacy
history bytes and Nautilus authoritative snapshot both remained unchanged.

Before any attended write, run:

```bash
python3 -m pipelines.dualtrack_nautilus_attended_cutover --json
```

The read-only precheck requires 7/7, a stopped and flat Legacy robot, clean
Legacy and Nautilus reconciliation, explicit service-environment approval, the
isolated runtime, and ready instrument/fee preflight. It records no config
change and submits no order.

After the precheck is ready, the only supported write path is the transactional
controller:

```bash
TRADING_ORCHESTRATOR_NAUTILUS_PAPER_SWITCH_APPROVED=1 \
TRADING_ORCHESTRATOR_NAUTILUS_PYTHON=/Users/wendy/.local/share/trading-orchestrator/nautilus-1.230.0/bin/python \
python3 -m pipelines.dualtrack_nautilus_cutover_apply apply \
  --acknowledgement I_UNDERSTAND_NAUTILUS_PAPER_CUTOVER_WILL_RESTART_LOCAL_SERVICES \
  --json
```

It quiesces `dualtrack-live-tick` and the dashboard before changing config,
backs up `configs/dualtrack.yaml` plus both installed LaunchAgent plists, writes
the attended approval/runtime into only those service definitions, restarts the
services, and requires a stopped, flat, reconciled Nautilus API. Failed
post-switch validation restores all three files and revalidates Legacy
automatically. It never starts a strategy or submits an order.

An explicit later rollback uses the successful apply receipt and the separate
rollback acknowledgement:

```bash
python3 -m pipelines.dualtrack_nautilus_cutover_apply rollback \
  --apply-receipt <outputs/dualtrack/cutover/apply/APPLY_ID.json> \
  --acknowledgement I_UNDERSTAND_NAUTILUS_PAPER_ROLLBACK_WILL_RESTORE_LEGACY_AND_RESTART_LOCAL_SERVICES \
  --json
```

Rollback is fail-closed unless Nautilus is stopped, flat, and reconciled. It
restores the byte-identical apply backups and validates Legacy after restart.

## Rollback boundary

Before cutover, legacy remains authoritative. After paper cutover, rollback
changes only the configured authoritative adapter and service environment; it
does not copy Nautilus events into the legacy ledger or rewrite either history.
Shadow history remains under `dualtrack/nautilus_paper`; a cutover writes only
to `dualtrack/nautilus_authoritative`, so prior candidate state cannot become a
production order by directory reuse.

## Gotchas

- A replay subprocess is useful parity evidence but is not yet a continuous
  execution runtime.
- OHLC bars cannot reveal intrabar path. Same-bar TP/SL priority stays explicit
  and conservative until canonical trade/quote events are available.
- An engine switch is not proven by factory construction. The dashboard,
  scheduler, control plane, restart recovery, and reconciliation must all show
  the selected engine.
- The current cutover blocker is evidence coverage (`0/7`), not fixed-fixture
  correctness.
- A candidate and an authoritative engine must never reuse the same persistence
  namespace. Factory-level engine selection without storage separation is not a
  safe cutover.
- Production reconciliation and shadow reconciliation are different truths.
  The API exposes both and never labels candidate drift as active-ledger drift.
- Historical fee-model drift remains evidence. Rewriting it to manufacture a
  7/7 streak would invalidate the migration gate.
- Editing only `dualtrack.yaml` is not a cutover: the dashboard and live-tick
  LaunchAgents also require the attended approval and isolated runtime in their
  loaded service environments. The transactional controller changes and
  validates the three-file/service boundary together.
