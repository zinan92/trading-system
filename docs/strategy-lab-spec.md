# Spec: Strategy Lab (research loop, task "R")

Hand-off spec for building the strategy research loop. Self-contained — an
implementing agent should not need the originating chat. Verify every file/line
reference against the current code before relying on it.

## 1. Objective

Build the **research loop** that turns strategy iteration into a measurement
discipline. Today the system has a production loop (signals → gates → paper
execution → traces) but no research loop: nothing can answer "does this
strategy have positive expectancy after costs?" without waiting months of
forward paper. At ~2 trades/day (risk cap), statistically separating a 55%
edge from coin-flipping needs on the order of 800 trades ≈ 2 years forward.
The lab compresses that to minutes by replaying strategies over historical 1m
bars with a trustworthy simulator and an anti-overfitting protocol.

The lab must be able to answer four standing questions (the first experiment
set, §8):

- **E1** — does the 1m MACD cross baseline have positive expectancy after
  costs, and what is its break-even cost level?
- **E2** — does the human direction filter (`DirectionBiasGate`) add alpha
  over the same strategy without it?
- **E3** — what fraction of time is gold in a tradeable directional regime?
- **E4** — how much does the maker-vs-taker execution assumption change E1?

Goal in one sentence: **a deterministic, cost-aware, walk-forward evaluation
harness with an experiment registry and a promotion gate, proven by running
E1–E4 end to end.**

Scope freeze: GOLD/XAUUSDT 1m/5m bars only, research artifacts only. The lab
must not change any production trading behavior.

## 2. What already exists (reuse, don't rebuild)

- `services/strategy_backtester.py` (~164 lines) — bar+signal simulator that
  already reuses the SAME paper-execution cost model as forward paper
  (`load_risk_rules()['default']['paper_execution_costs']`: `spread_pct`,
  `slippage_pct`, commission fields; see `_side_cost` ~line 112). Stop /
  target / timeout exits; `_metrics` + `_sharpe` (~lines 125-163). This is
  the seed of the lab evaluator.
- `services/local_backtester.py` (~220 lines), `services/backtest_client.py`
  (~80 lines) — audit for overlap; decide ONE engine and deprecate or delegate
  the rest (document the decision).
- `services/strategy_leaderboard.py` (~226 lines) — per-day forward-paper
  rows (win_rate, max_drawdown_pct, sharpe from equity points). This is the
  PAPER side of the promotion funnel; do not duplicate it.
- `services/macd_signal_engine.py` / `services/signal_engine.py` — signal
  generation. Signal ids are deterministic
  (`sha256(macd:sym:date:direction:CLOSE)` per docs/decision-trace-spec.md).
- `services/direction_bias_gate.py` — `DirectionBiasGate.apply()` blocks /
  downweights counter-bias signals from a daily `direction_score` (~lines
  27-59). Needed for E2. Investigate where its historical decisions and the
  daily market-view records persist; if history is thin, E2 falls back to
  prospective measurement plus whatever retro data exists.
- Data: `outputs/clean_bars/` (~44 day-dirs since 2026-05-07, with gaps);
  backfill infrastructure exists (`outputs/binance_usdm_1m_backfill/`,
  `outputs/binance_usdm_backfill/` — locate the pipelines that write these
  and their entry points).
- `services/config_loader.load_risk_rules()` — thresholds
  (`min_signal_strength=60`, `min_confidence=55`, `risk_reward_min=1.5`,
  `max_loss_pct=0.5`).
- Atomic writes: `services/journal_store.write_json` — use for every lab
  artifact.
- Python 3.13 (matches the strategies launchd job). pytest. House rules:
  files <800 lines, type annotations, immutable patterns, 80% coverage on
  new modules.

## 3. Non-negotiable principles

These encode the failure modes the lab exists to prevent. Violating any of
them silently is a spec violation even if tests pass.

1. **OOS-only judgment.** No metric computed on data a parameter choice has
   seen may be used for promotion. Walk-forward for iteration; a quarantined
   holdout (most recent K weeks, K≥4) that experiments cannot read — enforced
   in code (separate data path + test), not by convention.
