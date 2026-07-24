# Implementation progress — 2026-07-24

This is the single operator-facing progress view for the approved
[implementation plan](implementation-plan-2026-07-24.md). It deliberately
counts a story as **verified done** only when its stated evidence exists in
`main`; a UI mock or an unreviewed branch does not count.

## Current bar

```text
Verified done  19 / 26  ███████████████░░░░  73%
In progress     0 / 26
Remaining       7 / 26  █████
```

**As of:** `main@a4fe7dd` · **Current work:** no active story · **Next planned
story:** M3-06 operator journey expansion.

| Milestone | Done | Active | Remaining | Meaning |
|---|---:|---:|---:|---|
| M1 Operational truth | 5/5 | 0 | 0 | Runtime, failure and bounded-recovery contracts |
| M2 Paper lifecycle evidence | 5/6 | 0 | 1 | Grid/DCA evidence; multi-level traversal remains |
| M3 Operator product | 5/6 | 0 | 1 | Parameter/control, chart, and execution-table evidence complete |
| M4 Strategy Shadows/review | 1/3 | 0 | 2 | Variants complete; comparability and review audit remain |
| M5 Evolution/self-repair | 3/3 | 0 | 0 | Evidence threshold, proposal and safe queue complete |
| M6 Interfaces/release discipline | 0/3 | 0 | 3 | Adapter suite, CI compatibility and runbook remain |

## Story ledger

| Story | Status | Evidence or next proof |
|---|---|---|
| M1-01 Runtime-state contract | Verified done | `docs/contracts/authoritative-runtime-state-v1.md`; merge `fd16e08` |
| M1-02 Tick failure diagnosis | Verified done | Route/ledger phase tests; merge `3cb6fb9` |
| M1-03 Closeout fault injection | Verified done | Terminal-package retry evidence; merge `b79b71e` |
| M1-04 Failure-copy matrix | Verified done | API/browser compatibility matrix; merge `f2b4fd8` |
| M1-05 Safe recovery receipts | Verified done | Bounded receipt evidence; merge `05636e1` |
| M2-01 Grid lifecycle package | Verified done | [Grid rearm evidence](../evidence/issue-289-grid-rearm-2026-07-24.md); merge `5df6046` |
| M2-02 Multi-level traversal | Remaining | Define unsupported precision and event ordering; then replay tests |
| M2-03 Grid accounting chain | Verified done | Canonical preflight/accounting evidence; merge `6d7fea1` |
| M2-04 Aggregate TP | Verified done | Generation/quantity contract; merge `5d5b63b` |
| M2-05 Attended DCA lifecycle | Verified done | Natural two-addition aggregate-TP closeout documented in `REGISTRY.md` |
| M2-06 Switch residue | Verified done | Grid/DCA fail-safe switch test; merge `290f4f2` |
| M3-01 Parameter draft matrix | Verified done | Browser matrix; merge `84a22f8` |
| M3-02 Range-drag contract | Verified done | Drag/confirm/cancel/zoom tests; merge `874ebad` |
| M3-03 Start/stop outcomes | Verified done | Success, rejection and response-loss tests; merge `2129b13` |
| M3-04 Chart regression audit | Verified done | Browser paging/time-axis/autoscale evidence; merge `ce67229` |
| M3-05 Summary/table completeness | Verified done | Browser summary/positions/orders/fills audit; [PR #337](https://github.com/zinan92/trading-system/pull/337) |
| M3-06 Operator journey expansion | Remaining | Add the completed M3 states to one continuous browser journey |
| M4-01 Shadow comparability | Remaining | Audit/lock same-window, fee, execution and hash contract |
| M4-02 5–10 Grid variants | Verified done | Isolated 5-variant Grid Shadow generation; merge `e9f3591` |
| M4-03 12-hour review interpretation | Remaining | Audit plan-versus-result interpretation and evidence labels |
| M5-01 Evidence threshold | Verified done | Cross-period sample/persistence gate; merge `a05ba05` |
| M5-02 Promotion proposal | Verified done | Read-only cross-cycle proposal; merge `db36902` |
| M5-03 Safe-repair queue | Verified done | Non-actuating diagnostic queue; merge `f90814c` |
| M6-01 Adapter contract suite | Remaining | Verify normalized port contracts across market/execution/accounting/risk |
| M6-02 Runtime compatibility | Remaining | Promote Python 3.9 receipt to enforceable CI/pre-deploy gate |
| M6-03 Paper release/rollback runbook | Remaining | Standard merge/deploy/health/browser/rollback receipt |

## Update rule

After a story PR merges, update this page and the concise `REGISTRY.md` entry
in a separate documentation PR. Recalculate the bar from this ledger; do not
move a story to “verified done” merely because a branch, mock, or screenshot
exists. Before reporting a new GitHub Issue or PR link, read it back through
the GitHub API as described in
[the provenance audit](../audits/github-provenance-222-329.md).
