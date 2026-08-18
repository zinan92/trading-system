# Issue #742 — Current Strategy projection release evidence

Date: 2026-08-18 (Asia/Shanghai)

## Delivery identity

- Issue: [#742](https://github.com/zinan92/trading-system/issues/742)
- PR: [#751](https://github.com/zinan92/trading-system/pull/751)
- Deployed `main` SHA: `ebdbdd7aeb5ee805870d2a996bfa7fef1d0a5b5d`
- Deployed source tree SHA: `2d26fb42dbf1b6d1f85a7b19413200e1157301b7`
- Public target: `https://goldbot.park-ai-intel.com/dashboard-v5.html`
- Cloud source checkout: `/opt/gridmind/releases/trading-system-ebdbdd7aeb5ee805870d2a996bfa7fef1d0a5b5d-clean-v2`
- Changed services reloaded: `gridmind-dashboard.service`,
  `gridmind-access-gateway.service`, and the dependent
  `gridmind-cloudflared.service` to restore the public edge.

Remote file hashes matched the exact deployed tree for `dashboard-gridmind.html`,
`pipelines/dashboard_server.py`, `services/park_public_read_model.py`, and
`services/trading_system_read_model.py`.

## Read/control surface

- Loopback `GET /dashboard-v5.html` and `GET /dashboard-gridmind.html` returned
  HTTP 200 and contained both `currentStrategyCard` and `parkAiChatCard`.
- Loopback `/api/trading-system/read-model` returned the new
  `park-current-strategy-summary-v1` projection.
- The AI route without a session returned HTTP 401.
- The public target returned HTTP 302 to `/login` after the tunnel recovered.
- The current projection reports `legacy_exposure_blocker` with 19 accepted
  orders, 0 open positions, and reconciliation `ok`; it does not present the
  cycle-bound plan as an active Park Strategy Session.
- The current overall read model remains `degraded` where the persisted facts
  show market-trust/runtime or legacy-exposure blockers. This is intentionally
  not reported as a healthy Park cutover.

## Paper safety and runtime facts

- Cloud Linux Paper preflight passed with source SHA/tree bound to the deployed
  release, `paper_only=true`, `failed_check_ids=[]`, and
  `control_actions_executed=0`.
- Dashboard, Gateway, Cloudflare Tunnel, and Datafeed were active after the
  presentation-service reload. The live-tick timer remained active and enabled.
- The live-tick service was not manually restarted. One scheduled attempt
  immediately after the reload saw the stale blocked preflight receipt and
  failed closed; after the correct persistent `/var/lib/gridmind/outputs`
  preflight receipt was written, the next scheduled tick passed its boot gate,
  completed, and reported `control_actions_executed=0`.
- Deployment did not submit, cancel, close, flatten, reverse, or start any
  Paper strategy. The latest protective sweep showed 19 orders, 0 fills, and 0
  positions with reconciliation `ok`.

## Validation

- Focused read-model, Park public projection, Dashboard browser/static,
  Dashboard server, purity/concurrency, Strategy Session, and Recording Track
  suite: `171 passed`.
- `gitleaks detect --no-banner --redact`: pass.
- `git diff --check`: pass.
- Final Standards review: no hard documented-standard violations.
- Final Spec review: no material #742 gaps after lifecycle-authoritative
  geometry, DCA conditional fields, evidence-blocked UI, and malformed-data
  fixes.

This receipt proves delivery of the read-only current-strategy projection and
its Paper-safe deployment. It does not claim Park Strategy cutover is complete;
#744 remains the next implementation frontier.
