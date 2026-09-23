# Complete pure Strategy package acceptance gaps after DCA/Grid extraction

## Problem Statement

The public `trading-strategy` repository contains the extracted Canonical DCA
and Grid foundation, and its current core tests pass. The independent
acceptance review found that the package contract is not yet complete enough to
serve as a durable Strategy seam:

- pure running-Grid range adjustment is still only present in the source
  Trading System;
- two exported pure algorithms lack package-side characterization coverage;
- the golden capture tool records a fixed source SHA but does not fail closed
  when the mutable source checkout is at another revision;
- the reverse boundary test checks invented fallback names instead of the real
  external DCA API symbols.

Until these gaps are closed, the package can demonstrate selected parity but
cannot make a strong, repeatable claim that the full pure DCA/Grid contract is
complete and source-anchored.

## Solution

Complete the pure Strategy contract at one highest seam: the public
`trading_strategy` API. Add the missing pure Grid range-adjustment capability,
port the relevant DCA/Grid characterization matrix, make golden capture
fail-closed against the pinned source baseline, and test the actual external
DCA boundary. Preserve the existing algorithms, schema versions, field names,
precision rules, state transitions, and risk semantics byte-for-byte wherever
the source behavior is being extracted.

The Trading System remains the composition root. No compatibility bridge or
full Trading System decoupling is part of this spec.

## User Stories

1. As a Strategy maintainer, I want the public package to expose the complete
   pure DCA and Grid contract, so that a future composition root can consume
   Strategy behavior without importing host runtime code.
2. As a DCA user, I want the deterministic candidate builder to preserve long
   and short direction semantics, so that the extracted package cannot silently
   favor one side.
3. As a DCA user, I want the half-cent and opposite-float precision boundaries
   characterized, so that browser-compatible price rounding does not drift.
4. As a DCA user, I want fixed entry levels, per-addition notional sizing,
   quantity precision, aggregate target, strategy stop, and maximum additions
   to remain unchanged, so that extraction does not redesign the strategy.
5. As a DCA lifecycle reviewer, I want target and stop terminal replays to
   remain closed with no implicit restart, so that `loop_enabled=false` remains
   enforceable.
6. As a Grid user, I want the ordinary arithmetic and geometric preview
   geometry to remain unchanged, so that levels, spacing, TP, SL, quantity,
   notional, and risk fields remain compatible.
7. As a Grid user, I want the adaptive solver's unlocked and locked behavior
   characterized, so that range, count, profit, notional, leverage, precision,
   and alternative-selection rules remain stable.
8. As a Grid operator, I want arithmetic range-edge adjustments to snap to
   whole grid steps, so that dragging a running range does not alter internal
   geometry unexpectedly.
9. As a Grid operator, I want geometric range-edge adjustments to preserve the
   active ratio, so that geometric grids remain geometrically coherent.
10. As a Grid operator, I want edge-order deduplication to consider current
    accepted entries and open positions only, so that completed lines can
    re-arm at the same price.
11. As a Grid lifecycle reviewer, I want duplicate fills, partial close,
    entry-cancel confirmation, generation changes, re-arm, Hard Stop priority,
    and terminal flatten to remain characterized, so that reconnect behavior
    stays fail-closed and idempotent.
12. As a release reviewer, I want golden capture to verify the source HEAD and
    relevant source content before emitting a fixture, so that a mutable local
    checkout cannot falsely claim parity with the pinned baseline.
13. As a release reviewer, I want the fixture receipt to bind source revision,
    relevant source hashes, fixture hash, capture command, and capture result,
    so that parity evidence can be reproduced later.
14. As a package maintainer, I want the real external DCA plan and lifecycle
    symbols to be absent from the Strategy package boundary, so that the
    current external Broker adaptation cannot become a second canonical DCA.
15. As a package maintainer, I want the boundary test to reject broker-native
    objects and forbidden imports, so that the package remains engine-neutral.
16. As a composition-root maintainer, I want the source Trading System to keep
    authorization, risk, lifecycle, read-model, and host glue ownership, so
    that this remediation does not silently expand into full decoupling.
17. As a safety reviewer, I want standard-broker, datafeed, standard-kline,
    Dashboard, Park/Telegram, Cloud, live/Testnet, credentials, and order
    mutation paths to remain untouched, so that the extraction boundary remains
    Paper/read-only.
18. As an agent maintainer, I want focused package tests to run with no new
    skips and with a readable commit history, so that future work can rely on a
    deterministic acceptance gate.
19. As a project owner, I want all remaining integration seams and source
    baseline limitations documented, so that a green package test is not
    mistaken for full Trading System integration readiness.

