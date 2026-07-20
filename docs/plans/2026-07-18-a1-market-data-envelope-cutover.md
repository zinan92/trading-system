# A1 Market Data Envelope Cutover Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Carry `MarketDataEnvelope` end to end through a same-response shadow comparison, add explicit session-aware freshness for non-continuous venues, and prove the first DualTrack consumer can switch without changing strategy or execution behavior.

**Architecture:** Datafeed remains the owner of provider selection, candle quality, and market-session truth. Trading Orchestrator maps one HTTP response into both the legacy DualTrack payload and the new envelope projection while `mode=shadow`; it performs no second candle fetch. `mode=authoritative` is implemented and tested but stays disabled until shadow parity is clean and the runtime datafeed is reachable.

**Tech Stack:** Python 3.13, FastAPI/Pydantic in datafeed, frozen dataclasses in Trading Orchestrator, pytest, existing Tiger `market-sessions-v1` adapter receipt.

---

## User outcome

Changing a market-data adapter no longer forces strategy, execution, or dashboard code to reinterpret provider-specific dictionaries. The system can distinguish continuous markets, an open sessioned venue, a closed venue, and unknown session state without treating any of them as interchangeable.

## Observable success criteria

1. One raw candle response produces both legacy and envelope projections; no duplicate candle HTTP call occurs.
2. Shadow mode remains authoritative on the legacy payload and emits an exact parity/drift/blocked receipt.
3. Authoritative mode fails closed on invalid schema, stale data, unknown session, closed market, source mismatch, fallback, or synthetic data.
4. Binance USD-M continuous-market fixtures remain execution-ready only under bypass + strict + exact-source policy.
5. Tiger/COMEX can report `market_open=true/false/unknown`; only an open session with a fresh bar can be execution-ready.
6. Yahoo `GC=F` remains research-only even when its last candle is recent.
7. Existing strategy, order, risk, dashboard rendering, and Nautilus attended-switch behavior remain unchanged.

## Scope

- In: versioned datafeed response semantics, market-session freshness, same-response shadow projection, one gated DualTrack consumer.
- Out: enabling Tiger order execution, changing instruments, adding fallback venues, modifying StrategyPlan, changing grid risk, starting/stopping a robot, deploying the authoritative mode.

## Gotchas

- The primary datafeed worktree contains unrelated WebSocket/proxy fixes. Work only in the isolated datafeed A1 worktree and never overwrite those files wholesale during integration.
- Market closed is not the same as stale. A closed venue must produce `market_open=false`/`market_closed`; it must not fabricate a freshness verdict.
- A session lookup failure is `session_unknown`, not permission to reuse wall-clock freshness.
- Adding optional fields without changing a schema version would hide a semantic change. Session-aware responses use `kline-candles-v2`; the orchestrator mapper accepts v1 and v2 during migration.
- `authoritative` mode is code-complete evidence, not runtime authorization.

### Task 1: Add datafeed v2 session truth

**Repository:** `/Users/wendy/.config/superpowers/worktrees/datafeed/a1-session-aware-freshness`

**Files:**
- Modify: `src/kline/models.py`
- Modify: `src/kline/provenance.py`
- Modify: `src/kline/quality.py`
- Modify: `src/kline/api.py`
- Modify: `tests/test_provenance.py`
- Modify: `tests/test_quality.py`
- Modify: `tests/test_envelope.py`
- Modify: `tests/test_live_api.py`
- Modify: `tests/test_tiger_adapter.py`

**Steps:**

1. Write failing unit tests for continuous, session-open, session-closed, and session-unknown freshness.
2. Extend `CandleResponse`/`ErrorResponse` with `continuous_market`, `market_open`, `session_status`, `session_checked_at`, and `current_session_end`; emit `schema_version=kline-candles-v2`.
3. Add optional session state to `freshness()` and `analyze_candles()`:
   - continuous: existing age rule;
   - session open: age rule applies;
   - session closed: `fresh=None`, strict reject `market_closed`;
   - session unknown: `fresh=None`, strict reject `session_unknown`.
4. In the upstream fetch path, call adapter-owned `fetch_market_sessions()` only for a non-continuous, strict, execution-venue request. Convert the receipt into UTC session state; a lookup error remains unknown.
5. Pass the already-computed report into `_build_response`; do not recompute and discard session context.
6. Run:

```bash
PYTHONPATH=src python3 -m pytest tests/test_provenance.py tests/test_quality.py tests/test_envelope.py tests/test_live_api.py tests/test_tiger_adapter.py -q
```

7. Commit:

```bash
git add src/kline/models.py src/kline/provenance.py src/kline/quality.py src/kline/api.py tests
git commit -m "feat(data): add session-aware candle freshness"
```

### Task 2: Extend the orchestrator envelope to v2

**Repository:** `/Users/wendy/.config/superpowers/worktrees/trading-orchestrator/architecture-a1-market-data-cutover`

