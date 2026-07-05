# Spec: 人机双轨作战台 Dual-Track Console (build task "DT")

Hand-off spec for building the dual-track trading console. Self-contained — the
implementing agent should not need the originating chat. Verify every file/line
reference against current code before relying on it. Product decisions in this
doc are FINAL (locked by the owner 2026-07-05); implementation choices are
yours where not specified.

## 1. Objective

One page, one daily loop: **before each cycle the owner blind-writes a trading
plan (作战单); during the cycle a machine track auto-executes the effective
plan while the owner trades manually on a separate track; after the cycle both
tracks are scored with the same ruler.** The console generates, as a side
effect of daily use, the direction-call track records the strategy lab needs.

Product context (read first):
- `docs/uiux/dual-track-console-spec.md` — product spec + v1.1 revisions (all
  owner decisions; treat as requirements)
- `mockups/dualtrack-v2.html` — the visual contract (layout, states, copy,
  design tokens). Match it closely; tokens come from `dashboard-v4.html` :root.
- `outputs/lab/reports/R5_grid_oracle.md`, `R5C_grid_density.md` — the
  experiments behind the grid parameters and the 60% gate.

Scope freeze: GOLD only, PAPER only, two tracks × $10,000, default 10×
leverage cap. No real-money order routing in this task.

## 2. What already exists (reuse, don't rebuild)

- `dashboard-v4.html` + `pipelines/dashboard_server.py` (`/api/dashboard`,
  `DashboardState`) — page/server pattern, design tokens, bilingual labels.
- `dashboard-replay-v4.html` — replay page with URL params
  (`strategy, cursor, layout, ...`); the synced dual replay extends this.
- `services/lab_r5_grid.py` — `segment_cycles`, `simulate_cycle` (conditional
  grid mechanics: rungs, tp_mult, re_arm, budget sizing, conservative intrabar
  rules). The LIVE paper grid runner must produce identical fills to this
  simulator on identical bars (see acceptance 5).
- `services/journal_store.py` — `write_json` atomic writes. ALL persistence
  goes through it.
- Paper execution cost model: `load_risk_rules()['default']['paper_execution_costs']`
  — but note the console trades CFD raw-pricing economics: per-side cost is
  config-driven (default 0.5bp/side; see §6 config).
- Daily AI market view: the DirectionBiasGate/daily-review path reads a
  host-local Obsidian vault (known-fragile; see `services/direction_bias_gate.py`
  and its data source). AI plan derives from it. When unreadable → fail-closed
  (stand down), never a default direction.
- `services/binance_futures_feed.py` / `data/market_data.db` (`bars`,
  GOLD 1m) — bar source. Live chart: browser subscribes directly to Binance
  USDⓈ-M combined stream `kline_1m` for XAUUSDT as the CFD price proxy
  (document the venue-divergence caveat in the page footer).
- `services/feishu_report_sender.py` — weekly card delivery.
- Registry discipline: any strategy evaluation stays in the lab; this task
  builds an execution console, not experiments.

Branch note: R4 lab work is in flight on this working tree
(`pipelines/lab_run.py`, `services/lab_registry.py`, `lab_r4_promotion.py`).
Do NOT modify those files. Build on a fresh branch `feat/dualtrack-console`.

## 3. Non-negotiable invariants (each one gets a test)

1. **Blind before lock.** The API must not serve the AI plan for a cycle until
   the human plan for that cycle is locked OR the lock deadline passed. Not
   "the frontend hides it" — the endpoint returns nothing. Test proves it.
2. **Locked = immutable.** Mutation attempts after lock are rejected and
   audit-logged. Test.
3. **Precedence, fail-closed.** Effective plan = human if locked before
   deadline, else AI plan if available, else NO PLAN → machine track stands
   down for the whole cycle (zero orders). Test all three branches.
4. **Intraday machine blind.** During an open cycle the machine-track endpoint
   exposes ONLY: realized PnL, unrealized PnL, layer status strings. Entries,
   inventory, rung levels appear in the payload only after cycle close. Test:
   intraday payload contains no order/price fields.
