# DT6/DT8 follow-up: two bugs found while rendering the dual replay (2026-07-05)

Both surfaced by exercising the live system with a real open-ended plan +
manual human orders + a repeated cycle run (not caught by the green suites).
DT6 itself renders correctly and D6-1 holds (verified separately). These are in
the DT8 backend the replay sits on.

## BUG 1 — HIGH — `_out_of_plan` crashes on open-ended plans (`high: null`)

`services/dualtrack_human.py:93-95`:
```python
low = float((plan.get("range") or {}).get("low"))
high = float((plan.get("range") or {}).get("high"))   # TypeError when high is None
if price < low or price > high:
```
DT8.1 taught `validate_plan` to accept `range.high = null` for directional
plans (owner's model: open-ended upside). But `_out_of_plan` still force-casts
`high` to float, so **every manual human order under an open-ended plan raises
`TypeError: float() argument must be ... not 'NoneType'`** — i.e. the human
track is unusable under exactly the plan shape the owner uses by default
(long, floor only, open upside). This blocks tonight's intended human-track
usage.

Repro: seed a locked long plan with `range.high = null`, call
`DualTrackHumanEngine.submit_order(...)` → crashes.

Fix: treat a missing bound as open — skip that side's range check.
```python
rng = plan.get("range") or {}
low, high = rng.get("low"), rng.get("high")
if low is not None and price < float(low):
    return True
if high is not None and price > float(high):
    return True
```
For a long (floor only) an order below the floor is out-of-plan; there is no
upper bound. Mirror for a short (ceiling only, open downside).

Test: manual order under an open-ended long validates; a buy below the floor
flags `out_of_plan=True`; an order far above the floor is in-plan.

## BUG 2 — HIGH (verify live path) — repeated cycle run appends machine fills

Running the orchestrator twice on the same cycle **doubles** the machine fills:
```
fast-forward 2026-06-15_DAY  -> machine fills: 68
fast-forward 2026-06-15_DAY  -> machine fills: 136   (should be 68 — replace, not append)
```
`services/dualtrack_machine.py:93` uses `write_json(self._fills_path(cycle_id),
fills)` which *replaces* — so the append is happening elsewhere in the run path
(pre-cycle + intraday sequencing, or a load-extend-write somewhere). D8-3
("idempotent ticks — running the intraday tick twice yields identical persisted
fills, no double-count") does not actually hold across repeated invocations.

Why it matters: the DT8 launchd job runs `--event auto` every 60s. Over a 12h
cycle that is ~720 intraday invocations. If each re-run appends rather than
replaces the recomputed fills, the machine fills, PnL, and ledger balloon
without bound — the machine track becomes garbage in live operation.

Action: make the cycle's machine fills a pure function of the cycle bars —
every intraday recompute REPLACES the fills file for that cycle (idempotent by
construction, matching the prefix-replay design). Extend the D8-3 test to run
the intraday/auto step twice for the same cycle and assert the fill count and
PnL are unchanged (not doubled). Confirm specifically for the live `--event
auto` path, not only `fast-forward-day`.

## Not affected

DT6 replay page + D6-1 (closed-only, no open-cycle leak — verified), direction
grading, blind protocol, lab R5, machine sizing (fixed per-rung, verified).
