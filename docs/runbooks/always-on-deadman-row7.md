# Row 7 Always-On Heartbeat And External Dead-Man

Status: testnet/mainnet-canary readiness only. This runbook does not enable mainnet trading.

## What This Proves

- Critical local jobs (`runner`, `strategies`) are judged at read time from UTC heartbeat age. A stale critical heartbeat becomes `BLOCKED_ALWAYS_ON_STALE`.
- Non-critical observability surfaces (`cycle_audit`, dashboard surface) can degrade without blocking new signals. Canonical state becomes `DEGRADED`.
- The external dead-man ping is out-of-box: if this laptop sleeps, loses power, or all local processes die, pings stop and the external service alerts.
- Position-aware pings are supported. With an exchange position open, the ping severity is `critical` and can use a separate URL.
- A success ping is only sent when critical always-on liveness is healthy. If `always_on.status` is `BLOCKED_*`, the job pings the Healthchecks `/fail` endpoint instead of sending a green ping.
- Exposure severity is fail-safe. If live reconciliation is stale, missing, or has an invalid/future timestamp, the ping treats position state as unknown and uses `critical` severity.

## Configure External Ping

Create two healthchecks, or one if you only want a single alert path:

```bash
export TRADING_ORCHESTRATOR_DEADMAN_URL="https://hc-ping.com/<flat-or-default-check>"
export TRADING_ORCHESTRATOR_DEADMAN_POSITION_URL="https://hc-ping.com/<open-position-check>"
```

Set the external service timeout above the local cadence. The local launchd job pings every 300 seconds, so configure the external grace window for at least 10-15 minutes.

Regenerate and install launchd jobs after setting the variables:

```bash
python3 -m pipelines.schedule --json
python3 -m pipelines.schedule_install --json
python3 -m pipelines.schedule_status --json
```

The generated plist embeds `TZ=UTC`. If the URL variables are present when the schedule is generated, the dead-man plist embeds them too.

## Manual Smoke Test

Dry run without sending:

```bash
python3 -m pipelines.deadman_ping --dry-run --json
```

Send one ping:

```bash
python3 -m pipelines.deadman_ping --json
```

Artifact to inspect:

```bash
cat outputs/deadman_ping/current.json
```

Expected fields:

- `status`: `sent`, `dry_run`, or `not_configured`.
- `ping.failure_signal`: true when the job sent or would send the Healthchecks `/fail` endpoint because critical liveness is blocked.
- `severity`: `normal` when exchange is flat, `critical` when `live_reconciliation.current` shows an exchange position or suspected naked position.
- `exposure.position_unknown`: true when reconciliation is not fresh enough to prove flat; this is treated as critical.
- `always_on.status`: `READY`, `DEGRADED`, or `BLOCKED_ALWAYS_ON_STALE`.
- `exposure.has_open_position`: true only when exchange truth indicates exposure.

## Canonical State Mapping

- `runner` stale or missing: `BLOCKED_ALWAYS_ON_STALE`, new entries blocked.
- `strategies` stale or wrong UTC run date: `BLOCKED_ALWAYS_ON_STALE`, new entries blocked.
- Reconciliation unknown/drift/naked: existing reconciliation and live money guardrail gates own the block. Do not create a second staleness rule that can disagree.
- `cycle_audit` stale or missing: `DEGRADED`, new entries allowed, observability needs review.
- Dashboard availability: checked by the reader/external probe, not by trusting a self-written dashboard status.

## Operator Notes

- The local checker cannot detect whole-machine death. The external service must alert when pings stop.
- Machine dead plus flat means missed trading. Machine dead plus open position is exposure; use the position URL for louder paging.
- Server-side protective orders still protect the position while the laptop is dead, but the operator still needs to know the machine is not supervising.
