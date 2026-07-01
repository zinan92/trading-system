from __future__ import annotations

import argparse
import json
from datetime import date
from services.run_date import utc_run_date

from services.broker_receipts import BrokerReceiptImporter


def main() -> None:
    parser = argparse.ArgumentParser(description="Import MT5/broker bridge receipt JSON files into local artifacts.")
    parser.add_argument("--date", default=utc_run_date())
    args = parser.parse_args()

    print(json.dumps(BrokerReceiptImporter().import_pending(args.date), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
