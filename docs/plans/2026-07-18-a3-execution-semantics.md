# A3 Unified Execution Semantics Implementation Plan

**Goal:** Make one frozen StrategyPlan plus one chronological trusted market-event
stream produce one Nautilus execution meaning across Strategy Shadow, candidate
promotion evidence, and restart replay, while the existing Legacy-to-Nautilus
platform parity gate continues to prove compatibility with current paper.

**Architecture:** Reuse the existing `ExecutionEngineAdapter`, normalized
execution command, canonical market event, `dualtrack-execution-v1` snapshot,
Nautilus persistent replay, reconciliation, and the existing exact
Legacy-to-Nautilus parity gate. Strategy Shadow becomes an orchestration/read-
model service over a replay port; it no longer contains a second matching
engine. Existing Lab simulators remain fast research filters, but their winners
cannot become paper-eligible without a candidate-specific Nautilus replay
receipt plus current platform parity evidence. The candidate receipt never
compares a Nautilus snapshot with itself.

**Tech Stack:** Python 3.13, existing NautilusTrader 1.230.0 isolated runtime,
frozen JSON contracts, pytest.

---

## User outcome

The same executable grid plan no longer earns one answer in Strategy Lab and a
second answer from a self-made Strategy Shadow matcher. Fast research can still
rank many variants, but executable candidate truth comes from Nautilus. Current
paper remains Legacy-authoritative until the separate attended seven-cycle gate
passes; compatibility is a platform-level parity claim, not a Shadow self-claim.

## Observable success criteria

1. Strategy Shadow has no inline order-matching, TP/SL, fee, position, or P&L
   calculation. It depends on an explicit replay port and persists the
   engine-neutral snapshot plus canonical accounting projection.
2. The exact executable grid orders from a StrategyPlan become the same
   normalized commands in Strategy Shadow and the production paper control
   plane, including price/quantity precision, fees, plan ID, and plan version.
3. Candidate conformance has two named layers: (a) one candidate input bundle is
   replayed once by Nautilus and must pass Nautilus `reconcile()` with exact
   plan/input/fee/execution-contract hash binding; (b) the existing fixed
   Legacy-authoritative versus Nautilus-candidate parity gate must be current
   and `pass`. A Nautilus-versus-itself snapshot comparison is forbidden.
4. Strategy Shadow rejects events earlier than the plan's evidenced availability
   timestamp and records the exact evaluation window. Historical Lab replay
   stays separate and keeps its existing walk-forward/holdout quarantine.
5. Fast Lab screening remains available, but `paper_eligible` cannot become true
   without a verifier-approved Nautilus receipt token bound to that candidate
   and current platform parity evidence. Direct callers without a token fail
   closed. Missing, stale, mismatched, legacy-schema, or failed receipts block.
6. Strategy Shadow uses a dedicated persistence namespace and proves it cannot
   write production orders, fills, positions, runtime state, P&L, or any of the
   authority evidence directories `dualtrack/reconciliation`,
   `dualtrack/nautilus/parity`, and `dualtrack/cutover`. Duplicate runs and
   restarts reproduce the same economic snapshot and receipt identity.
7. The attended Nautilus paper switch, seven consecutive command-bearing cycle
   gate, real-money ineligibility, and all current broker/risk gates remain
   unchanged.

## Scope

- In: execution scenario/receipt contract, shared StrategyPlan-to-command
  projection, Nautilus Strategy Shadow replay adapter, future-function gate,
  accounting projection, the active Lab-to-paper promotion boundary, Dashboard
  compatibility for v1/v2 Shadow artifacts, golden and real-runtime tests.
- Out: replacing fast Lab simulators, changing strategy logic, automatic paper
  promotion, changing the 7-cycle authority gate, switching the authoritative
  paper engine, real-money routing, Dashboard redesign, or deleting historical
  artifacts.

## Gotchas

- Existing `strategy-shadow-run-v1` artifacts can show `cost=0` even when their
  plans contain executable fees, and at least one current production artifact
  replays bars from before `locked_at` while declaring `future_function=false`.
  Preserve these files as legacy trace, but do not treat them as conformance or
  promotion evidence.
- `pipelines/dashboard_server.py` reads the last row of every file under
  `dualtrack/strategy_shadows`. Emit `strategy-shadow-run-v2`, preserve v1
  readability, and make every conformance verifier reject v1 as evidence.
- An AI proposal may have `created_at` but no production StrategyPlan ID/version.
  It can be evaluated as research, but an executable conformance receipt must
  bind to a versioned plan or a deterministic candidate-plan identity.
- OHLC bars do not reveal intrabar path. Preserve the existing conservative
  same-bar priority; do not invent tick ordering.
- Nautilus internal event UUIDs are replay-local. Business identity remains the
  normalized client order/fill identity and must include cycle/plan scope where
  source IDs are not already globally unique.
- The pure grid simulators remain valuable for scanning thousands of variants.
  Their metrics are research evidence, not execution evidence.
- A Strategy Shadow replay namespace must never equal `legacy_paper`,
  `nautilus_paper`, or `nautilus_authoritative`.
- The current Nautilus authority gate remains evidence-bound and attended. A3
  conformance does not authorize cutover.
- The primary worktree contains unrelated Debug and range-drag work. A3 is
  isolated and must not overwrite or copy whole primary files.

## Baseline

