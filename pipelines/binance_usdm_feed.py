from __future__ import annotations

import argparse
import json
from datetime import date

from services.binance_futures_feed import run_binance_usdm_backfill, run_binance_usdm_feed_import


def main() -> None:
    parser = argparse.ArgumentParser(description="Import Binance USDM XAUUSDT 5m futures candles into the local market database.")
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--backfill-start", help="ISO datetime (UTC) to start a historical backfill, e.g. 2026-05-19T00:00:00.")
    parser.add_argument("--backfill-end", help="ISO datetime (UTC) to end the backfill (default: now).")
    parser.add_argument(
        "--replace-provider",
        action="append",
        default=[],
        help="Delete this provider's GOLD 5m rows before backfilling (repeatable), e.g. --replace-provider yahoo_chart:GC=F.",
    )
    args = parser.parse_args()

    if args.backfill_start:
        from datetime import datetime, timezone

        end = args.backfill_end or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        result = run_binance_usdm_backfill(args.backfill_start, end, replace_providers=args.replace_provider)
    else:
        result = run_binance_usdm_feed_import(args.date)

    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
