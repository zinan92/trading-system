# Keep RT-06 as the Paper reconciliation seam

**Status:** accepted
**Date:** 2026-08-22

RT-06 hardens ambiguous outcomes, reconnect snapshots, duplicate observations, and
reconciliation facts. It must not become a second owner of the canonical order
lifecycle.

## Decision

- `HyperliquidRuntimeReconciliationAdapter` is a Paper-only reconciliation and
  resilience seam. It consumes an injected canonical order lifecycle and injected
  canonical account/fact source.
- RT-06 does not own order submission, cancellation, replacement, signing,
  transport, strategy intent, risk authorization, or Recording Track authority.
- Every authoritative snapshot at the seam carries the bound Paper account,
  positions, and a monotonic Broker watermark. A changed fact set at the same
  watermark fails closed.
- Retry reconciliation facts arrive as one `ReconciliationSnapshot`; separate
  fills/positions/account queries are not an acceptable substitute because they
  can describe different Broker cursors.
- A native `tid` plus hash can absorb a later matching hash-only observation.
  A hash-only observation cannot later be promoted to a `tid` without an
  authoritative identity proof, so that direction fails closed.
- A later runtime-composition milestone (RT-08) must provide the one canonical
  order lifecycle owner and route its observations through this seam. No second
  direct submit/cancel/query/reconcile path may be introduced.
- `trading-system` remains the host and composition root. It may invoke only the
  canonical host contract and continues to own strategy, risk, Paper gates,
  Recording Track, Supervisor, boot, release-SHA, and Telegram control.

## Consequences

- RT-06 can be tested independently with local Paper fixtures and cannot prove
  external testnet or Live execution.
- The injected lifecycle owner remains authoritative for receipt quantities,
  fill aggregation, and terminal state; RT-06 keeps only observation identity,
  ordering, and recovery coordination state.
- A validated retry snapshot commits its account, positions, watermark, and
  fact fingerprint atomically before a recovery lease is issued. An unchanged
  cursor may be replayed only when its canonical facts are identical.
- Rate-limit retry is fail-closed unless the error explicitly declares that no
  Broker side effect was possible. A canceled or rejected order is retryable
  only when its receipt carries an explicit ambiguous/transient reason and a
  current one-use recovery lease.
- RT-08 is responsible for composing the lifecycle owner with this seam; it must
  not duplicate Broker-native mapping or create a second Hyperliquid transport.
- Any unsupported runtime capability remains a capability gap and blocks the
  dependent path; the seam never simulates success.

## Non-goals

- No testnet or Live network access.
- No credentials, signing, deployment, cloud mutation, strategy redesign, or
  automatic Broker switching.
