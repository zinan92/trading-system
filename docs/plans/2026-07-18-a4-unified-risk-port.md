# A4 Unified Risk Port Implementation Plan

**Status:** Opus-reviewed; P0/P1 corrections incorporated on 2026-07-18.

**Goal:** Make every exposure-increasing production mutation pass through one
versioned, engine-neutral risk decision that is bound to the exact candidate,
trusted market, canonical account facts, current execution state, and policy;
recheck that decision immediately before the first order write.

**Architecture:** Keep grid geometry/sizing pure and transparent. Add a small
`RiskDecisionPort` plus immutable `risk-request-v1` / `risk-decision-v1`
contracts. A paper-grid adapter recomputes candidate economics from the exact
commands plus canonical account and engine-neutral execution facts; it never
trusts the preview's fallback-derived risk numbers. A bridge adapter translates
the mature `LiveMoneyGuardrails` result into the same decision contract.
Strategy control and broker composition roots ask the port for permission;
execution engines never implement risk rules. The server derives economic
action class instead of trusting caller `source`. Exit, flatten, reduce-only,
and cancel remain available even when exposure-increasing actions are blocked.

**Tech stack:** Python 3.13, existing canonical accounting and execution
snapshots, existing LiveMoneyGuardrails, frozen JSON contracts, pytest.

---

## User outcome

The system can no longer show “risk budget exceeded” and then start the grid
anyway. Pulling a range wider still does not silently resize each rung: the
operator sees the original requested notional, the explicit blocker, and the
risk-budget notional recommendation. Legacy and Nautilus receive the same
risk decision for the same economic candidate. Closing risk is never trapped
behind an entry-risk gate.

## Observable success criteria

1. One immutable `risk-decision-v1` records exact candidate, canonical
   accounting snapshot ID/reconciliation, market, normalized execution-state,
   evaluator, and policy hashes plus explicit blockers, warnings, metrics,
   limits, and allowed action classes.
2. Grid start and running regrid fail before candidate-plan activation or
   runtime/order mutation when the account is unknown, market is
   untrusted/outside range, reconciliation
   drifts, projected leverage/margin exceeds policy, or maximum plan loss
   exceeds its budget.
3. A decision is rebuilt from fresh canonical account, market, and execution
   facts inside the shared production-mutation critical section immediately
   before the first submit. Any candidate/account/market/execution/policy
   mismatch is `risk_decision_stale` and writes no new orders; any staged
   plan/runtime state is restored by the same transaction.
4. Preview geometry, count, and per-grid notional remain unchanged. A blocked
   decision exposes `recommended_notional_per_grid` from the already-visible
   risk cap; it never applies that recommendation automatically.
5. Regrid replaces only pending entries, preserves open positions and their
   TP/SL, includes their remaining exposure and stop risk, and never treats
   cancellable old pending entries as permanent post-replacement exposure.
   Unknown protection/risk on an existing open position blocks new exposure.
6. Stop, flatten, protective exit, reduce-only, and cancel remain allowed even
   under daily-loss, leverage, notional, HALT, stale-account, or entry-limit
   blockers; malformed close identity still fails closed.
7. Every server-received exposure-increasing manual order is gated regardless
   of caller-supplied `source`; the manual path shares the same mutation lock
   as start/regrid. Historical/internal compatibility helpers are not network
   authorization boundaries.
8. Existing Binance/Tiger limits and blocker ordering remain behaviorally
   unchanged behind a live-risk adapter, while the broker boundary explicitly
   consumes the canonical allow/block decision.
9. Risk receipts are append-only audit evidence, never order/accounting
   authority. Legacy and Nautilus parity tests prove engine selection cannot
   change the economic risk outcome.

## Scope

- In: versioned risk request/decision contracts; paper StrategyPlan batch risk;
  canonical-account and execution-state binding; start/regrid/manual-entry
  composition roots; live-money guardrail bridge; pre-submit recheck;
  deterministic persistence; focused, integration, and full regression tests.
- Out: changing risk limits, auto-resizing a grid, redesigning the confirmation
  Card, changing the grid strategy, replacing broker reconciliation, live grid
  order routing, automatic position liquidation, Nautilus authority cutover,
  or deleting legacy `RiskEngine`/`RiskMonitor` research and monitoring views.

