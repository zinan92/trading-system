# A6 Trading System Read Model Implementation Plan

**Status:** In progress on 2026-07-18.

**Goal:** Give every operator-facing GET and the production GridMind Dashboard
one immutable, versioned snapshot of the running strategy, market trust,
orders, positions, completed round trips, canonical P&L, risk, broker/execution
identity, and runtime state. The browser displays that answer; it does not
recalculate trading truth.

**Architecture:** Add a provider- and engine-neutral
`trading-system-read-model-v1` projection over the existing Market Data,
Execution, Accounting, Risk, Broker, and StrategyPlan ports. Assemble every
section from one request-scoped source snapshot and content-bind it with one
snapshot identity. Preserve the current Strategy Console API as a compatibility
facade, while GridMind switches to the stable read-model endpoint. Remove the
two known read-side provider branches and all durable writes from Dashboard GET
paths. This is a projection/cutover milestone, not a UI redesign.

**Reference model:** CQRS read models and NautilusTrader's cache/report model
both separate query projections from command/execution authority. A6 adopts
that split without adding event sourcing, a new database, or a second P&L
calculator.

**Tech stack:** Python 3.13, current canonical accounting/risk/broker/execution
ports, JSON HTTP API, existing vanilla GridMind frontend, pytest, Ruff, browser
acceptance.

---

## User outcome

The screen answers one question consistently: “What strategy is running right
now, through which execution/broker path, with how many orders, positions and
trades, at what P&L and risk?” Every number belongs to the same snapshot and a
refresh cannot create a StrategyPlan, close a trade, mark P&L, or otherwise
change trading state.

## Observable success criteria

1. One versioned `trading-system-read-model-v1` response contains a stable
   snapshot ID, generated time, source identities, cycle, market trust,
   StrategyPlan summary, runtime, execution/broker identity, canonical
   accounting, risk, and review/shadow references.
2. Counts have explicit semantics in the same snapshot: open entry orders,
   open positions, started trade lifecycles, completed round trips, and fills
   are distinct. “One open” and “one open then close” both remain one trade;
   only the latter is one completed round trip.
3. Current strategy direction, style, grid mode/range/count, per-grid notional,
   spacing, plan ID/version, engine, and data provider are backend-projected;
   the browser does not infer them from provider files or recompute P&L/risk.
4. All Dashboard GET builders are durable-state read-only. Repeated GETs leave
   plans, runtime, orders, fills, positions, trades, risk decisions, and paper
   artifacts byte-identical and do not create missing compatibility plans.
5. Market, execution mark, accounting, and runtime are assembled from one
   request snapshot. A second provider read cannot silently make the execution
   P&L disagree with the displayed price.
6. GridMind consumes the new endpoint and visibly shows the authoritative
   counts and running-strategy summary. The existing current-console endpoint
   and command POSTs remain backward compatible.
7. The read model and runner diagnostic projection contain no Binance/Tiger or
   Legacy/Nautilus selection branch. Adding an adapter changes composition and
   conformance tests, not the Dashboard projector.
8. Focused/full regression, Ruff, verified Opus review, and desktop/mobile
   browser evidence pass without changing the active strategy, config,
   credentials, broker authority, or risk limits.

## Scope

- In: stable read-model contract/projector; request-scoped assembly; GET purity;
  broker/reconciliation diagnostic projection; API route; GridMind data
  binding and count labels; compatibility facade; focused/full tests; browser
  evidence and review.
- Out: redesigning all historical Dashboards; replacing the artifact store;
  event sourcing; changing StrategyPlan, order, fill, risk, accounting, or
  matching semantics; activating a broker; changing live/paper authority;
  implementing range dragging or notification UX.

## First-principles invariants

1. A query observes state; it never creates or advances trading state.
2. AccountingSnapshot remains the only P&L/trade-count authority. The read
   model copies its answer and labels unknown values honestly; it never
   reconstructs P&L from UI-visible rows.
3. RiskDecision remains authorization evidence, not reusable permission. The
   read model may display it only when its identity matches the current runtime
   or clearly labels it historical/stale.
4. Provider and engine names are data, not control flow. Composition selects
   adapters; the read model projects normalized descriptors and capabilities.
5. One response uses one market observation for display, execution marking,
   and accounting. Section timestamps and source snapshot IDs remain visible.
6. Compatibility aliases may translate the stable model for old consumers,
   but no new frontend code may depend on provider-native payloads or internal
   ledger paths.
7. Missing or inconsistent facts degrade/block the read model honestly. They
   never become a fabricated zero or a green “running” state.

## Milestone 1 — Freeze the stable snapshot contract

### User outcome

All current-state numbers have one schema, one identity, and explicit meaning.

