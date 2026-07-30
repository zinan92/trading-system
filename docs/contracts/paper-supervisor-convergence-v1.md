# Paper Supervisor convergence v1

Status: proposed for Park review.  This document is a design contract only;
it authorizes no control action and changes no existing gate.

## Purpose and non-goals

The Paper Supervisor is a state-driven convergence loop.  On every completed
natural live tick it reads the current authoritative state and makes at most
one safe convergence decision.  It is not a cycle-rollover handler and does
not require a rollover event to have occurred.

The declared target for the current cycle is:

> A matching active strategy is `running`, with the exact accepted order
> cardinality required by its plan, unless a durable structural blocker exists.

The Supervisor is Paper-only.  It never accesses live credentials or calls an
adapter/private execution method.  Every preview, prepare, and start uses the
existing public Dashboard composition and `StrategyControlPlane.control()`;
all existing market, tick, old-cycle closure, reconciliation, immutable-fill,
risk, envelope, manual-confirmation, prepared-start, and identity gates remain
the deciding authority.

The approved `cycle-risk-envelope-v1` semantics and
`paper-supervisor-blocker-v1` whitelist are imported unchanged.  This contract
does not add a permissive classifier path: an unlisted or malformed condition
is `unknown_blocker` / structural.

## Invocation and ownership

`gridmind-live-tick.timer` naturally invokes `dualtrack_cycle_runner --event
live-tick` every minute.  After the tick has completed lifecycle work,
protective sweeps, plan sync, intraday work, ledger rebuild, and has written a
fresh heartbeat, it must unconditionally invoke:

```text
PaperSupervisor.converge_once(current_cycle_id, completed_tick_at)
```

`paper_supervisor_enabled` is a mechanically exclusive Paper configuration
switch. When true, the production live-tick takes the Supervisor path and does
not call `CycleDecisionCoordinator.ensure()` at all. When false, the old
coordinator/ledger keeps its current behavior. The two paths may never run
together for a cycle. Historic coordinator decisions and all historic cycle
artifacts remain immutable and readable.

The Supervisor uses the same scheduler-owner and Cloud boot gates that already
protect the live-tick service.  A failed/incomplete tick writes no heartbeat,
therefore never reaches the Supervisor entry point.

## Authority snapshot

Inside its lease, immediately before deciding or calling control, the
Supervisor rereads all of the following.  Dashboard/read-model projections are
not authority for a mutation.

| Fact | Authoritative source | Safe use |
| --- | --- | --- |
| current cycle | `cycle_window(completed_tick_at)` | Reject a superseded loop. |
| active plan and identity | `StrategyControlPlane.active_plan()` | Determine whether planning is required and bind attempts. |
| runtime and tick health | `StrategyControlPlane.runtime_state()` | Determine running/stopped and existing tick gate evidence. |
| accepted orders and open positions | configured Nautilus execution snapshot | Compare exact identity/cardinality; never repair mismatches. |
| reconciliation | existing authoritative reconciliation projection | A non-`ok` result is structural. |
| attempt/episode history | Supervisor immutable event store | Decide wait/probe/cap; corrupt data is structural. |
| control outcome reconciliation | append-only control audit plus authority snapshot | Resolve an unfinished `start_intent`; never guess. |

For Grid, required cardinality is the active plan's declared Grid count and
the accepted order count returned by the successful start receipt.  For any
other supported strategy, the exact expected initial cardinality must be
explicit in the existing control receipt.  Missing, non-positive, or
inconsistent cardinality is `order_identity_conflict`; the Supervisor never
adds, cancels, or repairs orders itself.

The Phase 1 classifier is the only normalized-code authority. Phase 2 may add
only missing typed routes for codes already approved in #447, never a new
whitelist row or a text/regex route:

| Exact typed Supervisor condition | Approved normalized machine code |
| --- | --- |
| durable store fails schema/integrity validation | `attempt_store_corrupt` |
| exact plan/order fingerprint or cardinality conflict | `order_identity_conflict` |
| a thirteenth start intent would exceed the durable per-cycle cap | `cycle_start_attempt_cap_reached` |

Each route requires an adversarial classifier test. A generic local timeout,
exception, or deadline is not transient; it remains `unknown_blocker` /
structural. Only an independently typed approved source failure (for example,
`source_failure=upstream_timeout`) may classify transient.

## Persistent concurrency and crash contract

The existing `production_mutation_lock()` serializes public control actions but
does not persist Supervisor intent.  The Supervisor adds a dedicated store:

```text
outputs/dualtrack/supervisor/convergence/
  .lease.lock
  lease.json
  states/<cycle_id>.json
  events/<cycle_id>.jsonl
  observations/<UTC-date>.jsonl
```

`events` is append-only, opened with `O_APPEND`, flushed and fsynced.  A
derived `state` is atomically replaced and its directory fsynced. The lease
record is audit evidence, not the lock itself: `fcntl.flock(LOCK_EX|LOCK_NB)`
is held on `.lease.lock`, an inode that is never replaced; `lease.json` is a
separately replaceable observation receipt. The lock remains held across one `converge_once`; process
death releases it.  The next holder writes a `previous_lease_unfinished`
observation before proceeding.  It obtains the existing production mutation
lock as well, so its control calls are re-entrant with the control plane.

