# Spec: Hyperliquid external execution runtime integration v1

**Spec status:** ready for ticket decomposition
**Scope:** `standard-broker` Hyperliquid Runtime Adapter and its canonical host seam
**First external environment:** Hyperliquid testnet, behind a separate approval gate
**First product:** default validator-operated perpetuals
**First proof:** one canonical order lifecycle, not DCA/Grid strategy execution

## Problem Statement

The existing `standard-broker` T01–T08 implementation establishes a provider-neutral six-port contract, Hyperliquid default-perps mappings, capability gaps, Paper fixtures, protection semantics, fee facts, reconciliation contracts, and a Paper-safe host boundary. It does not yet perform an external Hyperliquid order lifecycle.

The trading system now needs a path to actual Broker execution while keeping the boundary unambiguous:

- Hyperliquid-native fields, endpoints, signing, WebSocket behavior, account/signer mapping, order lifecycle, and Broker reconciliation must not leak into strategy or risk code.
- `trading-system` must remain the host and composition root for strategy, risk, Park authorization, Paper/testnet/live gates, Recording Track, Supervisor fail-closed behavior, and Telegram control.
- `standard-broker` must become the Broker-facing Runtime Adapter boundary without becoming a second strategy or execution engine.
- Local Paper evidence must never be represented as proof that an external Broker action occurred.

## Solution

Add a Hyperliquid external Runtime Adapter to `standard-broker`. The adapter owns canonical-to-Broker translation, capability preflight, account/signer binding, Broker lifecycle correlation, normalized receipts, and reconciliation facts. It wraps the pinned compatible Nautilus Hyperliquid implementation for low-level REST, WebSocket, signing, nonce, and execution-engine behavior.

The first runtime proof is intentionally narrow: one explicitly bound default validator-operated perpetual Broker Runtime Session must support submit, query, cancel/replace, fill, position, fee, and reconciliation semantics. The first proof does not include DCA/Grid strategy behavior.

The first external environment target is testnet, but this spec does not itself authorize testnet credentials or network calls. Testnet execution requires a separate environment approval and evidence record. Live/mainnet is a later independent milestone.

The highest test seam is the Canonical Broker Adapter Contract:

```text
canonical request or observation
  → environment and capability preflight
  → Hyperliquid Runtime Adapter
  → pinned Nautilus implementation
  → normalized receipt, provenance, lifecycle, and reconciliation fact
```

The same seam must be usable with local fake/fixture transport, a controlled external testnet transport, and future Broker implementations without changing strategy semantics.

## User Stories

