from __future__ import annotations

import argparse
import json
from services.run_date import utc_run_date

from services.market_data_refresh import refresh_market_data, refresh_market_data_range


def main() -> None:
    parser = argparse.ArgumentParser(description="Import Binance USDM XAUUSDT 5m futures candles into the local market database.")
    parser.add_argument("--date", default=utc_run_date())
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
        result = refresh_market_data_range(run_date=args.backfill_start[:10], symbol="GOLD", timeframe="5m", output_kind="binance_usdm_backfill", start=args.backfill_start, end=end, chunk_days=30)
    else:
        result = refresh_market_data(run_date=args.date, symbol="GOLD", timeframe="5m", output_kind="binance_usdm_feed")

    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