2. **Every trial is registered.** Each evaluation run writes a registry
   entry BEFORE results are inspected. Reports must display the total trial
   count for the hypothesis family so best-of-N inflation is visible.
   (Multiple-testing discipline; deflated-Sharpe context.)
3. **Cost curve is mandatory.** Every experiment reports metrics at per-side
   costs of {0, 0.5, 1, 2, 5} bp **plus** the current Binance USDⓈ-M reality
   (taker 5 bp, maker 2 bp — verify current values). Break-even cost is a
   first-class output.
4. **Objective = constraints, then scalar.** Hard gates first: OOS trade
   count ≥ 100; max drawdown ≤ configured cap; still positive at cost +50%;
   non-negative in ≥2 of 3 regime slices. Only gate-passers are ranked, by
   OOS Sortino (also report Sharpe, log-wealth, expectancy/trade after cost,
   turnover). Constraint values live in config, not code.
5. **Sizing is pinned and separate.** Research evaluation uses FIXED notional
   per trade (e.g. $1,000, no leverage semantics) so metrics measure signal
   quality, not sizing policy. Note: production has a known
   margin-vs-notional (5× leverage) inconsistency between the risk gate and
   the paper executor — the lab must not inherit ambiguous sizing; pin the
   semantics explicitly in code and doc.
6. **Fail-closed metrics.** NaN/missing bars/zero-trade windows make an
   experiment INVALID (registered as such), never silently dropped or coerced
   to 0. No lookahead: signal computed on bar *t* close ⇒ execution no
   earlier than bar *t+1* open; intrabar stop/target resolution order must be
   documented and conservative (stop before target when both hit).

## 4. What to build

### M1 — Data foundation

- Backfill 12–24 months of 1m XAUUSDT bars via the existing backfill
  pipelines; produce a **coverage report** artifact (gaps by day/hour).
- Regime labeler: tag each day (and each 4h block) with volatility bucket
  (realized vol terciles) and trend bucket (e.g. ADX or |close-open|/ATR
  based) → `outputs/lab/regimes/{range}.json`. Used by all evals (principle
  4) and directly answers E3.

### M2 — Evaluator hardening

- Choose the single engine (start from `strategy_backtester.py`); document
  simulation semantics (entry timing, intrabar resolution, timeout, cost
  application) in the module docstring.
- **Offline signal regeneration**: a bar-replayer that regenerates strategy
  signals deterministically from historical bars (starting with the MACD
  engine) so evaluation does not depend on live-runner history.
- Cost model: accept per-side bp override grid on top of the existing
  cost-rules path.
- **Sanity battery** (pytest, part of CI for the lab):
  a. random-direction strategy at zero cost → mean net PnL ≈ 0 (statistical
     bound, seeded);
  b. random strategy WITH costs → mean ≈ −(expected cost drag);
  c. synthetic series with planted trend → strategy that should capture it
     does, within tolerance;
  d. determinism: identical config twice → identical results (modulo
     timestamps).

### M3 — Objective module

`services/lab_objective.py`: pure functions
`evaluate_objective(trades, equity, config) -> {gates: {...}, metrics: {...},
passed: bool}` implementing principle 4. No I/O.

### M4 — Walk-forward harness

`services/lab_walkforward.py`: rolling train/validate windows (window sizes
in config; default e.g. train 60d / validate 14d / step 14d) over the
backfilled range, holdout excluded by construction. Emits per-window and
aggregate results.

### M5 — Experiment registry

`services/lab_registry.py` + `outputs/lab/experiments/{exp_id}.json` and
`outputs/lab/registry.json` (index). Entry schema (compact):

```
exp_id, created_at, hypothesis, family,          // family groups trials for N-count
strategy_ref, params_diff, data_range, windows,
cost_grid_results, regime_slice_results,
objective: {gates, metrics, passed},
status: valid | invalid | error, notes
```

