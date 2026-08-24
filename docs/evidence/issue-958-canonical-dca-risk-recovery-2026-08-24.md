# Issue #958 — canonical Plan-03 Hyperliquid Testnet proof

Status: `FLAT_RECONCILED`. This report covers one attended entry from the
previously confirmed Plan-03 semantics, the fail-closed risk blocker, and the
separately confirmed risk-recovery flatten. It is not soak, Live, or Mainnet
readiness.

## Strategy parity and bindings

| Field | Result |
|---|---|
| Strategy source | Prior confirmed Paper DCA Plan-03 semantics |
| Source strategy plan | `paper-dca-paxg-testnet-20260824-02` |
| Source strategy digest | `sha256:b1c5771ec38258d86b40f70b0e7ce980e2e19ed1f4737b0b9252c8821871d0ae` |
| Semantics | short; entries `4630` / `4660`; `0.06` / `0.06`; target `4580`; stop `4680`; max additions `2`; loop `false` |
| Instrument | `PAXG-USD-PERP` on Hyperliquid Testnet |
| Account | fingerprint only: `sha256:75b327ae65b27db8eb8955fd922a291945da160ba52e9f64995ed9e430df0c56` |
| Broker seam | standard-broker public external Testnet seam, `d43d0bbb51e38da1186c0417778ed8ca0b9da76e` |
| Trading-system code | PR #985 merge `49e1ead44be78336b9838a0a3ff64b03e81aa45a` |

## Attended lifecycle

1. Protected preflight passed with `protection_ready=true`, exact account,
   runtime, release, capability, and Testnet identity. No order operation or
   signer resolution occurred during preflight.
2. Exactly one first entry was submitted from the confirmed external plan.
   Venue Order ID `58421230104` filled `0.060 PAXG` at `4681.300`; actual fee
   was `0.126395 USDC`. The fill exceeded the plan's slippage guard, so the
   lifecycle persisted canonical entry facts and stopped at
   `BLOCKED/entry_fill_slippage_exceeded` before protection or next-entry.
3. Recovery queried the persisted entry identity without submitting another
   entry. Stale/unknown facts remained blockers until a fresh canonical read
   persisted the `-0.060` short position.
4. The first flatten attempt returned `UNKNOWN` because its candidate price
   violated Hyperliquid five-significant-figure precision. No retry was made.
5. A separately confirmed risk-recovery plan fixed a fresh-market, venue-valid
   close price of `4680.2`. Hyperliquid Order History supplied the exact
   flatten Order ID `58423568585`; explicit broker-identity recovery then read
   the canonical fills and fees without submit/cancel/retry.

## Final causal facts

| Fact | Result |
|---|---|
| Recovery plan digest | `sha256:f56e16a7e0a82a768147f19316a86aac2f2cb009ad5fa5354929f14a2eea12d6` |
| Broker Order ID | `58423568585` |
| Client Order ID | `0xf3de776dce3b06e903e7573374d01a09` |
| Close fills | `0.026 @ 4672.600` + `0.034 @ 4672.500`, both `buy` / close-short |
| Actual fees | `0.05466900` + `0.07148900` USDC |
| Cursor | `1787586439505` |
| Position | `0` |
| Open orders | `0` |
| Reconciliation | `coherent=true`, `freshness=fresh`, signed position `0` |
| Final facts digest | `sha256:4fdf4c3f3d7c9d06c80e8e0ae02ffce0ec729d960d9a38ffa9560f9dbde9999b` |
| Lifecycle status | `FLAT_RECONCILED` |
| Next action | `record_dca_result` |

The browser Order History independently displayed the same filled PAXG close
Order ID `58423568585` and the account page displayed zero current position and
`998.55 USDC` perps equity. Browser use was read-only.

## Verification and boundaries

- Trading-system external DCA/CLI focused recovery suites: `54 passed`.
- Final full trading-system suite against clean standard-broker RT-20 seam:
  `3390 passed`, `50 skipped`, `6 warnings`.
- standard-broker clean suite: `303 passed`.
- `compileall`, `git diff --check`, and `gitleaks` passed.
- No second entry, automatic next-entry loop, protection submission after the
  risk blocker, scheduler, Mainnet/Live, Dashboard/Cloudflare mutation, or
  cloud deployment occurred.
- Testnet evidence remains separate from soak and Live readiness. No new DCA
  cycle starts automatically after this terminal cleanup.

No signer value, raw provider payload, or credential is included here.
