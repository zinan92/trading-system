# Cloud unit memory calibration — 2026-08-12

## Measurement contract

- Host: the 1.6 GiB Alibaba Cloud Paper VM.
- Source under measurement: `17f8d6258918cf640f03a782a01d3cbe5fe6ab1c`.
- Authority: cgroup v2 `memory.current` sampled at 20–50 ms, or the retained
  systemd cgroup `MemoryPeak` for long-running services. Process RSS is not used
  to size `MemoryMax`.
- Natural samples used the real Paper service and timer. Isolated canaries used
  the deployed service user and source, wrote only below
  `/var/lib/gridmind/measurements`, and made no strategy control, order, position,
  or production-output change.
- `MemoryHigh` is a reclaim/throttle boundary. `MemoryMax` is the hard kill
  boundary and therefore retains additional headroom.

## Results and final limits

| Unit | Sample | Cgroup peak bytes | Peak MiB | Final `MemoryHigh` | Final `MemoryMax` | Basis |
|---|---|---:|---:|---:|---:|---|
| `gridmind-datafeed.service` | Natural process lifetime through 2026-08-12 10:13 CST | 657,870,848 | 627.39 | — | 768 MiB | 22% hard-limit headroom over the observed lifetime peak. |
| `gridmind-dashboard.service` | Natural process lifetime through 2026-08-12 10:13 CST | 114,581,504 | 109.27 | — | 256 MiB | More than 2x the observed peak. |
| `gridmind-access-gateway.service` | Natural process lifetime through 2026-08-12 10:13 CST | 15,634,432 | 14.91 | — | 96 MiB | More than 6x the observed peak. |
| `gridmind-cloudflared.service` | Natural process lifetime through 2026-08-12 10:13 CST | 42,397,696 | 40.43 | — | 128 MiB | More than 3x the observed peak; OOM repair priority remains `-900`. |
| `gridmind-live-tick.service` | Natural running Paper ticks, 10:22–10:28 CST | 445,034,496 | 424.42 | 384 MiB | 512 MiB | The old 384 MiB hard cap was reached. The calibrated shape throttles above 384 MiB and preserves about 21% hard-limit headroom. |
| `gridmind-daily-24h.service` | Isolated production-evidence report build | 45,789,184 | 43.67 | — | 128 MiB | 2.9x the report-builder peak. Delivery configuration is tracked separately in #628. |
| `gridmind-daily-self-review.service` | Isolated 2026-08-11 evidence review | 51,535,872 | 49.15 | — | 128 MiB | 2.6x the observed peak. |
| `gridmind-backup.service` | Isolated 512 MiB production-shape file plus SQLite, streaming hash | 268,931,072 | 256.47 | 256 MiB | 384 MiB | Completed under pressure without OOM; hard limit retains about 50% over the reclaim boundary. |
| `gridmind-deadman-ping.service` | Natural post-fix cycle, 10:22–10:28 CST | 70,430,720 | 67.17 | — | 192 MiB | 2.8x the post-fix peak and covers the late-cycle canary. |
| `gridmind-deadman-watchdog.service` | Natural one-minute runs, 10:22–10:28 CST | 16,674,816 | 15.90 | — | 64 MiB | 4x the observed peak. |
| `gridmind-ai-provider-readiness.service` | Natural failed-provider refresh, 10:22–10:28 CST | 11,808,768 | 11.26 | — | 128 MiB | Includes the real provider subprocess attempt; generous provider-variation headroom. |
| `gridmind-next-cycle-plan.service` | Natural due-path generation, 2026-08-13 08:04 CST | 134,217,728 | 128.00 | 192 MiB | 256 MiB | The old hard limit was reached and killed Python with about 145.5 MiB resident (130,444 KiB anonymous plus 18,560 KiB file RSS). The new hard limit is 2x the observed cgroup ceiling and retains `OnFailure` alerting. |
| `gridmind-unit-failure-alert@.service` | Isolated unconfigured-delivery failure event | 12,595,200 | 12.01 | — | 64 MiB | More than 5x the observed peak; the canary wrote only isolated evidence. |

`ssh.service` is not a GridMind Python unit. Its measured process-lifetime cgroup
peak was 13,778,944 bytes (13.14 MiB); it remains outside the administrator user
slice and retains `OOMScoreAdjust=-900` rather than a guessed hard cap.

## Administrator-session containment

Before containment, `user-1000.slice` had `MemoryHigh=infinity` and
`MemoryMax=infinity`. Its observed peak was 43,487,232 bytes before the change
and 59,932,672 bytes while the restricted deployment/measurement sessions were
active. The source-controlled template now applies:

```ini
[Slice]
MemoryAccounting=true
MemoryHigh=384M
MemoryMax=512M
```

The limit applies to the administrator login slice, not `ssh.service`,
`gridmind-cloudflared.service`, or the system GridMind services. Existing and
fresh restricted SSH sessions were verified after applying the runtime form of
the same limit.

The independent dead-man watchdog also snapshots the user-slice
`memory.events` counters. A new `high`, `oom`, or `oom_kill` increment creates a
structured event and signals the external dead-man fail endpoint; the first
observation is a baseline, and repeated checks do not create an alert loop.
