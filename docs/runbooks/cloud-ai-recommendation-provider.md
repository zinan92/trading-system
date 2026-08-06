# Cloud Paper AI recommendation provider

The Paper recommendation service consumes one explicit command from
`machine_planner.command`. The default is the portable command name `codex`;
each runtime must configure and authenticate its own provider outside the
repository.

The provider receives the complete recommendation prompt on standard input and
must honor the `--output-last-message <path>` argument by writing one JSON
object to that path. The service runs it without shell interpolation, enforces
the configured timeout, validates the decision schema, and archives a success
or failure receipt. A provider failure cannot create a proposal, StrategyPlan,
order, position, or fill.

Stable failure codes:

- `strategy_recommendation_provider_missing`
- `strategy_recommendation_provider_not_executable`
- `strategy_recommendation_provider_timeout`
- `strategy_recommendation_provider_failed`
- `strategy_recommendation_provider_invalid_output`

## Server-local unattended provider

For the unattended Cloud Paper path, install the official Codex CLI release
for the host architecture and keep its authentication state in a host-only
directory owned by `gridmind` (mode `0700`, with the auth file mode `0600`).
The deployed command must be the repository wrapper
`tools/cloud_codex_provider_wrapper.sh`, installed as `/usr/local/bin/codex`.
The wrapper sets `HOME` and `CODEX_HOME` to `/opt/gridmind` and
`/opt/gridmind/.codex` and then execs the pinned `/opt/gridmind/bin/codex`
binary.  It never reads the Mac exchange directory.

Provider readiness is a separate, source-bound receipt.  It proves the
executable/version, `codex login status`, the bounded recommendation command,
and the JSON response contract.  The receipt records only bounded status facts
and the deployed source SHA/tree; it never records the prompt, response, access
token, or other secret.  Existing strategy lifecycle work remains independent
of provider availability.  A fresh proof is required only at the exact new-AI,
new-plan, prepare, and pre-`start_intent` entry seams.

The canonical unattended renewal units are:

- `gridmind-ai-provider-readiness.service`
- `gridmind-ai-provider-readiness.timer`

The oneshot service runs as `gridmind`, is bounded to 120 seconds, and retains
the same read-only/no-order/no-exchange-credential contract as the manual
probe.  The timer uses `OnBootSec=120` and `OnUnitInactiveSec=300`, so executions
never overlap and a failed proof is rechecked within five minutes.  The renewal
pipeline performs only local verification until the current proof reaches six
hours of age; it then runs a new provider smoke check, well before the 24-hour
expiry.  A real refresh writes `readiness_current.json` plus an immutable
digest-named history receipt; successful refreshes also advance
`readiness_last_success.json`.  A failed run does not erase the last successful
proof.

Install the source-rendered units passively first.  Activate only the canonical
renewal timer through the repository action; never guess a unit name:

```bash
python -m pipelines.cloud_systemd \
  --repo-root /opt/gridmind/src/trading-system \
  --datafeed-root /opt/gridmind/src/datafeed \
  --app-python /opt/gridmind/venvs/app/bin/python \
  --datafeed-python /opt/gridmind/venvs/datafeed/bin/python \
  --render-dir /tmp/gridmind-systemd \
  --action activate-provider-readiness --apply
```

Then collect the read-only mechanical receipt:

```bash
python -m pipelines.cloud_timer_contract --json
```

The provider timer row must show `enabled=true`, `active=true`, the exact
`/etc/systemd/system/gridmind-ai-provider-readiness.timer` load path, a
non-empty next trigger once the oneshot finishes, the five-minute recovery
cadence, the effective unit content SHA-256, and a digest/source-valid latest
successful receipt.  The service boot receipt must bind the same timer
contract.  Do not
print the provider token, environment files, or auth file while collecting
evidence.

A failed refresh is warning-level while an already-running strategy continues.
If the failed proof blocks current-cycle convergence for more than 300 seconds,
the existing Supervisor health condition becomes critical and dead-man sends
the external failure signal.  A broken sibling timer must never prevent the
dead-man service itself from running.  The next five-minute check automatically
recovers with a new digest when the provider becomes healthy; no old prepared
start or control request is reused.

The provider remains proposal-only: a provider success creates no plan, order,
position, fill, or risk authorization until the existing outer-policy,
preview, confirmation, and public-control gates all pass.

## Credential-free attended bridge

For an attended Paper window, Cloud may configure:

```text
python tools/strategy_recommendation_file_bridge.py \
  --exchange-dir <protected-runtime-directory>
```

The bridge writes `request.json` containing the exact prompt, prompt SHA-256,
and expected response filename. Run the model in a separately authenticated
environment, validate that it returned one JSON object, upload it as
`response-<prompt-sha256>.json.next`, and atomically rename it to the expected
filename. The Cloud request then resumes through the normal validation and
receipt path.

The exchange directory must be readable and writable only by the Paper service
account. Remove the temporary exchange after the receipt is verified. Never
copy model credentials, exchange keys, or live-trading secrets into the
repository, exchange directory, logs, screenshots, Issues, or PRs.
