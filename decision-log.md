# Decision Log

# 2026-11-05 — Trade review: one read-only timeline per run (#1306)

## Decision

- New read model `apps/trading-desk/src/trading_desk/review_timeline.py`, two
  GET routes (`/api/review/runs`, `/api/review/timeline`) and `gm-review.js`.
  It joins the existing ledgers. It adds no new store and writes nothing.
- Paper bars come only from that run's own recorded `market_events`, so the
  chart shows what the strategy saw. Testnet bars come from the Hyperliquid
  Testnet history. Neither falls back to another source; a gap is named on the
  page as 「这段记录缺失」.
- The end reason comes from the last segment of the seal's `terminal_key`.
- A fill stamped at the seal second keeps that ledger time and is drawn at the
  `terminal_close_completed` time. The page says how many fills this affects.
- No result is shown for a book that does not net to flat, and fees missing from
  a Testnet record stay missing. The Paper result is labelled as excluding funding.

## Gotchas

- GridMind rewrites `#review` on every render, and its tab switch finds a panel
  with a single-element query. So the new view takes over `data-panel="review"`
  and the old div becomes `review-legacy`. #1310 removes it.
- `StandardKlineChart` needs a host with a fixed height. In an auto-height host
  its resize loop grew the chart to 15,000 px.
- Plan lines outside the bar range fall off the chart, so the page passes an
  `autoscaleInfoProvider` that includes them.
- `pkill -f trading_desk` during the dev check matched the production desk as
  well as the dev one. launchd restarted it within 8 seconds; `/trade` returned
  200 and the read model showed no active strategy, orders or positions. Stop a
  dev server by its port only: `lsof -tiTCP:18790 -sTCP:LISTEN`.

## Verification

- `tests/test_review_timeline.py`, `tests/test_gm_review.py`, the review route
  tests in `tests/test_desk.py`: desk suite 94 passed.
- Dev instance with a scratch copy of the real ledgers: Paper 9/8 run shows 56
  fills, net −122.14, the 4300/4500/4250 lines, 4 events and 3 backdated fills.
  Testnet 0e696f60 shows 「成交记录没有轧平，算不出结果」. At 375px there is no
  horizontal scroll.

