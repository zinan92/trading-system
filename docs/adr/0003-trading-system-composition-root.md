---
status: accepted
---

# Keep Strategy and Broker independent behind the Trading System composition root

The target architecture keeps Strategy and Broker as independent modules with
different ownership: Strategy defines Broker-neutral algorithmic intent, while
Broker defines canonical instrument, account, order, protection, fee, and
reconciliation capabilities. The current repository still co-locates Strategy
logic with the Trading System host, so Path A must add the composition seam
without claiming that the full decoupling is already complete. In the target
state, `trading-system` is the composition root that glues Strategy, Broker,
Data Feed, risk, authorization, lifecycle, read-model, and control contracts;
it must not become a second strategy engine or a venue adapter.

## Consequences

- A Broker change must not rewrite Strategy logic or prior Strategy decisions.
- A Strategy change must not require Dashboard or Broker-native payload changes.
- The composition root owns binding identity, policy gates, lifecycle orchestration,
  and evidence association.
- Dashboard talks to the Trading System contract only; it never imports Strategy
  or Broker-native modules directly.
- The current co-location is transitional; a future extraction must preserve the
  canonical Strategy contract rather than create a second algorithm.