- Focused Strategy Shadow, execution contract/adapters, Nautilus parity, Lab
  promotion, and strategy-promotion suite before A3: `55 passed`.

### Task 1: Freeze one executable scenario and candidate receipt

**Files:**

- Create: `services/execution_conformance.py`
- Create: `tests/test_execution_conformance.py`

**Steps:**

1. Define a deterministic scenario identity from a frozen plan identity,
   normalized command batch, canonical event stream, execution-contract hash,
   fee-contract hash, and evaluation window.
2. Define one versioned receipt over the existing normalized execution snapshot
   plus its `reconcile()` result; do not create a second snapshot model.
3. Bind the receipt to the exact candidate, plan availability, normalized
   commands, canonical events, fee contract, execution contract, replay version,
   and dedicated namespace.
4. Fail closed on missing fields, non-chronological events, plan/event time
   leakage, input-hash mismatch, reconciliation drift, stale/missing platform
   parity, legacy schemas, or undeclared normalization.
5. Preserve the existing exact Legacy-to-Nautilus comparator and seven-cycle
   cutover receipts; never invoke it with the same engine on both sides.

### Task 2: Share StrategyPlan command projection and replace Strategy Shadow matching

**Files:**

- Create: `services/strategy_plan_execution.py`
- Modify: `services/strategy_control_plane.py`
- Rewrite: `services/strategy_shadow.py`
- Create: `services/strategy_shadow_nautilus.py`
- Create: `pipelines/strategy_shadow_replay.py`
- Modify only if compatibility needs it: `pipelines/dashboard_server.py`
- Modify: `tests/test_strategy_control_plane.py`
- Modify: `tests/test_strategy_shadow.py`
- Create: `tests/test_strategy_shadow_nautilus.py`
- Modify: `tests/test_dashboard_server.py`

**Steps:**

1. Extract the existing production grid-order-to-command mapping into one pure
   function and prove byte-equivalent commands for the control plane.
2. Make Strategy Shadow depend on a small replay port. Remove its inline grid
   touch, protective exit, position, fee, and P&L implementation.
3. Implement the replay port with `NautilusExecutionAdapter` in a dedicated,
   content-addressed shadow namespace; batch commands/events and flush once.
4. Project the returned execution snapshot through `accounting-snapshot-v1` and
   derive display metrics only from canonical accounting facts.
5. Reject pre-availability events and record `available_at`, evaluation start/
   end, event count, scenario ID, engine, replay version, and safety namespace.
6. Prove repeat/restart idempotence and byte stability without touching any
   production ledger path or any reconciliation/parity/cutover evidence path.
7. Emit only `strategy-shadow-run-v2` for new runs while keeping Dashboard reads
   of historical v1 artifacts intact; v1 never satisfies conformance.

### Task 3: Require Nautilus conformance at the promotion boundary

**Files:**

- Modify: `services/lab_promotion.py`
- Create: `services/lab_execution_conformance.py`
- Modify: `tests/test_lab_promotion.py`
- Create: `tests/test_lab_execution_conformance.py`

**Steps:**

1. Keep existing walk-forward, holdout, bootstrap, cost-grid, and fast-simulator
   outputs unchanged.
2. Add a read-only verifier keyed by candidate plan/scenario identity. It returns
   the only token accepted by `promotion_gate`; direct calls without it block.
3. Require a current `pass` candidate receipt from Nautilus with matching plan,
   input, fee, and execution-contract hashes plus a current platform parity
   receipt before `paper_eligible=true`.
4. Surface explicit blockers for missing, stale, drifted, mismatched, or
   non-Nautilus receipts; never auto-apply a candidate.
5. Prove old Lab artifacts remain readable but safely blocked until conformance
   exists.
6. Leave the separate legacy `StrategyPromotionGate` unchanged; it is not the
   active grid Lab-to-paper route and changing it would widen A3 scope.

### Task 4: Golden runtime proof and closure

**Files:**

- Modify: `tests/test_dualtrack_nautilus_runtime_integration.py`
- Modify: `tests/test_dualtrack_nautilus_parity_gate.py`
- Modify: `docs/dualtrack-execution-engine-adapter-spec.md`
- Modify: `decision-log.md`

**Steps:**

1. Run one golden versioned grid plan through the pinned Nautilus runtime and
   assert exact orders, fills, quantities, fees, positions, P&L, margin,
   exposure, equity, scenario ID, `reconcile()==ok`, and restart identity.
2. Prove a Lab winner with no/mismatched conformance remains blocked and the
   same winner with a matching pass receipt becomes requestable only for paper.
3. Re-run the ten-class Nautilus parity fixture and confirm the 7-cycle attended
   authority gate is neither bypassed nor reset.
4. Run focused, real-runtime, and full repository regression.
5. Run Opus adversarial review for future leakage, fee drift, identity mismatch,
   accidental production writes, tolerance masking, and promotion fail-open;
   fix every valid P0/P1 and record accepted/rejected findings.

## Completion boundary

A3 is complete when Strategy Shadow delegates execution to Nautilus, executable
plan commands are shared with current paper, Lab promotion requires a matching
candidate receipt plus existing platform parity evidence, future leakage is
blocked, v1 remains readable but ineligible, and exact runtime/full regression
plus Opus review pass. A3 does not authorize a paper-engine switch, automatic
promotion, or real-money execution.