### Success criteria

1. A pure projector emits `trading-system-read-model-v1` and a deterministic
   snapshot ID from JSON-safe source facts.
2. Strategy, market, runtime, execution, broker, accounting, risk, health,
   counts, and research/review sections have stable documented fields.
3. Counts are copied from canonical accounting plus current execution state;
   started trades and completed round trips cannot be conflated.
4. Strategy summary and grid spacing are projected once in Python.
5. Unknown/malformed source sections produce explicit completeness issues
   without crashing or manufacturing zeros.
6. Broker descriptors expose only safe identity, environment, capabilities,
   credential environment-variable names, and sanitized readiness.

### In scope / Out of scope

- In: `services/trading_system_read_model.py`, schema tests and documentation.
- Out: HTTP routing and frontend changes.

## Milestone 2 — Make every GET observational

### User outcome

Refreshing the page can never change an order, trade, plan, or P&L artifact.

### Success criteria

1. `StrategyControlPlane.read_model()` reads the active plan only; legacy plan
   migration remains available solely through an explicit command/mutation
   path.
2. `DashboardState.snapshot()` no longer evaluates exits or marks positions to
   market. Scheduled/reporting services retain those explicit responsibilities.
3. Production-console assembly shares one market snapshot with the execution
   mark and accounting projection.
4. A filesystem fingerprint test proves repeated GET builders create and
   modify no durable trading artifacts. It covers `/api/dashboard`,
   `/api/system/status`, `/api/trader/overview`, `/api/ops/status`, and the
   un-migrated legacy-plan edge of `/api/strategy-console/current`.
5. Existing POST command behavior, idempotency, risk checks, ambiguous-outcome
   reconciliation, and compatibility migration remain unchanged.

### In scope / Out of scope

- In: read-side control plane, DashboardState, source assembly, GET purity
  tests.
- Out: moving scheduled paper-exit evaluation to a new scheduler; it already
  exists in reporting/daily-review flows.

## Milestone 3 — Cut API and GridMind over

### User outcome

The production screen shows one authoritative strategy/runtime/account answer,
including understandable counts, without frontend trading arithmetic.

### Success criteria

1. `GET /api/trading-system/read-model` validates query inputs and returns the
   stable contract with `no-store` semantics.
2. One named pure `_assemble_strategy_console_snapshot` builds request-scoped
   sources. `GET /api/trading-system/read-model` and
   `GET /api/strategy-console/current` both delegate to it; neither endpoint
   calls the other.
3. GridMind loads the new endpoint and binds strategy, runtime, execution,
   accounting, risk, and counts from their stable sections.
4. Account cards no longer sum P&L or calculate return in JavaScript; current
   order/position/trade/round-trip counts come directly from the snapshot.
   Static tests explicitly reject client-side trading arithmetic and
   provider/engine display branches.
5. The three operational tabs show authoritative counts in parentheses and the
   trade count follows lifecycle semantics rather than counting entry and exit
   as two trades.
6. Controls remain POST-only and continue to reconcile uncertain outcomes
   against the new read endpoint.

### In scope / Out of scope

- In: Dashboard server route, GridMind bindings, static/API/browser tests.
- Out: visual redesign, range-drag feature, audio/toast redesign, deprecated
  Dashboard removal.

## Milestone 4 — Remove diagnostic leakage and close A6

### User outcome

Changing broker or execution engine no longer requires editing Dashboard or
runner presentation logic.

### Success criteria

1. Runner execution-profile output is built from the selected Broker Port
   descriptor/readiness, not provider-specific branches.
2. Reconciliation status/block reason uses one normalized projection rather
   than Binance/Tiger labels in orchestration.
3. Provider-neutrality tests cover the new read projector, GridMind endpoint,
   and runner diagnostics.
4. Focused tests, full suite, Ruff, and verified Opus review have no unresolved
   P0/P1.
5. Desktop and mobile browser captures prove the strategy summary and counts
   render from the new schema and are saved in the project Evidence folder.
   One attended test capture also proves a safe POST control round trip is
   reflected by the new GET without making GET itself authoritative.

### In scope / Out of scope

- In: normalized broker read projection, conformance tests, docs, decision log,
  visual acceptance.
- Out: moving venue networking code or switching execution authority.

## Task batches

### Batch 1 — Contract and query purity

1. Add failing contract/count/completeness/snapshot-identity tests.
2. Implement the pure stable projector.
3. Make Strategy Control Plane and legacy Dashboard snapshot reads pure.
4. Thread one market snapshot through execution marking.
5. Add durable filesystem fingerprint tests across all four legacy
   `DashboardState.snapshot()` endpoints and the un-migrated console case.
6. Run the focused baseline plus new read-side suite.

