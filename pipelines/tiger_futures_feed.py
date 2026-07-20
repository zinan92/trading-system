from __future__ import annotations

import argparse
import json

from services.run_date import utc_run_date
from services.market_data_refresh import refresh_market_data, refresh_market_data_range


def main() -> None:
    parser = argparse.ArgumentParser(description="Import read-only Tiger OpenAPI COMEX futures bars into the local market database.")
    parser.add_argument("--date", default=utc_run_date())
    parser.add_argument("--contract", help="Tiger futures contract identifier, e.g. MGCmain, MGC2608, 1OZmain, 1OZ2608.")
    parser.add_argument("--output-symbol", help="Symbol to store in the local market database. Defaults to the contract.")
    parser.add_argument("--limit", type=int, help="Number of recent bars to request from Tiger OpenAPI.")
    parser.add_argument("--backfill-start", help="ISO datetime (UTC) to start a historical backfill.")
    parser.add_argument("--backfill-end", help="ISO datetime (UTC) to end a historical backfill.")
    parser.add_argument("--backfill-total", type=int, help="Maximum bars to request while paging.")
    parser.add_argument("--page-size", type=int, help="Tiger page size for historical backfill.")
    parser.add_argument("--time-interval", type=float, default=0, help="Seconds to wait between Tiger historical pages.")
    args = parser.parse_args()

    symbol = args.output_symbol or args.contract or "MGCmain"
    if args.backfill_start:
        if not args.backfill_end:
            parser.error("--backfill-end is required when --backfill-start is set")
        result = refresh_market_data_range(
            run_date=args.date,
            symbol=symbol,
            timeframe="1m",
            output_kind="tiger_futures_backfill",
            start=args.backfill_start,
            end=args.backfill_end,
            chunk_days=30,
        )
    else:
        result = refresh_market_data(
            run_date=args.date,
            symbol=symbol,
            timeframe="1m",
            output_kind="tiger_futures_feed",
        )
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
