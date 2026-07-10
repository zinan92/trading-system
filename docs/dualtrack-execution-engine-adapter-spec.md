# DualTrack Execution Engine Adapter Spec

Status: compatibility adapter and 1m OHLC replay implemented; Nautilus spike not enabled
Date: 2026-07-10

## Objective

Give the DualTrack widgets one stable execution contract while allowing the
underlying paper engine to move from the local ledger to NautilusTrader without
rewriting the frontend.

This boundary owns execution and accounting only. Strategy plans, chart
rendering, and market-data acquisition remain separate.

## Non-negotiable invariants

1. GET endpoints never mutate orders, fills, positions, accounts, or PnL.
2. Browser-supplied prices never trigger fills or protective exits.
3. Synthetic, fallback, stale, future-dated, out-of-cycle, provider-mismatched,
   or source-ambiguous market events never reach an execution engine.
4. Every exit closes explicit entry units. Equal notional is not equal quantity.
5. `sum(fill.realized_pnl)` reconciles to the account realized PnL.
6. A fill ID is globally idempotent within an engine ledger.
7. Raw historical fills are immutable. Migrations rebuild derived positions and
   PnL rather than rewriting source events.
8. Engine-specific fields do not leak into frontend widgets.

## Boundary

```text
datafeed -> canonical MarketEvent -> ExecutionEngineAdapter
                                      |-> orders
                                      |-> fills
                                      |-> positions
                                      |-> account / pnl
                                      `-> reconciliation

standard-kline <- market data + normalized overlays
DualTrack widgets <- normalized execution snapshot
```

## Adapter protocol

`services/dualtrack_execution_adapter.py` defines four operations:

### `submit_order(command)`

Accepts an order command and returns the resulting fill or engine receipt.
Network callers must already have passed same-origin, server-clock, current
cycle, canonical-market, and TP/SL geometry validation.

### `process_market_event(event)`

Accepts a canonical, trusted market event. Required fields:

| Field | Meaning |
|---|---|
| `cycle_id` | Target DualTrack cycle |
| `ts_event` | UTC event timestamp |
| `price` | Canonical execution mark |
| `event_started_at` | Bar start for OHLC events; omitted for point-price events |
| `open` / `high` / `low` | Optional trusted bar range used for protective triggers |
| `fresh` | Must be exactly `true` |
| `is_synthetic` | Must be exactly `false` |
| `source` | Non-empty data lineage |

### `snapshot(cycle_id, mark_price, mark_fresh, mark_source)`

Returns `dualtrack-execution-v1`:

```json
{
  "schema_version": "dualtrack-execution-v1",
  "engine": "legacy_paper",
  "cycle_id": "2026-07-10_NIGHT",
  "orders": [],
  "fills": [],
  "positions": [],
  "account": {},
  "pnl": {"realized": 0.0, "unrealized": 0.0},
  "mark": {"price": null, "fresh": false, "source": ""},
  "capabilities": {}
}
```

Widgets consume only this normalized envelope. They must not read Nautilus
cache objects, legacy fill files, or venue payloads directly.

### `reconcile(cycle_id)`

Returns a deterministic list of invariant violations, including duplicate fill
IDs, orphan exits, negative remaining units, and account/fill PnL mismatch.

## Adapter 1: `legacy_paper`

The compatibility adapter wraps `DualTrackHumanEngine` and the current local
JSON ledger. It is now the order entry path used by the dashboard and the
protective-exit path used by `dualtrack-live-tick`.

Known capability limits are explicit:

- no native order lifecycle (`orders` is empty);
- protective orders replay every trusted 1m OHLC bar since the earliest open
  trade and use high/low for trigger detection;
- restart reconciliation is local-ledger-only;
- no exchange queue, partial-fill, tick path, or same-minute pre/post-entry path
  model. If one bar touches both stop and target, stop wins conservatively.

No additional features should be added to this engine beyond safety and
reconciliation fixes needed for migration.

## Adapter 2: Nautilus spike

The spike stays isolated until it passes the same adapter contract. Do not make
Nautilus a production dependency before parity is proven.

### Input

- Instrument definition supplied by `datafeed`; do not hard-code tick size,
  price precision, quantity precision, or contract multiplier.
- Canonical bars or trade/quote events with source lineage.
- No fallback or synthetic events.
- Explicit fee, slippage, and matching configuration.

### Fixed scenarios

1. Long and short market entry.
2. Limit entry that is not filled until the market reaches it.
3. Scale in with two fills and weighted average price.
4. Partial reduction followed by full close.
5. Stop-loss and take-profit execution.
6. Entry, stop, and target touched in one bar with an explicit conservative
   same-bar priority.
7. Fees, slippage, margin, nominal exposure, and realized/unrealized PnL.
8. Duplicate command/event replay with no duplicate fill.
9. Restart from persisted state and reconciliation.
10. The 2026-07-09 machine-fill fixture that previously left residual units.

### Parity gate

For every scenario, normalize both adapters and compare:

- order terminal state;
- fill count, side, price, and quantity;
- open/closed position quantity;
- realized and unrealized PnL;
- account balance, margin, and exposure;
- reconciliation status.

Differences require a written model decision. They must not be hidden by
tolerance changes or frontend formatting.

## Cutover

1. Run both adapters in shadow mode from the same immutable event stream.
2. Keep `legacy_paper` authoritative while recording parity reports.
3. Require all fixed fixtures plus seven consecutive paper cycles without an
   unexplained reconciliation drift.
4. Switch the configured engine to Nautilus for paper only.
5. Keep rollback to `legacy_paper` until restart and persistence tests pass.
6. Consider real-money integration only under a separate attended approval.

## Gotchas

- OHLC bars do not reveal the intrabar path. If both TP and SL are touched, the
  compatibility engine executes the stop first. Exact path semantics require
  the standardized datafeed trade/quote stream.
- Replaying all stored 1m bars prevents a recovered close from hiding an
  earlier wick. It does not make execution tick-real-time; the installed
  scheduler must also use the generated 60-second live-tick definition.
- A fresh browser WebSocket does not make the server ledger fresh. Orders block
  when the canonical server feed is stale.
- Historical legacy fills can contain incorrect exit notional. Preserve the raw
  events and regenerate normalized positions instead of editing history.
- `legacy_paper` is a migration adapter, not the mathematical framework to keep
  extending.
