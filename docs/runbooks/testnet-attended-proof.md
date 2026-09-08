# Hyperliquid Testnet attended Grid proof

This runbook is the owner-operated acceptance step for Issue #1145. It permits
one BTC Grid Execution Slice on the exact
`hyperliquid-testnet-position-protection` profile. It is Testnet-only and does
not start a scheduler, change launchd, touch the online output roots, or enable
Mainnet/live trading.

## Before Park confirms

Prepare a fresh immutable activation and Grid plan for `BTC-USD-PERP`. The
activation must contain the same `plan_digest`, account fingerprint, release
SHA, runtime ID, and capability revision
`hyperliquid-testnet-position-protection-runtime-v1`. Confirm that the account
is flat and has no open orders using the standard-broker read path. A stale,
non-flat, unknown, or identity-mismatched read is a blocker.

The runtime configuration may contain only an opaque reference such as
`file-secret://hyperliquid-testnet` and a `secret_file` path. The signer file is
owned by the operator, must be mode `0600`, and must never be opened, copied,
printed, or pasted into a receipt, log, issue, or chat message.

## Attended execution

Run from this checkout with a separate local output root. Use the existing
composition root to construct `StandardBrokerExternalExecutionAdapter` from
the approved build context, passing the secret file path as configuration only.
Do not construct a generic Hyperliquid or Mainnet adapter. The following is the
required call order; each returned mapping is persisted by the coordinator as
machine evidence:

```python
from services.testnet_automation_coordinator import TestnetAutomationCoordinator

coordinator = TestnetAutomationCoordinator("<new-local-output-root>")
coordinator.activate(ACTIVATION, command_id="<unique-activation-command>")
coordinator.enable_testnet_execution(broker)
coordinator.submit_grid_orders(
    GRID_PLAN,
    broker=broker,
    command_id="<unique-grid-command>",
)
```

`broker` must be the public standard-broker adapter configured with
`transport_profile=hyperliquid-testnet-position-protection`,
`environment=testnet`, `live_trading_enabled=false`, and
`real_money_eligible=false`. Park remains present for the submit and every
subsequent action. Do not retry a timeout or ambiguous response.

After each entry fill, read the canonical facts bundle and record the fill,
fee, position, and reconciliation digests. Build a position-following
`ProtectionGroup` for the observed filled quantity, submit TP limit and SL
market through `broker.request("protection_order", "submit", group)`, then
query it until the returned observation is active and digest-valid. A partial
fill protects only the observed quantity; a cancel race or unknown result
freezes the slice and waits for Park.

Continue only while every protection query is confirmed. When Grid reaches its
authorized terminal condition, cancel remaining entries, reconcile again, and
record either a flat account or a durable `blocked` state. Never auto-flatten
unrelated positions and never start a new Plan after TP/SL.

## Acceptance evidence

The owner attaches the coordinator event/state files and the standard-broker
machine receipts, with their SHA-256 digests, showing:

- at least one real Testnet BTC Grid fill;
- TP/SL protection query-confirmed for the filled quantity;
- fills, fees, positions, and reconciliation bound to the same account,
  runtime, release, profile, and plan digests; and
- a final flat state or a durable blocked state.

Fixture tests, HTTP 200, a UI screenshot, or a Paper receipt are not evidence
of this acceptance. Keep the secret value and provider-native signed payloads
out of all artifacts.
