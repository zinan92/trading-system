# Keep standard-broker as the canonical broker boundary

**Status:** accepted
**Date:** 2026-08-21

`standard-broker` will own the broker-neutral domain model, six canonical ports, capability declarations, provenance, fee/order lifecycle contracts, and fail-closed policy. `trading-system` remains the host/composition root for strategy, risk, Paper safety, Recording Track, and Telegram control; `standard-broker` will not become a second execution engine.

## Considered options

- Put all broker logic back inside `trading-system` — rejected because it preserves provider coupling and makes future broker extraction harder.
- Make `standard-broker` a full independent trading engine — rejected because Nautilus already owns execution-engine responsibilities.
- Keep a thin canonical broker boundary — accepted because it gives Tiger, Binance, and Hyperliquid one stable seam without changing strategy semantics.

## Consequences

The core contracts must remain provider-neutral. Broker-specific behavior belongs in adapters and capability details; unsupported behavior must be rejected before network I/O.
