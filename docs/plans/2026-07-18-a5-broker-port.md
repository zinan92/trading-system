# A5 Broker Port And Composition Roots Implementation Plan

**Status:** Implemented and verified on 2026-07-18.

**Goal:** Make broker selection a composition concern and venue behavior an
adapter concern, so a new broker can be added primarily through one adapter,
configuration/credentials, registry wiring, and the common conformance suite.

**Architecture:** Introduce a small engine-neutral Broker Port with explicit
capabilities, plus a registry-based composition root. Keep the proven Binance
and Tiger order, protection, guardrail, and reconciliation algorithms intact
behind their existing adapters. Route production factories and the
multi-strategy cycle through the registry instead of branching on provider
names. Replace core `hasattr`/private-method calls with declared cancel and
protection capabilities. Preserve the legacy `services.broker_adapter` imports
as a compatibility facade while new code depends on the port/composition root.
This is a strangler step, not a mass rewrite of the 2,067-line legacy adapter.

**Reference model:** NautilusTrader's official adapter guide separates
configuration/factories, execution clients, venue networking, and
reconciliation. A5 adopts that boundary without switching live-money authority
or rewriting the existing wire protocol implementation.

**Tech stack:** Python 3.13, existing broker adapters and reconciliation
services, frozen dataclasses/Protocols, pytest, Ruff.

---

## User outcome

The running strategy and control plane no longer need to know whether an order
goes to Binance or Tiger. Selecting a broker is configuration plus registry
composition. Each venue advertises exactly what it can do; unsupported cancel
or protective recovery fails before a provider-private method is called. The
existing production gates and external behavior remain unchanged.

## Observable success criteria

1. One `BrokerExecutionPort` owns the normalized submit/preflight boundary;
   cancel, protective recovery, and reconciliation are explicit capabilities,
   not `hasattr` guesses or private provider calls.
2. One registry-based composition root resolves paper, Binance, Tiger, and
   legacy-supported live providers from execution mode, provider, and
   environment. Application services contain no adapter-constructor branch for
   Binance versus Tiger.
3. `StrategyControlPlane`, Dashboard command execution, and Strategy Lab remain
   provider-neutral; tests prevent broker-provider conditionals or imports from
   entering those command paths.
4. The multi-strategy runner obtains execution and reconciliation ports from
   the same composition boundary. Expiry cancellation and missing-protection
   recovery use public capability methods only.
5. Binance and Tiger pass the same conformance matrix for identity, preflight
   shape, normalized order receipt, secret safety, deterministic capability
   declaration, and fail-closed unsupported operations.
6. Existing approval, activation, live-money risk, preflight, same-day
   reconciliation, ambiguous-submit recovery, lifecycle, TP/SL attachment, and
   protective recovery behavior remains covered and passing.
7. Existing `services.broker_adapter` imports and factory APIs keep working
   during migration; no config flip, credential change, or real-money
   enablement is introduced.
8. The live Binance registry path still reaches the existing
   `real_money_ready` activation check, while demo/testnet retain their exact
   mode labels, flags, endpoints, and guardrail behavior. Registry resolution
   cannot turn an unregistered provider into an armed adapter.

## Scope

- In: broker request/port contracts; capability declarations; registry and
  composition root; compatibility facade; core factory/cycle-runner routing;
  Binance/Tiger conformance tests; architecture documentation; focused and full
  regression.
- Out: moving all Binance/OANDA/MT5 wire methods out of the legacy file;
  switching live execution authority to Nautilus; changing order semantics,
  limits, credentials, or activation flags; adding a new venue; redesigning
  reconciliation truth; frontend work.

## First-principles invariants

1. The application issues normalized intent; the venue adapter translates it.
2. Provider selection occurs only at a composition root.
3. A declared capability is part of the adapter contract. It is resolved from
   the concrete adapter plus normalized provider identity, defaults to absent,
   and never follows inherited method presence. Absence blocks before any
   network call.
4. Preflight, risk, activation, reconciliation, and protection are cumulative
   gates; composition can add structure but never remove a gate.
