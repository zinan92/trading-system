# Implementation progress — 2026-07-24

This is the single operator-facing progress view for the approved
[implementation plan](implementation-plan-2026-07-24.md). It deliberately
counts a story as **verified done** only when its stated evidence exists in
`main`; a UI mock or an unreviewed branch does not count.

## Current bar

```text
Verified done  25 / 26  █████████████████████  96%
In progress     0 / 26
Remaining       1 / 26
```

**As of:** `main@1b2be9c` · **Current work:** no active story · **Next planned
story:** M6-03 Paper release/rollback runbook.

| Milestone | Done | Active | Remaining | Meaning |
|---|---:|---:|---:|---|
| M1 Operational truth | 5/5 | 0 | 0 | Runtime, failure and bounded-recovery contracts |
| M2 Paper lifecycle evidence | 6/6 | 0 | 0 | Grid/DCA lifecycle and multi-level precision evidence complete |
| M3 Operator product | 6/6 | 0 | 0 | Parameter, control, chart, table, and continuous-journey evidence complete |
| M4 Strategy Shadows/review | 3/3 | 0 | 0 | Variants, comparability, and readable review evidence complete |
| M5 Evolution/self-repair | 3/3 | 0 | 0 | Evidence threshold, proposal and safe queue complete |
| M6 Interfaces/release discipline | 2/3 | 0 | 1 | Adapter suite and pre-deploy compatibility verified; runbook remains |

## Story ledger

| Story | Status | Evidence or next proof |
|---|---|---|
| M1-01 Runtime-state contract | Verified done | `docs/contracts/authoritative-runtime-state-v1.md`; merge `fd16e08` |
| M1-02 Tick failure diagnosis | Verified done | Route/ledger phase tests; merge `3cb6fb9` |
| M1-03 Closeout fault injection | Verified done | Terminal-package retry evidence; merge `b79b71e` |
| M1-04 Failure-copy matrix | Verified done | API/browser compatibility matrix; merge `f2b4fd8` |
| M1-05 Safe recovery receipts | Verified done | Bounded receipt evidence; merge `05636e1` |
| M2-01 Grid lifecycle package | Verified done | [Grid rearm evidence](../evidence/issue-289-grid-rearm-2026-07-24.md); merge `5df6046` |
| M2-02 Multi-level traversal | Verified done | One-event/bar-granularity and receipt semantics; [PR #345](https://github.com/zinan92/trading-system/pull/345) |
| M2-03 Grid accounting chain | Verified done | Canonical preflight/accounting evidence; merge `6d7fea1` |
| M2-04 Aggregate TP | Verified done | Generation/quantity contract; merge `5d5b63b` |
| M2-05 Attended DCA lifecycle | Verified done | Natural two-addition aggregate-TP closeout documented in `REGISTRY.md` |
| M2-06 Switch residue | Verified done | Grid/DCA fail-safe switch test; merge `290f4f2` |
| M3-01 Parameter draft matrix | Verified done | Browser matrix; merge `84a22f8` |
| M3-02 Range-drag contract | Verified done | Drag/confirm/cancel/zoom tests; merge `874ebad` |
| M3-03 Start/stop outcomes | Verified done | Success, rejection and response-loss tests; merge `2129b13` |
| M3-04 Chart regression audit | Verified done | Browser paging/time-axis/autoscale evidence; merge `ce67229` |
| M3-05 Summary/table completeness | Verified done | Browser summary/positions/orders/fills audit; [PR #337](https://github.com/zinan92/trading-system/pull/337) |
| M3-06 Operator journey expansion | Verified done | Paper browser journey verifies start → read-model → summary/table → stop; [PR #341](https://github.com/zinan92/trading-system/pull/341) |
| M4-01 Shadow comparability | Verified done | Market-input/window/contract fail-closed comparison; [PR #349](https://github.com/zinan92/trading-system/pull/349) |
| M4-02 5–10 Grid variants | Verified done | Isolated 5-variant Grid Shadow generation; merge `e9f3591` |
| M4-03 12-hour review interpretation | Verified done | Closed-cycle browser review and optional screenshot; [PR #353](https://github.com/zinan92/trading-system/pull/353) |
| M5-01 Evidence threshold | Verified done | Cross-period sample/persistence gate; merge `a05ba05` |
| M5-02 Promotion proposal | Verified done | Read-only cross-cycle proposal; merge `db36902` |
| M5-03 Safe-repair queue | Verified done | Non-actuating diagnostic queue; merge `f90814c` |
| M6-01 Adapter contract suite | Verified done | [Adapter-contract audit](../audits/adapter-contract-suite-2026-07-24.md); [PR #357](https://github.com/zinan92/trading-system/pull/357) |
| M6-02 Runtime compatibility | Verified done | Non-actuating Paper pre-deploy receipt with actual launchd Python/API smoke; [PR #361](https://github.com/zinan92/trading-system/pull/361) |
| M6-03 Paper release/rollback runbook | Remaining | Standard merge/deploy/health/browser/rollback receipt |

## Update rule

After a story PR merges, update this page and the concise `REGISTRY.md` entry
in a separate documentation PR. Recalculate the bar from this ledger; do not
move a story to “verified done” merely because a branch, mock, or screenshot
exists. Before reporting a new GitHub Issue or PR link, read it back through
the GitHub API as described in
[the provenance audit](../audits/github-provenance-222-329.md).
