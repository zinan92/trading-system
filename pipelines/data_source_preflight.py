from __future__ import annotations

import argparse
import json
from datetime import date
from services.run_date import utc_run_date

from services.data_source_preflight import DataSourcePreflight


def main() -> None:
    parser = argparse.ArgumentParser(description="Check market data provenance and live-readiness.")
    parser.add_argument("--date", default=utc_run_date())
    parser.add_argument("--symbol", default="GOLD", help="Local market-data symbol to inspect, e.g. GOLD or MGCmain.")
    parser.add_argument("--timeframe", default="5m", help="Local bar timeframe to inspect, e.g. 5m or 1m.")
    args = parser.parse_args()

    print(
        json.dumps(
            DataSourcePreflight(symbol=args.symbol, timeframe=args.timeframe).run(args.date),
            indent=2,
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