1. As a trading-system owner, I want an explicitly named Hyperliquid Broker Runtime Session, so that execution never depends on an implicit default Broker.
2. As a trading-system owner, I want every Runtime Session to bind a Broker, environment, account reference, signer reference, capability profile, and lifecycle correlation, so that execution identity is auditable.
3. As a safety operator, I want Paper, testnet, and Live represented as separate environments, so that a local Paper configuration cannot silently reach an external network.
4. As a credential owner, I want signer identity separated from account identity, so that API-wallet signing and master-account queries cannot be confused.
5. As a credential owner, I want private keys and signed payloads supplied only through an out-of-band secret provider, so that credentials never enter canonical models, logs, issues, fixtures, or Recording Track.
6. As a Broker maintainer, I want the Runtime Adapter to wrap the pinned compatible Nautilus Hyperliquid implementation, so that the system does not maintain a second signing, nonce, WebSocket, or execution-engine stack.
7. As a Broker maintainer, I want Nautilus version, commit, and capability compatibility checked before invocation, so that an adapter upgrade cannot silently change execution semantics.
8. As a strategy consumer, I want the existing standard K-line to remain the only candle contract, so that Hyperliquid-native candle shapes never reach strategy code.
9. As a market-data consumer, I want canonical ticker, BBO, order book, trades, freshness, source, and execution-scope provenance, so that execution-grade market data is distinguishable from stale or ambiguous observations.
10. As an order validator, I want Hyperliquid precision, quantity step, minimum quantity, minimum notional, contract, margin, leverage, and supported order types enforced through InstrumentPort, so that invalid orders are blocked before Broker submission.
11. As an execution consumer, I want a canonical OrderIntent to be translated into a Hyperliquid request, so that strategy code never constructs provider-native payloads.
12. As an execution consumer, I want limit and IOC-limit order intent mapped with explicit time-in-force semantics, so that the Broker request is deterministic and auditable.
13. As an execution consumer, I want canonical Market intent to require fresh BBO and an explicit slippage policy, so that market-like execution does not rely on an unavailable native market primitive.
14. As an execution consumer, I want market-like intent represented as an aggressive IOC limit when required by the Broker capability profile, so that the canonical contract remains stable without pretending a native market order exists.
15. As an execution consumer, I want submit receipts to include canonical identity, Broker identity, environment, client identity, and provenance, so that every attempt can be correlated.
16. As an execution consumer, I want client order identity and idempotency preserved across retries and reconnects, so that a retry cannot create an unintended duplicate order.
17. As an execution consumer, I want cancel and replace represented as a linked lifecycle, so that the replacement order is not confused with the canceled order.
18. As an execution consumer, I want partial fills represented explicitly, so that filled quantity, remaining quantity, fees, and protection coverage remain correct.
19. As an execution consumer, I want an ambiguous submit or cancel result represented as unknown/submitting, so that a timeout cannot be treated as a safe failure.
20. As an execution consumer, I want ambiguous outcomes reconciled by query before retry, so that the adapter never performs blind duplicate submission.
21. As a resilience operator, I want REST and WebSocket observations reconciled idempotently, so that reconnect snapshots and out-of-order messages do not create duplicate fills or false terminal states.
22. As a resilience operator, I want rate limits, retryability, backoff, and non-retryable errors represented in the adapter lifecycle, so that external Broker pressure cannot turn into uncontrolled retries.
23. As a position owner, I want canonical queries for open orders, fills, positions, account, fees, and funding, so that system state is derived from Broker observations rather than strategy memory.
24. As a protection consumer, I want all close legs to be reduce-only, so that a protective action cannot accidentally increase exposure.
25. As a protection consumer, I want TP/SL market, TP/SL limit, trigger reference, grouping, quantity policy, and sibling behavior declared explicitly, so that the system does not infer Hyperliquid protection semantics.
26. As a protection consumer, I want fixed-size and position-following protection distinguished, so that current position coverage is never overstated.
27. As a protection consumer, I want partial-fill protection repair behavior declared, so that a residual position cannot be silently left unprotected.
28. As a risk owner, I want an unsupported or unknown protection capability to fail closed before increasing exposure, so that the strategy cannot continue on an unsafe approximation.
29. As an accounting consumer, I want actual maker/taker fees, rebates, funding, builder fees where applicable, fee currency, timestamps, sources, and schedule identity normalized, so that fee logic remains outside strategy code.
30. As an accounting consumer, I want actual fees separated from estimated fees, so that testnet/runtime evidence is not mistaken for a planning estimate.
31. As a Recording Track owner, I want canonical orders, fills, fees, funding, positions, account facts, provenance, and reconciliation events, so that raw Hyperliquid responses remain evidence rather than the accounting contract.
32. As a trading-system host, I want to resolve an explicit Hyperliquid Broker binding through the canonical registry, so that host composition does not import Hyperliquid-native types.
33. As a trading-system owner, I want strategy, risk, Park confirmation, Telegram, Supervisor, and environment gates to remain in `trading-system`, so that adding external execution does not weaken existing fail-closed controls.
34. As a Broker maintainer, I want no automatic Broker switching, so that a failed Hyperliquid session cannot silently route to Tiger or Binance.
35. As a test operator, I want local fake/fixture transport to exercise the complete canonical lifecycle, so that most behavior can be verified without credentials or network access.
36. As a release owner, I want external testnet evidence labelled separately from Paper evidence, so that a local test pass cannot be reported as external execution proof.
37. As a testnet operator, I want a controlled proof to record environment, account, order lifecycle, fills, fees, positions, and reconciliation, so that an external result is independently auditable.
38. As a release owner, I want Live/mainnet activation to require a separate specification, credential review, release identity, and human approval, so that testnet readiness cannot become live authority by configuration alone.
39. As a future Broker maintainer, I want the same canonical contract and conformance expectations to apply to Tiger, Binance, Hyperliquid, and future Brokers, so that adding a Broker does not change strategy semantics.

## Implementation Decisions

### Boundary ownership

- `standard-broker` owns the Hyperliquid Runtime Adapter, canonical-to-Broker mapping, capability preflight, Broker-facing account/signer binding, Broker lifecycle correlation, normalized receipts, and reconciliation facts.
- `trading-system` remains the host/composition root for strategy, risk, Park authorization, Paper/testnet/live policy, market trust and freshness gates, old-state gates, immutable-fill guard, Recording Track, Supervisor, boot, release-SHA ownership, and Telegram-only control.
- The Runtime Adapter is not a strategy engine, execution engine, bot runtime, MCP server, or control plane.
- Hyperliquid-native fields, endpoints, serialization, and lifecycle types may exist only behind the Runtime Adapter or inside the pinned compatible Nautilus implementation.

