# standard-broker Registry

## Now

- The approved canonical Broker vocabulary and architecture are recorded in `CONTEXT.md` and `docs/adr/`.
- Hyperliquid research and the accepted build Spec are recorded in the repository documentation and GitHub Spec Issue #2.
- T01 is merged in PR #11 and establishes the six-port Paper Broker Contract seam with local-only transport and fail-closed capability checks.
- T02 is merged in PR #12: Hyperliquid default-perps market data and instrument mapping.
- T03 is merged in PR #13: Hyperliquid account, fee, funding, and liquidation facts.
- T04 is merged in PR #14: Hyperliquid order lifecycle and reconciliation.
- T05 is merged in PR #15: Hyperliquid protection order semantics.
- T06 is merged in PR #16: Nautilus Hyperliquid compatibility bridge.
- T07 is merged in PR #17: Broker conformance, security, and Paper proof.
- T08 is merged in PR #18: trading-system host integration contract.
- Runtime RT-01 is merged in PR #31: explicit Hyperliquid runtime session and environment boundary.
- Runtime RT-02 is merged in PR #32: Paper-safe runtime read path.
- Runtime RT-03 is merged in PR #33: Paper-safe runtime order lifecycle.
- Runtime RT-04 is merged in PR #34: runtime account, position, fee, and funding facts.
- Runtime RT-05 is merged in PR #35: runtime lifecycle and fact integration.
- Runtime RT-06 is merged in PR #36 at `d83812f`: Paper-only reconciliation/resilience seam with cursor-bound snapshots, transactional rollback, and fail-closed retry leases.
- Runtime RT-07 is merged in PR #39 at `006512a`: ProtectionOrderPort lifecycle with explicit mark/sibling capabilities, reduce-only enforcement, fixed/position-following coverage gaps, Paper SUBMITTED/FROZEN states, and auditable bounded retry plans.
- Decision A is recorded in `docs/adr/0005-rt06-reconciliation-seam.md`: RT-06 is not a second order lifecycle owner; RT-08 will compose the unique lifecycle.

## Next

- RT-08 (#28): compose the canonical host binding and Recording Track contract; this is the next step toward external execution but does not authorize it. ACTIVE protection evidence remains reserved for Broker/ledger observations.
- RT-09 (#29): conformance/security/Paper-only proof for the runtime composition.
- TESTNET-01 (#30): separate human-gated external testnet proof; local Testnet fixtures exist, but external proof is not started and not authorized.
- No live/testnet milestone is implied; future external environments require a separate approved plan.

## Safety boundary

- Paper only; no testnet or mainnet credentials, network I/O, or live execution.
- The trading system remains the host for strategy, risk, Recording Track, and Telegram control.
