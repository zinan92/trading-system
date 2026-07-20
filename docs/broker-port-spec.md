# Broker Port Specification

**Version:** A5 / `broker-port-descriptor-v1`

## Purpose

The Broker Port keeps strategy and orchestration code independent of Binance,
Tiger, or any future venue. Application code submits normalized intent. A
registered adapter owns venue translation, credentials, network I/O, order
lifecycle, and external-state reconciliation.

This is a gradual strangler boundary. The existing Binance/OANDA/MT5 wire
implementation remains in `LiveBrokerAdapter` for now; new application code no
longer selects providers or calls its private venue helpers.

## Authority boundary

| Concern | Authority |
|---|---|
| Strategy/range/order intent | StrategyPlan and application service |
| Exposure permission | canonical Risk Port |
| Broker selection | `BrokerPluginRegistry` composition root |
| Symbol/payload/signature/network | concrete venue adapter |
| Order lifecycle transition | existing venue adapter + `OrderLifecycleStore` |
| Account/order/position external truth | venue reconciliation port |
| P&L/account truth | canonical Accounting Port |
| Dashboard display | read model (A6) |

The registry never authorizes money. Risk, activation, preflight,
reconciliation, attended confirmation, and protective-order checks remain
cumulative gates inside the existing mutation path.

## Public contracts

`BrokerExecutionPort` exposes:

- stable `name`, `provider`, `descriptor`, and `capabilities`;
- `preflight() -> dict`;
- `submit_order(BrokerOrderRequest) -> PaperOrder`.

Optional public operations are capability-gated:

- `cancel_order(BrokerCancelRequest)`;
- `recover_protective_orders(BrokerProtectiveRecoveryRequest)`;
- a separate `BrokerReconciliationPort.run(run_date)`.

Requests are engine-neutral. They carry local asset/order/lifecycle identity,
not Binance symbols, Tiger contracts, REST endpoints, or provider payloads.

## Capability rules

Capabilities are closed values:

- `preflight`
- `submit_order`
- `cancel_order`
- `protective_recovery`
- `reconciliation`

They default to absent except for the baseline execution pair. A capability is
resolved from normalized provider identity, never from `hasattr` or inherited
method presence, and is frozen when the adapter is constructed. This matters
because Tiger paper inherits the legacy class that contains Binance helpers:
inheritance or later mutation of `adapter.provider` does not grant Tiger
permission to call them.

Current matrix:

| Provider | Submit | Cancel | Protection recovery | Reconciliation |
|---|---:|---:|---:|---:|
| Paper simulator | yes | no | no | no |
| Binance USD-M lineage | yes | yes | yes | yes |
| Tiger OpenAPI paper | yes | no | no | yes |
| OANDA/MT5/manual compatibility | yes | no | no | not in A5 |
| Unknown provider | unarmed compatibility only | no | no | no |

An absent capability raises before provider-private code or network I/O.

## Registry resolution

Plugins use the exact key `(execution_mode, provider, environment)`. Resolution
order is:

1. exact key;
2. exact provider with wildcard environment;
3. explicit mode wildcard provider fallback.

Duplicate keys fail construction. A factory result must structurally satisfy
the relevant port, and its actual execution capabilities must exactly match the
plugin declaration (excluding the separate reconciliation capability), or
assembly fails.

Paper, Binance demo, Binance testnet, Binance configured/live, Tiger paper,
OANDA, MT5, and manual gateway are registered explicitly. The final unknown
provider fallback is always `live_trading_enabled=False` and `dry_run=True`,
even if its caller requests an armed adapter.

The active-demo profile is also resolved at this composition boundary. Only a
plugin explicitly marked demo-capable can be selected. Unsupported/OANDA/MT5
profiles fall back to the paper strategy path rather than receiving a live
adapter from a demo toggle.

## Environment invariants

Binance demo and testnet currently share
`https://demo-fapi.binance.com`, but they are distinct plugins:

- demo uses `mode=demo` and intentionally does not invoke the live-money
  guardrail branch;
- testnet uses `mode=testnet` and does invoke it;
- request namespaces, protective endpoint defaults, and recovery metadata stay
  environment-specific;
- reconciliation receives the effective config from the selected execution
  adapter, so it cannot observe mainnet while execution targets demo/testnet.

The Binance configured/live plugin always uses the concrete
`BinanceUsdmBrokerAdapter.submit_order`. It therefore retains the
`real_money_ready` activation gate. The registry does not force
`live_trading_enabled=True` or `dry_run=False` for mainnet.

The legacy configured factory has one compatibility nuance: a broker config
may say `environment=demo` while the dedicated demo strategy is disabled. That
path continues to build the activation-gated venue adapter. Only an explicit
demo composition context selects `BinanceDemoBrokerAdapter`.

## Ambiguous outcomes

The port passes `PaperOrder` and lifecycle behavior through unchanged. It does
not re-wrap venue receipts or translate `submitting` into rejected/accepted.

A Binance timeout or ambiguous acknowledgement remains a durable
`submitting` lifecycle with the existing idempotency key. Recovery queries
venue truth before any retry; a second cycle cannot issue a duplicate merely
because the registry was introduced.

## Credentials and safe descriptors

Composition receives already-resolved configuration but never resolves secret
values. `broker-port-descriptor-v1` exposes only configured environment-variable
names. Literal keys/secrets, signed fields, timestamps, and signatures are not
included.

Venue adapters retain their current credential gates:

- Binance environment keys/secrets;
- Tiger owner-only props file plus explicit paper TradeClient/confirmation;
- OANDA token/account environment variables;
- existing live environment placeholder rejection.

## Compatibility facade

`services.broker_adapter` continues to re-export `BrokerOrderRequest`,
`BrokerAdapter`, and the existing factory functions. It loads pipeline config at
the old outer boundary, preserving tests and operational monkeypatch seams,
then delegates to `broker_composition`.

Venue imports inside the registry are lazy. This avoids recursion because
Binance/Tiger adapters still import the compatibility base class.

## Adding a broker

A new broker is accepted only when all of these exist:

1. adapter implementing `BrokerExecutionPort`;
2. explicit config/environment and credential-name mapping;
3. registry plugin with fail-closed capabilities;
4. reconciliation port before any production exposure is allowed;
5. common conformance matrix: identity, preflight shape, normalized receipt,
   secret safety, unsupported capability blocking, and config selection;
6. venue tests for order types, partial fills, cancellation, ambiguous outcomes,
   restart reconciliation, positions, fees, and protective orders;
7. existing risk/activation/attended gates proven additive.

Official Nautilus execution clients can satisfy this port in a future
milestone. A5 does not switch live authority or bypass the existing seven-cycle
cutover and reconciliation gates.

## A6 diagnostic closure

`MultiStrategyRunner._execution_profile_for`, reconciliation diagnostics, and
GridMind presentation now consume provider-neutral broker/read-model
projections. They contain no Binance/Tiger selection branch, do not invoke
venue preflight from a query, and expose only local descriptor/config facts.

The remaining Broker Port debt is physical rather than directional: the large
`LiveBrokerAdapter` compatibility class still contains multiple venue wire
implementations. New application code composes through `BrokerPluginRegistry`;
future work can move one venue at a time into isolated adapter packages without
changing normalized intent, risk, accounting, reconciliation, or Dashboard
contracts.

## Reference architecture

- [NautilusTrader adapter developer guide](https://nautilustrader.io/docs/latest/developer_guide/adapters/)
- [NautilusTrader execution flow](https://nautilustrader.io/docs/latest/concepts/execution/)
- [NautilusTrader live reconciliation](https://nautilustrader.io/docs/latest/concepts/live/)
