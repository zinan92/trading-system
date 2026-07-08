# Trading Orchestrator

Gold-first MVP for the Trading OS eight-stage pipeline:

```text
kline -> intel -> signal -> copilot -> backtest -> risk -> local paper executor -> journal
```

This first version focuses on the product loop:

- read a gold-first watchlist (GOLD tradable, DXY / real yield / GLD flow / Fed-CPI event factors)
- fetch GOLD 5m snapshots from the local market store plus `gold-api.com` live XAU price; intraday history can be backfilled from Yahoo `GC=F` as a clearly flagged futures proxy
- clean raw bars into auditable datasets
- generate normalized gold intraday 5m signals
- route each signal through a methodology analysis layer
- attach backtest-style evidence
- create risk-aware manual trade tickets
- create local paper orders after an executed_paper decision
- seed journal pending records
- write a daily briefing, daily report, and strategy review notes

## Run

Collect a live GOLD 5m snapshot into the local SQLite market database:

```bash
python3 -m pipelines.collect --date 2026-05-26
```

Backfill several days of GOLD 5m history into the same local SQLite database:

```bash
python3 -m pipelines.backfill_gold --date 2026-05-26 --yahoo-symbol 'GC=F' --range 5d
```

This writes `GOLD` 5m bars with provider `yahoo_chart:GC=F` and quality flags `historical_5m,futures_proxy`. It is useful for local intraday context and strategy development, while the latest spot snapshot remains `gold-api.com` when reachable.

You can also import broker, MT5, or TradingView exported XAUUSD 5m CSV:

```bash
python3 -m pipelines.import_bars /path/to/XAUUSD_5m.csv --symbol GOLD --timeframe 5m --provider broker_csv --date 2026-05-26
```

For a broker/MT5 feed bridge, drop exported XAUUSD 5m CSV files into:

```text
data/broker_feeds/gold_5m/
```

Then import every new file into the local SQLite market database:

```bash
python3 -m pipelines.broker_feed_doctor --date 2026-05-26
python3 -m pipelines.data_gap_doctor --date 2026-05-26
python3 -m pipelines.data_gap_repair_request --date 2026-05-26
python3 -m pipelines.broker_feed --date 2026-05-26
python3 -m pipelines.broker_feed_smoke --date 2026-05-26
```

The expected CSV headers are:

```text
timestamp,open,high,low,close,volume
```

`timestamp`, `datetime`, `time`, or `date` are accepted for the time column. The default bridge provider is `mt5_csv`, which is treated as an official broker feed candidate by data-source preflight.

Check whether the current local GOLD 5m data is paper-only public data or official broker data:

```bash
python3 -m pipelines.data_source_preflight --date 2026-05-26
```

For a lightweight live loop, keep it running on a five-minute cadence:

```bash
python3 -m pipelines.collect --date 2026-05-26 --iterations 12 --interval-seconds 300
```

Run the full daily trading pipeline:

```bash
python3 -m pipelines.daily --date 2026-05-07
```

Or run one complete Bot cycle: collect data, generate signal/ticket, optionally execute the first paper ticket, and write the daily review.

```bash
python3 -m pipelines.bot --date 2026-05-26 --paper-auto-approve
```

Run the local bot runner on a five-minute cadence and write heartbeat status:

```bash
python3 -m pipelines.runner --date 2026-05-26 --paper-auto-approve --iterations 0 --interval-seconds 300
```

For a single runner cycle during testing:

```bash
python3 -m pipelines.runner --date 2026-05-26 --paper-auto-approve --iterations 1
```

Outputs:

```text
outputs/daily_briefings/YYYY-MM-DD.md
outputs/raw_snapshots/YYYY-MM-DD/*
outputs/clean_bars/YYYY-MM-DD/*
outputs/signals/YYYY-MM-DD.json
outputs/analyses/YYYY-MM-DD.json
outputs/backtests/YYYY-MM-DD.json
outputs/trade_tickets/YYYY-MM-DD.json
outputs/paper_orders/YYYY-MM-DD.json
outputs/paper_positions/current.json
outputs/journal_pending/YYYY-MM-DD.json
outputs/journals/YYYY-MM-DD.md
outputs/reports/YYYY-MM-DD.md
outputs/review_notes/YYYY-MM-DD.md
outputs/collector_runs/YYYY-MM-DD.json
outputs/backfills/YYYY-MM-DD.json
outputs/imports/YYYY-MM-DD.json
outputs/broker_feed_doctor/current.json
outputs/broker_feed_imports/YYYY-MM-DD.json
outputs/broker_feed_smoke/current.json
outputs/data_source_preflight/current.json
outputs/data_gaps/current.json
outputs/data_gap_repair_requests/YYYY-MM-DD.md
outputs/data_gap_repair_requests/current.json
outputs/runner_status/current.json
outputs/runner_status/YYYY-MM-DD.json
```

## Record Manual Decision

After reviewing a ticket, mark it as executed_paper, skipped, or rejected:

```bash
python3 -m pipelines.decision \
  --date 2026-05-07 \
  --ticket-id ticket_gold_20260507_ef10f510b3 \
  --decision executed_paper \
  --notes "Approved local paper test."
```

This moves the item from:

