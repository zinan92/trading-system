# A15 Binance Execution Adapter Extraction Plan

**Status:** Complete.

**Goal:** Remove the remaining cross-venue inheritance and move Binance USD-M
order lifecycle, protection, cancellation, recovery, and mainnet authority into
a venue-owned `BrokerExecutionPort` adapter, while preserving every current
money-safety gate and legacy call seam.

**Architecture:** First make the existing Tiger paper adapter independent of
`LiveBrokerAdapter`; otherwise extracting Binance would make Tiger inherit a
Binance implementation. Then relocate the shared Binance mainnet/demo/testnet
execution body behind the existing transport, let the three Binance variants
share that venue-owned base, and leave `LiveBrokerAdapter` as a compatibility
surface for manual and old direct callers only.

## User outcome

Park can replace or evolve Binance, Tiger, OANDA, or MT5 without editing another
venue's execution code. Mainnet, demo, and testnet still enforce their distinct
authority and safety gates, and existing runbooks/tests keep the same methods
until the compatibility facade is retired separately.

## Observable success criteria

1. `TigerOpenApiPaperBrokerAdapter` satisfies `BrokerExecutionPort` without
   importing, inheriting, or constructing `LiveBrokerAdapter`; old direct Tiger
   facade calls delegate to that single implementation.
2. A concrete Binance USD-M adapter owns preflight, canonical live-risk check,
   entry payload/lifecycle, ambiguous-submit recovery, TP/SL validation,
   emergency close, cancellation, and protective recovery.
3. Mainnet registry selection returns the concrete Binance adapter. Demo and
   testnet inherit only the venue-owned Binance base and retain their separate
   endpoint, reconciliation, flatness, cap, close, and activation semantics.
4. `LiveBrokerAdapter` contains no Binance endpoint, payload, lifecycle,
   protection, recovery, or Tiger implementation. Legacy public/private seams
   remain structurally thin compatibility paths with identical behavior.
5. Missing credentials, failed reconciliation, failed canonical risk,
   missing activation, ambiguous entry responses, incomplete TP/SL, and
   protective mismatch continue to fail closed before unsafe exposure.
6. Config/opener mutation, order IDs, payload ordering, lifecycle transitions,
   journals, local mirrors, receipt shapes, and error classifications remain
   unchanged.
7. Focused/full regressions, architecture fitness, Ruff, verified Opus review,
   and Evidence Contract audit pass without production mutation.

## Milestone 1 — Freeze dependency and behavior contracts

**User outcome:** Extraction cannot quietly weaken a money gate or swap one
venue's behavior into another.

**Success criteria:**

- Inventory Tiger's inherited helper contract and direct-facade behavior.
- Freeze Binance mainnet/demo/testnet class identity, methods, payloads,
  lifecycle transitions, reconciliation, recovery, and kill-switch seams.
- Pin registry products and current forbidden dependency direction.
- Establish the 159-test focused cross-venue baseline and A14 full baseline.

**In scope:** dependency map, behavior proof, implementation boundary.
**Out of scope:** new order types, risk policy, retry policy, or live cutover.

## Milestone 2 — Independent Tiger adapter

**User outcome:** Tiger can change without importing Binance/manual compatibility
code.

**Success criteria:**

- Move Tiger preflight, dry-run translation, request persistence, and shared
  deterministic helpers into its concrete adapter.
- Preserve armed Tiger paper TradeClient behavior and fail-closed legacy
  non-dry facade behavior.
- Make the facade Tiger methods thin delegates and remove all Tiger SDK/config
  implementation from the facade.
- Prove no Tiger module imports the facade.

**In scope:** Tiger physical independence and compatibility.
**Out of scope:** Tiger live-money enablement or new account behavior.

## Milestone 3 — Venue-owned Binance execution

**User outcome:** Binance lifecycle/protection logic is independently
replaceable and shared safely by mainnet, demo, and testnet.

**Success criteria:**

- Relocate Binance execution and generic lifecycle support to one venue-owned
  adapter behind `BinanceUsdmTransport`.
- Cut mainnet registry, demo, and testnet to the concrete Binance lineage.
- Preserve subclass overrides and established opener/config/clock seams.
- Keep direct `LiveBrokerAdapter(provider=binance_usdm)` compatibility without
  a second implementation.