5. **No intervention surface.** The console has no pause/flatten/override
   endpoint for the machine track. Emergency stop lives in ops tooling only.
6. **Track isolation.** Separate position ledgers and cash accounts per track;
   human orders can never touch machine positions and vice versa. Identical
   cost model applied to both.
7. **Discipline flags, not blocks.** Human orders outside the plan's range or
   against its direction execute normally but are flagged `out_of_plan` in the
   ledger. Never block a manual order for plan reasons.
8. **Structured invalidation only.** Plan invalidation is
   `[{side: below|above, price: float, confirm: close_1m|touch}]` — no free
   text. The same object drives the machine hard stop; direction-only plan
   grading is handled separately by the cycle scorer.
9. **UTC internally.** Cycles are UTC 01:00–13:00 (DAY) and 13:00–01:00
   (NIGHT) — 09:00/21:00 Beijing. All timestamps ISO-8601 UTC; CST only in
   display strings.
10. **Atomic writes** via `journal_store.write_json` for every artifact.
11. **No production impact.** No behavioral change to `pipelines/bot.py`,
    multi_strategy_runner, gates, executors; full existing test suite passes.

## 4. Data contracts

Storage root `outputs/dualtrack/` (git-ignored like other outputs):

```
plans/{cycle_id}_{author}.json      # author: human | ai
  {cycle_id, author, direction: long|short|flat, range: {low, high},
   key_levels: [float, ...],                      # 1..N, owner decision v1.1
   invalidation: [{side, price, confirm}],        # structured, §3.8
   confidence: 1..10 | null, locked_at, source: console|obsidian,
   status: locked | fallback_active | absent}
fills/{cycle_id}_{track}.json       # track: machine | human
  [{fill_id, ts, side, price, notional, sl, tp, layer: grid|trend|manual,
    order_type: limit|market, out_of_plan: bool, realized_pnl}]
cycles/{cycle_id}.json              # cycle meta + close snapshot
  {cycle_id, kind: DAY|NIGHT, start, end, open_price, close_price,
   realized_direction, effective_plan_author, machine_stood_down: bool,
   opportunity_count, machine_captured, human_captured}
scoreboard.json                     # rolling plan grades
  {human: {graded, hits, rolling_30: {graded, hits}}, ai: {...},
   trend_leg_gate: {threshold: 0.60, armed: bool, basis: rolling_30}}
ledger/daily/{YYYY-MM-DD}.json      # per-day per-track totals
ledger/weekly/{ISO-week}.json       # weekly rollup + target progress
```

`cycle_id` format: `YYYY-MM-DD_DAY|NIGHT` (date = cycle start, Beijing).

API (extend `pipelines/dashboard_server.py` or a sibling server file):

```
GET  /api/dualtrack/cycle/current          # meta + countdown + effective-plan status
GET  /api/dualtrack/plan/{cycle_id}        # human plan; ai plan ONLY per §3.1
POST /api/dualtrack/plan                   # create/lock human plan (blind window)
GET  /api/dualtrack/machine/{cycle_id}     # intraday: PnL-only; closed: full reveal
POST /api/dualtrack/orders                 # human paper order (limit|market + sl/tp)
GET  /api/dualtrack/human/{cycle_id}       # human positions/fills (own track: full)
GET  /api/dualtrack/attribution/{cycle_id} # closed cycles only: both tracks, same ruler
GET  /api/dualtrack/ledger?week=...        # daily rows + weekly rollup
POST /api/dualtrack/verdict                # one-line review note per cycle
```

## 5. Build phases (order matters; each phase lands green before the next)

- **DT1 Cycle clock + plan store.** Cycle segmentation (reuse the UTC-hour
  rule from `lab_r5_grid._cycle_key`), plan schema validation, blind state
  machine (draft → locked → revealed; deadline handling), AI-plan ingestion
  from the daily market view (direction + range derived as
  `cycle_open ± k×prev_cycle_range`, k from config). Invariants 1–3, 8–10.
