# Trader UI Information Architecture Audit

Date: 2026-06-27
Scope: retired trader console and replay surfaces before the v4 shell migration

## Executive Summary

The current issue is not lack of functionality. The product has too many user-facing surfaces competing for attention.

The main product split should be:

- Trader-facing: helps a trader decide, review, and learn from trades.
- PM-facing: helps a portfolio manager compare strategies and allocate attention.
- OPS-facing: proves the machine is alive, safe, reconciled, and alerting.

The current Replay page mixes all three. This creates a page that is technically rich but visually unclear. The primary fix is to make the Replay page a trading replay tool, not a backend maturity audit page.

Target user journey:

1. Pick strategy and replay time.
2. Watch the main candle chart advance.
3. See current Go / No-Go and Entry / TP / SL.
4. If needed, open the evidence drawer.
5. If something is unsafe or broken, show one concise blocker and link to OPS.

## Implemented Cleanup On 2026-06-27

The first cleanup pass has been applied to the product, not just documented.

### Dashboard V3

- The Overview page now starts with the trader command strip before the NAV chart, so the user sees the PM action first.
- The strategy-vs-gold chart remains the primary performance surface.
- The detailed promotion, evidence, maturity, and platform-diagnostic boards are behind the `More evidence / platform diagnostics` drawer.
- The right-side `Today Brief` panel now shows only two default cards:
  - Today stance: whether to add exposure or review first.
  - OHLC trust: whether the candle data is usable for replay or promotion.
- Park direction filter, chart focus, evidence gap, shortlist, and Trader Focus are moved under `Expand today details`.

Current default visible hierarchy:

```text
Command strip
Strategy vs Gold chart
Today Brief: action + OHLC trust
Action board
Review queue
Evidence / platform diagnostics drawer
```

This makes the Dashboard a PM command surface instead of a mixed PM/OPS report.

### Multi-Timeframe Replay

- The replay page uses a main replay chart plus 2x2 context charts.
- Replay transport is compressed into a TradingView-style control bar.
- The current candle decision lens is compact: status, direction, confidence, price, plan, and next step.
- Trade illness card, signal cross-section, PM attribution, and technical detail are collapsed by default.
- Boundary and synchronization details are not allowed to compete with the main chart unless opened deliberately.
- The area below the chart is now one `Review Workspace`, not separate competing sections.
- The workspace order is:
  - Trade record / current decision / attribution.
  - Market oral view / multi-timeframe locator.
  - System sync / replay boundary / PM evidence internals.
- OPS wording is kept out of the default Trader view; system details stay collapsed unless a blocker requires investigation.
- Inside the trade review drawer, the first visible panel is now a single current-candle review card:
  - Current candle decision dock.
  - Decision illness card.
  - Selected trade illness card, if a trade exists.
  - Signal cross-section and PM attribution stay as secondary drawers.

Current default visible hierarchy:

```text
Replay transport
Main replay chart
Context charts
Current candle decision lens
Review Workspace:
  Trade illness / decision / attribution drawer
  Market context drawer
  System detail drawer
```

This makes Replay a chart-first review tool instead of an audit wall.

## Role Split

### Trader Needs

Trader needs only the information that affects interpretation or execution:

- Main replay candle chart.
- Context candle charts for higher/lower timeframes.
- Current candle decision: Go / No-Go / Long / Short / Watch.
- Entry, Take Profit, Stop Loss, and risk/reward.
- Why the decision happened, in plain language.
- Recent trade markers on the chart.
- Market view / Park oral direction only if it is active and affects the decision.
- Trade record card when a trade exists.
- Review notes and attribution after the trade.

### PM Needs

PM needs strategy-level and portfolio-level judgment:

- Which strategy is healthy enough to evaluate.
- Which strategy has enough closed trades to judge edge.
- Frequency attribution: no signal, risk blocked, quality gate blocked, system blocked.
- Strategy comparison table.
- Review queue: what needs human attention today.
- Promotion or demotion status, only after enough samples.

PM information should appear on the dashboard, not dominate the candle replay page. Replay can link to PM evidence when reviewing a specific strategy or trade.

### OPS Needs