```text
outputs/journal_pending/YYYY-MM-DD.json
```

to:

```text
outputs/journal_decisions/YYYY-MM-DD.json
```

`executed_paper` also writes:

```text
outputs/paper_orders/YYYY-MM-DD.json
outputs/paper_positions/current.json
```

Execution is routed through a broker adapter. The default config is:

```json
"execution_mode": "paper",
"live_trading_enabled": false
```

The live adapter is intentionally guarded and will refuse orders until a real broker integration is wired and explicitly enabled.

Run broker readiness checks and write a local preflight artifact:

```bash
python3 -m pipelines.broker_preflight
```

Outputs:

```text
outputs/broker_preflight/current.json
```

When `execution_mode` is switched to `live` and `live_trading_enabled` is explicitly true, the live adapter can record dry-run live order requests under:

```text
outputs/live_order_requests/YYYY-MM-DD.json
```

Dry-run requests are local artifacts only; they do not send real broker orders.

For a local MT5 bridge, set:

```json
"execution_mode": "live",
"live_trading_enabled": true,
"broker": {
  "provider": "mt5_file_bridge",
  "dry_run": true,
  "outbox_dir": "data/broker_outbox/mt5"
}
```

With `dry_run: true`, orders are written to the MT5 outbox as artifacts but should not be executed by an EA. With `dry_run: false`, the system writes `submitted_to_bridge` order files into the outbox; a separate MT5 EA or manual executor must consume those files. The Python bot still does not place broker orders directly.

The MT5 EA or manual bridge can write execution receipts into:

```text
data/broker_inbox/mt5/
```

Receipt JSON should include at least:

```json
{
  "order_id": "live_dryrun_...",
  "broker_order_id": "mt5-ticket-id",
  "status": "filled",
  "fill_price": 4572.5,
  "filled_quantity": 1.2,
  "timestamp": "2026-05-26T01:00:00Z"
}
```

Import receipts manually:

```bash
python3 -m pipelines.broker_receipts --date 2026-05-26
```

The runner and doctor also import pending receipts automatically. Imported receipts are stored under:

```text
outputs/broker_receipts/current.json
outputs/broker_receipts/YYYY-MM-DD.json
```

Run a local-only MT5 bridge smoke test:

```bash
python3 -m pipelines.mt5_bridge_smoke --date 2026-05-26
```

This uses `data/broker_bridge_smoke/`, writes one dry-run MT5 outbox order, creates a mock receipt, imports it, and records:

```text
outputs/mt5_bridge_smoke/current.json
outputs/mt5_bridge_smoke/YYYY-MM-DD.json
```

It is an integration smoke test for the file bridge path; it does not contact MT5 or place a real order.

## Health Check

Run a local end-to-end artifact health check:

```bash
python3 -m pipelines.health --date 2026-05-26
```

Outputs:

```text
outputs/health/current.json
outputs/health/YYYY-MM-DD.json
```

The dashboard shows this status in the System Health panel.

Run the one-command system doctor before or after a trading session:

```bash
python3 -m pipelines.doctor --date 2026-05-26
```

For the full JSON payload:

```bash
python3 -m pipelines.doctor --date 2026-05-26 --json
```

Outputs:

```text
outputs/doctor/current.json
outputs/doctor/YYYY-MM-DD.json
```

Doctor refreshes broker preflight, data-source preflight, health, and completion audit, then returns the current readiness state plus concrete next actions. A `warn` state is expected when the system is paper-ready but still lacks an official broker/MT5 market-data feed.

## Daily Review

Generate a compact daily report, human-readable Trading Journal, and self-review notes from signals, tickets, pending items, manual decisions, and paper PnL:

```bash
python3 -m pipelines.review --date 2026-05-07
```

Outputs:

```text
outputs/reports/YYYY-MM-DD.md
outputs/journals/YYYY-MM-DD.md
outputs/review_notes/YYYY-MM-DD.md
```

## Dashboard

Serve the project root with the local dashboard API and open the dashboard:

```bash
python3 -m pipelines.dashboard_server --host 127.0.0.1 --port 8765
```

Then visit:

```text
http://127.0.0.1:8765/dashboard-v4.html
```

The API endpoint behind the page is:

```text
http://127.0.0.1:8765/api/dashboard?date=YYYY-MM-DD
```

The dashboard auto-refreshes every 30 seconds and displays the latest GOLD provider plus quality flags, open paper trades, risk envelope, journal feed, and local database coverage. The first bootstrapped 5m history may include `local_synthetic_seed` rows anchored to the live XAU price; the latest bar should show `gold-api.com` when the live API is reachable. Historical coverage may include `yahoo_chart:GC=F`, which is a futures proxy and is shown separately in the Data Coverage panel.

## Strategy Config

The active gold intraday strategy is configured in:

```text
configs/strategy.yaml
```

It controls the 5m MA windows, signal thresholds, factor weights, local backtest stop/target model, max holding bars, and verdict thresholds. The dashboard and daily reports surface these values so each journal entry can be reviewed against the exact rules used that day.

## Notes

The `.yaml` config files are JSON-compatible YAML, so the MVP can run with only the Python standard library. Replace the config loader with PyYAML later if richer YAML syntax is needed.