### Environment and execution stages

- The first external target is Hyperliquid testnet.
- Local Paper remains the default development and conformance environment.
- Testnet is an external environment with separate credentials, account state, asset mappings, approvals, and evidence. It is not Paper.
- Live/mainnet is a separate later milestone and is not enabled by this spec or by a testnet configuration.
- No testnet credential onboarding, network call, or external order is authorized by this spec alone.

### First vertical slice

The first implementation slice supports one explicitly bound default validator-operated perpetual and proves:

- instrument and precision validation;
- canonical market data and fresh BBO preflight;
- canonical limit/IOC-limit intent;
- submit and client identity;
- query and open-order observation;
- cancel/replace;
- fill and partial-fill observation;
- position and account observation;
- actual fee/funding fact mapping;
- ambiguous outcome handling;
- idempotent reconciliation;
- canonical Recording Track receipts.

DCA/Grid strategy lifecycle remains downstream. It consumes the verified canonical Broker lifecycle and is not used to discover low-level Broker semantics.

### Nautilus integration

- The compatible pinned Nautilus Hyperliquid adapter supplies low-level REST, WebSocket, signing, nonce, and execution-engine behavior.
- `standard-broker` must validate the declared Nautilus package/version/commit/capability surface before invocation.
- Nautilus upgrades are compatibility changes that require conformance evidence.
- The project will not add CCXT or the official Hyperliquid Python SDK as the v1 runtime layer.
- The official SDK and protocol documentation remain reference and fixture sources where useful.

### Runtime Session and credentials

- A Broker Runtime Session is explicit and immutable for its lifecycle: `broker_id`, environment, account reference, signer reference, capability profile, and lifecycle correlation.
- No default Broker, account, signer, or environment is inferred.
- Signer identity and queried account identity remain separate concepts.
- Credentials are acquired by an out-of-band secret provider at the runtime boundary. Raw credentials, private keys, signatures, and signed payloads never enter canonical data, logs, issues, fixtures, or Recording Track.

### Market and instrument behavior

- Existing standard K-line remains the canonical candle contract.
- Broker-native market data is normalized with source, Broker identity, execution scope, venue timestamp, receive timestamp, transport state, and mapping revision.
- Hyperliquid precision, quantity steps, minimum notional, contract type, margin/leverage metadata, and supported order types are validated through InstrumentPort.
- Native market execution is not assumed. Market-like canonical intent requires fresh BBO and an explicit slippage policy and is represented as an aggressive IOC limit when the capability profile requires it.

### Order lifecycle and reconciliation

- Order submission, cancellation, replacement, query, open orders, fills, positions, and reconciliation are canonical operations.
- Client identity is deterministic and idempotent across retry and reconnect paths.
- Cancel/replace preserves a canonical lineage between the original and replacement Broker order identities.
- A timeout after a request may have reached the Broker is an ambiguous lifecycle state. The adapter queries or reconciles before retrying.
- REST and WebSocket observations are merged idempotently. Duplicate fills, stale old-order events, out-of-order replacement events, and reconnect snapshots must not create duplicate accounting facts or false completion.
- Retry behavior distinguishes retryable transport/rate-limit failures from non-retryable validation, authentication, capability, and account failures.

### Protection and fail-closed behavior

- Reduce-only is mandatory for every close leg.
- TP/SL market, TP/SL limit, trigger reference, grouping, sibling cancellation, position-level behavior, fixed-size behavior, position-following behavior, cancel/replace, and partial-fill repair are explicit capability fields.
- An unsupported or unknown capability blocks the dependent path before external network I/O; nearby native operations cannot be presented as equivalent success.
- Protection coverage must be reconciled against the current owned position after fills and replacements.
- A protection update failure freezes the dependent exposure path, retries only according to the declared retry policy, and becomes a visible structural blocker after its deadline.

### Fees, account facts, and Recording Track

- AccountPort provides canonical equity/NAV, balance, margin, exposure, realized/unrealized PnL, liquidation facts, fees, and funding.
- FeePort is the only fee boundary. Maker/taker fees, rebates, funding, builder fees where applicable, fee currency, timestamp, source, schedule identity, and actual-versus-estimated state remain explicit.
- `standard-broker` produces canonical receipts and facts with provenance. `trading-system` owns their strategy-session and Recording Track association.
- Raw Hyperliquid payloads are not accepted as the system accounting contract.

