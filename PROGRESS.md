# Strategy extraction progress

## Contract

Extract the locked Canonical DCA and Grid foundation from the source repository
into this independent local repository. This repository must remain a pure,
engine-neutral Strategy package: no broker/native venue, datafeed runtime,
Dashboard, Telegram/Park authorization, Cloud runtime, network, credentials,
or filesystem order execution dependency.

Canonical behavior is preserved by characterization/golden tests. This task is
an extraction, not a strategy redesign. Existing source behavior that is
identified as a defect is recorded as `KNOWN_DEFECT` rather than changed here.

## Source baseline

- Source repository: `/Users/wendy/work/trading-system-testnet`
- Baseline command: `git rev-parse HEAD`
- Baseline SHA: `b841800ee03fd98107063c0cbbf5144096a5c4c0`
- Source checkout: detached HEAD
- Source worktree at baseline capture: dirty but preserved; existing changes were
  `M CONTEXT.md` and untracked `docs/adr/0002-canonical-dca-broker-adaptation.md`,
  `docs/adr/0003-trading-system-composition-root.md`, and
  `docs/adr/0004-broker-transport-substitution.md`.
- Prohibited source checkout `/Users/wendy/work/trading-system`: not touched.

## Test baseline

### Focused strategy baseline (complete)

Command:

```text
python3 -m pytest -q tests/test_dca_plan.py tests/test_dca_execution_lifecycle.py tests/test_park_dca_track.py tests/test_grid_sizing.py tests/test_park_grid_track.py tests/test_grid_multi_level_traversal_contract.py tests/test_grid_range_adjustment.py
```

Result: `88 passed in 2.99s`; skipped: `0`; failures: `0`.

### Source strategy/core regression after extraction (complete)

Command:

```text
python3 -m pytest -q tests/test_dca_plan.py tests/test_dca_execution_lifecycle.py tests/test_park_dca_track.py tests/test_grid_sizing.py tests/test_park_grid_track.py tests/test_grid_multi_level_traversal_contract.py tests/test_grid_range_adjustment.py tests/test_dualtrack_dt2_machine_runner.py tests/test_dualtrack_machine_independence.py tests/test_lab_r5_grid.py
```

Result: `135 passed in 3.72s`; skipped: `0`; failures: `0`.

Reconfirmed after remediation from a temporary `git archive` of pinned source
ref `b841800ee03fd98107063c0cbbf5144096a5c4c0`: `135 passed in 3.85s`;
skipped: `0`; failures: `0`.

### Full source baseline (complete)

Command: `python3 -m pytest -q`

Result: `3349 passed, 1 skipped, 32 failed, 6 warnings in 646.24s
(0:10:46)`; no failures were hidden or converted to skips.

The failures are outside this extraction's pure Strategy scope and are kept as
source baseline facts:

- `tests/test_dashboard_gridmind_range_drag_browser.py::test_gridmind_drag_release_keeps_draft_until_explicit_confirm` — browser drag fixture observed no lower-bound change.
- `tests/test_live_activation_gate.py::test_live_preflight_is_read_only_and_binds_exact_identity`
- `tests/test_live_activation_gate.py::test_live_activation_requires_exact_confirmation_and_is_idempotent`
- `tests/test_live_activation_gate.py::test_confirmation_rechecks_current_readiness_and_canary_stays_blocked`
- `tests/test_live_activation_protocol.py::test_production_proposal_seam_creates_only_local_confirmation_proposal`
- 8 `tests/test_live_dca_canary.py` cases — activation preflight blocked by `tracked_source_tree_dirty` / Testnet readiness blockers before fixture transport.
- 11 `tests/test_schedule_manager.py` cases — launchd/schedule state transitions returned `blocked` instead of the fixture's expected active/pass state.
- 5 `tests/test_standard_broker_external_canary.py` cases — installed `standard_broker` lacks the expected public canary symbols.
- `tests/test_standard_broker_external_protection.py::test_opt_in_protection_builder_starts_without_backend_invocation` — installed `standard_broker` lacks the expected protection builder symbol.
- 2 `tests/test_testnet_soak_readiness.py` cases — soak rows returned `blocked` rather than pass.

These failures do not import the new package and are not modified by this
task. The one skipped case was retained as reported by pytest.

## File classification (initial evidence-based audit)

Classification is based on imports and executable behavior, not filenames.
Detailed closure and line references will be expanded before extraction.

