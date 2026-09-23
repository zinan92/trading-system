# standard-broker Registry

## Now

- Issue #139 (in review): read-only Hyperliquid Mainnet BTC sub-account profile `hyperliquid-mainnet-btc-readonly`; no write path. Capped submit is #140, gated on #139 and a completed Testnet grid cycle.
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
- SB-EXT-04 is merged in PR #71 at `24a60cb`: the exact Testnet profile composes host-backed typed observations into a watermark-bound `ExternalReconciliationSnapshot` with freshness/skew/digest checks; missing or incoherent facts remain non-coherent.
- SB-EXT-05 is merged in PR #74 at `6856781`: the exact runtime factory maps instrument metadata through the public host fact seam before constructing the consumer canary binding; consumers do not assemble provider objects or raw runtime payloads.
- SB-EXT-06 is merged in PR #77 at `a1fb8ba`: an opt-in
  `hyperliquid-testnet-position-protection` profile exposes a typed external
  position-level TP/SL binding with reduce-only/OCO mapping, grouped-response
  handling, remote query/coverage confirmation, digest-bound observations, and
  fail-closed unknown/cancel-replace behavior. The default
  `hyperliquid-testnet-default` profile remains protection-disabled.
- SB-EXT-28 / Issue #120 is merged in PR #121 at `f48d2a2`: external
  ProtectionOrder runtime receipts preserve canonical identity, operation,
  state, acceptance, observed coverage, order identities, and digest; invalid,
  stale, mismatched, canceled, filled, or partial observations remain
  fail-closed. The shipped position-protection capability descriptor is wired
  to the supported semantic operations.
- RT-10 / Issue #94 is merged in PR #95 at `25a04be`: public mapping of the
  Nautilus Testnet `AccountState` into the canonical account snapshot, with
  deterministic sole-USDC fallback and account-wide no-fill reads.
- RT-11 / Issue #96 is merged in PR #97 at `65d4bc3`: persisted canonical
  client-identity recovery for an ambiguous external order, with no transport
  call during recovery. RT-11 validation dispatch is fixed in PR #99 at
  `883e1d4`.
- RT-12 / Issue #100 is merged in PR #101 at `d241708`: order-scoped fill
  recovery falls back to typed instrument scope and filters to the recovered
  canonical order identity.
- RT-13 / Issue #102 is merged in PR #103 at `28d9a7b`: public client-scoped
  fill recovery preserves canonical cache identity. RT-14 / Issue #104 is
  merged in PR #105 at `4e5cf2e`: the typed client scope is forwarded to the
  native fill query.
- RT-15 / Issue #106 is merged in PR #107 at `26df219`: deterministic Nautilus
  native CLOID normalization returns canonical identity across fresh-process
  recovery. RT-16 / Issue #108 is merged in PR #109 at `1c0d970`: unknown or
  conflicting CLOID/OID reports fail closed instead of satisfying causal
  reconciliation.
- Post-RT-16 validation: standard-broker full suite `291 passed, 2 skipped`;
  pinned Nautilus external-backend/canary suite `28 passed`. No new order,
  cancel, retry, credential, Mainnet/Live, or cloud action was performed.
- Decision A is recorded in `docs/adr/0005-rt06-reconciliation-seam.md`: RT-06 is not a second order lifecycle owner; RT-08 will compose the unique lifecycle.

## Next

- TESTNET-01 (#30) is merged in PR #48 at `f5dbf87`: human-gated Hyperliquid Testnet proof for one default validator-operated perpetual, including submit/query/cancel-replace/fill/fee/position/reconciliation evidence. The final account was flat with no open orders; evidence is stored outside the repository.
- SB-EXT-01A–01G and SB-EXT-02/03/04/05/06 are complete in standard-broker.
  SB-EXT-28 is also complete. The next dependency is trading-system's
  opt-in DCA/Grid attended Testnet proof against the position-protection
  profile. No strategy, soak, scheduler, or Mainnet/Live authority is implied
  by this broker merge; external proof still requires an attended plan and
  approval.
- The current trading-system Path A cleanup remains externally blocked: the
  account is flat with no open orders, but the ambiguous close has no causal
  public fill/fee identity. This broker registry does not promote that state
  to `FLAT_RECONCILED`; see trading-system Issue #958.
- Mainnet/live remains a separate future milestone requiring its own specification, credentials, release identity, approval, and evidence.

## Safety boundary

- Paper remains the default development environment. The Hyperliquid Testnet proof is a separate, explicitly approved evidence class and is not live authority or a host-wide default.
- No Mainnet/live credentials, network I/O, or live execution.
- The trading system remains the host for strategy, risk, Recording Track, and Telegram control.
