from __future__ import annotations

import argparse
import json
from datetime import date
from services.run_date import utc_run_date

from services.binance_futures_feed import run_binance_usdm_1m_feed_import
from services.multi_strategy_runner import MultiStrategyRunner


def refresh_1m_feed(run_date: str) -> dict:
    """Import the most recent GOLD 1m klines so the chan strategy signals on the
    current structure instead of a frozen backfill tail. Best-effort: a feed
    error (offline, rate limit, API down) is recorded and swallowed so it never
    blocks the strategy run — the strategies launchd job runs on Python 3.13,
    where the pure-stdlib feed imports cleanly alongside the chan core."""
    try:
        return run_binance_usdm_1m_feed_import(run_date)
    except Exception as exc:  # noqa: BLE001 — never let a feed hiccup kill the fleet
        return {"status": "error", "message": f"{type(exc).__name__}: {exc}"}


def main() -> None:
    parser = argparse.ArgumentParser(description="Run every enabled strategy in its own isolated paper namespace (outputs/strategies/<id>/).")
    parser.add_argument("--date", default=utc_run_date())
    parser.add_argument("--paper-auto-approve", action="store_true", help="Auto-execute each strategy's first pending ticket in its own paper account.")
    parser.add_argument("--skip-feed-refresh", action="store_true", help="Skip the live 1m feed refresh (e.g. offline replay of historical dates).")
    args = parser.parse_args()

    if not args.skip_feed_refresh:
        feed = refresh_1m_feed(args.date)
        print(json.dumps({"feed_1m_refresh": feed.get("status"), "imported_rows": feed.get("imported_rows")}, ensure_ascii=False))

    summary = MultiStrategyRunner().run(args.date, paper_auto_approve=args.paper_auto_approve)
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
