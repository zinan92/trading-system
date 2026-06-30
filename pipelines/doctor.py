from __future__ import annotations

import argparse
import json
from datetime import date

from services.system_doctor import SystemDoctor


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a one-command readiness check for the GOLD Trading Bot.")
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--json", action="store_true", help="Print the full JSON payload.")
    args = parser.parse_args()

    result = SystemDoctor().run(args.date)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return
    summary = result["summary"]
    print(f"doctor: {result['status']} date={result['run_date']}")
    print(f"health={summary['health']} audit={summary['audit']} data_source={summary['data_source']} runner={summary['runner_state'] or 'missing'}")
    print(f"latest={summary['latest_price']} provider={summary['latest_provider']} paper_ready={summary['paper_ready']} live_ready={summary['live_ready']}")
    print("next_actions:")
    for item in result["next_actions"]:
        print(f"- {item}")


if __name__ == "__main__":
    main()
