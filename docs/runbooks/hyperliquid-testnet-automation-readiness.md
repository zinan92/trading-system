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
