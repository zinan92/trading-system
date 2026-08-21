# Stage Hyperliquid external execution behind the canonical Broker boundary

**Status:** accepted
**Date:** 2026-08-21

The T01–T08 Paper foundation is complete. The next phase is to make Hyperliquid capable of an externally verified order lifecycle without moving Broker-native behavior into `trading-system` or weakening its safety gates.

## Decision

- The first external environment is Hyperliquid testnet. Live/mainnet is a separate later milestone and is not enabled by this decision.
- The first runtime proof is a single default validator-operated perpetual order lifecycle: submit, query, cancel/replace, fill, position, fee, and reconciliation. DCA/Grid is not the first runtime proof.
- `standard-broker` owns the Hyperliquid Runtime Adapter, canonical request/receipt mapping, capability preflight, account/signer binding, and Broker lifecycle correlation.
- `trading-system` remains the host/composition root. It owns strategy, risk, Park authorization, Paper/testnet/live gates, Recording Track, Supervisor behavior, and Telegram control.
- The low-level REST, WebSocket, signing, nonce, and execution-engine behavior is supplied by the pinned compatible Nautilus Hyperliquid implementation. `standard-broker` wraps it; it does not create a second signing or execution stack.
- CCXT and the official Hyperliquid Python SDK are not v1 runtime layers. They remain protocol, comparison, or fixture references only.
- Every runtime session explicitly binds a Broker, environment, account reference, signer reference, capability profile, and lifecycle correlation. No default account, signer, or environment is inferred.
- Credentials are supplied through an out-of-band secret provider. Private keys, signatures, signed payloads, and raw credentials never enter the canonical model, logs, Recording Track, issues, or fixtures.
- A canonical Market request uses fresh BBO plus an explicit slippage policy and an aggressive IOC limit representation when the Broker does not provide a native market operation. Missing freshness or slippage inputs fail closed.
- An ambiguous result is represented as unknown/submitting. The adapter must query or reconcile before retrying; blind retry is forbidden.
- Protection capabilities remain explicit. Reduce-only close legs are mandatory, and unsupported or unknown TP/SL, quantity-following, cancel-replace, or partial-fill behavior blocks the dependent path rather than being silently simulated.
- Local Paper/fixture evidence, external testnet evidence, and Live evidence are separate evidence classes. A Paper result never proves an external Broker action.

## Boundary test

If an implementation contains Hyperliquid-native fields, endpoints, order serialization, signing, WebSocket handling, Broker account/signer mapping, or Broker lifecycle reconciliation, it belongs behind the `standard-broker` adapter boundary or inside the pinned Nautilus implementation.

If an implementation contains strategy, risk, Park confirmation, Telegram, Recording Track, Supervisor, or environment activation policy, it belongs in `trading-system`.

## Rejected alternatives

- Put direct Hyperliquid API calls in `trading-system` — rejected because it would leak Broker primitives into strategy and risk paths.
- Reimplement Hyperliquid transport/signing in `standard-broker` — rejected because it duplicates Nautilus high-risk execution behavior.
- Treat local Paper as sufficient proof of external execution — rejected because Paper has no external state, credentials, or network lifecycle.
- Start with DCA/Grid end-to-end — rejected because it combines strategy lifecycle questions with the lower-level Broker execution proof.
- Enable Live after testnet by configuration toggle — rejected because Live requires a separate authorization, credential, release, and evidence contract.

## Consequences

The next build spec must define the runtime adapter seams and external evidence contract before implementation. The first implementation can remain narrow and testable while preserving the canonical six-port model and existing Paper safety boundary. Testnet access, credentials, deployment, and Live execution remain separate future actions.