If another loop owns the lease, the contender performs no control action and
returns transient `lease_held` without writing an event or state. Only the
lease holder writes events/state. If durable state cannot be read or validated,
the result is `attempt_store_corrupt` / structural; it must not reconstruct
history heuristically.

Before a `start` call the following is a hard, durable ordering:

```text
fresh authority reread
→ append+fsync start_intent(attempt_id, preview_id, prepared_start_id)
→ public control(start)
→ append+fsync start_result
```

`start_call_count` increments with `start_intent`, including an indeterminate
call.  A prepared-start id that appears in `start_intent` is permanently spent
by the Supervisor and may never be passed to `start` again.

On restart an intent without result is reconciled before any new control call.
`start_intent` also freezes plan id/version, exact expected order cardinality,
the canonical expected order fingerprints (side, price, quantity, event, and
plan identity; never a volatile engine id), and envelope verification id. If
runtime, every accepted fingerprint, cardinality, reconciliation and control
audit prove a complete accepted start, the attempt becomes
`executed`/`adopted_existing`.
If they prove an exact rejected outcome, it is classified under the approved
whitelist; only a later eligible tick can begin a fresh sequence.  Any other
result is `control_outcome_unknown` / structural with zero automatic retry.

## Attempt and episode facts

Each immutable event includes schema version, sequence, UUID attempt and
episode ids, cycle id, UTC timestamp, mode (`normal`, `backoff`, `probe`),
plan identity, preview id, prepared-start id, classifier version, raw evidence
hash, normalized machine code, human reason, next action, and control result.
It contains no credentials.  Useful event kinds are:

```text
observation, lease_acquired, lease_released, attempt_opened,
plan_created, envelope_bound, prepare_succeeded, start_intent, start_result,
backoff_scheduled, episode_short_budget_exhausted, probe_scheduled,
blocked_structural, structural_rechecked, structural_cleared,
adopted_existing, executed, alert_requested
```

Each cycle has a separate state/event path; an episode, exhausted budget,
probe time, and start cap never cross a cycle boundary.  Old records are
preserved as read-only history.

An AI cycle may bind a fresh envelope only through an existing immutable
Park-authorized outer policy identified by exact `policy_id`, version and
digest. The Supervisor never chooses or creates an outer policy. The exact
reference comes from a separately immutable, Park-written Supervisor policy
binding registry: each binding names one strategy type/direction and the exact
policy id/version/digest, Park actor, and authorization timestamp. It is not
AI-writable, is matched by exact fields only, and has no fallback selection.
Missing, expired, malformed, or unmatched binding is
`outer_strategy_policy_missing`/`outer_strategy_policy_invalid`.

After `refresh_recommendation`, the no-active-plan path is exactly:

```text
returned proposal/evaluation identity
→ public StrategyControlPlane.lock_production_plan(selected_proposal_id)
→ reread the exact active plan identity
→ read the exact Park policy binding
→ authorize the inner envelope with that binding
→ fresh preview / prepare / start
```

The Supervisor uses the policy's exact approved limits, persists the
field-by-field outer-policy comparison, and then verifies each fresh preview
through the already-approved envelope API. Any identity failure in that chain
is structural; it leaves the newly created plan and all historical artifacts
unchanged, and does not select another proposal or policy.

## Complete state dispatch

The current state is one of `healthy`, `converging`, `backing_off`, `probing`,
`blocked_structural`, or `lease_held`.  Every observation dispatches exactly
one row below after handling an unfinished intent.

| Observed condition | Supervisor action | Persisted result |
| --- | --- | --- |
| no active plan | Execute the exact recommendation -> returned proposal -> public plan lock -> reread plan -> Park binding/envelope chain above, then one fresh prepare/start sequence. | `converging`, or classified failure. |
| active plan + stopped + no attempt | Enter a fresh sequence immediately. | `converging`; this is the 2026-07-30 DAY recovery. |
| active plan + stopped + transient attempt, before `next_attempt_at` | No control call. | `backing_off` with visible next time. |
| active plan + stopped + eligible backoff/probe | Enter one fully fresh sequence. | `converging`, then outcome. |
| running + exact expected accepted orders | No control call. | `healthy` / `adopted_existing`. |
| running + unknown/wrong order count | No repair control. | `blocked_structural(order_identity_conflict)`. |
| structural blocker | Reread only the exact authoritative condition on every tick. | Keep blocked and alert, or append `structural_cleared`. |
| stale/superseded loop cycle | No control call. | `superseded_cycle`; the next tick reads its own current cycle. |
| corrupt/unclassifiable input | No control call. | `blocked_structural` via approved fail-closed code. |

When a structural condition is unequivocally cleared, the Supervisor appends
`structural_cleared`; it does not replay a prepared id or historical command.
A subsequent fresh observation may begin a new attempt.  A cycle start cap is
not clearable during its cycle: it remains a visible, alerted structural block
until the next cycle.

## Fresh control sequence and retries

One eligible attempt is:

