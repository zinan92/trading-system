from __future__ import annotations

import argparse
import json

from services.run_date import utc_run_date
from services.tiger_realtime_validation import run_tiger_realtime_validation


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate Tiger OpenAPI futures bar freshness during market hours.")
    parser.add_argument("--date", default=utc_run_date())
    parser.add_argument("--contract", help="Tiger futures contract identifier, e.g. MGCmain or MGC2608.")
    parser.add_argument("--output-symbol", help="Symbol label used in local validation output.")
    parser.add_argument("--trading-date", help="Tiger trading date to inspect. Defaults to around the validation timestamp.")
    parser.add_argument("--as-of", help="ISO timestamp for deterministic validation.")
    parser.add_argument("--poll-seconds", type=float, default=75.0)
    parser.add_argument("--max-lag-seconds", type=float, default=180.0)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument(
        "--require-market-hours-pass",
        action="store_true",
        help="Exit non-zero unless a market-hours validation run returns status=pass.",
    )
    args = parser.parse_args()

    overrides = {}
    if args.contract:
        overrides["contract"] = args.contract
        overrides.setdefault("output_symbol", args.contract)
    if args.output_symbol:
        overrides["output_symbol"] = args.output_symbol
    result = run_tiger_realtime_validation(
        args.date,
        config=overrides,
        as_of=args.as_of,
        trading_date=args.trading_date,
        poll_seconds=args.poll_seconds,
        max_lag_seconds=args.max_lag_seconds,
        limit=args.limit,
        require_market_hours_pass=args.require_market_hours_pass,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    raise SystemExit(int((result.get("market_hours_gate") or {}).get("exit_code", 0)))


if __name__ == "__main__":
    main()
