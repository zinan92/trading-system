from __future__ import annotations

import argparse
import json
from datetime import date
from services.run_date import utc_run_date

from services.config_loader import ROOT, load_pipeline_config
from services.strategy_daily_review import StrategyDailyReview


def main() -> None:
    parser = argparse.ArgumentParser(description="Build by-strategy daily review attribution.")
    parser.add_argument("--date", default=utc_run_date())
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    cfg = load_pipeline_config()
    output_root = ROOT / str(cfg.get("output_root", "outputs"))
    result = StrategyDailyReview(output_root).build(args.date)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    print(f"strategy_daily_review: {result['strategy_count']} strategies {result['status_counts']}")


if __name__ == "__main__":
    main()
