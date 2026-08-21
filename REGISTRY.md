# standard-broker Registry

## Now

- The approved canonical Broker vocabulary and architecture are recorded in `CONTEXT.md` and `docs/adr/`.
- Hyperliquid research and the accepted build Spec are recorded in the repository documentation and GitHub Spec Issue #2.
- T01 is merged in PR #11 and establishes the six-port Paper Broker Contract seam with local-only transport and fail-closed capability checks.
- T02 is merged in PR #12: Hyperliquid default-perps market data and instrument mapping.
- T03 is merged in PR #13: Hyperliquid account, fee, funding, and liquidation facts.
- T04 is merged in PR #14: Hyperliquid order lifecycle and reconciliation.
- T05 is merged in PR #15: Hyperliquid protection order semantics.
- T06 is merged in PR #16: Nautilus Hyperliquid compatibility bridge.
- T07 is merged in PR #17: Broker conformance, security, and Paper proof.
- T08 is merged in PR #18: trading-system host integration contract.

## Next

- T01–T08 are complete and merged; the final full-suite and requirement audit passed.
- No live/testnet milestone is implied; future external environments require a separate approved plan.

## Safety boundary

- Paper only; no testnet or mainnet credentials, network I/O, or live execution.
- The trading system remains the host for strategy, risk, Recording Track, and Telegram control.