OPS needs system safety and runtime details:

- Data feed freshness.
- Runner heartbeat.
- Broker/demo reconciliation.
- TP/SL coverage.
- Alert delivery.
- Backend maturity M0-M5.
- Artifact availability.
- API/gateway/public access health.

OPS information should live in `ops-dashboard.html` or a collapsed debug drawer. It should not occupy the default Trader replay screen unless it blocks trading.

## Replay Page Inventory

### Keep Above The Fold

| Section | Role | Keep | Reason |
|---|---|---:|---|
| Header controls: date, strategy, replay time | Trader | Yes | Required to choose replay context. |
| Replay transport: select candle, prev, play, next, +/-15m | Trader | Yes | Core replay control. |
| Main 1m chart | Trader | Yes | Primary visual focus. |
| Context charts 1D/4H/15m/5m | Trader | Yes | Supports multi-timeframe judgment. |
| Current candle decision dock | Trader | Yes | One sentence: Go/No-Go, direction, confidence, price. |
| Entry / TP / SL plan | Trader | Yes | Required when decision is Go; otherwise a compact "no plan". |

### Keep But Move Below Chart

| Section | Role | Keep | Placement |
|---|---|---:|---|
| Market oral view | Trader/PM | Yes | Below chart, collapsed unless active or expired recently. |
| Signal cross-section | Trader | Yes | Drawer under decision dock. |
| Trade record card | Trader/PM | Yes | Drawer, only expanded when trade exists or selected. |
| Replay attribution worksheet | PM | Yes | Below fold, review mode only. |
| Key event navigation | Trader | Yes | Compact horizontal event tape below chart. |

### Collapse By Default

| Section | Role | Collapse Reason |
|---|---|---|
| PM trust gate M1-M5 | PM/OPS | Important but not a candle replay primitive. Show only a single status chip by default. |
| PM evidence checkbench | PM | Too much audit detail for primary Trader workflow. |
| PM evidence detail | PM | Detail drawer only. |
| Decision pipeline | PM/Debug | Duplicate of decision dock and cross-section. |
| Timeframe locator cards | Trader/Debug | Current chart cursor already shows this; keep as hover tooltip or drawer. |
| Replay boundary / sync details | Debug | Useful for validation, not for daily trader focus. |
| As-of alignment and keyboard shortcuts | Debug/Help | Move to help drawer. |

### Move To OPS

| Section | Role | Why |
|---|---|---|
| Backend maturity M0-M5 raw evidence | OPS | It is system trust data, not trade replay data. |
| Alerting / Feishu delivery status | OPS | Only show a red blocker on Trader page if broken. |
| Data freshness / API public gateway details | OPS | Trader only needs "data fresh" or "data stale". |
| Reconciliation / broker safety internals | OPS | Trader only needs "execution blocked: reconciliation drift". |
| Artifact path / generated_at debugging | OPS | Not a human trading decision input. |

## Replay Page Duplicates

| Duplicate Information | Current Locations | Recommendation |
|---|---|---|
| Current Go / No-Go decision | decision gate top strip, decision dock, decision pipeline, current K-line card | Keep one primary decision dock; put details in drawer. |
| Entry / TP / SL | plan panel, decision dock, trade card, lifecycle strip | Show once in decision dock when Go; repeat inside trade card only for selected trade. |
| M1-M5 maturity | PM trust gate, PM evidence checkbench, PM evidence detail | Show one compact trust chip; full detail behind "PM evidence". |
| Replay timing / boundary | timeline, boundary strip, locator cards, chart overlay | Keep chart cursor and transport time; move sync details to help/debug drawer. |
| Strategy identity | header, strategy brief, replay target, PM evidence | Header is enough; strategy brief only in PM review mode. |
| No-Go reason | decision top, dock, pipeline, trade/no-go card | Keep the clearest reason in dock; cross-section drawer has the full breakdown. |
| Market view status | market panel, signal filter, decision pipeline | Keep only if active/expired and affecting direction filter. |

## Dashboard V3 Inventory

### Keep As Default Trader Dashboard

