# Grid multi-level traversal contract v1

## Purpose

This contract defines what a Paper Grid may say when one trusted market event
spans more than one grid level.  It prevents a 1m candle from being presented
as an observed tick-by-tick execution path.

## Input and ordering

- The execution boundary receives one immutable
  `dualtrack-market-event-v1` event at a time.  The event must have a stable
  `event_id`, UTC `ts_event`, trusted non-synthetic provenance, and a finite
  execution price.  OHLC is optional but, when present, is a complete
  open/high/low tuple for that same event.
- The live runner sends only completed bars, in chronological `ts_event`
  order.  It never expands one bar into synthetic ticks or creates a second
  event for a level that the bar happened to span.
- A replay engine may deterministically model matching against that one bar
  according to its documented Paper bar-ordering configuration.  Such a match
  is a **Paper model fill**, not evidence of exchange queue priority, intrabar
  tick order, or a live venue fill.
- For accepted entry orders that existed before the event started, the
  compatibility Paper adapter evaluates touched levels once in accepted-order
  identity order after existing-position protection.  Replaying the same
  event cannot create a second receipt for an already-filled order.

## What the system may claim

- Every accepted entry and close needs its own execution fill receipt and its
  command/trade identity.  A completed grid loop additionally needs the
  lifecycle transitions, an original-price rearm command, and a passing
  reconciliation.  A candle high/low alone is never a completed loop.
- Receipts with the same `ts_event` are ordered only by their immutable engine
  receipt identity.  That makes replay deterministic; it does **not** recover
  the price path inside the event.
- If source granularity cannot resolve whether level A occurred before level
  B, the audit language is `modelled_at_bar_granularity`.  The dashboard and
  lifecycle evidence must not call it a natural tick sequence or use it as
  proof of a live multi-level traversal.

## Example: a 1m bar spans three levels

Suppose buy entries exist at 4,004, 4,000, and 3,996.  A completed bar is
`open=4,008`, `high=4,009`, `low=3,994`, `close=4,003`.

1. The runner submits **one** event with that OHLC and its actual source,
   timestamp, and event ID; it does not manufacture `4,004 → 4,000 → 3,996`
   ticks.
2. The Paper engine may emit one or more model fill receipts using its pinned
   bar-ordering rule.  The receipt IDs, command IDs, and plan identity are the
   only auditable evidence of those fills.
3. Without those receipts, the visible result is `unsupported_precision`, not
   three assumed fills.  Without the later target fill and rearm receipt, none
   of the entries is counted as `completed_rearmed`.

## Non-goals

- This contract does not infer exchange fills, order-book queue position,
  slippage, or actual intrabar tick order from OHLC data.
- It does not authorize live trading or relax market freshness, reconciliation,
  or existing Paper safety gates.
