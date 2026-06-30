# North Star Framework — trading-orchestrator (worldview → disciplined trade)

> Stage caveat: pre-PMF (~2%, likely N=1). This is a **compass that dictates what to
> build**, not yet a dashboard to optimize. The metric is chosen precisely because
> optimizing it forces the differentiated, non-extractive product into existence.

## The fork this resolves

The North Star is the same decision as "casino vs disciplined tool." If you pick a
**volume/engagement** North Star you have chosen to build the casino (optimizing it =
getting people to trade more = harming most of them). If you pick a **value-delivered**
North Star you have chosen the worldview-compiler. This doc commits to the second.

## Business Game

**Productivity** (not Attention, not Transaction).

The value is *better decisions* — faithfully translating a user's conviction into a
disciplined, risk-bounded, tracked position, and telling them honestly whether the
thesis is playing out. It is NOT screen time (Attention) and NOT trade count
(Transaction). Reject the Attention framing explicitly: for a trading tool, more
engagement often means more anxiety and more overtrading, i.e. *negative* user value.

## North Star Metric

**Weekly Conviction Loops Closed (WCL)**

**Definition**: count, per week, of *theses* that complete the full loop:
1. user articulates a worldview →
2. it is compiled into a concrete, **risk-bounded** strategy on a specific instrument →
3. it runs (paper or live) →
4. it is **reviewed against its own stated, falsifiable premise** →
5. the user makes a decision (hold / adjust / exit / log-the-learning).

A "thesis" is the unit of value. A "loop closed" is one full turn of conviction →
expression → honest feedback → decision.

**Why this metric**: the entire defensible value — and the whole anti-casino guardrail —
lives in step 4–5 (the feedback loop). Anyone can get a user to *open* a position; almost
no product closes the loop. Optimizing WCL forces you to build exactly the differentiated
things: structured thesis capture, faithful translation, risk bounds, and thesis-tracking.

**Current value**: ~0 (the instrumentation doesn't exist yet — which is the point: WCL is
also the build directive).
**Target (compass, not commitment)**: get WCL ≥ 1/week for yourself (dogfood N=1) before
anything else; that single loop proves the product thesis end-to-end.

## Validation

| Criterion | Pass? | Notes |
|---|---|---|
| Expresses value | Y | The value *is* disciplined conviction + honest learning. |
| Leading indicator of revenue | Y | Users who close loops retain and would pay; volume/AUM are lagging. |
| Measurable | Partial | Needs instrumentation (structured thesis + falsifiable premise + review event). Buildable — and building it *is* the product. |
| Understandable | Y | "People actually close the loop on their convictions." |
| Actionable | Y | Each step is a feature lever (extraction, translation, bounds, review nudge). |
| Not vanity | Y | Strongly anti-vanity; cannot rise from idle usage. |
| Not gameable without real value | Y | You cannot fake an honest thesis review; gaming requires doing the thing. |

## Input Metrics (the levers that drive WCL)

| Input Metric | Drives WCL by | Owner | Notes |
|---|---|---|---|
| Worldviews compiled → strategy (per week) | Top of funnel: does the AI analyst turn convictions into concrete strategies at all? | AI-analyst layer | The "compiler works" metric. |
| % strategies that are risk-bounded & activated | Faithful translation + actually runs | Strategy/exec layer | A thesis that never runs can't close. |
| % live theses with a defined falsifiable premise + tracking signal | You can only "close" what you can judge | Thesis-tracking layer | This is the moat instrumentation. |
| Review rate (% theses reviewed at their checkpoint) | The loop-closing behavior itself | Product/UX | Nudges, scheduled reviews. |
| Translation faithfulness (executed vs stated worldview) | Quality, not just quantity, of loops | AI-analyst layer | Guards against "AI traded something I didn't mean." |

## Metrics Constellation

```
            Weekly Conviction Loops Closed (WCL)   ← North Star (value delivered)
                          ▲
        ┌─────────────────┼───────────────────┬──────────────────┐
   worldviews        % risk-bounded      % theses with        review rate
   compiled→strategy  & activated        falsifiable premise   (loop closed)
   (compiler works)   (faithful run)     + tracking (judgeable) (the behavior)
                          │
                  translation faithfulness (quality guard)
```

## Counter-Metrics (Goodhart guards)

| Metric | Protects against |
|---|---|
| User drawdown / risk-of-ruin | "Loops closed" achieved by churning losing trades. |
| Overtrading rate & realized leverage | The casino failure mode — esp. "scale-in-on-dips with 0.8–1.2x leverage" can become add-to-losers-into-ruin. |
| Translation faithfulness | The AI generating trades that don't represent the user's actual view. |

## Anti-Patterns Avoided (and why)

- **Trading volume / notional**: the casino metric. Gameable, extractive, and the user
  already identified it as the danger. Optimizing it harms most users.
- **AUM**: lagging; rewards gathering deposits, not delivering value.
- **DAU / engagement**: vanity for a trading tool; more screen time ≠ value (often the
  opposite — anxiety, FOMO, overtrading).
- **Revenue**: lagging; never a North Star.

## What this implies for the build (compass → action)

WCL is ~0 because steps 1, 4, 5 don't exist yet. So the metric *prescribes* the next
work in priority order:
1. **Structured thesis object** — capture worldview + falsifiable premise + chosen
   archetype + instrument + risk bounds (this sits on your existing SignalEngine registry).
2. **Thesis tracking** — a signal that says "is the premise playing out?" + a scheduled
   review.
3. **The review/decision surface** — the loop-closing UX.

Execution (Binance/IB/paper) is *not* on this list — it's commoditized and already mostly
built. The North Star says: stop adding execution rails; build the thesis-loop.
