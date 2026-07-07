from __future__ import annotations

import argparse
import json

from services.run_date import utc_run_date
from services.tiger_price_feed_readiness import TigerPriceFeedReadiness


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate artifact-only Tiger price-feed readiness.")
    parser.add_argument("--date", default=utc_run_date())
    parser.add_argument("--min-imported-rows", type=int, default=500)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    result = TigerPriceFeedReadiness(min_imported_rows=args.min_imported_rows).run(args.date)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(
            "tiger_price_feed_readiness: "
            f"{result['status']} date={result['run_date']} "
            f"blockers={len(result['blockers'])} "
            f"can_enable_broker_orders_from_this_gate={result['can_enable_broker_orders_from_this_gate']}"
        )


if __name__ == "__main__":
    main()