| Section | Role | Keep | Reason |
|---|---|---:|---|
| NAV / Gold comparison | PM/Trader | Yes | Top-level outcome. |
| Strategy performance board | PM | Yes | Primary PM control surface. |
| Strategy health / triage table | PM | Yes | Tells who needs attention. |
| Replay entry points | Trader/PM | Yes | Launches specific review. |
| Daily loop summary | PM | Yes | Plan-review-learning loop. |
| Review queue | PM | Yes | Drives daily action. |

### Move Or Collapse

| Section | Role | Action |
|---|---|---|
| Execution safety board | OPS | Collapse to one safety banner; link to OPS. |
| Product trust M1-M6 journey | PM/OPS | Keep as "Platform maturity" drawer, not default hero. |
| Detailed strategy workflow guide | PM | Collapse under strategy table. |
| M4 frequency internals | PM | Show summary; detail drawer. |
| Raw trade journal grid | PM | Keep under Replay/Journal tab, not front page. |
| API/data/backend trust text | OPS | Move to OPS dashboard except critical blockers. |

## Dashboard vs Replay Boundary

Dashboard V3 should answer:

- Is the portfolio making or losing money?
- Which strategies deserve review?
- Which strategy needs action today?
- Is the system safe enough to trust?
- Where should I click next?

Replay should answer:

- At this candle, what did the strategy see?
- Did it Go or No-Go?
- If Go, what were Entry / TP / SL?
- Did the trade follow the plan?
- What should I learn from this candle or trade?

The same information should not appear at equal weight in both places. Dashboard selects the review target. Replay performs the review.

## Proposed Information Architecture

### Replay Page V2

Default visible layout:

```text
Header: strategy / date / replay time / return
Transport: select candle / prev / play / next / jump to latest GO / latest trade

Left: Main replay chart (1m)
Right: Context charts (1D, 4H, 15m, 5m) in 2x2

Decision dock:
  Current candle: GO / NO-GO
  Direction + confidence
  Entry / TP / SL if GO
  One-line reason

Below fold:
  Tabs or accordion:
    Evidence
    Trade card
    Market view
    PM attribution
    Debug / OPS
```

Default hidden:

- Replay contract.
- Boundary sync cards.
- Full PM trust gate.
- PM evidence checkbench.
- PM evidence detail.
- Decision pipeline.
- Raw cross-section table.
- OPS safety internals.

Critical exception:

- If there is a blocker, show one red banner above charts:
  "Trading blocked: reconciliation drift / stale data / protection missing. Open OPS."

### Dashboard V3 V2

Default visible layout:

```text
Top:
  Portfolio NAV vs Gold
  Safety status chip
  Today's action: review X / fix Y / no action

Main:
  Strategy table with performance, samples, frequency, status
  Review queue

Side:
  Daily loop status
  Market view status
  Open risks

Tabs:
  Overview
  Strategies
  Replay
  Journal
  OPS link
```

Default hidden:

- Backend maturity detail.
- Execution safety internals.
- Data artifact details.
- Long PM journey explanation.

## Priority Order

### P0: Stop User Attention Fragmentation

- Replay page: keep chart + decision dock visible; collapse PM/OPS blocks.
- Dashboard: keep NAV, strategy table, review queue; collapse platform maturity and OPS internals.

Success criteria:

- A first-time trader can identify the primary action within 5 seconds.
- Replay above the fold contains only: controls, charts, current decision, plan.
- No duplicate Go/No-Go or TP/SL blocks above the fold.

### P1: Separate OPS Surface

- Create or reinforce "Open OPS" path.
- Trader pages show only one safety status chip and one blocker banner.
- OPS page owns all detailed health data.

Success criteria:

- Trader page has no raw backend status cards unless status is red.
- OPS page can still diagnose data, runner, reconciliation, and alerts.

### P2: Rebuild Replay As Review Tool

- Add drawers/tabs under chart: Evidence, Trade Card, Market View, PM Attribution, Debug.
- Show Evidence drawer only when user asks "why".
- Show Trade Card only when selected candle has trade or selected event.

Success criteria:

- A replay user can step candles without scrolling through audit sections.
- A selected trade can be understood in one card.

