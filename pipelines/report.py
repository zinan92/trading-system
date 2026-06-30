from __future__ import annotations

import argparse
from datetime import date

from services.reporting import ReportBuilder


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a Trading OS daily report.")
    parser.add_argument("--date", default=date.today().isoformat(), help="Run date in YYYY-MM-DD format.")
    args = parser.parse_args()

    path = ReportBuilder().build_daily_report(args.date)
    journal_path = path.parents[1] / "journals" / f"{args.date}.md"
    print("Trading OS daily report completed.")
    print(f"report: {path}")
    print(f"journal: {journal_path}")


if __name__ == "__main__":
    main()
