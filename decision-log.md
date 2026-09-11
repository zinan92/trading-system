# Decision log

## Issue #137: exact protection identity recovery after an empty submit response

- Each position-following leg derives its deterministic `SBP-...` ID and the
  venue CLOID from `nautilus_pyo3.hyperliquid_cloid_from_client_order_id`.
  Recovery is bounded to five rounds and probes, in order, the SBP ID, the
  venue CLOID, then the instrument-scoped status-report list. List matching is
  identity-only (`SBP` or venue CLOID); quantity is never a matching key.
- An identity hit must have an open/resting/waiting status and matching
  `side` and `trigger_px`; `reduce_only` is also checked. Multiple list rows
  for one leg or any conflicting field raises
  `protection_recovery_conflict`. Successful rows persist the real venue `oid`
  and venue CLOID for later query/cancel calls.
- Every recovery round records bounded raw summaries for all three stages,
  including `status`, `oid`, `cloid`, `side`, `trigger_px`, `quantity`, and
  `reduce_only`; failures retain the last two rounds and cap evidence at 4000
  characters. This preserves the original returned rows for the next
  diagnosis instead of recording only matches.
- Nautilus 1.230.0's Hyperliquid execution adapter converts pyo3
  `OrderStatusReport.trigger_price` and `.reduce_only` directly into the
  public report (`execution.py` `generate_order_status_reports` plus
  `execution/reports.py` `OrderStatusReport.from_pyo3`). They are therefore
  reliable consistency fields for this recovery path; quantity remains
  evidence only because position-following reports can expose a different
  size, including zero.

## Issue #135: recover positionTpsl protection after an empty submit response

- When Nautilus `submit_orders` returns an empty or non-list response, the
  Hyperliquid adapter performs at most five instrument-scoped open-order
  queries. A complete match requires both reduce-only legs to have the
  requested side, trigger price, and quantity; the recovered Broker `oid` and
  native `cloid` are persisted for later query and cancel operations.
- Recovery is fail-closed: zero matched legs raises
  `protection_submit_unconfirmed`, while one matched leg raises
  `protection_submit_partial`. The error includes a redacted, bounded query
  summary rather than treating an empty response as success.

### Gotchas

- Nautilus can return no actionable group report even after Hyperliquid has
  accepted both child orders. Hyperliquid replay payloads may also wrap the
  order under `order` and expose lifecycle status beside it; both forms must
  be normalized only at the adapter boundary.
- The canonical `SBP-...` IDs are not necessarily the native Hyperliquid
  CLOIDs. Once recovery finds native identities, all later status and cancel
  calls must use those persisted identities.

## Issue #133: declarative recovered fill state and bounded fill reconciliation

- `recover()` and `recover_client_order()` preserve a persisted `filled` or
  `partially_filled` state as declarative identity only: they initialize
  `filled_quantity=0` and `remaining_quantity=original_quantity`, retaining
  `recovered_persisted_*` as the reason. Broker fills are the sole source that
  accumulates canonical filled quantity and transitions the receipt to its
  observed fill state.
- Runtime fill queries isolate per-observation application failures. A
  conflicting or over-counted fill is retained as `UnattributedFill`, while
  other observations in the same instrument-scoped response continue to be
  applied. Reconciliation therefore returns the available positions, orders,
  and fills with an explicit non-passed outcome instead of raising from one
  order.

### Gotchas

- A recovered receipt may report `FILLED` or `PARTIALLY_FILLED` before the
  Broker has returned a matching fill; this is persisted identity, not local
  accounting evidence. Do not use its declared state as a filled quantity.
- A fill application failure is intentionally visible through
  `unattributed_fills`; it is not silently retried or converted into a valid
  canonical fill. Repeated valid observations remain idempotent by trade ID.

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

## Issue #131: canonical instrument-scoped fill recovery

- Instrument-scoped fill queries normalize canonical IDs, Broker symbols, and
  `<canonical>.HYPERLIQUID` aliases before sending the Broker symbol to the
  transport and comparing canonical fill identities.
- Runtime recovery binds the receipt before registering it, and fills derived
  from the recovered lifecycle carry the same session identity used by
  reconciliation.

### Gotchas

- `HyperliquidExternalSnapshotReader` passes the canonical instrument ID to the
  typed order facade; the adapter translates it to the Broker-native symbol at
  the transport boundary.
- An instrument alias is only a lookup convenience. It does not change the
  canonical `OrderFill.instrument_id` or create a second instrument identity.
