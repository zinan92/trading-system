# Spec: Dual-Track Console DT6 + DT8 (build tasks "DT6", "DT8")

Supplement to `docs/dualtrack-console-build-spec.md`. DT1–DT5 are done and
committed (`72bd80c`, `dc5d8f9`). This spec covers the two pieces that turn the
MVP workbench into a self-running engine: **DT8 (cycle orchestrator + plan-schema
refinement)** and **DT6 (synced dual replay)**. DT7 (weekly Feishu card) stays
deferred until a week of ledger exists.

Same rules as before: self-contained; verify every file/line ref before relying
on it; build on `feat/dualtrack-console`; do NOT touch the §2 R4 files
(`pipelines/lab_run.py`, `services/lab_registry.py`, `services/lab_r4_promotion.py`)
or the pre-existing uncommitted working-tree changes (`configs/strategy.yaml`,
`services/technical_rule_signal_engine.py`, `services/strategy_registry.py`,
`tests/test_technical_rule_signal_engine.py`).

DT8 and DT6 are **independent files and can be built in parallel.** DT8 is the
critical path — it produces the closed-cycle data DT6 renders and DT7 will
aggregate. DT6 is an empty frame until DT8 (or a manual cycle run) has closed at
least one cycle with fills.

## Why this exists (the gap DT1–DT5 left)

DT1–DT4 built the components (`DualTrackMachineRunner.run_effective_plan`,
`DualTrackPlanStore.ensure_ai_plan`, `DualTrackScorer.close_cycle`) and DT5 built
the UI, but **nothing drives them on a schedule.** No cron/launchd/daemon
ingests the AI plan before a cycle, runs the machine grid on live bars during
the cycle, or closes+scores at cycle end. The console can accept a human blind
plan and manual orders today, but the machine track and auto-scoring do not run
unattended. DT8 closes that gap.

---

# DT8 — Cycle orchestrator + plan-schema refinement

## DT8.1 Plan-schema refinement (owner insight 2026-07-05)

For a **directional** plan, an upper Range bound is conceptually wrong: a long
that expects a breakout has open-ended upside; the only hard boundary is the
**floor = invalidation price**. This matches the grid mechanics exactly (a long
grid places buy rungs from the anchor down to the floor; take-profits are
per-rung and open upward — no hard cap needed).

Change `validate_plan` (`services/dualtrack_store.py:162`) and the schema:

- `range.high` becomes **optional** when `direction ∈ {long, short}`. When
  omitted, upside (for long) / downside (for short) is open; the grid's far
  extent needs no bound because take-profits are per-rung.
- `range.low` for a long is the **floor** and MUST be consistent with a
  `below` invalidation entry (for a short: `range.high` is the ceiling, MUST be
  consistent with an `above` invalidation). If the floor is omitted, derive it
  from the matching-side invalidation price.
- `key_levels` (≥1) carries the upside structure (e.g. 4210 as a scaling/target
  marker) — it is NOT a cap.
- `flat` plans need no range (they mean "no trade" → machine stands down).
- The stored plan keeps a stable shape: persist `range: {low, high|null}` so old
  readers don't break; `high: null` is the "open" signal.

Owner's first real plan is already recorded in this shape for reference:
`outputs/dualtrack/plans/2026-07-05_NIGHT_human.json` (long, floor 4160, key
4210, high 4390 marked as a soft placeholder in `notes`). Under the refined
schema that `high` would be `null`.

Update `mockups/dualtrack-v2.html` copy and DT5 form: for a directional plan the
upper Range input is optional/greyed with a "上方开放 open upside" affordance;
the floor input is labelled as the invalidation/floor and is required.

## DT8.2 The orchestrator

New `pipelines/dualtrack_cycle_runner.py` (+ a launchd plist under the repo's
existing agent-install path, mirroring the strategies job). It knows the cycle
clock (`services/dualtrack_clock.cycle_window`) and fires three events per cycle:

1. **Pre-cycle (at lock deadline / cycle open, UTC 01:00 & 13:00):**
   `DualTrackPlanStore.ensure_ai_plan(cycle_id, ...)` to ingest the AI plan from
   the daily market view. If the market view is unreadable → fail-closed (no AI
   plan; if no human plan either, the machine stands down — invariant 3).

2. **Intraday (each new closed 1m bar):** run the machine grid over the cycle's
   bars-so-far and persist fills + running PnL. Use the **prefix-replay**
   approach: call `DualTrackMachineRunner.run_effective_plan(cycle_id,
   bars[cycle_start .. now], prev_range=...)` each tick. Because
   `simulate_conditional_grid` is a deterministic bar loop, the prefix run over
   `bars[0:k]` is by construction identical to the batch simulator on that
   prefix — so **sim-live parity holds automatically** and at cycle end the
   final prefix (`bars[0:N]`) equals the full batch run. `prev_range` = previous
   cycle's high−low (compute from the prior cycle's bars via
   `lab_r5_grid.segment_cycles` or an equivalent one-cycle helper).
   - Intraday `machine_payload` stays PnL-only (invariant 4, already enforced) —
     realized from closed round-trips, unrealized marked-to-market from open
     rungs at the latest close. (Adding real unrealized is a small extension to
     `machine_payload`; keep it read-only, never expose rung levels intraday.)

