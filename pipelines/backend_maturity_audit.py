from __future__ import annotations

import argparse
import json
from datetime import date

from services.backend_maturity_audit import BackendMaturityAudit


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit M0-M5 backend platform maturity evidence.")
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    result = BackendMaturityAudit().run(args.date)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return
    print(f"backend_maturity_audit: {result['status']} date={args.date}")
    for check in result.get("checks", []):
        print(f"- {check.get('name')}: {check.get('status')} {check.get('summary')}")


if __name__ == "__main__":
    main()
