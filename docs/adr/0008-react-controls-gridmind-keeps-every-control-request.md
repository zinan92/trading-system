---
status: accepted
---

# React draws the control panel; GridMind keeps every control request

The frontend move (spec `docs/plans/frontend-components-spec.md` step 5,
#1356) puts GridMind's controls into React components. These are:

- the venue and instrument selects;
- the strategy buttons;
- preview and confirm;
- the risk dialog and its acknowledgements;
- start and stop;
- the main K-line, whose range drag ends in `executeGridRangeReplacement`.

These controls start, change and stop Paper and Testnet strategies. The issue
asks one question first: may `apps/web` send these requests?

## Decision

**No.** `apps/web` never sends a control or order request. The ban in
`islands.test.tsx` stays as it is: no `dashboard-control`, `/api/execute`,
`/api/grid/`, `/api/dualtrack/orders`, `/api/park-paper` or `/api/mainnet`, and
POSTs only to its allow-list.

**Controls send intents, not requests.**
- A control fires a DOM event that names the action and its arguments, for
  example preview, confirm, start or stop.
- GridMind's control script listens for these intents and runs the handler it
  already has. The requests it sends are byte for byte what it sends today.
- The same intent is fired by the legacy markup and by the React component. So
  the page works without `web.js`, and replacing an element never drops a
  listener. (#1329 found that GridMind binds elements by reference at load, and
  a re-render drops the binding without an error.)

**GridMind decides; React shows.**
- Whether a button can be pressed comes from GridMind, which publishes it, and
  so do the preview numbers and the blockers. React renders what is published.
  It never computes a gate.
- The risk acknowledgements live in GridMind's state, keyed by preview id.
  React renders the published list under that key. A new preview has a new
  key, so earlier ticks are gone and confirm is disabled until the new list is
  ticked.
- GridMind reads its own state for the gate, never the DOM.

**The main K-line canvas stays with GridMind** until the page itself moves
(#1330). React hosts the container. A finished drag is an intent, and
`executeGridRangeReplacement` does not move.

## Order of work

1. GridMind listens for intents, with no visual change (#1395).
2. Selects and strategy buttons (#1396).
3. Preview, confirm, the risk dialog, start and stop (#1397).
4. The main K-line container and drag (#1398).

## Consequences

- `tests/test_dashboard_control_wire_browser.py` must pass with zero edits to
  its expected request bytes at every step.
- Each step adds a test that replaces or re-renders a control and then checks
  that pressing it still sends the same request.
- Step 3 adds a test that a new preview clears earlier acknowledgements.
- Verification uses mocks and dev only. In production, never change a select,
  press a strategy, preview, confirm, start or stop button, or drag the range.
- Deploys that change `dashboard-gridmind.html` use `--dashboard`.
- When the page becomes React (#1330), the intent layer is the seam where the
  control script is replaced. The request bytes and the wire test stay the
  acceptance bar.

## Considered options

- **Rejected: let `apps/web` send the control requests.** It would put a second
  order path beside GridMind's, with its own gate logic. It would also widen a
  ban that has caught real mistakes.
- **Rejected: React re-renders over elements that GridMind still binds by
  reference.** A listener lost on re-render fails silently.
- **Rejected: read the acknowledgement checkboxes back from the DOM.** A
  re-render that kept a tick across a new preview would open the gate.
