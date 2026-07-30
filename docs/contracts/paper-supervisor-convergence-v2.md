# Paper Supervisor convergence contract v2

Status: approved design contract for Issues #460–#472. This contract authorizes
no control action by itself and changes no existing safety-gate decision.

## Outcome and boundaries

The Paper Supervisor is a state-driven convergence loop. It repeatedly reads
authoritative state and tries to reach this declaration:

> The current Paper cycle has one matching active strategy that is `running`
> with the exact accepted order cardinality required by its plan, unless a
> durable structural blocker exists.

The loop does not depend on a rollover event. A missing rollover event must not
prevent convergence on the next eligible observation.

The Supervisor is Paper-only. It never reads live credentials and never calls a
private execution adapter. Preview, prepare and start use the existing public
control path. Market trust, tick heartbeat, old-cycle closure, reconciliation,
immutable fills, human risk confirmation, release SHA ownership and boot gates
remain the deciding authorities. The Supervisor may respond to their results;
it may not weaken, bypass, skip or reinterpret them.

Out of scope: live/real money, exchange keys, strategy tuning, new strategy
types, UI redesign, backtesting and cross-cycle position handoff (#413).

## State and authority

The current state is exactly one of `healthy`, `converging`, `backing_off`,
`probing`, `blocked_structural` or `lease_held`. Each eligible observation
performs at most one convergence attempt.

Within its exclusive lease, immediately before deciding or calling control, the
Supervisor rereads:

| Fact | Authority |
| --- | --- |
| current cycle | `cycle_window(observed_at)` |
| active plan and identity | `StrategyControlPlane.active_plan()` |
| runtime and tick health | `StrategyControlPlane.runtime_state()` |
| accepted orders and open positions | configured Nautilus Paper execution snapshot |
| reconciliation | existing authoritative reconciliation projection |
| attempt and episode history | Supervisor append-only event store |
| uncertain control outcome | append-only control audit plus all authority above |

Dashboard and read-model projections are never mutation authority. Exact Grid
order cardinality comes from the active plan and successful control receipt.
Missing, non-positive or inconsistent cardinality is structural; the
Supervisor never repairs an identity or count mismatch by adding, cancelling
or replacing orders.

## Exact blocker classifier v2

The machine-readable source is
`docs/contracts/paper-supervisor-blocker-v2.json`. Classification uses exact
typed evidence and exact machine-code equality only. Substring, prefix, regex,
exception prose and free-text matching are forbidden.

An unlisted, malformed or newly observed code is always
`unknown_blocker`/structural. Unknown never means retry.

### Transient whitelist

| Exact code | Meaning and next action |
| --- | --- |
| `prepared_start_market_moved` | Generate a new preview and prepared start after backoff. |
| `prepared_start_expired` | Generate a new preview and prepared start after backoff. |
| `trusted_market_temporarily_unavailable` | Wait for trusted market evidence, then start a wholly fresh sequence. |
| `execution_tick_heartbeat_temporarily_missing` | Wait for a fresh complete tick; promote after ten continuous minutes as specified below. |
| `upstream_data_source_transient_failure` | Probe the external source, then start a wholly fresh sequence. |
| `supervisor_attempt_deadline_before_intent` | Local Supervisor budget ended strictly before durable `start_intent`; schedule a fresh attempt. |

`supervisor_attempt_deadline_before_intent` is local resource pressure.
`upstream_data_source_transient_failure` requires independent typed evidence of
an external provider failure. They are never inferred from the same generic
timeout exception.

### Structural whitelist

| Exact code | Meaning |
| --- | --- |
| `control_outcome_unknown` | A response was lost after `start_intent`; do not retry until authority proves the result. |
| `partial_execution_or_cleanup_required` | Authority proves possible partial execution or cleanup. |
| `ledger_reconciliation_drift` | Accounting or execution reconciliation is not exact. |
| `previous_cycle_paper_state_unresolved` | The old cycle is not safely closed. |
| `order_identity_conflict` | Plan/order/position identity or cardinality conflicts. |
| `attempt_store_corrupt` | Durable Supervisor history fails schema or integrity checks. |
| `execution_tick_scheduler_down` | One heartbeat-missing episode has lasted more than ten continuous minutes. |
| `outer_strategy_policy_missing` | No exact Park-authored outer-policy binding exists. |
| `outer_strategy_policy_invalid` | The exact outer policy or binding cannot be verified. |
| `outer_strategy_policy_expired` | The exact outer policy is outside its authorized validity. |
| `outer_strategy_policy_envelope_out_of_bounds` | AI cycle envelope exceeds at least one exact outer-policy field. |
| `risk_envelope_missing` | The required immutable cycle risk envelope is absent. |
| `risk_envelope_authorization_invalid` | Envelope authorization identity or digest is invalid. |
| `risk_envelope_preview_out_of_bounds` | Fresh preview exceeds at least one exact envelope field. |
| `manual_risk_confirmation_required` | An existing human confirmation gate still requires Park. |
| `prepared_start_identity_changed` | Prepared-start identity differs from the preview-bound identity. |
| `strategy_preview_identity_changed` | Preview identity changed before preparation or start. |
| `active_plan_missing` | No exact current-cycle active plan is available. |
| `runtime_state_conflict` | Runtime or plan-version state conflicts with the attempt. |
| `existing_exposure_conflict` | Accepted orders or open positions conflict with a new start. |
| `plan_identity_conflict` | Plan id/version/type/direction differs from its binding. |
| `execution_receipt_identity_invalid` | Start receipt order identities are missing, duplicate or invalid. |
| `immutable_fill_guard_triggered` | Existing immutable execution history guard rejected the action. |
| `risk_policy_rejected` | Existing risk policy rejected or could not produce a current valid decision. |
| `trusted_market_provenance_invalid` | Market provenance, identity or payload is structurally invalid. |
| `supervisor_configuration_invalid` | Required classifier, schedule or integration configuration is invalid. |
| `dangerous_start_attempt_cap_reached` | A third dangerous start intent would exceed the per-cycle cap of two. |
| `clean_refusal_observation_cap_reached` | A forty-ninth proven-clean refusal would exceed the logic-runaway guard. |
| `unknown_blocker` | Fail-closed result for every unclassified or malformed condition. |

### Event labels, not blockers

`episode_short_budget_exhausted` records five consecutive transient failures,
requests an alert and enters probe mode. It does not terminate the episode or
cycle. `clean_refusal_observation_budget_warning` is emitted before the
clean-refusal guard is exhausted and is not itself blocking.

### Explicit amendment to the #447 v1 whitelist

The v2 amendment is exhaustive:

- add transient `supervisor_attempt_deadline_before_intent`;
- add structural `outer_strategy_policy_expired`;
- replace structural `cycle_start_attempt_cap_reached` with
  `dangerous_start_attempt_cap_reached`, changing the count from all start
  intents to only dangerous/uncertain intents;
- add structural `clean_refusal_observation_cap_reached` as a separate
  high-water logic-runaway guard;
- remove structural `retry_budget_exhausted` and replace it with non-blocking
  event label `episode_short_budget_exhausted`;
- add non-blocking event label
  `clean_refusal_observation_budget_warning`;
- preserve `outer_strategy_policy_missing` and
  `outer_strategy_policy_invalid` while making expiration its own exact code;
- preserve every other code and its v1 classification exactly.

No other addition, removal, rename or semantic change is authorized.

## Immutable Park outer policy

An AI cycle envelope is valid only inside a Park-authored outer strategy
policy. The outer policy has an independent `policy_id`, version, canonical
digest, Park actor, authorization timestamp and validity interval. It is
immutable and not AI-writable.

A separate immutable binding registry selects one exact policy by strategy
type/direction and exact id/version/digest. There is no fallback policy. The AI
proposal and cycle envelope must prove every field is within the outer policy;
the policy comparison rows and envelope comparison rows are persisted. Missing,
expired or unverifiable policy evidence is structural. The AI cannot authorize
its own boundary.

The envelope adds a boundary; it never replaces existing manual confirmation.
Facts digests, acknowledgements and risk confirmations cannot be reused,
synthesized or forged.

## Durable concurrency and start ordering

The store is append-only and crash-safe:

```text
outputs/dualtrack/supervisor/convergence/
  .lease.lock
  lease.json
  states/<cycle_id>.json
  events/<cycle_id>.jsonl
  observations/<UTC-date>.jsonl
```

Events use append plus flush/fsync. Derived state uses atomic replacement plus
directory fsync. A non-blocking `flock` on the stable `.lease.lock` inode makes
one process the writer. The existing production mutation lock also remains in
force. A contender performs no control action.

Before every public `start` call:

```text
fresh authority reread
→ append+fsync start_intent(attempt_id, preview_id, prepared_start_id)
→ public control(start)
→ append+fsync start_result
```

A `prepared_start_id` appearing in a `start_intent` is permanently spent and
can never be sent to `start` again.

On restart, an unfinished intent is reconciled before any new control call. It
may become `executed`/`adopted_existing` only when runtime, exact accepted
orders, cardinality, reconciliation and control audit jointly prove a complete
accepted start. It may become a clean rejection only when the same authorities
prove zero orders were created. Otherwise it is
`control_outcome_unknown`/structural with no automatic retry.

## Episodes, retries and caps

Every attempt creates new preview and prepared-start identities. A successful
`prepare_start` proves the previous transient condition cleared and resets the
episode short budget before classification of the corresponding start result.

Consecutive transient failures use backoff of 60, 120, 300, 600 and 1200
seconds. The fifth failure emits `episode_short_budget_exhausted`, sends an
alert and enters 30-minute probe mode. Probe mode remains active for the cycle;
when the condition clears, a fresh sequence starts normally.

Episode id, short budget and probe state never cross a cycle boundary. A new
cycle starts with a clean episode budget even if the prior cycle was probing.

Heartbeat absence is transient for at most ten continuous minutes in one
episode. At the first observation beyond that duration it becomes
`execution_tick_scheduler_down`/structural and alerts immediately.

Two separate per-cycle guards apply:

1. **Dangerous cap: 2.** Count a start intent when its result is unknown,
   possibly partial, or zero order creation cannot be proven by runtime,
   authoritative order snapshot and control audit. A would-be third records
   `dangerous_start_attempt_cap_reached` and creates no order.
2. **Clean-refusal observation guard: 48.** A typed refusal such as
   market-moved/expired consumes this guard only when all three authorities
   prove zero orders. It does not consume the dangerous cap. Emit a warning
   near the cap. A would-be forty-ninth records
   `clean_refusal_observation_cap_reached` and creates no order.

Neither guard is inherited by a new cycle. Read-only observations continue
after either structural block.

## Attempt deadline boundary and scheduler topology

Timing values are not approved until Issue #461 measures real Cloud tick stage
latency. The implementation must choose one of:

- raise the systemd deadline;
- run the Supervisor in an independent timer while requiring a fresh,
  successfully completed tick heartbeat; or
- reduce the AI sub-budget.

The chosen values must leave demonstrated headroom for market, lifecycle,
protective sweep, plan synchronization, intraday work, ledger rebuild and
heartbeat persistence.

A deadline strictly before durable `start_intent` creates
`supervisor_attempt_deadline_before_intent`/transient. No order could have been
created. A deadline at or after `start_intent` is
`control_outcome_unknown`/structural until authority reconciles it. Cancellation
must not guess the outcome.

## State dispatch

| Observation | Action |
| --- | --- |
| no active plan | Run the exact recommendation → returned proposal → public plan lock → reread plan → exact Park policy/envelope chain → one fresh sequence. |
| active plan + stopped + eligible | Start one fresh sequence. |
| transient before `next_attempt_at` | No control; expose backoff. |
| eligible backoff/probe | Start one fresh sequence. |
| running + exact plan/orders | No control; record healthy/adopted. |
| running + identity/cardinality conflict | Structural; no repair. |
| structural blocker | Reread the exact authoritative condition, alert or record clear; never replay an old command. |
| stale loop cycle | No control; next observation resolves its own current cycle. |
| malformed/unclassified input | `unknown_blocker`/structural. |

## Read-model, utilization and alert severity

`runtime.supervisor` exposes current status/cycle, observation time, every
attempt's timestamp/reason/result/next action/preview id/prepared id, episode
budget and next attempt, dangerous and clean-refusal counters, exact structural
blocker, policy/envelope verification references, lease, alert and conservative
24h/7d utilization.

Utilization counts only intervals between sufficiently close Supervisor
observations where both ends prove matching plan, runtime `running`, exact
orders, fresh complete tick and reconciliation `ok`. Missing or unknown time is
zero. Before the first complete 24-hour window, status is `insufficient`.
Control-audit intervals are never used to fill evidence gaps optimistically.

Critical dead-man failure is limited to:

- structural blocker;
- no Supervisor attempt for more than 300 seconds while an active plan is
  stopped and unblocked;
- no Supervisor observation for more than 300 seconds; or
- exhausted short episode needing human attention while probe mode continues.

Utilization below 85% is warning/informational in read-model and Dashboard. It
does not drive the dead-man fail endpoint. `insufficient` never participates in
health status.

When Supervisor mode is enabled, the same exclusive switch replaces the old
cycle-decision health check with Supervisor convergence health. The old
coordinator receives zero calls and its history remains read-only.

## Verification and completion

Focused tests must prove:

1. market-moved rejection then new preview/prepared identities and successful
   retry, with no prepared id reused;
2. five transient failures, alert, 30-minute probing and later recovery;
3. reconciliation drift causes no retry and zero orders;
4. active-plan/stopped and completely absent rollover events both converge;
5. crashes before and after `start_intent`, concurrent leases and unfinished
   intent reconciliation;
6. pre-intent deadline is transient while post-intent uncertainty is
   structural;
7. dangerous cap counts only unproven/uncertain outcomes; clean refusals require
   three-source zero-order proof and use their separate guard;
8. ten-minute heartbeat promotion, exact outer-policy failures, corrupt store,
   malformed/new codes and all unclassified input fail closed;
9. conservative utilization gaps, severity ramp and dead-man delivery;
10. exact Grid N/N order cardinality and read-model audit history.

Passing tests and merging code do not complete the project. Completion requires
48 continuous Cloud hours with at least 85% conservative strategy utilization,
three real cycle boundaries including a 21:00 Beijing boundary, and one complete
real transient auto-recovery audit chain.
