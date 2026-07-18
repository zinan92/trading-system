# A10 Backtest Ports Plan

**Status:** In progress on 2026-07-18.

**Goal:** Make every backtest use case explicitly replaceable and provenance-
bound without pretending that signal context, strategy ranking, and exchange
execution replay share one universal payload.

**Architecture:** A10 adds one frozen plugin registry with three narrow port
kinds: signal evidence, historical strategy ranking, and Strategy Shadow
execution replay. Each composition path selects one named adapter before it
writes artifacts. Current Local, event-driven, remote, synthetic-context, and
Nautilus implementations are wrapped without changing their simulation math.

## User outcome

Park can see which backtest engine produced each result, whether that result is
historical, remote, synthetic, or execution-replay evidence, and whether it is
eligible for promotion. Swapping an implementation no longer requires editing
Daily, the strategy leaderboard pipeline, or Strategy Shadow orchestration.

## Observable success criteria

1. `pipelines/daily.py`, `pipelines/backtest_strategies.py`, and
   `pipelines/strategy_shadow_replay.py` select no concrete backtest engine.
   They consume three narrow runtime-checked ports from one frozen registry.
2. Signal evaluation uses a versioned, immutable, content-hashed request and a
   normalized `BacktestEvidence` carrying engine, evidence tier, input identity,
   degradation, and promotion-eligibility metadata.
3. Production Daily explicitly selects local historical signal evidence. It has
   no automatic remote or mock fallback. Empty history returns transparent thin
   evidence; an unavailable selected adapter fails closed.
4. Synthetic evidence exists only as an explicitly named compatibility/context
   plugin, is always degraded and promotion-ineligible, and cannot silently
   masquerade as a historical sample.
5. Historical strategy ranking and Nautilus Strategy Shadow preserve current
   results byte-for-byte apart from additive plugin/provenance evidence. Unknown,
   wrong-kind, duplicate, late-mutated, or invalid plugins fail before output.
6. The existing `BacktestClient` remains a compatibility facade, but production
   application code no longer imports it or its fallback flags.
7. Focused conformance, existing strategy/backtest/shadow behavior, architecture
   fitness, and full repository regression pass with no execution/risk/broker
   authority, strategy parameter, live state, order, or position changes.

## Milestone 1 — Signal evidence port

**User outcome:** Daily analysis shows honest, source-bound backtest context and
never fabricates historical evidence because a remote service is down.

**Success criteria:**

- Immutable `signal-backtest-request-v1` binds signal, analysis, bars, run
  context, and input hash.
- Local, remote, and synthetic-context adapters implement the same narrow port.
- Core normalization rejects identity mismatch, negative/non-finite metrics,
  unknown verdicts, and adapter-forged provenance.
- Daily selects `local_signal` explicitly; config typos fail during startup.
- Compatibility mock output is visibly degraded and promotion-ineligible.

**In scope:** contract, adapters, registry, composition, Daily cutover,
compatibility facade, conformance tests. **Out of scope:** changing local setup,
stop/target math, signal logic, or using backtest verdict as a paper-entry gate.

## Milestone 2 — Ranking and Strategy Shadow composition

**User outcome:** Both research leaderboard replay and the authoritative grid
challenger can replace their engine through config, while retaining their own
correct input/output semantics.

**Success criteria:**

- Historical strategy ranking consumes a `HistoricalStrategyBacktestPort` and
  preserves all current trade metrics and MA approximation flags.
- Strategy Shadow consumes the existing replay contract from the shared port
  module and selects Nautilus through composition rather than a direct import.
- A wrong-kind plugin is rejected even if it happens to expose a similarly named
  method.
- Plugin descriptor/fingerprint evidence is stored with leaderboard and Shadow
  results without granting authority.
- Unknown selected plugins fail before backtest/Shadow artifacts are created.

**In scope:** narrow protocols, adapters around existing engines, composition
cutover, additive audit fields, behavior tests. **Out of scope:** replacing the
Nautilus execution simulator, unifying result payloads, or changing promotion
thresholds.

## Milestone 3 — Architecture closure

