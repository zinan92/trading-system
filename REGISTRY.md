# standard-broker Registry

## Now

- The approved canonical Broker vocabulary and architecture are recorded in `CONTEXT.md` and `docs/adr/`.
- Hyperliquid research and the accepted build Spec are recorded in the repository documentation and GitHub Spec Issue #2.
- T01 is merged in PR #11 and establishes the six-port Paper Broker Contract seam with local-only transport and fail-closed capability checks.
- T02 is merged in PR #12: Hyperliquid default-perps market data and instrument mapping.
- T03 is merged in PR #13: Hyperliquid account, fee, funding, and liquidation facts.
- T04 is in progress: Hyperliquid order lifecycle and reconciliation.

## Next

- T05: Hyperliquid protection order semantics, after T04 merges.
- T04–T08 remain blocked in dependency order by the published GitHub tickets.

## Safety boundary

- Paper only; no testnet or mainnet credentials, network I/O, or live execution.
- The trading system remains the host for strategy, risk, Recording Track, and Telegram control.