| Source file | Classification | Evidence / boundary |
| --- | --- | --- |
| `services/dca_plan.py` | Pure strategy algorithm + plan/schema | Plain dict input/output, deterministic hashes and precision math; no I/O. It imports only the source's pure execution-command normalization and grid sizing helpers. |
| `services/grid_sizing.py` | Pure strategy algorithm + plan/schema | Explicitly documents pure functions; builds deterministic Grid geometry, sizing, risk diagnostics, and versioned preview fields. Its dependencies are pure normalization and marketability helpers. |
| `services/dualtrack_grid_core.py` | Pure strategy algorithm + pure state transition | Simulates conditional/explicit Grid behavior and contains `GridLineLifecycle`; imports only a market bar value object and pure cost math. Includes Hard Stop, rung geometry, fill idempotency, and re-arm behavior. |
| `services/dca_execution_lifecycle.py` | Execution/lifecycle orchestration; retain in Trading System | `DcaPaperLifecycle` calls an execution adapter and persists state through filesystem journal helpers; not eligible for the pure package. |
| `services/park_dca_track.py` | Host/Park lifecycle orchestration; retain in Trading System | Requires `ParkStrategyLifecycleLedger` and `ParkTelegramLedger`, exact authorization receipt, and outbound notification behavior. |
| `services/park_grid_track.py` | Host/Park lifecycle orchestration; retain in Trading System | Requires Park lifecycle and Telegram ledgers, authorization receipt, and terminal notification behavior. |
| `services/dualtrack_execution_contract.py` | Pure contract helper subset | Only `normalize_execution_command` is needed by the extracted package; broader execution/reconciliation helpers stay outside the package. |
| `services/dualtrack_costs.py` | Pure strategy cost helper subset | Grid simulation uses deterministic cost math; venue/runtime adapters are not imported by the extracted package. |
| `services/venue_costs.py` | Pure cost helper subset | Plain cost-rule calculations only; no broker object or transport import. |
| `services/grid_marketability.py` | Pure strategy validation helper | Direction/range blocker is a side-effect-free function. |
| `services/grid_range_adjustment.py` | Pure strategy geometry algorithm | Running-Grid arithmetic/geometric edge adjustment, drag constraints, edge-order deduplication, and completed-fill re-arm; no execution or account access. |
| `schemas/market_data.py` (`Bar` only) | Pure strategy value schema subset | Grid simulation needs the immutable OHLCV `Bar` value object; market envelopes/runtime remain outside. |
| `tests/test_dca_plan.py` | Pure strategy characterization tests | Directly exercises DCA candidate, preview, plan projection, replay, and validation. |
| `tests/test_grid_sizing.py` | Pure strategy characterization tests | Directly exercises Grid geometry, ATR, sizing, adaptive solver, precision, and risk behavior. |
| `tests/test_grid_range_adjustment.py` | Strategy geometry contract tests | Exercises Grid edge geometry, deduplication, completed-fill re-arm, and boundary constraints. |
| `tests/test_park_dca_track.py` | Host lifecycle tests; not extracted | Tests Park authorization/ledger/notification orchestration around DCA. |
| `tests/test_park_grid_track.py` | Host lifecycle tests; not extracted | Tests Park authorization/ledger/notification orchestration around Grid. |
| `tests/test_dca_execution_lifecycle.py` | Execution/lifecycle tests; not extracted | Uses filesystem journals and execution adapters, including Nautilus/Paper adapter types. |
| `tests/test_grid_multi_level_traversal_contract.py` | Mixed integration/adapter contract | Includes canonical market event and Paper adapter/runtime behavior; pure Grid behavior will receive standalone golden tests. |

## Extraction evidence

The two read-only audits established the following import/behavior closure:

- DCA pure/plan closure: `dca_plan.py` plus precision normalization and
  `grid_sizing` helpers (`account_equity`, `order_at_notional`,
  `validate_market`). `dca_execution_lifecycle.py` calls adapter methods and
  filesystem journal helpers; `park_dca_track.py` uses Park lifecycle and
  Telegram ledgers, so neither is a pure package dependency.
- Grid pure closure: `grid_sizing.py`, marketability rules, precision
  normalization, `dualtrack_grid_core.py`, `GridLineLifecycle`, the `Bar`
  value object, and deterministic cost helpers. `park_grid_track.py` activates
  a Park ledger in its constructor and writes Telegram outbox notifications;
  it remains composition-root code.
- The source `build_dca_entry_commands` output is a pure projection but carries
  existing `strategy_dca_paper` contract fields; those fields were preserved,
  not interpreted as permission to execute.
- Existing behavior traps retained as characterization facts: DCA
  `loop_enabled=true` is rejected; DCA aggregate target is one active,
  replace-after-fill target; DCA terminal replay stops after target/stop; Grid
  plan and explicit replay use different field shapes (`price/tp/sl` versus
  `entry/take_profit`); Grid plan Hard Stop wins over ambiguous same-bar entry;
  partial Grid close requires entry-cancel confirmation before re-arm.
- Provenance is fail-closed in two modes: the default mutable-checkout mode
  requires the pinned HEAD and clean relevant files, while explicit
  `--source-ref b841800...` captures from a local Git-object snapshot. Both
  modes compare relevant source hashes against a committed expected digest map.
- The boundary test identifies the real host module symbols
  `ExternalDcaPlan`/`ExternalDcaLifecycle` and verifies neither the symbols nor
  the host module are copied into the package.

The extracted package files are:

- `trading_strategy/dca_plan.py` — copied Canonical DCA plan/schema, sizing,
  command projection, and pure replay.
