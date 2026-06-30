from __future__ import annotations

import argparse
import json
from datetime import date

from services.schedule_status import ScheduleStatus


def main() -> None:
    parser = argparse.ArgumentParser(description="Check whether generated launchd jobs are installed and loaded.")
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--json", action="store_true", help="Print the full JSON payload.")
    args = parser.parse_args()

    result = ScheduleStatus().run(args.date)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return
    print(f"schedule_status: {result['status']} date={result['run_date']}")
    print(f"installed={result['installed_count']}/{result['required_count']} loaded={result['loaded_count']}/{result['required_count']}")
    print(result["message"])
    if result.get("install_commands"):
        print("install commands are recorded in outputs/schedules/README.md")


if __name__ == "__main__":
    main()
