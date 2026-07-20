# A0 Market Data Envelope Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add one versioned market-data trust envelope with a frozen header and batch membership, plus a tested datafeed adapter path, without changing any existing production reader, strategy, order, risk, dashboard, or Nautilus cutover behavior.

**Architecture:** Keep the existing `Bar` as the only in-process OHLCV value object and wrap batches in `MarketDataEnvelope`; do not add a fourth candle representation. Translate the upstream `kline-candles-v1` HTTP payload in one adapter mapper. Expose the new path as an opt-in repository method while the existing `load_bars()` compatibility path remains byte-for-byte behaviorally unchanged until A1.

**Tech Stack:** Python 3.13, frozen dataclasses, standard library validation, pytest, existing datafeed HTTP contract.

---

## Invariants

1. `MarketDataEnvelope.schema_version` belongs to the transport/domain boundary; individual `Bar` instances do not carry a schema version.
2. The envelope preserves all trust evidence from an accepted response. Synthetic or rejected responses remain structured HTTP errors and fail before a trusted envelope is created.
3. Execution readiness is an explicit derived decision; research/display data is not silently promoted to executable data.
4. Existing `DatafeedMarketRepository.load_bars*`, `load_latest_*`, `MarketStore`, strategy, execution, and dashboard behavior is unchanged in A0.
5. Invalid HTTP-200 contract payloads fail closed through a dedicated adapter error and never produce a partial envelope; non-200 upstream errors remain `DatafeedUnavailable` with their complete response body.

### Task 1: Freeze success and rejection fixtures

**Files:**
- Create: `tests/fixtures/datafeed/trusted_execution_candles_v1.json`
- Create: `tests/fixtures/datafeed/research_candles_v1.json`
- Create: `tests/fixtures/datafeed/upstream_error_v1.json`
- Create: `tests/test_market_data_envelope.py`

**Step 1: Add representative immutable JSON fixtures**

The trusted fixture must contain every `kline.models.CandleResponse` trust field and one XAUUSDT 1m candle. The research fixture must be real but `execution_venue=false`, `fresh=null`, and use Yahoo futures provenance. The error fixture must preserve the Binance upstream error shape, including an empty provider detail and `access_issues`.

**Step 2: Write failing contract tests**

Cover:

```python
def test_trusted_execution_fixture_round_trips_without_losing_trust_fields(): ...
def test_research_fixture_is_real_but_not_execution_ready(): ...
def test_envelope_collections_are_immutable_tuples(): ...
def test_unknown_envelope_schema_fails_closed(): ...
```

**Step 3: Run the tests and prove they fail**

Run:

```bash
python3 -m pytest tests/test_market_data_envelope.py -q
```

Expected: collection/import failure because `MarketDataEnvelope` does not exist.

**Step 4: Commit only fixtures and failing tests after the implementation task turns them green**

The failing state is evidence during TDD but must not be left as a branch commit.

### Task 2: Add the immutable domain envelope

**Files:**
- Modify: `schemas/market_data.py`
- Test: `tests/test_market_data_envelope.py`

**Step 1: Implement the minimal frozen model**

Add a `MarketDataEnvelope` frozen dataclass wrapping `tuple[Bar, ...]` plus the upstream trust header. Use tuples for `attempted_sources`, `quality_flags`, and `access_issues`. Existing `Bar.quality_flags` remains a compatibility list in A0; the immutable envelope-level flags are the authoritative trust evidence.

Required public behavior:

```python
MARKET_DATA_ENVELOPE_SCHEMA = "market-data-envelope-v1"

@dataclass(frozen=True)
class MarketDataEnvelope:
    schema_version: str
    upstream_schema_version: str
    instrument_id: str
    provider_symbol: str
    asset_class: str
    timeframe: str
    provider: str
    source_mode: str
    requested_source: str
    selected_source: str
    selection_reason: str
    attempted_sources: tuple[str, ...]
    cache_policy: str
    quality_policy: str
    fallback_policy: str
    require_execution_venue: bool
    quality_flags: tuple[str, ...]
    is_synthetic: bool
    served_from: str
    fresh: bool | None
    latest_timestamp: str | None
    age_seconds: float | None
    max_age_seconds: float | None
    execution_venue: bool
    reject_reason: str | None
    access_issues: tuple[str, ...]
    bars: tuple[Bar, ...]

    @property
    def execution_ready(self) -> bool:
        ...

    def to_dict(self) -> dict:
        ...
```

`execution_ready` is true only when data is non-synthetic, fresh is exactly true, the selected source and provider are non-empty, the source is an execution venue, no rejection is present, at least one bar exists, and the latest timestamp is present.

**Step 2: Run the pure contract tests**

Run:

```bash
python3 -m pytest tests/test_market_data_envelope.py -q
```

Expected: model construction/immutability tests pass; adapter mapping tests remain pending for Task 3.

### Task 3: Add the sole datafeed payload mapper

**Files:**
- Create: `services/datafeed_market_mapper.py`
- Modify: `tests/test_market_data_envelope.py`

**Step 1: Write failing mapper tests**

Cover exact schema validation, count mismatch, missing source/provider/instrument/timeframe, source mismatch, execution-venue mismatch, synthetic data, rejected payload, malformed OHLC, unordered timestamps, and trust-field preservation.

**Step 2: Implement the adapter mapper**

