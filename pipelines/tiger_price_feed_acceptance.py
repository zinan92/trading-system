from __future__ import annotations

import argparse
import json
from pathlib import Path

from services.run_date import utc_run_date
from services.tiger_price_feed_acceptance import TigerPriceFeedAcceptance


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one-command Tiger price-feed acceptance.")
    parser.add_argument("--date", default=utc_run_date())
    parser.add_argument("--contract", help="Tiger futures contract identifier, e.g. MGCmain or MGC2608.")
    parser.add_argument("--output-symbol", help="Symbol label used in local validation output.")
    parser.add_argument("--trading-date", help="Tiger trading date to inspect. Defaults to around validation timestamp.")
    parser.add_argument("--as-of", help="ISO timestamp for deterministic validation.")
    parser.add_argument("--poll-seconds", type=float, default=75.0)
    parser.add_argument("--max-lag-seconds", type=float, default=180.0)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--min-imported-rows", type=int, default=500)
    parser.add_argument("--output-root", default="", help="Override artifact output root.")
    parser.add_argument("--market-db", default="", help="Override local market DB path for acceptance runs.")
    parser.add_argument(
        "--plan-only",
        action="store_true",
        help="Write and print a local operator plan without opening Tiger QuoteClient or refreshing acceptance artifacts.",
    )
    parser.add_argument(
        "--skip-realtime-run",
        action="store_true",
        help="Do not open a quote client; aggregate the latest local realtime-validation artifact only.",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    service = TigerPriceFeedAcceptance(
        output_root=Path(args.output_root) if args.output_root else None,
        market_db=Path(args.market_db) if args.market_db else None,
    )
    if args.plan_only:
        result = service.plan(
            args.date,
            contract=args.contract,
            output_symbol=args.output_symbol,
            poll_seconds=args.poll_seconds,
            max_lag_seconds=args.max_lag_seconds,
            limit=args.limit,
            min_imported_rows=args.min_imported_rows,
        )
    else:
        result = service.run(
            args.date,
            contract=args.contract,
            output_symbol=args.output_symbol,
            run_realtime=not args.skip_realtime_run,
            as_of=args.as_of,
            trading_date=args.trading_date,
            poll_seconds=args.poll_seconds,
            max_lag_seconds=args.max_lag_seconds,
            limit=args.limit,
            min_imported_rows=args.min_imported_rows,
        )
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(
            "tiger_price_feed_acceptance: "
            f"{result['status']} date={result['run_date']} "
            f"exit_code={result['exit_code']} "
            f"ready_for_price_feed={result['ready_for_price_feed']} "
            f"blockers={len(result['blockers'])}"
        )
    raise SystemExit(int(result.get("exit_code", 2)))


if __name__ == "__main__":
    main()
