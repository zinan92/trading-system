from __future__ import annotations

import argparse
import json
from datetime import date
from services.run_date import utc_run_date

from services.live_switch_plan import run_live_switch_plan


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the local paper-to-live switch plan artifact.")
    parser.add_argument("--date", default=utc_run_date())
    args = parser.parse_args()

    print(json.dumps(run_live_switch_plan(args.date), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
