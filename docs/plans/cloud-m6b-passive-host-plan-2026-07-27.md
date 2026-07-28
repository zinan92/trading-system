# Cloud M6b passive host implementation plan

Issue: [#398](https://github.com/zinan92/trading-system/issues/398)

## Outcome

Provision one Alibaba Cloud Simple Application Server in Singapore from a
reviewed, source-bound plan, then install the Paper stack in passive mode. The
live-tick timer remains disabled and no strategy or order control path is
called.

## Implementation

1. Validate the purchased Simple Application Server against the checked-in
   contract: Singapore `ap-southeast-1`, Ubuntu 24.04 x86_64, 2 vCPU, 2 GiB
   memory, and 40 GiB storage.
2. Build deterministic local source archives and Git bundles from the exact
   trading-system and datafeed Git SHAs. Render a checksum-pinned bootstrap
   which verifies both forms before extraction, reconstructs a clean Git
   checkout for runtime source attestation, creates the `gridmind` account and
   persistent paths, and installs Python 3.13.7, isolated virtual environments,
   cloudflared, and passive systemd units.
3. Keep ports 8100, 8765, and 8766 loopback-only and never expose application
   ports through the provider or host firewall. SSH remains key-only for
   attended provisioning.
4. Start the datafeed, run the read-only Cloud preflight, and activate the
   Dashboard only after the preflight passes. Do not enable the live-tick,
   report, review, backup, or dead-man timers.
5. After the Alibaba Cloud login/payment boundary, upload the two exact source
   archives and bootstrap over SSH, inspect the secret-free provisioning
   receipt, configure a dedicated Cloudflare Tunnel outside Git, activate
   authenticated remote access, and rehearse restore into temporary paths.

## Verification

- Unit tests cover instance-contract validation, deterministic source archives,
  dry-run purity, source mismatch, failed commands, scheduler-disabled
  invariants, private application ports, and secret redaction.
- Shell syntax and placeholder completeness are validated without running
  cloud-init.
- Focused M1-M6 service, preflight, access, backup, ownership, and cutover tests
  remain green.
- The real host must provide receipts for exact SHAs, preflight, passive
  services, firewall, authenticated Dashboard access, and temporary restore
  before Issue #398 closes.

## Safety boundary

Alibaba Cloud login, identity, CAPTCHA, provider agreement, and payment remain
user-controlled. Provisioning is Paper-only. It does not copy active state,
activate a scheduler owner, start Grid/DCA, or touch live credentials.
