# trading-strategy

Standalone, engine-neutral Strategy foundation for Canonical DCA and Grid.

Status: standalone extraction complete and merged to `main`. The next phase is
an opt-in integration seam from Trading System; this repository does not yet
replace the Strategy implementation inside Trading System.

## Source contract

The package is pinned to source baseline
`b841800ee03fd98107063c0cbbf5144096a5c4c0` from
`/Users/wendy/work/trading-system-testnet`.

The pinned contract preserves:

- Canonical DCA direction, explicit entry ladder, fixed notional sizing,
  precision rounding, maximum additions, aggregate TP, strategy SL, and
  terminal no-reopen semantics;
- Grid sizing and preview schema, adaptive solver behavior, arithmetic and
  geometric range adjustment, Hard Stop priority, rung lifecycle, duplicate
  fill idempotency, re-arm, and terminal flatten;
- existing schema versions, field names, risk flags, and error behavior.

## Package boundary

Runtime imports are limited to the Python standard library and local
`trading_strategy` modules. The package does not depend on:

- Hyperliquid, Binance, standard-broker, or native Broker types;
- datafeed or standard-kline runtime;
- Dashboard, Telegram/Park authorization, or Cloud runtime;
- network, credentials, filesystem journals, or filesystem order execution.

The composition root remains responsible for risk, authorization, execution
adapters, persistence, lifecycle orchestration, read models, and control glue.
The audited Paper/Park lifecycle modules are intentionally not copied into
this package.

## Verification

Run the package suite:

```bash
python3 -m pytest -q
python3 -m compileall -q trading_strategy tests tools
```

Reproduce the pinned golden fixture from the local Git object:

```bash
python3 tools/capture_canonical_golden.py \
  --source-ref b841800ee03fd98107063c0cbbf5144096a5c4c0 \
  | diff -u tests/fixtures/canonical_golden.json -
python3 tools/compare_pinned_source.py
```

The capture tool fails closed when the mutable source checkout is not at the
pinned HEAD or when relevant source files drift. A later source commit is not
automatically adopted as a new Strategy baseline.

Current acceptance evidence is recorded in
[PROGRESS.md](PROGRESS.md), with integration limitations in
[BLOCKED.md](BLOCKED.md). The durable golden receipt is
[canonical_golden.receipt.json](tests/fixtures/canonical_golden.receipt.json).

## Next phase

Create a separate integration spec in `trading-system-testnet` for an opt-in
Strategy import/compatibility seam. Integrate Paper/read-only DCA first, then
Grid, and prove differential behavior before deprecating any duplicate source
implementation. Do not touch the original dirty `/Users/wendy/work/trading-system`
checkout or expand into live/Testnet execution as part of this standalone repo.
