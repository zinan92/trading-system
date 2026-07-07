from __future__ import annotations

import argparse
import json

from services.run_date import utc_run_date
from services.tiger_openapi_account_sync import TigerOpenApiAccountSync


def main() -> None:
    parser = argparse.ArgumentParser(description="Run read-only Tiger OpenAPI paper account/balance sync.")
    parser.add_argument("--date", default=utc_run_date())
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = TigerOpenApiAccountSync().run(args.date)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        balance = result.get("exchange_balance", {}) if isinstance(result.get("exchange_balance"), dict) else {}
        accounting = result.get("exchange_accounting", {}) if isinstance(result.get("exchange_accounting"), dict) else {}
        print(
            "tiger_openapi_account_sync: "
            f"{result['sync_status']} date={result['run_date']} "
            f"balance_present={balance.get('balance_present')} "
            f"realized_pnl={accounting.get('net_realized_pnl_estimate')}"
        )


if __name__ == "__main__":
    main()
