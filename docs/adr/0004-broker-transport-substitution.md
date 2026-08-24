---
status: accepted
---

# Preserve the existing system during Broker transport substitution

Path A is a Broker Transport Substitution: keep the established Strategy,
risk, control plane, Dashboard contract, and lifecycle semantics, unplug the
Binance Paper binding, and plug in the Hyperliquid Testnet binding. This is not
a new DCA strategy and not a full Strategy extraction. The selected binding must
carry an explicit compatible Instrument and execution-grade market-data source;
the system must never make decisions from one venue's prices while submitting
to another venue.

## Consequences

- The old DCA Strategy remains the canonical algorithmic foundation.
- Hyperliquid-specific behavior belongs in the Broker adaptation and its
  declared capability/lifecycle gates.
- `PAXG-USD-PERP` is an explicit Instrument mapping, not an implicit alias for
  the old Binance `GOLD/XAUUSDT` contract; parity claims must name this mapping.
- Data Feed module code may remain unchanged when its existing contract is
  reused, but the selected source must be execution-grade for the chosen Broker.
- Extracting Strategy into a separate repository is a future architecture
  milestone and does not block the current Hyperliquid adaptation.
