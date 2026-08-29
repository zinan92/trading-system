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

**Portfolio Session**:
The account-scoped execution identity created by one Strategy Session. It may
allocate across multiple assets at the same time; the Strategy and Portfolio
remain the top-level decision and lifecycle owners rather than creating one
independent strategy per asset.
_Avoid_: asset-owned strategy session, one top-level lifecycle per asset

**Portfolio Snapshot**:
The immutable, account-scoped fact set used for one Portfolio evaluation. It
binds equity, cash, positions, open orders, allocation/execution slices,
ownership, freshness, and coherence to one Portfolio Session identity.
_Avoid_: live account query, mutable portfolio cache, asset-only snapshot

**Portfolio Policy**:
The versioned set of Portfolio-level concentration, capacity, exposure, margin,
loss, and cash-buffer limits. It is a subtractive gate configuration and does
not generate strategy signals or increase a Strategy Position Plan.
_Avoid_: strategy sizing algorithm, Broker capability profile, auto-rebalance rule

**Asset Allocation Slice**:
The Portfolio Session's current allocation intent for one asset. It records how
much of the Portfolio is assigned to that asset, but it does not own the
strategy algorithm or become a separate Strategy Session.
_Avoid_: asset strategy, isolated strategy identity

**Execution Slice**:
The Broker-bound orders, position, protection, fills, and reconciliation facts
used to realize one Asset Allocation Slice. It has local execution state, while
the Portfolio Session owns allocation, risk, and continuation decisions.
_Avoid_: independent asset lifecycle, venue-owned strategy authority

**Portfolio Risk Hold**:
A Portfolio-level pause on new entries when an Asset Allocation Slice has an
unknown non-zero exposure or when shared-account margin/risk cannot be proven
independent. A flat, causally reconciled slice may be removed while unrelated
slices continue only when the Portfolio can still prove the account-wide risk
and ownership boundaries.
_Avoid_: automatic flatten of every asset, ignoring an unknown position

**Portfolio Risk Gate**:
The fixed Portfolio constraint boundary applied to a Strategy Position Plan. It
may accept the requested plan unchanged, scale exposure down, or reject it. It
may never increase exposure, change direction, or rewrite the Strategy's
position-management semantics.
_Avoid_: second strategy, alpha generator, position-size augmenter

**Path A Testnet Slice**:
The first bounded Hyperliquid Testnet rollout of the existing Grid or Canonical
DCA Strategy Family: scan the full default-perp candidate universe, select one
eligible asset (BTC is the first proof), and run one Execution Slice through
the Testnet Automation Coordinator. It uses the existing Dashboard read/control
contract and requires canonical reconciliation and position-following
protection before continuation. Strategy extraction into a separate repository
and concurrent multi-asset execution are future directions outside this slice.
_Avoid_: generic multi-Broker rollout, multi-asset portfolio, automatic Broker switching

**Canonical DCA Strategy**:
The previously established Paper DCA strategy contract is the algorithmic
foundation for every Broker adaptation; a transport adaptation may add venue
safety gates but must not replace its entry, sizing, aggregate-exit, or
terminal semantics with a new DCA algorithm.
_Avoid_: new external DCA algorithm, disposable Testnet strategy, rebuilt DCA foundation

**Strategy Module**:
A future Broker-neutral module that consumes canonical market/instrument facts
and emits strategy intents—including desired position size, position management,
and exit/protection intent—without importing a Broker implementation or the
Trading System composition root; the current repository still co-locates part
of this logic with the Trading System host.
_Avoid_: venue strategy, Broker-owned strategy, Dashboard strategy logic

**Strategy Position Plan**:
The Strategy's complete desired position expression for an asset or candidate
set: when to enter, how much to hold, how to add or reduce, and how to exit or
protect the position. It is the source of strategy semantics before Portfolio
risk constraints are applied.
_Avoid_: raw signal without sizing, Portfolio allocation decision, Broker order

**Strategy Candidate Set**:
The Strategy's ranked collection of asset-specific Position Plans considered at
one Portfolio evaluation. Ranking and desired size come from the Strategy; the
Portfolio Gate may accept only a feasible subset.
_Avoid_: Portfolio-approved holdings, unranked asset universe

