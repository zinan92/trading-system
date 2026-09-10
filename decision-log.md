# Decision log

## Issue #129: instrument-scoped account fill reconciliation

- Account and instrument reconciliation reads query the Broker's instrument-
  scoped fill stream. Registered fills are attributed by native CLOID first and
  Broker order ID second, so recovered OID-only orders receive later fills.
- A fill that has neither registered identity is retained as an immutable
  `UnattributedFill` on the reconciliation snapshot and makes the result
  explicit `unknown`; it is never coerced into a canonical order or silently
  discarded. Repeated observations are deduplicated by Hyperliquid trade ID.

### Gotchas

- The external fill transport requires an instrument scope; an account-wide
  read still means all fills for the requested instrument, not an unscoped
  network query.
- `unattributed_fills` is evidence for review, not an execution identity and
  cannot be used to place, modify, or protect an order.

## Issue #127: recovered native CLOID attribution

- Lifecycle observations resolve client identity first, then fall back to an
  already registered Broker order ID. A native CLOID discovered through that
  fallback becomes an alias of the canonical order for later events and fills.
- `OrderReceipt.native_client_order_id` is the persistence surface for the
  Broker-native CLOID. Both recovery paths accept it, and submit responses
  populate it from the native report when available.
- Nautilus open-order reports expose cumulative `filled_qty` and original
  `quantity`; the adapter projects `sz` as their difference and `origSz` as
  `quantity`, formatted at the instrument size precision.

### Gotchas

- A CLOID already owned by another canonical order never falls back by OID;
  conflicting identities remain fail-closed.
- The native CLOID alias is process-local unless the caller persists
  `native_client_order_id` and supplies it during recovery.
- `filled_qty` is not an open order's remaining size, even when both values
  happen to share the same fixed precision.

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
