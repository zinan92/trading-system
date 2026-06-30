from __future__ import annotations

import argparse
import json
from datetime import date

from services.daily_plan_review import DailyPlanReview


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the morning trading plan artifact.")
    parser.add_argument("--date", default=date.today().isoformat())
    args = parser.parse_args()

    print(json.dumps(DailyPlanReview().morning_plan(args.date), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
