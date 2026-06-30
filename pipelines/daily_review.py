from __future__ import annotations

import argparse
import json
from datetime import date

from services.daily_review_runner import DailyReviewRunner


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the end-of-day Trading Journal, review, performance, and readiness loop.")
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--json", action="store_true", help="Print the full JSON payload.")
    args = parser.parse_args()

    result = DailyReviewRunner().run(args.date)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return
    summary = result["summary"]
    print(f"daily_review: {result['status']} date={args.date}")
    print(f"mock={summary.get('mock_runtime')} health={summary.get('health')} audit={summary.get('audit')} doctor={summary.get('doctor')}")
    print(f"net_marked={summary.get('net_pnl_marked')} open_R={summary.get('open_unrealized_r')} expectancy_R={summary.get('expectancy_r')}")
    print(f"report: {result['artifacts']['report']}")
    print(f"journal: {result['artifacts']['journal']}")
    print(f"review_notes: {result['artifacts']['review_notes']}")


if __name__ == "__main__":
    main()
