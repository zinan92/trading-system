# Adapter contract-suite audit — 2026-07-24

This is a Paper-safe audit of the normalized ports. It reports supported
contracts, not provider activation or live readiness.

| Boundary | Normalized contract / quality meaning | Focused evidence |
| --- | --- | --- |
| Market | `dualtrack-market-event-v1`; trusted, fresh, non-synthetic data is required for execution; an upstream failure is explicit `blocked`, never synthetic fallback. | `tests/test_dualtrack_market_feed.py`, `tests/test_dualtrack_provider_contract.py` |
| Execution | `dualtrack-execution-v1` snapshots plus versioned execution/fee fingerprints; adapters must expose submit/cancel/event/snapshot/reconcile. | `tests/test_dualtrack_execution_contract.py`, `tests/test_execution_engine_plugin_registry.py` |
| Accounting | `accounting-snapshot-v1` is the P&L/count truth and uses registered projectors rather than a broker API. | `tests/test_accounting_projection_registry.py` |
| Risk | `risk-decision-v1` has a provider-free allow/block contract and does not itself submit an order. | `tests/test_risk_port.py`, `tests/test_risk_policy_registry.py` |
| Strategy evaluation | Backtest/Shadow plugins bind their input and registry fingerprints; promotion stays read-only. | `tests/test_backtest_plugin_registry.py`, `tests/test_strategy_proposal_registry.py`, `tests/test_strategy_plugin_registry.py` |

## Provider matrix

| Provider / source | Contract status | Quality/error result | Claim boundary |
| --- | --- | --- | --- |
| Binance USD-M Futures | Supported market adapter for Paper; source provenance is retained. | Ready only when the returned bar is trusted/fresh/non-synthetic; upstream failure is `blocked`. | Not a promise of exchange execution. |
| Deterministic fixture | Supported test adapter. | Stable valid or deliberately invalid fixtures prove schema/error handling. | Test evidence only. |
| Yahoo Finance | Future adapter. | No registered execution-grade provider contract yet. | Not selectable for Paper execution. |
| Tiger | Future/provider-specific integration. | Its own dated-contract/readiness gates apply; it is not interchangeable with Binance. | No activation is implied here. |

## Schema-diff gates

- Plugin registries expose a stable fingerprint; test fixtures reject forged or
  stale registry evidence.
- The market contract accepts only complete normalized bars and exposes errors
  as errors.  Unsupported or drifted candidates cannot silently replace the
  authoritative source.
- This audit changes no runtime selection and grants no provider or execution
  authority.