Auto-registered at run start (status pending → final). Atomic writes. A small
CLI: `python -m pipelines.lab_run list|show|compare`.

### M6 — Promotion gate

Doc + code: a candidate is *paper-eligible* only after passing the objective
gates on walk-forward AND on the one-shot holdout evaluation (holdout may be
consumed once per candidate family; consumption is recorded in the registry).
Wire "paper-eligible" as a flag the strategy registry can read — do NOT
auto-enable anything in production. Paper→live comparison stays with the
existing leaderboard; add a `lab_expectation` block to the leaderboard row so
live-vs-backtest divergence is visible per strategy.

### M7 — First experiment set (proves the lab end to end)

- **E1** `macd_baseline_cost_curve`: 1m MACD cross, no direction filter,
  full cost grid, walk-forward. Deliver break-even bp.
- **E2** `direction_filter_ab`: same strategy ± `DirectionBiasGate` replay.
  First locate historical market-view/direction records; if <60 days of
  history, run retro on what exists AND set up prospective logging, report
  both with sample sizes.
- **E3** `gold_regime_share`: % of hours/days in each regime bucket over the
  backfilled range (falls out of M1's labeler).
- **E4** `maker_vs_taker`: E1 re-run under maker-fee + limit-entry
  assumption (document fill-assumption caveats conservatively: assume fill
  only if the bar trades through the limit price).

## 5. File map (new)

```
services/lab_evaluator.py      // engine wrapper + signal regeneration glue
services/lab_objective.py
services/lab_walkforward.py
services/lab_registry.py
services/lab_regimes.py
pipelines/lab_run.py           // CLI entry
tests/test_lab_evaluator.py    // incl. sanity battery
tests/test_lab_objective.py
tests/test_lab_walkforward.py
tests/test_lab_registry.py
docs/strategy-lab-spec.md      // this file; update if reality diverges
outputs/lab/...                // artifacts (git-ignored like other outputs)
```

Keep each file <400 lines where feasible; split rather than grow.

## 6. Success criteria (acceptance)

1. **Determinism**: running any experiment twice with the same config yields
   identical registry entries (modulo timestamps). Tested.
2. **Sanity battery passes** (M2a–d) in pytest.
3. **Holdout isolation is enforced**: a test proves experiment code cannot
   read holdout data through the public API; holdout consumption is recorded.
4. **E1–E4 run end to end** on ≥12 months of backfilled bars, each producing
   a registry entry + a human-readable markdown report under
   `outputs/lab/reports/`, and E1's report states the break-even cost in bp.
5. **Every run is registered** including failed/invalid ones; registry writes
   are atomic (kill-test: interrupting a run never corrupts
   `registry.json`).
6. **Trial-count visibility**: any report shows N trials for its hypothesis
   family.
7. **No production impact**: `git diff` shows no behavioral change to
   pipelines/bot.py, multi_strategy_runner, gates, or executors (read-only
   imports allowed); full existing test suite still passes.
8. **House rules**: new modules typed, ≥80% coverage, files within size
   limits, all writes via `journal_store.write_json`.

## 7. Sequencing & risks

Build order M1 → M2 → M3/M5 (parallel) → M4 → M6 → M7. M1 is the long pole
(backfill runtime) — start it first, build M2 against the existing 44 days
meanwhile.

Known risks: (a) bar-data gaps — the coverage report must precede any
conclusion; (b) lookahead bugs — the sanity battery and the t+1-open rule are
the guard; (c) sizing ambiguity — principle 5; (d) direction-history
scarcity — E2's fallback is defined above; (e) engine divergence between the
three existing backtester files — resolve in M2, delete or delegate.

## 8. Non-goals

- No UI page (JSON + markdown reports suffice; a lab panel may join the
  command center later).
- No multi-asset, no live-trading changes, no fee-venue migration.
- No parameter-search campaigns beyond E1–E4 — this task builds the
  instrument, not the search. Optimization campaigns are a separate task
  that must go through this lab's registry.
