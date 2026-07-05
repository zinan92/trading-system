# Strategy Lab Implementation Notes

## Product Purpose

Strategy Lab exists to reject unreliable strategy promotion. Its job is not to
find the prettiest historical curve; it must make strategy evidence auditable:
fixed rules, fixed sizing, explicit costs, out-of-sample judgment, registered
trials, and fail-closed invalidation when evidence is thin or corrupted.

## Verified Baseline

- Base branch before work: `feat/autonomy-remaining` at `c53aeff`.
- Work branch: `codex/strategy-lab-research`.
- Spec read: `docs/strategy-lab-spec.md`.
- Existing candidate lab seed: `services/strategy_backtester.py`.
- Existing non-lab backtest surfaces:
  - `services/local_backtester.py` is a production signal-evidence helper.
  - `services/backtest_client.py` wraps remote/local evidence for production.
  - `pipelines/backtest_strategies.py` writes legacy backtest reports under
    `outputs/backtests/`.
- 1m backfill entry point: `pipelines/backfill_gold_1m.py`, which calls
  `services.binance_futures_feed.run_binance_usdm_1m_backfill`.
- Historical direction artifacts:
  - `outputs/market_views/`
  - `outputs/direction_bias_decisions/`

## Conservative Decisions

- The lab engine will use `services/strategy_backtester.py` semantics as the
  seed because it already models one-position-at-a-time stop/target/timeout
  outcomes and existing paper-execution costs.
- `local_backtester.py` and `backtest_client.py` remain production evidence
  surfaces. Strategy Lab will not delegate promotion-grade research decisions
  to them.
- Research sizing is fixed notional. It will not inherit production
  `position_size_pct` or leverage semantics.
- Signal-on-close execution is no earlier than the next bar open.
- If stop and target are both touched in the same bar, the stop wins.
- Missing bars, NaN metrics, or zero-trade evaluation windows are invalid
  results, not neutral or zero-valued results.

## Deviations And Reality Checks

- Current local `data/market_data.db` contains `GOLD` `1m` bars starting at
  `2025-12-11T08:05:00+00:00`, which is less than 12 calendar months as of
  this task.
- User decision on 2026-07-05: using the full available XAUUSDT history is an
  acceptable substitute for the original 12-month requirement because Binance
  official metadata shows the contract did not exist earlier.
- `pipelines/backfill_gold_1m.py` defaults to `2025-12-11T00:00:00+00:00` and
  describes that as the XAUUSDT listing-date area.
- Binance public USDⓈ-M `exchangeInfo` confirms `XAUUSDT` has
  `onboardDate=2025-12-11T08:05:00+00:00`, `contractType=TRADIFI_PERPETUAL`,
  and `status=TRADING`.
- `outputs/clean_bars/` has 44 day directories, but current 1m research data
  lives in SQLite, not `outputs/clean_bars/GOLD_1m.json`.
- Historical `DirectionBiasGate` evidence is thin: market views exist only for
  a few dates, and direction-bias decisions begin around 2026-06-25. E2 must
  report both the retrospective sample size and prospective-logging status.
- R1 prompt described 21 strategies, but current `configs/strategy.yaml`
  contains 22 registered strategies. R1 scanned all 22 current strategies so no
  current registry entry was silently omitted.
- R1 5m replay resamples 1m SQLite bars with UTC epoch-floor 5-minute buckets,
  matching `MarketStore.load_aggregated_bars_between`: first open, max high,
  min low, last close, summed volume, bucket-start timestamp.
- Chan strategies expose `historical_signals`, but the engine docstring states
  that its fast historical path has mild lookahead versus the causal live
  detector. Because Strategy Lab §3 forbids lookahead, R1 marks chan variants
  `not_replayable` with `historical_signals_noncausal_lookahead_risk` instead
  of using those metrics.
- `gold_5m_v1` uses the legacy MA/macro `SignalEngine`, which has no
  deterministic `historical_signals` interface and depends on live event/macro
  context. R1 marks it `not_replayable` with `missing_historical_signals`.
