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
- Runtime RT-08 is merged in PR #46 at `c0ee74c`: typed canonical host requests/queries across six ports, explicit binding provenance and safety validation, canonical receipt normalization, and Recording Track raw-payload rejection.
- Runtime RT-09 is merged in PR #47 at `c689a29`: Paper runtime readiness evidence with distinct Paper/Testnet/Live identity, reconciliation evidence schema, provenance/secret checks, and explicit pending external gate.
- SB-EXT-01A is merged in PR #57 at `043074b`: reusable typed external Broker host context and canonical receipt seam with runtime/transport attestation, digest/provenance binding, Mainnet fail-closed behavior, and fixture-only public-boundary tests.
- Decision A is recorded in `docs/adr/0005-rt06-reconciliation-seam.md`: RT-06 is not a second order lifecycle owner; RT-08 will compose the unique lifecycle.

## Next

- TESTNET-01 (#30) is merged in PR #48 at `f5dbf87`: human-gated Hyperliquid Testnet proof for one default validator-operated perpetual, including submit/query/cancel-replace/fill/fee/position/reconciliation evidence. The final account was flat with no open orders; evidence is stored outside the repository.
- SB-EXT-01B–01G (#51–#56): remain blocked behind the merged external host binding and are not implemented by PR #57.
- Mainnet/live remains a separate future milestone requiring its own specification, credentials, release identity, approval, and evidence.

## Safety boundary

- Paper remains the default development environment. The Hyperliquid Testnet proof is a separate, explicitly approved evidence class and is not live authority or a host-wide default.
- No Mainnet/live credentials, network I/O, or live execution.
- The trading system remains the host for strategy, risk, Recording Track, and Telegram control.
