# Hyperliquid Testnet lifecycle proof

Status: human-gated operator runbook for Issue #30.

## Scope

This runbook proves one explicitly bound default validator-operated Hyperliquid
perpetual through the `standard-broker` canonical seam. It is Testnet only and
uses mock USDC. It does not authorize Mainnet, live keys, strategy execution,
cloud changes, or automatic broker switching.

The lifecycle must produce evidence for:

```text
submit → query → cancel/replace → fill → fee → position → reconciliation
```

## Required identity

The operator binds all of these before any external write:

- `broker_id=hyperliquid`;
- `environment=testnet`;
- master account address used for balance/order/position queries;
- API-agent signer reference;
- pinned Nautilus version `1.230.0` and its approved release SHA;
- one immutable lifecycle ID and execution scope `hypercore:default`.

The API-agent private key is read only by the out-of-band secret provider. It
must never appear in this file, GitHub, logs, screenshots, receipts, fixtures,
or Recording Track. An Ethereum `0x...` address is public identity material;
it is not a signing key and cannot execute an order.

## Preflight

1. Confirm the account is the funded Testnet master account.
2. Confirm the local secret file contains one 32-byte hexadecimal private key,
   has mode `600`, and is not inside the repository.
3. Confirm the runtime capability profile is Testnet-only and has no Mainnet
   endpoint or credential reference.
4. Load the default validator-operated perpetual universe and verify symbol,
   quantity step, five-significant-figure price rule, minimum notional, margin,
   and supported order types.
5. Read a fresh BBO and choose an order price that satisfies the canonical
   precision and slippage policy.
6. Abort before write on any account mismatch, signer mismatch, stale BBO,
   unsupported capability, missing precision fact, rate-limit denial, or
   Nautilus compatibility mismatch.

## Lifecycle

The recommended smallest proof is a short round trip on one default perpetual:

1. Submit a non-crossing GTC limit order with an explicit CLOID.
2. Query it and verify the canonical receipt is `RESTING`.
3. Cancel/replace it with another non-crossing GTC limit using the same
   canonical order lineage and a new Broker order identity.
4. Cancel and reconcile the replacement, then submit an independent IOC limit
   at the fresh approved BBO to prove the fill path; do not blindly retry an
   unknown result.
5. Read the actual fill fee and fee currency; keep it separate from the fee
   schedule estimate.
6. Read the position and verify the signed quantity against the fill.
7. Submit a reduce-only IOC close for the observed filled quantity.
8. Re-read orders, fills, fees, position, and account until the final
   reconciliation watermark is authoritative.

## Evidence contract

The proof emits only canonical, non-secret facts. The evidence identity must
include environment, account scope/address, execution scope, lifecycle ID,
release SHA, order IDs, fill IDs, fee IDs, position IDs, and reconciliation IDs.
`ExternalTestnetLifecycleEvidence` rejects a partial lifecycle, Paper
provenance, or an identity mismatch.

Testnet evidence must be stored and reviewed separately from Paper evidence.
It cannot be used as Mainnet authorization.

## Abort and recovery

The proof stops fail-closed on an ambiguous submit/cancel/replace, missing fill
identity, stale or conflicting account/position snapshot, unsupported
protection capability, rate-limit error, transport disconnect without a fresh
snapshot, or any unreconciled exposure. The operator queries and reconciles
before retrying; blind duplicate submission is forbidden.
