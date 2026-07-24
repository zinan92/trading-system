# Paper launchd Python compatibility gate

Before restarting or deploying a local Paper service, verify the interpreter
that launchd actually runs:

```bash
/usr/bin/python3 -m pipelines.launchd_python_compatibility --json
```

The command imports the dashboard, Paper tick, daily-report pipeline, schedule
manager, and strategy control plane using `/usr/bin/python3` (the local
launchd Python 3.9). It writes a durable, credential-free receipt to
`outputs/runtime_compatibility/launchd_python_current.json`.

The deployable gate is the wrapper below; it runs that same actual-interpreter
check, verifies the dashboard API surface, and writes a release receipt:

```bash
/usr/bin/python3 -m pipelines.paper_predeploy_gate --json
```

`status=pass` is required before a Paper service restart. It writes
`outputs/release_gates/paper_predeploy_current.json` with the nested
compatibility receipt. `status=blocked` exits non-zero and is a deployment
blocker: fix the reported interpreter or import failure first. The gate neither
reads exchange keys nor starts, stops, submits, cancels, or closes any order.
