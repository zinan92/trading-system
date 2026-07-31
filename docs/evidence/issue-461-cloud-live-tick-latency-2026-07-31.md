# Issue #461 — Cloud Paper live-tick latency evidence

Date: 2026-07-31 (Asia/Shanghai)

## Verdict

Use the existing natural `gridmind-live-tick.service` process for Paper
Supervisor convergence. Do not add an independent Supervisor timer.
This is approved timing option (c): compress the AI provider sub-budget from
the existing 30 seconds to 25 seconds, keep the whole Supervisor attempt at
45 seconds, and keep the systemd deadline at 55 seconds.

The measured non-Supervisor tick work fits both the 10-second allowance and
the current 55-second systemd deadline:

- proposed Supervisor attempt budget: 45 seconds;
- selected AI provider sub-budget: 25 seconds;
- measured non-Supervisor p99: 1.785 seconds;
- measured non-Supervisor max: 2.610 seconds;
- p99 headroom inside the 10-second allowance: 8.215 seconds;
- max headroom inside the 10-second allowance: 7.390 seconds.

The later integration must keep the current ordering: lifecycle, protective
sweep, plan sync, intraday work, ledger rebuild and a successfully persisted
complete tick heartbeat all finish before Supervisor convergence begins.

## Authority and sample

The report was built from append-only timing receipts copied read-only from:

`/var/lib/gridmind/outputs/dualtrack/observability/live_tick_timing/`

All 1,281 rows passed the source-bound homogeneous-sample validator:

| Field | Evidence |
| --- | --- |
| Window | `2026-07-30T04:57:23.453569Z` → `2026-07-31T03:23:50.852869Z` |
| Runtime | `cloud` |
| Host | `iZt4nfeioqj4mzyjvn4vruZ` |
| Scheduler owner | `cloud-primary`, epoch `3` |
| Source SHA | `742c49fd1079c5a70ea710d9ba781f36483cf8bb` |
| Source tree SHA | `76934be827252b30abb1f63ded96ab58de059b1b` |
| Source tree | clean |
| Receipt status | 1,281 `success`; no failed row in the sample |
| Timing instrumentation control actions | `0` in every row |
| Successful tick coverage | `98.0107%` |

Input and output hashes:

| Artifact | SHA-256 |
| --- | --- |
| `2026-07-30.jsonl` (1,087 rows) | `bf8f6629680506b17bb0989e6f435c0c735dfac9ee1acb2c337d1338c4313677` |
| `2026-07-31.jsonl` (194 rows) | `bb54333f4035ce85b6e90e481fb0b36be5678d81275886186324df89a727322e` |
| Generated report | `a60c5b3f25206fe836e641523c3a5f8e3b53f4dbd070534e8491b772d18c48c0` |

The raw JSONL is not committed because the Cloud append-only source remains
authoritative. The hashes above bind this decision to the exact copied bytes.

## Real cycle boundaries

The report verifies each receipt's cycle identity against its UTC timestamp
using the production Beijing cycle clock. It observed two natural boundaries:

| Boundary | Last old-cycle tick | First new-cycle tick |
| --- | --- | --- |
| `2026-07-30_DAY → 2026-07-30_NIGHT` | `2026-07-30 20:59:06.569966 CST` | `2026-07-30 21:00:08.469597 CST` |
| `2026-07-30_NIGHT → 2026-07-31_DAY` | `2026-07-31 08:59:27.450487 CST` | `2026-07-31 09:00:28.433426 CST` |

Both new cycles were naturally observed well inside the five-minute boundary
requirement. No fixture or synthetic rollover event was used.

## Measured latency

All values are milliseconds.

| Work | p50 | p95 | p99 | max |
| --- | ---: | ---: | ---: | ---: |
| Total natural live tick | 1,828.730 | 2,091.336 | 2,138.324 | 32,658.769 |
| Non-Supervisor work | 1,484.200 | 1,742.241 | 1,784.965 | 2,610.116 |
| Lifecycle | 1,001.002 | 1,231.766 | 1,266.420 | 1,816.411 |
| Protective sweep | 1.244 | 1.412 | 1.645 | 3.003 |
| Plan sync | 62.347 | 90.243 | 186.979 | 214.350 |
| Intraday | 16.689 | 27.192 | 28.236 | 41.408 |
| Ledger | 395.163 | 406.117 | 414.563 | 756.693 |
| Heartbeat persistence | 2.717 | 4.361 | 4.689 | 5.322 |
| Existing cycle decision | 344.814 | 352.035 | 359.312 | 31,242.964 |

`non_supervisor` is computed per receipt as total duration minus the existing
`cycle_decision` phase. This matches the approved question: whether market,
lifecycle, protective sweep, plan sync, intraday, ledger and heartbeat work
fit in the 10 seconds left by a 45-second Supervisor budget.

## Timer contract

The deployed unit was read with `systemctl cat/show`:

- `gridmind-live-tick.service`: `Type=oneshot`;
- `TimeoutStartSec=55`;
- `gridmind-live-tick.timer`: `OnUnitInactiveSec=60`, `AccuracySec=1`;
- timer state at evidence collection: `active (waiting)`;
- most recent service result: `success`.

The observed inactive gap was p50 `61.204s`, p95 `61.862s`, p99 `61.910s`,
max `62.208s`. This proves the timer is non-overlapping and schedules from
unit inactivity; it is not a fixed 60-second start-to-start cadence.

## Safety boundary

This evidence selects timing topology only. It does not:

- deploy Supervisor code;
- authorize an AI policy or risk envelope;
- create, cancel or modify an order or position;
- alter any existing market, heartbeat, reconciliation, immutable-fill,
  human-confirmation, release-SHA or boot gate;
- prove the final 48-hour utilization acceptance.

The final completion claim still requires the separate continuous 48-hour
window, at least 85% conservative strategy utilization, three real cycle
boundaries including one 21:00 boundary, and one real transient automatic
recovery audit chain.
