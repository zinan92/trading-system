# Dashboard V5 Venue–Instrument–Strategy control plane

Status: approved by Park for specification and ticketing
Issue: #1071

Parent contract: #1023 — Hyperliquid Testnet automated Grid/DCA via Testnet
Automation Coordinator

## Problem Statement

Park wants to use the existing Trading BOT/Dashboard V5 as the product control
surface. Today the backend has separate Broker, market, Strategy, Portfolio,
lifecycle, and read-model contracts, but the operator path is not yet one
clear track. Choosing an exchange, asset, or strategy can leave the next layer
ambiguous, and a chat surface can appear to own decisions that belong to the
Trading System composition root. The Dashboard must become a modular control
plane without changing the established DCA/Grid algorithms or allowing venue,
Paper, or browser state to leak into execution.

The current Hyperliquid Testnet public market binding is available, but account
facts and the complete execution-capability gate remain explicit prerequisites.
The Dashboard must expose those blockers honestly rather than borrowing Binance
Paper facts or pretending that a preview is a running strategy.

## Solution

Build one highest-level Dashboard Control Plane over the existing Trading System
Composition Root and Testnet Automation Coordinator. The product flow is:

```text
Venue Profile
  → Instrument Catalog
  → one Instrument
  → Canonical DCA or Grid
  → Strategy Configuration
  → Strategy Preview
  → Operator Confirmation
  → Testnet Automation Coordinator
```

The first execution scope is one selected perpetual Instrument and one active
Strategy Session per Testnet account. Every Hyperliquid default-perp pair stays
visible in the structural catalog, with source-bound eligibility and stable
pair-local blockers. BTC is the first bounded proof, not a hard-coded universe
restriction. Binance Paper remains a separately labelled environment; it is
never a fallback for Hyperliquid Testnet.

The Dashboard owns selection, preview presentation, confirmation, and
read-only observation. The Coordinator/runtime owns order mutation, scheduler
continuity, lifecycle progression, protection, fills, fees, reconciliation,
and durable notifications. Jessie/Telegram may continue to form a draft or
preview, but it must use the same durable control contract and cannot create a
second execution path.

## Outcome

Park can open Dashboard V5, select `Hyperliquid Testnet`, select an eligible
perpetual Instrument, choose the old DCA or Grid Strategy Family, review the
calculated requested/effective plan and maximum loss, and explicitly confirm
one bounded Testnet run. The run remains attributable and observable from the
same Dashboard without exposing credentials, mixing Paper facts, or silently
changing strategy semantics.

## Acceptance Criteria

1. The Dashboard presents explicit Venue Profiles, including
   `Hyperliquid Testnet` and `Binance Paper`; Mainnet/Live is not selectable in
   this scope.
2. Selecting a Venue Profile loads its source-bound Instrument Catalog. Every
   Hyperliquid default perpetual remains visible with eligibility, freshness,
   and stable blocker reasons; unsupported or unsafe pairs cannot be confirmed.
3. The operator can select exactly one Instrument, choose Canonical DCA or Grid,
   fill the established strategy fields without editing JSON, and see all
   derived ladder, sizing, margin, notional, and loss values before execution.
4. The Strategy Preview reads fresh, coherent market and Testnet account facts,
   applies the subtractive Portfolio Gate, and displays requested versus
   effective size, gate reasons, market quality, identity, and blockers. It
   never submits an order or creates a position.
5. `Confirm & Run` creates one immutable, digest-bound Strategy Revision tied to
   Venue Profile, Instrument, account fingerprint, Broker capability revision,
   release/runtime identity, and Testnet environment. No selection or browser
   reload can authorize a run.
6. After confirmation, the existing Coordinator/runtime owns DCA/Grid order,
   protection, fill, fee, reconciliation, scheduler, and lifecycle behavior.
   The Dashboard observes authoritative records and cannot call the Broker
   directly.
7. TP, SL, and manual interrupt produce the agreed lifecycle state and durable
   Dashboard/Telegram notification; no automatic next Plan or blind retry is
   created. Unknown remains fail-closed with a visible next action.