## First-principles invariants

1. Risk is a precondition to exposure mutation, not a display field.
2. A risk decision is useful only for the exact state it evaluated. Preview
   risk figures are display diagnostics and are never mutation authority.
3. The Risk Port decides; it never submits, cancels, fills, or edits a plan.
4. The execution adapter executes; it never invents risk policy.
5. Exposure-reducing safety actions cannot depend on entry permission.
6. Unknown money/account/protection facts block new risk, never closing risk.
7. “Recommended” and “applied” are different states and must stay visible.

## Milestone 1 — Freeze the risk contract and pure grid adapter

### User outcome

Every risk answer has one inspectable meaning and cannot be reused for a
different plan or account snapshot.

### Success criteria

1. Immutable request/decision builders reject NaN, non-JSON, missing identity,
   unknown action class, and malformed blockers.
2. Content identities bind the complete economic candidate, canonical
   accounting snapshot, normalized execution risk state, and resolved policy,
   not object paths or wall-clock defaults.
3. The grid adapter recomputes plan loss, leverage, and margin from exact
   commands and canonical equity; changing only preview risk fields cannot
   change its answer.
4. The grid adapter reports all applicable blockers, a stable primary blocker,
   and explicit `allow_exposure_increase` / `allow_reduce_only` flags.
5. Plan-loss, projected leverage, projected margin, range inclusion, account
   availability, and execution reconciliation are independently tested.
6. Suggested risk-budget notional is returned but the candidate remains byte-
   unchanged.

### In scope / Out of scope

- In: `schemas/risk.py`, `services/risk_port.py`, pure unit tests.
- Out: control-plane or broker wiring.

## Milestone 2 — Gate StrategyPlan start and regrid

### User outcome

No grid entry order can appear after a blocked or stale risk decision.

### Success criteria

1. Start requires an already-selected active plan and evaluates before candidate
   plan activation/runtime transition/order submission; risk rejection cannot
   auto-lock or supersede a plan.
2. Start/regrid recheck exact canonical account, market, and normalized
   execution inputs under the shared mutation lock immediately before the
   first submit.
3. Regrid binds old pending orders, open positions, and the replacement batch;
   it preserves the existing two-phase local-paper recovery semantics without
   counting old pending orders as post-replacement exposure.
4. A blocked or stale decision leaves active plan, runtime, pending orders, and
   positions unchanged; only risk/audit evidence may be appended.
5. Successful responses and runtime state expose the decision ID and policy ID.
6. The normalized execution risk state is projected through existing canonical
   accounting, with only a thin identity join for TP/SL fields that accounting
   does not currently preserve. Missing remaining units or protection blocks.
7. The same candidate under independent Legacy and Nautilus executions yields
   the same economic
   metrics, blockers, and allow/block result.

### In scope / Out of scope

- In: `services/strategy_control_plane.py`, dashboard strategy composition,
  control-plane/integration tests.
- Out: changing fill matching or switching execution authority.

## Milestone 3 — Bridge manual production orders and live guardrails

### User outcome

Manual production entries and broker entries cannot bypass the common risk
contract, while exits remain available during stress.

### Success criteria

1. The HTTP production-order boundary derives `exposure_increase` versus
   `reduce_only` server-side. Every entry, regardless of caller `source`,
   requires a fresh canonical decision bound to its command and optional
   StrategyPlan; non-entry close actions receive reduce-only permission without
   entry-limit evaluation.
2. The manual-order boundary uses the same production mutation lock and fresh
   state source as start/regrid; a concurrent order makes the decision stale.
3. Binance and Tiger adapters translate the exact existing
   `LiveMoneyGuardrails` result into `risk-decision-v1` without changing limit
   values, blocker precedence, dry-run behavior, or attended gates.
4. Broker network submission checks the canonical decision flag additively:
   existing activation, preflight, reconciliation, and attended gates remain
   mandatory and can never be weakened by translation.
5. Canonical `allow_exposure_increase` is exactly `not legacy.blockers`; missing
   or invalid bridge output fails new exposure closed.
