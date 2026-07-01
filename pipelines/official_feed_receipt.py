from __future__ import annotations

import argparse
import json
from datetime import date
from services.run_date import utc_run_date

from services.official_feed_receipt import OfficialFeedReceipt


def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh the official XAUUSD 5m feed receipt without placing trades.")
    parser.add_argument("--date", default=utc_run_date())
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    result = OfficialFeedReceipt().refresh(args.date)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return

    print(
        f"official_feed_receipt: {result['status']} date={args.date} "
        f"official_rows={result['official_rows']} live={result['ready_for_live']} "
        f"truth={result['truth_level']}"
    )
    print(
        f"latest: {result.get('latest_price')} provider={result.get('latest_provider')} "
        f"timestamp={result.get('latest_timestamp')}"
    )
    for action in result.get("next_actions", []):
        print(f"- {action}")


if __name__ == "__main__":
    main()
