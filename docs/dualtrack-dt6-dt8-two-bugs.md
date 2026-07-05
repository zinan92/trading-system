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

## BUG 2 — CORRECTED DIAGNOSIS — trend gate reads the LIVE scoreboard (MEDIUM)

**This was originally reported (by me) as a "fills append / idempotency" bug and
its live severity was overstated. That was wrong. Corrected here after isolating
the mechanism.** Codex's "replace, not append" hardening (machine.py:93) is
correct and kept, but it was never the cause.

What actually happens, isolated step by step:
- `run_effective_plan` alone is idempotent — same bars twice → 68, 68 (replace works).
- `intraday_tick` repeated on an OPEN cycle (no close between) → 68, 68 (idempotent).
- But: `run → scorer.close_cycle → run` → 68, then **136**. The extra 68 are
  `layer: trend`, not duplicated grid:
  ```
  run 1 layers:            {grid: 68}
  run 2 (post-close):      {grid: 68, trend: 68}
  ```

Root cause: the machine runner arms the **trend leg** by reading the **live,
mutable scoreboard** at run time. On the first run the scoreboard is empty →
gate off → grid only. After the cycle closes, its own graded hit lands in the
scoreboard (1/1 = 100% ≥ 60% gate) → a re-run of the same cycle now arms the
trend leg and adds ~68 trend fills. So the "doubling" is the trend leg
activating, not an append.

Severity is MEDIUM, not the catastrophic live-ballooning originally claimed:
- In normal forward operation the scoreboard only changes at cycle close, so it
  is stable during a cycle's intraday ticks — the gate does not flip mid-cycle
  and fills do not balloon.
- The real defect: **DT2's invariant "trend gate read at cycle START, not
  mid-cycle" is not implemented** — the gate is read live. Consequence:
  re-processing / replaying / re-running acceptance on a closed cycle is
  non-deterministic (its own close feeds back into the gate).

Fix: honor the DT2 invariant — snapshot the trend-gate armed flag into the cycle
state at `pre_cycle` (from the scoreboard as of cycle start), and have
`intraday_tick` / `close_cycle` read that FROZEN flag instead of the live
scoreboard. Then a cycle's machine result is a pure function of its bars + its
frozen gate, and re-processing is deterministic.

Test: `run → close → run` on the same cycle yields identical fills (frozen gate,
no trend leg appearing on the re-run); and a cycle whose frozen gate is armed
runs grid+trend on the FIRST pass.

## Not affected

DT6 replay page + D6-1 (closed-only, no open-cycle leak — verified), direction
grading, blind protocol, lab R5, machine sizing (fixed per-rung, verified).
