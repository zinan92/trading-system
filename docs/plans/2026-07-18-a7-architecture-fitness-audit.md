# A7 End-to-End Architecture Fitness Audit Plan

**Status:** Complete on 2026-07-18.

**Goal:** Give Park one evidence-backed answer to “How close is the full trading
system to a ports-and-adapters architecture?” and freeze the boundaries already
won in A0-A6 so future feature work cannot silently couple core trading logic
back to Binance, Tiger, Nautilus, Legacy, HTTP, SQLite, or Dashboard code.

**Architecture:** A7 adds no trading runtime service and changes no strategy,
risk, order, account, broker, or market-data authority. It documents the
end-to-end ownership map, scores seven product lines against four observable
hexagonal gates, and adds a narrow architecture-fitness test over contract
kernels and replaceable protocols. Remaining seams stay explicit instead of
being disguised with empty interfaces.

## User outcome

Park can see one honest progress bar, understand exactly which modules can be
swapped today, and know the shortest remaining path to a full plug-and-play
system. The answer distinguishes architectural modularity from live readiness
and from self-evolution maturity.

## Observable success criteria

1. One canonical audit maps Data download, Data cleaning/quality, Analysis,
   Backtest/replay, Live execution/broker, Risk/accounting/reconciliation, and
   Dashboard/read model to their contract, core, adapters, composition root,
   conformance evidence, authority, and remaining seam.
2. Every line is scored on the same four 25-point gates: versioned contract,
   adapter isolation, explicit composition, and conformance plus production
   cutover. The overall percentage is reproducible from the visible row scores.
3. Architecture-fitness tests prove the stable contract kernels import no
   concrete venue/engine adapters or network clients, and generic provider-free
   plugins structurally satisfy the Market Data, Execution, Risk, and Broker
   ports.
4. The audit names real residual coupling: strategy engine selection inside the
   registry, no first-class Backtest Port, Legacy implementation sharing the
   Execution Port module, provider accounting mappings sharing one projector,
   the large compatibility Broker adapter, and Market Data Envelope v2 still
   held in shadow mode.
5. Self-repair and strategy self-evolution receive separate readiness scores;
   neither is inflated into the core architecture percentage or described as
   closed-loop before its actuator/promotion evidence exists.
6. Focused architecture and A0-A6 conformance tests, final full regression,
   changed-file Ruff, and verified Opus review pass with no unresolved P0/P1.

## In scope / Out of scope

- In: canonical architecture audit, dependency/Protocol fitness tests, stale
  architecture-doc correction, decision log, regression, verified review.
- Out: extracting venue packages from `LiveBrokerAdapter`, inventing an empty
  Strategy/Backtest interface, changing configured data or execution authority,
  activating live money, auto-repairing processes, or auto-promoting a Shadow.

## Scoring contract

Each product line receives four independently explained scores:

| Gate | 25 points means |
|---|---|
| Contract | Versioned, provider-neutral input/output and explicit semantics |
| Isolation | Core logic has no concrete adapter/network/storage dependency |
| Composition | Adapter selection occurs at one explicit composition boundary |
| Proof/cutover | Conformance tests exist and the intended production path uses it |

Partial points are allowed only with a named residual seam. A completed A0-A7
milestone does not imply a 100% architecture score.

## Task batches

### Batch 1 — Canonical ownership and progress audit

1. Inspect current A0-A6 contracts, composition roots, tests, and configured
   authority without changing config.
2. Write one full-system ports-and-adapters map and visible scoring table.
3. Separate core modularity, live readiness, self-repair, and self-evolution.
4. Reduce the remaining architecture work to a short ordered backlog.

### Batch 2 — Executable fitness gates

1. Add an AST/import gate for the contract kernels.
2. Add provider-free fake ports proving structural plug-in compatibility.
3. Reuse existing specialized data, execution, accounting, risk, broker, and
   read-model conformance suites instead of duplicating their behavior.
4. Correct stale A5 documentation that still labels completed A6 diagnostic
   work as debt.

### Batch 3 — Closure

1. Run the focused architecture pack and full repository suite.
2. Run Ruff on every A7-changed Python file and `git diff --check`.
3. Submit the audit, tests, and threat checklist to verified Opus.
4. Fix every valid P0/P1, record Gotchas, and publish the final progress bar.

## Gotchas

- A static `Protocol` alone does not make a subsystem modular. A score receives
  composition/proof points only when a real adapter boundary and tests exist.
- Paper authority and real-money readiness are different axes. Nautilus paper
  or a Broker Port can be architecturally modular while live activation remains
  correctly blocked.
- Market Data Envelope v2 has fixture and shadow evidence, but configured mode
  remains `shadow` until real same-response v2 parity is observed. The audit
  must not call that cutover complete.
- `accounting-snapshot-v1` is canonical, but broker-specific normalization still
  lives in `accounting_projection.py`; this is a known adapter-extraction seam.
- Strategy selection still branches on engine names in `Strategy._base_engine`.
  Creating a cosmetic interface without moving composition would overstate the
  architecture, so A7 documents rather than hides it.
- The primary worktree contains unrelated Debug, range, and product changes.
  A7 remains stacked on the isolated A6 branch and changes no active runtime.
- A7 adds no visible product surface. Under the Evidence Contract, new visual
  proof is not applicable; A6 desktop/mobile captures remain the latest UI
  evidence, while A7 tests/docs are trace material.
- The first audit draft scored 84%. Opus correctly identified that the legacy
  `BacktestClient` mock fallback can still create paper evidence consumed by a
  legacy decision/reporting path. The final whole-system score is therefore the
  more conservative 83%; this is not a live-capital defect because the current
  authoritative DualTrack/Nautilus path is independent and real money remains
  disabled.
- Import bans alone do not catch inline provider dispatch. A7 now freezes the
  exact existing branch debt and regression-tests alias imports, comparisons,
  `match`, prefix calls, mapping lookup, and `get("provider")` forms.

## Completion evidence

- Final architecture progress: `83%`, reproducible from the seven visible audit
  rows; self-repair and strategy self-evolution remain separate at `58%` and
  `62%`.
- Final focused architecture/A0-A6 conformance pack: `118 passed`.
- Final full repository regression after the last fitness-gate hardening:
  `1741 passed, 7 skipped in 371.95s`.
- A7 fitness tests: `6 passed`; changed-Python Ruff and `git diff --check` are
  clean.
- Initial verified Opus used `claude-opus-4-8`, session
  `6a00e210-39c9-4119-b70a-94f37acc1baf`, receipt
  `20260718T012149Z_0bf84316-62a6-4dd2-8a7d-97ca41329b67.json`; no P0/P1 and
  `SHIP`, with two accepted P2 corrections.
- Verified follow-up Opus used `claude-opus-4-8`, session
  `36a1e86d-2935-4ac6-8007-ff8f56e0ead9`, receipt
  `20260718T013959Z_52d4ea83-6332-44d0-a820-1731f0d07ab4.json`; both P2s
  closed, no new P0/P1, and final `SHIP`. Its two test-only P3 coverage notes
  were subsequently closed with explicit regression fixtures.
- No runtime configuration, provider authority, credential, strategy, order,
  account, or production state changed in A7.

## Completion boundary

A7 is complete only when the architecture percentage can be recomputed from
the audit table, stable boundaries have executable regression gates, every
remaining seam is named with a next action, the full suite is green, and Opus
reports no unresolved P0/P1. A7 does not claim the system is fully hexagonal or
self-evolving merely because this audit is complete.
