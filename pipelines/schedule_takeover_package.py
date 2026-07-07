from __future__ import annotations

import argparse
import json

from services.run_date import utc_run_date
from services.schedule_takeover_package import ScheduleTakeoverPackage


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a no-write package for attended schedule takeover.")
    parser.add_argument("--date", default=utc_run_date())
    parser.add_argument("--check-current", action="store_true", help="Validate the current takeover package without creating a new one.")
    parser.add_argument("--json", action="store_true", help="Print the full JSON payload.")
    args = parser.parse_args()

    service = ScheduleTakeoverPackage()
    result = service.check_current(args.date) if args.check_current else service.run(args.date)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return
    command_name = "schedule_takeover_package_check" if args.check_current else "schedule_takeover_package"
    print(f"{command_name}: {result['status']} date={result['run_date']}")
    if result.get("blocker"):
        print(f"blocker={result['blocker']}")
    if result.get("operator_next_action"):
        print(f"next_action={result['operator_next_action'].get('action')}")
    for name, command in result.get("commands", {}).items():
        if command:
            print(f"- {name}: {command}")


if __name__ == "__main__":
    main()
