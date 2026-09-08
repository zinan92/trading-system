# Decision log

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
