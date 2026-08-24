---
status: accepted
---

# Preserve the Canonical DCA Strategy across Broker adaptations

The previously established Paper DCA strategy is the locked algorithmic
foundation for Path A. Hyperliquid Testnet work must adapt the Broker transport,
account/instrument facts, and venue safety lifecycle around that strategy rather
than introduce a new DCA variant; the current PAXG plan is therefore a fixture
for adapting and proving the canonical DCA, not a replacement foundation.

## Considered Options

- **Accepted:** preserve the established DCA plan and algorithm, then adapt its
  canonical intents and lifecycle to Hyperliquid Testnet.
- **Rejected:** treat the current external PAXG DCA implementation as a new
  canonical strategy, because that discards prior strategy decisions and makes
  earlier DCA discussions non-transferable.

## Consequences

- Strategy fields and semantics remain owned by `trading-system`; Broker code
  supplies transport-specific facts and capabilities only.
- Any difference between Paper DCA and Hyperliquid DCA must be recorded as a
  compatibility gap or safety gate, never silently treated as a strategy change.
- Path A cannot be marked complete until the canonical DCA behavior is mapped
  and its external lifecycle is reconciled on Testnet.
