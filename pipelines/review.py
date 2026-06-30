from __future__ import annotations

import argparse
from datetime import date

from services.reporting import ReportBuilder


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the daily trading journal report and strategy review notes.")
    parser.add_argument("--date", default=date.today().isoformat(), help="Run date in YYYY-MM-DD format.")
    args = parser.parse_args()

    report_path = ReportBuilder().build_daily_report(args.date)
    review_path = report_path.parents[1] / "review_notes" / f"{args.date}.md"
    journal_path = report_path.parents[1] / "journals" / f"{args.date}.md"
    print("Trading OS daily review completed.")
    print(f"report: {report_path}")
    print(f"review_notes: {review_path}")
    print(f"journal: {journal_path}")


if __name__ == "__main__":
    main()
