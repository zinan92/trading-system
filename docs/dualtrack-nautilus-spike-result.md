# DualTrack NautilusTrader Spike Result

Date: 2026-07-10
NautilusTrader: 1.230.0
Status: execution fixture and live GOLD instrument construction passed; adapter not enabled

## Result

The isolated fixture submits a market BUY for one unit at 100 with an SL at 95
and TP at 105. The next 1m bar is `O=100 H=101 L=94 C=100`.

Nautilus produced:

- entry: `MARKET BUY 1 @ 100`;
- exit: `STOP_MARKET SELL 1 @ 95`;
- final position: flat;
- realized PnL: `-5.03510000 USDT`, including `0.0351 USDT` commission.

This matches the compatibility engine decisions for a bar wick crossing a stop
and for a non-gap stop fill at the trigger price.

Run the fixture in an isolated environment containing NautilusTrader:

```bash
python spikes/dualtrack_nautilus_fixture.py
```

## Live GOLD Instrument Construction

The shadow builder consumes datafeed `instrument-definition-v1` and rejects
cache, synthetic, non-execution, non-trading, and unexplained-multiplier
definitions. A live upstream response constructed:

- instrument: `XAUUSDT-PERP.BINANCE`;
- Nautilus asset class: commodity perpetual;
- price increment: `0.01`;
- size increment: `0.001`;
- contract multiplier: `1`, with USD-M derivation provenance;
- notional check: `1.000 XAU @ 4,100.00 = 4,100.00000000 USDT`.

Binance's public definition does not contain account maker/taker fee rates. The
builder therefore requires both rates explicitly; it has no fee default.

## Why This Is Not Yet The Adapter

The fixture deliberately uses NautilusTrader's packaged test perpetual. It must
not be relabeled as GOLD. A production `NautilusExecutionAdapter` needs a
canonical instrument definition from datafeed with at least:

- venue and canonical instrument ID;
- price precision and tick size;
- quantity precision, step size, and minimum quantity;
- contract multiplier and inverse/linear settlement model;
- quote, base, settlement, and margin currencies;
- initial/maintenance margin rules and leverage cap;
- maker/taker fees and any funding model.

Hard-coding these fields inside trading-orchestrator would recreate the same
split-brain data model the adapter is meant to remove.

## Next Gate

1. Add a versioned instrument-definition endpoint to datafeed.
2. Build the Nautilus instrument only from that response and preserve source
   lineage in every event.
3. Run the ten parity scenarios in the execution adapter spec.
4. Keep the local ledger authoritative for seven clean paper cycles.
5. Enable Nautilus for paper only after unexplained parity drift is zero.
