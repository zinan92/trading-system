from __future__ import annotations

import argparse
import json
from datetime import date

from services.mock_trading_uat import run_mock_trading_uat


def main() -> None:
    parser = argparse.ArgumentParser(description="Build Mock Trading UAT evidence for the local paper loop.")
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--json", action="store_true", help="Print the full JSON payload.")
    args = parser.parse_args()

    result = run_mock_trading_uat(args.date)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return
    summary = result["summary"]
    print(f"mock_uat: {result['status']} date={result['run_date']}")
    print(f"passed={summary['passed']} warned={summary['warned']} failed={summary['failed']}")
    print("next_actions:")
    for item in result["next_actions"]:
        print(f"- {item}")


if __name__ == "__main__":
    main()