3. **Cycle close (UTC 13:00 & 01:00):** final prefix run over the full cycle,
   then `DualTrackScorer.close_cycle(cycle_id, bars)` to set realized direction,
   grade both plans (direction-only per FIX-4), write the daily+weekly ledger,
   and snapshot the cycle. After close, the full machine reveal is available.

Bars come from `data/market_data.db` (GOLD 1m), kept current by the existing
`services/binance_futures_feed.py` ingestion — confirm that job is running for
the console venue, or have the orchestrator trigger a feed refresh before each
tick. Document the venue-divergence caveat (Binance USDⓈ-M as CFD price proxy)
already noted in the page footer.

## DT8.3 Invariants (each gets a test)

- **D8-1 Deterministic replay parity.** Prefix run at any k over `bars[0:k]`
  equals the batch `simulate_conditional_grid` over the same slice (extend the
  existing golden fixture to assert an intraday prefix, not just the full cycle).
- **D8-2 Fail-closed ingestion.** Market view missing/corrupt → no AI plan, no
  crash, audit event; if no human plan either → machine stands down (zero fills)
  for the whole cycle.
- **D8-3 Idempotent ticks.** Running the intraday tick twice for the same
  bar-set yields identical persisted fills (no double-count). Atomic writes.
- **D8-4 Close is single-shot.** `close_cycle` grades and writes the ledger
  exactly once per cycle even if invoked again (guard on existing cycle
  snapshot).
- **D8-5 No intraday leak survives orchestration.** With the orchestrator
  running mid-cycle, `GET /api/dualtrack/machine/{open_cycle}` still returns
  PnL-only (re-assert invariant 4 against live-produced fills).
- **D8-6 Schema: directional plan without upper bound validates;** a long with
  `range.high` omitted is accepted, floor derived from the `below` invalidation,
  and the grid runs. A flat plan stands down.

## DT8.4 Acceptance

1. D8-1..D8-6 green.
2. Replay a recorded historical day through the orchestrator (fast-forward, not
   wall-clock): two cycles produced, AI plan ingested where a market view
   exists, machine fills accumulate intraday, both plans graded at close, ledger
   rows written — fully unattended.
3. Owner's `2026-07-05_NIGHT` human plan (already stored) grades correctly once
   that cycle's bars close.
4. House rules: typed, <800-line files, ≥80% coverage on new modules, atomic
   writes, existing suite green, R4 files zero diff.

---

# DT6 — Synced dual replay

## DT6.1 What it is

A closed-cycle review page: two synchronized replay panes over one cycle, human
fills on the left, machine fills on the right, one shared transport/cursor. It
is the landing page for the console's existing "查看完整回放" button (DT5 already
stubs this with a "DT6 接入" note).

Build as `layout=dualtrack&cycle=<cycle_id>` on the replay surface
(`dashboard-replay-v4.html` pattern, or a sibling `dashboard-dualtrack-replay.html`
sharing v4 tokens). Reuse the replay transport/cursor machinery already there.

Data: `GET /api/dualtrack/attribution/{cycle_id}` (both tracks' fills + stats,
full reveal) and the cycle's 1m bars. Left pane draws human fills (blue),
right pane draws machine fills (violet); the shared cursor scrubs both panes to
the same timestamp so you can see, at each moment, where you traded vs where the
machine traded.

## DT6.2 Invariant (this one is security-critical)

- **D6-1 Closed cycles only.** The dual replay reveals machine fills, so it MUST
  refuse to render for an open cycle — otherwise it is an intraday machine-leak
  backdoor around invariant 4. Guard at the data layer: `attribution` already
  404s for open cycles; the page must treat that as "cycle not closed yet, come
  back after close," never fall back to the machine endpoint. **Test:** requesting
  the dual replay for an open cycle yields no machine fills.

## DT6.3 Acceptance

1. D6-1 green (open-cycle request reveals nothing).
2. Given a closed cycle with fills on both tracks, both panes render their fills,
   the shared cursor scrubs both in lockstep, and clicking a fill shows its
   detail. (Verifiable against a seeded closed-cycle fixture.)
3. The console's "查看完整回放" button deep-links here with the right `cycle_id`.
4. House rules as above.

---

## Sequencing

DT8 and DT6 in parallel (independent files). DT8 first-among-equals because it
produces the data DT6 needs — DT6 can be built and unit-tested against a seeded
fixture, but only becomes useful once DT8 (or one manual `close_cycle`) has
closed a real cycle. After both land and a few cycles have run, DT7 (weekly
Feishu card) becomes meaningful.

Stop and report after DT8 (before DT6) if the orchestrator surfaces any
live-feed or market-view coupling problem — those are known-fragile and worth a
checkpoint.
