from __future__ import annotations

import argparse
import json

from services.run_date import utc_run_date
from services.tiger_openapi_order_sync import TigerOpenApiOrderSync


def main() -> None:
    parser = argparse.ArgumentParser(description="Run read-only Tiger OpenAPI paper order/fill sync.")
    parser.add_argument("--date", default=utc_run_date())
    parser.add_argument("--start-time", default=None)
    parser.add_argument("--end-time", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = TigerOpenApiOrderSync().run(args.date, start_time=args.start_time, end_time=args.end_time)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(
            "tiger_openapi_order_sync: "
            f"{result['sync_status']} date={result['run_date']} "
            f"open_orders={result['open_order_count']} filled_orders={result['filled_order_count']}"
        )


if __name__ == "__main__":
    main()
