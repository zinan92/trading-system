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

## Password-authenticated remote access and layered health

Never point a public tunnel at ports 8100 or 8765. The public chain is:

```text
Cloudflare Tunnel -> 127.0.0.1:8766 password/session allowlist gateway
                  -> 127.0.0.1:8765 Dashboard (independent assertion check)
```

Copy `cloudflared.yml.example` to `/etc/gridmind/cloudflared.yml`, replace only
the tunnel ID and hostname, and keep the credential JSON outside the Git
checkout. `GOLDBOT_ACCESS_EMAIL` remains the internal Park actor binding; it
is not a login field. Provision a non-reversible password record and an
independent random session-signing secret through the private prompt:

```bash
sudo install -d -o gridmind -g gridmind -m 0700 /etc/gridmind/dashboard-auth
sudo -u gridmind /opt/gridmind/venvs/app/bin/python \
  -m services.cloud_password_auth provision \
  --password-file /etc/gridmind/dashboard-auth/password.scrypt \
  --session-secret-file /etc/gridmind/dashboard-auth/session-secret
sudo chmod 600 /etc/gridmind/dashboard-auth/password.scrypt \
  /etc/gridmind/dashboard-auth/session-secret
```

The command never accepts the password as an argument or environment value.
The public gateway requires an expiring Secure/HttpOnly session and exact
same-origin POST; the loopback Dashboard re-verifies the signed assertion
before accepting Park as the actor. Only exact Dashboard assets/APIs are
allowlisted, logout revokes the server-side session, and neither passwords,
assertions, tunnel credentials, nor dead-man tokens enter the audit log.

Keep the Cloudflare Access application attached until password login, logout,
anonymous denial, and a non-mutating `preview` have passed against the origin.
Only then replace the OTP policy with the narrowly scoped bypass for this one
hostname. Roll back by removing that bypass and reattaching the previous Access
policy; this restores email OTP without changing trading state.

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

The focused daily self-review is scheduled for 01:10 UTC (09:10 Beijing),
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

The source-bound deployment and single-owner transition are specified in
[`docs/runbooks/cloud-paper-cutover-v1.md`](../../docs/runbooks/cloud-paper-cutover-v1.md).
Render `pipelines.cloud_deploy_manifest` before provisioning. The actual
cutover controller is dry-run by default and its remote/local service ports
must be supplied by the provider adapter; no generic shell execution surface
is exposed.

## AWS Lightsail passive host

The initial provider package is driven by `lightsail-plan.json`. It discovers
the current provider catalog instead of hard-coding blueprint, bundle, or
availability-zone IDs. Rendering is read-only and apply remains explicit:

```bash
python -m pipelines.cloud_lightsail catalog > /tmp/lightsail-catalog.json
python -m pipelines.cloud_lightsail render \
  --catalog /tmp/lightsail-catalog.json \
  --operator-cidr <CURRENT_PUBLIC_IPV4>/32 \
  --key-pair-name <EXISTING_LIGHTSAIL_KEY_PAIR> \
  --render-dir /tmp/gridmind-lightsail
python -m pipelines.cloud_lightsail apply \
  --plan /tmp/gridmind-lightsail/provision-plan.json
```

The final command above is still a dry run. Only after the AWS
login/identity/payment boundary and plan review may the operator append
`--apply`. Cloud-init verifies exact source revisions and pinned binary hashes,
installs passive services, starts the datafeed, runs preflight, and starts the
loopback Dashboard. It fails if the live-tick timer is enabled.

The Lightsail firewall plan exposes only SSH restricted to the supplied
operator `/32`; ports 8100, 8765, and 8766 are never public. Authenticated
Dashboard access is activated separately after a dedicated Cloudflare Tunnel
credential and Access policy are installed in host-owned paths.

## Alibaba Cloud Simple Application Server passive host

`aliyun-plan.json` is the reviewed contract for the purchased Singapore host:
Ubuntu 24.04 x86_64, 2 vCPU, 2 GiB memory, and 40 GiB storage. The deployment
does not place a GitHub credential on the host. Instead, it creates
deterministic source archives and Git bundles from the exact local Git SHAs,
uploads them over key-only SSH, and verifies their SHA256 digests before
reconstructing clean, attestable checkouts.

```bash
python -m pipelines.cloud_aliyun render \
  --datafeed-root /path/to/datafeed \
  --render-dir /tmp/gridmind-aliyun \
  --host <PUBLIC_IP> \
  --ssh-key /path/to/private-key
python -m pipelines.cloud_aliyun apply \
  --plan /tmp/gridmind-aliyun/deploy-plan.json
```

The second command is a dry run unless `--apply` is appended. Bootstrap starts
only the loopback datafeed and Dashboard after preflight. It explicitly
disables the live-tick timer, never starts Grid or DCA, and writes only a
secret-free provisioning receipt. Cloudflare Tunnel credentials and Access
policy remain outside Git and are installed only after loopback acceptance.
