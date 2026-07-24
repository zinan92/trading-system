# Trading-system implementation plan — 2026-07-24

**Status:** approved for staged implementation. It does not authorize an
attended Paper lifecycle, deployment that changes Paper lifecycle, or any
real-money action; those retain their explicit gates below.

## 1. Product expectation

Build a **Paper-first, modular, explainable, auditable, and evolvable** trading
system — not merely a Dashboard that can send a start request.

- **One execution truth:** Nautilus supplies backtest, matching, and Paper
  execution semantics. Grid and DCA use the same risk, execution, and
  accounting vocabulary.
- **Ports/adapters:** market data, candles, analysis, strategies, risk,
  execution, accounting, and Dashboard are replaceable modules. Changing a
  provider must not silently change calculation or risk meaning.
- **Operator product:** the user can see the active strategy, orders,
  positions, TP/SL, P&L, trusted-market state, blockers, and the next action
  without inspecting logs or source.
- **Strategy scope:** Grid handles range markets with a repeatable
  entry→TP→original-level-reorder lifecycle. DCA handles directional markets:
  additions accumulate a position and its single aggregate TP quantity is
  updated; TP/SL ends the whole round.
- **Evolution:** 5–10 same-window Strategy Shadows compare explicit What-if
  variants. A shadow can only recommend promotion after enough comparable
  evidence (initially 100 valid trades) and cross-period persistence. It never
  changes the production strategy itself.
- **Self repair:** safe service/read-model/diagnostic recovery may be
  automated and receipted. Anything affecting orders, positions, strategy
  parameters, leverage, risk limits, or real money requires human approval.
- **Real-money boundary:** excluded from this plan. It remains behind separate
  live-readiness, attended-canary, and Park `park-approved` gates.

## 2. Single progress baseline

**Authoritative whole-product progress: 65%.** This supersedes the older
`70%` figure in `docs/trading-roadmap-2026-07-20.md`, which measured an
earlier, narrower roadmap. The reduction is not a regression: this plan adds
real Paper lifecycle proof, self-evolution, and controlled self-repair to the
definition of done.

| Area | Present evidence | Remaining outcome |
|---|---|---|
| Architecture | 19-section ports/adapters refactor and Nautilus Paper authority | adapter contract suite, deployment/runtime compatibility, release gate |
| Runtime/data | trusted-market gate, tick health, old-cycle protection, classified failures | one operational truth and complete recovery diagnosis |
| Grid | preview, risk review, start, cycle/reorder, closeout code | repeated real Paper lifecycle + reconciliation proof |
| DCA | entries, aggregate TP generations, stop, preview, read model | first real two-addition → TP-update → TP/SL exit proof |
| Dashboard | primary UI paths and focused browser coverage | final human journey acceptance and regression-only chart review |
| Shadows | production baseline and limited What-if support | comparable 5–10 variants, evidence threshold, promotion proposal |
| Self repair | some fail-closed detection/explanation | bounded diagnose→recover→verify→receipt loop |

**Important:** a UI test, mocked control response, or successful static build
is never evidence that a trade happened.

## 3. Operating rules for every later Issue

1. One Issue = one user story = one branch = one PR. The Issue contains
   Outcome, 3–7 testable criteria, in/out scope, risk, and exact verification.
2. Run focused tests for local changes; broaden only for cross-module work.
   Every PR has What / Why / Validation.
3. Behavior-changing PRs update `decision-log.md` with a decision and Gotchas.
   **Pure deployment, registry-status, or evidence-only PRs are exempt** from
   decision-log entries.
4. Default tests and deployments do not start, stop, cancel, or flatten Paper.
   A real Paper lifecycle observation requires a separately approved attended
   window and must never access a live key/path.
5. Completion evidence includes tests, relevant browser/JSON evidence, and a
   statement of Paper/live side effects. Update REGISTRY only with the current
   state and next action; do not paste long plans into it.
6. Automated repair has a deny-list: orders, positions, plan changes,
   leverage, risk policy, and real-money actions must be
   `requires_human_confirmation`.

## 4. Milestones and Issue contracts

### M1 — Operational truth and recoverability (P0)

