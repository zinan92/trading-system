from __future__ import annotations

import argparse
import json
from datetime import date
from services.run_date import utc_run_date

from services.daily_plan_review import DailyPlanReview


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the morning trading plan artifact.")
    parser.add_argument("--date", default=utc_run_date())
    args = parser.parse_args()

    print(json.dumps(DailyPlanReview().morning_plan(args.date), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
