# Decision log

## Issue #125: unregistered open-order projection

- `open_orders()` treats a Broker order that is absent from the recovered local
  registry as an immutable `OrderReceipt` with `state=unknown` and reason
  `unregistered_exchange_order`; it does not add that order to the registry.
- Reconciliation keeps the complete open-order observation, exposes the
  unregistered receipts and count, and returns a non-coherent result with
  `unregistered_open_orders` for the caller to accept or reject.
- Event and fill ownership continue to use strict identity resolution; only the
  read-only open-order query has the external projection path.

### Gotchas

- Process-local registration is not proof that a Broker order is unrelated to
  the wider system. Persisted identities must be restored with `recover()`
  before a fresh process can assign those orders to canonical order IDs.
- An unregistered open-order projection is observation data, not execution
  authority: cancel, replace, fill, and event paths cannot resolve its
  `external:<broker_order_id>` identity.

## Issue #123: recover/cancel instrument identity

- Recovery now synchronizes the canonical intent instrument into the runtime
  transport index, so recovered orders can cancel or modify before submit.
- External instrument loading treats a standard perp without
  `info.asset_index` as incomplete and rebuilds the default perp set from
  authoritative perp metadata before caching it.

### Gotchas

- `load_instrument_definitions` can succeed while omitting Hyperliquid's
  `asset_index`; caching that object makes Nautilus fail later during cancel.
- Recovery does not call submit, so submit-only transport state must not be the
  source of instrument identity.
