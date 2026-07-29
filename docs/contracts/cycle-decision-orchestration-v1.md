# Cycle Decision Orchestration v1

## Outcome

Every Paper cycle owns exactly one durable decision record. A decision may be
executed, deliberately left unexecuted with a stable reason, or blocked by an
existing-position direction conflict. A missing record is unhealthy; an
explicit `not_executed` record is not missing.

## Decision identity and storage

- The identity is the cycle ID (`YYYY-MM-DD_DAY` or `YYYY-MM-DD_NIGHT`).
- Records are stored under
  `outputs/dualtrack/strategy_control/cycle_decisions/<cycle_id>.json`.
- The first valid record is immutable. Repeating the same decision is
  idempotent; attempting to replace it with a different decision fails closed.
- `source` is `manual` or `auto_ai`.
- `outcome` is `executed`, `not_executed`, or `position_conflict`.
- Every non-executed record includes a machine-readable `reason_code`, a
  human-readable reason, and a concrete `next_action`.

## Automatic decision path

When a cycle has no manual production decision, the live-tick runner:

1. completes the normal lifecycle, protection, synchronization, intraday and
   ledger phases;
2. writes the successful execution heartbeat;
3. requests one fresh AI evaluation using trusted D1/4H/1H/15m inputs plus the
   authoritative open positions and accepted orders;
4. builds the deterministic preview;
5. selects the AI proposal;
6. calls the existing `prepare_start` and `start` controls.

No safety or risk check is bypassed. If the preview requires human risk
acknowledgement, automatic execution stops and records
`risk_confirmation_required`. Stale market, stale tick, unresolved prior
cycle, provider failure, reconciliation failure, or incomplete order
acceptance likewise becomes one explicit `not_executed` decision.

## Existing-position conflict

- Existing positions remain governed by their existing TP, SL and range
  invalidation.
- A recommendation opposite to an open position cancels only still-accepted
  entry orders, then does not start, flatten, reverse, hedge, or create either
  old-direction or new-direction entries. Protective orders are identity
  checked before and after the cancellation.
- The cycle records `position_conflict`, including position IDs and both
  directions.
- A future reversal requires an explicit operator action after the old
  position exits naturally. v1 never performs that reversal automatically.

## Manual decisions

An already running manually sourced plan is recorded as an executed manual
decision. A manually sourced plan that has not completed its operator start is
recorded as `manual_start_pending`; automation does not impersonate the human
confirmation.

## Health

Cloud Paper health exposes `cycle_decision`:

- `ready` when the current runtime cycle has one valid record;
- `blocked / cycle_decision_missing` when the record is absent;
- `blocked / cycle_decision_invalid` when more than one or an invalid record is
  present.

## Safety boundaries

- Paper only; no live route or exchange credential access.
- No automatic acknowledgement of human-only risk.
- No automatic reversal, flatten, hedge, or protective-order deletion.
- Recommendation or control failures are recorded once and are not retried
  within the same cycle.
