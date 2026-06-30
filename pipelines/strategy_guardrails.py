from __future__ import annotations

import argparse
import json
from datetime import date

from services.strategy_guardrails import run_strategy_guardrails


def main() -> None:
    parser = argparse.ArgumentParser(description="Build strategy guardrails from daily review and paper performance.")
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--json", action="store_true", help="Print the full JSON payload.")
    args = parser.parse_args()

    result = run_strategy_guardrails(args.date)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return
    print(f"strategy_guardrails: {result['status']} date={result['run_date']} allow_new_paper_order={result['allow_new_paper_order']}")
    for item in result["checks"]:
        print(f"- {item['name']}: {item['status']} {item['summary']}")


if __name__ == "__main__":
    main()
