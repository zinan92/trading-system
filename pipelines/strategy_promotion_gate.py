from __future__ import annotations

import argparse
import json
from datetime import date
from services.run_date import utc_run_date

from services.strategy_promotion_gate import StrategyPromotionGate


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate whether a GOLD 5m shadow experiment can be promoted to a paper-only parameter request.")
    parser.add_argument("--date", default=utc_run_date())
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    result = StrategyPromotionGate().run(args.date)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return
    print(f"strategy_promotion_gate: {result['status']} date={result['run_date']} allowed={result['promotion_allowed']}")
    print(f"candidate={result.get('candidate', {}).get('variant_id', 'n/a')} score_delta={result['score_delta']}")
    for item in result["blockers"][:6]:
        print(f"- {item['name']}: {item['summary']}")


if __name__ == "__main__":
    main()