**User outcome:** The progress bar reflects verified replaceability, not the
presence of cosmetic interfaces.

**Success criteria:**

- Fitness tests forbid concrete backtest imports and fallback selection in the
  three application pipelines.
- Compatibility seams and any remaining non-hexagonal backtest debt stay named.
- Focused/full tests, changed-file Ruff, diff checks, verified Opus review, and
  the Evidence Contract completion audit are clean.

**In scope:** audit score/backlog, decision log, completion evidence. **Out of
scope:** UI changes, production deployment, live trading, and strategy promotion.

## Mature-pattern decision

- NautilusTrader's official production recommendation is a config-driven
  `BacktestNode` containing explicit `BacktestRunConfig` objects. A10 follows the
  same separation of run configuration, engine construction, and results rather
  than adding another branch inside a client.
- Nautilus distinguishes high-level production runs, low-level engine control,
  and repeated-run lifecycle. A10 likewise keeps three typed use cases instead
  of hiding them behind one untyped dictionary interface.
- Current Strategy Shadow subprocess/runtime isolation remains correct: official
  guidance notes that multiple `BacktestNode`/`TradingNode` instances in one
  process are unsupported. A10 does not pull Nautilus into the main process.
- No Pluggy or automatic Python entry-point discovery is added. Explicit trusted
  registration is enough for five factories and avoids loading arbitrary
  installed code into a trading process.

References:

- <https://nautilustrader.io/docs/latest/concepts/backtesting/>
- <https://nautilustrader.io/docs/latest/getting_started/backtest_high_level/>
- <https://nautilustrader.io/docs/nightly/concepts/backtesting/apis-and-runs/>

## Task batches

### Batch 1 — Contract, registry, signal adapters

1. Define the three narrow protocols, request/result provenance, descriptor, and
   one kind-aware frozen registry.
2. Extract Local, remote, and synthetic signal-evidence adapters.
3. Add core signal-evidence normalization and production Daily composition.

### Batch 2 — Ranking and Shadow cutover

1. Wrap `StrategyBacktester` as the historical-ranking adapter and inject it into
   `backtest_strategies.py`.
2. Register `NautilusStrategyShadowReplay` and compose it in the Shadow pipeline.
3. Add custom fake, wrong-kind, unknown, invalid-result, and pre-write failure
   acceptance tests.

### Batch 3 — Closure

1. Freeze dependency direction and fallback removal in architecture fitness.
2. Re-score Backtest/replay conservatively and name remaining compatibility debt.
3. Run focused/full verification, Opus review, and Evidence Contract audit.

## Gotchas

- These are three use cases, not three names for the same thing. A single generic
  `run(dict) -> dict` port would erase type safety and make adapters harder to
  verify.
- `analysis_fallback_to_mock` currently controls Copilot and legacy Backtest
  behavior. After cutover it remains a Copilot compatibility setting only; Daily
  backtest selection comes from the explicit plugin block.
- Thin backtest evidence intentionally does not block paper sampling. A10 changes
  evidence honesty and replaceability, not that product rule.
- The price-only MA historical fallback remains marked `faithful_signals=false`.
  A registry must not upgrade an approximation into faithful evidence.
- Strategy Shadow receipts, parity, accounting, causal time boundary, and
  non-authoritative storage remain mandatory. A plugin descriptor never replaces
  those gates.
- A10 adds no visible product surface unless backtest provenance is exposed in
  the Dashboard. If that scope changes, visual Evidence becomes mandatory.

## Baseline

- A9 full repository regression: `1763 passed, 7 skipped`.
- Existing signal/local/ranking/Shadow behavior pack: `27 passed`.
- Current Daily path prefers Local when bars exist, then silently branches to
  remote/mock when they do not; current config sets `local_backtest_enabled=true`
  and `analysis_fallback_to_mock=true`.

## Completion boundary

A10 is complete only when all three application pipelines select typed backtest
implementations through the frozen registry, evidence tier and input identity
are explicit, production Daily cannot enter mock fallback, and current Local,
ranking, and Nautilus behavior remains verified. A generic registry around the
unchanged `BacktestClient` branch does not count.
