# Issue #958 — attended Hyperliquid Testnet proof (cleanup reconciled)

Status: `FLAT_RECONCILED` for the existing cleanup close. This is causal
evidence for the one historical reduce-only flatten only; it is not a new DCA
entry, a strategy-success claim, or Live/Mainnet readiness.

## Attempt boundary

| Field | Evidence |
|---|---|
| Trading-system source | `main` after PR #981 merge `71e844147d6ea79c684c7ad21ef6c6197e191f51` |
| Standard-broker source | `main` after PR #117 merge `d43d0bbb51e38da1186c0417778ed8ca0b9da76e` |
| Runtime release identity in plan | `c9a31732b61f0289867a3b8e40cc26a380072bbc` |
| Environment | Hyperliquid Testnet only |
| Account | Park-confirmed master account; only fingerprint persisted: `sha256:75b327ae65b27db8eb8955fd922a291945da160ba52e9f64995ed9e430df0c56` |
| Instrument | `PAXG-USD-PERP` |
| Plan | `cleanup-existing-paxg-20260824` |
| Plan digest | `sha256:0526d28f04429f72a0b6b371be92aee1cd71ecf8c879dd7afaf7682ede9afa0b` |
| Confirmation receipt digest | `sha256:ad66d3d79e555219bed5f2ff468afbffc8eeb19f409b59dad8950545b279c2bc` |
| Cloudflare/dashboard/cloud mutation | not invoked |

## One attended cleanup action

The existing short position was adopted at approximately `09:19:03Z` and one
reduce-only close intent was submitted at approximately `09:19:04Z`. The
canonical receipt was `UNKNOWN` with no broker order identity. This was the
only submit; no cancel, retry, next-entry, scheduler, Mainnet, or Live action
was performed afterward.

The persisted client identity was recovered through the public seam. Hyperliquid
Order History then supplied the exact venue Order ID `58400711187`. The final
recovery path used only `recover → query → canonical facts read`; it did not
submit, cancel, replace, retry, or infer identity from price/time/quantity.

## Final causal public facts

The final explicit-broker recovery persisted at approximately `11:18:21Z` is:

| Fact | Result |
|---|---|
| Position | `0` (`positions=[]`) |
| Open orders | `0` (`open_orders=[]`) |
| Broker Order ID | `58400711187` |
| Client Order ID | `0xc95e8360e8d6bf4c175718058051bfe3` |
| Fill facts | `2` chunks: `0.025` + `0.035` PAXG, both at `4642.500` |
| Fee facts | `2` actual USDC fees: `0.05222800` + `0.07311900` |
| Fill side | `buy` (closing the persisted short) |
| Cursor | `1787570302564` |
| Snapshot | `coherent=true`, `freshness=fresh` |
| Final facts digest | `sha256:f3caa991fd4a208a7f86533a12a5dd674c2ddc72883d83df21156d248b6dc28f` |
| Lifecycle status | `FLAT_RECONCILED` |
| Next action | `record_dca_result` |

The two fills share the exact broker Order ID and persisted client identity;
the public seam also returned zero position and zero open orders on the same
cursor. This is the explicit causal flatten proof required by #980.

The earlier flat-but-not-causally-reconciled snapshots remain in the append-only
state journal as historical blockers; they are not the final state.

Historical diagnostic before the exact venue OID was supplied: after the
RT-15/RT-16 public-seam fixes, a read-only redacted diagnostic saw
four instrument fill reports, zero reports carrying a client identity, zero
matches to the recovered canonical/native client identity, zero canonical
fills, and zero canonical fees. The diagnostic did not print raw provider
payload, order IDs, prices, quantities, or credentials. That historical
blocker was resolved by the explicit venue Order ID above; no price/time/
quantity inference was used.

## Public seam and test evidence

- standard-broker PRs #103/#105/#107/#109/#114/#115/#117 now provide the
  public client/OID recovery, distinct fill chunks, fee instrument context,
  and local-fixture identity compatibility used by this host.
- standard-broker clean RT-20 main (`d43d0bb`) full regression: `303 passed`.
- trading-system RT-25 focused DCA/CLI regression: `46 passed`; external
  canary/testnet seam regression: `27 passed`.
- trading-system full regression against the RT-20 seam: `3382 passed`, `50
  skipped`, `6` warnings. `compileall`, `diff-check`, and `gitleaks` passed.

No raw provider payload or signer value is included in this evidence. The
system remains Testnet-only, and this report does not advance soak, Live,
Mainnet, automatic promotion, or cloud deployment readiness.

## Next action

Do not start a new canonical DCA plan automatically. A future DCA run needs a
new plan, matching Park confirmation, and a separately scoped attended action;
the reconciled cleanup is not authorization to advance the strategy or enter a
new position.