### P3: Dashboard And Replay Linkage

- Dashboard chooses strategy/trade to review.
- Replay opens focused context with minimal extra PM scaffolding.
- Replay returns to dashboard with same strategy/date filter.

Success criteria:

- Dashboard and Replay no longer duplicate strategy performance boards.
- Replay never tries to be a portfolio dashboard.

## Product Principle

Every visible element must pass one of these tests:

1. Does this help the trader decide or review the current candle?
2. Does this help the PM choose which strategy/trade to review?
3. Is this a critical safety blocker that must interrupt the trader?

If the answer is no, the element should be hidden, collapsed, or moved to OPS.

## 2026-06-27 Implementation Notes

- Dashboard V3 right-side Today Brief is compact by default: only today's stance and OHLC trust stay visible; chart focus, evidence gaps, shortlist, and trader focus move into `todayDetailDrawer`.
- Replay review workspace now has three drawers in fixed priority order:
  1. `复盘工作台：病历 / 决策 / 归因`
  2. `市场上下文：口述观点 / 多周期定位`
  3. `系统细节：同步 / 边界 / 证据台`
- The first Replay drawer is one primary trade-review panel: current candle decision, selected trade card, and PM attribution support. Cross-sectional evidence and PM attribution checklist are secondary collapsed drawers.
- The market context drawer is now two-card layout: `市场背景` plus `多周期定位`. Repeated current decision and execution plan are collapsed under `展开本根决策和执行计划`;口述来源、过期规则、允许/拦截方向 are collapsed under `展开口述证据和过期条件`.
- The OPS drawer now shows only one intro and three diagnostic groups by default: `回放同步和边界`, `证据完整性和门控`, and `事件导航和生命周期`. Workflow rail, boundary strip, evidence drilldown, decision pipeline, selection receipt, event rail, and trade lifecycle remain available inside those groups but no longer occupy the default review surface.
- Dashboard V3 `策略相对黄金` no longer uses four separate edge tape cards above the strategy ledger. It now uses one `PM 摘要` strip with three compact facts: leading strategy, first replay target, and confidence issue; per-strategy detail remains in the ledger below.
- Replay chart layout now follows the trader sketch explicitly: `1m` is the large primary replay chart on the left, spanning two rows; `1D`, `4H`, `15m`, and `5m` are context charts in a 2x2 grid on the right. The layout is enforced with named CSS grid areas so later desktop media queries cannot drift back to the old four-up/top layout.
- Replay chart interaction is now an explicit TradingView-like contract: mouse wheel zooms the visible time range, time/price axes can be dragged and double-click-reset, and the chart container contains wheel/touch gestures so the page does not steal the interaction. Browser verification showed the `1m` visible range expanding from about 109 bars to about 594 bars, then shrinking back to about 59 bars.
- Trader Focus chart headers now hide repeated candle-range/decision-time lines; the chart keeps OHLC and the x-axis/crosshair carry time context. Detailed sync/boundary text remains available in the OPS drawer.
- Cross-timeframe hover sync is verified on real Lightweight Charts: hovering a visible `1m` candle maps to `1D`, `4H`, `15m`, `5m`, and `1m`, sets the synchronized crosshair position, and applies `hover-synced` visual focus to every chart while keeping the source chart marked separately.
- Price-axis drag zoom is exposed through the debug contract and verified in browser: dragging the right price axis changed the `1m` price visible range from about `1.87` to `3.31`, proving the vertical axis interaction works instead of only horizontal wheel zoom.

Browser verification screenshots:

- `/tmp/goldbot-dashboard-v3-today-brief-compact-final.png`
- `/tmp/goldbot-replay-review-panel-single-final.png`
- `/tmp/goldbot-replay-market-context-compact-final.png`
- `/tmp/goldbot-replay-ops-drawer-compact-final.png`
- `/tmp/goldbot-dashboard-v3-edge-summary-final.png`
- `/tmp/goldbot-replay-left-main-2x2-context-final.png`
- `/tmp/goldbot-replay-wheel-zoom-denoise-final.png`
- `/tmp/goldbot-replay-hover-sync-axis-contract-final.png`
