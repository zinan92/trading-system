# A11 Execution Plugin Registry Plan

**Status:** In progress on 2026-07-18.

**Goal:** Make Legacy, Nautilus, and continuous Shadow execution composition
replaceable by registration without moving attended cutover, parity, risk,
reconciliation, or real-money authority into plugins.

**Architecture:** Extract the execution port and Legacy adapter from the current
compatibility module, add a frozen factory registry, and move configured engine
selection into one trusted composition root. The old import path remains a thin
facade while production callers switch to the new root.

## User outcome

Park can add or replace a paper execution engine by registering one factory and
passing the same execution conformance suite. No application module needs a new
engine-name branch, and a plugin can never self-approve Nautilus cutover or
claim real-money eligibility.

## Observable success criteria

1. `ExecutionEngineAdapter` lives in a provider-free port module; Legacy,
   Nautilus, and Shadowing implementations live in adapter modules.
2. One frozen registry publishes stable descriptors/fingerprint and rejects
   empty, duplicate, unknown, late-mutated, malformed, or capability-incomplete
   factories.
3. Production cycle runner, Dashboard commands, strategy control plane, and
   attended cutover import only the execution composition root, not the legacy
   compatibility module or concrete adapters.
4. Config selection resolves registered capabilities rather than an
   application `if engine == ...` factory branch. Custom provider-free paper
   engines can become authoritative with `shadow=none` without editing core.
5. Attended approval, isolated-runtime requirement, seven-cycle shadow gate,
   exact override acknowledgement, paper-only restriction, and non-authoritative
   Shadow failure behavior remain unchanged and core-owned.
6. Existing Legacy/Nautilus snapshots, order IDs, fills, positions, P&L,
   reconciliation, idempotency, restart, regrid, cancellation, and rollback
   behavior remain unchanged.
7. Focused/full regressions, architecture fitness, parity-code coverage, Opus
   review, and Evidence Contract audit pass without config authority, orders,
   positions, accounts, or production-state mutation.

## Milestone 1 — Port, adapters, registry

**User outcome:** Execution implementations are physically isolated behind one
stable contract.

**Success criteria:**

- Move the Protocol to `execution_engine_port.py`.
- Move Legacy implementation and helper functions to its own adapter module.
- Register `legacy_paper`, `nautilus_paper`, and `shadowing` explicitly.
- Runtime-check every factory result and declared capability before use.
- Keep the old module as a compatibility-only re-export facade.

**In scope:** file boundary, descriptors, registry, compatibility imports.
**Out of scope:** changing any order/fill/accounting algorithm.

## Milestone 2 — Trusted composition and cutover

**User outcome:** Engine replacement is a config/registration act while money
safety remains invariant.

**Success criteria:**

- Compose the authoritative engine and optional shadow wrapper through registry
  factories.
- Evaluate cutover approval/gate/runtime policy before constructing Nautilus.
- Unknown configured plugins fail before trading artifacts are created.
- Missing/broken shadow remains visible but cannot fail authoritative Legacy.
- Production callers import only the new composition root.

**In scope:** composition, application cutover, additive plugin audit.
**Out of scope:** live broker execution, automatic cutover, or config mutation.

## Milestone 3 — Closure

**User outcome:** The progress bar measures verified replaceability rather than
a cosmetic registry.

**Success criteria:**

- Architecture fitness freezes dependency direction and explicit composition.
- The parity code hash covers the new port/registry/composition/adapter files.
- Custom plugin, wrong-kind/invalid product, gate preservation, and output
  parity tests pass.
- Canonical audit, decision log Gotchas, full suite, Opus receipt, and Evidence
  audit are complete.

**In scope:** proof and documentation. **Out of scope:** UI changes.

## Mature-pattern decision

- NautilusTrader adapters separate configuration, execution client, and factory
  modules, then register execution-client factories on the node before build.
  A11 mirrors that factory-registration boundary without importing Nautilus into
  the trusted application core.
- Nautilus execution clients own submit/cancel/reconciliation behavior, while
  the node/configuration owns selection and lifecycle. A11 likewise keeps
  attended cutover and evidence gates in composition, not adapter plugins.
- No Pluggy or Python entry-point discovery is added. Explicit trusted
  registration is sufficient for three factories and avoids arbitrary installed
  code in an execution process.

References:

- <https://nautilustrader.io/docs/latest/developer_guide/adapters/>
- <https://nautilustrader.io/docs/latest/how_to/configure_live_trading/>
- <https://nautilustrader.io/docs/latest/concepts/execution/>

## Gotchas

- A registry must not turn configuration into authority. Persisted config still
  cannot self-approve Nautilus or supply the isolated runtime path.
- Shadowing is a decorator around one authoritative port and an optional
  candidate port; it is not a third source of account truth.
- A new registry fingerprint is provenance, not parity evidence. Nautilus
  cutover still requires the existing platform/runtime/fee/precision gates.
- The parity code hash must include every moved file or a semantic change could
  retain stale promotion evidence.
- The old module has many test and compatibility imports. Removing it in A11
  would add migration risk without user value; it must become a thin facade,
  not disappear.
- This milestone has no visible surface unless execution-plugin identity is
  exposed in the Dashboard. If scope changes, visual Evidence becomes required.

## Baseline

- A10 full repository regression: `1788 passed, 7 skipped`.
- Execution/selection/Shadow/Nautilus/control-plane/fitness pack:
  `69 passed, 1 skipped`.

## Completion boundary

A11 is complete only when a custom provider-free execution engine can be
selected through the registry, all production selection flows through the new
composition root, and every existing cutover and accounting invariant remains
verified. Re-exporting the old hard-coded factory under a new name does not
count.