8. The complete flow is covered by deterministic control/read-model tests and
   an attended BTC-DCA and BTC-Grid Testnet proof; no Mainnet/Live, credentials,
   or unrelated account state is touched.

## User Stories

1. As Park, I want to select a complete Venue Profile, so that the Broker and
   environment are explicit before any asset or strategy choice.
2. As Park, I want `Hyperliquid Testnet` and `Binance Paper` shown as separate
   choices, so that the UI cannot hide an environment switch.
3. As Park, I want Mainnet/Live absent from this flow, so that a Testnet proof
   cannot accidentally become a real-money action.
4. As Park, I want the asset list to come from the selected Broker's catalog,
   so that a ticker is never mistaken for an executable Instrument identity.
5. As Park, I want every Hyperliquid default perpetual visible even when it is
   stale, thin, unmapped, or otherwise blocked, so that candidate omission is
   explainable.
6. As Park, I want each Instrument to show freshness, market quality, precision,
   and blocker status, so that I know why a pair is or is not executable.
7. As Park, I want to select one Instrument for one first-version Plan, so that
   the DCA/Grid proof remains attributable and bounded.
8. As Park, I want to choose Canonical DCA or Canonical Grid after selecting the
   Instrument, so that the Dashboard uses the existing Strategy Family rather
   than inventing a new algorithm.
9. As Park, I want DCA fields for direction, entries/ladder, sizing authority,
   leverage or risk, take profit, and stop loss, so that the old DCA contract is
   fully expressible.
10. As Park, I want Grid fields for direction, boundaries/range, rungs,
    spacing/count, sizing, exits, and Hard Stop, so that the old Grid contract
    is fully expressible.
11. As Park, I want form labels and derived values instead of raw JSON, so that
    the product understands my intent without making me fit an internal schema.
12. As Park, I want edits to reset the preview digest and require re-preview,
    so that an old approval cannot authorize a changed plan.
13. As Park, I want the preview to show gross notional, margin, stop-loss maximum
    loss, fees/slippage treatment, and Portfolio limits, so that risk is visible
    before confirmation.
14. As Park, I want to see the Strategy-requested size and Portfolio-effective
    size separately, so that a subtractive gate cannot silently rewrite my
    strategy.
15. As Park, I want the preview to show the exact Venue Profile, Instrument,
    account fingerprint, market source, and capability identity, so that the
    decision is bound to the facts I reviewed.
16. As Park, I want a first proof blocked when the Testnet account is not fresh,
    coherent, flat, or free of open orders/unknown exposure, so that the system
    does not adopt an ambiguous account.
17. As Park, I want a single explicit `Confirm & Run` action, so that choosing
    fields never accidentally submits an order.
18. As Park, I want the browser to remain usable while the runtime continues,
    so that closing or refreshing Dashboard does not stop a valid Plan.
19. As Park, I want Dashboard status to distinguish preview, confirmed,
    working, partial, protected, reconciled, paused, terminal, and blocked, so
    that “running” does not mean merely “a form was submitted”.
20. As Park, I want Pause to stop new entries while preserving protection,
    Stop to cancel entries and flatten the owned position, and Flatten to remain
    an explicit emergency action, so that control intent is unambiguous.
21. As Park, I want TP/SL/Interrupt and Unknown notifications in Dashboard and
    Telegram, so that I can decide the next Plan without hidden automation.
22. As Park, I want an Unknown state to offer reconciliation or a bounded next
    action rather than a Retry button, so that the system never guesses about a
    side effect.
23. As a Jessie/Telegram user, I want drafts and previews to use the same
    control contract, so that chat and Dashboard cannot disagree about strategy
    identity or authorization.
24. As an operator, I want Binance Paper facts to stay separate from Hyperliquid
    Testnet facts, so that a missing Testnet account cannot be masked by Paper
    NAV, positions, or fills.
25. As an evidence owner, I want the Dashboard to expose authoritative orders,
    fills, fees, positions, protection, reconciliation, blockers, and next
    action, so that browser reconstruction is unnecessary.