```text
fresh trusted server facts
→ fresh preview via public control path
→ exact envelope + outer-policy verification
→ fresh prepare_start
→ durable prepare_succeeded
→ durable start_intent
→ at most one public start(prepared_start_id)
→ reconcile N/N receipt and runtime
```

The Supervisor neither invents a risk acknowledgement nor reuses a preview
facts digest, preview id, or prepared-start id.  A prepare success closes the
current transient episode and resets its short budget before the corresponding
start result is classified.

Only an approved transient result schedules retries: 60, 120, 300, 600, then
1200 seconds.  The fifth consecutive transient short failure records
`episode_short_budget_exhausted`, requests an alert, and schedules a fresh
probe every 30 minutes.  It never becomes a permanent stopped state.  Each
probe obeys the same fresh sequence and exact gates.

The tick-heartbeat transient rule is bounded: on the first observation more
than 10 continuous minutes into that heartbeat episode, classification is
`execution_tick_scheduler_down` / structural, without probe mode.  Twelve
durably intended starts is the per-cycle ceiling.  A would-be thirteenth start
records and alerts `cycle_start_attempt_cap_reached` / structural; harmless
observations continue, but no preview/prepare/start sequence may continue in
that cycle. `episode_short_budget_exhausted` is an event label, not a new
blocker classifier code. Store/order/cardinality observations are normalized
only through the exact already-approved typed routes above; no free-text or
unapproved machine-code expansion is allowed.

The existing live-tick service has a 55-second timeout. One Supervisor attempt
has an explicit 45-second total budget, including a maximum 25-second AI
recommendation sub-budget; fresh authority/control/reconciliation have the
remaining bounded budget. A deadline before `start_intent` is
`unknown_blocker` / structural unless independent typed evidence matches an
approved transient source failure. A deadline after `start_intent` is never
cancelled and guessed: it uses the unknown-control-outcome recovery path on
the next tick.

## Read-model, health, and external alert contract

The canonical trading-system read-model adds `runtime.supervisor`:

```text
status, cycle_id, last_observed_at, attempts[], last_attempt_id,
episode{episode_id, consecutive_transient_failures, exhausted_at,
next_attempt_at}, start_call_count, structural_blocker{machine_code,
human_reason,next_action}, lease, alert, and utilization{24h,7d}.
```

`attempts[]` exposes timestamp, result, machine code, human reason, next
action, preview id, prepared-start id, envelope/outer-policy verification
references, and classifier version.  It is a projection of immutable events,
not a replacement for the events.

Utilization uses minute Supervisor observations, not optimistic control-audit
intervals.  An interval counts only when adjacent observations are at most 120
seconds apart and both prove matching plan identity, runtime `running`, exact
orders, fresh tick, and reconciliation `ok`.  Missing/unknown intervals count
zero.  Before a complete window exists it is `insufficient`, never 100%.

When `paper_supervisor_enabled=true`, `CloudPaperHealth` replaces (rather than
adds to) its legacy `CycleDecisionLedger` cycle-decision check with the
Supervisor convergence check. The Dashboard/read-model likewise projects
`runtime.supervisor` as the current-cycle decision surface and keeps legacy
cycle decisions as historical read-only evidence. This migration is selected
by the same exclusive switch, so a healthy Supervisor cycle cannot be marked
`cycle_decision_missing` and the old coordinator has zero calls in Supervisor
mode.

The Supervisor health check is non-healthy for:

- a structural blocker;
- an exhausted episode/probe requiring alert;
- active plan + stopped + no Supervisor attempt for more than 300 seconds;
- stale/missing Supervisor observation for more than 300 seconds; or
- an available 24-hour/7-day utilization window below the configured 85%.

It carries the machine code, human reason and next action.  The Supervisor
does not send HTTP alerts itself.  The existing dead-man service remains the
sole external sender; the non-healthy Cloud health check makes its natural
five-minute ping use the fail endpoint and persist the endpoint receipt.

## Required verification before release

Focused tests must inject and prove:

1. market-moved rejection then a fresh preview/prepared id and successful
   retry;
2. five transient failures, alert, 30-minute probe, and later auto-recovery;
3. reconciliation drift: zero start intent/order, alert, recheck after repair;
4. active plan/stopped/no attempts and a wholly absent rollover event, each
   recovered by a later natural tick;
5. two independent processes plus crash points before/after `start_intent`;
6. >10-minute tick episode, unknown control outcome, envelope/policy failure,
   malformed state, and 12-start cap all fail closed;
7. read-model fields, conservative utilization gaps, Cloud health to fake
   dead-man fail delivery, and exact Grid N/N cardinality.

An unhandled Supervisor exception must append a durable fail-closed structural
receipt if its store remains writable. If it cannot, no fresh Supervisor
observation is written; after 300 seconds health reports
`supervisor_tick_missing`. A fresh pre-Supervisor live-tick heartbeat is never
evidence that Supervisor convergence succeeded.

Release still follows the existing SHA-bound Paper runbook.  Passing tests is
not completion: the final acceptance evidence is 48 continuous hours with at
least 85% proven utilization, three real cycle boundaries including a 21:00
night boundary, and one complete transient auto-recovery audit chain.
