# standard-broker Registry

## Now

- The approved canonical Broker vocabulary and architecture are recorded in `CONTEXT.md` and `docs/adr/`.
- Hyperliquid research and the accepted build Spec are recorded in the repository documentation and GitHub Spec Issue #2.
- T01 is merged in PR #11 and establishes the six-port Paper Broker Contract seam with local-only transport and fail-closed capability checks.
- T02 is in progress: Hyperliquid default-perps market data and instrument mapping.

## Next

- T03: Hyperliquid account and fee facts, after T02 merges.
- T04–T08 remain blocked in dependency order by the published GitHub tickets.

## Safety boundary

- Paper only; no testnet or mainnet credentials, network I/O, or live execution.
- The trading system remains the host for strategy, risk, Recording Track, and Telegram control.
