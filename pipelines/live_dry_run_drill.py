from __future__ import annotations

import argparse
import json
from datetime import date

from services.live_dry_run_drill import LiveDryRunDrill


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a consolidated live dry-run drill for the GOLD 5m Trading Bot.")
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--json", action="store_true", help="Print the full JSON payload.")
    parser.add_argument("--no-refresh", action="store_true", help="Use existing artifacts instead of refreshing dependent gates.")
    args = parser.parse_args()

    result = LiveDryRunDrill().run(args.date, refresh_dependencies=not args.no_refresh)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return
    print(f"live_dry_run_drill: {result['status']} date={result['run_date']}")
    print(f"dry_run_ready={result['dry_run_ready']} real_money_ready={result['real_money_ready']} safe_to_submit_live_order={result['safe_to_submit_live_order']}")
    print(f"data_truth={result['data_truth_level']} latest={result.get('latest_price')} provider={result.get('latest_provider')}")
    for item in result["blockers"][:6]:
        print(f"- {item['name']}: {item['summary']}")


if __name__ == "__main__":
    main()