5. A transport timeout or ambiguous acknowledgement is not a rejection. The
   adapter leaves it recoverable and reconciliation resolves venue truth.
6. Credentials remain environment/file references. Descriptors, errors, and
   conformance artifacts never expose secret values.
7. Compatibility facades may delegate to the new port, but new application code
   cannot depend on provider-private helpers.

## Milestone 1 — Freeze the Broker Port and capability contract

### User outcome

Every broker looks the same to the application, while unsupported actions are
explicit and safe.

### Success criteria

1. Immutable normalized requests cover submit, cancel, and protective recovery
   without venue-native fields.
2. `BrokerExecutionPort` requires stable identity, preflight, capabilities, and
   normalized submit receipt behavior.
3. Capability names are closed, deterministic, and validated; unknown names
   fail construction.
4. Paper, legacy live, Binance demo/testnet, and Tiger expose capability sets
   without changing their order behavior. Both the Tiger paper subclass and a
   plain legacy live adapter configured as Tiger must exclude Binance cancel
   and protective-recovery capabilities.
5. Generic cancel maps an asset/order identity inside the Binance adapter; the
   runner no longer invokes `_binance_symbol` or `cancel_binance_order`.
6. Capability checks reject unsupported actions before a network method is
   reached.
7. Port submission returns the original normalized receipt identity and does
   not wrap, intercept, or reinterpret venue lifecycle transitions. An
   ambiguous Binance submission remains `submitting` and is resolved only by
   the existing recovery/reconciliation path.

### In scope / Out of scope

- In: `services/broker_port.py`, small compatibility methods on current
  adapters, pure contract tests.
- Out: factory routing and moving venue wire code.

## Milestone 2 — Add registry composition and compatibility facade

### User outcome

Choosing paper, Binance, or Tiger is configuration-driven and has one assembly
path.

### Success criteria

1. A registry resolves exact `(mode, provider, environment)` keys with explicit
   fallbacks and rejects duplicate registrations.
2. Default builders cover paper, Tiger paper, Binance demo/testnet/live, and the
   currently supported OANDA/MT5/manual compatibility paths.
3. Demo/testnet auxiliary config is passed as composition context, not read by
   core application branches.
4. Exact registry keys keep Binance `demo`, `testnet`, and `live` distinct even
   when demo/testnet share a base URL. The selected execution and
   reconciliation ports receive the same environment, endpoint, request
   namespace, and mode flags.
5. Unknown real-money providers remain fail-closed. Unknown demo providers
   preserve today's unarmed dry-run fallback; no wildcard or default registry
   entry can create an armed network adapter accidentally.
6. The Binance `live` builder uses the activation-gated base submit path and
   takes `dry_run`/`live_trading_enabled` only from the caller's resolved config;
   composition never forces an armed mainnet pair.
7. `build_broker_adapter`, `build_live_broker_adapter`, `broker_preflight`, and
   `resolve_broker_config` retain their signatures and delegate to the new root.
8. Existing monkeypatch/test seams keep working; configuration is loaded once
   at the outer factory boundary. Registry builders import venue adapters
   lazily, so the compatibility facade cannot recurse during import.

### In scope / Out of scope

- In: `services/broker_composition.py`, factory delegation in
  `services/broker_adapter.py`, registry unit tests.
- Out: changing config files or current active profiles.

## Milestone 3 — Cut core entry points over and prove conformance

### User outcome

The bot cycle can swap Binance and Tiger without changing execution logic.

### Success criteria

1. `MultiStrategyRunner._broker_adapter_for` has no provider-specific adapter
   construction and uses the registry for demo and live profiles.
2. Demo reconciliation is built through the reconciliation port; runner
   orchestration no longer imports Binance/Tiger reconciliation classes. Its
   endpoint is exactly the execution adapter's endpoint for the same
   environment.
3. Expired-entry cancellation uses `BrokerCancelRequest`; missing-protection
   recovery requires the declared capability before invoking the public port.
