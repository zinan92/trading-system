# Cloud OOM containment release — 2026-08-11

## Outcome

- Cloud Paper is running on exact `main@9856f83fccada01fcde3462506d3da4429ca716a`
  (`tree adb55f8ce847e738be052c5b890747a9ca78ac34`).
- `ssh.service` and `gridmind-cloudflared.service` both load
  `OOMScoreAdjust=-900`; a fresh SSH connection succeeded after restarting the
  loaded Ubuntu `ssh.service`.
- Managed Python units load bounded `MemoryMax` values and external
  `OnFailure` delivery.  The production-calibrated datafeed limit is
  805,306,368 bytes (768 MiB), above its pre-release peak of 652,828,672 bytes.
- At 23:06:32 CST the authoritative read-model reported NIGHT runtime
  `running`, 38 accepted/open orders, zero unknown orders, zero open positions,
  execution reconciliation `ok`, trusted/fresh market data, and healthy Cloud
  health with no critical or warning incidents.

## OOM attribution

Persistent kernel/auth/service journals identify all three large Python OOM
victims as interactive root session scopes, not `gridmind-*.service` cgroups.
The 2026-08-11 victim was `sudo /usr/bin/python3 -` in
`session-3051.scope`, left alive from 14:49 until the 18:12 global OOM with
538,752 KiB anonymous RSS.  The earlier victims reached 374,536 KiB and
430,512 KiB.  Scheduled backup had completed at 09:31:49 and was not running at
the OOM.  The two production whole-file hash sites were nevertheless a latent
copy of the same allocation pattern and are now streamed by #612.

## Release gates and mutation window

- Exact-SHA Cloud preflight: pass, Paper-only, zero control actions, source
  SHA/tree clean and exact.
- AI provider readiness: pass at the same SHA/tree, one secret-free receipt,
  zero production mutation authority.
- Paper predeploy gate: pass at 23:02:50 CST, expiring 23:17:50 CST.
- Atomic apply unit: 23:03:49–23:03:53 CST.  Seven timers were stopped only
  around receipt/source/systemd replacement and restored by an exit trap.
- Dashboard, access-gateway and live-tick boot receipts passed at the new
  SHA/tree.  All seven timers and datafeed/dashboard/access/cloudflared/SSH
  were active after release.

The pre-release state and post-release state matched on cycle, runtime,
desired state, plan identity, accepted/open/unknown order counts, open
positions, last strategy action, and reconciliation: NIGHT, running/running,
`strategy-plan-2026-08-11_NIGHT-4-7a704e5c`, 38/38/0, zero positions,
last action `start`, and reconciliation `ok`.  No release-time strategy
start/stop/cancel/flatten request was issued.

## Interruption and alert evidence

The final successful pre-release tick ended at 23:02:45 CST.  A tick already
in flight and the first immediate post-restart tick encountered datafeed
connection refusal while uvicorn was becoming ready; the new `OnFailure`
handler durably recorded the failures and delivered HTTP 200 to the external
dead-man fail endpoint.  The next natural tick started at 23:04:55 and
completed successfully at 23:05:04.  Successful-tick start gap was 130 seconds
(completion gap 139 seconds), runtime/order state stayed resolved, and operator
strategy controls remained zero.

A subsequent natural dead-man run completed at 23:06:18 with normal severity.
It regenerated Cloud health at the new SHA with no critical/warning incidents.

## Isolated canaries

- Streaming hash: the deployed service-user code hashed a sparse 768 MiB file
  under a transient `MemoryMax=64M` cgroup.  It completed in 3.697 seconds with
  1.0 MiB memory peak and digest
  `d8492a624b5ded59e8a2185b0755f195a58642456e8387ba2817e46f1e05b358`.
- Failure alert: a service-user canary wrote only to
  `/tmp/gridmind-611-alert-canary-isolated.radLZC`, durably recorded detected
  and delivery-result phases, and received HTTP 200 from the external fail
  endpoint.  Its event explicitly records Paper-only, zero controls, zero
  orders, zero position changes, and no secrets.

One preceding canary command inherited the production output root from the
systemd environment file and therefore appended a truthful canary event to the
immutable production failure journal before the isolated rerun.  It did not
alter controls, orders, positions, fills, trades, or cycle packages.  The event
was not deleted or rewritten.

## Remaining observation gates

- The next natural daily self-review and backup runs on the new SHA are still
  pending at 09:10 and 09:30 CST.  The large-file service-user canary proves the
  bounded hashing implementation but does not replace those scheduled runs.
- Datafeed `/api/health` still performs synchronous full SQLite
  `integrity_check`; observed latency was 9.40–16.49 seconds and twice exceeded
  the canonical preflight's 10-second budget.  A warm-cache canonical run
  passed without timeout relaxation.  Durable remediation is tracked in
  [datafeed #6](https://github.com/zinan92/datafeed/issues/6); no release gate
  was bypassed.
- Cross-workload admission/global batch budgeting remains #613, and the
  single-host failure domain remains #614.
