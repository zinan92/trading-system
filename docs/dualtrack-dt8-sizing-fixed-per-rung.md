# DT8 follow-up: machine grid → fixed per-rung sizing (owner chose "B")

Decision by owner 2026-07-05. After the grid-floor bug fix, verification showed
`budget_sizing` turns the plan floor into an **inverted risk knob**: a tighter
floor → fewer rungs → the full capital×leverage ($100k at 10×) concentrated into
those few rungs (e.g. 2 rungs of $50k). A stop should make risk *smaller* when
tightened, not larger. Switch the machine grid to **fixed per-rung notional**.

## Change

`services/dualtrack_machine.py`:
- Grid layer (~line 68): `budget_sizing=True` → **`budget_sizing=False`**.
- Trend layer (~line 88): `budget_sizing=True` → **`budget_sizing=False`**.

Leave `rung_notional` as is — it is already the correct fixed value:
- grid: `base_rung_notional(self.config)` = `capital×leverage / max_rungs`
  = `$10,000` per rung at $10k/10×/10 rungs.
- trend: `base_rung_notional × trend_budget_pct` = `$2,000` per trend rung.

With `budget_sizing=False`, `simulate_conditional_grid` line 59 uses
`effective_notional = rung_notional` (fixed), independent of `n_rungs`. So a
tight floor deploys *less* total ( fewer rungs × $10k ), a wide floor deploys
more — capped at `max_rungs × $10k = $100k` (= the 10× budget). Floor now behaves
like a real stop.

Do NOT change the lab path: `services/lab_r5_grid.py` intentionally keeps
`budget_sizing=True` (a research device for comparing geometries at equal
capital). This change is machine-console only.

## Tests

- Tight floor (e.g. 0.5% below anchor → ~2 rungs) deploys strictly less total
  notional than a wide floor, and **each rung notional == base_rung_notional**
  (fixed, independent of rung count). Add to
  `tests/test_dualtrack_dt2_machine_runner.py`.
- Update `tests/fixtures/dualtrack_machine_golden.json` (per-rung notional
  changes from budget-adjusted to fixed).
- Fast-forward acceptance: a tight-floor cycle now produces a *smaller*, saner
  machine PnL than under budget_sizing (assert the magnitude drop).

## Verify after

Owner's brain (Claude) will re-run the `2026-06-15_DAY` fast-forward repro
(seeded long, floor 0.5% below open, 10×) and confirm: fixed $10k/rung, tight
floor → smaller total deployment → saner PnL, stop-outs still losses, no rung
below floor. Then the machine track's PnL is trustworthy and DT6 can proceed.
