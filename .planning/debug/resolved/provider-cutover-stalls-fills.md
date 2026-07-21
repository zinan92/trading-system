---
status: resolved
trigger: "用户确认修复 datafeed 更新后挂单已接受但不成交，并执行安全历史事件回放。"
created: 2026-07-15T14:36:31Z
updated: 2026-07-15T15:01:00Z
---

## Symptoms

- Expected: trusted Binance USD-M 1m bars touch accepted grid orders, producing fills, positions, protective exits, and PnL.
- Actual: all 51 current-cycle limit orders remain `accepted`; fills and trades are absent.
- Error: every scheduled live tick skips execution with `market_provider_mismatch`.
- Timeline: started after the production market-data boundary moved to datafeed.
- Reproduction: start the production paper robot, then let a trusted bar cross the 4060.1089 buy limit.

## Current Focus

- hypothesis: confirmed — `configs/dualtrack.yaml` expected `binance_usdm` while datafeed emitted `binance_usdm_futures`.
- test: production provider contract plus an event-driven accepted-limit fill/target/idempotency regression.
- expecting: exact datafeed provider passes fail-closed validation; replay fills once and reconciles exactly.
- next_action: complete — full regression and visual proof are retained.
- reasoning_checkpoint: fixed without alias fallback; broker execution identity remains separately named `binance_usdm`.
- tdd_checkpoint: passed — the provider contract failed before the fix; 74 focused tests and the full 1475-test repository regression passed afterward.

## Evidence

- timestamp: 2026-07-15T14:36:31Z
  observation: current cycle has 51 accepted orders and no fills/trades files.
- timestamp: 2026-07-15T14:36:31Z
  observation: production live-tick log repeatedly reports `market_provider_mismatch`.
- timestamp: 2026-07-15T14:36:31Z
  observation: datafeed emits `binance_usdm_futures`; DualTrack expects `binance_usdm`.
- timestamp: 2026-07-15T14:38:29Z
  observation: controlled replay filled 4060.1089 and closed at target 4067.9911; reconciliation status is `ok`.
- timestamp: 2026-07-15T14:40:00Z
  observation: a second replay processed 85 events without changing orders, fills, trades, or account hashes.
- timestamp: 2026-07-15T14:44:17Z
  observation: dashboard shows running, 50 pending orders, the 21:59 Beijing target, zero open positions, and trusted datafeed health.
- timestamp: 2026-07-15T15:01:00Z
  observation: full repository regression completed with 1475 passed in 547.71 seconds.

## Eliminated

- hypothesis: scheduler is stopped.
  reason: LaunchAgent has 3027 runs and last exit code 0.
- hypothesis: price never touched an accepted order.
  reason: the 21:25 Beijing bar low was 4059.86, below the accepted 4060.1089 buy limit.

## Resolution

- root_cause: production market-provider expectation drifted from the canonical datafeed GOLD route after the datafeed cutover.
- fix: align DualTrack's production/default market provider with `binance_usdm_futures` while preserving exact fail-closed matching and distinct broker identity.
- verification: 4060.1089 entry plus 4067.9911 target recorded once; net current-cycle trade PnL +1.32861937 USD; reconciliation clean; scheduler restored with exit code 0; dashboard visually verified; full repository regression 1475 passed.
- files_changed: configs/dualtrack.yaml, services/dualtrack_config.py, tests/test_dualtrack_provider_contract.py, tests/test_dualtrack_dt8_cycle_runner.py, tests/test_dashboard_dualtrack_static.py, decision-log.md
