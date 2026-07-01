from __future__ import annotations

import argparse
import json
from datetime import date
from services.run_date import utc_run_date

from services.official_feed_onboarding import OfficialFeedOnboarding


def main() -> None:
    parser = argparse.ArgumentParser(description="Create the local runbook for connecting official XAUUSD 5m broker data.")
    parser.add_argument("--date", default=utc_run_date())
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    result = OfficialFeedOnboarding().build(args.date)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return
    print(f"official_feed_onboarding: {result['status']} date={args.date} official_rows={result['current_official_rows']}")
    print(f"feed_dir: {result['feed_dir']}")
    print(f"template: {result['template']}")
    print(f"next_action: {result['next_action']}")
    for command in result["commands"]:
        print(f"- {command}")


if __name__ == "__main__":
    main()
