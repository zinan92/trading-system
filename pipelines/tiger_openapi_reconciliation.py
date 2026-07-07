from __future__ import annotations

import argparse
import json

from services.run_date import utc_run_date
from services.tiger_openapi_reconciliation import TigerOpenApiPaperReconciliation


def main() -> None:
    parser = argparse.ArgumentParser(description="Run read-only Tiger OpenAPI paper reconciliation.")
    parser.add_argument("--date", default=utc_run_date())
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = TigerOpenApiPaperReconciliation().run(args.date)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(
            "tiger_openapi_reconciliation: "
            f"{result['confirmation_status']} date={result['run_date']} "
            f"drifts={result['drift_count']} positions={len(result['exchange_positions'])} "
            f"open_orders={len(result['exchange_open_orders'])}"
        )


if __name__ == "__main__":
    main()
