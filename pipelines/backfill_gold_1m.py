from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from services.run_date import utc_run_date

from services.market_data_refresh import refresh_market_data, refresh_market_data_range


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill GOLD 1m history (Binance XAUUSDT perpetual) into the local market DB for the chan strategy."
    )
    parser.add_argument(
        "--start",
        default="2025-12-11T00:00:00+00:00",
        help="ISO datetime start. Defaults to the XAUUSDT listing date (~2025-12-11).",
    )
    parser.add_argument(
        "--end",
        default=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        help="ISO datetime end. Defaults to now (UTC).",
    )
    parser.add_argument(
        "--refresh-only",
        action="store_true",
        help="Skip the historical backfill; just import the most recent 1m klines (live tail refresh).",
    )
    args = parser.parse_args()

    if args.refresh_only:
        result = refresh_market_data(run_date=utc_run_date(), symbol="GOLD", timeframe="1m", output_kind="binance_usdm_1m_feed")
    else:
        result = refresh_market_data_range(run_date=args.start[:10], symbol="GOLD", timeframe="1m", output_kind="binance_usdm_1m_backfill", start=args.start, end=args.end, chunk_days=7)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
