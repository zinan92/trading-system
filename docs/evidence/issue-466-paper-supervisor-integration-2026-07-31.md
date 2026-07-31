# Issue #466 Paper Supervisor integration evidence

Date: 2026-07-31
Scope: code and deterministic integration evidence only; not deployed

## Result

The repository now has a state-driven, Paper-only convergence composition that
runs in the existing natural live-tick process after the complete heartbeat.
The checked-in production configuration remains on
`legacy_cycle_decision`; this evidence does not claim Cloud activation or the
required 48-hour acceptance.

## Mechanical invariants

| Condition | Verified behavior |
| --- | --- |
| Supervisor mode | Legacy `CycleDecisionCoordinator` receives zero calls. |
| Incomplete, wrong-cycle, stale or future heartbeat | Zero plan/control actions. |
| Missing rollover event | Current authoritative state still converges. |
| Active plan + stopped | Fresh prepare, durable intent, public start. |
| `prepared_start_market_moved` | Exact zero-order recovery, backoff, then new preview and prepared identity. |
| Repeated observation after success | Exact running plan is adopted; no second start. |
| Lease contention | Contender returns `lease_held`; zero control actions. |
| Reconciliation drift / unresolved prior cycle | Structural blocker; zero orders. |
| Pre-intent 45-second deadline | Typed transient; no intent or start. |
| Post-intent exception or missing authority | No replay; fail-closed recovery. |
| Public prepared receipt | Binds pre-start plan, post-start plan and exact order fingerprints. |
| Pre-intent crash | Durable reservation is abandoned once; one transient failure is replayed. |
| Heartbeat projection crash | Typed fresh/missing WAL event is replayed once by sequence cursor. |
| Observation tail deletion | Checkpoint anchor mismatch fails closed. |
| Accepted receipt identity | Nonempty unique order IDs map to authoritative entry commands; non-entry TP/SL/flatten commands cannot pollute start authority and re-arms also require verified lifecycle ancestry. |
| Open-position identity | Position/trade/fill IDs, plan/version, side, command/order/fill quantities and exact fill-weighted entry price bind to the authoritative entry command; missing, reversed, overfilled or repriced lineage fails closed. |
| Prepared capability across cycles | Both Supervisor and public control use global one-shot registries. |
| Stalled cleanup/fsync | 45-second soft cleanup boundary plus independent 52-second hard process watchdog. |
| Immutable fill regression | Public typed `immutable_fill_guard_triggered`; existing guard unchanged. |

## Identity evidence

The market-moved integration fixture records:

1. `preview-1` / `prepared-1` intent before the first public start;
2. a unique rejected control audit and proven zero-order recovery;
3. a later `preview-2` / `prepared-2` intent;
4. one successful N/N public start; and
5. a later `adopted_existing` observation with no additional control.

The two prepared identities are distinct and each appears in at most one
`start_intent`. The append-only attempt projection and public global
consumption registry permanently mark both as spent. A Supervisor-bound
prepared receipt also freezes its pre-intent attempt ID; only the matching
durable `start_intent` can exercise it.

## Verification commands

```text
python3 -m ruff check <changed Python files>
python3 -m compileall -q services pipelines tests/test_paper_supervisor.py
python3 -m pytest -q \
  tests/test_paper_supervisor.py \
  tests/test_paper_supervisor_store.py \
  tests/test_paper_supervisor_episode.py \
  tests/test_strategy_control_plane.py \
  tests/test_strategy_dca_control_plane.py \
  tests/test_cycle_risk_envelope.py \
  tests/test_dualtrack_dt8_cycle_runner.py \
  tests/test_dualtrack_nautilus_execution_adapter.py \
  tests/test_dualtrack_nautilus_shadow_replay.py \
  tests/test_control_audit.py \
  tests/test_live_tick_timing.py
```

Latest focused result after adversarial review: `342 passed`.

Three independent L-level reviews re-ran the critical counterexamples. The
runtime review used real Grid and DCA control paths that raised the Supervisor
deadline after the first accepted order; both cleanup paths left zero accepted
orders and zero open positions. The hard-watchdog regression also distinguishes
the prior signal-sharing design (hang) from the independent watchdog (exit
124).

## Explicit non-claims

- No Cloud service, timer, Dashboard, strategy, order or position changed.
- No live/real-money path or exchange credential was read or modified.
- Issue #463 unattended Cloud AI provider is not complete.
- Supervisor health/read-model/dead-man migration remains #467/#468.
- Deployment and 48-hour real acceptance remain #469/#470.
- Cross-cycle open-position takeover remains forbidden #413 scope. Until that
  separate contract is implemented, a carried open position intentionally
  fails closed as an identity conflict; #466 never adopts it.
