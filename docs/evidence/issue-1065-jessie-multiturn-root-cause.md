# Issue #1065 — Jessie multi-turn revision root cause

Date: 2026-08-27

## Canonical production evidence

- Runtime checkout: `main@4560e9a56e5e4ba4e131cf07cf092a81a679b095`.
- Launchd owner: `com.wendy.trading-orchestrator.park-paper-control` using
  `/Users/wendy/work/trading-system-park-paper-main`.
- Telegram update `741098715` persisted `code=market_unavailable`, while its
  delivered Park-facing blocker was `Park strategy is blocked: TypeError`.
- The later clarification `82k止损 73k止盈` persisted `stop_price=73` and no
  new take-profit field. The explicit `80000 / 81000` two-level revision did
  not replace the previous three-level ladder, so the append-only merge kept
  the stale entries and count.
- Runtime receipts recorded zero created orders and zero created positions.

The exact historical line that raised the transient market `TypeError` cannot
be recovered: `default_market_reader` intentionally reduced its caught cause
to the exception class name and no traceback was persisted. A later direct
read returned a healthy trusted market envelope. The fix therefore preserves
the typed `market_unavailable` gate while preventing raw exception text from
becoming operator guidance; it does not claim the historical market read was
successful.

## Tight feedback loop

The Phase 1 temporary replay was converted into the durable regression seam:

```bash
PYTHONPATH=/Users/wendy/work/trading-system-testnet python3 -m pytest -q \
  tests/test_park_telegram_conversation.py::test_provider_outage_replays_revised_dca_geometry_without_raw_market_exception
```

Before the fix this deterministic sub-second replay failed on four exact
symptoms: raw `TypeError`, stale three-entry ladder, stale count `3`, and
`stop_price=73`. It replays the full sequence through `ParkTelegramRouter` in
an isolated pytest output root; no Telegram transport, credential, order,
position, scheduler, or external mutation is available to the harness.

After the fix the same command exits zero with:

```text
direction=short
strategy_type=dca
entry_prices=[80000.0, 81000.0]
order_count=2
maximum_leverage=10.0
stop_price=82000.0
take_profit_price=73000.0
finalize_code=market_unavailable
```

The user-facing market blocker is stable Chinese guidance and does not expose
an exception class. Market/account and exact confirmation gates remain
unchanged and fail closed.
