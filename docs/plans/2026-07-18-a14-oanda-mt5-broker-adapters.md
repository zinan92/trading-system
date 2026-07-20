# A14 OANDA and MT5 Broker Adapter Extraction Plan

**Status:** Complete.

**Goal:** Make OANDA REST and the MT5 file bridge physically independent
`BrokerExecutionPort` adapters selected directly by the frozen broker registry,
while preserving every existing activation, dry-run, credential, artifact, and
wire behavior.

**Architecture:** Move each provider's preflight, normalized-intent translation,
submission, receipt mapping, and provider I/O into its own adapter module.
Keep `LiveBrokerAdapter` as a temporary compatibility facade which lazily
delegates old direct/private call seams. Production and operational callers
compose the concrete adapters through `BrokerPluginRegistry`.

## User outcome

Park can replace OANDA or the MT5 bridge without editing Binance, Tiger, risk,
strategy, accounting, Dashboard, or the cross-venue registry contract. The
same order intent still produces the same durable request and receipt, and no
real order or executable bridge file can bypass the existing activation gate.

## Observable success criteria

1. Standalone OANDA and MT5 classes structurally satisfy
   `BrokerExecutionPort`, expose explicit closed capabilities/descriptors, and
   import no `LiveBrokerAdapter`.
2. The frozen registry uses separate lazy factories for OANDA and MT5; only
   manual/unknown compatibility paths resolve to the legacy class.
3. OANDA owns credentials, practice/live base URL, account-ID quoting,
   instrument mapping, payload/time-in-force formatting, HTTP POST, response
   mapping, and durable request recording.
4. MT5 owns root resolution, outbox/inbox preflight, docs/templates, bridge
   request creation, durable request recording, and receipt correlation.
5. Live-disabled, failed preflight, missing/placeholder credentials, and absent
   `real_money_ready` activation fail before OANDA network or executable MT5
   outbox I/O. Dry-run semantics remain exactly unchanged.
6. Existing direct `LiveBrokerAdapter(provider=oanda_rest|mt5_file_bridge)` and
   private helper names remain thin delegates with identical outputs, so old
   tests/runbooks do not break during the strangler.
7. Operational smoke/safety/audit consumers resolve via composition; focused
   and full regressions, architecture fitness, Ruff, Opus review, and Evidence
   Contract audit pass without production mutation.

## Milestone 1 — Freeze contracts and gates

**User outcome:** Extraction is bounded by current user-visible and money-safe
behavior rather than file shape.

**Success criteria:**

- Freeze current OANDA dry-run, credentials, activation, wire, fill, and
  durable receipt behavior.
- Freeze current MT5 dry/live bridge, preflight, templates, outbox, and smoke
  receipt behavior.
- Record registry identities and direct compatibility callers.
- Establish the 63-test focused baseline.

**In scope:** behavior inventory and proof plan.
**Out of scope:** new OANDA order types, MT5 EA protocol changes, retries, or
reconciliation.

## Milestone 2 — Concrete adapters and registry cutover

**User outcome:** OANDA and MT5 become independently replaceable plug-ins.

**Success criteria:**

- Add one standalone adapter per provider.
- Preserve the shared Broker Port request/receipt contract and closed
  capability set.
- Register provider-specific factories instead of the legacy factory loop.
- Keep registry assembly lazy and secret-free.
- Migrate operational construction to the composition root.

**In scope:** physical isolation, composition, exact behavior relocation.
**Out of scope:** removing the legacy facade or changing live authority.

## Milestone 3 — Compatibility facade and conformance

**User outcome:** Existing workflows keep working while new code uses clean
boundaries.

**Success criteria:**

- Legacy direct construction delegates once to the concrete adapter.
- Preserve effective config/opener replacement and in-place config mutation.
- Pin descriptor/capability equality and concrete registry products.
- Prevent OANDA/MT5 provider logic from returning to `broker_adapter.py`.
- Prove no network/outbox write occurs before required gates.

**In scope:** compatibility and failure defense.
**Out of scope:** new provider discovery or a generalized plug-in framework.

## Milestone 4 — Closure

**User outcome:** The architectural improvement is independently credible.

**Success criteria:**

