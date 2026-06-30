from __future__ import annotations

import argparse
from datetime import date

from services.data_integrity_check import DataIntegrityCheck


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify local GOLD 5m data integrity, archive manifest, and snapshot restore evidence.")
    parser.add_argument("--date", default=date.today().isoformat())
    args = parser.parse_args()
    result = DataIntegrityCheck().run(args.date)
    print(f"data_integrity: {result['status']} date={args.date} passed={result['summary']['passed']} failed={result['summary']['failed']}")
    for item in result["checks"]:
        print(f"- {item['status']}: {item['name']} - {item['summary']}")


if __name__ == "__main__":
    main()
