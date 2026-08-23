# Attended Hyperliquid Testnet external DCA

This runbook covers one manually attended DCA action through the public
standard-broker protected binding. It is Testnet-only and does not install a
scheduler, retry an unknown result, or submit the next DCA level automatically.

## Read-only digest

```bash
python3 -m pipelines.standard_broker_external_dca \
  --plan <path-to-external-dca-plan.json>
```

The default action only validates the canonical plan and prints its digest. It
does not load credentials or invoke a backend.

## Protected preflight

```bash
python3 -m pipelines.standard_broker_external_dca \
  --action preflight \
  --plan <path-to-external-dca-plan.json> \
  --account-address <testnet-account-address> \
  --approval-id <human-testnet-approval-id> \
  --approved-by park \
  --output-root <output-root>
```

Preflight binds the exact opt-in profile
`hyperliquid-testnet-position-protection`, verifies account/runtime/release
identity and `protection_ready=true`, and closes the runtime. It does not
resolve the signer file or invoke an order/facts operation.

## One attended start

Before this command, the plan digest must have a matching durable Park
confirmation in the selected output root. The signer file is a local path and
must never be pasted into chat or printed by the command.

```bash
python3 -m pipelines.standard_broker_external_dca \
  --action start \
  --plan <path-to-external-dca-plan.json> \
  --confirmation <path-to-confirmed-park-projection.json> \
  --account-address <testnet-account-address> \
  --secret-file <local-testnet-signer-file> \
  --approval-id <human-testnet-approval-id> \
  --approved-by park \
  --output-root <output-root> \
  --execute-testnet \
  --acknowledge I_UNDERSTAND_ONE_ATTENDED_EXTERNAL_DCA_TESTNET_ACTION
```

`start` submits at most one entry, reads canonical entry facts when the entry
is filled, and then submits/reconciles the position-following TP/SL group. It
stops at `WAITING_ENTRY`, `PROTECTION_ACTIVE`, or durable `BLOCKED`. A further
DCA level requires a new attended command and the same confirmation gate.

## One attended flatten

Use the same plan, confirmation, account, approval, signer, output root, and
explicit acknowledgement after the lifecycle reaches an exposure-bearing
state:

```bash
python3 -m pipelines.standard_broker_external_dca \
  --action flatten \
  --plan <path-to-external-dca-plan.json> \
  --confirmation <path-to-confirmed-park-projection.json> \
  --account-address <testnet-account-address> \
  --secret-file <local-testnet-signer-file> \
  --approval-id <human-testnet-approval-id> \
  --approved-by park \
  --output-root <output-root> \
  --execute-testnet \
  --acknowledge I_UNDERSTAND_ONE_ATTENDED_EXTERNAL_DCA_TESTNET_ACTION
```

Flatten cancels/query-confirms open entry orders, cancels/reconciles the
protection group, submits one ordinary reduce-only IOC close, and requires a
cursor-bound canonical flat reconciliation before reporting
`FLAT_RECONCILED`.

Any `BLOCKED` result is durable evidence. Do not retry blindly; inspect the
recorded blocker, notify Park, and create a fresh confirmation if the plan or
runtime identity changes.