**Portfolio Selection**:
The immutable result of applying Portfolio constraints to a Strategy Candidate
Set: accepted assets, effective sizes, rejected candidates, and the reasons for
any downward scaling or rejection.
_Avoid_: rewritten Strategy plan, hidden allocation mutation

**Portfolio Rebalance Decision**:
An explicit decision to reduce or exit existing Asset Allocation Slices in order
to admit different candidates. Rebalance is not an automatic consequence of a
new opportunity ranking and must preserve the old and new allocation evidence.
_Avoid_: automatic rotation, hidden replacement, Strategy revision


**Trading System Composition Root**:
The host that binds Strategy, Data Feed, Broker, risk, authorization, lifecycle,
read-model, and control contracts; it coordinates modules but does not redefine
their domain algorithms or venue wire semantics.
_Avoid_: strategy engine, Broker implementation, Dashboard backend

**Testnet Automation Coordinator**:
The composition-root seam that binds one explicit Hyperliquid Testnet Strategy
Session to candidate selection, Portfolio Gate, Broker execution, lifecycle,
scheduler, and durable read-model evidence. It exposes control and observation
contracts while preserving the ownership boundaries of those modules.
_Avoid_: venue-native API, second strategy engine, automatic live promotion

**Broker Transport Substitution**:
A change of Broker and environment binding that preserves the established
Strategy, risk, control, read-model, and lifecycle contracts; the binding must
also select an explicit matching Instrument and execution-grade market source,
never silently mixing one venue's prices with another venue's execution.
_Avoid_: strategy rewrite, hidden venue alias, Binance-price/Hyperliquid-order mixing

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

## Product control language

**Venue Profile**:
The explicit Broker-and-Environment identity selected by the operator, such as
Hyperliquid Testnet or Binance Paper. A Venue Profile is not a generic
exchange name and cannot silently fall back to another environment.
_Avoid_: exchange alias, hidden environment

**Instrument Catalog**:
The source-bound set of explicit Instruments exposed by a Venue Profile,
including each instrument's identity, market-fact freshness, and execution
eligibility. A ticker string alone is not an Instrument Catalog entry.
_Avoid_: hard-coded pair list, symbol dropdown without identity

**Dashboard Control Plane**:
The authenticated Dashboard surface that lets Park select a Venue Profile,
Instrument, and Strategy, inspect a non-authorizing preview, and issue explicit
control intents. It observes and composes domain contracts; it does not own
strategy, risk, or Broker wire semantics.
_Avoid_: Dashboard strategy engine, browser-owned scheduler

**Strategy Configuration**:
The operator-facing fields used to describe one Strategy Position Plan through
the canonical Grid or DCA vocabulary. It is a form-level representation, not a
request to edit internal JSON or to invent missing strategy facts.
_Avoid_: raw JSON plan, free-form order command

**Strategy Preview**:
The immutable, non-authorizing calculation of requested strategy sizing,
Portfolio Gate effects, market and account facts, maximum loss, and execution
identity before confirmation. A preview never submits an order or creates a
position.
_Avoid_: dry-run order, implicit authorization

**Effective Plan**:
The operator-visible result after the Portfolio Risk Gate accepts, scales down,
or rejects a Strategy Position Plan. It preserves the requested plan and the
reason for every downward change or rejection.
_Avoid_: silently resized plan, rewritten strategy

**Testnet Proof**:
A bounded end-to-end demonstration in which one selected Testnet Instrument
executes one canonical Strategy Revision through confirmation, order/fill,
protection, reconciliation, and terminal notification evidence. It is not a
promotion to Mainnet or Live.
_Avoid_: fixture-only readiness, live canary

**Operator Confirmation**:
Park's explicit approval of one fresh Strategy Preview and its Effective Plan,
bound to the Venue Profile, Instrument, Strategy Revision, and digest shown in
the Dashboard. Selection or a complete set of fields alone is never approval.
_Avoid_: implicit click-through, model authorization

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
