from __future__ import annotations

import argparse
import json
from datetime import date
from services.run_date import utc_run_date

from services.strategy_experiment_queue import StrategyExperimentQueue


def main() -> None:
    parser = argparse.ArgumentParser(description="Build daily paper-only shadow strategy experiments for the active GOLD strategy.")
    parser.add_argument("--date", default=utc_run_date())
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    result = StrategyExperimentQueue().build(args.date)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return
    print(f"strategy_experiments: {result['status']} date={result['run_date']} sample_bars={result['sample_bars']}")
    print(f"best={result.get('best_candidate', {}).get('variant_id', 'n/a')} score={result.get('best_candidate', {}).get('score', 'n/a')}")
    for item in result["blockers"][:5]:
        print(f"- {item['name']}: {item['summary']}")


if __name__ == "__main__":
    main()
