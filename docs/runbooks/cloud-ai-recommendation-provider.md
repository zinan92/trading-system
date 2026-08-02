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

Provider readiness is a separate, source-bound receipt.  Before enabling or
restarting the live-tick timer, run the Cloud readiness pipeline as `gridmind`.
It must prove the executable/version, `codex login status`, the bounded
recommendation command, and the JSON response contract.  The receipt records
only boolean/status facts and the deployed source SHA/tree; it never records
the prompt, response, access token, or other secret.  The live-tick boot gate
rejects a missing, expired, failed, or source-mismatched readiness receipt.

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
