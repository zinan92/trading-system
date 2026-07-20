# A12 Accounting Adapter Registry Plan

**Status:** Complete on 2026-07-18.

**Goal:** Make broker accounting normalization replaceable by explicit source
registration while preserving one canonical `accounting-snapshot-v1` truth and
every current fail-honest money invariant.

**Architecture:** Separate provider-free execution accounting from broker
composition, move Binance USD-M and Tiger mappings into isolated adapters, and
select them through one frozen source-aware registry. Keep the old import path
as a compatibility-only facade while production callers move to the relevant
core or composition module.

## User outcome

Park can add a broker/account source by registering one read-only adapter and
passing the same accounting conformance suite. No risk, Dashboard, execution,
or reconciliation module needs a provider-name branch, and an adapter cannot
turn missing evidence into zero or create trading authority.

## Observable success criteria

1. A provider-free accounting projection port describes accepted source names,
   source schema, read-only capability, and canonical snapshot output.
2. One frozen registry publishes stable descriptors/fingerprint and rejects
   empty, duplicate source aliases, unknown providers, late mutation,
   malformed descriptors, false implementation identity, and invalid results.
3. Binance USD-M and Tiger mappings live in separate adapter modules; the
   provider-free accounting core imports neither and contains no provider
   selection branch.
4. Production execution consumers import the execution-accounting core;
   reconciliation and Tiger sync import only broker-accounting composition.
   The old `accounting_projection.py` is a re-export-only facade.
5. Binance/Tiger canonical payloads and snapshot IDs remain exactly equal to
   their A11 behavior, including fees, funding, unknown lifecycle, balances,
   drift, duplicate identity, and malformed-row handling.
6. Unknown/malformed broker evidence remains blocked and unknown; source
   persistence is never suppressed and no exception text leaks through the
   emergency receipt.
7. Focused/full regressions, architecture fitness, parity-code coverage, Opus
   review, and Evidence Contract audit pass without config, credential, order,
   position, account, or production-state mutation.

## Milestone 1 — Port and frozen source registry

**User outcome:** Accounting source selection has one stable, auditable plug-in
boundary.

**Success criteria:**

- Add a narrow `BrokerAccountingProjectionPort` and immutable descriptor.
- Resolve aliases by explicit registration, not inline provider conditionals.
- Freeze and content-hash the registry before first use.
- Runtime-check adapter identity and canonical snapshot result.
- Reject duplicate source ownership and late mutation.

**In scope:** contract, descriptors, registry, provenance.
**Out of scope:** changing any amount, rounding, or reconciliation rule.

## Milestone 2 — Physical adapters and production composition

**User outcome:** Replacing a venue mapper no longer changes P&L core code.

**Success criteria:**

- Isolate Binance USD-M and Tiger mappings in separate modules.
- Keep execution-engine projection provider-free.
- Keep fail-honest fallback generic and driven by registered source metadata.
- Switch production consumers to the new boundaries.
- Preserve the old module only as a no-logic compatibility facade.

**In scope:** file boundaries, selection, imports, compatibility.
**Out of scope:** broker networking, strategy/risk changes, or new venue facts.

## Milestone 3 — Economic parity and closure

**User outcome:** Modularity is proved without changing one cent of displayed
or reconciled truth.

**Success criteria:**

- Freeze exact A11-vs-A12 canonical payload and snapshot-ID parity.
- Test custom provider adapters, alias resolution, wrong products, unknown
  providers, and fallback persistence.
- Update architecture fitness and platform parity hashing.
- Close decision log, full suite, Opus review, and Evidence Contract audit.

**In scope:** proof and documentation. **Out of scope:** UI changes.

## Mature-pattern decision

- Reuse the repository's proven explicit frozen-registry pattern from strategy,
  backtest, broker, and execution composition. No new plug-in framework is
  needed for two trusted accounting adapters.
- Treat provider mapping as an anti-corruption adapter: it may interpret venue
  field names, but it must emit only the stable canonical accounting contract.
- Keep completeness and reconciliation in the result. Missing venue facts are
  epistemic state, not numeric zero.

## Gotchas

- `accounting-snapshot-v1` IDs are content hashes. Reordering lists,
  limitations, or issues is an economic compatibility change even when amounts
  look identical.
- Binance fill history cannot infer entry versus exit from the current receipt;
  trade counts must remain `None` rather than guessed.
- Tiger provides aggregate account evidence only. Orders, fills, positions,
  gross P&L, fees, funding, exposure, and ending cash remain unknown.
- Fail-honest projection must never block the original reconciliation/account
  sync receipt from being persisted.
- The compatibility facade must not become a second composition root.
- This milestone has no visible surface unless canonical output changes. If it
  does, visual proof and explicit product approval become required.

## Baseline

- A11 full repository regression: `1800 passed, 7 skipped`.
- Accounting/risk/reconciliation/Dashboard pack: `113 passed`.

## Completion boundary

A12 is complete only when a custom source adapter can be selected through the
registry, all production selection flows through the new composition root, and
the exact Binance/Tiger canonical snapshots remain unchanged. Moving functions
without removing the provider branch from core does not count.

## Completion evidence

- A custom provider-free broker source projects through a supplied frozen
  registry; unknown sources fail before any adapter factory is constructed.
- Binance aliases and Tiger aggregate evidence resolve through separate
  read-only adapters. Production execution consumers use the provider-free
  core, while reconciliation/account sync use the sole composition root.
- Frozen A11 snapshot IDs remain exact for both Binance and Tiger. Git
  verbatim-relocation diffs for execution core, Binance, Tiger, and broker
  counts all returned `exit=0` with no differences.
- The platform parity hash covers every core/registry/composition/adapter
  semantic file; risk evaluator identity now hashes the provider-free core.
- Final repository regression: `1813 passed, 7 skipped in 400.54s`.
- Verified Opus review: `SHIP`, no P0-P2. Its one actionable P3 architecture
  guardrail was applied and reverified.
- Evidence Contract result: Visual Evidence N/A. A12 changes no visible surface
  and deploys no local service; tests, commits, documents, diffs, and the Opus
  receipt are trace material only.