- `trading_strategy/grid_sizing.py` — copied Grid preview/schema and adaptive
  sizing solver.
- `trading_strategy/grid_range_adjustment.py` — pure running-Grid edge
  geometry, drag constraints, deduplication, and re-arm projection.
- `trading_strategy/grid_core.py` — copied conditional/explicit Grid replay,
  `GridLineLifecycle`, Hard Stop, and re-arm semantics.
- `trading_strategy/precision.py`, `grid_marketability.py`, `costs.py`,
  `venue_costs.py`, `market.py` — the minimal pure dependency closure.
- `tests/fixtures/canonical_golden.json` — generated from the source baseline
  before implementation copying.
- `tests/test_canonical_golden.py` and `tests/test_package_boundary.py` —
  differential/golden and reverse-boundary tests.
- `tests/test_grid_range_adjustment.py`,
  `tests/test_grid_adaptive_characterization.py`, and
  `tests/test_capture_provenance.py` — remediation characterization and
  provenance tests.

## Current status

- [x] Target path checked absent before initialization.
- [x] New local Git repository initialized without a remote.
- [x] Source HEAD recorded before extraction.
- [x] Focused source strategy baseline captured.
- [x] Full source baseline final count and failures.
- [x] Final dependency closure and exact source line references.
- [x] Characterization/golden fixtures created before copying implementation.
- [x] Pure DCA and Grid package extracted.
- [x] Differential and reverse dependency tests pass.
- [x] Source focused regression rerun passes.
- [x] compileall, ruff-if-present, gitleaks, and source-preservation evidence recorded.
- [x] Target clean-worktree and extraction commit SHA recorded after commit.
- [x] Pure Grid range-adjustment seam and source characterization tests added.
- [x] DCA candidate and adaptive Grid characterization matrices added.
- [x] Source-bound capture and real external DCA boundary checks hardened.

## Commits

- Extraction commit: `0f4a5df3a5c30bcd2e07dfc2e99ab8b0f8288cbc`
- Commit message: `extract canonical dca and grid strategy foundation`
- Remediation commits: `009fef4`, `b16bc93`, `b084c9a`, `074f172`, `754aa29`.
- The final verification metadata update is intentionally a separate local
  commit so this file can contain the actual extraction SHA without a
  self-referential commit hash.

## Final verification evidence

New package command: `python3 -m pytest -q`

Result after the acceptance-gap implementation: `48 passed in 4.19s`;
skipped: `0`; failures: `0`.

Compile command: `python3 -m compileall -q trading_strategy tests tools`

Result: exit code `0`.

Golden capture command: `python3 tools/capture_canonical_golden.py --source-ref b841800ee03fd98107063c0cbbf5144096a5c4c0 | diff -u tests/fixtures/canonical_golden.json -`

Result: `GOLDEN_CAPTURE_DIFF_PASS`; fixture SHA-256 and source SHA are recorded
in `tests/fixtures/canonical_golden.receipt.json`.

Default mutable-checkout capture result: `DEFAULT_CAPTURE_FAIL_CLOSED` with
`source HEAD mismatch` against the later source checkout drift.

Source-copy comparison command: `python3 tools/compare_pinned_source.py`

Result: `PINNED_COPY_COMPARISON_PASS`, `modules_checked=7`,
`source_ref=b841800ee03fd98107063c0cbbf5144096a5c4c0`.

Import scan command: strict AST allowlist over `trading_strategy/*.py`, allowing
only standard-library or package-local imports; paired with an I/O/network call
scan.

Result: `IMPORT_SCAN_PASS`, `IO_NETWORK_CALL_SCAN_PASS`, `violations=0`.

Reverse-boundary tests include an attempted `NativeBrokerOrder` object
injection and an import of the absent `trading_strategy.broker` module; both
are rejected as expected.

Secret scan command: `gitleaks dir --no-banner .`

Result: scanned `668.45 KB`; `no leaks found`.

Ruff check: the source environment has no `ruff` executable (`RUFF_UNAVAILABLE`);
there is no existing project ruff configuration to invoke. This is recorded as
unavailable, not as a fabricated pass.

Source preservation check: `git status --short --branch` after all source
tests still shows only the pre-existing `CONTEXT.md` modification and the
three pre-existing untracked ADRs. No source file in the forbidden or allowed
strategy set was changed.

Target repository check immediately after the extraction commit:
`git status --porcelain` was empty before this metadata-only update.

## Post-baseline source drift

After the original extraction, the mutable source checkout moved to
`e80a1a93e503d588fc4f48fc4c5c8cf1172191d5` and currently has unrelated dirty
Broker files plus a changed `services/dca_plan.py`. The default capture command
therefore fails closed with `source HEAD mismatch`; it is not treated as a new
canonical baseline. The pinned Git-object capture uses the original
`b841800ee03fd98107063c0cbbf5144096a5c4c0` source and is the authoritative
parity evidence for this repo. The source checkout was not modified.
