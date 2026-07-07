from __future__ import annotations

import argparse
import json
from datetime import date
from services.run_date import utc_run_date

from pathlib import Path

from services.schedule_installer import (
    SCHEDULE_INSTALL_ACKNOWLEDGEMENT,
    SCHEDULE_ROLLBACK_ACKNOWLEDGEMENT,
    ScheduleInstaller,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Install generated Trading Orchestrator launchd jobs into ~/Library/LaunchAgents.")
    parser.add_argument("--date", default=utc_run_date())
    parser.add_argument("--rollback", action="store_true", help="Restore LaunchAgent plists from a prior install backup receipt.")
    parser.add_argument("--receipt", default="", help="Install receipt path to use for rollback. Defaults to outputs/schedules/install_current.json.")
    parser.add_argument("--dry-run", action="store_true", help="Write an install plan without copying plists or modifying launchd.")
    parser.add_argument("--no-restart-loaded", action="store_true", help="Do not bootout existing loaded jobs before bootstrap.")
    parser.add_argument(
        "--acknowledgement",
        default="",
        help=f"Required for non-dry-run install or rollback: {SCHEDULE_INSTALL_ACKNOWLEDGEMENT} / {SCHEDULE_ROLLBACK_ACKNOWLEDGEMENT}",
    )
    parser.add_argument("--package-id", default="", help="Required for non-dry-run install when generated jobs need replacement.")
    parser.add_argument("--json", action="store_true", help="Print the full JSON payload.")
    args = parser.parse_args()

    installer = ScheduleInstaller()
    receipt_path = Path(args.receipt) if args.receipt else None
    if args.rollback:
        result = (
            installer.rollback_plan(args.date, receipt_path=receipt_path, restart_loaded=not args.no_restart_loaded)
            if args.dry_run
            else installer.rollback(args.date, receipt_path=receipt_path, restart_loaded=not args.no_restart_loaded, acknowledgement=args.acknowledgement)
        )
    else:
        result = (
            installer.plan(args.date, restart_loaded=not args.no_restart_loaded)
            if args.dry_run
            else installer.install(
                args.date,
                restart_loaded=not args.no_restart_loaded,
                acknowledgement=args.acknowledgement,
                package_id=args.package_id,
            )
        )
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return
    status = result.get("schedule_status", {})
    if args.rollback:
        command = "schedule_rollback_plan" if args.dry_run else "schedule_rollback"
    else:
        command = "schedule_install_plan" if args.dry_run else "schedule_install"
    print(f"{command}: {result['status']} date={result['run_date']}")
    if result.get("blocker"):
        print(f"blocker={result['blocker']} required_acknowledgement={result.get('required_acknowledgement', '')}")
    print(f"installed={status.get('installed_count', 0)}/{status.get('required_count', 3)} loaded={status.get('loaded_count', 0)}/{status.get('required_count', 3)}")
    print(f"matches_current={status.get('matching_generated_count', 0)}/{status.get('required_count', 3)} active_current={status.get('active_current_count', 0)}/{status.get('required_count', 3)}")
    for job in result["jobs"]:
        print(f"- {job['label']}: {job.get('status') or job.get('action')}")


if __name__ == "__main__":
    main()