Required API:

```python
class DatafeedContractError(ValueError):
    pass

def map_candle_response(
    payload: dict,
    *,
    expected_asset_class: str,
    expected_timeframe: str,
    expected_source: str,
    require_execution_venue: bool,
) -> MarketDataEnvelope:
    ...
```

Rules:

- Accept only `kline-candles-v1`.
- Require `count == len(candles)`.
- Require a non-empty canonical `instrument_id`, `provider`, `source_mode`, and `selected_source`.
- Require `selected_source == expected_source`; A0 has no fallback policy.
- Reject `is_synthetic=true`, non-empty `reject_reason`, or an execution request served by a non-execution venue.
- Validate timestamps are timezone-aware, ordered, and unique.
- Validate finite positive OHLC, `high >= max(open, close, low)`, `low <= min(open, close, high)`, and finite non-negative volume.
- Merge response and row quality flags without losing provenance.
- Emit only `MarketDataEnvelope`, never dicts or partial `Bar` lists.

**Step 3: Run mapper tests**

Run:

```bash
python3 -m pytest tests/test_market_data_envelope.py -q
```

Expected: all envelope and mapper tests pass.

**Step 4: Commit Tasks 1–3**

```bash
git add schemas/market_data.py services/datafeed_market_mapper.py tests/test_market_data_envelope.py tests/fixtures/datafeed
git commit -m "feat(data): add trusted market data envelope"
```

### Task 4: Add the opt-in repository seam without changing old readers

**Files:**
- Modify: `services/datafeed_market_repository.py`
- Modify: `tests/test_datafeed_market_repository.py`
- Modify: `services/market_data_access.py`
- Modify: `tests/test_market_data_access.py`

**Step 1: Write failing repository tests**

Add tests proving `load_envelope()`:

- passes the route source, timeframe, and execution-venue requirement to the mapper;
- returns every trust field from the fixture;
- reports `execution_ready=true` only for the strict execution fixture;
- does not alter existing `load_bars()` or `load_latest_quote()` behavior.

**Step 2: Implement the new opt-in method**

Add to `DatafeedMarketRepository`:

```python
def load_envelope(
    self,
    symbol: str,
    timeframe: str,
    limit: int,
    *,
    start: str | None = None,
    end: str | None = None,
) -> MarketDataEnvelope:
    ...
```

It performs one existing client request and delegates all translation to `map_candle_response`. Do not make `_fetch()` call it during A0.

Add a narrow `TrustedMarketDataReadPort` Protocol for this opt-in method. Keep `MarketDataReadPort` and `market_data_repository()` return behavior unchanged.

**Step 3: Run repository and boundary tests**

Run:

```bash
python3 -m pytest \
  tests/test_market_data_envelope.py \
  tests/test_datafeed_market_client.py \
  tests/test_datafeed_market_repository.py \
  tests/test_market_data_access.py \
  tests/test_market_data_boundary.py -q
```

Expected: all pass; no production consumer uses `load_envelope()` yet.

**Step 4: Commit the repository seam**

```bash
git add services/datafeed_market_repository.py services/market_data_access.py tests/test_datafeed_market_repository.py tests/test_market_data_access.py
git commit -m "feat(data): expose opt-in trusted envelope reads"
```

### Task 5: Record the decision and verify A0

**Files:**
- Modify: `decision-log.md`
- Modify: `docs/plans/2026-07-18-a0-market-data-envelope.md`

**Step 1: Append a decision-log entry**

Record:

- Envelope versioning belongs at the batch boundary.
- `Bar` remains the single OHLCV value object.
- A0 is opt-in and deliberately does not migrate runtime consumers.
- `GC=F` and `XAUUSDT` are structurally compatible but not semantically substitutable.

Required `Gotchas`:

- `fresh=None` can be valid research/session data but is never execution-ready.
- `execution_venue=true` alone is insufficient without matching selected source and freshness.
- The current HTTP client returns raw dicts; only the mapper may convert them into the domain envelope.
- The current dirty primary worktree contains unrelated Debug/range changes and must not be overwritten or merged blindly.

**Step 2: Run focused and upstream regression**

Run:

```bash
python3 -m pytest \
  tests/test_market_data_envelope.py \
  tests/test_datafeed_market_client.py \
  tests/test_datafeed_market_repository.py \
  tests/test_market_data_access.py \
  tests/test_market_data_boundary.py \
  tests/test_dualtrack_execution_contract.py \
  tests/test_dualtrack_market_feed.py \
  tests/test_data_health.py -q
```

Expected: all pass.

**Step 3: Run the full repository suite**

Run:

```bash
python3 -m pytest -q
```

Expected: no regression relative to the clean HEAD baseline.

**Step 4: Run an adversarial Opus review**

Review for schema duplication, false execution readiness, lost provenance, hidden runtime changes, and failure to preserve the attended execution gates. Address every valid finding and rerun affected tests.

**Step 5: Commit documentation and final fixes**

```bash
git add decision-log.md docs/plans/2026-07-18-a0-market-data-envelope.md
git commit -m "docs: record A0 market data contract"
```

## A0 completion boundary

A0 is complete only when the opt-in envelope and conformance evidence are green and current production consumers remain on their original compatibility path. Migrating production reads to the envelope is A1 and requires its own shadow comparison and rollout gate.
