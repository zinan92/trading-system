# Issue #532 — Cloud dead-man liveness authority release

## Outcome

Issue #532 is deployed to the Cloud Paper host at clean source SHA
`94b14bff38f5f965f93237959abb02473437bdc3` and tree SHA
`f864b93fa379b58dc89321e15c2ff9682012bfad`.  A natural systemd timer run
used Cloud-native health as its liveness authority and delivered a successful
external dead-man ping.  The retired local runner heartbeat remained visible
for diagnosis but no longer created a false `/fail` signal.

## Release and boot evidence

- PR #533 merged at `2026-08-03T10:17:23Z`.
- The exact candidate Cloud Paper preflight returned `pass`,
  `paper_only=true`, `control_actions_executed=0`, a clean tracked tree, and
  the SHA/tree above.
- Candidate AI provider readiness returned `pass` at the same SHA/tree and did
  not authorize orders or production mutation.
- Dashboard and dead-man boot receipts returned `pass` and bound the same
  SHA/tree.  No boot/SHA gate was bypassed.
- Dashboard, authenticated access gateway, cloudflared, datafeed, and all five
  Cloud timers remained active.

## Natural end-to-end delivery

No manual dead-man command or timer trigger was issued.  The existing
`gridmind-deadman-ping.timer` fired naturally, and its receipt at
`2026-08-03T10:23:34Z` recorded:

- `status=sent`;
- `delivered=true`, HTTP status 200;
- `target_kind=success`;
- `failure_signal=false`, `success_ping=true`;
- `liveness_authority=cloud_health`;
- `cloud_health_authoritative=true`;
- `legacy_always_on_applicable=false`;
- `failure_signal_sources=[]`.

Cloud health was `degraded/warning` only for
`daily_self_review_missing_or_incomplete` and contained zero critical
incidents.  The legacy always-on projection still reported
`BLOCKED_ALWAYS_ON_STALE` because the Cloud host does not produce the retired
local `runner_status/current.json`; retaining that row proves the fix changed
routing authority rather than rewriting evidence.

## Trading-state preservation

The authoritative read-model after delivery remained:

- cycle `2026-08-03_DAY`;
- runtime `running/running`;
- 38 accepted orders with 38 unique order IDs;
- zero open positions;
- Nautilus execution reconciliation `ok`;
- trusted market ready and fresh;
- Supervisor `healthy/adopted_existing`;
- exactly one start intent for the cycle.

The release did not issue a strategy control, create or cancel an order, alter
a position, or touch a live/real-money path.

## Remaining acceptance boundary

Dead-man warning routing now has real external delivery evidence.  The larger
Supervisor objective still requires the continuous 48-hour conservative
running-rate result of at least 85 percent and the required real cycle
boundaries before it can be called complete.
