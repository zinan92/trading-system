# Telegram Trading Expert design

Issue: #847
Status: approved for implementation by Park in the current task

## Outcome

The Park Telegram bot becomes a bounded Trading Expert. It can research and
discuss markets and strategy ideas, maintain an editable strategy draft, and
prepare a complete Paper proposal. Conversation convergence is a review point,
not execution authority. Only Park's explicit finalize/execute intent may cross
the proposal seam, and the existing exact confirmation and Paper safety gates
remain authoritative.

## Modes and transitions

The conversation contract keeps read-only and execution-oriented states
separate:

```text
query / research / discuss / off_topic
                 |
                 v
        strategy_forming  <---- revise / abandon
                 |
       complete fields + explicit finalize intent
                 v
       ready_for_confirmation
                 |
          exact Park confirmation
                 v
          existing Paper path
```

The provider may classify a message and propose a draft patch, but it cannot
create a strategy session, proposal, order, position, confirmation receipt, or
execution authorization. A complete draft without explicit finalize intent
stays in `strategy_forming`.

Research mode is bounded to the facts and material supplied to the adapter. It
may compare hypotheses, evidence, assumptions, and falsifiers, but it must say
when external/current source material is unavailable. This change does not add
a web-research connector.

## Trading domain contract

The provider prompt must preserve explicit fields across turns and explain the
semantic distinction between:

- Grid: authorized Boundaries, Grid Entry Range, spacing mode, rung prices or
  order count, sizing/risk authority, and strategy-level Hard Stop. A Grid Order
  Exit does not terminate the Strategy Revision unless the existing lifecycle
  says so.
- DCA: direction, entry prices or range, number/size of additions, maximum
  leverage or acceptable loss, and explicit strategy-level stop loss and take
  profit. The parser must not infer exits from the range.

The draft retains explicit user fields, missing fields, assumptions, conflicts,
and evidence. Model output remains untrusted and is normalized by the existing
deterministic planner.

## Module and seam

`ParkTelegramConversationAgent` remains the deep conversation module. Its small
external interface is `evaluate(text, update_id, context) -> conversation
result`; its implementation owns bounded history, provider fallback, explicit
field merging, and conversation journaling.

`ParkTelegramRouter.handle_update` remains the execution seam. It routes
conversation results to the existing deterministic normalizer and risk planner
only when the result is `ready_for_confirmation` with explicit execution
intent. The active strategy guard continues to block strategy mutation, while
read-only/query/research/discussion responses remain available during an active
strategy.

## Failure behavior

- Provider timeout, malformed JSON, or unavailable credentials falls back to a
  deterministic read-only response or the existing narrow strategy parser;
  neither fallback may authorize a proposal.
- Missing or conflicting Grid/DCA fields produce a visible draft and follow-up,
  not a guessed value.
- Untrusted/stale market, account, reconciliation, ownership, or Paper state
  continues to block proposal creation.
- The exact Park confirmation digest remains the only execution capability.

## Tests

The public seam under test is `ParkTelegramRouter.handle_update`. Focused tests
must cover:

1. research/discussion/query do not create plans or execution authority;
2. multi-turn Grid and DCA fields merge into an editable draft;
3. complete fields without explicit finalize remain conversation-only;
4. explicit finalize creates a proposal with `execution_authorized=false`;
5. active strategies still answer read-only questions without accepting edits;
6. provider fallback preserves the same no-authorization invariants;
7. the deployed Paper copy reproduces the same behavior.

## Non-goals

No live trading, exchange-key handling, new broker integration, automatic
strategy switching, external web-research connector, risk-formula change, or
deployment to real-money infrastructure.
