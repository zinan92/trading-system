# Cloud M6b passive host implementation plan

Issue: [#398](https://github.com/zinan92/trading-system/issues/398)

## Outcome

Provision one AWS Lightsail host in Singapore from a reviewed, source-bound
plan, then install the Paper stack in passive mode. The live-tick timer remains
disabled and no strategy or order control path is called.

## Implementation

1. Render a deterministic Lightsail plan from the provider catalog. Select an
   active Ubuntu 24.04 Linux blueprint, the lowest-cost bundle with at least
   2 vCPU and 2 GB RAM, and an availability zone in `ap-southeast-1`.
2. Render checksum-pinned cloud-init with the exact trading-system and datafeed
   SHAs. It creates the `gridmind` account and persistent paths, installs Python
   3.13.7, isolated virtual environments, cloudflared, and passive systemd
   units.
3. Restrict the Lightsail firewall to SSH from the operator CIDR. Ports 8100,
   8765, and 8766 remain loopback-only and are never public.
4. Start the datafeed, run the read-only Cloud preflight, and activate the
   Dashboard only after the preflight passes. Do not enable the live-tick,
   report, review, backup, or dead-man timers.
5. After the AWS login/payment boundary, apply the plan, inspect the
   secret-free provisioning receipt, configure a dedicated Cloudflare Tunnel
   outside Git, activate authenticated remote access, and rehearse restore into
   temporary paths.

## Verification

- Unit tests cover catalog selection, dry-run purity, missing provider context,
  source mismatch, failed commands, scheduler-disabled invariants, firewall
  shape, and secret redaction.
- Shell syntax and placeholder completeness are validated without running
  cloud-init.
- Focused M1-M6 service, preflight, access, backup, ownership, and cutover tests
  remain green.
- The real host must provide receipts for exact SHAs, preflight, passive
  services, firewall, authenticated Dashboard access, and temporary restore
  before Issue #398 closes.

## Safety boundary

AWS login, identity, CAPTCHA, provider agreement, and payment remain
user-controlled. Provisioning is Paper-only. It does not copy active state,
activate a scheduler owner, start Grid/DCA, or touch live credentials.
