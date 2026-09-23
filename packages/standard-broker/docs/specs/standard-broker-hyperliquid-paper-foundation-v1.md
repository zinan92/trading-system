# Spec: Canonical broker adapter boundary and Hyperliquid Paper foundation

**Spec status:** accepted for ticket decomposition
**Scope:** `standard-broker` canonical broker layer
**First broker:** Hyperliquid
**First product:** default validator-operated perpetuals
**First environment:** local Paper fixtures only

## Problem Statement

The trading system must be able to work with Tiger Finance, Binance, Hyperliquid, and future Brokers without embedding broker-native symbols, order types, account fields, fee rules, or lifecycle semantics in strategy, risk, Recording Track, or Telegram control code.

Hyperliquid adds important capabilities and important differences: native on-chain order books, API-wallet signer/account separation, five-significant-figure price rules, mark-price triggers, grouped TP/SL semantics, cancel-replace modification, partial fills, funding, builder fees, and broker-specific reconciliation behavior. These semantics cannot safely be reduced to an unqualified lowest-common-denominator API.

NautilusTrader already provides the execution engine used by the trading system and includes a Rust-native Hyperliquid adapter. The system therefore needs an independent canonical broker boundary, not a second execution engine or a second Hyperliquid signing stack.

## Solution

Create a provider-neutral `standard-broker` library with six canonical ports and one highest-level test seam: the Canonical Broker Adapter Contract.

The contract receives canonical requests or observations, applies frozen capability and safety checks, delegates to a Broker Adapter, and returns normalized receipts, provenance, fees, lifecycle state, or an explicit capability gap.

The first adapter scope is Hyperliquid default validator-operated perpetuals in local Paper mode using fake or fixture transport. The low-level Hyperliquid REST, WebSocket, signing, nonce, order lifecycle, and reconciliation implementation is supplied through the compatible NautilusTrader Hyperliquid adapter; `standard-broker` owns the canonical boundary, capability policy, and mapping contract.

The trading system remains the host and composition root. It continues to own strategy, risk, Paper-only enforcement, market trust/freshness gates, Recording Track, Park confirmation, Supervisor fail-closed behavior, and Telegram as the only control plane.

## User Stories

1. As a trading-system owner, I want one Broker-neutral contract, so that adding a Broker does not require changing strategy or risk code.
2. As a strategy consumer, I want every Broker candle normalized into the existing standard K-line, so that I do not need a broker-specific K-line format.
3. As a market-data consumer, I want trades, order books, tickers, mark/index data, and funding context through one MarketDataPort, so that each source has one canonical meaning.
4. As a safety operator, I want market-data provenance and receive freshness attached to observations, so that stale or ambiguous data cannot pass the existing trust gates.
5. As an instrument consumer, I want symbol mappings, quantity steps, price rules, minimum notional, contract type, margin, leverage, and supported order types through InstrumentPort, so that order validation is not scattered across strategies.
6. As an account consumer, I want equity, balance, margin, exposure, realized/unrealized PnL, funding, fees, and liquidation facts through AccountPort, so that account truth is not reconstructed from strategy state.
7. As an execution consumer, I want a canonical OrderIntent, so that a strategy never constructs a broker-native signed payload.
8. As an execution consumer, I want submit, cancel, query, open-order, fill, and position operations through OrderExecutionPort, so that the lifecycle is consistent across Brokers.
9. As an execution consumer, I want deterministic idempotency and client-order identity, so that retries cannot silently create duplicate orders.
10. As an execution consumer, I want cancel-replace semantics represented explicitly, so that a replacement order is not confused with the canceled original order.
11. As an execution consumer, I want ambiguous network outcomes represented as unknown/submitting, so that a timeout never becomes an unsafe blind retry.
12. As a protection consumer, I want reduce-only, TP, SL, OCO, bracket, and position-level protection represented with trigger and quantity policies, so that protection behavior is not reduced to a boolean flag.
13. As a protection consumer, I want fixed-size and position-following protection distinguished, so that a Broker’s quantity semantics are not misrepresented.
14. As a risk owner, I want partial-fill protection gaps to fail closed, so that an unprotected residual position is never reported as safely protected.
15. As a fee consumer, I want maker/taker fees, rebates, funding, builder fees, fee currency, timestamps, sources, and actual-versus-estimated status through FeePort, so that fee logic cannot leak into strategy code.
16. As an accounting consumer, I want liquidation facts separated from liquidation fees, so that the system does not invent a fee where the Broker documents no separate clearance fee.
17. As a credential owner, I want signer identity separated from account identity, so that API-wallet actions query the correct master account and do not produce false empty-account results.
18. As a Paper operator, I want the first adapter to use local fake/fixture transport without credentials or network I/O, so that the first proof remains Paper-safe.
19. As a Paper operator, I want testnet and mainnet represented as separate future environments, so that external state and live authority cannot be enabled by a Paper configuration toggle.
20. As a Nautilus owner, I want the Broker boundary to reuse the existing Nautilus Hyperliquid adapter, so that the system does not maintain a second signing, WebSocket, nonce, or reconciliation implementation.
21. As a maintainer, I want the core contracts to be independent of provider-native types, so that Nautilus or another low-level adapter can be replaced without changing strategy semantics.
22. As a maintainer, I want all unsupported capabilities to carry stable reason codes, so that fail-closed behavior is observable and testable.
23. As a Recording Track owner, I want normalized order, fill, fee, funding, and provenance receipts, so that raw Broker responses are evidence rather than the accounting contract.
24. As a Telegram operator, I want no MCP, agent, bot, or alternative control plane inside `standard-broker`, so that Telegram remains the only authorized system control surface.
25. As a future Broker maintainer, I want a shared conformance contract, so that Tiger, Binance, Hyperliquid, and future Brokers can be added without duplicating safety expectations.
26. As a release owner, I want Broker adapter compatibility tied to explicit dependency and contract evidence, so that a Nautilus upgrade cannot silently change execution semantics.
27. As a reviewer, I want each story to be independently testable, so that one Issue, one branch, and one PR remain meaningful delivery units.

