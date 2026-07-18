# A8 Strategy Plugin Registry Plan

**Status:** In progress on 2026-07-18.

**Goal:** Make strategy analysis genuinely plug-and-play: a new signal engine
must enter through one explicit, testable factory registry without editing
`Strategy`, the multi-strategy runner, the daily pipeline, backtest callers, or
the Dashboard.

**Architecture:** A8 introduces a narrow `StrategyAnalysisPort`, a registry of
named plugin descriptors/factories, and one built-in composition root. Strategy
configuration continues to select an engine by a stable name, but the domain no
longer branches on `chan`, `macd`, or technical-rule names. Missing `engine`
preserves the legacy `ma` default; an explicitly unknown engine fails closed.

## User outcome

Park can add a new grid variant or a completely different analysis engine by
implementing the port and registering one factory. Existing data, risk,
execution, accounting, broker, and Dashboard modules do not need to know the
plugin's concrete class.

## Observable success criteria

1. `Strategy._base_engine` and its engine-name branch are removed; neither
   `Strategy` nor `StrategyRegistry` imports concrete analysis engines.
2. Every configured production engine resolves through one explicit built-in
   composition root and produces the same concrete engine/filter chain as
   before A8.
3. A provider-free fake plugin can be registered and executed through
   `StrategyRegistry` without editing any production registry source.
4. Duplicate, empty, explicitly unknown, or structurally invalid plugins fail
   closed with actionable errors. An omitted engine remains the compatible
   `ma` default.
5. Multi-strategy preflight exposes plugin availability and skips an enabled
   strategy whose plugin is unresolved before creating trading artifacts.
6. Architecture-fitness tests prevent the old branch or concrete-engine imports
   from returning, while focused and full regressions remain green.

## In scope / Out of scope

- In: analysis port, descriptors, explicit built-in factories, dependency
  injection, registry audit, runner preflight, conformance tests, architecture
  score/backlog update, decision log.
- Out: changing strategy parameters or enablement, loading arbitrary installed
  packages automatically, redesigning signal formulas, extracting the large
  `TechnicalRuleSignalEngine`, creating the Backtest Port, changing execution
  authority, or activating real money.

## Mature-pattern decision

- NautilusTrader separates strategy configuration from construction and offers
  `StrategyFactory` for importable strategy/config classes. A8 keeps that same
  separation but targets this repository's smaller signal-analysis boundary.
- PyPA entry points are the standard mechanism for separately distributed
  Python plugins, and Pluggy is mature when an application needs a hook
  protocol. Neither is necessary for a single factory call today.
- A trading process must not auto-load arbitrary installed code. A8 therefore
  uses explicit registration and dependency injection. Opt-in PyPA discovery
  can be added later behind a signed allowlist and startup audit without
  changing the port.

References:

- <https://nautilustrader.io/docs/python-api-nightly/config.html>
- <https://packaging.python.org/en/latest/guides/creating-and-discovering-plugins/>
- <https://pluggy.readthedocs.io/en/stable/>

## Task batches

### Batch 1 — Contract and registry

1. Define the engine-neutral generate port and plugin descriptor schema.
2. Implement normalized registration, resolution, construction, and
   structural validation with deterministic duplicate/unknown errors.
3. Compose MA, Chan, MACD, and every existing technical-rule name through lazy
   built-in factories.

### Batch 2 — Production cutover and safety

1. Inject the registry into every `Strategy` produced by `StrategyRegistry`.
2. Preserve filter wrapping after base-plugin construction.
3. Add plugin availability audit and fail-closed multi-runner preflight.
4. Prove a custom plugin needs no core edits and all production config names
   remain registered.

### Batch 3 — Architecture closure

1. Strengthen architecture fitness against engine-name branching regression.
2. Re-score Analysis/strategy and the overall architecture only from verified
   cutover evidence.
3. Run focused and full regressions, changed-file Ruff, diff checks, and the
   Evidence Contract completion audit.

## Gotchas

- A runtime-checkable `Protocol` checks method shape, not trading semantics.
  Built-ins still require behavioral parity tests through their existing
  signal/backtest suites.
- Chan imports must remain lazy because its pandas/vendor dependencies are not
  available in every job.
- Explicit unknown engines currently fall through to MA. Removing that silent
  fallback is intentional fail-closed behavior, but the production config must
  be exhaustively proven registered before cutover.
- Signal filters have their own small `_FILTERS` registry. A8 preserves it; the
  analysis engine boundary must not absorb filter composition.
- Automatic entry-point discovery is a code-execution authority surface. It is
  deliberately deferred, not forgotten.
- A8 changes no visible product surface, so new visual Evidence is not expected.
  If that changes, capture the affected operator view before completion.

## Baseline

- A7 full repository regression: `1741 passed, 7 skipped`.
- A8 focused Strategy/runner/backtest/Chan baseline: `46 passed in 227.70s`.
- Current production config contains 25 strategies across MA, Chan, MACD, and
  17 technical-rule engine names.

## Completion boundary

A8 is complete only when production and injected custom strategies both resolve
through the same port/registry, no core engine-name branch remains, unknown
plugins are blocked before execution, every configured name is proven, and the
final full suite is green. A registry class without production cutover does not
count as completion.
