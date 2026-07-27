# Cloud always-on Paper and daily self-review plan

Status: approved product direction; this document is the implementation
contract for GitHub milestone `Cloud Always-On Paper + Daily Self-Review`.

## Outcome

The Paper Grid/DCA system keeps collecting trusted market data, advancing
Nautilus Paper lifecycles, reconciling execution, serving the Dashboard, and
writing one daily self-review when Park's Mac is shut down.

This is not a live-money migration. Cloud deployment must preserve
`execution_mode=paper`, `live_trading_enabled=false`, and
`real_money_eligible=false`.

## Current verified baseline

| Concern | Current owner | Cloud implication |
|---|---|---|
| Dashboard | `pipelines.dashboard_server`, launchd KeepAlive, port 8765 | replace launchd with a Linux service; keep the existing HTTP/read-model contract |
| Market data | independent `/Users/wendy/datafeed`, FastAPI on port 8100, SQLite owner storage | install the datafeed package as its own service and persist its database |
| Paper execution | `pipelines.dualtrack_cycle_runner --event live-tick` every 60 seconds | use a non-overlapping Linux timer; a failed tick writes no healthy heartbeat |
| Matching | Nautilus Paper in a dedicated Python runtime | build and probe a dedicated cloud runtime before any Paper start |
| State | `outputs/`, datafeed SQLite, and immutable lifecycle/cycle packages | place on persistent paths, back up atomically, and prove restore |
| Secrets | ignored `configs/live.env` plus optional delivery URLs | create on the host with mode 0600; never copy values through Git |
| Remote access | local Dashboard plus optional Cloudflare tunnel | run a dedicated tunnel/reverse proxy; Dashboard remains non-public without access control |
| Existing reporting | 24-hour terminal P&L report at 01:03 Beijing time | retain it as execution evidence, not as the complete self-review |
| Existing daily review | broad legacy pipeline, excluded by `dualtrack_focus` | do not enable the legacy full-profile job; add a focused DualTrack review |
| Planner dependencies | local Codex CLI and a local newsletter path | optional in cloud phase 1; absence must be explicit and must not break Paper execution |

The July 27 reboot reproduced a Mac-only launchd LWCR failure. A source-gated
single-service rebootstrap restored ticks, but that is a local bridge, not
continuous hosting.

## Target topology

Use one small Linux VM first, with one Unix user and these independently
supervised units:

1. `gridmind-datafeed.service`: loopback-only port 8100.
2. `gridmind-dashboard.service`: loopback-only port 8765.
3. `gridmind-live-tick.service` + `.timer`: one tick per minute,
   `Persistent=true`, no overlapping invocation.
4. `gridmind-daily-24h.service` + `.timer`: existing terminal execution report.
5. `gridmind-daily-self-review.service` + `.timer`: one prior-Beijing-day
   review after terminal cycle evidence is available.
6. `gridmind-backup.service` + `.timer`: atomic snapshot and retention.
7. `cloudflared.service` (or an equivalent authenticated reverse proxy):
   remote Dashboard access without exposing ports 8100/8765 directly.

Persistent state lives outside the Git checkout:

```text
/var/lib/gridmind/outputs/
/var/lib/gridmind/datafeed/
/var/lib/gridmind/backups/
/etc/gridmind/paper.env
```

The checkout is immutable at a deployed SHA. Development and deployed trees
must be separate. Boot and restart require a release receipt bound to that SHA
and clean tracked tree. The Mac and cloud schedulers must never both own the
same Paper namespace.

## Operational and safety invariants

- Paper-only flags are asserted at install, boot, health check, and read model.
- The datafeed and Dashboard bind only to loopback.
- The live-tick timer may run only when datafeed health, Nautilus imports,
  writable persistence, current deployed SHA, and execution reconciliation
  pass.
- A missing/failed tick never writes a healthy heartbeat and freezes new
  Grid/DCA starts using the existing 180-second gate.
- Service recovery may restart a service. It may not start a strategy, replay
  an uncertain control command, alter parameters, cancel orders, flatten
  positions, or promote a Shadow.
- Backup/restore preserves order, fill, position, StrategyPlan, lifecycle,
  cycle-package, reconciliation, and review identities.
- A deployment has exactly one scheduler owner. Cutover uses
  `local_only -> paused -> cloud_only`; there is no `local_and_cloud` state.
- Secret values never enter GitHub, receipts, logs, screenshots, or backups
  stored without encryption.

## Daily self-review contract

The focused review is deterministic and evidence-backed. An optional AI
adapter may summarize it later, but missing AI credentials cannot prevent the
base artifact.

For the prior Beijing calendar day it writes immutable JSON and readable
Markdown containing:

1. `operational_truth`: uptime/tick coverage, datafeed freshness and failures,
   service restarts, and unresolved incidents.
2. `execution_truth`: strategies active, orders/fills/round trips, realised
   and unrealised P&L, fees, reconciliation, and residue.
3. `strategy_truth`: Grid/DCA direction and parameters, profit/risk outcomes,
   lifecycle completion, and comparable Shadow evidence.
4. `went_well`: evidence-linked facts only.
5. `went_poorly`: evidence-linked failures, losses, missing samples, and
   unknowns; no silent conversion of missing data to zero.
