# DualTrack Execution Engine Adapter Spec

Status: typed port, isolated Legacy/Nautilus/Shadow adapters, and frozen plugin
registry implemented; Strategy Shadow delegates to isolated Nautilus replay;
fixed parity suite passed; Nautilus paper adapter not selected because the
seven command-bearing cycle gate is incomplete
Date: 2026-07-18

## Objective

Give the DualTrack widgets one stable execution contract while allowing the
underlying paper engine to move from the local ledger to NautilusTrader without
rewriting the frontend.

This boundary owns execution and accounting only. Strategy plans, chart
rendering, market-data acquisition, and promotion policy remain separate.

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
9. A Strategy Shadow bar cannot start before its StrategyPlan availability or
   command timestamp.
10. Candidate replay namespaces cannot write production ledgers or the
    reconciliation, parity, and cutover evidence that controls paper authority.
11. Candidate and platform evidence must bind the exact source semantics,
    Nautilus runtime version, fee/precision contracts, and evidence timestamp;
    an old child fixture cannot be restamped as current evidence.

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
Strategy Shadow -> content-bound scenario -> Nautilus replay -> accounting snapshot
Lab promotion <- candidate receipt + current platform parity
```

## Adapter protocol

`services/execution_engine_port.py` defines five operations.
`services/execution_plugin_composition.py` is the only production selection
root, while `services/dualtrack_execution_adapter.py` remains a re-export-only
compatibility facade:

### `submit_order(command)`

Accepts an order command and returns the resulting fill or engine receipt.
Network callers must already have passed same-origin, server-clock, current
cycle, canonical-market, and TP/SL geometry validation.

### `cancel_orders(cycle_id, order_ids, strategy_plan_id, ts, reason)`

Cancels only the selected accepted orders and returns an idempotent normalized
receipt. It never infers a whole-plan cancellation from an empty identity.

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

## Adapter 2: `nautilus_paper`

`services/dualtrack_nautilus_execution_adapter.py` implements the same adapter
contract through an isolated Nautilus runtime. It persists immutable commands
and market events plus normalized orders, fills, positions, accounts, replay
inputs, replay outputs, and snapshots under
`outputs/dualtrack/nautilus_paper/`. A separate processed-event acknowledgement
journal makes a persisted event retryable after a replay failure. Restart
reconciliation compares the durable projections against the latest normalized
snapshot.

The factory still fails closed. Creating this adapter requires all three:

- explicit attended paper-switch approval;
- `shadow_gate_current.status=ready_for_attended_paper_switch`;
- an explicit isolated Nautilus Python path.

The current real gate is blocked at `0/7` by the latest reconciliation drift,
so `legacy_paper` remains authoritative and no configured engine has changed.

The isolated 1.230.0 bracket fixture is recorded in
`docs/dualtrack-nautilus-spike-result.md`. It proves market entry, stop fill,
fees, position closure, and realized PnL. A second shadow check constructs a
commodity perpetual from datafeed's live `instrument-definition-v1`, preserving
tick size, quantity step, multiplier provenance, margins, and currencies. These
results are evidence for the engine and instrument model, not an enabled GOLD
adapter.

### Input

- Instrument definition supplied by `datafeed`; do not hard-code tick size,
  price precision, quantity precision, or contract multiplier.
- Canonical bars or trade/quote events with source lineage.
- No fallback or synthetic events.
- Explicit fee, funding observation, slippage, and matching configuration.
- Account-observed maker/taker rates must come from a signed read-only broker
  endpoint. Public instrument metadata is not accepted as account fee proof.

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

Every child fixture and the historical residual check records its own platform
code hash and generation time. The aggregate gate passes only when all child
artifacts use the current execution, accounting, command-projection, and
promotion semantics; the pinned Nautilus `1.230.0` runtime; one exact
execution/fee contract pair; and evidence no older than seven days. Its
`generated_at` is the oldest child timestamp, so rebuilding only the aggregate
cannot make stale evidence fresh.

## Strategy Shadow execution

`services/strategy_shadow.py` is now orchestration and read-model code only. It
does not calculate grid touches, protective exits, positions, fees, or P&L.
The exact grid command projection used by the production control plane lives in
`services/strategy_plan_execution.py` and is reused to create one
`strategy-execution-scenario-v1` bundle.

The scenario binds:

- the versioned StrategyPlan and its `available_at`;
- the exact normalized command batch and start-market context;
- chronological canonical market events whose bar start is not earlier than
  plan or command availability;
- execution-precision and fee-contract hashes;
- starting-cash and leverage settings used by the replay;
- evaluation window and content-addressed scenario identity.

`NautilusStrategyShadowReplay` persists only below a
`strategy_shadow_<scenario-hash>` namespace, flushes once, reconciles the final
snapshot, and emits `nautilus-candidate-execution-receipt-v1`. The Dashboard
continues to read the latest row per variant: historical
`strategy-shadow-run-v1` rows remain trace, while new runs use
`strategy-shadow-run-v2`. A v1 row can never satisfy conformance.

The candidate receipt also carries the runtime-reported Nautilus version and
platform code hash from the replay subprocess. Promotion requires those values
to match both the current source tree and the fixed platform parity gate; a
receipt produced by old replay semantics cannot be combined with a newer gate.
The promotion token retains the parity timestamp, code hash, runtime, and
contracts and revalidates them at every call boundary, so persisting a once-pass
token cannot extend its seven-day evidence window.

Candidate conformance and platform parity are deliberately different claims:

1. Candidate evidence proves that this plan/input bundle replayed under
   Nautilus, preserved plan trace, passed adapter reconciliation, and projected
   to one `accounting-snapshot-v1`.
2. Platform evidence compares Legacy-authoritative and Nautilus-candidate
   semantics across the fixed ten-class parity suite.

Nautilus is never compared with itself as a parity proof. The active Lab
promotion boundary requires a content-valid candidate receipt and the current
passing platform fixture gate. Missing, mismatched, blocked, or legacy evidence
keeps `paper_eligible=false`; it never enables automatic application or real
money.

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
- The currently observed Binance account costs are from the configured demo
  environment. They are valid inputs for paper parity but are explicitly
  `real_money_eligible=false`; they do not prove mainnet account costs.
- Funding rate and timestamp are now observed and persisted, but a rate sample
  is not itself a funding cash-flow. Settlement must only be booked from a
  timestamped position exposure at an actual funding boundary.
- Historical `current.json` parity artifacts without child code, runtime,
  contract, and timestamp binding are intentionally stale and fail closed until
  the full ten-class fixture is rerun.