- **DT2 Machine track runner.** Paper conditional grid executing the effective
  plan on live 1m bars: base layer tp1 direction-agnostic within range; trend
  leg tp2 in plan direction, armed only when scoreboard gate ≥60% (invariant:
  gate read at cycle start, not mid-cycle); hard stop from `invalidation`;
  re-arm once; flatten at cycle end. MUST reuse/share the mechanics of
  `lab_r5_grid.simulate_cycle` (extract common core rather than duplicate).
  This is the riskiest phase — do it early, test it hardest (acceptance 5).
- **DT3 Human track.** Paper order engine (limit/market, SL/TP brackets),
  positions, per-fill ledger, out_of_plan flagging. Invariants 6–7.
- **DT4 Scoring + ledger.** Plan grading (realized cycle direction vs plan;
  flat plans ungraded), scoreboard rollups, opportunity census per closed
  cycle (zigzag r=10bp, runs ≥0.3%; captured = fill inside a run's window
  with matching direction), daily/weekly ledger, cycle attribution payload.
- **DT5 Frontend** `dashboard-dualtrack-v5.html`. Single file, v4 tokens,
  bilingual, three moments per `mockups/dualtrack-v2.html`: blind form (multi
  key-levels, structured invalidation, lock flow), intraday (live WS kline
  main chart + 2 collapsible context TFs; range band, own markers, TP/SL
  lines, key levels; machine card PnL-only; order ticket; risk card), 
  attribution (monthly cash-flow header with floor/ceiling calibration note
  ALWAYS visible, dual-track table, plan grading, verdict input, weekly
  ledger table).
- **DT6 Synced dual replay.** Extend the replay page with
  `layout=dualtrack&cycle=...`: two panes, one transport, human fills left,
  machine fills right (closed cycles only).
- **DT7 Weekly card.** Sunday-evening pipeline: weekly ledger → Feishu card
  via `feishu_report_sender` (existing channel conventions).

## 6. Config (new `configs/dualtrack.yaml`; values are launch defaults)

```yaml
capital_per_track_usd: 10000
max_leverage: 10                 # notional budget = capital * max_leverage
cycle_hours_utc: {day_start: 1, night_start: 13}
plan_lock_deadline_min_before_cycle: 0   # lock allowed until cycle start
grid:                            # from R5-C best cell; provisional until next lab round
  spacing_bp: 20
  range_k: 1.0                   # half-width = k * prev cycle (high-low)
  tp_mult_base: 1
  tp_mult_trend: 2
  re_arm_max: 1
  trend_leg_budget_pct: 20
cost_per_side_bp: 0.5            # CFD raw-pricing estimate; keep configurable
scoreboard: {gate_threshold: 0.60, window_cycles: 30}
census: {reversal_bp: 10, min_run_pct: 0.3}
weekly_target_usd: [1000, 1500]
```

## 7. Acceptance criteria

1. Invariant tests 1–11 all green (each invariant has at least one dedicated
   test; blind/immutability/precedence/PnL-only are API-level tests).
2. Cycle math: bars at UTC 00:59/01:00/12:59/13:00 land in the correct cycle;
   cycle_id dates use Beijing anchor. Tested.
3. A full simulated day (recorded bars replayed through the runner) produces:
   two cycles, plans graded, ledger rows, attribution payloads — end to end.
4. Frontend states verified (can be via server-rendered payload assertions):
   pre-lock (AI plan absent from every response), intraday (machine payload
   PnL-only), post-close (reveal complete).
5. **Sim-live parity:** the DT2 runner on a closed cycle's bars produces the
   same fills/PnL as `lab_r5_grid.simulate_cycle` under the same config
   (golden test on ≥3 recorded cycles, including one stop-out and one re-arm).
6. Ledger arithmetic: sum(fills) == cycle totals == daily row == weekly rollup
   (property-style test).
7. House rules: typed, files <800 lines (split by feature), ≥80% coverage on
   new modules, pytest, all writes atomic; existing suite untouched and green.

## 8. Non-goals (do not build)

Broker API sync / real-money routing; intervention UI for the machine track;
multi-asset; mobile; in-browser experiment or parameter tooling (lab CLI owns
that); automation of the human track (would contaminate the comparison);
changes to lab R4 files listed in §2.