6. `tomorrow_actions`: ranked actions with owner, reason, expected evidence,
   and permission class (`automatic_service_recovery`,
   `human_strategy_decision`, or `park_approved_live_only`).
7. `evidence`: artifact paths, hashes, deployed SHA, window, schema version,
   and generation timestamp.

The review may recommend a strategy experiment or Shadow comparison. It may
not change StrategyPlan, leverage, risk policy, orders, positions, or the main
strategy.

## Initial provider choice

Start with an AWS Lightsail Linux VM in Singapore if the account is available:
2 vCPU, 2 GB RAM, 60 GB SSD, public IPv4 (currently listed at USD 12/month).
The initial hourly preflight must verify:

- Ubuntu/Python/Nautilus installation and imports;
- outbound HTTPS and DNS;
- Binance USD-M public instrument, latest-candle, and historical-candle routes;
- sustained datafeed and live-tick memory below 70% of host RAM;
- remote access and backup upload.

If memory exceeds 70%, use the 4 GB plan before cutover. If execution-venue
routes are unavailable from that region, stop before migration and test the
next provider/region; do not introduce a synthetic or non-execution fallback.

The provider remains replaceable because services consume loopback ports and
persistent paths rather than provider APIs. Official price references:

- https://aws.amazon.com/lightsail/pricing/
- https://docs.aws.amazon.com/lightsail/latest/userguide/amazon-lightsail-bundles.html
- https://docs.hetzner.com/general/infrastructure-and-availability/price-adjustment/

## Implementation issues and dependency order

### Cloud M1 — Portable runtime and preflight

Outcome: the stack can be installed and validated on Linux without starting a
strategy.

Acceptance:

- portable config removes Mac absolute runtime/datafeed paths;
- a dependency manifest builds the Dashboard and dedicated Nautilus runtime;
- preflight emits pass/blocked JSON for OS, SHA, Paper flags, datafeed,
  persistence, ports, Nautilus imports, and upstream venue access;
- focused tests cover success and each blocking dependency;
- no Paper control action occurs.

### Cloud M2 — Linux services and timers

Outcome: systemd owns datafeed, Dashboard, minute ticks, terminal report, and
dead-man health.

Acceptance:

- units use least privilege, loopback binding, persistent state, bounded
  restart policy, non-overlapping timers, and explicit logs;
- boot/restart consumes the source-bound release gate;
- tick failure remains classified and never produces a healthy heartbeat;
- installer supports dry-run and uninstall without deleting persistent data;
- service/timer contract tests pass.

### Cloud M3 — Focused daily self-review

Outcome: one prior-Beijing-day JSON/Markdown review implements the schema
above.

Acceptance:

- terminal evidence and missing-data behavior are deterministic;
- positive, loss-day, no-trade, stale-tick, and reconciliation-failure fixtures
  produce correct `went_well`, `went_poorly`, and ranked actions;
- automatic actions are restricted to service/display recovery;
- files are hashed and idempotent for the same evidence revision;
- the timer schedule and read-model link are tested.

### Cloud M4 — Persistence, backup, restore, and split-brain guard

Outcome: a host can fail without losing Paper identity or creating two owners.

Acceptance:

- atomic backup covers both owner databases and immutable execution artifacts;
- retention and encryption configuration are explicit;
- restore into a clean temporary root reproduces hashes and reconciliation;
- scheduler ownership lease blocks a second host;
- cutover/rollback never runs local and cloud ticks concurrently.

### Cloud M5 — Remote observability and operator access

Outcome: Park can see current cloud truth and receives a clear incident signal.

Acceptance:

- authenticated Dashboard URL works while ports remain loopback-only;
- health separates delivery, data freshness, execution, reconciliation,
  review generation, and backup;
- dead-man check detects stale ticks and a missed daily review;
- alerts contain failure stage, evidence, and next action without secrets;
- browser acceptance verifies Dashboard, runtime card, report, and review.

### Cloud M6 — Provision, deploy, cut over, and soak

Outcome: the cloud host is the only Paper scheduler and survives Park's Mac
being offline.

Acceptance:

- provider/region preflight passes before state cutover;
- current Paper is stopped, 0 accepted orders, 0 positions, and reconciled
  before copying state;
- deployed SHA, service state, and scheduler ownership are receipted;
- 24-hour soak includes at least 60 consecutive successful ticks per hour,
  one terminal 24-hour report, one daily self-review, one backup, and one
  tested service auto-recovery;
- the Mac can be shut down while cloud health and artifacts continue advancing;
- rollback returns to one scheduler owner with no order/position residue.

## Execution sequence

`M1 -> M2 -> M3 -> M4 -> M5 -> M6`.

M3 implementation may run in parallel with M2 after M1 defines the portable
paths. M6 is the only phase that requires Park at an account, identity,
payment, CAPTCHA, or provider-agreement boundary. Creating the VM is a
reversible Paper infrastructure action, but the payment/login interaction must
remain user-controlled.

## Definition of done

This milestone is complete only when the cloud, not the Mac, advances Paper
ticks for a full 24-hour soak; the Dashboard is remotely usable; execution and
reconciliation remain authoritative; backup/restore evidence passes; and one
daily self-review records what worked, what failed, and tomorrow's actions.

