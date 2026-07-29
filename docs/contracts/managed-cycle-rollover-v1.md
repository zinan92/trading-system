# Managed Paper cycle rollover v1

## Outcome

A 12-hour cycle boundary closes the reporting book without automatically
closing a healthy Paper strategy. Open positions, accepted orders, protective
orders, and Grid lifecycle identities may cross the boundary only through an
explicit, verified handoff to an active StrategyPlan for the new cycle.

This contract is Paper-only. It does not authorize or alter any live/real-money
path.

## Healthy handoff

All conditions are required:

1. The previous runtime is `running` and belongs to the previous cycle.
2. The previous-cycle execution tick heartbeat is fresh at the boundary.
3. The market snapshot used at the boundary is trusted and fresh.
4. The current cycle has an active StrategyPlan whose
   `takeover_from_strategy_plan_id` equals the running previous plan ID.
5. The authoritative execution adapter can create and reconcile a new-cycle
   execution checkpoint without changing any accepted order ID, open position
   ID, or Grid lifecycle line ID.

The checkpoint retains prior commands and market events only as replay seed
evidence. Prior fills and realized P&L remain attributed to the previous cycle;
the new cycle begins with the previous ending cash, zero cycle realized P&L,
and the carried open positions' current unrealized P&L.

After checkpoint verification:

- runtime ownership moves to the current cycle and its active plan;
- the previous cycle package closes with `terminal_mode=handed_off`;
- no cancel, flatten, resubmit, or synthetic fill occurs;
- the handoff receipt links both plans and both cycle IDs.

## Fail-closed fallback

If any required condition is missing or checkpoint verification fails, the
existing safe rollover path remains authoritative: cancel accepted orders,
flatten open positions, reconcile, package the previous cycle, and wait for an
operator start. The rollover receipt records the exact failed condition.

## Crash and retry

The handoff receipt is append-only and identity-verifiable. A retry may repair
runtime/package publication from a verified receipt, but must not create a
second checkpoint or repeat order mutation. Partially prepared target files
without a verified receipt are never treated as a successful handoff.

## Explicit exclusions

- Generating the next StrategyPlan is #415, not this contract.
- Direction-conflict policy is #415.
- Exit-reason taxonomy is #414.
- Historical fills, trades, and terminal cycle packages are never rewritten.
