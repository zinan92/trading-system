# Issue #958 — attended Hyperliquid Testnet proof (blocked)

Status: `blocked` — no clean-slate or canonical DCA proof claim.

## Attempt boundary

| Field | Evidence |
|---|---|
| Trading-system source | `main@212c4c70a40abba8e09ac139830b790cc4496884` |
| Standard-broker source | `196e36846cba49101f9b3ef43931c1efd0ada1ec` (`standard-broker-testnet`) |
| Environment | Hyperliquid Testnet only |
| Account | master account supplied by Park; only its redacted fingerprint was used |
| Instrument | `PAXG-USD-PERP` |
| Operation attempted | public standard-broker account/facts read only |
| Order submit/cancel/flatten | not invoked |
| Mainnet/Live/scheduler/cloud | not invoked |

## Observed blocker

The public binding constructed successfully after loading the bundled
`nautilus_trader==1.230.0` runtime, but the cursor-bound account read stopped at
the standard-broker public mapper with:

```text
account.read -> canonical_account_snapshot_gap
```

This is an upstream public-seam capability gap. The Trading System does not
read provider-native payloads or call Hyperliquid directly to work around it.
The existing PAXG canary therefore remains `UNKNOWN`; this artifact does not
claim `FLAT_RECONCILED`, and no new canonical DCA start is permitted.

The signer value was not printed, logged, or persisted. Because the external
runtime constructs its read client before the mapper fails, this attempt does
not claim that signer resolution was absent; only that no secret value escaped
the boundary and no order mutation occurred.

## Required next action

1. Fix or publish a reviewed standard-broker public `account.read` mapping that
   returns the canonical account/position/open-order/reconciliation facts.
2. Repeat the read-only clean-slate audit and retain cursor-bound zero-position
   and zero-open-order evidence.
3. Obtain a fresh Park confirmation for any owned flatten action if the audit
   finds exposure; only after `FLAT_RECONCILED` may the canonical DCA proof
   proceed.

This is Testnet evidence only. It does not advance soak readiness, Live/Mainnet
activation, automatic promotion, or any cloud deployment state.
