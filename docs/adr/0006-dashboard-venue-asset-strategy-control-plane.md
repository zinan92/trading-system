---
status: accepted
---

# Make Dashboard the canonical Venue–Instrument–Strategy control plane

The Dashboard V5 is the single canonical operator path for composing and
running a strategy: Park selects an explicit Venue Profile, selects an
Instrument from that profile's dynamic catalog, selects the existing Canonical
DCA or Grid family, reviews a deterministic Strategy Preview, and then issues
an explicit Operator Confirmation. The first execution scope is one selected
perpetual Instrument per Testnet account; the complete Hyperliquid Testnet
default-perp universe remains visible with source-bound eligibility and
pair-local blockers, while concurrent multi-asset execution stays a later
Portfolio milestone.

The Dashboard never receives private keys, owns a scheduler, rewrites strategy
sizing, or silently changes venue. The Testnet Automation Coordinator and its
runtime scheduler remain responsible for durable execution, lifecycle,
protection, fills, reconciliation, and terminal notification. Jessie/Telegram
may continue to produce a draft or preview, but it must use the same durable
control contract and cannot bypass Dashboard-style confirmation gates.

## Module boundaries

- **Data Feed** supplies normalized, source-bound market facts.
- **Standard K-line** remains a read/display contract and is not rewritten for
  this selection flow.
- **Standard Broker** supplies the explicit Instrument, account, order,
  protection, fill, and capability seams required by the selected Venue Profile.
- **Trading System** remains the composition root for strategy, Portfolio Gate,
  authorization, lifecycle, read-model, and control contracts.
- **Dashboard** owns selection, preview presentation, confirmation, and
  read-only observation.
- **Cloud/runtime** owns scheduler continuity and deployment health; a
  Cloudflare tunnel outage is an independent operations issue, not a reason to
  weaken execution gates.

## Safety and lifecycle consequences

- Hyperliquid Testnet is represented explicitly; Mainnet/Live is hidden from
  this phase and there is no cross-venue or Paper fallback.
- The account is runtime-bound and shown only through a non-secret alias or
  public fingerprint. A first proof requires a coherent, fresh, flat account
  with no open orders or unknown exposure.
- The preview shows requested versus effective sizing, gross notional, margin,
  stop-loss maximum loss, market quality, fees/slippage treatment, identity,
  and blockers before confirmation.
- Plan identity remains immutable. Plans do not expire; TP, SL, and manual
  interrupt are the only terminals. Unknown is fail-closed and never triggers
  blind retry.
- The Dashboard can expose pause, stop, and flatten as distinct intents, but
  the runtime remains the sole owner of order mutation and reconciliation.

## Considered options

- **Rejected:** auto-run immediately after selecting an asset or strategy,
  because it removes the explicit risk review and confirmation boundary.
- **Rejected:** a generic `Hyperliquid` selector with a hidden environment,
  because it makes Testnet/Mainnet identity ambiguous.
- **Rejected:** a new Dashboard repository or a second strategy engine now,
  because it would duplicate control and lifecycle contracts before the
  Testnet proof is complete.
- **Rejected:** using the Dashboard as a browser scheduler or letting Jessie
  own a separate execution path, because runtime continuity and attribution
  would become surface-dependent.