## Implementation Decisions

- Use the public `trading_strategy` API as the single highest seam for pure
  Strategy behavior and evidence.
- Extract the pure running-Grid range-adjustment capability and expose its
  existing contract without changing names, schema versions, field semantics,
  arithmetic/geometric behavior, deduplication, or re-arm rules.
- Preserve the source Canonical DCA candidate, preview, plan projection,
  command projection, replay, Grid sizing, adaptive solver, explicit/conditional
  replay, and line lifecycle behavior. Any source defect remains a
  `KNOWN_DEFECT`; it is not fixed as part of extraction.
- Keep all Strategy package runtime imports limited to the standard library and
  package-local modules. Plain dictionaries and value objects are allowed;
  Broker-native classes and host runtime imports are not.
- Extend the package public contract only with pure Strategy capabilities. Do
  not add a compatibility bridge to the Trading System in this issue.
- Use the pinned source baseline
  `b841800ee03fd98107063c0cbbf5144096a5c4c0` as the golden oracle. The capture
  tool must verify the source repository HEAD before importing source modules,
  and must verify relevant source files have not drifted from that baseline.
- Bind the capture receipt to the source revision, relevant source hashes,
  fixture hash, deterministic capture command, and successful diff result.
- Test actual external DCA API names and module boundaries, not invented
  placeholder names. The external Broker adaptation remains a consumer of the
  canonical foundation and never becomes a second algorithm.
- Preserve the current `BLOCKED.md` statement that the optional composition
  bridge requires a separate contract and issue.
- Keep the source checkout's existing dirty documentation changes untouched.

## Testing Decisions

- Tests assert externally visible contracts and behavior, not copied
  implementation structure. Exact golden comparisons are appropriate for
  versioned preview, plan, command, fill, and lifecycle envelopes.
- Add package-side characterization coverage for the deterministic DCA
  candidate, including both directions and known floating-point boundary cases.
- Add package-side coverage for adaptive Grid solver locks, unlocked search,
  precision limits, profit targets, leverage, stale/synthetic market rejection,
  and alternatives.
- Add package-side coverage for arithmetic and geometric range adjustment,
  edge expansion/contraction, range dragging, market bounds, deduplication,
  completed-fill re-arm, and active-position preservation.
- Retain and extend golden coverage for DCA aggregate target/stop terminal
  behavior, Grid explicit/conditional geometry, Hard Stop priority, rung
  lifecycle, duplicate-fill idempotency, re-arm, and terminal flatten.
- Run a source-oracle capture/diff check before package tests. A mismatched
  source HEAD or relevant-file digest must fail rather than regenerate a new
  baseline silently.
- Run a strict package import allowlist, I/O/network call scan,
  `python3 -m compileall`, gitleaks, and ruff when the tool exists.
- Run the source focused strategy/core regression suite after extraction. The
  known full-source baseline failures remain recorded rather than hidden or
  converted into skips.
- Follow the existing source characterization test style for DCA planning,
  Grid sizing, range geometry, and Grid lifecycle replay. New tests must not
  mock the algorithm under test or weaken assertions.

## Out of Scope

- Designing a new DCA or Grid strategy, changing any existing strategy rule,
  or making the external DCA implementation canonical.
- Adding the Trading System compatibility bridge or completing full Strategy /
  Broker / Data Feed decoupling.
- Modifying the original dirty Trading System checkout, standard-broker,
  datafeed, standard-kline, Dashboard, Park/Telegram authorization, Cloud
  runtime, protection, reconcile, or Testnet position paths.
- Network calls, credentials, live/Testnet orders, cancellations, deployment,
  Cloudflare, or broker integration.
- Adding payments, SaaS multi-tenancy, external UI, or a new runtime service.
- Rewriting the source implementation for style, removing source behavior, or
  resolving unrelated source baseline failures.

## Further Notes

- Source baseline: `b841800ee03fd98107063c0cbbf5144096a5c4c0`.
- Current public package and setup commits are already pushed to
  `zinan92/trading-strategy`.
- Source focused strategy/core regression was recorded as `135 passed, 0
  skipped`; the new package currently has `9 passed, 0 skipped` before this
  remediation.
- The full source baseline was recorded as `3349 passed, 1 skipped, 32
  failed`; those failures are outside the pure extraction scope and remain
  documented in the progress record.
- The target repository remains a public GitHub repository with GitHub Issues,
  default triage labels, and single-context domain documentation.
- This spec is the parent contract for the next `to-tickets` step. Each child
  ticket must have one branch and one PR, explicit blocking edges, and its own
  acceptance evidence.
