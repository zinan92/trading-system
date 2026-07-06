# Spec: Dual-Track Console DT9 — chart data integrity + page cleanup + polish

Hand-off spec, self-contained. Found during a live audit of the running console
at `http://127.0.0.1:8765/dashboard-dualtrack-v5.html` on 2026-07-06 (owner is
using this page today for real). Three phases, strict order — P0 is a safety
issue (misleading price data on a page used for real manual trading decisions
today), P1 is a scope leak, P2 is polish. Do not skip ahead: land and verify P0
before touching P1/P2.

Same constraints as prior DT specs: don't touch `pipelines/lab_run.py`,
`services/lab_registry.py`, `services/lab_r4_promotion.py`; don't touch
pre-existing uncommitted working-tree changes unrelated to this task
(`configs/strategy.yaml`, `services/technical_rule_signal_engine.py`,
`services/strategy_registry.py`, `tests/test_technical_rule_signal_engine.py`);
typed, atomic writes (`journal_store.write_json`), keep the existing suite
green.

## P0 — CRITICAL, safety: main chart renders fabricated prices as if real

### Confirmed facts (verified live, not hypothetical)

- `GET /api/dualtrack/market/bars?limit=96` (what the DT5 main chart calls)
  returned `symbol: "MGCmain"`, `provider: "synthetic_seed:dualtrack_dashboard"`,
  `quality_flags: [synthetic_seed, display_only, not_for_trading_signal]`.
  The synthetic series drifted from a close of ~4158 to ~4510 across roughly
  1h35m of bars — an ~8% move, not physically plausible for gold, and wildly
  inconsistent with every other real number on the same page (AI plan range
  4155–4220, order-ticket default price 4154.4, machine-track math).
- **Even an explicit request for the real symbol still returns synthetic**:
  `GET /api/dualtrack/market/bars?symbol=GOLD&timeframe=1m&limit=5` returned
  `symbol: "MGCmain"`, `source_mode: "synthetic_fallback"` — i.e. the
  `requested.symbol=GOLD` was silently ignored.
- At the same moment, real `GOLD`/`1m` bars in `data/market_data.db` were
  **fresh** (last bar 2026-07-06T07:19:00Z, checked at 07:25:00Z — 6 minutes
  old).
- Root cause, traced into `services/dualtrack_market_feed.py`:
  - `_candidates(symbol="GOLD", timeframe="1m")` correctly returns a single
    `{"symbol": "GOLD", ..., "source_mode": "requested_symbol"}` candidate when
    a symbol is explicitly passed (line ~30-45).
  - `_load_bars(symbol, timeframe, limit)` (line ~75) runs
    `SELECT ... WHERE symbol = ? AND timeframe = ? ORDER BY timestamp DESC
    LIMIT ?` against a **read-only URI connection**
    (`file:{resolved_path}?mode=ro`) to `self.market_db`. This query, run
    directly against the same file with a normal connection, returns real
    fresh rows (verified with a raw `sqlite3.connect` in this audit). But
    through the dashboard server process it returned empty, causing
    `snapshot()` to fall through past the `requested_symbol` candidate to
    `_derived_bars` and finally to the synthetic-seed fallback.
  - **Not yet root-caused to the exact line**: whether this is a `mode=ro` URI
    / WAL-visibility issue, a `self.market_db` path resolution mismatch
    between the running server process and the file checked in this audit
    (e.g. `TRADING_ORCHESTRATOR_MARKET_DB` env var pointing elsewhere for the
    server process), or something else. **Investigate and fix the actual
    cause** — do not paper over it by just changing candidate order.

### Required fix

1. Find and fix why `_load_bars("GOLD", "1m", N)` returns empty in the running
   server process despite real fresh rows existing in the database file it is
   configured to read. This is the priority — an explicit, correctly-specified
   request for real data must return real data when it exists and is fresh.
2. For the **default candidate order** (no explicit symbol — what the DT5 main
   chart actually calls), current order is Tiger/COMEX first, Binance/GOLD
   second, synthetic last. Tiger/MGC has **no real local coverage past
   2026-07-03** (per earlier project note) — meaning in practice this order
   currently skips a real, fresh GOLD source in favor of eventually inventing
   fake data. Until Tiger/MGC has real live coverage, prefer the real, fresh
   Binance/GOLD source over synthetic invention. (If Tiger is meant to be
   primary going forward, that's fine once it has real data — the point is
   synthetic invention must never win over an available real+fresh source.)
3. **Never present synthetic data as if it were current without a visible
   marker.** Add a hard invariant: whenever `provider` contains
   `synthetic_seed` or `source_mode` is a synthetic fallback, the frontend
   chart MUST render an unmistakable watermark/banner (e.g. "⚠ 模拟数据 · 非真实价格"
   across the chart), not silently draw it as if it were the live feed. This
   is a defense-in-depth requirement independent of fixing the root cause —
   if this ever regresses again, it must be visually obvious, not silently
   misleading.

