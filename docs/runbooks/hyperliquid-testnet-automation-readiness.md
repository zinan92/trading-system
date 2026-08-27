# Hyperliquid Testnet automation readiness

This runbook records evidence for the approved Testnet Automation Coordinator.
It is not a Mainnet/Live activation and it never reads or prints a signer
secret. The scheduler and order lifecycle remain blocked until their exact
identity and capability gates pass.

## Scope

- Run one Strategy Family at a time: DCA or Grid.
- Use one selected BTC-USD-PERP Execution Slice.
- Record two consecutive Beijing 12-hour windows for each mode.
- Keep the existing 14-window contract reserved for a future Live decision.
- Treat a window as evidence only; it never expires or mutates the Plan.

## Protected Broker preflight and attended start

The default `hyperliquid-testnet-default` profile remains preflight-only. Use
the explicit position-protection profile for a strategy proof. The following
preflight does not read the signer file or invoke an order/fact backend; the
path is accepted as an opaque reference only:

```sh
PYTHONPATH=/path/to/standard-broker/src python3 -m pipelines.testnet_automation_proof \
  --action preflight \
  --account-address <testnet-account-address> \
  --runtime-id <runtime-id> \
  --release-sha <trading-system-release-sha> \
  --approval-id <testnet-approval-id> \
  --approved-by park \
  --secret-file <local-testnet-signer-file>
```

After a fresh Park confirmation, a full StrategyPlan and a complete
source-bound market-fact document are available, `start` is the only command
that may submit the first Testnet entry. It requires both
`--execute-testnet` and the exact acknowledgement below; it creates one
candidate/Execution Slice and stops at the lifecycle's next attended action:

The confirmation document must be the latest durable Park decision (or its
projection) with `event=confirmed`, `execution_authorized=true`,
`execution_environment=testnet`, the exact `plan_digest` and
`activation_id`, a non-empty `confirmation_id`, and `confirmed_at`.

```sh
PYTHONPATH=/path/to/standard-broker/src python3 -m pipelines.testnet_automation_proof \
  --action start \
  --strategy-plan <strategy-plan-v1.json> \
  --market <hyperliquid-market-facts.json> \
  --confirmation <park-confirmation.json> \
  --account-address <testnet-account-address> \
  --runtime-id <runtime-id> \
  --release-sha <trading-system-release-sha> \
  --approval-id <testnet-approval-id> \
  --approved-by park \
  --secret-file <local-testnet-signer-file> \
  --execute-testnet \
  --acknowledge I_UNDERSTAND_ONE_ATTENDED_TESTNET_STRATEGY_ACTION
```

Use `--strategy-family grid` only when the loaded plan is Grid. The market
document must contain the full BBO/L2/depth/slippage/oracle and Broker/source
identity fields; a price-only or synthetic document is blocked. This command
does not enable Mainnet/Live or the scheduler and never creates a new Plan
after TP/SL.

## Read-only collection

The observation document must contain the exact strategy session/revision,
Plan digest, Testnet Broker/account/release identity, safety flags, window
timestamps, and every required evidence category. Each category must point to a
JSON artifact with a matching SHA-256 digest and the same identity/window
fields. Required categories include orders/fills/positions/reconciliation,
protection coverage, capability status, market freshness/trust, runtime health,
retry outcomes, release/account/environment identity, and recording package.

Collect one window without executing a control action:

```sh
python3 -m pipelines.testnet_soak \
  --output-root <output-root> \
  --observation <observation.json> \
  --automation \
  --strategy-family dca \
  --instrument-id BTC-USD-PERP \
  --artifact orders_fills_positions_reconciliation=<orders.json> \
  --artifact protection_coverage=<protection.json> \
  --artifact capability_status=<capability.json> \
  --artifact market_freshness_trust=<market.json> \
  --artifact runtime_health=<runtime.json> \
  --artifact retry_outcomes=<retry.json> \
  --artifact release_account_environment_identity=<identity.json> \
  --artifact recording_package=<recording.json>
```

Use `--strategy-family grid` for the Grid evidence. Add `--finalize` only on
the second window after the first window has passed and the identity remains
unchanged.

## Pass and stop rules

Readiness is pass only when both windows are complete, contiguous, immutable,
source-attested, and every category is green. Stop and notify Park on stale or
dislocated market facts, missing protection, identity drift, duplicate owner,
unknown side effect, reconciliation mismatch, loss-cap breach, incomplete
artifact, or notification failure. Pair-local issues freeze that slice;
account/universe/ownership/scheduler uncertainty enters Portfolio Risk Hold.

Unknown submit/replace/cancel/flatten outcomes receive one identity-bound
reconcile query only. Do not blind-retry, flatten unrelated positions, switch
assets after a side effect, or create a new Plan automatically after TP/SL.

## Evidence boundary

A successful local fixture or attended Testnet package proves only the recorded
Testnet behavior. It does not authorize Mainnet/Live, automatic promotion, or
an unattended real-money scheduler. Keep credentials, signed payloads, and
provider-native data out of observations, artifacts, issues, logs, and
screenshots.
