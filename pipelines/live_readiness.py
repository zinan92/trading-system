from __future__ import annotations

import argparse
import json
from datetime import date

from services.live_readiness import LiveReadiness


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the real-money live readiness gate for the GOLD 5m Trading Bot.")
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--json", action="store_true", help="Print the full JSON payload.")
    args = parser.parse_args()

    result = LiveReadiness().run(args.date)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return
    summary = result["summary"]
    print(f"live_readiness: {result['status']} date={result['run_date']} live_ready={result['live_ready']}")
    print(f"passed={summary['passed']} warned={summary['warned']} failed={summary['failed']}")
    print("next_actions:")
    for item in result["next_actions"]:
        print(f"- {item}")


if __name__ == "__main__":
    main()