6. The bridge never calls entry guardrails for reduce-only/close actions.
7. Existing live guardrail regression tests remain exact; new tests cover
   canonical receipt identity and exit/cancel non-blocking behavior.

### In scope / Out of scope

- In: dashboard manual-order boundary, broker adapter bridge, tests.
- Out: new broker types, relaxed canary limits, real-money enablement.

## Milestone 4 — Persistence, adversarial review, and closure

### User outcome

Risk behavior is auditable, restart-safe, and demonstrably independent of the
selected execution engine.

### Success criteria

1. Decisions persist append-only under `dualtrack/risk_decisions` (paper) or
   `risk_decisions` (venue), with deterministic validation and no authority
   over orders/accounting. No runtime path reads a stored receipt as permission.
2. Tampered/stale request, policy, evaluator, account, market, or execution
   binding fails closed.
3. Focused risk/control/broker tests, pinned Nautilus integration where needed,
   and the full repository suite pass.
4. Opus adversarial review covers stale-state TOCTOU, close-action lockout,
   regrid double exposure, engine-specific drift, hidden resizing, and
   fail-open exceptions; every valid P0/P1 is fixed and recorded.
5. Documentation and `decision-log.md` state the authority boundary and Gotchas.

### In scope / Out of scope

- In: audit-receipt persistence/validation, architecture spec, regression and
  review.
- Out: frontend confirmation Card and screenshots; A4 has no new visible UI.

## Gotchas

- Current auto-sizing intentionally uses leverage capacity and often exceeds
  the 10% plan-loss budget. A4 must block application, not change preview
  notional. The future confirmation UI can offer the explicit recommendation.
- `grid_sizing.account_equity()` currently defaults unknown accounts to
  `$10,000`. Preview compatibility may retain that behavior temporarily, but
  the mutation-time Risk Port must reject unknown equity and recompute every
  economic metric; it may not copy `preview.risk` or `plan.risk_budget` values.
- `accounting-snapshot-v1` normalizes remaining position quantity and account
  equity but currently drops `sl`/`tp`. The Risk Port may join those two fields
  back by canonical trade identity; it must not create a second P&L or quantity
  reducer.
- Regrid currently stages replacement orders before cancelling old pending
  orders. In the local paper adapters no market event is processed inside that
  critical section. This is not an atomic live-venue replace primitive; live
  grid routing remains out of scope and must not inherit that assumption.
- Open position risk must use remaining units and its actual SL. Entry price or
  realized P&L cannot substitute for missing protection evidence.
- The canonical production-history snapshot is the accounting source of truth.
  Compatibility callers that pass an explicit account dict remain testable,
  but the dashboard composition root must bind its accounting snapshot ID and
  reconciliation status.
- A HALT or daily-loss limit must block exposure increase but must never block
  protective orders, flatten, reduce-only, or cancel.
- Risk receipts are evidence, not reusable authorization tokens. The server
  always reconstructs and matches the request at the mutation boundary.
- `build_dualtrack_order_post_response()` is also used as an internal test and
  historical compatibility helper. The HTTP handler is the security boundary:
  it must enable risk enforcement explicitly after server market validation;
  caller `source` can never disable it.
- The primary worktree contains unrelated Debug/range work. A4 stays isolated
  and must never be integrated by copying whole files.

## Baseline

- Grid sizing, strategy control plane, live guardrails, and dashboard server:
  `76 passed` before A4.
- Opus planning review: verified success with two P0, four P1, three P2, and
  two P3 observations. All P0/P1 corrections above were accepted. The live
  bridge remains in A4 but lands only after the paper/manual gate and stays
  additive; audit receipt verification was cut because receipts never authorize.

## Completion boundary

A4 is complete when all exposure-increasing production paths in scope consume
one matching `risk-decision-v1`, blocked/stale decisions cause zero production
mutation, exit safety remains available, existing live limits remain exact,
Legacy/Nautilus risk parity passes, full regression passes, and Opus reports no
unresolved P0/P1. A4 does not change limits, auto-size positions, build the
confirmation Card, or authorize real money.