- R1 technical-rule replay uses each engine's configured `min_bars` plus a
  conservative bounded causal rolling window for indicator warmup. This avoids
  full-history recomputation while keeping replay deterministic and aligned
  with live engines seeing finite candle buffers.
- R3 ran on a moving local SQLite backfill during the session; unfrozen reruns
  can change only `data_range` as fresh 1m bars arrive. Determinism was verified
  by freezing `--end 2026-07-05T02:22:00+00:00`; two fixed-input R3 runs
  produced the same scrubbed artifact hash
  `9f75b0c0053e926a06ee6a8a02a168702506c137ff587fd53773d7dcb037b4c9`.
- R3 recommended the R2 primary-label horizon from the selected gross/risk cell:
  `gold_5m_psych_level_rejection`, hold `8x`, stop/target scale `1.0x`, horizon
  `960` minutes.
- R2 features use causal rolling regime proxies (`causal_regime_vol`,
  `causal_regime_trend`) instead of precomputed E3 regime labels. Full-sample E3
  buckets would introduce avoidable lookahead risk into model features.
- R2 `gbt_depth3` is fixed to a light triage configuration
  (`n_estimators=24`, `max_depth=3`, `max_features="sqrt"`, `subsample=0.8`,
  seeded) because full calibrated GBT sweeps were the wall-clock bottleneck. The
  spec pins depth and determinism, not tree count.
- Historical chan live signal records reference
  `outputs/clean_bars/YYYY-MM-DD/GOLD_1m.json` source artifacts that are no
  longer present locally. R2 therefore reports chan parity as
  `source_artifacts_missing` instead of claiming a match from reconstructed
  SQLite bars.
- The chan engine's fast `historical_signals` path remains noncausal per its own
  docstring, and the true causal detector is too slow for the 296k-bar lab range.
  R2 registers all four chan-family addendum trials as `not_replayable` with an
  explicit bounded-replay reason rather than using the noncausal fast path.
- Owner decision on 2026-07-05 for R4: funding costs are excluded because the
  target venue will not be a perpetual and simplicity is preferred. R4 reports
  carry the required footnote: "Funding excluded (owner decision); revisit before
  real-money on any perpetual venue."
- Owner decision on 2026-07-05 for R4: pass/fail verdicts use a lab-scoped
  primary cost basis of `0.5` bp/side, while the Binance-maker reference basis of
  `2` bp/side is reported as secondary information only. Production
  `paper_execution_costs` remain untouched.
- R4 pins its evaluation input to the R3 candidate artifacts' shared
  `data_range.end` (`2026-07-05T02:22:00+00:00`) before reproducing anchors.
  This keeps the pre-registered R3 cells stable even as local SQLite backfill
  continues to receive newer 1m bars.
- R4 regime-slice gates reuse E3's deterministic 4h `volatility_bucket` labels
  as the fixed three-slice battery (`low`, `mid`, `high`), with a primary pass
  requiring non-negative expectancy in at least two of the three slices.
- R4 promoted `gold_5m_psych_level_rejection_swing` to lab-scoped
  `paper_eligible`; `gold_5m_ema50_position_swing` failed the pre-registered
  holdout expectancy criterion and is not paper-eligible.
- R4 adds disabled strategy config entries for the two swing variants. The
  current production runner ignores disabled entries, but the live/paper
  execution path does not yet express R4's 8x hold and scaled stop/target
  geometry as runtime exit semantics. Before any manual enablement, the minimal
  production change would be to add strategy-scoped exit-geometry support to the
  paper/live execution path and prove parity with the lab replay.

## Backfill Safety

The identified 1m backfill path writes market data to the local SQLite market
database and receipts under `outputs/binance_usdm_1m_backfill/`. It does not
submit, approve, close, cancel, or route orders.

## Promotion Gate

- Lab promotion can only mark a strategy as paper-eligible after both
  walk-forward and holdout objective results pass, and after holdout consumption
  is recorded.
- The promotion flag is lab-scoped. It does not mutate `configs/strategy.yaml`
  and does not enable paper, demo, or live execution.
- `strategy_leaderboard` may display a read-only `lab_expectation` block so
  forward-paper rows can be compared against lab evidence.
