from __future__ import annotations

import argparse
import json
from datetime import date
from services.run_date import utc_run_date

from services.mock_runtime import MockTradingRuntime


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local Mock Trading runtime readiness check.")
    parser.add_argument("--date", default=utc_run_date())
    parser.add_argument("--json", action="store_true", help="Print the full JSON payload.")
    args = parser.parse_args()

    result = MockTradingRuntime().run(args.date)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return
    summary = result["summary"]
    print(f"mock_runtime: {result['status']} date={result['run_date']} mock_ready={result['mock_ready']} mock_running={result['mock_running']}")
    print(f"passed={summary['passed']} warned={summary['warned']} failed={summary['failed']}")
    print("next_actions:")
    for item in result["next_actions"]:
        print(f"- {item}")


if __name__ == "__main__":
    main()
