# DT1–DT4 Review Fix List (before DT5)

Adversarial review + independent verification of the dual-track console
DT1–DT4 on `feat/dualtrack-console`, 2026-07-05. Full suite re-run green
(891 passed). Refactor of `lab_r5_grid` → `dualtrack_grid_core` verified
behavior-preserving (inline-vs-refactored R5-B diff: all 36 cells identical).

Apply these on `feat/dualtrack-console`, keep §2 R4 files untouched, re-run
the full suite, then proceed to DT5.

## FIX-1 · CRITICAL · clamp `as_of` at the HTTP boundary
`pipelines/dashboard_server.py` GET handlers `_handle_dualtrack_plan_get`,
`_handle_dualtrack_machine_get`, `_handle_dualtrack_current` pass
`params.get("as_of")` from the query string into the builders. This lets
`GET /api/dualtrack/plan/{cycle}?as_of=<future>` reveal the AI plan during the
blind window (via `DualTrackPlanStore.reveal_allowed`, store.py:89, which
compares `parse_utc(as_of) >= lock_deadline`), and
`GET /api/dualtrack/machine/{cycle}?as_of=<cycle end>` return the full
post-close reveal (fills/prices/rungs) mid-cycle (machine.py:148).
**Fix:** do not accept `as_of` from the network on these paths — use server
time only. Keep `as_of` as an internal/test-only parameter on the builder
functions (existing tests call builders directly, so they keep working).
**Test:** `GET /plan/{cycle}?as_of=<future>` during the blind window returns
`ai_plan_revealed: false`; `GET /machine/{cycle}?as_of=<past cycle end>`
mid-cycle returns PnL-only.

## FIX-2 · CRITICAL · gate `cycle/current` author behind reveal
`build_dualtrack_cycle_current_response` (dashboard_server.py:449) calls
`store.effective_plan(...)` which falls back to the AI plan whenever the human
isn't locked (store.py:110-112), exposing `effective_plan_status.author:"ai"`
during the blind window even with real server time. That leaks that the AI has
taken a directional stance before the owner blind-writes.
**Fix:** suppress `author` (and compute `machine_stands_down` conservatively)
until `reveal_allowed(cycle_id)` is true.
**Test:** pre-lock `cycle/current` omits `author`; post-lock (or post-deadline)
it includes it.

## FIX-3 · HIGH · re-arm never fires in production
`dualtrack_grid_core.py:105` re-arms only when `stop is None`, but `run_plan`
always passes a hard stop derived from the mandatory `invalidation`
(`_hard_stop`, machine.py:259). So the machine stops out permanently on first
breach and never re-arms — contradicting build-spec §5 ("hard stop from
invalidation; re-arm once"). The golden test masks this by exercising the
test-only `run_grid_cycle` wrapper (no stop passed), not the production
`run_plan`/`run_effective_plan`.
**Fix:** allow exactly one re-arm after the plan-invalidation stop fires
(track "stop consumed once"), so both coexist as the spec intends.
**Test:** re-point the acceptance-5 golden/parity test at `run_plan`
(production entrypoint) on a stop-out + re-arm cycle.

## FIX-4 · DECISION (owner: keep direction-only) · grading vs invalidation
`dualtrack_scoring._grade_plans` grades direction (close vs open), not the
`invalidation` object — contradicting build-spec §3.8 ("same object drives
(a) machine hard stop, (b) plan grading"). Owner decision: **grading stays
direction-only** (the scoreboard measures the 12h direction call vs the 60%
bar; invalidation is the machine's risk stop, a separate concern).
**Fix:** update build-spec §3.8 wording to stop claiming invalidation drives
grading; no code change to scoring.

## FIX-5 · TEST HARDENING (not a bug) · de-circularize parity test
Post-refactor both `lab_r5_grid.simulate_cycle` and the runner call the same
`simulate_conditional_grid`, so `test_acceptance_7_5_...` compares the function
to itself. Behavior-preservation was verified out-of-band (inline-vs-refactored
diff identical), so this is not currently masking a bug — but the test should
compare against a committed frozen fixture (expected per-fill events for the 3
golden cycles incl. stop-out + re-arm) so a future regression in the shared
core is actually caught.
