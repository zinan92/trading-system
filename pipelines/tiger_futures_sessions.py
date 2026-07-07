from __future__ import annotations

import argparse
import json

from services.run_date import utc_run_date
from services.tiger_futures_feed import run_tiger_futures_sessions


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch read-only Tiger OpenAPI futures trading sessions.")
    parser.add_argument("--date", default=utc_run_date(), help="Run/artifact date.")
    parser.add_argument("--trading-date", help="Exchange trading date to query. Defaults to --date.")
    parser.add_argument("--contract", help="Tiger futures contract identifier, e.g. MGC2608 or 1OZ2608.")
    args = parser.parse_args()

    overrides = {}
    if args.contract:
        overrides["contract"] = args.contract
        overrides["output_symbol"] = args.contract
    result = run_tiger_futures_sessions(args.date, trading_date=args.trading_date, config=overrides)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
