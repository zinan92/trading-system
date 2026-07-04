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
