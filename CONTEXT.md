# Standard Broker Context

This repository defines the broker boundary used by the trading system. It keeps broker-specific market, account, order, protection, fee, and lifecycle semantics behind canonical contracts while preserving explicit capability gaps.

## Boundary language

**Broker**:
An order counterparty that provides market data, account state, and/or order execution, including Tiger Finance, Binance, and Hyperliquid.
_Avoid_: using “exchange” or “venue” as the primary domain name.

**Standard Broker**:
The canonical broker boundary and adapter contract shared by the trading system; it is not a strategy engine or an execution engine.
_Avoid_: “Hyperliquid bot” or “second trading engine”.

**Trading System**:
The host and composition root that owns strategy, risk, Paper safety, recording, and Telegram control decisions.
_Avoid_: treating it as a broker implementation.

**Broker Adapter**:
A broker-specific translation boundary that maps canonical requests and observations to a broker’s native capabilities and reports unsupported semantics explicitly.
_Avoid_: leaking broker primitives into strategy or risk code.

**Canonical Port**:
A broker-neutral contract for one responsibility: market data, instruments, accounts, order execution, protection orders, or fees.
_Avoid_: a provider-shaped convenience API.

**Execution Venue ID**:
Metadata identifying the execution scope inside a broker, such as a broker’s default order book; it is provenance, not the primary business identity.
_Avoid_: promoting it to the system-wide broker abstraction.

## Market and data language

**Standard K-line**:
The existing canonical candle contract consumed by strategies and research; every broker-native candle must be normalized into this contract.
_Avoid_: creating a second broker-specific K-line consumer format.

**Market Data Provenance**:
The source, broker, execution scope, venue timestamp, receive timestamp, transport state, and mapping identity attached to canonical market data.
_Avoid_: treating an adapter response as authoritative without provenance.

**Data Feed**:
An optional normalization and freshness capability that may be used when broker-native data is not already execution-grade; its know-how may be reused without requiring a separate runtime service.
_Avoid_: making Data Feed a mandatory layer between every broker and the trading system.

## Execution and safety language

**Order Intent**:
A broker-neutral request expressing what should be attempted after strategy and risk gates have allowed it.
_Avoid_: putting signed payloads, provider IDs, or broker-native order groups in the intent.

**Protection Order**:
A broker-neutral take-profit, stop-loss, reduce-only, or linked protective instruction with explicit trigger, quantity, parent, sibling, and partial-fill semantics.
_Avoid_: reducing protection to a single `has_stop` flag.

**Capability Gap**:
An explicit declaration that a broker cannot provide a requested canonical behavior with the required semantics; the core fails closed rather than silently simulating success.
_Avoid_: using a missing capability as permission to call a nearby native operation.

**Paper**:
A local, non-network execution environment using fake or fixture transport and no trading credential; Paper is not testnet.
_Avoid_: calling an external testnet call “just Paper”.

**Testnet**:
An external broker network with separate state, credentials, asset mappings, and approvals; it requires its own integration and safety decision.
_Avoid_: assuming testnet proves local Paper safety.

**Live**:
An external real-money execution environment; it is outside the current repository scope and requires separate explicit authorization.
_Avoid_: inferring live authority from read access or adapter availability.

**Signer**:
The credential-bearing identity that authorizes broker actions; it may differ from the account whose balances, orders, and positions are queried.
_Avoid_: assuming signer identity and account identity are always the same.

**Recording Track**:
The system-owned record of canonical orders, fills, fees, funding, provenance, and reconciliation facts.
_Avoid_: making broker-native raw responses the accounting contract.
