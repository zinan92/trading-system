# Issue #737 — Park Dashboard AI chat release evidence

Date: 2026-08-17 (Asia/Shanghai)

## Delivery identity

- Issue: [#737](https://github.com/zinan92/trading-system/issues/737)
- PR: [#738](https://github.com/zinan92/trading-system/pull/738)
- Deployed `main` SHA: `a91505c7e557ea468984a9eebef13cac2600a4f9`
- Deployed source tree SHA: `4f2d61fbb0805dfcb47f2b7e8b0d80bd5217365c`
- Public target: `https://goldbot.park-ai-intel.com/dashboard-v5.html`
- Cloud source checkout: `/opt/gridmind/releases/trading-system-a91505c7e557ea468984a9eebef13cac2600a4f9-clean`
- Changed service labels: `gridmind-dashboard.service` and
  `gridmind-access-gateway.service` only.

The deployed files were reconstructed from a source-bound archive and Git
bundle. Remote file hashes matched the exact local `main` checkout for
`dashboard-gridmind.html`, `pipelines/dashboard_server.py`,
`services/cloud_access_gateway.py`, `services/park_ai_chat.py`, and
`services/park_strategy_snapshot.py`.

## Changed user flow

- The additive `parkAiChatCard` is now before the account metrics and carries
  the `top-ai-card` visual treatment.
- Loopback `GET /dashboard-v5.html` and `GET /dashboard-gridmind.html` returned
  HTTP 200 and the response contained both `id="parkAiChatCard"` and
  `top-ai-card`.
- The gateway route `GET /api/park-paper/ai-chat` returned HTTP 401 without a
  session, proving the route is behind the existing authentication boundary.
- An unauthenticated public request to the target returned HTTP 302 to `/login`.
  The public login/session boundary remains intact; no Access or password
  policy was changed.

## Paper safety and runtime facts

- The Cloud preflight for the deployed SHA passed with `paper_only=true` and
  `control_actions_executed=0` after a transient datafeed health timeout was
  retried successfully.
- Before and after the Dashboard release, the read model reported
  `runtime_actual_state=running`, 19 accepted Paper orders, 0 positions, and
  reconciliation `ok`.
- `gridmind-live-tick.service` was not restarted or directly controlled. Its
  timer stayed `active` and `enabled`. After the source switch, the next
  scheduled one-shot passed its source boot gate and completed normally.
- While the new preflight receipt existed but the old source symlink was still
  present, one scheduled live-tick boot was rejected by the existing
  source-attestation gate (exit 79). It performed no Paper control action;
  this was a fail-closed observation. The source switch was then completed
  atomically, and the following scheduled tick passed.
- `configs/park_strategy_track.json` remains `feature_enabled=false`,
  `runtime_mode=paper_only`, `autonomous=false`, `shadow_mutation=false`, and
  `feishu_control=false`. This release did not enable the Park Paper runtime,
  submit/cancel/close an order, or change a position.

## Validation

- Focused regression: `123 passed` for Dashboard static, Park AI chat,
  Dashboard server, and Cloud gateway tests.
- Full implementation suite: `3080 passed, 1 skipped`.
- `git diff --check`: pass.
- `gitleaks detect --no-banner --redact`: pass; no secrets added.

This receipt proves delivery and the changed read/control surface. It does not
claim that a DeepSeek key or an authenticated Park conversation has been
executed against the public site, and it does not claim live/real-money
readiness.
