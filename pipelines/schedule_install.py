from __future__ import annotations

import argparse
import json
from datetime import date

from services.schedule_installer import ScheduleInstaller


def main() -> None:
    parser = argparse.ArgumentParser(description="Install generated Trading Orchestrator launchd jobs into ~/Library/LaunchAgents.")
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--no-restart-loaded", action="store_true", help="Do not bootout existing loaded jobs before bootstrap.")
    parser.add_argument("--json", action="store_true", help="Print the full JSON payload.")
    args = parser.parse_args()

    result = ScheduleInstaller().install(args.date, restart_loaded=not args.no_restart_loaded)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return
    status = result.get("schedule_status", {})
    print(f"schedule_install: {result['status']} date={result['run_date']}")
    print(f"installed={status.get('installed_count', 0)}/{status.get('required_count', 3)} loaded={status.get('loaded_count', 0)}/{status.get('required_count', 3)}")
    for job in result["jobs"]:
        print(f"- {job['label']}: {job['status']}")


if __name__ == "__main__":
    main()
