# Cloud Paper runtime (M1)

These files define the dependency and path contract only. They do not install
services or start a Paper strategy.

Build three isolated environments on a Linux host:

```bash
set -a
. deploy/cloud/source-lock.env
set +a
git clone "$GRIDMIND_DATAFEED_REPOSITORY" /opt/gridmind/src/datafeed
git -C /opt/gridmind/src/datafeed checkout --detach "$GRIDMIND_DATAFEED_SHA"

python3.13 -m venv /opt/gridmind/venvs/app
/opt/gridmind/venvs/app/bin/pip install -r deploy/cloud/requirements-app.txt

python3.13 -m venv /opt/gridmind/venvs/nautilus
/opt/gridmind/venvs/nautilus/bin/pip install -r deploy/cloud/requirements-nautilus.txt

python3.13 -m venv /opt/gridmind/venvs/datafeed
/opt/gridmind/venvs/datafeed/bin/pip install /opt/gridmind/src/datafeed
```

Copy `runtime.env.example` to a host-owned environment file and keep any
secret delivery values in `/etc/gridmind/paper.env` with mode 0600. Do not
commit that file.

After the independent datafeed is listening on loopback, run the read-only
preflight from a clean deployed checkout:

```bash
set -a
. /etc/gridmind/runtime.env
set +a
/opt/gridmind/venvs/app/bin/python -m pipelines.cloud_paper_preflight --json
```

The receipt is written to
`$TRADING_ORCHESTRATOR_OUTPUT_ROOT/cloud/preflight/current.json`. A
`status=blocked` result must stop the later service installer. This command
does not call the trading control plane or the cycle runner.

Render and stage the systemd surface without activating any Paper scheduler:

```bash
/opt/gridmind/venvs/app/bin/python -m pipelines.cloud_systemd \
  --repo-root /opt/gridmind/src/trading-system \
  --datafeed-root /opt/gridmind/src/datafeed \
  --app-python /opt/gridmind/venvs/app/bin/python \
  --datafeed-python /opt/gridmind/venvs/datafeed/bin/python \
  --render-dir /opt/gridmind/rendered-systemd

# On the Linux host, after reviewing the JSON dry-run:
sudo /opt/gridmind/venvs/app/bin/python -m pipelines.cloud_systemd \
  --repo-root /opt/gridmind/src/trading-system \
  --datafeed-root /opt/gridmind/src/datafeed \
  --app-python /opt/gridmind/venvs/app/bin/python \
  --datafeed-python /opt/gridmind/venvs/datafeed/bin/python \
  --render-dir /opt/gridmind/rendered-systemd \
  --action install-passive --apply
```

`install-passive` starts only the independent datafeed. Run the full Cloud
Paper preflight after it becomes healthy, then use `--action
activate-dashboard --apply`. Neither action enables the live-tick, report, or
dead-man timers. Scheduler activation belongs to the later single-owner
cutover contract.

The focused daily self-review is scheduled for 17:10 UTC (01:10 Beijing),
after the terminal 24-hour report. It writes immutable evidence revisions
under `outputs/dualtrack/daily_self_reviews/` and is readable from:

```text
GET /api/trading-system/daily-self-review
GET /api/trading-system/daily-self-review?date=YYYY-MM-DD
```

The review may recommend a service recovery or human strategy decision. It
executes no service, strategy, risk, order, position, or live-money action.
