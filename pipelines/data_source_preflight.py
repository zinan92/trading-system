from __future__ import annotations

import argparse
import json
from datetime import date
from services.run_date import utc_run_date

from services.data_source_preflight import DataSourcePreflight


def main() -> None:
    parser = argparse.ArgumentParser(description="Check GOLD 5m market data provenance and live-readiness.")
    parser.add_argument("--date", default=utc_run_date())
    args = parser.parse_args()

    print(json.dumps(DataSourcePreflight().run(args.date), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