### Host integration

- `trading-system` resolves an explicit Broker binding through the canonical host contract.
- The host passes canonical requests only and receives normalized receipts/facts only.
- No strategy, risk, Telegram, or Recording Track module imports Hyperliquid-native fields or calls its API directly.
- No automatic Broker switching is added.
- Existing Paper, market trust, tick freshness, old-state, reconciliation, immutable-fill, Park, release-SHA, boot, Supervisor, and Telegram-only gates remain mandatory.

### Evidence and rollout

- Local Paper/fixture evidence, external testnet evidence, and Live evidence are separate classes.
- The first implementation may build testnet-ready runtime paths and local test harnesses without making external calls.
- Any external testnet proof must record environment identity, account identity, Broker order identities, canonical lifecycle states, fills, fees, positions, provenance, and final reconciliation.
- Live/mainnet requires a separate specification and explicit human authorization after the testnet evidence is reviewed.

## Testing Decisions

### Highest test seam

Tests target one highest seam: the Canonical Broker Adapter Contract from canonical request/observation through capability and environment preflight to normalized receipt, provenance, lifecycle, and reconciliation result. Tests should assert externally observable behavior, not Nautilus method names, internal helper structure, or provider implementation inheritance.

### Local contract tests

- Use fake/fixture Nautilus-compatible backends and deterministic transports.
- Prove Paper has no network I/O and accepts no credential-bearing signer.
- Prove environment/account/signer/capability mismatches fail before backend invocation.
- Prove standard K-line, BBO, order book, trades, instruments, precision, fees, funding, and account facts are normalized with provenance.
- Prove submit, cancel, replace, query, partial fill, timeout/unknown, duplicate observations, stale events, reconnect snapshots, and reconciliation recovery.
- Prove reduce-only protection, fixed-size versus position-following semantics, capability gaps, and partial-fill protection blockers.
- Prove actual fee versus estimated fee and funding remain separate.
- Prove secret redaction and absence of signed payloads in receipts/log-shaped artifacts.

### Runtime integration tests

- Use a controlled injected runtime backend at the same canonical seam.
- Test rate-limit classification, retry/backoff policy, reconnect/re-subscription behavior, REST/WebSocket ordering, and ambiguous outcomes without requiring a live Broker.
- Test the first vertical slice end-to-end with a deterministic account/order/fill event stream.
- Keep any external testnet proof as a separately labelled integration/evidence test; it must not be required for ordinary local test execution.

### Prior art

- The T01–T08 `standard-broker` Paper contract, market-data fixtures, account/fee mappings, order lifecycle, protection semantics, Nautilus compatibility bridge, conformance/security tests, and host contract are the direct baseline.
- The existing `trading-system` Paper execution/replay, broker-port, reconciliation, immutable-fill, market trust/freshness, Supervisor, and Telegram control tests remain upstream safety constraints.
- No test may use live credentials, authorize real-money movement, or treat a Paper receipt as proof of a Broker network action.

## Out of Scope

- Hyperliquid mainnet or real-money execution.
- Live keys, API-wallet private keys, seed material, credential provisioning, or credential rotation.
- Testnet credential onboarding or external network calls in the current implementation/specification session.
- Native spot, HIP-3 builder-deployed perpetuals, HIP-4 outcome markets, multi-DEX, unified account, or portfolio-margin support.
- CCXT, Hummingbot, Senpi, MCP write tools, autonomous LLM agents, or bot runtime integration.
- Reimplementation of Nautilus signing, nonce coordination, REST/WebSocket transport, or execution-engine behavior.
- Strategy redesign, DCA/Grid redesign, multi-strategy execution, automatic Broker switching, Shadow mutation, Feishu control, or a new control plane.
- Moving strategy/risk/Telegram/Recording authority into `standard-broker`.
- Treating local Paper or static fixtures as external execution proof.
- Cloud deployment, launchd/Supervisor changes, live cutover, or cloud-state modification.

## Further Notes

- This spec is a new runtime phase; it does not reopen or invalidate the completed T01–T08 Paper foundation.
- The first implementation should be split into dependency-ordered tickets after this spec is accepted. Each ticket remains one Issue, one branch, and one PR, with explicit blocking edges.
- The likely ticket families are runtime session/environment boundary, Nautilus external compatibility, market/instrument runtime, order lifecycle, protection lifecycle, account/fee/reconciliation, conformance/evidence, and trading-system host integration.
- The spec defines implementation intent, not authorization to access an external Broker. Testnet and Live require their own human-approved execution boundaries.