- Run focused and complete repository regressions plus Ruff.
- Obtain verified Opus adversarial review; close all P0-P2 findings.
- Update audit score, decision log, plan evidence, and Evidence Contract result.

**In scope:** proof and documentation. **Out of scope:** UI changes.

## Mature-pattern decision

- Reuse the existing Broker Port and frozen `(mode, provider, environment)`
  registry. No second service locator or transport framework is warranted.
- Follow the same incremental compatibility-facade pattern used by A12/A13:
  production composition moves first; old imports stay as thin delegates until
  callers are exhausted.
- Keep each provider adapter explicit. OANDA HTTP and MT5 filesystem delivery
  have different failure and trust models and should not share a generic
  `send(dict)` abstraction.

## Gotchas

- OANDA dry-run preflight is intentionally ready without credentials and still
  reports both missing names. Real mode must reject before constructing a POST.
- The OANDA account ID is URL-quoted and the bearer token must never appear in
  descriptors, durable requests, exception receipts, or object repr.
- `actual_size=0` historically falls back through truthiness to calculated
  quantity. Do not silently reinterpret it in a relocation milestone.
- Existing live order IDs intentionally ignore `requested_price` despite the
  private method signature. Changing that would break idempotency and receipts.
- MT5 relative inbox/outbox paths resolve against repository `ROOT`, not the
  output root. Existing runbooks and receipt importer depend on that location.
- MT5 preflight creates directories and documentation. It is an operational
  readiness mutation, not a pure query; preserve it until a separate contract
  migration.
- `dry_run=false` MT5 writes an executable-intent file only after activation;
  `dry_run=true` creates a clearly non-executable artifact without activation.
- The facade must refresh a cached delegate when the effective config object or
  opener changes, while continuing to observe in-place config mutations.
- A14 has no visible product surface. Tests, code, commits, docs, and review
  receipts are trace material, not visual Evidence.

## Baseline

- A13 full repository regression: `1822 passed, 7 skipped`.
- OANDA/MT5/Broker Port/smoke/safety/audit focused pack: `63 passed`.

## Completion boundary

A14 is complete only when the registry returns concrete OANDA and MT5 adapters,
operational callers compose them without importing the legacy class, old direct
calls still behave identically through thin delegates, and provider-specific
I/O no longer lives in `broker_adapter.py`. Copying code while leaving registry
authority or a second provider implementation in the facade does not count.

## Completion evidence

- The frozen registry now returns `OandaRestBrokerAdapter` and
  `Mt5FileBridgeBrokerAdapter` directly. Manual/unknown compatibility remains
  fail-closed, and the old OANDA/MT5 private names are AST-pinned one-return
  delegates rather than a second implementation.
- OANDA credentials, URL/account quoting, payload/TIF formatting, POST,
  response mapping, and durable request recording are provider-owned. MT5 owns
  root-relative inbox/outbox resolution, readiness templates, bridge intent,
  durable request recording, and receipt correlation.
- Negative tests prove missing credentials and missing activation block OANDA
  before the opener, and missing activation blocks executable MT5 `*.json`
  intent. Dry-run parity, order IDs, capabilities, descriptors, wire shape,
  timeout, and runtime delegate refresh remain pinned.
- Final repository regression after review hardening: `1837 passed, 7 skipped
  in 401.49s`. Focused A14 regression: `78 passed`; wider cross-provider broker
  and architecture regression: `171 passed`; post-review defense pack:
  `54 passed`. Changed-file Ruff and `git diff --check` passed.
- Verified Opus review: actual model `claude-opus-4-8`, session
  `5264e18c-30d7-453a-b0b1-8ac5c6fb1245`, receipt
  `20260718T065100Z_e61f4fc6-a8f3-4404-b220-fff5a2d67a48.json`; verdict `SHIP`,
  no P0/P1. Its P2 false-confidence finding and both P3 hardening findings were
  closed before the final regression.
- Live execution/broker improves from `92/100` to `94/100`; overall progress
  remains the reproducible `92%` (`644 / 7 = 92.0%`).
- Evidence Contract result: Visual Evidence N/A. A14 changes no visible surface
  and deploys no service; tests, commits, documents, diffs, and the Opus receipt
  are trace material only.
- No strategy parameters, risk thresholds, credentials, live configuration,
  orders, positions, accounts, or production state were changed.
