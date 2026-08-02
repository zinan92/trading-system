# North Star — trading-system

> Stable intent. Change this only when Park explicitly changes the product
> destination, approved baseline, or success definition.

## What we are building

An auditable multi-market trading system with Grid as its primary strategy.
It validates every strategy and operational path in Paper first; its risk gates
remain independent of strategy logic and every claimed action has a durable
receipt.

## Done looks like

The Paper Supervisor has passed its defined operational acceptance without
invented evidence: a continuous 48-hour conservative utilisation rate of at
least 85%, three real cycle boundaries (including one 21:00 boundary), and one
complete transient-recovery audit chain. A merged PR, a green test suite, or a
deployment alone is not completion. Any future live/real-money capability is a
separate Park-approved destination, not part of this North Star.

## Approved foundations

| Item | Canonical artifact / reference | Decision date | Do not reinvent |
| --- | --- | --- | --- |
| Product implementation baseline | `docs/plans/implementation-plan-2026-07-24.md` | 2026-07-24 | yes |
| Paper release and rollback contract | `docs/runbooks/paper-release-rollback-v1.md` | 2026-07-24 | yes |
| Current operational truth | `REGISTRY.md` | rolling | yes |

## Milestones to the intent

| # | Milestone | Evidence that it is complete | Status |
| --- | --- | --- | --- |
| 1 | Implement the approved Paper-first system | Approved implementation stories and scoped verification receipts | complete (26/26 planned stories) |
| 2 | Remove structural Paper blockers and obtain a clean Supervisor start | Source-backed Cloud Paper receipt; no unknown control outcome; no open orders or positions when blocked | in progress |
| 3 | Pass operational Paper acceptance | 48-hour >=85% conservative utilisation, three real cycle boundaries, and one transient-recovery audit chain | not started |

## Non-negotiables

- Paper-first: do not enable, submit, cancel, or close a real-money order.
- A market or datafeed failure blocks new entries; stale data is evidence only.
- Missing, partial, or conflicting runtime evidence is fail-closed, never
  inferred as healthy.
- No runtime, scheduler, or configuration mutation follows from documentation
  work alone.

## Change control

`REGISTRY.md` may report progress through these milestones but cannot redefine
them. Record the rationale for a North Star change in `decision-log.md` and
link the approving decision.
