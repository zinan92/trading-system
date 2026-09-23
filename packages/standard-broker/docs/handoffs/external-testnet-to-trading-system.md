# External Testnet handoff to `trading-system`

Status: `standard-broker` SB-EXT-01A–01G contract work complete only after PR #56 is merged. This note is a boundary handoff, not an execution authorization.

## What `standard-broker` owns

- Exact Hyperliquid external Testnet profile and runtime identity.
- Canonical market/instrument/account/fee/fill facts.
- Canonical order lifecycle, client/Broker identity, idempotency, replacement lineage, and fail-closed ambiguous outcomes.
- Protection capability gaps and explicit reduce-only-close semantics.
- Cursor-bound reconciliation/evidence envelopes and Broker-facing capability preflight.
- The public `ExternalBrokerHost`, `HyperliquidExternalFactAdapter`, `HyperliquidExternalOrderAdapter`, and reconciliation types.

## What `trading-system` owns

- Host composition and module wiring.
- Strategy, risk, Paper/testnet/live policy, market trust/freshness/old-state gates.
- Park authorization, Recording Track persistence, immutable-fill guard, Supervisor/boot/release-SHA ownership, and Telegram-only control.
- Deciding whether a canonical order intent is admitted and when a non-pass reconciliation result blocks execution.

## Next dependency

`trading-system` may plan a separate external-host bridge issue that consumes only the public standard-broker seam. It must not import Hyperliquid native types, call `_invoke_native()`, construct signed payloads, read credentials, or create a second order/reconciliation owner.

In particular, private runtime methods and native Hyperliquid payloads remain implementation details of the adapter.

This handoff does not enable DCA/Grid, automatic Broker switching, Mainnet/live, a soak scheduler, or new Testnet orders. Any Testnet execution still requires the existing human approval, release, account, lifecycle, and reconciliation gates owned by the host composition.
