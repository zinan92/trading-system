# Issue #958 — attended Hyperliquid Testnet proof (awaiting Park confirmation)

Status: `blocked_existing_position` — the clean-slate read is now coherent and
fresh, but the existing PAXG position is not flat. No canonical DCA proof claim
has been made.

## Attempt boundary

| Field | Evidence |
|---|---|
| Trading-system source | `main@cf4fe8b32abad88ea0c0f5fb4ddca0f740c400b5` |
| Standard-broker source | `main@25a04be00eadf9a1f6da7dad7204f68bcc1d1632` |
| Environment | Hyperliquid Testnet only |
| Account | master account supplied by Park; only its redacted fingerprint was used |
| Instrument | `PAXG-USD-PERP` |
| Operation completed | public standard-broker account/facts read only |
| Order submit/cancel/flatten | not invoked |
| Mainnet/Live/scheduler/cloud | not invoked |

## Clean-slate read result

After standard-broker PR #95, the public binding returned:

| Fact | Result |
|---|---|
| Position | `-0.060 PAXG-USD-PERP` |
| Open orders | `0` |
| Cursor | present |
| Reconciliation | `coherent=true`, `freshness=fresh` |
| Mutation | no submit/cancel/flatten |

The existing canary is therefore a known non-flat short, not a clean slate.
The Trading System did not call a native venue API or infer a flat state.

The signer value was not printed, logged, or persisted. The external runtime
may resolve the signer internally for a read-only client; this evidence makes
no claim that resolution was absent. No secret value escaped the boundary and
no order mutation occurred.

## Cleanup plan awaiting Park confirmation

The isolated cleanup plan is `cleanup-existing-paxg-20260824` with digest
`sha256:0526d28f04429f72a0b6b371be92aee1cd71ecf8c879dd7afaf7682ede9afa0b`.
The local durable proposal is
`cleanup-existing-paxg-20260824-proposal`, expiring at
`2026-08-24T10:30:00+00:00`. No `confirmed` decision has been written.

Next action: Park must explicitly confirm that exact digest. Only then may the
isolated `adopt-flatten` action read the position again, submit one reduce-only
close, and require cursor-bound `FLAT_RECONCILED` evidence. A new canonical DCA
start remains forbidden until that evidence exists.

This is Testnet evidence only. It does not advance soak readiness, Live/Mainnet
activation, automatic promotion, or any cloud deployment state.
