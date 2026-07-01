from __future__ import annotations

import argparse
import json
from datetime import date
from services.run_date import utc_run_date

from services.config_loader import ROOT, load_pipeline_config
from services.paper_risk_action_plan import PaperRiskActionPlan


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a paper risk action plan from risk and exit queues.")
    parser.add_argument("--date", default=utc_run_date())
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    output_root = ROOT / load_pipeline_config().get("output_root", "outputs")
    result = PaperRiskActionPlan(output_root).build(args.date)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return
    summary = result["summary"]
    print(
        f"paper_risk_action_plan: {result['status']} "
        f"actions={summary['action_count']} high={summary['high_priority']} exits={summary['exit_actions']}"
    )


if __name__ == "__main__":
    main()
