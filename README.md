# Trading Platform

Everything behind **trade.park-ai-intel.com/trade** lives in this one repository:
the trading console (GridMind), the desk that wraps it, the broker adapters, the
strategy engine and the K-line widget. Clone it, run one script, open `/trade`.

```
in   Hyperliquid Testnet (public account reads, K-line bars, optional signed orders)
     Binance XAUUSDT perpetual bars (gold paper track)
     optional: a news service on :8001, daily newsletter HTML files, Telegram/Feishu
out  http://127.0.0.1:8790/trade   交易：GridMind console + news rail + judgment/execution panel
     http://127.0.0.1:8790/desk    日报 / 系统：morning brief, K-line cards, health, execution log
     outputs/                      every receipt, ledger, read model and boot gate, append-only JSON

fail no pre-deploy receipt          → dashboard refuses to boot (exit 79); run `make gate`
fail dashboard down                 → desk /trade shows the reason; /desk still works
fail news service absent            → news rail shows "no source"; nothing else changes
fail Testnet key file absent        → everything is read-only; execute buttons stay disabled
fail preview digest ≠ confirm       → order path rejects; no retry, no auto-next-plan
```

## What is in the box

| Path | What | Talks to |
|---|---|---|
| `pipelines/dashboard_server.py` + `services/` | **GridMind** trading console and control plane (`:8765`). Venue → instrument → strategy → preview → confirm & run. Stdlib HTTP server. | `packages/*` |
| `apps/trading-desk` | **交易台** (`:8790`). Same-origin proxy of GridMind at `/trade` behind a passcode gate, plus judgments, plans, 72-hour review, daily cards. FastAPI. | dashboard, news, Hyperliquid public API |
| `packages/standard-broker` | Provider-neutral broker ports and fail-closed adapters (Hyperliquid Testnet via Nautilus, read-only Mainnet observer). | exchange |
| `packages/trading-strategy` | Engine-neutral canonical DCA / Grid planner: deterministic plans, previews, replays, receipts. | nothing |
| `packages/standard-kline` | OHLCV candlestick widget over TradingView Lightweight Charts, served to the console. | browser |
| `configs/` | Strategy, risk, pipeline and asset YAML (JSON-compatible; stdlib only). | |
| `deploy/local` | launchd + Cloudflare Tunnel templates that reproduce the production setup on a Mac. | |
| `docs/` | Specs, ADRs, runbooks, evidence. Start with `docs/north-star.md` and `docs/runbooks/`. | |

```
browser ──▶ trading-desk :8790 ──/trade──▶ dashboard_server :8765 ──▶ services/ ──▶ standard-broker ──▶ Hyperliquid Testnet
                │                                │                        │
                │ desk.db (judgments, plans)      │ outputs/ (receipts)    └── trading-strategy (plans, previews)
                └── optional news :8001           └── data/market_data.db
```

## Quick start

Requirements: Python 3.13+, `make`, git. Node is optional (only for the K-line widget's own tests).

```bash
git clone https://github.com/zinan92/trading-system.git
cd trading-system
scripts/bootstrap.sh          # venv, deps, all test suites, boot receipt
make dashboard                # terminal 1 → http://127.0.0.1:8765
make desk                     # terminal 2 → http://127.0.0.1:8790/trade
```

`scripts/bootstrap.sh --with-nautilus` additionally builds an isolated venv with
`nautilus_trader`, which is only needed to **send** Testnet orders or run the
paper ledger. Without it the whole product runs read-only.

Configuration is one file: copy `.env.example` to `.env` (bootstrap does this)
and edit. Every path and port has a default inside the repo, so the only thing
you must add to trade on Testnet is your own account address and a `0600` key
file.

## Boundaries

- **Testnet and Paper only.** Mainnet is a read-only observer with a loss
  monitor; there is no code path that places a real-money order. The North Star
  (`NORTH_STAR.md`) says any live capability is a separate, explicitly approved
  destination.
- **Every order is preview → confirm → run, digest-bound, once.** Timers, page
  loads and refreshes never place orders. The desk only calls the dashboard's
  proven control path; it does not talk to a broker itself.
- **Fail closed.** Missing receipt, stale account facts, unknown fills, a
  changed source tree: each blocks rather than guesses. Read `docs/runbooks/`
  before "fixing" a blocker.
- **Secrets never enter git.** `.gitignore` + gitleaks in `make gitleaks`;
  keys live in `0600` files referenced from `.env`.

## Day-to-day

```bash
make test           # root, desk, broker, strategy, kline suites
make gate           # refresh the paper pre-deploy receipt after pulling
make gitleaks       # before every commit
```

Testnet execution replay and mutation gates: `scripts/testnet_replay.sh`,
`scripts/testnet_replay_mutations.sh`.

## Deploy

`deploy/local/README.md` reproduces production: two launchd agents and one
Cloudflare Tunnel that exposes only the desk. `deploy/cloud/` is the retired
Linux/systemd variant, kept as reference.

## Optional upstreams

Separate repositories the production instance also runs; the platform degrades
gracefully without them.

| Service | Repo | Used by |
|---|---|---|
| News feed on `:8001` | [zinan92/intel](https://github.com/zinan92/intel) | desk news rail |
| Market data on `:8100` | [zinan92/datafeed](https://github.com/zinan92/datafeed) | cloud paper preflight only |
| Morning brief / K-line newsletters | [zinan92/park-morning](https://github.com/zinan92/park-morning), [zinan92/daily-newsletter](https://github.com/zinan92/daily-newsletter) | `/desk#news` cards |

## Project records

`REGISTRY.md` is where the project is now and what is next. `decision-log.md`
holds the reasons and the traps. `NORTH_STAR.md` holds the destination. The
original gold-pipeline documentation moved to `docs/gold-pipeline-readme.md`.

## License

Private repository. `packages/standard-kline` is MIT.
