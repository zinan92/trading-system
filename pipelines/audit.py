from __future__ import annotations

import argparse
import json
from datetime import date
from services.run_date import utc_run_date

from services.completion_audit import CompletionAudit


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit Trading Bot completion evidence against the user objective.")
    parser.add_argument("--date", default=utc_run_date())
    args = parser.parse_args()

    print(json.dumps(CompletionAudit().run(args.date), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
