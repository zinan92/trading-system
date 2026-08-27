# Issue #1067 — Conversation-first risk preview

Date: 2026-08-27

## Decision

Jessie now treats a complete, calculable candidate as a conversation preview,
not as a provider-owned form completion. The internal semantic patch remains
untrusted and non-authorizing. A deterministic calculator derives only values
whose inputs are explicit or whose equal-split rule is already part of the
canonical planner; fees, slippage, account equity, and authorization stay
explicitly unavailable when they are not authoritative.

The execution gate remains downstream. Ordinary discussion, clarification, and
read-only questions cannot call clean-slate admission. Only an explicit
finalize/confirm path can create a proposal, and the proposal still requires
Park confirmation before any Paper action.

## Worked DCA example

For two short entries at 80000 and 81000, each with explicit notional `5×AUM`,
stop 82000, and take-profit 73000, the calculator records:

```text
total entry exposure = 5×2 = 10× AUM
stop loss = 5×(82000-80000)/80000 + 5×(82000-81000)/81000
          = 18.67283951% AUM (gross price loss)
```

The reply labels these as derived and states that fees/slippage are not
included. If only total maximum leverage is stated, the existing equal-split
planner rule derives the per-entry notional and uses the same formula.

The same preview seam covers Grid geometry when direction, boundaries, count,
maximum leverage, and trusted current price are present. It derives total
exposure and the full-depth Hard Stop loss without creating a plan.

## Verification

- The provider-timeout and provider-success conversation paths return the same
  local risk preview; a complete candidate does not wait for the provider.
- A discussion with an open-position fixture remains `conversation_replied`
  and never enters clean-slate admission.
- Focused conversation/parser/runtime/provider validation passes `93` tests;
  ruff, compileall, and diff-check pass. No credentials, Telegram token,
  order, position, scheduler, cloud, Mainnet, or Live mutation occurred.
