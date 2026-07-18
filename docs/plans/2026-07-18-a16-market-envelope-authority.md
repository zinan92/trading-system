# A16 Market Envelope Authority Cutover Plan

**Status:** In progress.

**Goal:** Make the versioned `MarketDataEnvelope` the authoritative production
interpretation for market reads, remove the duplicate raw-candle mapping path,
and preserve explicit fail-closed behavior for invalid contracts and upstream
outages.

**Architecture:** Keep datafeed as the owner of source selection, session truth,
quality, cache, and fallback policy. Trading Orchestrator accepts data only
through the existing v1/v2 mapper, then exposes the existing `Bar` value object
to callers that do not need the trust header. No new candle model, fallback
service, or second fetch is introduced.

## User outcome

Park can swap Binance for another conforming source without rechecking every
strategy, chart, or historical reader: one adapter boundary validates source,
session, freshness, provenance, and OHLCV before any production consumer sees
bars.

## Milestone 1 — Freeze real V2 cutover evidence

**User outcome:** Authority changes only after real, same-response evidence.

**Success criteria:**

- Run the session-aware datafeed commit on an isolated port and temporary DB.
- Use real Binance USD-M XAUUSDT 1m responses; do not substitute fixtures.
- Prove at least five successful shadow samples with zero legacy/envelope drift.
- Prove one authoritative read returns the envelope projection and remains
  execution-ready.
- Treat upstream 502 as availability evidence and verify it remains blocked;
  never count a blocked request as parity.

**In scope:** read-only local rehearsal and receipts.
**Out of scope:** restarting production datafeed or a trading strategy.

## Milestone 2 — Switch the first production consumer

**User outcome:** DualTrack/Dashboard market truth comes from the typed envelope,
not a parallel raw-dict interpretation.

**Success criteria:**

- Set the canonical pipeline mode to `authoritative`.
- Make authoritative the safe default when a custom config omits the mode.
- Keep explicit `shadow` as a diagnostic rollback mode, never an implicit
  authority fallback.
- Preserve one HTTP request per snapshot and the current blocked payload on
  upstream or contract failure.
- Freeze config/default/cutover behavior in focused tests.

**In scope:** DualTrack market composition and canonical config.
**Out of scope:** changing instruments, sources, freshness thresholds, or UI.

## Milestone 3 — Remove duplicate repository interpretation

**User outcome:** Every production datafeed bar read passes the same contract
validator before reaching analysis, replay, health, or Lab consumers.

**Success criteria:**

- Make `load_bars`, range reads, latest reads, and point-in-time reads project
  from `load_envelope()`.
- Delete the raw response-to-`Bar` comprehension from the repository.
- Preserve request policy, `Bar` shape, ordering, start/end semantics, provider,
  and quality flags for conforming v1/v2 responses.
- Reject malformed HTTP-200 payloads instead of returning partial bars.
- Keep temporary SQLite stores isolated to tests/migration rehearsals.

**In scope:** `DatafeedMarketRepository` and its ports/tests.
**Out of scope:** deleting `MarketStore` test/replay support.

## Milestone 4 — Adversarial closure

**User outcome:** The cutover is reversible, testable, and honest about remaining
availability/deployment risk.

**Success criteria:**

- Run focused market, Dashboard, architecture, and full repository suites.
- Run the datafeed session-contract suite against its A1 worktree.
- Obtain verified Opus review and close every valid P0-P2.
- Update the progress audit, decision log, Gotchas, and exact remaining gaps.
- Keep Visual Evidence N/A because no visible surface changes.

**In scope:** proof, review, audit, and documentation.
**Out of scope:** claiming the intermittent Binance 502 is repaired by this
contract cutover.

## Mature-pattern decision

- Reuse the existing anti-corruption mapper and typed envelope, following the
  established ports-and-adapters boundary. Do not add another repository,
  schema, event bus, or provider switch.
- Preserve v1 acceptance during rollout; v2 adds explicit session truth. The
  consumer rejects unknown future versions.
- Keep shadow mode only as an explicit operational rollback/diagnostic. It may
  compare interpretations but cannot silently become authoritative.

## Gotchas

- Contract parity and upstream availability are different truths. A 502 can
  coexist with perfect same-response parity on successful requests.
- Binance is continuous; Tiger/COMEX is sessioned. V1 inference is retained for
  migration, while v2 must carry explicit session fields.
- Historical/research reads may legitimately be non-execution-ready. They still
  require a valid envelope and must not be rejected merely for `fresh=null`.
- Datafeed A1 code lives at commit `92e5e8c`; production port 8100 still served
  v1 during the rehearsal. Authority can accept v1 and v2, but v2 deployment is
  tracked separately from the consumer cutover.
- No order, position, strategy, risk threshold, credential, or live process is
  mutated by A16.

## Baseline and live evidence

- A15 full repository: `1852 passed, 7 skipped`.
- Focused market/envelope/DualTrack/architecture baseline: `99 passed`.
- Isolated live V2 rehearsal: six shadow receipts and one authoritative receipt,
  each comparing 458 fields with zero differences (`3,206` comparisons total).
- One independent request returned the known Binance upstream 502 and the
  authoritative path stayed blocked before projection. This is availability
  evidence, not a contract failure.

## Completion boundary

A16 is complete only when canonical authority is the envelope, every production
datafeed bar projection uses the sole mapper, explicit shadow rollback still
works, malformed or unavailable upstream data fails closed, and review/full
regression/audit pass. Fixture-only parity or a config-only flip does not count.