4. One parametrized conformance suite exercises Binance and Tiger through the
   composition root and asserts the same normalized boundary.
5. Provider-neutrality tests inspect only command-execution functions in
   Strategy Control Plane, Dashboard order dispatch, Strategy Lab, and the
   cycle-runner execution constructor path. They explicitly exempt
   `_execution_profile_for` and `_demo_reconciliation_block_reason`, which are
   diagnostic read-model presentation branches deferred to A6.
6. Existing broker, multi-strategy, risk, accounting, lifecycle, canary, and
   reconciliation suites pass unchanged except for intentional public-port
   expectations.
7. The common conformance suite proves the live Binance builder cannot order
   without `real_money_ready`, testnet executes the live-money guardrail branch,
   demo does not, and neither environment can reconcile against a different
   endpoint.

### In scope / Out of scope

- In: `services/multi_strategy_runner.py`, conformance/integration tests, small
  adapter public capability shims.
- Out: presentation-only venue diagnostics and onboarding UIs; A6 will expose
  their stable read model.

## Milestone 4 — Documentation, adversarial review, and closure

### User outcome

The broker plug-in boundary is inspectable, regression-safe, and ready for the
next venue without risking current execution.

### Success criteria

1. `docs/broker-port-spec.md` documents authority, capability, registry,
   credential, ambiguous-outcome, and strangler boundaries.
2. Focused broker/cycle/control/risk tests, Ruff, and the full repository suite
   pass.
3. Opus reviews provider leakage, accidental live enablement, gate loss,
   capability spoofing, secret exposure, ambiguous-submit handling,
   reconciliation drift, and facade recursion; every valid P0/P1 is fixed.
4. `decision-log.md` records value, decisions, verification, and Gotchas.
5. No visible product surface changed; visual evidence is therefore not
   applicable under the Evidence Contract.

### In scope / Out of scope

- In: spec, full validation, Opus review, decision log.
- Out: frontend screenshots or primary-worktree integration.

## Task batches

### Batch 1 — Contract and registry skeleton

1. Add failing contract tests for immutable requests, closed capabilities, and
   registry exact/fallback resolution, including inherited Tiger capability
   exclusion.
2. Implement `services/broker_port.py`.
3. Add failing composition tests for paper, Binance, Tiger, demo/testnet/live,
   unknown real, and unknown demo provider selection.
4. Implement `services/broker_composition.py` with lazy venue builders.
5. Delegate legacy public factories without changing signatures.
6. Run contract/factory tests and broker baseline.

### Batch 2 — Core cutover and conformance

1. Add public cancel/protective capability shims while retaining legacy method
   aliases.
2. Route multi-strategy execution and reconciliation construction through the
   composition root.
3. Remove private Binance symbol/cancel access from the runner.
4. Add the shared Binance/Tiger conformance matrix.
5. Pin live activation, demo/testnet guardrail+endpoint pairing, and
   ambiguous-submit identity pass-through with focused regression tests.
6. Add function-scoped provider-neutrality tests for the four core entry paths,
   with the two named diagnostic read-model exemptions.
7. Run focused broker/cycle/control/risk/accounting regression.

### Batch 3 — Hardening and closure

1. Document the port and strangler boundary; update the decision log.
2. Run Ruff and the full repository suite.
3. Submit the isolated diff and threat checklist to verified Opus review.
4. Fix all valid P0/P1 findings and rerun proportionate plus full regression.
5. Record final verification and keep the primary dirty worktree untouched.

## Gotchas

- `LiveBrokerAdapter` is both a public compatibility class and a venue utility
  base for Binance demo/testnet and Tiger paper. Moving its 2,000 lines in one
  change would multiply execution risk; A5 first moves dependency direction and
  composition, then later venue extractions can be mechanical.
- Existing direct tests instantiate `LiveBrokerAdapter` and monkeypatch
  `services.broker_adapter.load_pipeline_config`. The compatibility facade must
  keep both seams valid while new code uses `broker_composition`.
