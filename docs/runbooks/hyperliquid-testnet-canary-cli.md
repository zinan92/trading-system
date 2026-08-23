# Hyperliquid Testnet canary CLI

This runbook is for one attended, release-bound Testnet canary. It is not a
strategy scheduler, DCA/Grid runner, ProtectionOrder path, Mainnet path, or
Live activation path.

## Inputs

`--plan` is a JSON object containing the complete `TestnetCanaryPlan` fields
from `services/standard_broker_testnet_canary.py`, including a matching
`plan_digest`. The plan must name the exact profile
`hyperliquid-testnet-default`, a full 40-character `release_sha`, explicit
quantity/price precision, and a worst-case loss no greater than 50 USD.

`--confirmation` is a JSON projection of the durable Park `confirmed` row. It
must contain `event=confirmed`, `execution_authorized=true`,
`execution_environment=testnet`, the exact `canary_id` and `plan_digest`, a
`proposal_id`/`confirmation_id`, and the `receipt_digest`. The corresponding
proposal and confirmed decision must already exist in
`<output-root>/park_strategy/confirmations.jsonl`; the projection does not
create authorization.

The secret file is an out-of-band local file owned by the operator. The CLI
accepts its path but never prints it. `standard-broker` reads it only when the
explicit `run` action first needs an external operation.

## Safe progression

```sh
python3 -m pipelines.standard_broker_testnet_canary \
  --plan /path/to/canary-plan.json
```

The default action is `digest`; it performs no network operation and resolves
no secret. Compare the printed digest with the exact Park confirmation.

```sh
python3 -m pipelines.standard_broker_testnet_canary \
  --action preflight \
  --plan /path/to/canary-plan.json \
  --account-address 0x<account-address> \
  --approval-id <testnet-approval-id> \
  --approved-by park
```

`preflight` validates the plan/account binding and starts the release-bound
runtime. It does not invoke the backend, read market data, or resolve the
signing secret. The first instrument/market read happens only in `run` while
constructing the reviewed public standard-broker canary binding.

Only after Park has confirmed the exact digest and the operator is present:

```sh
python3 -m pipelines.standard_broker_testnet_canary \
  --action run \
  --plan /path/to/canary-plan.json \
  --confirmation /path/to/confirmed-projection.json \
  --account-address 0x<account-address> \
  --secret-file /protected/path/to/testnet-signer \
  --credential-reference file-secret://hyperliquid-testnet \
  --approval-id <testnet-approval-id> \
  --approved-by park \
  --output-root outputs \
  --execute-testnet \
  --acknowledge I_UNDERSTAND_ONE_ATTENDED_HYPERLIQUID_TESTNET_ORDER
```

`run` performs one in-process `prepare -> submit -> fill-to-flat` lifecycle.
If the entry limit rests, the reviewed ordinary cancel path is used so the
process does not leave an unattended order behind. Unknown outcomes freeze the
durable canary state as `BLOCKED`; there is no retry, fallback, scheduler, or
Mainnet substitution. The evidence journal is written below
`outputs/standard_broker_testnet_canary/`.

## Stop conditions

Stop and notify Park if the command returns a non-zero exit code, the durable
state is `BLOCKED`, the plan/confirmation digest changes, the account
fingerprint does not match, or the runtime release/capability identity drifts.
Do not paste signer material, provider-native payloads, or secret paths into
issues, logs, screenshots, or chat.
