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
