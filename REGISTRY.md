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
- SB-EXT-01B is merged in PR #58 at `c7d6841`: exact Hyperliquid Testnet profile resolution, API-agent signer binding, runtime metadata attestation, approval/release/lifecycle checks, capability preflight, and no-fallback tests.
- SB-EXT-01C is merged in PR #60 at `509cb0f`: canonical external Hyperliquid ticker/instrument/account/position/fee/fill fact envelopes with freshness, provenance, identity, lifecycle, and non-secret digest binding; incomplete data and unsupported funding/liquidation reads fail closed.
- SB-EXT-01D is merged in PR #61 at `5b2b946`: typed external Hyperliquid order facade with exact Testnet lifecycle binding, external fill query/normalization, idempotency and lineage preservation, stale/terminal event protection, and fail-closed ambiguous retry handling.
- SB-EXT-01E is merged in PR #62 at `b274e95`: cursor/watermark-bound external reconciliation evidence across account, positions, orders, fills, fees, and funding, with identity/drift/stale/unknown/incomplete fail-closed outcomes and digest-only canonical evidence.
- SB-EXT-01F is merged in PR #59 at `f9cada9`: explicit protection capability matrix, reduce-only-close distinction, and ProtectionGroup fail-closed gating for unsupported external protection semantics.
- SB-EXT-01G is merged in PR #63 at `81fe9ed`: preflight-only external conformance report, exact-profile/no-fallback proof, full identity/digest checks, strict secret/raw rejection, complete protection-gap proof, and pinned trading-system handoff.
- SB-EXT-02 is merged in PR #65 at `9f11651`: public attended Testnet canary order binding with enriched canonical request/receipt identity, idempotency surface, market/instrument capability preflight, and typed fill/fee/account/position/open-order/reconciliation contracts.
- SB-EXT-03 is merged in PR #68 at `8cf99b4`: typed fact reads stay behind `ExternalBrokerHost.read_fact()`, consumer code cannot call private runtime/native methods, and the canary facts reader requires a real cursor-bound `ExternalReconciliationSnapshot` provider instead of manufacturing one from independent reads.
- Decision A is recorded in `docs/adr/0005-rt06-reconciliation-seam.md`: RT-06 is not a second order lifecycle owner; RT-08 will compose the unique lifecycle.

## Next

- TESTNET-01 (#30) is merged in PR #48 at `f5dbf87`: human-gated Hyperliquid Testnet proof for one default validator-operated perpetual, including submit/query/cancel-replace/fill/fee/position/reconciliation evidence. The final account was flat with no open orders; evidence is stored outside the repository.
- SB-EXT-01A–01G and SB-EXT-02/03 are complete in standard-broker. The next dependency is a separate `trading-system` wrapper and Park-attended #895 run. Without a real cursor-bound snapshot reader, the public canary binding fails closed; no strategy, ProtectionOrder, soak, or Mainnet/Live authority is implied.
- Mainnet/live remains a separate future milestone requiring its own specification, credentials, release identity, approval, and evidence.

## Safety boundary

- Paper remains the default development environment. The Hyperliquid Testnet proof is a separate, explicitly approved evidence class and is not live authority or a host-wide default.
- No Mainnet/live credentials, network I/O, or live execution.
- The trading system remains the host for strategy, risk, Recording Track, and Telegram control.
