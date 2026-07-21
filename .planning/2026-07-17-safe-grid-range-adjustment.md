# Safe grid range adjustment — milestones

## Objective

Park can diagnose market-data failures without false order churn, then adjust a
running grid through either of two explicit workflows without hidden position or
risk changes.

## Milestone 1 — Stable runtime and trustworthy market state

### User outcome

Stopping or starting a grid has one durable result: orders do not briefly appear,
disappear, and reappear, and market/history errors appear in the correct place.

### Success criteria

1. A completed start accepts exactly the new plan's entry orders, not historical
   cancel-journal rows.
2. A manually stopped current cycle is not restarted by stale rollover intent.
3. Failed-start cleanup cannot flatten or cancel another plan.
4. One current-dashboard request performs one canonical live market fetch.
5. Historical pagination reaches a trusted end without a false load failure.
6. Empty upstream transport messages retain the concrete exception type.
7. Strict execution-venue quality remains fail-closed.

### Scope

- In: execution receipts, lifecycle authority, history loading, datafeed liveness,
  strict same-source retry, error presentation.
- Out: changing the proxy node, adding a fallback venue, starting a production grid.

## Milestone 2 — Two explicit adjustment models

### User outcome

The right-side control and chart drag do exactly what their labels promise and
never silently resize risk.

### Success criteria

1. Right-side range expansion preserves spacing/ratio and per-grid notional.
2. Right-side expansion changes grid count only by whole edge steps.
3. Right-side expansion preserves positions, protective orders, and internal
   entry orders.
4. Chart middle drag preserves width, count, spacing, and per-grid notional.
5. Chart edge drag preserves count and per-grid notional while recomputing spacing.
6. Arithmetic and geometric grids use deterministic calculations.
7. Risk-limit failures disable application and offer an explicit notional recalc.

### Scope

- In: pure preview calculations, boundary snapping, risk receipts, order deltas.
- Out: changing strategy direction/style/leverage as an implicit side effect.

## Milestone 3 — Deliberate chart editing

### User outcome

Park can make several small drag adjustments before deciding whether to review or
discard the draft.

### Success criteria

1. Chart dragging is available only after enabling `调整网格` mode.
2. Middle and edge handles expose the specified grab and vertical-resize cursors.
3. Dragging renders only a translucent dashed preview and never changes orders.
4. Multiple pointer releases accumulate one draft without opening a modal.
5. Small `确认` and `取消` controls appear only after the draft differs.
6. Cancel restores the production overlay exactly.
7. Confirm opens an old-to-new specification and risk card.

### Scope

- In: chart hit targets, draft state, preview overlay, review dialog.
- Out: automatic application on pointer release.

## Milestone 4 — Safe production application and proof

### User outcome

The final action is understandable, revalidated against live state, idempotent,
and cannot damage the old plan before validation succeeds.

### Success criteria

1. The card shows bounds, width, mode, count, spacing/ratio, per-grid and total
   notional, margin, leverage, max risk, order deltas, position handling, and TP/SL.
2. The final button is exactly `停止+平仓+撤单+交易新网格`.
3. Plan identity, preview identity, current price, market trust, and risk are
   revalidated immediately before stopping the old grid.
4. A failed precondition leaves the running plan untouched.
5. A repeated network submission cannot execute the replacement twice.
6. The existing right-side adjustment remains non-flattening and separately named.
7. Focused tests, adversarial review, and browser evidence pass without starting a
   real production grid during acceptance.

### Scope

- In: stop/flatten/cancel/new-plan transaction boundary, idempotency, visual proof.
- Out: automatic rollback after an exchange accepts only part of a new live plan;
  that condition stays fail-closed and requires operator recovery.
