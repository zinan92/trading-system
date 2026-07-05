# Decision Log

## Dualtrack DT6 Synced Replay

Date: 2026-07-05

### Decisions

- Implement DT6 as a sibling replay surface: `dashboard-dualtrack-replay.html?layout=dualtrack&cycle=<cycle_id>`.
  - Rationale: the existing `dashboard-replay-v4.html` is a single-trade strategy replay. A sibling page keeps that stable while giving dualtrack a purpose-built two-pane review surface.
  - Evidence: `dashboard-dualtrack-replay.html`, `dashboard-dualtrack-v5.html`.

- Treat closed attribution as the only source of machine fill detail.
  - Rationale: machine fills are intentionally blind during the cycle. A replay page that falls back to the machine endpoint would bypass the blind protocol.
  - Evidence: DT6 page fetches `GET /api/dualtrack/attribution/{cycle_id}` and does not call the intraday machine endpoint.

- Add fills to the closed attribution payload.
  - Rationale: DT6 needs both track stats and exact fill timestamps/prices to render synchronized markers. The closed attribution artifact is already the safe reveal boundary.
  - Evidence: `services/dualtrack_scoring.py`.

- Keep one shared cursor for both panes.
  - Rationale: the user is comparing behavior at the same market moment, not separately browsing two histories.
  - Evidence: `cursorRange`, `setCursor`, and `renderPane("human"/"machine")` in `dashboard-dualtrack-replay.html`.

- Make the DT5 "查看完整回放" link point directly to DT6 with `layout=dualtrack&cycle=...`.
  - Rationale: closed-cycle review should be one click from the console's attribution section.
  - Evidence: `dashboard-dualtrack-v5.html`.

### Gotchas

- Do not add `/api/dualtrack/machine` or `/api/dualtrack/human` as a replay fallback. The page must fail closed when attribution is missing.

- Existing attribution files created before DT6 may not contain `fills`. The page tolerates empty fills, but useful dual replay starts with cycles closed after the DT6 payload change.

- The current market-bars endpoint is read-only latest-bars oriented. DT6 filters those bars to the closed cycle window and falls back to a display-only synthetic path when no matching bars are present.

- The replay page is static, so D6-1 is enforced by both sides: frontend only asks for attribution, and backend attribution remains unavailable until close.

- Keep the page no-cache. Reviewing a just-closed cycle with cached JS or HTML can look like a data leak or a missing-fill bug.

- Real-data verification found four bugs after green tests: phantom stops from floor/stop/rung-bottom drift, inverted risk from budget-sized rungs, open-ended range bounds crashing human orders, and live scoreboard feedback making closed-cycle re-runs arm a trend leg that was not armed at cycle start. Keep owner replay checks in the release loop before trusting cash-flow surfaces.

- Open-ended plans mean missing bounds are open, not invalid. Long plans may have only a floor, short plans may have only a ceiling, and out-of-plan checks must enforce only the bounds that exist.

- Machine fills are a per-cycle recomputation artifact. Every intraday or auto run must replace the machine fill file for that cycle; append-only runner logs belong under runner/audit, not fills.

- The trend gate is a cycle-start invariant. Freeze the scoreboard-derived armed flag during pre-cycle and preserve it through intraday, close, scoring, and replay; a cycle's own close must never feed back into that same cycle's machine result.
