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

- Current local `data/market_data.db` contains `GOLD` `1m` bars from
  `2025-12-11T08:05:00+00:00` through `2026-07-04T14:54:00+00:00`, which is
  less than the spec acceptance requirement of at least 12 months.
- `pipelines/backfill_gold_1m.py` defaults to `2025-12-11T00:00:00+00:00` and
  describes that as the XAUUSDT listing-date area. If Binance cannot provide
  earlier XAUUSDT 1m bars, acceptance criterion 6.4 remains blocked unless a
  longer approved data source is added.
- Binance public USDⓈ-M `exchangeInfo` confirms `XAUUSDT` has
  `onboardDate=2025-12-11T08:05:00+00:00`, `contractType=TRADIFI_PERPETUAL`,
  and `status=TRADING`.
- `outputs/clean_bars/` has 44 day directories, but current 1m research data
  lives in SQLite, not `outputs/clean_bars/GOLD_1m.json`.
- Historical `DirectionBiasGate` evidence is thin: market views exist only for
  a few dates, and direction-bias decisions begin around 2026-06-25. E2 must
  report both the retrospective sample size and prospective-logging status.

## Backfill Safety

The identified 1m backfill path writes market data to the local SQLite market
database and receipts under `outputs/binance_usdm_1m_backfill/`. It does not
submit, approve, close, cancel, or route orders.
