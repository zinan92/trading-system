# Maturity plan — 2026-11-05

Owner: Park. Written overnight by Claude on 2026-11-04/05 at Park's request:
"make this a more mature and production ready system … 不要做到看起来加了还不如不加".

This plan sets the order of the overnight work. Every PR links back here.
It does not change `NORTH_STAR.md`: Paper/Testnet-first stays, and nothing
here places, enables, or deploys a real-money order.

## 1. Diagnosis (measured 2026-11-04 23:40–2026-11-05 00:30 Asia/Shanghai)

| # | Finding | Evidence | Effect |
|---|---|---|---|
| D1 | **Gold has one market source, and that source is blocked from this machine.** Binance answers HTTP 451 ("restricted location") for `fapi` and `api` alike. | `curl fapi.binance.com/fapi/v1/klines` → 451; datafeed `:8100` → 502 `upstream_error … 451`; dashboard `/api/ops/status` → `contract_failed` | `/trade` shows "读取中" everywhere, no K-line, "NO LIVE KLINE DATA". Fail-closed is correct; the product is still unusable. |
| D2 | **Other venues are reachable from this machine and list gold.** | OKX `XAU-USDT-SWAP` and `XAUT-USDT` 200; Bitget `XAUTUSDT` 200; Kraken `PAXGUSD` 200; Hyperliquid mainnet `xyz:GOLD` (HIP-3) 200; Bybit 403 (CloudFront geo-block) | A second source is a config choice, not a research project. |
| D3 | **Test suites are not green**, and a fresh clone cannot run them. | `requirements.txt` misses `scikit-learn`; root suite collection errors in a clean venv. trading-strategy: 2 failures from hard-coded `/Users/wendy/work/trading-system-testnet` (deleted in #1284). Root: 30 failures under the system Python (clock-dependent fixtures and contract drift; detail in the PR). | The "make test is green" promise in the README is false. |
| D4 | **A 7-week-old runtime record is shown as current state.** | `/api/dashboard-control/runtime-status` returns a 2026-09-15 BTC Testnet grid (`grid_terminal`, blocker `position_open_unprotected`) while the account read shows 0 positions and the selection is XAU Paper DCA. | The page says "position open, unprotected" while there is no position. A trader can't trust that page. |
| D5 | **The control track crashes in the browser.** | `/trade` shows "控制轨道暂不可用：Cannot read properties of null (reading 'direction')" | Venue/instrument/strategy panel unusable. |
| D6 | **Paper runtime retries a dead window forever.** | `runtime_blockers.jsonl`: `recording_package_blocked` for window `2026-09-14_NIGHT`, ~600 rows/day | Noise that hides real blockers; disk growth. |
| D7 | **The desk's hourly futures watch times out every run.** | `~/Library/Logs/TradingDesk/watch.log`: `"futures": {"ok": false, "reason": "TimeoutError"}` | Same root cause as D1 (Binance), so it is silent. |
| D8 | **UI speaks engineer.** | `/trade` mixes "Strategy Shadows", "Nautilus 迁移门禁", "grid_terminal · position_open_unprotected", "Supervisor", raw exception text, a dozen separate "读取中" | It looks unfinished even when it works. |

What is fine: the desk (59 tests) and standard-broker (348 tests) are green; the
fail-closed gates behaved correctly during D1; secrets stay out of git; the
architecture already has ports and registries (see
`docs/trading-system-ports-and-adapters-audit-2026-07-18.md`).

**Main problem.** The system is not short of features. It is short of
**never showing wrong or stuck state**. A top-5% trading product earns trust by
showing correct data, labelling a stale source as stale, recovering by itself,
and keeping an audit trail of every action. More venues or strategies on top of D1–D8 would make it worse.

## 2. Order of work

Each step is one PR (or one per repo). Acceptance applies to every PR:
all five suites green (`make test`), a browser check on an isolated dev
instance (own ports, own `outputs/`, no keys, no Telegram/Feishu tokens),
`REGISTRY.md` + `decision-log.md` updated.

| Step | What | Acceptance |
|---|---|---|
| S1 | **Green baseline.** Complete `requirements*.txt` so a fresh clone runs `make test`; fix trading-strategy pinned-source paths to this repo; fix root failures by freezing time or updating drifted fixtures. **Do not loosen a fail-closed gate to make a test pass.** | `make test` green in a fresh `.venv`. |
| S2 | **Correct state on screen.** Runtime status older than its freshness budget, or from a different selection, is shown as *history*, never as the current blocker. Fix the control-track null crash. Stop retrying a recording window that can never complete (mark it `abandoned` with a receipt, once). | Dev `/trade` shows no false "position open"; control track renders; blocker log grows only on new events. |
| S3 | **Swappable market source (datafeed repo).** Add an OKX public market-data provider (crypto + `XAU-USDT-SWAP`) and a Bitget provider behind the existing `SourceManifest`/`MarketDataPort`. Each source has its own provenance and passes the same quality gate; **no silent failover**. The gold source is chosen in config. | Provider tests with recorded fixtures; live smoke from this machine; `/api/candles` schema unchanged. |
| S4 | **Gold source switch (this repo).** Make the paper gold instrument's market source a configured choice, with the venue and instrument shown on the chart and in every receipt. The default stays as today until Park picks. The switch to OKX ships but is not deployed. | Dev instance with source=OKX shows live gold bars, a fresh trust gate, and the source label. |
| S5 | **Strategy: Enhanced Beta.** Implement it in `packages/trading-strategy`, following Park's written rules (PARK-OS §三·交易): exposure between 0.8× and 1.2× of a base position, set by large-timeframe structure (3D/1D/4H); only rebalances against extreme moves; always keeps cash. Deterministic plan + replay over real bars + receipts, Paper only, registered beside Grid/DCA. | Golden tests; replay report over ≥1 year of BTC daily bars; it shows in the strategy family list on the dev instance. |
| S6 | **交易回看 (trade review).** Turn what already exists (12h review, NAV, judgments ledger, executions table, replay pages) into one review view: pick a window → K-line with every judgment, plan, fill, and exit drawn on it, with the outcome next to each. No new replay page beside the old ones; the old ones get retired. | Dev instance: a past Testnet grid and a past judgment are both visible on the chart with timestamps that match the ledgers. |
| S7 | **Polish by removing.** Plain-language state names; one degraded-state banner instead of a dozen "读取中"; engineering panels move to 系统; root-level screenshots / `dashboard-v2…v4` / `codex-task-*.md` move to `docs/archive/` only after grep shows nothing serves them. | Before/after screenshots; no link breaks. |

## 3. Out of scope tonight

- Any real-money capability, Mainnet order path, or change to the Mainnet observer.
- Deploying to production (`~/work/trading-platform`, launchd, `~/park-runtime/datafeed`). Production stays on `bbdcf90` until Park says deploy.
- Entering API keys, creating exchange accounts, Telegram/Feishu changes.
- Order-capable adapters for new exchanges. New venues are market-data only until one passes `packages/standard-broker` conformance on its testnet.

## 4. Decisions only Park can make (collected for the morning)

1. Which gold source replaces Binance XAUUSDT for Paper: OKX `XAU-USDT-SWAP` (closest to a gold perpetual), Hyperliquid `xyz:GOLD`, or fix the network route so Binance works again.
2. Whether to deploy the merged PRs to production.