- Binance demo/testnet intentionally bypass only the real-money activation gate
  for non-mainnet endpoints. Registry context must never let `environment=live`
  inherit that bypass. The live Binance builder must return the activation-gated
  base path; no composition default may force `dry_run=False`.
- Tiger paper has a real network-capable TradeClient path but remains guarded by
  owner-only props, `network_order_submission=paper_tradeclient`, explicit
  confirmation, reconciliation, account checks, risk, and protective order
  validation. Composition cannot infer or enable any of these flags.
- The runner currently uses `hasattr` and the private `_binance_symbol` helper
  for expiry cancellation. A generic cancel port must preserve exchange symbol
  resolution inside the adapter and must not expose provider payloads upstream.
- Protective recovery is currently implemented only by the Binance lineage.
  Tiger must advertise the capability as absent; inheritance must not
  accidentally make it look supported. Capability computation keys on resolved
  provider as well as class because plain `LiveBrokerAdapter(provider=tiger)`
  also inherits the Binance methods.
- Unknown providers historically produce dry-run request artifacts but reject
  real submission. Unknown demo providers remain unarmed; the compatibility
  fallback may preserve dry-run behavior, but an unknown armed network provider
  must remain impossible.
- Binance demo and testnet currently share `https://demo-fapi.binance.com`, but
  they are not equivalent: mode labels, money-guardrail behavior, protective
  endpoints, request namespaces, and recovery metadata differ. Registry keys
  and conformance tests must keep them distinct and bind reconciliation to the
  exact execution endpoint.
- Port/facade code must pass through `PaperOrder` and lifecycle state unchanged.
  The `submitting` state after an ambiguous Binance response is deliberate
  duplicate-order protection, not a receipt-normalization problem.
- Dashboard contains legitimate Tiger diagnostic/read-model functions.
  Provider-neutrality tests target command execution only;
  `_execution_profile_for` and `_demo_reconciliation_block_reason` stay as
  documented A6 presentation debt rather than triggering a risky A5 rewrite.
- Nautilus already ships mature venue adapters, including Binance. A5 makes
  those usable later as another registered execution client; it does not change
  live authority before parity, reconciliation, and operational gates exist.
- The primary worktree contains unrelated Debug and range-drag work. A5 remains
  isolated and must never be integrated by copying whole files.

## Baseline

- Focused broker, Binance demo/testnet, Tiger, multi-strategy, journal, live
  safety, mainnet canary, and broker-accounting suite: `105 passed` before A5.
- Current production factory has one special-case Tiger branch; the
  multi-strategy runner has explicit Binance/Tiger construction, private
  Binance cancellation access, and provider-specific reconciliation selection.
- Strategy Control Plane itself is currently provider-neutral. A5 protects that
  invariant instead of introducing broker selection into it.
- Verified Opus plan review found no P0 and three accepted P1 corrections:
  fail-closed capabilities independent of inheritance, activation-gated live
  Binance construction, and exact demo/testnet mode+endpoint preservation.

## Completion boundary

A5 is complete when all scoped application paths obtain broker execution and
reconciliation through the registered port, Binance and Tiger pass the same
contract suite, unsupported capabilities fail before network I/O, all existing
production safety gates remain exact, full regression passes, and verified
Opus reports no unresolved P0/P1. A5 does not switch live authority, enable real
money, add a venue, move every legacy wire helper, or change the Dashboard UI.

## Final verification

- Focused broker, composition, runner, control, risk, lifecycle, accounting,
  reconciliation, and canary regression: `131 passed`.
- Full repository suite: `1719 passed, 7 skipped`.
- Ruff on every changed Python file: `All checks passed`.
- Final verified Opus follow-up used `claude-opus-4-8`, session
  `421c1d8e-3041-4ddd-9fec-756c519f3394`, receipt
  `20260717T230038Z_e68d8f4c-4bec-41c7-8805-e55909ce0064.json`: all five
  hardening items closed and explicit `NO P0/P1 findings`.
- No visible product surface changed; visual Evidence is not applicable for
  A5. The tests, review receipts, code, and documents are trace material.