## Implementation Decisions

- `standard-broker` is a standalone broker-boundary repository and library. It is not a strategy engine, execution engine, bot runtime, MCP server, or data-feed service.
- The six canonical ports are `MarketDataPort`, `InstrumentPort`, `AccountPort`, `OrderExecutionPort`, `ProtectionOrderPort`, and `FeePort`.
- All order counterparties are called Brokers. `broker_id` is the primary identity. An execution scope identifier is provenance metadata, not the primary domain abstraction.
- The single highest test seam is the Canonical Broker Adapter Contract: canonical request/observation → capability gate → Broker Adapter → normalized receipt/provenance/reconciliation result.
- The core domain model must not require Hyperliquid `coin`, numeric `oid`, `cloid`, `normalTpsl`, `positionTpsl`, or DEX-native fields. Those values may exist only inside adapter mappings or provenance details.
- Existing standard K-line remains the canonical candle consumer contract. Broker-native candles are normalized into it; no second K-line format is introduced.
- Data Feed is optional. Broker-native data may flow directly through normalization when it already satisfies the execution-grade market-data contract. Data-feed know-how may be reused as normalizer, freshness, and fixture logic.
- The first product scope is default validator-operated Hyperliquid perpetuals. Native spot, HIP-3 builder-deployed perpetuals, HIP-4 outcome markets, and multi-DEX behavior are deferred because their collateral, margin, oracle, fee, settlement, and lifecycle semantics differ materially.
- The first environment is local Paper. Fake or fixture transport is required; network I/O and trading credentials are forbidden. Testnet is a later external environment, not a synonym for local Paper. Mainnet/live is outside this spec.
- The low-level Hyperliquid implementation is the compatible NautilusTrader Hyperliquid adapter. The `standard-broker` integration must pin and audit the compatible Nautilus version and treat adapter upgrades as compatibility changes.
- The official Hyperliquid Python SDK is a protocol/signing reference and fixture source, not the first runtime dependency. CCXT is not a v1 runtime dependency.
- Capability descriptors are frozen, validated data. Capability presence is not inferred from method existence or inheritance.
- A capability gap must fail closed before network I/O. A qualified emulation is allowed only when explicitly declared, bounded, and represented in the capability details.
- Hyperliquid market intent is represented canonically, but the adapter declares `native_market=false` and `market_as_ioc_limit=true`. Market execution requires a fresh BBO and an explicit slippage policy; missing inputs deny the request.
- Hyperliquid protection mapping distinguishes reduce-only, TP/SL market/limit, mark-price trigger, grouped OCO/bracket, position-level protection, fixed-size protection, position-following protection, and partial-fill repair policy.
- `normalTpsl` and `positionTpsl` are adapter semantics and do not become core fields. The adapter must not claim universal partial-fill protection when the Broker cancels children or requires an additional protection action.
- Hyperliquid modify is modeled as cancel-replace. The canonical client-order identity remains traceable across the old and replacement Broker order identities.
- A signed request that times out after leaving the process produces an ambiguous lifecycle state. The adapter must query or reconcile before retrying; blind retry is forbidden.
- Fill identity must be durable and idempotent. Duplicate observations from WebSocket snapshots, reconnects, and REST reconciliation must not create duplicate accounting events.
- FeePort is the only canonical fee boundary. Maker/taker, rebate, builder fee, funding, fee currency, fee time, fee source, schedule provenance, and actual-versus-estimated status remain explicit. Funding is not merged into trading fee. No liquidation fee is fabricated when the Broker provides no separate clearance fee.
- Signer identity and account identity are separate concepts. Credential descriptors expose only environment names and public addresses. Private keys, seeds, signed payloads, and signatures never enter the core model, receipts, logs, or issue text.
- `trading-system` remains the composition root and retains strategy, risk, market trust, tick freshness, old-state, reconciliation, immutable-fill, Park, release-SHA, boot, Supervisor, and Telegram-only boundaries.
- `standard-broker` does not include MCP, Feishu, Senpi, Hummingbot, autonomous LLM agents, strategy templates, or automatic Broker switching.
- The implementation order is: canonical domain/capability contract; Hyperliquid default-perps instrument/market-data mapping; account/fee/funding mapping; order/protection serializer and capability gaps; Nautilus compatibility boundary; conformance/security/Paper proof; trading-system host integration.
- Delivery follows one Issue = one branch = one PR. A later story cannot change an earlier story’s acceptance contract without a new specification decision.

