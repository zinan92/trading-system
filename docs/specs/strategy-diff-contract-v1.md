# strategy-diff-contract-v1

## Purpose

This is a read-only, versioned comparison contract for the `trading_strategy`
package and the internal strategy paths. It does not change either
implementation, submit orders, use a venue, or authorize a lifecycle action.

The pinned package source is `trading-strategy` `main` at
`d4daae915c549fe657a98b6cbf0e539886af355c`.

## Inputs and coverage

`tools.strategy_diff` owns deterministic in-code fixtures. It produces 24 DCA
preview cases, 24 Grid sizing preview cases, and 24 Grid conditional simulation
cases. Inputs include long/short/neutral direction, steady/aggressive styles,
arithmetic/geometric modes, budget sizing, re-arm limits, changing account
equity, and boundary-shaped values. Market bars are explicitly labelled as
fixture observations; they are not execution evidence.

## Normalization

Results are recursively converted to JSON with sorted object keys. Dataclasses
become objects, tuples/sets become arrays, and `Decimal` values become fixed
decimal strings. Finite floats are rendered with 15 significant digits. Numeric
comparison uses absolute tolerance `1e-9` plus relative tolerance `1e-9`; each
numeric mismatch records its absolute delta. Exceptions are normalized as
`ok=false`, exception type, and message so rejected inputs remain comparable.

The envelope digest is SHA-256 over the sorted, compact normalized envelope,
including case IDs, both observations, and this contract version. Re-running
the same pinned inputs must produce the same digest.

## Difference classification and authority advice

Every mismatch has a JSON path and is classified as `semantic` (value or
behavior), `precision` (numeric delta beyond tolerance), or `schema` (shape,
type, or missing field). The report gives a non-implementing recommendation
for each mismatch: preserve approved `trading_strategy` semantics for semantic
differences, preserve venue-precision/Decimal normalization for precision
differences, and resolve versioned field compatibility for schema differences.
These recommendations are review input only.

## Verification

```sh
PYTHONPATH=src python3 -m pytest -q tests/test_strategy_diff.py
PYTHONPATH=src python3 -m tools.strategy_diff --report /tmp/diff.json
```

The report is an acceptance artifact and may be attached to Issue #1139. It is
not proof of a live venue action; live XAU Paper and datafeed runtime checks
remain owner verification and are intentionally outside this story.
