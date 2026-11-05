# Spec — switching the Paper gold source (S4)

Status: **Park chose OKX on 2026-11-05.** Step S4 of
[`maturity-plan-2026-11-05.md`](maturity-plan-2026-11-05.md). Being built in two
PRs (datafeed, then this repo). Deploying them needs Park's separate OK.

## 1. Why this is not a config flag

A Paper plan is trustworthy only when three things come from **one venue**:

| What | Today | Where it is wired |
|---|---|---|
| Bars (price, trust gate) | Binance USDⓈ-M `XAUUSDT` | `configs/pipeline.yaml` `datafeed.source` and `instrument_routes.GOLD.source` |
| Instrument definition (tick, lot, min size, margin) | Binance `XAUUSDT` | `configs/dualtrack.yaml` `execution_shadow.nautilus.instrument_endpoint`, `instrument_source`; `services/config_loader.py` (rewrites the endpoint from `TRADING_ORCHESTRATOR_DATAFEED_URL`); `services/dualtrack_instrument_source.py` defaults |
| Execution instrument and fees | `XAUUSDT.BINANCE`, taker 0.0400% from a Binance demo account | `configs/dualtrack.yaml` `execution_contract`, `paper_fee_model`; `services/dashboard_control_plane.py` venue profile `binance.paper` |

These keys live in five places and are set independently. If only the bars
change to OKX, Paper fills and sizes still follow Binance's contract: a plan
with mixed provenance. The fail-closed rules exist to stop exactly that.

## 2. The contracts are not the same

| | Binance `XAUUSDT` | OKX `XAU-USDT-SWAP` |
|---|---|---|
| Price tick | 0.01 | **0.1** |
| Size step | 0.001 XAU | 0.001 XAU (1 contract × `ctVal` 0.001) |
| Daily bar boundary | UTC | UTC (`1Dutc`; plain `1D` is Hong Kong time) |
| Fee source today | demo account `commissionRate` (observed 2026-07-10) | none yet: public endpoints carry no fee. Must be an explicit Paper assumption |
| Reachable from this Mac | **no** (HTTP 451 since ≥ 2026-11-04) | yes |

A 0.1 tick changes every DCA ladder and grid level. Replay goldens for gold
would need new fixtures; they cannot be reused silently.

## 3. Seam

The Paper runtime does **not** read the Dashboard selection. `park_control`
builds `ParkPaperRuntime` from config, and the default market reader hard-codes
`binance` / `XAUUSDT.BINANCE`. So the seam is one config block, not the selection:

```
paper_gold_venue:
  active: okx            # binance | okx; an unknown value fails closed
  profiles:
    binance: {market_source, instrument_source, execution_instrument_id, contract, fee_model, label}
    okx:     {...}
```

- **One loader derives every touchpoint** from the active profile:
  - datafeed route source (charts and the `dual` direct read)
  - execution-market venue and instrument
  - instrument endpoint and source
  - execution contract
  - fee model
  - the Dashboard `*.paper` catalog
  - desk labels

  The five places listed in §1 can no longer disagree.
- **Invariant:** bar source == instrument-definition source == execution-instrument
  venue. A plan whose instrument differs from the active profile's is refused.
  Switching the profile while a Paper session is active is refused too. Both are
  fail-closed blockers.
- **No fallback.** If OKX is down, Paper gold is blocked. It never borrows
  Binance bars.
- The chart header, trust gate and receipts name the venue and instrument.

Already shipped: datafeed serves `okx_swap_public` candles and instrument
definitions (zinan92/datafeed#182).

## 4. Work

1. **datafeed:** the execution-market worker gains `XAU-USDT-SWAP.OKX`. Dispatch
   and parsing key on the venue; one venue failing never stops another.
2. **This repo:**
   - the profile block and loader, plus the guards
   - an explicit OKX Paper fee assumption, with source and date
   - replay goldens: the existing goldens are built on Binance fixtures with
     explicit contracts and are kept unchanged. **OKX goldens are deferred**
     until a first OKX Paper session gives real events to capture. Tests that
     depend on a tick pin the venue (`dualtrack_config(paper_gold_venue=...)`)
3. A browser check on the dev instance against a dev datafeed.
4. Deploy (Park's OK): datafeed first, then this repo.

## 5. Decisions

1. Source: **OKX `XAU-USDT-SWAP`** (Park, 2026-11-05).
2. Fee: **still open for Park.** There is no OKX account to observe a rate, and
   OKX's fee page redirected to the OKX US spot schedule on 2026-11-05, so no
   perpetual rate could be read. Until Park sets one, OKX Paper keeps exactly the
   current Paper rates (maker 0, taker 0.04%, from the Binance demo account), so
   the switch changes the venue and nothing else. It is an explicit
   `paper_assumption` with `verified: false` and `real_money_eligible: false`.