26. As an evidence owner, I want each receipt to bind Strategy, Revision,
    Venue, Instrument, account, environment, release, and capability identity,
    so that Testnet results remain attributable.
27. As a rollout owner, I want BTC-DCA and BTC-Grid proofs to run sequentially,
    so that the two Strategy Families are independently verified before any
    pair expansion.
28. As a rollout owner, I want pair-local blockers isolated from account-wide
    Portfolio Risk Holds, so that the minimum necessary scope is disrupted.

## Implementation Decisions

- The highest test seam is the public Dashboard Control Plane composed with the
  Trading System Composition Root and Testnet Automation Coordinator. The
  Dashboard does not introduce a second strategy engine or a direct Broker
  client.
- Venue Profile is the canonical selection identity. It binds Broker and
  Environment; the UI label must not be a generic `Hyperliquid` with a hidden
  environment.
- Instrument Catalog is loaded from the selected Venue Profile. The structural
  catalog retains all default perpetuals, while active eligibility is a
  separate source-bound projection.
- A first-version activation accepts one Instrument and one active Strategy
  Session per Testnet account. A pair can be replaced before side effects only
  inside one candidate snapshot; after submit or fill, asset switching is
  forbidden.
- Only the established Canonical DCA and Grid Strategy Families are selectable.
  Strategy owns requested entry timing, sizing, additions/reductions, exits,
  and protection intent. The Portfolio Gate may accept, scale down, or reject,
  but may never add exposure, change direction, or rewrite the Strategy.
- Strategy Configuration is a form-level representation of the canonical
  fields. It supports explicit DCA ladders or ranges/counts and explicit Grid
  geometry; internal JSON remains an implementation detail.
- Strategy Preview is deterministic and non-authorizing. It includes fresh
  market/account facts, requested/effective sizing, gross notional, margin,
  maximum loss, fee/slippage treatment, market quality, identity, and blockers.
- The first Testnet slice uses the approved bounded caps: gross notional at the
  lower of 10% of freshly reconciled equity or 100 USDC, and worst-case loss at
  the lower of 5% of freshly reconciled equity or 50 USDC. Fees, slippage, and
  the explicit risk buffer are included in worst-case loss.
- The first proof requires a coherent and fresh account, no open orders, no
  unknown exposure, and no unowned position. The account is runtime-bound and
  exposed to the Dashboard only as an alias or public fingerprint; private keys
  never enter the browser contract.
- Confirm & Run creates an immutable, digest-bound Operator Confirmation. A
  changed field, Venue Profile, Instrument, account, capability revision, or
  release requires a new preview and confirmation.
- Market-quality, precision, minimum-notional, oracle/mid/mark coherence,
  two-sided BBO/L2, and requested-size depth are execution gates. A blocked
  pair is visible but cannot be force-confirmed or silently replaced after
  side effects.
- The Coordinator/runtime remains the owner of scheduler continuity, DCA
  next-entry, Grid re-arm, protection, fills, fees, reconciliation, and
  terminal lifecycle. The browser is not a scheduler and closing it is not a
  Stop intent.
- Plan lifecycle remains non-expiring. Only strategy-level TP, SL, and manual
  interrupt terminate or pause the approved Strategy Revision. Unknown is
  fail-closed, uses query-first identity-bound reconciliation, and never
  authorizes blind retry.
- TP/SL sends a durable Dashboard/Telegram notification and enters
  `AWAITING_OPERATOR`; no new Plan, asset switch, or live promotion follows.
- Jessie/Telegram may create a Conversation Candidate or Strategy Preview, but
  it must project through the same durable control and confirmation contract.
  It cannot own a separate execution identity.
- Data Feed and Standard K-line contracts remain stable. Standard Broker is
  changed only when a separately reviewed account, Instrument, order,
  protection, fill, fee, or capability seam is missing. Trading System remains
  the composition root. Cloud/runtime remains the scheduler/deployment module.
- Cloudflare Tunnel repair is an independent operations issue. A tunnel outage
  must not weaken authorization, fallback, or execution gates.
