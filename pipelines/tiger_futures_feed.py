from __future__ import annotations

import argparse
import json

from services.run_date import utc_run_date
from services.tiger_futures_feed import run_tiger_futures_backfill, run_tiger_futures_feed_import


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

    overrides = {}
    if args.contract:
        overrides["contract"] = args.contract
        overrides.setdefault("output_symbol", args.contract)
    if args.output_symbol:
        overrides["output_symbol"] = args.output_symbol
    if args.backfill_start:
        if not args.backfill_end:
            parser.error("--backfill-end is required when --backfill-start is set")
        result = run_tiger_futures_backfill(
            args.backfill_start,
            args.backfill_end,
            config=overrides,
            total=args.backfill_total,
            page_size=args.page_size,
            time_interval=args.time_interval,
        )
    else:
        result = run_tiger_futures_feed_import(args.date, config=overrides, limit=args.limit)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
