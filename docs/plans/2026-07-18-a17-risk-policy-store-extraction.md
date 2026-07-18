# A17 Risk Policy and Decision Store Extraction Plan

**Status:** Complete.

**Goal:** Make risk policy selection an explicit frozen plugin composition and
separate append-only audit persistence from evaluation, without changing one
risk threshold, blocker order, permission, or production mutation gate.

**Size:** Medium. The code movement is contained, but the boundary authorizes
exposure and therefore has high semantic blast radius.

## User outcome

Park can replace the grid risk evaluator with another conforming policy without
editing Strategy Control, brokers, accounting, or the Dashboard. Changing the
audit store cannot grant trading permission, and changing the execution engine
cannot change the economic risk answer.

## First-principles boundary

- The request binds current facts and the exact selected evaluator identity.
- The evaluator decides; it never persists, submits, cancels, or mutates plans.
- The audit store persists validated decisions; it exposes no authorization
  read and cannot evaluate policy.
- Strategy Control composes both ports once and re-evaluates fresh facts under
  the existing production mutation lock.
- Nautilus remains the final order-level defense for precision, balance,
  reduce-only, rate limits, and trading state. Portfolio/grid policy remains an
  application-level port above execution engines.

## Milestone 1 — Freeze plugin and store contracts

**User outcome:** A configured policy name has one inspectable implementation
and cannot silently fall back.

**Success criteria:**

1. `RiskDecisionPort` exposes stable evaluator metadata as well as `evaluate()`.
2. A versioned plugin descriptor records name, implementation, capabilities,
   and evaluator identity.
3. A frozen registry rejects empty, duplicate, unknown, late, malformed, and
   runtime-identity-mismatched plugins.
4. A `RiskDecisionStorePort` contains only validated `persist()` behavior; it
   exposes no permission/read API.
5. Registry and descriptor fingerprints are deterministic.

**In scope:** risk port contracts, registry, conformance tests.
**Out of scope:** changing policy math or risk limits.

## Milestone 2 — Physically isolate adapters

**User outcome:** Policy logic and audit files no longer share the canonical
port module.

**Success criteria:**

1. Move the paper-grid evaluator and its pure economics helpers into one policy
   adapter module with no journal, broker, venue, or execution-engine import.
2. Move file persistence into one audit-store adapter module.
3. Move the live-money guardrail bridge into its own adapter module while
   preserving legacy blocker order and safe-action behavior.
4. Leave `risk_port.py` responsible only for contracts, request construction,
   canonical fact projection, permission checks, and stale-decision matching.
5. Preserve every existing economic output except expected evaluator/source
   identities caused by the physical source move.

**In scope:** physical ownership and compatibility imports inside this repo.
**Out of scope:** replacing `LiveMoneyGuardrails` or changing broker gates.

## Milestone 3 — Cut production composition over

**User outcome:** Strategy Control uses the configured registered policy and a
separate store without knowing either concrete class.

**Success criteria:**

1. Add one canonical `risk_policy.paper_grid` selection with the existing
   evaluator as the default and production value.
2. `StrategyControlPlane` imports only the port plus composition root; injected
   ports/stores remain supported for tests and future adapters.
3. The exact composed evaluator metadata is included in every grid risk request
   and checked again before order mutation.
4. Empty or unknown configuration fails closed before runtime/order mutation;
   there is no implicit fallback after an explicit bad value.
5. Binance and Tiger consume the isolated live-risk bridge with unchanged
   behavior.

**In scope:** dualtrack config, composition root, Strategy Control, broker
imports, focused integration tests.
**Out of scope:** UI changes, new policies, changing active strategy parameters.

## Milestone 4 — Adversarial closure

**User outcome:** The extraction is proven behavior-preserving and does not
create a new way to bypass risk.

**Success criteria:**

1. Risk contract, evaluator, store, Strategy Control, Binance, Tiger, and
   architecture suites pass.
2. Opus reviews evaluator identity, fail-closed selection, store non-authority,
   stale-state recheck, safe exits, and broker blocker parity.
3. Run one full repository suite only after review hardening because this is a
   money-safety composition change; do not run a second full suite for
   test/document-only closure edits.
4. Update the architecture audit, decision log, Gotchas, and exact remaining
   seams.
5. Keep Visual Evidence N/A because A17 changes no visible surface.

**In scope:** verification, adversarial review, documentation.
**Out of scope:** claiming policy profitability or live-money readiness.

## Mature-pattern decision

- Reuse the repository's proven frozen registry plus explicit composition-root
  pattern already used for strategy, backtest, execution, accounting, and
  broker adapters. Do not add a service locator, dependency-injection framework,
  event bus, or second risk contract.
- Reuse Nautilus RiskEngine as the downstream order-level guard. It does not
  replace the application policy because it does not select StrategyPlans or
  own the grid's portfolio loss budget.
- Keep one file audit adapter behind a narrow port. A store registry would add
  an entity before there is a second legitimate storage implementation.

## Gotchas

- Evaluator source hashes will change when policy code moves files. That is
  correct identity drift; economic outcomes and limits must remain equal.
- A custom evaluator must not be paired with the built-in evaluator metadata.
  The request, registry descriptor, and runtime port must agree exactly.
- Safe reduce/cancel actions remain available even when entry policy is blocked.
- `current.json` remains display evidence only. No runtime may read a persisted
  decision as reusable permission.
- Live bridge results remain additive to every existing broker activation,
  preflight, reconciliation, attended, and lifecycle gate.
- Opus found a pre-existing cross-layer concern outside A17: the Dashboard
  network-order preparation reads market state before canonical action
  classification, so stale market data may block cancel/flatten. Do not change
  this casually: cancellation needs no price, while flatten may require current
  venue pricing. It is recorded as the next debug-priority audit rather than
  hidden inside this policy extraction.
- The primary worktree has unrelated user work. A17 stays in this isolated
  worktree and never copies whole files from the primary tree.

## Baseline

- A16 repository baseline: `1860 passed, 7 skipped`.
- A17 focused risk/control/broker/architecture baseline: `143 passed`.
- Overall formal architecture baseline: `95%`; Risk/accounting/reconciliation
  is `95/100` because evaluator and store still share one module.

## Pre-final verification

- Implementation-focused pack: `150 passed`; a narrower post-change subset:
  `144 passed`.
- Opus review: verified `claude-opus-4-8`, session
  `08a6d7b7-4cfe-4856-82e8-c06da714a9ea`, receipt
  `20260718T102007Z_5f754f88-a4d5-4dae-9b92-2a395686d314.json`; verdict
  `SHIP WITH FIXES`, no P0/P1/P2.
- Post-review hardening removed an unrelated source hash, removed inert runtime
  descriptor fields, required store injection in the live bridge, and made
  contradictory legacy success status fail closed. The minimal affected pack
  passed: `41 passed`; changed-file Ruff and `git diff --check` were clean.
- Registry capability callability remains explicit even though the runtime
  Protocol also checks shape: the descriptor declares executable capabilities,
  so composition validates both protocol presence and callable behavior.
- Architecture score after extraction is `668 / 7 = 95.4%`, still reported as
  `95%`; the risk/accounting/reconciliation row is now `100/100`.
- The one final repository regression passed: `1869 passed, 7 skipped in
  389.50s`. It was not repeated after documentation-only closure.

## Completion boundary

A17 is complete only when production composes a frozen, identity-bound risk
policy plugin and a separate non-authoritative decision store; all existing
entry blockers, safe exits, stale-state checks, and broker guardrails remain
equivalent; Opus and the proportionate final regression pass; and the audit
names every remaining compatibility seam.