**Outcome:** API, Dashboard, runtime JSON, and ledger describe the same
operational reality, including degraded and old-cycle states.

#### Epic M1.A — Authoritative runtime state

**M1-01 — Runtime-state contract (L; plan before implementation)**

- Goal: design the single read-model contract for current/previous cycle,
  runtime, open/accepted orders, positions, tick health, and reconciliation.
- Done: every allowed state has ownership, fields, invariants, and an operator
  action; no page derives a competing truth; unresolved prior orders can never
  display as zero.
- Verify: contract document first; fixture matrix for running/stopped/error/
  stale/previous-unresolved; API schema tests; browser rendering tests.
- Out: no strategy or order behaviour change in this design Issue.

**M1-02 — Tick failure phase diagnosis (delta after #171/#185/#192/#194)**

- Goal: distinguish remaining route failure and ledger-write failure stages;
  do not rebuild the existing heartbeat gate.
- Done: status names the stage, records a recovery action, and writes a
  heartbeat only after the existing full-success condition.
- Verify: route-failure and ledger-failure fixtures; existing start-gate
  regressions; read-only launchd/read-model evidence.

**M1-03 — Closeout fault injection (delta after #172/#189)**

- Goal: prove retries preserve order identity after failures during closeout.
- Done: one terminal package only; no duplicate cancellation/plan; retry keeps
  order identities; current planning remains blocked until old closeout is
  authoritative.
- Verify: lifecycle fault-injection fixtures against terminal package, runtime,
  and ledger. No normal rollover rewrite.

#### Epic M1.B — Failure explanation and bounded recovery

**M1-04 — Failure-copy compatibility matrix (delta after #145/#146/#216)**

- Goal: prevent drift between machine code, Chinese explanation, and next step
  across API, toast, dialog, and runtime card.
- Done: every currently supported failure code has one canonical fixture;
  response-loss is never called “not executed”.
- Verify: API/browser failure-chain matrix; snapshot assertions for code, title,
  explanation, and next action.

**M1-05 — Safe recovery receipts**

- Goal: make service/read-model/cache recovery auditable without creating a
  hidden execution retry.
- Done: allowed actions are explicitly service/display only; each action has a
  receipt; uncertain `start` is reconciled, never blindly retried.
- Verify: permission-matrix tests; mocked recovery receipts; negative assertion
  of no order-control call.

**M1 exit:** one fixture produces identical operational facts in all surfaces;
tick loss, old-cycle residue, and lost responses are clear and fail closed.

### M2 — Grid and DCA real Paper lifecycle evidence (P0)

**Outcome:** strategy semantics are proven by auditable Paper events, not by
screenshots or mocks.

#### Epic M2.A — Grid lifecycle

**M2-01 — Grid lifecycle acceptance package**

- Goal: define entry→TP→original-price-reorder evidence for one grid level.
- Done: audit links one grid level, order/trade IDs, StrategyPlan, TP, reorder,
  and reconciliation pass.
- Verify: deterministic replay fixture; in a separately approved attended
  Paper observation, archive lifecycle JSON and before/after read-models.

**M2-02 — Multi-level traversal semantics**

- Goal: state exact behaviour when price crosses multiple levels faster than
the available feed granularity.
- Done: no invented fills from a 1m candle; event ordering, price source, and
unsupported precision are explicit.
- Verify: event-sequence tests, market-event replay, and one written example.

**M2-03 — Grid accounting chain**

- Goal: reconcile orders, fills, trades, fees, realised/unrealised P&L.
- Done: a completed open+close is one round trip; missing identities fail
closed; UI counts match accounting.
- Verify: accounting fixtures, read-model/table browser tests, terminal package
reconciliation pass.

#### Epic M2.B — DCA lifecycle

**M2-04 — Aggregate TP contract**

- Goal: lock down how every DCA addition changes the aggregate TP generation
and quantity.
- Done: TP is labeled event-driven aggregate protection, not entry order;
generation, quantity, price, failure behaviour, and whole-round exit are
unambiguous.
- Verify: unit/replay cases for one, two, and many additions plus TP-update
failure. No Paper control action.

**M2-05 — First attended DCA lifecycle observation (parallel P0)**

- Goal: collect the first real DCA proof rather than wait behind unrelated UI
work.
- Prerequisite: Park explicitly authorizes one attended Paper window; tick
healthy; no unresolved old cycle; reconciliation current; no live path.
- Done: two additions occur; aggregate TP generation/quantity increases; TP or
SL exits the entire round; `dca_lifecycle`, orders, fills, positions, and final
reconciliation can be cross-checked.
- Verify: preserve only evidence and receipts. The work must not autonomously
start a DCA strategy; the human controls the Paper action.

**M2-06 — Strategy-switch residue test (delta after #164)**

- Goal: extend existing Grid/DCA mutual exclusion with protection cleanup.
- Done: plan IDs/audit directories remain separate; a simulated switch leaves
no previous protective order or stale lifecycle projection.
- Verify: integration/read-model/browser fixtures, no actual orders.

**M2 exit:** at least one full Grid and one full DCA Paper lifecycle with
event-chain, audit JSON, and reconciliation `pass`.

### M3 — Operator product acceptance (P1)

**Outcome:** a non-developer can safely understand and operate the Paper
product without guessing what a button or status means.

#### Epic M3.A — Parameter and control journey

**M3-01 — Parameter draft-state matrix**

- Goal: make AUTO/manual, changed/computing/invalid/risk-awaiting-confirmation
explicit for Grid and DCA.
- Done: edits recalculate dependent variables; malformed values identify a
field and make no control request; well-formed risk choices reach explicit
human confirmation rather than a vague rejection.
- Verify: browser matrix across range/count/profit/notional/leverage/direction/
strategy type; negative network-control assertions.

**M3-02 — Range-drag contract regression**

- Goal: keep pre-start and running Range drag/preview/confirm/cancel coherent.
- Done: drag has zero order side effects; boundary/middle rules, old→new card,
risk acknowledgement, and cancel semantics match the contract.
- Verify: Playwright drag/zoom/pan; preview API contract; no-side-effect tests.

**M3-03 — Start/stop outcome clarity**

- Goal: make prepare/start/confirm/stop wait, success, rejection, and unknown
result understandable.
- Done: start failures use the central dialog; trade events use notification;
stop states show actual order/position closeout; response loss is reconciled.
- Verify: mocked success/reject/response-loss browser cases and read-model
reconciliation tests; no real Paper action.

#### Epic M3.B — Readability and chart evidence

**M3-04 — Chart regression audit (gate, not assumed implementation)**

- Goal: first determine whether #68/#101/#102 historical paging, 240-bar view,
bottom time axis, and Y autoscale have actually regressed.
- Done: publish a browser evidence matrix. If no regression exists, close the
Issue with evidence; only a reproduced regression creates a follow-up fix.
- Verify: browser history/pan/zoom fixtures and screenshots against current
main. Do not pre-authorise a rewrite.

**M3-05 — Production-summary/table completeness**

- Goal: make current strategy and order/position/trade data compact but
complete.
- Done: strategy type/direction/range/count/notional/leverage/target appear in
one summary; TP/SL/quantity/P&L/count semantics are visible and aligned.
- Verify: static/read-model/browser table checks and desktop screenshot.

**M3-06 — Operator journey expansion (delta after #180)**

- Goal: extend, not replace, the existing main Playwright journey.
- Done: coverage explicitly includes invalid draft recovery, Range drag,
DCA selection, notifications, 12h review, Shadows, and historical-chart audit
state.
- Verify: one continuous Playwright path plus focused chart tests; mocks or
read-only state only.

**M3 exit:** a new operator can answer current strategy, blocker, modification,
order/position result, and next action from the screen alone.

### M4 — Comparable Strategy Shadows and review (P1)

**Outcome:** “what if we used another grid/side?” becomes a same-contract
experiment, not an attractive but incomparable backtest.

#### Epic M4.A — Same-window What-if engine

**M4-01 — Shadow comparability contract**

- Goal: version market input, window, fees, execution version, plan, input
hash, and non-comparable reasons.
- Done: input/fee/execution mismatch cannot be shown as a performance delta;
each result shows exactly what changed and its baseline.
- Verify: schema, hash/window, comparable and non-comparable fixtures.

**M4-02 — Finite 5–10 Grid variants**

- Goal: generate explainable variants (neutral/one-sided, style, count, range,
notional) rather than arbitrary parameter search.
- Done: every variant has ID, declared parameter difference, and isolated
execution; production plan, orders, and ledger are immutable.
- Verify: isolated Nautilus/replay tests and production-immutability assertions.

**M4-03 — 12-hour review interpretation**

- Goal: display plan, execution, P&L, drawdown, count, capital use,
reconciliation, and What-if results in one comparable review.
- Done: insufficient evidence says “not comparable”; one period cannot create a
promotion claim.
- Verify: package/read-model/browser rendering and readable screenshot.

**M4 exit:** every completed cycle can explain baseline, variants, and why a
comparison is or is not valid.

### M5 — Evolution proposals and bounded self repair (P2)

**Outcome:** evidence becomes a human-reviewed recommendation or a safe repair
task — never an unsupervised trading change.

#### Epic M5.A — Evidence-based proposals

**M5-01 — Sample and persistence threshold**

- Goal: define valid trade, 100-trade initial threshold, cross-period
persistence, drawdown/cost limits, and versioned parameters.
- Done: missing, incomparable, or too-small samples cannot declare a winner.
- Verify: synthetic ledgers for pass/fail/missing/abnormal cases.

**M5-02 — Promotion proposal**

- Goal: generate a recommendation with evidence, counter-evidence, risk
change, and human confirmation route.
- Done: proposal has evidence IDs/window/contract and leaves production plan
unchanged.
- Verify: proposal schema + Dashboard tests; assert no control call.

**M5-03 — Safe-repair queue**

- Goal: queue service/cache/read-model recoveries with before/after evidence.
- Done: forbidden domains are marked `requires_human_confirmation`; no route
can mutate order/risk state automatically.
- Verify: permission-matrix, receipt, and negative mutation tests.

**M5 exit:** the system can explain both a strategy recommendation and a
recovery recommendation without crossing the trading-authority boundary.

### M6 — Adapter contracts, release gate, and Paper operations (P1)

**Outcome:** swapping providers, adding strategies, and releasing Paper code
are repeatable engineering operations rather than bespoke debugging.

#### Epic M6.A — Interfaces and release discipline

**M6-01 — Adapter contract suite**

- Goal: freeze normalized contracts for market feed, execution, accounting,
and strategy evaluation adapters.
- Done: supported adapters return the same normalized data/quality/error
meaning for the same fixture.
- Verify: provider matrix (Binance, fixture, future Yahoo/Tiger) and schema
diff gate.

**M6-02 — CI runtime compatibility (delta after #179)**

- Goal: move the existing Python 3.9 import receipt into an enforceable CI or
pre-deploy gate; do not recreate the compatibility check.
- Done: actual launchd interpreter import/API smoke must pass before restart;
failure blocks deployment but changes no runtime process.
- Verify: CI/pre-deploy command, receipt, and deliberate incompatible fixture.

**M6-03 — Paper release/rollback runbook**

- Goal: standardise merge, deployment, health check, changed-flow browser
proof, rollback, and evidence location.
- Done: each release records commit, health, runtime counts, changed-flow proof,
and a no-lifecycle-side-effect statement.
- Verify: dry-run runbook, read-only evidence, rollback simulation.

**M6 exit:** a provider/strategy can be added without changing core accounting
or risk semantics, and a Paper release is reproducible, reversible, and
auditable.

## 5. Ordering and review gates

1. Review this plan before opening implementation Issues.
2. First code work: **M2-04** (pure DCA contract tests) can start immediately.
3. **M2-05** is parallel and waits only for Park’s explicit attended Paper
window; it does not wait for the whole M1 backlog.
4. **M1-01** starts as an L-sized design/contract Issue, then implementation is
planned from the approved contract.
5. Remaining M1 deltas precede broad UI work; M3 follows stable semantics.
6. M4 precedes M5; M6 runs where its contracts/release gates are needed.

The plan succeeds when Paper strategy execution is continuously evidenced and
reconciled, operator state is understandable, strategy recommendations use
enough comparable evidence, and all trading-impacting actions remain under
explicit human authority.
