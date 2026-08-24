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

## Canonical external projection

Before an attended start, project the locked Paper DCA StrategyPlan through the
non-secret Broker/Instrument binding. This is local-only and produces the
external plan digest used by the durable Park confirmation:

```bash
python3 -m pipelines.standard_broker_external_dca \
  --action project \
  --plan <path-to-strategy-plan-v1.json> \
  --binding <path-to-non-secret-hyperliquid-instrument-binding.json>
```

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
  --strategy-plan <path-to-strategy-plan-v1.json> \
  --binding <path-to-non-secret-hyperliquid-instrument-binding.json> \
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
first requires a fresh cursor-bound clean-state read: any existing position,
open order, stale/unknown snapshot, or identity mismatch blocks the start. It
stops at `WAITING_ENTRY`, `PROTECTION_ACTIVE`, or durable `BLOCKED`. A further
DCA level requires a new attended command and the same confirmation gate.

## Reconcile one entry

While an entry is resting, use this attended command to query it once. If the
entry is still resting it remains `WAITING_ENTRY`; if it filled, the command
reads canonical facts and activates/reconciles protection. It never submits an
entry.

```bash
python3 -m pipelines.standard_broker_external_dca \
  --action reconcile-entry \
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

## Reconcile an expired entry

If the plan expires while a GTC entry is resting, create a fresh Park
confirmation for the same plan digest and use this explicit expiry path. It
only queries, cancels, and re-queries the owned entry; it never submits a
replacement. An observed fill is retained as a blocker requiring an attended
flatten.

```bash
python3 -m pipelines.standard_broker_external_dca \
  --action expire-reconcile \
  --plan <path-to-expired-external-dca-plan.json> \
  --confirmation <path-to-fresh-park-confirmation.json> \
  --account-address <testnet-account-address> \
  --secret-file <local-testnet-signer-file> \
  --approval-id <human-testnet-approval-id> \
  --approved-by park \
  --output-root <output-root> \
  --execute-testnet \
  --acknowledge I_UNDERSTAND_ONE_ATTENDED_EXTERNAL_DCA_TESTNET_ACTION
```

## Flatten an existing canary position

For an exposure that predates the current lifecycle journal, use a separate
cleanup journal and a fresh confirmation bound to the cleanup plan digest. The
command reads the account snapshot, adopts only the explicitly identified
position, and submits one reduce-only close; it never adopts unknown open
orders or starts a new strategy.

```bash
python3 -m pipelines.standard_broker_external_dca \
  --action adopt-flatten \
  --plan <path-to-explicit-cleanup-plan.json> \
  --confirmation <path-to-fresh-cleanup-confirmation.json> \
  --account-address <testnet-account-address> \
  --secret-file <local-testnet-signer-file> \
  --approval-id <human-testnet-approval-id> \
  --approved-by park \
  --output-root <output-root> \
  --execute-testnet \
  --acknowledge I_UNDERSTAND_ONE_ATTENDED_EXTERNAL_DCA_TESTNET_ACTION
```

If `adopt-flatten` returns `BLOCKED` with
`flatten_submit_receipt_unknown`, do not run `adopt-flatten` again. Use the
same confirmed cleanup plan once to query the persisted idempotency key:

```bash
python3 -m pipelines.standard_broker_external_dca \
  --action reconcile-flatten \
  --plan <path-to-explicit-cleanup-plan.json> \
  --confirmation <path-to-fresh-cleanup-confirmation.json> \
  --account-address <testnet-account-address> \
  --secret-file <local-testnet-signer-file> \
  --approval-id <human-testnet-approval-id> \
  --approved-by park \
  --output-root <output-root> \
  --execute-testnet \
  --acknowledge I_UNDERSTAND_ONE_ATTENDED_EXTERNAL_DCA_TESTNET_ACTION
```

## One attended next entry

After `start` reaches `PROTECTION_ACTIVE`, one additional approved ladder level
can be submitted manually:

```bash
python3 -m pipelines.standard_broker_external_dca \
  --action next-entry \
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

This command submits exactly one next level, reads its canonical facts when it
fills, and re-confirms position-following protection. It never loops or
submits another level automatically. Repeat only while Park is present and the
durable confirmation remains valid.

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
