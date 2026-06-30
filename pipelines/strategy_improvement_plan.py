from __future__ import annotations

import argparse

from services.strategy_improvement_plan import StrategyImprovementPlan


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the daily GOLD 5m strategy improvement plan from review, experiments, and guardrails.")
    parser.add_argument("--date", required=True)
    args = parser.parse_args()
    result = StrategyImprovementPlan().build(args.date)
    print(f"strategy_improvement_plan: {result['status']} date={result['run_date']} steps={len(result['next_steps'])}")


if __name__ == "__main__":
    main()
