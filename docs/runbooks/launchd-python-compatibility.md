# Paper launchd Python compatibility gate

Before restarting or deploying a local Paper service, verify the interpreters
that its installed launchd plists actually resolve:

```bash
python3 -m pipelines.launchd_python_compatibility --json
```

The command reads the Dashboard and `dualtrack-live-tick` plists, resolves each
`ProgramArguments[0]` using that job's own `PATH`, and probes them independently.
It also probes `TRADING_ORCHESTRATOR_NAUTILUS_PYTHON` as a distinct Nautilus
dependency; that environment value is not described as the launcher for every
job. The durable credential-free receipt is
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
