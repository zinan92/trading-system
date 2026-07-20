# A16 Market Envelope Authority Cutover Plan

**Status:** Complete (2026-07-18).

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
- Obtain verified Opus review, close every in-scope P0-P2, and explicitly
  backlog any cross-repository contract debt.
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
- `load_bars_between()` still requests one page capped at 60,000 bars. A longer
  interval can be incomplete if datafeed caps the response. Correct closure is
  a versioned pagination/continuation contract across both repositories, not a
  second local fetch interpretation hidden inside A16.
- No order, position, strategy, risk threshold, credential, or live process is
  mutated by A16.

## Verification evidence

- A15 full repository: `1852 passed, 7 skipped`.
- Focused market/envelope/DualTrack/architecture baseline: `99 passed`.
- Isolated live V2 rehearsal: six shadow receipts and one authoritative receipt,
  each comparing 458 fields with zero differences (`3,206` comparisons total).
- One independent request returned the known Binance upstream 502 and the
  authoritative path stayed blocked before projection. This is availability
  evidence, not a contract failure.
- Post-cutover focused market/repository/DualTrack pack: `168 passed`.
- Session-aware datafeed A1 repository: `86 passed`.
- One full repository regression after the authority and sole-mapper change:
  `1860 passed, 7 skipped in 390.67s`.
- Post-review behavior/fail-closed defense pack: `81 passed`; changed-test Ruff
  and diff checks passed.
- Verified Opus retry: `claude-opus-4-8`, session
  `70a1bdef-d104-4a01-9092-eacd5ced5958`, receipt
  `20260718T083929Z_466da7a5-defb-47ff-bb38-d82d9d624fa8.json`; verdict
  `SHIP WITH FIXES`, no P0/P1. The valid ordering/boundary and direct-502 proof
  gaps were closed with behavior-aware tests. The 60,000-bar pagination limit
  is the explicit cross-repository follow-up above.
- The first Opus invocation ended in `api_error` and was not counted as review:
  session `1593c5f8-d53e-404e-bc02-27d51f92b095`, receipt
  `20260718T083010Z_9f45d7ad-f84a-461c-a515-356f37828399.json`.
- No second full suite was run after review because the only post-review changes
  were tests and documentation. Targeted regression was the proportionate gate.

## Completion boundary

A16 is complete only when canonical authority is the envelope, every production
datafeed bar projection uses the sole mapper, explicit shadow rollback still
works, malformed or unavailable upstream data fails closed, and review/full
regression/audit pass. Fixture-only parity or a config-only flip does not count.

That boundary is satisfied for the consumer cutover. Runtime deployment of
datafeed V2, versioned long-window pagination, and eventual retirement of v1
continuous-market inference remain named follow-ups; none restores a parallel
candle interpreter or implicit shadow authority.