### Batch 2 — API and frontend cutover

1. Add the stable GET route and request builder.
2. Preserve the legacy endpoint as a facade.
3. Switch GridMind core rendering and uncertain-outcome reconciliation to the
   new endpoint.
4. Render strategy/count/account/risk facts from backend fields.
5. Add static and API compatibility tests, including explicit rejection of
   frontend P&L/count/strategy/provider/engine arithmetic branches.
6. Run browser acceptance at desktop and mobile widths plus one safe
   POST-to-GET control round trip.

### Batch 3 — Adapter-neutral diagnostics and closure

1. Replace runner provider-specific diagnostic branches with the normalized
   broker/reconciliation read projection.
2. Run provider-neutrality, broker, execution, accounting, risk, control, and
   Dashboard regressions.
3. Run Ruff and the full repository suite.
4. Submit the isolated diff and threat checklist to verified Opus review.
5. Fix every valid P0/P1, rerun proportionate/full verification, capture final
   visual Evidence, and record Gotchas.

## Gotchas

- `DashboardState.snapshot()` currently calls `PaperExecutor.evaluate_exits`
  and `mark_to_market`; removing these hidden command effects can expose stale
  scheduled artifacts that the GET previously masked. The read model must show
  staleness honestly, not restore the side effect elsewhere in the query path.
- `StrategyControlPlane.read_model()` currently materializes a legacy active
  plan. Existing mutation commands already own an explicit compatibility
  migration path; tests must prove the GET no longer writes while commands
  still migrate safely.
- The current console obtains market data more than once per response. A price
  movement between reads can make displayed price and P&L internally
  inconsistent even when each individual source is valid. The shared assembler
  must pass its one market observation into execution marking and production
  accounting; those builders may not re-read for the same response.
- The AccountingSnapshot contains both current-cycle and all-plan views in
  different projections. Open orders/positions come from the selected current
  execution snapshot; started/completed lifecycle totals come from canonical
  versioned production history. Their scopes must be explicit.
- A RiskDecision `current.json` is observability only. It cannot authorize a
  new order and cannot be presented as current if its decision ID differs from
  runtime.
- Broker preflight can inspect environment presence and file permissions but
  must not open a network client. The read model must redact literal secret
  values, credential paths, signatures, raw acknowledgements, and commands.
- The GridMind browser currently derives run inconsistency, counts, total P&L,
  return, and some strategy labels. Moving these fields backend-side must keep
  control availability fail-closed and cannot loosen POST authorization.
- GridMind also branches on `binance_usdm`, `legacy_paper`, and
  `nautilus_paper` for display labels. A6 projects normalized labels and removes
  those branches; provider/engine identifiers remain data for diagnostics.
- The primary worktree contains unrelated Debug, range-drag, and product
  changes. A6 remains isolated; integration must be commit-by-commit and never
  overwrite the primary HTML or server wholesale.

## Baseline

- Focused Dashboard server/GridMind, Strategy Control Plane, system-state,
  Accounting, Broker Accounting, and provider-neutrality suite before A6:
  `93 passed`.
- A5 full repository baseline: `1719 passed, 7 skipped`.
- Existing stable pieces to reuse: `accounting-snapshot-v1`,
  `risk-decision-v1`, `broker-port-descriptor-v1`, the configured
  `ExecutionEngineAdapter`, StrategyPlan versioning, and Market Data Envelope.

## Opus planning review

- Verified `claude-opus-4-8` review, session
  `17644104-235a-4d4f-8080-9c523ba29b0b`, receipt
  `20260717T230955Z_35299a00-c6e8-4993-930e-16903cd2ec33.json`: no P0; safe
  to implement after the accepted corrections below.
- Accepted P1: fingerprint all four snapshot-backed GET endpoints and seed the
  conditional un-migrated legacy-plan case so purity cannot pass vacuously.
- Accepted P1: thread one request-scoped market observation through display,
  execution mark, and production accounting.
- Accepted P1: both the new endpoint and compatibility facade delegate to one
  pure named assembler; neither calls the other.
- Accepted P1: pair screenshots with static tests proving client-side
  P&L/count/strategy arithmetic is gone.
- Accepted P2: project provider/engine labels instead of branching in GridMind,
  and capture one safe POST-to-new-GET control round trip.

## Completion boundary

A6 is complete only when the production GridMind reads the stable model,
current strategy/count/P&L/risk/runtime facts share one snapshot, all GET query
paths are durable-state read-only, provider-specific diagnostics are removed,
compatibility endpoints and POST commands remain safe, browser Evidence exists,
the full suite is green, and verified Opus reports no unresolved P0/P1. A6
does not redesign every Dashboard or authorize any trading/config change.
