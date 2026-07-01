from __future__ import annotations

import argparse
import json
from datetime import date
from services.run_date import utc_run_date

from services.data_source_lineage import DataSourceLineage


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit local GOLD 5m database provider lineage.")
    parser.add_argument("--date", default=utc_run_date())
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = DataSourceLineage().run(args.date)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return
    print(
        f"data_source_lineage: {result['status']} date={args.date} "
        f"truth={result['truth_level']} live={result['ready_for_live']}"
    )


if __name__ == "__main__":
    main()
