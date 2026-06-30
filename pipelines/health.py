from __future__ import annotations

import argparse
import json
from datetime import date

from services.health_check import HealthCheck


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Trading Bot health checks against local artifacts.")
    parser.add_argument("--date", default=date.today().isoformat())
    args = parser.parse_args()

    print(json.dumps(HealthCheck().run(args.date), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
