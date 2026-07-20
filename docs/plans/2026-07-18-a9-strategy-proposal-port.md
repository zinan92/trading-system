# A9 Strategy Proposal Port Plan

**Status:** Complete on 2026-07-18.

**Goal:** Make the current production grid planner genuinely replaceable while
keeping validation, persistence, revision history, review provenance, and
fail-closed behavior under one trusted core service.

**Architecture:** `DualTrackMachinePlanner` remains the application service
that owns plan lifecycle. External decision logic moves behind a versioned
`StrategyProposalPort`: it receives immutable trusted context and returns an
untrusted proposal only. A frozen registry/composition root selects the
proposal adapter. The core still applies range policy, validates the machine
plan, persists it, records revisions/traces, and produces the degraded no-trade
plan on any adapter failure.

## User outcome

Park can replace the current Codex+newsletter proposal logic with a deterministic
grid planner, another model, or a What-if challenger without modifying the cycle
runner or weakening the safety rules that prevent an invalid plan from trading.

## Observable success criteria

1. `DualTrackCycleRunner` and `DualTrackMachinePlanner` no longer own a concrete
   Codex/newsletter provider branch. The provider is selected through one
   explicit, frozen proposal-plugin registry.
2. The proposal port receives versioned context containing trusted market
   summary, prior range, volatility floor, previous review, replan context, and
   bounded newsletter research; it has no order, broker, risk, or persistence
   authority.
3. The existing Codex-newsletter adapter preserves the exact prompt,
   read-only/ephemeral subprocess restrictions, JSON requirement, and error
   behavior.
4. Empty, duplicate, unknown, late-mutated, or structurally invalid proposal
   plugins fail closed. An unknown configured plugin is rejected during runner
   construction before trading artifacts are written.
5. A provider-free fake proposal plugin can complete `pre_cycle` through the
   real core service and store the same validated machine-plan contract without
   editing the runner or planner.
6. Initial plan, forced range replan, previous-review change, legacy neutral
   repair, provider failure preservation, and degraded no-trade behavior remain
   byte-compatible where identity timestamps are fixed.
7. Architecture fitness, focused planner/cycle tests, and the full repository
   regression are green with no production configuration or state mutation.

## In scope / Out of scope

- In: proposal request/port, descriptor and registry, Codex-newsletter adapter,
  explicit selection, core planner cutover, runner audit evidence, custom plugin
  and failure-path conformance, architecture score/backlog, decision log.
- Out: changing the grid prompt or trading rules, promoting a Shadow, adding a
  second production planner, changing plan/order schemas, changing execution or
  risk authority, automatic package discovery, or creating the Backtest Port.

## First-principles boundary

- External/model output is untrusted data, even when generated locally. The
  adapter may propose; it may not validate itself into authority or write the
  active plan.
- Core validation and persistence must be invariant across all proposal
  plugins. Otherwise swapping a planner would also swap safety semantics.
- The registry is frozen before a cycle begins. A plugin cannot be replaced
  between preflight and intraday replan.
- The existing callable `decision_provider` remains a test/compatibility seam,
  but it is wrapped by the same Codex-newsletter adapter contract.

## Task batches

### Batch 1 — Contract and adapter extraction

1. Define immutable `StrategyProposalRequest`, proposal port, descriptor, and
   frozen registry.
2. Move prompt construction and restricted Codex subprocess invocation into a
   concrete newsletter proposal adapter without behavior changes.
3. Register that adapter as the explicit default plugin.

### Batch 2 — Core and runner cutover

1. Make `DualTrackMachinePlanner` consume the proposal port while retaining all
   validation, range-floor, persistence, trace, revision, and audit behavior.
2. Select the configured plugin before cycle work and expose safe descriptor /
   registry fingerprint evidence.
3. Add a custom fake plugin and hostile unknown/invalid plugin acceptance tests.

### Batch 3 — Architecture closure

1. Freeze dependency direction and remove the planner seam from architecture
   fitness only after production cutover proof.
2. Re-score the Analysis/strategy row conservatively and name the next gap.
3. Run focused/full regression, changed-file Ruff, diff checks, and the Evidence
   Contract completion audit.

## Gotchas

- The newsletter is research input, not an instruction source. Its bounded,
  explicitly delimited prompt treatment must remain unchanged.
- `force=True` failure must preserve the previous locked plan; initial failure
  must persist a degraded plan with no grid orders.
- Review changes remain one-dimensional paper challengers. A proposal plugin
  cannot self-promote or bypass the minimum-cycle/trade gates.
- Chan/Signal Strategy Plugins from A8 are a different port. Do not merge signal
  generation and production plan proposal into one oversized interface.
- A9 adds no visible product surface unless runner audit evidence is surfaced
  in the Dashboard; if that scope changes, visual Evidence becomes mandatory.

## Baseline

- A8 full repository regression: `1752 passed, 7 skipped`.
- A9 planner + DT8 cycle baseline: `58 passed`.
- Current configured proposal implementation: local Codex CLI with newsletter,
  previous-review, trusted-bar, volatility, and replan context.

## Completion boundary

A9 is complete only when a custom proposal adapter can replace the configured
adapter without changing planner/core/runner code, while identical core
validation and persistence remain authoritative and all current failure/replan
semantics pass. A registry wrapped around the unchanged monolith does not count.

## Completion evidence

- `StrategyProposalRequest` is versioned, bounded, and deeply immutable.
  Proposal plugins receive market/review/volatility/replan context and only the
  external context they explicitly declare; they receive no broker, order,
  risk, persistence, or promotion authority.
- The explicit proposal registry is frozen before cycle construction and emits
  safe descriptors plus a 64-character content fingerprint. Empty, duplicate,
  unknown, late-mutated, unsupported-context, invalid factory, invalid result,
  and reserved lifecycle-field injection paths fail closed.
- The configured `codex_newsletter` adapter preserves the exact pre-A9 prompt
  hash and the same approval-never, ignore-user-config, ephemeral, read-only,
  bounded-timeout subprocess restrictions.
- A provider-free deterministic grid plugin completes the real `pre_cycle`
  path without a newsletter, core edit, runner edit, or alternate validation
  path. Unknown configuration is rejected before output artifacts exist.
- `DualTrackMachinePlanner` retains range-floor policy, machine-plan
  validation, degraded no-trade fallback, forced-replan preservation,
  persistence, revision history, traces, and audit. Untrusted proposals are
  copied through an explicit field allowlist before these rules run.
- Focused proposal/planner/fitness regression after review: `76 passed`.
  Focused A0-A9 architecture/conformance regression: `212 passed`.
- Final repository regression after the Opus finding was fixed:
  `1763 passed, 7 skipped in 378.62s`.
- Changed-file Ruff and `git diff --check`: clean. Canonical architecture
  progress is `87%`; Backtest composition is now the highest-value remaining
  architecture boundary.
- Verified Opus review used `claude-opus-4-8`, session
  `114afe14-6c34-4355-baa6-f6d9d56e8221`, receipt
  `20260718T030459Z_fac8bdbd-56f5-4c7d-9546-54fd629e2eab.json`. It found no
  P0/P1 and returned `SHIP`. Its P2 reserved-metadata finding was accepted,
  fixed with an allowlist, and covered by a hostile-plugin regression.
- A9 changes no strategy parameters, execution/risk/broker authority,
  credentials, orders, positions, accounts, or production state. It adds no
  visible UI/report surface, so new visual Evidence is not applicable under the
  trading-system Evidence Contract.