**In scope:** physical relocation and composition authority.
**Out of scope:** changing lifecycle policy or removing compatibility names.

## Milestone 4 — Adversarial closure

**User outcome:** The extraction is credible under real-money failure modes.

**Success criteria:**

- Add AST/import-graph guards and concrete registry conformance tests.
- Run all broker, risk, reconciliation, canary, kill-switch, lifecycle, and
  multi-strategy regressions plus the full repository.
- Obtain verified Opus review and close every P0-P2.
- Update architecture score, decision log, plan evidence, and Evidence result.

**In scope:** proof and documentation. **Out of scope:** UI changes.

## Mature-pattern decision

- Reuse the existing Broker Port, frozen registry, venue transport, lifecycle
  store, canonical risk adapter, and compatibility-facade strangler pattern.
- Do not introduce another service locator, event bus, HTTP client, or generic
  `send(dict)` abstraction.
- Share code only inside one venue lineage. Tiger must not inherit Binance
  execution merely because both submit broker orders.

## Gotchas

- Tiger's standalone file currently inherits `LiveBrokerAdapter` and relies on
  its preflight, dry-run payload, quantity/order-id, and journal helpers.
- Legacy `LiveBrokerAdapter(provider=tiger_openapi, dry_run=false)` intentionally
  raises `NotImplementedError`; the concrete Tiger paper adapter has a separately
  armed TradeClient path. Compatibility must not accidentally enable it.
- Binance demo and testnet override authority, reconciliation, signed-GET
  envelopes, recovery, and close behavior while reusing the main lifecycle.
- Existing tests and operational kill switches monkeypatch private Binance
  methods on adapter instances. Relocation must preserve those dynamic seams.
- Mainnet requires credentials, fresh same-cycle reconciliation, canonical
  risk approval, activation, and verified protective orders. Demo/testnet do
  not share mainnet activation authority.
- Ambiguous submit recovery and local `PaperExecutor` mirrors are safety state,
  not transport concerns; they must move together with the lifecycle owner.
- A15 has no visible product surface. Tests, commits, docs, diffs, and review
  receipts are trace material, not visual Evidence.

## Baseline

- A14 full repository regression: `1837 passed, 7 skipped`.
- Tiger/Binance/Broker/architecture focused baseline:
  `159 passed in 167.63s`.

## Completion boundary

A15 is complete only when Tiger imports no facade, all Binance production
variants inherit a venue-owned implementation, the mainnet registry constructs
that implementation directly, and `broker_adapter.py` contains only manual and
thin compatibility behavior. Moving method names while retaining cross-venue
inheritance or a second lifecycle implementation does not count.

## Completion evidence

- `TigerOpenApiPaperBrokerAdapter` is standalone and imports no facade.
- `BinanceUsdmBrokerAdapter` owns the complete shared Binance execution,
  lifecycle, protection, cancellation, and recovery behavior. Mainnet
  composition returns it directly; demo and testnet inherit only this
  venue-owned base.
- `LiveBrokerAdapter` is composition-only: it inherits from `object`, owns no
  Binance wire/lifecycle/protection implementation, and preserves legacy seams
  through thin delegates over the same concrete adapter.
- The configured production path cannot interpret a stored Binance
  demo/testnet label as authority: non-dry submission still fails at the
  production `real_money_ready` gate before any POST.
- Verified Opus review used `claude-opus-4-8`, session
  `abd8ceef-e3bd-4a07-8f7d-4d2a8e570339`, receipt
  `20260718T074050Z_7564bd48-7c30-461a-8b1a-5bb2d48c7c2e.json`: verdict
  `SHIP`, no P0/P1. All valid P2/P3 findings were closed with true facade
  composition, authority documentation/tests, and dry/non-dry parity proof.
- Post-review cross-provider safety pack: `192 passed`. Final full repository:
  `1852 passed, 7 skipped in 386.35s`. Changed-file Ruff and
  `git diff --check`: clean.
- Live execution/broker improves from `94/100` to `98/100`; overall architecture
  progress is the reproducible rounded `93%` (`648 / 7 = 92.6%`).
- A15 changes no visible surface and deploys no service. Under the Evidence
  Contract, Visual Evidence is not applicable; tests, commits, docs, diffs, and
  the Opus receipt are trace material only.