## Testing Decisions

- Tests exercise externally visible behavior at the Canonical Broker Adapter Contract seam. Internal helper structure, SDK method names, and implementation inheritance are not test contracts.
- Contract tests use fake Broker Adapters and fixture transports. They must prove that capability gates run before network I/O and that unsupported behavior returns stable fail-closed reason codes.
- Market-data tests cover standard K-line normalization, trades, order-book precision/depth, ticker/BBO/mid separation, provenance, receive freshness, stale state, reconnect state, and incomplete snapshots.
- Instrument tests cover dynamic symbol/asset mapping, five-significant-figure and decimal precision rules, quantity step, minimum notional, derived minimum quantity, contract type, margin mode, leverage, and unsupported product scopes.
- Account and fee tests cover equity, balance, margin, exposure, realized/unrealized PnL, funding, maker/taker fees, negative rebates, fee currency, actual-versus-estimated separation, liquidation facts, and duplicate-event prevention.
- Order lifecycle tests cover resting, filled, waiting-for-fill, waiting-for-trigger, rejected, canceled, partial fill, duplicate fill, timeout/unknown, cancel-replace, out-of-order replacement events, stale old-order events, and reconciliation recovery.
- Protection tests cover reduce-only, TP/SL market/limit, mark-price trigger, grouped parent/children, fixed-size, position-following, sibling cancellation, and partial-fill capability gaps.
- Security tests prove no-network Paper behavior, no credential acceptance in Paper, secret redaction, no signed payload leakage, and separation of static fixture evidence from external runtime evidence.
- Nautilus compatibility tests pin the supported adapter contract and fail when the declared implementation/version does not match the tested compatibility surface.
- Prior art is the existing trading-system Paper execution/replay, broker-port, reconciliation, immutable-fill, and fail-closed gate behavior; the new contract must be additive and must not weaken those boundaries.

## Out of Scope

- Hyperliquid mainnet or real-money execution.
- Hyperliquid testnet execution or testnet credential onboarding.
- Any live key, API wallet private key, seed, or signer provisioning.
- Native spot, HIP-3, HIP-4, multi-DEX, unified account, or portfolio-margin support.
- CCXT, Hummingbot, Senpi, Condor, community MCP servers, or autonomous LLM trading.
- Strategy redesign, Grid redesign, multiple strategies, automatic Broker switching, Shadow mutation, Feishu control, or a new control plane.
- Reimplementation of Nautilus execution-engine behavior, EIP-712 signing, nonce coordination, WebSocket transport, or venue lifecycle internals.
- Cloud deployment, launchd/Supervisor changes, live cutover, credential rotation, or cloud-state modification.
- Replacing the existing standard K-line contract or moving fee calculations into strategy code.

## Further Notes

- Hyperliquid’s official API documentation is the protocol authority for order, TP/SL, precision, fee, funding, nonce, rate-limit, account, and testnet semantics.
- Nautilus v1.230.0’s Hyperliquid integration is the initial low-level implementation reference; its compatibility and known edge cases must be treated as explicit evidence, not assumed away.
- The research conclusion and source register are recorded separately from this build spec. The research identifies the existing Nautilus adapter and explains why CCXT and application-layer agent/MCP products are not v1 runtime dependencies.
- The next process step after this spec is ticket decomposition into SB-001 through SB-007. Implementation begins only after the tickets are created and the first ticket is selected.
