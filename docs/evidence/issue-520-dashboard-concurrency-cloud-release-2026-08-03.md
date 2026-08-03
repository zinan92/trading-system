# Issue #520 / #522 — Dashboard concurrency Cloud release evidence

## Outcome

The Dashboard concurrency guard is deployed on the Cloud Paper host at exact
clean source SHA `7b37c116513a837f69c83344548a07c1360fe7f7` and tree SHA
`b554dc9552836602a523ce0ebf460cb91aa2be59`. This release did not invoke a
Paper control action and does not claim that the separate Supervisor blocker or
48-hour soak is complete.

## Root-cause evidence

The Linux kernel recorded an OOM kill at `2026-08-03 02:14:42 CST` in
`/system.slice/gridmind-dashboard.service`:

- victim: Dashboard Python process PID 27541;
- virtual memory: 1,993,664 KiB;
- anonymous RSS: 1,119,512 KiB;
- host memory: approximately 1.6 GiB;
- systemd result: `oom-kill`.

The same boot repeatedly recorded journald memory-pressure cache flushes, then
Cloudflare QUIC failures and SSH unavailability. This directly assigns the
resource-death chain to the Dashboard cgroup. Near-zero network throughput does
not disprove this mechanism: in-flight requests send only small request bytes
and do not emit their multi-megabyte responses until assembly completes.

## Released contract

- Issue #520 / PR #521: identical in-flight read-model requests share one
  computation; there is no TTL or completed-result cache. The HTTP server admits
  at most four request workers and queues excess connections before creating a
  worker thread.
- Issue #522 / PR #523: accepted strategy-console control requests advance the
  read-model generation both immediately before control and in `finally` after
  control. A post-control reload cannot join a pre-control or in-control flight.
- No safety gate, Paper execution path, strategy, order, position, live path, or
  credential path changed.

## Release gates

Before cutover, the new release ran Cloud Paper preflight as the `gridmind`
service user with the production systemd environment:

- status: `pass`;
- source SHA and tree: exact values above;
- Paper-only: true;
- control actions executed: 0;
- failed checks: none.

The source symlink was atomically moved to the new immutable release and only
the Dashboard was explicitly restarted. Its systemd dependency graph also
restarted the access gateway and Cloudflare tunnel; both returned active, and
cloudflared registered four QUIC connections with all connectivity prechecks
passing. Datafeed PID 866 did not change. The five timer unit hash and unit
definitions were not modified.

One natural live-tick trigger occurred after source cutover while the independent
AI provider readiness receipt still named the previous source SHA. The boot gate
correctly rejected it with `cloud_ai_provider_source_sha_mismatch`; no tick body
or control action ran. The side-effect-free provider smoke was then rerun from
the exact new SHA with `orders_allowed=false` and
`production_mutation_allowed=false`. The next natural timer trigger at
`2026-08-03 16:39:57 CST` passed the boot gate and completed normally. No boot
gate was bypassed and no tick was manually invoked.

## Production concurrency smoke

Four simultaneous loopback requests to
`/api/trading-system/read-model` produced:

| Evidence | Observed |
|---|---:|
| HTTP results | 4/4 HTTP 200 |
| Response sizes | 4/4 exactly 3,115,835 bytes |
| Completion times | 17.696–17.728 seconds |
| Dashboard threads | baseline 2, peak 6, final 2 |
| Dashboard RSS | baseline 130,000 KiB, peak 224,976 KiB, final 156,956 KiB |
| Dashboard CPU delta | 17 seconds |
| Batch wall time | 18 seconds |

The four matching responses completed together with one build-sized CPU cost.
The process stayed within the configured four request-worker bound, released all
workers, and did not reproduce the monotonic growth toward the historical
1.07 GiB OOM state. Deterministic tests separately count the underlying builder
calls and prove that N identical concurrent requests invoke it once.

## State preservation and availability

The pre- and post-release authoritative read-model facts matched:

- cycle: `2026-08-03_DAY`;
- strategy plan: active;
- runtime: stopped;
- accepted and open orders: 0;
- open positions: 0;
- execution reconciliation: ok, no issues;
- market: ready, fresh, trusted;
- start/stop/cancel calls during release: 0.

After release, Dashboard, access gateway, cloudflared, and datafeed were active;
all five Cloud timers were active. Public HTTPS returned the expected Cloudflare
Access `302` challenge rather than Cloudflare `530/1033`, proving that the tunnel
was reachable without bypassing Access.

## Remaining boundary

The current Supervisor observation remains
`blocked_structural/unknown_blocker` with zero control actions. The Dashboard P0
is resolved, but the strategy is still stopped for a separate fail-closed reason.
This evidence must not be used to claim trading recovery, 48-hour soak completion,
or the final running-rate acceptance criterion.
