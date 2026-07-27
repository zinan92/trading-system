# Cloud Paper deploy, cutover, and rollback v1

This runbook moves only the Paper scheduler. It never starts a strategy and
never authorizes live/real-money execution.

## 1. Human-only provider boundary

Automation stops for cloud-account login, identity verification, billing or
payment authorization, CAPTCHA, and provider agreements. Park completes those
steps in the provider UI. No credential, card, recovery code, or Access token
is copied into chat, GitHub, logs, screenshots, or receipts.

After the VM exists, automation resumes with its public IP/hostname and the
provider-confirmed region. Host SSH keys remain in the user's SSH agent or a
host-owned secret store.

## 2. Immutable deployment

Render the exact non-secret deployment manifest:

```bash
python -m pipelines.cloud_deploy_manifest \
  --trading-system-sha <40-char-main-SHA> \
  --datafeed-sha <40-char-datafeed-SHA> \
  --hostname <authenticated-hostname> \
  --region <provider-region>
```

The deployed checkout is detached at the recorded SHA. `/etc/gridmind/paper.env`
is created directly on the host with mode 0600; it is never sourced from the
Git checkout. Install systemd passively, start only the loopback datafeed, run
Cloud preflight, then activate Dashboard and authenticated remote access.
Live-tick/report/review/backup timers remain disabled.

## 3. Mandatory pre-cutover truth

`CloudPaperCutover.precheck()` must report every item true:

- local authoritative runtime is `stopped`;
- accepted-order count is known and zero;
- current Nautilus snapshot has zero open/non-zero positions;
- current execution reconciliation is `ok` or `pass`;
- a current verified backup receipt has timestamp and manifest hash;
- local scheduler owner is active and matches `local-mac`;
- Cloud preflight is Paper-only, side-effect-free, and passes at the exact
  deployed SHA;
- Cloud live-tick timer is disabled and inactive.

Any false or unknown item blocks before service or ownership mutation.

## 4. Forward cutover

Run dry-run first. The applied state machine is fixed:

```text
disable local tick
-> local owner active -> paused
-> create paused-state backup
-> transfer, restore, and hash-verify paused state
-> cloud owner paused -> active
-> enable cloud tick
-> require layered cloud health = healthy
```

If any step after pause fails, the controller disables Cloud tick and leaves
local tick disabled. It does not replay a control request, start a strategy,
cancel an order, or flatten a position. The next action is explicit rollback.

## 5. Rollback

Rollback is the reverse single-owner transition:

```text
disable cloud tick
-> cloud owner active -> paused
-> transfer the higher paused epoch to local
-> import only a validated monotonic paused state
-> local owner paused -> active
-> enable local tick
```

Failure keeps schedulers disabled. Never restore an older owner epoch or
manually edit `scheduler_ownership/current.json`.

## 6. Evidence

Required receipts:

- `outputs/cloud/cutover/precheck_current.json`
- `outputs/cloud/cutover/cutover_current.json`
- `outputs/cloud/cutover/rollback_current.json` when rollback is used
- Cloud preflight, backup manifest, layered Cloud health, and systemd state

Every receipt is Paper-only, source-bound, and secret-free. A deploy manifest,
successful SSH copy, or reachable Dashboard is not proof that cutover or the
24-hour soak succeeded.
