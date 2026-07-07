from __future__ import annotations

import argparse
import json

from services.run_date import utc_run_date
from services.schedule_post_install_verifier import SchedulePostInstallVerifier


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify that generated launchd jobs were installed as the current active schedule.")
    parser.add_argument("--date", default=utc_run_date())
    parser.add_argument("--json", action="store_true", help="Print the full JSON payload.")
    args = parser.parse_args()

    result = SchedulePostInstallVerifier().run(args.date)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return
    print(f"schedule_post_install_verify: {result['status']} date={result['run_date']}")
    for check in result.get("checks", []):
        print(f"- {check['name']}: {check['status']} — {check['summary']}")


if __name__ == "__main__":
    main()
