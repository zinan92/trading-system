# Issue #725 — Park Paper Dashboard restore evidence (2026-08-17)

## Scope and decision

The original Park Paper Dashboard is the product baseline. The release keeps
the Dashboard HTML and existing browser surface intact; the intentional change
is the authenticated Gateway/session layer needed to replace Cloudflare OTP
with the Park Paper password while keeping the exact API allowlist and strict
mutation boundaries.

This evidence is Paper-only. It does not claim that a mutating control action
was exercised.

## Source and deployment

- Main SHA: `1a2ec229c940f1f133adc95ec3f422dfd2f9a990`
- Main tree SHA: `ac3852e4644c036b793ae489404e8af1a084d6b4`
- Deployed checkout: `/opt/gridmind/releases/trading-system-1a2ec229c940f1f133adc95ec3f422dfd2f9a990-clean`
- Active symlink: `/opt/gridmind/src/trading-system`
- Original Dashboard file SHA256: `4de1f38cfe1f015124e266d4e5217297bee5c8cddcac47807d35f0deedac87e7`
- The same Dashboard file SHA256 was observed in the prior baseline and the
  deployed checkout.

## Runtime smoke evidence

- `gridmind-dashboard.service`, `gridmind-access-gateway.service`,
  `gridmind-cloudflared.service`, and `gridmind-datafeed.service`: active.
- All eight Paper timers remain enabled: live tick, 24h report, daily review,
  backup, dead-man ping, dead-man watchdog, provider readiness, and next-cycle
  plan.
- Cloud Paper preflight: `pass`; `paper_only=true`; `control_actions_executed=0`.
- Public login page: HTTP 200, fixed-password form, no Cloudflare OTP.
- Anonymous `GET /api/auth/session`: HTTP 200 with `authenticated=false`.
- Anonymous control POST: HTTP 401; no upstream control call.
- Codex in-app browser login: the browser sends `Origin: null`; PR #724 adds a
  host-only CSRF challenge so a matching page form is accepted. An invalid
  password with a matching challenge returns HTTP 401 (`密码不正确`), not
  `origin_denied`.
- The real Dashboard loaded the original K-line surface, timeframe controls,
  Grid/DCA/direction/style/mode controls, orders, fills, positions, Supervisor,
  12h review, Strategy Shadows, and NAV tabs. Seven read-only tabs were opened
  successfully and the browser reported zero console errors.

## Invariant comparison

The authoritative read model was captured immediately before and after the
release symlink switch and service restart. These values were equal:

| Fact | Before | After |
| --- | --- | --- |
| Recording cycle | `2026-08-17_DAY` | `2026-08-17_DAY` |
| Runtime | `running` / `running` | `running` / `running` |
| Strategy plan | `strategy-plan-2026-08-17_DAY-2-ddaab7c5` v2 | same |
| Accepted/open orders | 19 / 19 | 19 / 19 |
| Open positions | 0 | 0 |
| Reconciliation | `ok` | `ok` |
| Control actions | 0 | 0 |

## Partial evidence and limits

The current Codex provider-readiness receipt is `blocked` with
`strategy_recommendation_provider_timeout`; the last successful receipt is
from the prior source SHA. Therefore the AI-enhanced “刷新趋势” path is
partial and is not claimed healthy by this release evidence. This does not
invalidate the deterministic Dashboard/read-model path or the Paper runtime.

No start, stop, replace-grid, cancel, manual-close, order, position, strategy,
live-path, exchange-key, Feishu, Shadow, or autonomous mutation was invoked.
Passwords, session tokens, tunnel credentials, and provider credentials were
not recorded.
