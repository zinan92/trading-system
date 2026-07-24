# Authoritative runtime-state contract v1

**Status:** design contract for #227. It describes the state that the Paper operator is allowed to see. It does not change execution, risk, orders, positions, or deployment behaviour.

## Purpose and boundary

`GET /api/trading-system/read-model` is the only Dashboard state authority. The browser must render its `runtime`, `execution`, `market`, `risk`, and `completeness` projections; it may keep only ephemeral UI state (a draft, dialog, request-in-flight flag, or visual selection). It must never derive a safer trading state from a missing field, a previous response, or a count it calculated itself.

Commands remain separate. A successful HTTP response is not execution proof; the next read model plus the corresponding execution snapshot is the truth.

This contract covers Paper only. Nothing here grants real-money authority.

## Sources, ownership, and precedence

| Fact | Owner | Read-model rule | Browser rule |
|---|---|---|---|
| Current cycle | cycle clock / read-model contract identity | `contract.source_identities.cycle_id` and `execution.cycle_id` must agree; disagreement is incomplete | Show unavailable/degraded; do not infer a cycle |
| Desired and actual runtime state | `strategy_control/runtime.json` via `StrategyControlPlane.runtime_state` | Preserve both fields; mismatch is degraded | Never replace actual with desired |
| Active StrategyPlan identity | immutable current-cycle plan | A running runtime and plan ID/version must match | Never call a running strategy healthy without the matching plan |
| Orders and positions | configured authoritative execution adapter snapshot | Use snapshot rows and counts; unknown rows/counts are not zero | Render unknown explicitly and disable new exposure |
| DCA target state | `dca_lifecycle/<cycle>.json` reconciled with adapter snapshot | Show aggregate target as event-driven protection, not an entry order | Do not describe a missing entry order as missing DCA TP |
| Accounting and reconciliation | current-cycle canonical accounting + adapter reconciliation | Current `pass`/`ok` is required for a healthy start; historical accounting is labelled historical | Do not substitute historical reconciliation for current-cycle status |
| Tick liveness | current-cycle complete `dualtrack-live-tick` heartbeat | Nautilus Paper running state exposes ready/blocked plus reason and age | Show blocked tick as `运行降级`, never as normal running |
| Market trust | market read model | Trusted and fresh fields are independent from a displayed last price | A retained last trusted candle is view-only; do not enable exposure |
| Previous-cycle state | persisted runtime row when its cycle differs | `previous_*` fields are always explicit; unresolved is never masked | Surface the prior cycle and its count; block new starts |
| Control attribution | append-only control audit | Last action/event is explanatory only; it cannot override runtime/snapshot truth | Never render a toast or request response as final state |

Precedence is deliberately narrow: adapter snapshot owns executable orders and positions; the runtime row owns intent/transition; the current reconciliation owns accounting health. When facts disagree, the projection is **degraded** and records a completeness issue. No source can erase another source's negative evidence.

## Required state matrix

| Operator state | Required facts | Invariants | Operator action | Must not do |
|---|---|---|---|---|
| `stopped` | actual/desired stopped; current snapshot counts known | New start is possible only if market/trust, current reconciliation, and prior-cycle gates pass | Edit/preview/prepare a Paper strategy | Claim a plan is running because historical rows exist |
| `starting` | runtime transition is recorded; counts may still change | Do not say start succeeded until runtime is running and adapter state matches | Wait for a refreshed read model | Retry `start` automatically |
| `running` | matching current plan, known adapter counts, trusted market, current reconciliation, ready tick for Nautilus | All identity, count, and health checks pass | Observe execution or issue an explicit stop | Infer accepted orders from plan geometry |
| `degraded` | actual state may remain running, but one or more invariants fail | The precise completeness/tick/identity reason is preserved | Read the displayed cause; use only safe reduction/cancel policy where offered | Start/replan/increase exposure |
| `stopping` | transition recorded; adapter settlement still pending | It is not stopped until accepted orders and positions are zero and reconciliation is ok | Wait for the receipt/read-model | Start another strategy |
| `error` | failed transition and `last_error` are recorded; snapshot is still read | Unknown final counts remain unknown | Inspect status and decide a safe follow-up | Treat error as zero orders/positions |
| `previous-cycle-unresolved` | current cycle may be stopped but `previous_runtime_unresolved=true` | Prior cycle ID, state, and accepted count are non-null and visible | Resolve/close the prior Paper cycle | Start Grid or DCA in the new cycle |
| `unavailable` | source cannot be read or contract identity is invalid | Missing is not zero; no start capability is derived | Refresh/check service state | Display stale cached data as live truth |

