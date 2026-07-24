# #289 — Paper Grid rearm lifecycle evidence (2026-07-24)

## Result

**Verified:** one Nautilus Paper Grid line completed `entry → TP → original-price
rearm`, with final engine reconciliation `ok`.

This is a real Paper observation. No synthetic market event, manual price
injection, order replay, or execution-engine restart was used to create it.

## Immutable evidence

| Fact | Value |
| --- | --- |
| Cycle / engine | `2026-07-24_DAY` / `nautilus_paper` |
| StrategyPlan | `strategy-plan-2026-07-24_DAY-7-0440c038` v7, `grid` |
| Grid line | `nautilus-command-003fe5bb3d69b9c65313` generation 1 |
| Entry | 1.965 at 4045.56; fill `nautilus-nautilus-command-003fe5bb3d69b9c65313` |
| TP | 1.965; fill `nautilus-nautilus-command-003fe5bb3d69b9c65313-TP` |
| Rearm | generation 2 order `nautilus-command-d6586d69f235813422d1` at the original 4045.56 |
| Evidence result | `completed_rearmed_count=1`, `unverified_count=0`, reconciliation `ok` |

The verified lifecycle transition is recorded as
`close_fill_confirmed_rearm` at `2026-07-24T07:14:00+00:00`.

## Source artifacts (local, immutable outputs)

| Artifact | SHA-256 |
| --- | --- |
| `outputs/dualtrack/grid_lifecycle/2026-07-24_DAY_nautilus.json` | `887f59b4f1eec041ab73beb79a1735122de4c54114ac4882783870bb49eccbe4` |
| `outputs/dualtrack/nautilus_authoritative/commands/2026-07-24_DAY.json` | `3e1ef34959c546abfdc225b47f872e2348b67a004764ef6cf77876d02bee2926` |
| `outputs/dualtrack/nautilus_authoritative/fills/2026-07-24_DAY.json` | `3c18c03da458157f6845579677d5aad9c0761d907736060f28d0425dc2a1963b` |
| `outputs/dualtrack/nautilus_authoritative/snapshots/2026-07-24_DAY.json` | `3b1661ee3579598c7dfdc7122d8e34b0bb209dabbc9794b8da007da05367f686` |

## Post-observation safety action

The next scheduled heartbeat did not remain within the 180-second freshness
window. The operator therefore issued the normal Paper safe stop after the
rearm had already been verified: remaining generation-2/other accepted orders
were canceled, the final snapshot had zero open positions, and reconciliation
remained `ok`. This safety closeout does not replace or manufacture the prior
completed lifecycle evidence.
