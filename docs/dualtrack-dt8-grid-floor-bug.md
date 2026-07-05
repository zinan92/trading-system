# DT8 follow-up: grid rungs placed below the plan stop (HIGH)

Found during independent verification of DT8, 2026-07-05. The orchestrator runs
end-to-end correctly (unattended cycle → machine fills → direction grading →
ledger; fail-closed on no plan; human track + scoreboard all correct). This is a
single, isolated bug in the grid core that makes the **machine-track PnL
untrustworthy** whenever a plan invalidation floor is supplied. Fix before the
machine PnL is believed or anything goes near real money. The human track and
direction scoreboard are unaffected.

## Symptom

Fast-forwarding `2026-06-15_DAY` with a seeded long plan (floor 4273.14) produced
machine PnL **+$1,173.78** on a $10k/10× track. Inspecting the 82 fills: the
largest are `event: stop`, `side: sell`, price 4274.1, with **positive** PnL
(+$146, +$123, +$100). A stop-out on a long selling *above* its entry is
impossible for a correctly-built grid — the stop must be the floor, below every
buy rung.

## Root cause

`services/dualtrack_grid_core.py`:
- line 53–54: `half_width = range_k * prev_range`; `n_rungs` derived from it.
- line 60: `levels` placed from anchor down by `spacing`, `n_rungs` deep →
  bottom rung ≈ `anchor - half_width` (≈ 4202 in the repro).
- line 61: `active_stop = stop or GridStop(... anchor - half_width)`. When the
  orchestrator passes the **plan invalidation floor** (4273.14, tighter than
  `half_width`), the stop moves up to 4273 but the **rungs stay placed down to
  4202**. Rungs 3–8 now sit *below* the stop. Price falls through, fills them at
  4218–4270, then the stop "sells" them at 4274 for phantom profit.

Rung placement (`half_width`) and the stop level are decoupled when a custom
stop is provided. This only manifests on the **orchestrator/console path** (which
passes a plan floor). The lab research path passes `stop=None` → stop = range
edge = rung-range bottom (self-consistent), so R5 numbers are unaffected
(verified byte-identical earlier).

## Fix (aligns with the DT8.1 schema model)

Park's model: **floor = invalidation = grid bottom = stop — one level.** Make the
code honor it:

- When a plan `stop` is provided, derive the rung range from it:
  `half_width = abs(anchor - stop.price)`, then `n_rungs = min(max_rungs,
  half_width / spacing)`, levels placed anchor→floor. `range_k × prev_range` is
  ignored when a plan floor is given (correct — the human's floor defines grid
  density, not a mechanical multiple).
- When no plan stop (lab path), keep current behavior (`half_width = range_k ×
  prev_range`, stop = range edge).
- Result: no rung is ever below the stop; a stop-out is always a loss for a long
  (a gain-side exit for a short). Re-arm re-anchors and re-derives the same way.

## Tests

- Add: a long grid with a plan floor tighter than `range_k × prev_range` places
  **no rung below the floor**, and every `event: stop` fill has PnL ≤ 0 for a
  long (≥ 0 sign-flipped for a short).
- Re-run the D8-1 golden parity fixture after the change (fills will shift; the
  fixture is expected to update).
- Re-run the fast-forward acceptance; machine PnL should drop to a sane
  magnitude and stop-outs should be losses.

## Not affected

Human track, direction-only grading (FIX-4), scoreboard, ledger arithmetic,
fail-closed precedence, blind-protocol invariants, lab R5 numbers.
