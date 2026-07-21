# Issue 88 — Paper dashboard user-view acceptance

Environment: local paper runtime only. No live-money process or exchange key
was read or changed.

| User action | Expected result | Observed result | Latency | Result |
|---|---|---|---:|---|
| Start bot | Runtime becomes running and the complete derived grid remains accepted | Plan v3, 40 grids, 20 accepted buy entries, 5,002.67 USD/grid, planned minimum 10.78 USD, actual leverage 9.9969x; accounting complete and reconciliation passed | UI settled within the 15.8 s observation window | Pass |
| Refresh trend | A new evaluation, not cosmetic rerendering | Evaluation receipt changed from `ai-eval-2bc9258e...` to `ai-eval-4b1dca76...` | 31.7 s | Pass |
| Enter grid adjustment and drag | Draft stays local; small Confirm/Cancel controls appear | Preview appeared without changing orders | 2.48 s first preview | Pass |
| Confirm unsafe Range move | Destructive action remains disabled | Downward move projected 10.5x and was blocked | Immediate after preview | Pass |
| Confirm safe Range move | Card states the replacement and preserves count/notional | Plan v4 replaced the grid: cancel 20, flatten 0, submit/accept 19; 40 grids and 5,002.67 USD/grid preserved, minimum 10.77 USD, actual leverage 9.5x | ~7 s | Pass |
| Zoom in/out | Chart viewport visibly changes and returns | `+` and `-` produced distinct chart screenshots | Immediate | Pass |
| Stop + cancel + flatten | Runtime stopped with no residual paper exposure | Canceled 19, flattened 0; zero open orders and positions, accounting complete, reconciliation passed | 3.355 s | Pass |
| Current orders | Count and rows agree; quantity, price, TP, SL and planned profit are complete | Initial inspection exposed missing planned profit and useless plan version; read-model/UI correction adds exact-plan profit and removes version | Retested after deploy | Pass after repair |
| Current positions | Quantity, TP, SL and unrealized P&L are visible when present | Initial headers/aliases were incomplete; correction normalizes `remaining_units` and attaches exact-plan protection | Retested after deploy | Pass after repair |
| Trades | One completed round trip is one record, with count matching rows | 23 lifecycle trades/round trips; version column removed | Immediate | Pass |
| 12-hour review | Previous complete cycle is readable and reconciled | `2026-07-21_DAY`, +7.05 USD, reconciliation passed, structured plan/result rows | Immediate | Pass |
| Strategy Shadows | Only comparable evidence is shown | No comparable same-cycle shadow exists; UI says so rather than inventing a result | Immediate | Truthful gap |
| Historical P&L / NAV | Numeric production results from canonical ledger | Correction maps machine realized P&L and computes cumulative P&L/Paper NAV from starting balance | Retested after deploy | Pass after repair |
| Drag chart right at 240-bar edge | Load older trusted bars | Initial E2E reproduced the 240-bar lock. Edge-aware drag intent now requests the previous trusted page | Retested after deploy | Pass after repair |

Artifacts `01` through `05` are the baseline, running grid, refreshed trend,
replacement grid, and zoom screenshots. Later numbered artifacts capture the
history repair and final running state.