**Files:**
- Modify: `schemas/market_data.py`
- Modify: `services/datafeed_market_mapper.py`
- Create: `tests/fixtures/datafeed/trusted_execution_candles_v2.json`
- Create: `tests/fixtures/datafeed/sessioned_execution_candles_v2.json`
- Modify: `tests/test_market_data_envelope.py`

**Steps:**

1. Write failing tests proving v1 compatibility and v2 continuous/open/closed/unknown behavior.
2. Add session fields to `MarketDataEnvelope` and its serialized form.
3. Accept only `kline-candles-v1` or `kline-candles-v2`; require explicit session fields for v2.
4. Make `execution_ready` require:
   - `continuous_market=true` and `fresh=true`; or
   - `continuous_market=false`, `market_open=true`, `session_status=open`, and `fresh=true`.
5. Keep all existing strict source/cache/quality/fallback/age requirements.
6. Run:

```bash
python3 -m pytest tests/test_market_data_envelope.py tests/test_datafeed_market_repository.py -q
```

7. Commit:

```bash
git add schemas/market_data.py services/datafeed_market_mapper.py tests/fixtures/datafeed tests/test_market_data_envelope.py
git commit -m "feat(data): support session-aware market envelopes"
```

### Task 3: Build the same-response DualTrack shadow projection

**Files:**
- Create: `services/market_data_envelope_projection.py`
- Create: `tests/test_market_data_envelope_projection.py`
- Modify: `services/dualtrack_market_feed.py`
- Modify: `tests/test_dualtrack_market_feed.py`
- Modify: `configs/pipeline.yaml`

**Steps:**

1. Write fixtures/tests for exact parity, drift, mapper-blocked, legacy-authoritative shadow, and envelope-authoritative modes.
2. Implement pure functions:

```python
def project_dualtrack_market_payload(envelope, *, requested, datafeed_url, checked_at) -> dict: ...
def compare_market_payloads(legacy: dict, candidate: dict) -> dict: ...
```

3. Add `datafeed.market_data_contract_mode` with allowed values `shadow|authoritative`; default `shadow`.
4. In `_datafeed_snapshot`, fetch candles exactly once, build the existing legacy payload, then attempt the envelope projection from that same dict.
5. In shadow mode, return the unchanged legacy payload plus `market_data_contract_shadow` receipt.
6. In authoritative mode, return the envelope projection; mapper/session failures return a blocked DualTrack payload and never fall back to the legacy interpretation.
7. Compare status, instrument, provider symbol, timeframe, bar count, timestamps, OHLCV, provider, quality flags, synthetic state, and freshness. No formatting tolerance hides semantic drift.
8. Run:

```bash
python3 -m pytest tests/test_market_data_envelope_projection.py tests/test_dualtrack_market_feed.py tests/test_dashboard_server.py -q
```

9. Commit:

```bash
git add services/market_data_envelope_projection.py services/dualtrack_market_feed.py configs/pipeline.yaml tests
git commit -m "feat(data): shadow the DualTrack envelope projection"
```

### Task 4: Prove and gate the first consumer cutover

**Files:**
- Modify: `decision-log.md`
- Modify: `docs/plans/2026-07-18-a1-market-data-envelope-cutover.md`

**Steps:**

1. Run the datafeed full suite.
2. Run the Trading Orchestrator focused market/dashboard/execution suite.
3. Run the Trading Orchestrator full suite.
4. If the local datafeed is reachable, perform one read-only Binance 1m request and capture the shadow receipt. If upstream is blocked, record the concrete error and do not manufacture live parity evidence.
5. Run Opus adversarial review across both branches, focusing on market-closed handling, double fetching, false readiness, and hidden authoritative cutover.
6. Keep `market_data_contract_mode=shadow` unless live same-response parity is proven. No config switch is permitted merely because fixtures pass.
7. Record test counts, live/blocked evidence, accepted review fixes, and Gotchas in both repos' decision logs.

## Completion boundary

A1 is complete when session semantics are explicit, the first consumer has a tested authoritative path, and the configured runtime remains shadow until real same-response parity is observed. A1 does not require or authorize starting a strategy or switching the execution engine.

## Execution record - 2026-07-18

- Task 1 complete in datafeed commits `1845f8d` and `60282c0`: v2 session
  truth, fail-closed adapter boundaries, timezone normalization, and source
  health/state separation.
- Task 2 complete in Trading Orchestrator commit `5b829ab`: v1/v2 envelope
  mapping plus continuous and sessioned readiness checks.
- Task 3 complete in commits `a346342` and `8000ee3`: one-request DualTrack
  shadow comparison, tested authoritative failure path, exception sandbox, and
  bounded diagnostic digest cost.
- Task 4 complete: datafeed `86 passed` plus Ruff; Orchestrator focused
  `152 passed`; full `1615 passed, 6 skipped`; two Opus reviews found no P0/P1.
- Live v1 same-response shadow parity passed with 40 comparisons and zero
  differences. Live v2 parity is not yet proven, so the rollout gate remains
  `market_data_contract_mode=shadow` and legacy remains authoritative.
