from __future__ import annotations

import argparse
from datetime import date

from services.config_loader import ROOT, load_pipeline_config
from services.strategy_learning_actions import StrategyLearningActions


def main() -> None:
    parser = argparse.ArgumentParser(description="Build actionable strategy learning tasks from the daily GOLD review.")
    parser.add_argument("--date", default=date.today().isoformat())
    args = parser.parse_args()
    output_root = ROOT / load_pipeline_config().get("output_root", "outputs")
    result = StrategyLearningActions(output_root).build(args.date)
    print(f"strategy_learning_actions: {result['status']} date={args.date} actions={result['summary']['action_count']}")
    for item in result["actions"][:5]:
        print(f"- [{item['priority']}] {item['action_id']}: {item['summary']}")


if __name__ == "__main__":
    main()