`status=degraded` is a projection, not a replacement for `actual_state`. Consumers must retain both. A stopped current cycle with an unresolved prior cycle is `stopped` operationally but must be rendered as `previous-cycle-unresolved`, not a normal idle state.

## Cross-field invariants

1. `runtime.actual_state=running` requires a current StrategyPlan whose ID and version match `runtime.strategy_plan_id` and `runtime.strategy_plan_version`.
2. A known adapter snapshot is the only source for open/accepted order and open-position counts. If a snapshot is unavailable or contains unknown rows, the count is unknown, never `0`.
3. New exposure requires: trusted/fresh market; current-cycle reconciliation pass/ok; no previous unresolved runtime; zero conflicting accepted orders and positions; and, for Nautilus Paper, a fresh complete tick heartbeat.
4. A running Nautilus Paper strategy with a blocked heartbeat is degraded even if its last price is fresh.
5. If the persisted runtime belongs to a previous cycle, its accepted count and actual state are exposed in `previous_*`; a nonzero count or a running-like state blocks both Grid and DCA starts.
6. The Dashboard can show historical accounting and fills, but cannot use them as current-cycle readiness evidence.
7. DCA has exactly one active aggregate-target generation for the reconciled open quantity. It is displayed separately from entry orders and cannot be counted as an accepted entry order.
8. Control responses and notices are provisional. A command result is only confirmed after a refreshed authoritative read model satisfies the relevant state invariants.

## Contract shape consumed by the browser

The existing `trading-system-read-model-v1` remains the response schema. The implementation follow-up must make these fields stable and documented:

```text
contract.source_identities.cycle_id
runtime.{desired_state,actual_state,status,status_label,updated_at,
         strategy_plan_id,strategy_plan_version,strategy_type,
         open_order_count,unknown_order_count,accepted_order_count,
         accepted_order_count_known,execution_tick_health,liveness_degraded,
         stale_cycle,previous_cycle_id,previous_actual_state,
         previous_accepted_order_count,previous_runtime_unresolved,last_error}
execution.{cycle_id,engine,orders,open_orders,accepted_orders,
           positions,open_positions,reconciliation,dca_lifecycle}
market.{trusted,fresh,retained_last_trusted,latest_timestamp,latest_close}
risk.{status,current_decision,blockers,warnings}
completeness.{status,issues}
```

Adding fields is backward-compatible. Removing, changing the meaning of, or silently defaulting any field above requires a schema-version decision and a Dashboard migration in the same implementation Issue.

## Fixture and browser matrix for implementation follow-ups

| Fixture | Expected API state | Required browser evidence |
|---|---|---|
| Clean stopped current cycle | stopped; start capability depends on independent gates | Controls explain any remaining gate |
| Start response lost, later accepted | refreshed runtime/snapshot confirms running | One success state; no duplicate start |
| Running + matching plan + ready tick | running | Green running status and truthful accepted count |
| Running + heartbeat stale/missing | degraded + reason/age | `运行降级`, no claim of healthy execution |
| Runtime/plan mismatch | degraded + `runtime_strategy_plan_mismatch` | No start/replan exposure control |
| Unknown snapshot row/count | degraded + unknown count | Explicit unknown, never zero |
| Previous cycle with accepted orders | previous unresolved | Cycle ID and count visible; Grid/DCA start blocked |
| Current reconciliation drift | degraded/start blocker | Cause and next action visible; no new exposure |
| DCA aggregate target | DCA lifecycle target state separate from entries | TP labelled as event-driven aggregate protection |
| Source unavailable | unavailable/incomplete | Refresh/recovery action; no false live price or zero counts |

## Planned implementation split

This design PR is intentionally non-behavioural. After review, create small Issues in this order:

1. Formalize read-model runtime schema/fixture conformance and precedence.
2. Add any missing state projection and Dashboard rendering solely required by the matrix.
3. Extend control-response reconciliation tests so the browser confirms only the authoritative state.
4. Add a compatibility/release gate for the documented fields.

Each follow-up must preserve Paper default safety and use focused tests. Any change to order submission, stop/flatten, leverage, or risk policy is out of scope and needs its own contract.

