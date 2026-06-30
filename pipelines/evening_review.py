from __future__ import annotations

import argparse
import json
from datetime import date

from services.daily_plan_review import DailyPlanReview


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the evening trading review artifact.")
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--notes", default="")
    args = parser.parse_args()

    print(json.dumps(DailyPlanReview().evening_review(args.date, notes=args.notes), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
