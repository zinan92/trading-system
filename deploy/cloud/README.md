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

## Authenticated remote access and layered health

Never point a public tunnel at ports 8100 or 8765. The public chain is:

```text
Cloudflare Access -> cloudflared -> 127.0.0.1:8766 allowlist gateway
                  -> 127.0.0.1:8765 Dashboard
```

Copy `cloudflared.yml.example` to `/etc/gridmind/cloudflared.yml`, replace only
the tunnel ID and authenticated hostname, and keep the credential JSON outside
the Git checkout. In `/etc/gridmind/paper.env` configure
`GOLDBOT_ACCESS_TEAM_DOMAIN`, `GOLDBOT_ACCESS_AUD`, and
`GOLDBOT_ACCESS_EMAIL`. The gateway validates the signed Access JWT and
allowlists exact Dashboard assets/APIs; assertions and tunnel/dead-man tokens
never enter its audit log.

After Dashboard preflight passes, `activate-remote-access` may enable the
gateway and tunnel. It does not enable the live-tick scheduler:

```bash
sudo /opt/gridmind/venvs/app/bin/python -m pipelines.cloud_systemd \
  --repo-root /opt/gridmind/src/trading-system \
  --datafeed-root /opt/gridmind/src/datafeed \
  --app-python /opt/gridmind/venvs/app/bin/python \
  --datafeed-python /opt/gridmind/venvs/datafeed/bin/python \
  --render-dir /opt/gridmind/rendered-systemd \
  --action activate-remote-access --apply
```

`GET /api/trading-system/cloud-health` reports datafeed freshness, live tick,
execution, reconciliation, daily self-review, backup, scheduler ownership, and
deployed SHA separately. Dashboard reachability is explicitly not treated as
system health. In Cloud mode, the dead-man sends its fail signal when any
blocking or degraded layer exists; its persisted receipt records only
`target_kind=success|fail`, never the configured URL.

The focused daily self-review is scheduled for 17:10 UTC (01:10 Beijing),
after the terminal 24-hour report. It writes immutable evidence revisions
under `outputs/dualtrack/daily_self_reviews/` and is readable from:

```text
GET /api/trading-system/daily-self-review
GET /api/trading-system/daily-self-review?date=YYYY-MM-DD
```

The review may recommend a service recovery or human strategy decision. It
executes no service, strategy, risk, order, position, or live-money action.

Backups run after the review and use the SQLite backup API for the independent
datafeed. Each backup has a file/size/SHA manifest and excludes the secret
environment file, which remains outside `outputs/`. `GRIDMIND_BACKUP_ENCRYPTION_MODE`
records the at-rest/export protection contract; the initial provider volume
must have encryption at rest enabled.

Restore always targets a new empty output root and a new datafeed database. It
never overwrites the active namespace, never activates a scheduler, and marks
application reconciliation as a required next step.

The live-tick also consumes the persistent scheduler owner. A new Cloud host
must not have a valid owner by default. The later cutover controller performs
the only allowed sequence:

```text
local-mac active -> paused -> cloud owner active
```

Rollback uses the same pause boundary in reverse. There is no dual-owner
transition and no public low-level transition command in this milestone.
