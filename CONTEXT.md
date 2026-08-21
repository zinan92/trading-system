# Trading Execution Context

This context defines Park Paper execution language so recording, strategy
identity, and operator actions are not conflated.

## Time and identity

**Recording Window**:
A Beijing-time 12-hour interval used to package facts and produce a review. It
does not change the running strategy.
_Avoid_: execution cycle, strategy cycle

**Strategy Session**:
The continuous execution identity that remains active across Recording
Windows until the strategy reaches a terminal state or Park explicitly changes
it.
_Avoid_: cycle-scoped plan, cycle identity

**Strategy Revision**:
A new explicit version of a Strategy Session created only after Park requests
a strategy change or Reverse. Any Park-confirmed material change to direction,
strategy type, range, leverage, TP, or SL creates a new immutable revision; the
previous revision is never edited.
_Avoid_: automatic rollover plan

**Strategy Terminal**:
A strategy ends only because it reaches a strategy-level take profit or stop
loss, or Park explicitly changes the strategy. A Recording Window boundary and
a Grid Order Exit are never Strategy Terminals.
_Avoid_: cycle close, recording-window stop

**DCA Strategy Exit**:
The single take-profit or stop-loss condition that closes the whole DCA
Strategy Revision.
_Avoid_: per-addition exit

**Grid Hard Stop**:
A strategy-level stop at an authorised Grid boundary that closes the whole
Grid Strategy Revision. By default it is the lower boundary for Long Grid, the
upper boundary for Short Grid, and both boundaries for Neutral Grid; an
explicit Park instruction overrides the default. Triggering it cancels the
remaining entries, flattens the Grid positions, reconciles and seals the
revision, then pauses for Park.
_Avoid_: grid-order stop, rung exit

**Grid Order Exit**:
A take-profit that closes one Grid order or its resulting position without
ending the Grid Strategy Revision. A local per-order stop is absent by default
and exists only when Park explicitly authorises one.
_Avoid_: Grid Hard Stop, strategy stop

**Grid Boundary**:
The outer authorised price envelope of a Grid, reserved for strategy-level
invalidation rather than entry placement.
_Avoid_: outermost entry

**Grid Entry Range**:
The price interval containing Grid Rungs, inset from each Grid Boundary by one
Grid spacing. For boundaries 3800–4000 with spacing 10, the Entry Range is
3810–3990.
_Avoid_: Grid Boundary

**Grid Rung**:
One repeatable entry level inside a Grid range, with its own position and exit
conditions.
_Avoid_: one-shot limit order

**Grid Cycle**:
The lifecycle in which a Grid Rung fills, takes profit, and re-arms the same
entry level while the Grid Strategy Revision remains active.
_Avoid_: strategy restart, new revision

## Operator actions

**Trading Conversation**:
  A bounded Trading Expert conversation about trading, markets, finance, and
  Paper facts. It has research, discussion, read-only query, strategy-forming,
  and confirmation-ready modes. It may begin as exploration or a read-only
  question and does not itself create strategy authority.
_Avoid_: strategy submission, execution command

**Conversation Convergence**:
  The point at which Park's explicitly stated trading decision has enough
  fields for a complete candidate snapshot and Park has explicitly asked to
  finalize/execute; the system must ask Park for confirmation. Complete fields
  alone do not converge. Convergence is not authorization.
_Avoid_: automatic execution, model approval

**Conversation Candidate**:
  An unconfirmed set of strategy fields accumulated during a Trading Conversation.
  It may be revised or abandoned and cannot create a strategy session, proposal,
  orders, positions, or a strategy card until Park explicitly finalizes and then
  confirms the deterministic snapshot.
_Avoid_: active strategy, confirmed plan

**Cancel**:
Cancel unfilled entry orders while preserving the protective exits that manage
existing positions.
_Avoid_: flatten, stop

**Flatten**:
Close every currently open position so the net position is zero.
_Avoid_: cancel, pause

**Stop**:
End the current execution by cancelling its unfilled entry orders and
flattening its open positions.
_Avoid_: recording-window close

**Reverse Proposal**:
A Park-requested direction change for which the system calculates and displays
a complete new strategy specification before Park approval.
_Avoid_: automatic reversal, AI-authorized reversal

## Records

**Yesterday PnL**:
The net realized Paper PnL for the previous complete Beijing calendar day,
including fees and funding but excluding unrealized PnL and Shadow replay.
_Avoid_: current equity change, cumulative PnL

**12h Package**:
An immutable record of the facts observed in one Recording Window, including
plans, orders, fills, positions, PnL, reconciliation, and review evidence.
_Avoid_: handoff package
