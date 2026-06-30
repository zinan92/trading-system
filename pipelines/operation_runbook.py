from __future__ import annotations

import argparse
import json
from datetime import date

from services.operation_runbook import OperationRunbook


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the daily operation runbook for the GOLD Trading Bot.")
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    result = OperationRunbook().run(args.date)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return
    print(f"operation_runbook: {result['status']} date={result['run_date']} mode={result['mode']}")
    print(f"paper_manual={result['permissions']['paper_manual_review']} paper_auto={result['permissions']['paper_auto_approve']} live={result['permissions']['live_trading']}")
    print("next_actions:")
    for item in result["next_actions"]:
        print(f"- {item}")


if __name__ == "__main__":
    main()