- The Dashboard continues to project authoritative records rather than
  recalculating trading truth from browser state. Binance Paper and Hyperliquid
  Testnet read models remain explicitly separate.

## Testing Decisions

- Control-plane tests drive Venue selection, Instrument discovery, Strategy
  configuration, Preview, Gate application, Confirm & Run, status reads, and
  lifecycle controls through the highest seam. They assert external behavior,
  not private UI implementation details.
- Deterministic tests use fake market, account, Broker, capability,
  Coordinator, scheduler, clock, notification, and read-model ports. Ordinary
  test runs never require credentials, network, wall-clock sleeps, or a live
  venue.
- Catalog tests cover complete inventory retention, eligibility status,
  freshness, precision, depth, oracle dislocation, pair-local blockers, and
  no hard-coded BTC exception.
- Configuration and Preview tests cover both old DCA and old Grid field sets,
  explicit ladder/range parsing, derived quantities, requested/effective
  sizing, notional/margin/loss caps, immutable digest changes, and no hidden
  JSON requirement.
- Admission tests cover Testnet account identity, clean-state requirements,
  Paper/Testnet separation, missing account/protection capability, unknown
  exposure, and no fallback.
- Lifecycle contract tests reuse the canonical DCA/Grid characterization
  fixtures and verify that Dashboard commands do not alter existing entry,
  re-arm, protection, terminal, or no-expiry semantics.
- Failure tests cover stale/dislocated market facts, thin books, unknown
  submit/cancel/flatten, duplicate confirmation, duplicate scheduler owner,
  notification failure, browser refresh, and runtime restart. They assert
  fail-closed behavior and minimum-disruption scope.
- Read-model/browser tests cover the complete selection-to-status flow and
  authoritative projection of orders, fills, fees, positions, protection,
  reconciliation, blockers, and next action. The browser must never call a
  Broker endpoint directly.
- A separate attended external acceptance records one BTC-DCA and one BTC-Grid
  Testnet proof with exact non-secret identity and receipts. It is not ordinary
  CI, never uses Mainnet/Live, and stops on any unknown.
- Existing focused DCA/Grid, standard-broker, Portfolio, Coordinator,
  scheduler, Dashboard, compile, diff-check, and gitleaks gates remain
  required. No assertion may be weakened to accommodate the new UI path.

## Out of Scope

- Mainnet, Live, real-money execution, live credentials, automatic promotion,
  or any cross-environment fallback.
- A new DCA/Grid algorithm, changed strategy semantics, or a second Strategy
  repository.
- Concurrent multi-asset DCA/Grid execution, MACD/SRSI or other multi-asset
  alpha strategies, automatic Portfolio rotation, or automatic rebalance.
- Running Grid and DCA concurrently in one activation.
- Direct browser-to-Broker calls, venue-native types in Dashboard/control code,
  or a Dashboard-owned scheduler.
- Rebuilding Data Feed or Standard K-line, or creating another Broker/Portfolio
  repository when the existing public seams can be reused.
- Automatic adoption or flattening of unrelated/manual/unknown positions.
- Blind retry, automatic next Plan, silent asset switching, or hidden slippage
  widening.
- Treating a Dashboard reload, Recording Window boundary, or scheduler restart
  as Plan expiry.
- Cloudflare Tunnel repair, generic Cloud redesign, or unrelated UI cleanup.

## Further Notes

- This is a new control-plane contract that builds on the existing Coordinator
  execution spec; it does not rewrite that parent issue's lifecycle guarantees.
- “All pairs are candidates” means complete catalog retention and deterministic
  eligibility evaluation. It does not mean one DCA/Grid Plan owns every pair.
- The Testnet account seam and declared protection capability are prerequisites;
  a green Dashboard preview must remain blocked if either is unavailable.
- After publication, split this spec into atomic implementation issues with
  explicit blocking edges. Each issue gets one branch and one PR, with no more
  than three implementation items in progress. Run one unified Claude Sonnet
  review after all implementation issues are complete.
