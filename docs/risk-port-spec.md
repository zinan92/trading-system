# Unified Risk Port

## Purpose

Risk is a precondition to increasing exposure. It is not a chart estimate and
not an execution-engine side effect. Every in-scope production entry consumes
one immutable `risk-decision-v1` bound to the exact candidate and current
facts immediately before submission.

## Authority boundary

| Component | Owns | Must not own |
|---|---|---|
| Grid sizing | Pure geometry, count, spacing, requested notional, preview diagnostics | Permission to submit |
| Risk Port | Strategy/portfolio policy, exact loss/leverage/margin evaluation, allowed action class | Order submission, cancellation, fills, plan mutation |
| Execution adapter | Precision normalization, order lifecycle, fill semantics, engine reconciliation | Strategy risk policy |
| Nautilus RiskEngine | Final order-level execution checks such as precision, balance, rate limits, trading state, reduce-only | StrategyPlan selection or portfolio policy |
| Risk decision store | Append-only audit evidence | Reusable authorization or accounting truth |

Nautilus remains the mature last-line execution defense. The canonical port
sits above it so Legacy paper, Nautilus paper, Binance, and Tiger expose the
same application-level decision contract. See the official Nautilus
[execution model](https://nautilustrader.io/docs/latest/concepts/execution/),
[risk API](https://nautilustrader.io/docs/python-api-latest/risk.html), and
[order semantics](https://nautilustrader.io/docs/latest/concepts/orders/).

## Composition and plugin identity

- `risk_port.py` owns only the public evaluator/store protocols, canonical fact
  projection, request construction, permission checks, and stale-decision
  matching.
- `risk_policy_paper.py` owns the current grid economics and ordered blockers;
  `risk_policy_core.py` contains its provider-free pure helpers.
- `risk_policy_registry.py` registers evaluator implementations by explicit
  name, validates their implementation and source-bound evaluator identity,
  publishes a deterministic fingerprint, and freezes before use.
- `risk_policy_composition.py` is the only production construction boundary for
  the selected paper policy, live bridge, and file audit store. Explicit empty
  or unknown policy configuration fails before a runtime can mutate trading
  state.
- `risk_decision_store.py` and `risk_live_money_adapter.py` are separate
  adapters. The live bridge requires an injected store and cannot construct a
  concrete persistence implementation by itself.

Production currently selects `risk_policy.paper_grid=paper_grid_risk`. A new
policy must implement `RiskDecisionPort`, register a matching descriptor before
freeze, and return evaluator metadata identical to that descriptor. Every
request receives policy and evaluator metadata from that exact composed port;
callers cannot pair a custom evaluator with the built-in identity.

## Contract

`risk-request-v1` content-binds:

- exact normalized candidate commands and action class;
- canonical accounting snapshot identity and reconciliation;
- trusted market provenance and freshness;
- normalized execution orders, positions, and reconciliation;
- resolved policy limits;
- evaluator version and source hashes.

`risk-decision-v1` records:

- allow/block outcome and distinct exposure/reduce/cancel permissions;
- stable ordered blockers and warnings;
- recomputed loss, leverage, margin, and exposure metrics;
- effective limits;
- an optional recommendation that is never applied automatically;
- the complete bound request and content identities.

Any changed candidate, account, market, execution state, policy, or evaluator
produces a different request identity. A prior decision therefore becomes
stale and cannot authorize submission.

## Mutation flows

### Grid start and regrid

1. Build preview geometry without changing the requested notional.
2. Build the exact commands to be submitted.
3. Read canonical account, market, execution snapshot, and reconciliation.
4. Evaluate and append the decision as audit evidence.
5. Reject before plan, runtime, or order mutation if blocked.
6. Under the shared production lock, rebuild the request from the same adapter
   and require an exact decision match.
7. Submit the already-evaluated commands.

Regrid stages every replacement pending order before cancelling the old set.
The local paper critical section processes no market event between those two
steps. This is not an atomic live-venue replace guarantee.

### HTTP manual orders

The server derives action class from `event`; caller `source` grants no
permission. The HTTP handler enables risk enforcement only after validating
the server market. It shares the same production mutation lock as start and
regrid, evaluates the exact normalized command twice, and submits that same
command only after an exact match.

Historical/internal calls to `build_dualtrack_order_post_response()` keep an
explicit compatibility default. They are not a network authorization
boundary.

### Binance and Tiger

`LiveMoneyGuardrails` remains the venue policy authority. A thin adapter maps
its ordered blockers and limits into `risk-decision-v1`; it cannot turn a
legacy blocker or denial into permission. Broker submission consumes the
canonical flag in addition to all existing preflight, activation,
reconciliation, attended, and lifecycle gates.

The bridge grants new exposure only for legacy status `READY`, or the exact
intentional paper-route status `SKIPPED` with reason
`require_live_money_guardrails_before_entry=false`. A contradictory success
flag paired with any other status is converted into the canonical
`legacy_guardrail_status_invalid` blocker.

Dry-run/demo behavior is unchanged. Reduce-only and close operations do not
call entry money guardrails.

## Fail-closed rules

New exposure is blocked when any required money fact is unknown or when:

- canonical account identity or reconciliation is invalid;
- market data is stale, synthetic, provenance-free, or outside a grid range;
- execution state cannot be normalized or reconciled;
- an existing position lacks remaining quantity or valid stop evidence;
- a start would layer over existing pending entries or positions;
- a regrid replacement set differs from current pending entries;
- exact projected loss, leverage, or margin exceeds policy;
- the pre-submit request differs from the evaluated request;
- the live guardrail bridge is malformed, has an invalid success status, or
  denies entry.

Valid close, flatten, reduce-only, and cancel actions do not depend on entry
permission. Their position/order identity must still be valid.

## Persistence

- Paper decisions: `outputs/dualtrack/risk_decisions/`
- Venue decisions: `outputs/risk_decisions/`

History is append-only and deduplicated by decision identity. `current.json`
is an observability pointer only. No authorization path reads it.

## Known limits

- Preview still retains the legacy `$10,000` fallback for display
  compatibility; mutation-time risk rejects a missing canonical account.
- Risk percentages use fractional values in `[0, 1]`. A display-style value
  such as `5` fails closed as an invalid policy; it is never silently treated
  as 5%.
- The recommendation currently expresses a risk-budget notional ceiling. The
  future confirmation Card must require a separate operator action to apply it.
- Live grid order replacement is out of scope. The two-phase paper invariant
  must not be treated as a venue atomicity guarantee.
- A4 changes no risk limits and enables no real-money route.
