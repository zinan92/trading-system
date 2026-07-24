# Paper release and rollback runbook (v1)

This is the release checklist for the local **Paper-only** GridMind dashboard
and its supporting Paper services. It is a procedure and evidence contract,
not an order-control script. It never authorizes live execution, exchange-key
use, venue configuration, starting a strategy, cancelling an order, or
flattening a position.

## 1. Establish the release candidate

Record the exact merge candidate before changing a local service:

```bash
git fetch origin main
git rev-parse origin/main
git status --short
```

The candidate must be a clean checkout of the recorded `origin/main` commit.
Save that commit in the release evidence entry; a branch name or a green local
test alone is not a deployment receipt.

## 2. Block unsafe deployment before any restart

Run the actual launchd-interpreter gate first:

```bash
/usr/bin/python3 -m pipelines.paper_predeploy_gate --json
```

Continue only when the output is `status=pass`. The durable receipt is
`outputs/release_gates/paper_predeploy_current.json`, including its nested
`outputs/runtime_compatibility/launchd_python_current.json` compatibility
result. A blocked gate is a stop condition: fix the interpreter/import/API
surface first. Do not use a developer-shell Python result as a substitute.

## 3. Release the Paper service narrowly

Use the existing local service manager only for the intended Paper component.
The canonical labels are:

- Dashboard: `com.wendy.trading-orchestrator.dashboard`
- Market/lifecycle scheduler: `com.wendy.trading-orchestrator.dualtrack-live-tick`

Do not restart both by default. Do not use `start.command` for a managed Paper
release: it kills the port listener and starts a foreground runner. Never
restart, stop, or inspect any live/real-money process as part of this runbook.

Before a scheduler restart, retain the current runtime, accepted-order,
position, and reconciliation facts as evidence. A process restart is delivery
evidence only; it is not proof that data is fresh or that an order executed.

## 4. Verify four separate evidence surfaces

After the narrow Paper release, record each surface independently.

| Surface | Required proof | It does **not** prove |
|---|---|---|
| Delivery | recorded `main` SHA and the intended Paper service restart | data freshness or execution |
| Health | local API response and current runtime/read-model state | a strategy is allowed to start |
| Data | trusted-market freshness/quality and tick-health facts | an order was accepted or filled |
| Execution | accepted-order/position/lifecycle/reconciliation receipts | a browser toast or K-line crossing |

Health/read-model probe (read-only):

```bash
curl --fail --silent --show-error http://127.0.0.1:8765/api/trading-system/read-model > /tmp/gridmind-read-model.json
```

Inspect the saved JSON for the current runtime, strategy plan, order and
position counts, reconciliation result, and data/tick state. If the request
fails, report it as a Dashboard/gateway failure rather than guessing whether a
command reached the backend.

## 5. Prove the changed user flow

Run the focused browser regression for the changed surface before declaring the
release complete. The baseline Paper operator-flow command is:

```bash
python3 -m pytest -q tests/test_dashboard_gridmind_operator_journey_browser.py
```

For the actual local dashboard, open
`http://127.0.0.1:8765/dashboard-v5.html`, exercise the changed **read-only or
Paper-safe** flow once, and save a screenshot plus the relevant read-model or
control receipt. A test fixture and a screenshot prove UI behavior; only
accepted lifecycle and reconciliation artifacts prove Paper execution.

## 6. Release evidence location

Create one dated release note under `docs/evidence/` or link the corresponding
GitHub Issue/PR. It must include:

1. merge SHA and time;
2. pre-deploy receipt status/path;
3. service label changed (if any);
4. health/read-model result;
5. data/tick freshness result;
6. changed-flow browser command, result, and screenshot path; and
7. explicit Paper runtime/order/position/reconciliation facts.

Redact credentials and never paste a key, signed request, or secret-bearing
environment value into the evidence.

## 7. Roll back safely

Roll back only the component released in step 3 and preserve the failed
candidate evidence before changing it. Record the known-good main SHA, restore
that checkout through the same Paper-only service-manager path, re-run the
pre-deploy gate, then repeat sections 4 and 5.

Do **not** roll back by deleting outputs, fabricating lifecycle facts, issuing
control commands, or silently switching data providers. If runtime/order state
is uncertain, stop the release and use the authoritative read model and
reconciliation records to determine the next human action. Any live/real-money
rollback remains outside this runbook and requires separate human approval.