### Tests (required)

- `GET .../market/bars?symbol=GOLD&timeframe=1m` returns real bars (not
  synthetic) when real fresh rows exist for that exact symbol/timeframe in the
  configured database.
- Default (no symbol) snapshot prefers a real, fresh source over synthetic
  fallback when at least one real source has fresh data, regardless of
  candidate order preference between real sources.
- A synthetic-fallback response is flagged in a way the frontend can key off
  unambiguously (already has `provider`/`source_mode` fields — add a
  frontend-facing explicit boolean like `is_synthetic: true` if that's cleaner
  than string-matching provider).
- Frontend: rendering a payload with `is_synthetic`/synthetic provider shows
  the warning banner (a static/DOM test asserting the banner element appears
  for a mocked synthetic payload and is absent for a real payload).

## P1 — Remove OPS/connector content from this page (scope leak, not a bug)

The right column currently renders two large panels that are internal
operations/administration state, not trading-decision information a user
needs while running the dual-track loop:

- `老虎 TIGER · PAPER VENUE` card (`dashboard-dualtrack-v5.html` ~line 314,
  `$("venueCard").innerHTML = ...`) — 接入目录/行情验证/行情门禁/价格源门/验收/
  数据源预检/执行合约/账户证据/下单演练/授权门/授权包/对账/Kill-switch/新单门.
- `接入 CONNECTOR` card (~line 353, `$("connectorCard").innerHTML = ...`) —
  平台选择/凭证/验证接入/预览激活/写入预检, plus a long readiness dump (写入预检,
  预检批次, 包检, 授权下发, 写入守卫, 切换审查, 交接单, 最终授权包, 最终审计, 运行现状,
  写后验证, 回滚证据, 备份位置, 回滚数据).

This is genuinely useful information for whoever operates the Tiger venue
onboarding/config-switch process — it belongs on the **运维 (ops)** surface,
not on the page the owner uses every 12 hours to make a trading decision.
Mixing them makes the decision page read like a backend engineering console.

### Required fix

- Remove both panels (and their DOM containers, the `venueCard`/
  `connectorCard` render calls, and the `fetch("/api/dualtrack/venue/tiger")`
  / whatever backs the connector card) from `dashboard-dualtrack-v5.html`
  entirely.
- **Do not delete the backing API endpoints or services** — they may be used
  by ops tooling elsewhere; this is a front-end-only removal from this one
  page. If there is no current ops-page consumer, leave the endpoints in
  place undisturbed (out of scope for this task to build an ops page).
- Reclaim the freed vertical space in the right column — do not leave an
  empty gap; let the remaining cards (机器轨 MACHINE, 人轨 HUMAN, 风险) use it, or
  compress the column width back down if nothing needs it.

### Test

- Static test asserting `dashboard-dualtrack-v5.html` contains no reference to
  `venueCard`, `connectorCard`, `/api/dualtrack/venue/tiger`, or 老虎/TIGER/
  CONNECTOR copy strings.

## P2 — Frontend polish (do after P0 and P1 land)

1. **Context 15m / 1h panels unreadable** — currently cramped small line
   charts with tiny floor/high/low labels. Increase panel height and chart
   font sizes so the direction/trend read is legible at a glance; this is a
   pure CSS/layout change, the underlying data (per earlier verification) is
   already correctly aggregated from real 1m bars — do not touch the data
   layer here.
2. **AI 作战单 (plan) card is a raw, unformatted data dump** — currently a
   dense inline row of labels and values with no visual hierarchy. Restructure
   to match the label/value card layout already established in
   `mockups/dualtrack-v2.html` (stacked rows, clear label above value, grouped
   sections: direction+range, key levels, invalidation, confidence, source).
   No data changes — this plan payload already has everything needed
   (direction, range, key_levels, invalidation, confidence, source, status).
3. **Large empty space below the main chart** — re-check after P0 lands; this
   was very likely a side effect of the synthetic-data chart bug (real candles
   compressed into a fraction of the chart width while a bogus trailing series
   stretched across the rest). If any gap remains after P0's fix, resolve it
   as a container/viewBox sizing issue.

### Test

- Visual/static assertions are enough here (no new invariants); keep existing
  DT5 static tests passing after the markup changes.

## Sequencing note

Land and verify P0 fully (including the frontend synthetic-data warning
banner) before starting P1. Land P1 before P2. Report back after P0 — it's the
safety-relevant one and worth a checkpoint before continuing. If P0's root
cause turns out to require a design decision (e.g. changing which venue is the
default source going forward), stop and flag it rather than deciding
unilaterally.
