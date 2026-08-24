# Issue #958 — attended Hyperliquid Testnet proof (blocked, fail-closed)

Status: `BLOCKED`. The account is currently flat with no open orders, but the
single ambiguous cleanup close has no public causal fill/fee evidence. Flat
account state alone is not `FLAT_RECONCILED`, so no canonical DCA success claim
is made.

## Attempt boundary

| Field | Evidence |
|---|---|
| Trading-system source | `main` after PR #975 merge `a951628372c26315d4f903669297e31105fd393d` |
| Standard-broker source | `main` after PR #105 merge `4e5cf2e22ff7a80b644e3171a165771c78313194` |
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

The persisted client identity was recovered through the public seam. Querying
that identity returned canonical `UNKNOWN` with reason `missing`; the public
fill/fee read returned no matching fill and no fee. The system therefore kept
the lifecycle blocked.

## Latest public facts

The latest account-only public read was coherent/fresh and returned zero
positions and zero open orders. The final causal read persisted by the
lifecycle at approximately `10:17:20Z` is:

| Fact | Result |
|---|---|
| Position | `0` (`positions=[]`) |
| Open orders | `0` (`open_orders=[]`) |
| Fill facts | `0` |
| Fee facts | `0` |
| Cursor | `1787566641004` |
| Snapshot | `coherent=false`, `freshness=unknown` because causal fill/fee observations are missing |
| Final facts digest | `sha256:b2369648896415a586cc581ed7cd2cccafc49d3f8dafb7daa384883bc053a79c` |
| Lifecycle status | `BLOCKED` / `facts_reconciliation_not_coherent` |
| Next action | `notify_park_and_wait` |

This is an explicit flat-but-not-causally-reconciled state. It must not be
promoted to `FLAT_RECONCILED` or used to start a new DCA cycle.

## Public seam and test evidence

- standard-broker PR #103 added client-scoped fill recovery; PR #105 forwarded
  the typed client scope to the native fill query.
- trading-system PR #973 passed the recovered client identity into final facts;
  PR #975 persisted blocked final facts without weakening validation.
- standard-broker focused/full regression after PR #105: `287 passed, 1
  skipped`.
- trading-system external lifecycle/canary regression after PR #975: `28
  passed` (two existing collection warnings).

No raw provider payload or signer value is included in this evidence. The
system remains Testnet-only, and this report does not advance soak, Live,
Mainnet, automatic promotion, or cloud deployment readiness.

## Next action

Keep #958 open and do not start a new canonical DCA plan. A future continuation
needs a new, explicitly scoped read/recovery decision and a public causal
fill/fee fact (or a separately reviewed contract change); it must not infer
success from the flat account snapshot.
