from __future__ import annotations

import argparse
import json
from datetime import date
from services.run_date import utc_run_date

from services.config_loader import ROOT, load_pipeline_config
from services.paper_exit_decisions import PaperExitDecisionQueue


def main() -> None:
    parser = argparse.ArgumentParser(description="Build paper trade exit decision queue.")
    parser.add_argument("--date", default=utc_run_date())
    parser.add_argument("--json", action="store_true", help="Print the full JSON payload.")
    args = parser.parse_args()

    output_root = ROOT / load_pipeline_config().get("output_root", "outputs")
    result = PaperExitDecisionQueue(output_root).build(args.date)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return
    summary = result["summary"]
    print(
        f"paper_exit_decisions: {result['status']} "
        f"open={summary['open_items']} high={summary['high_priority']} decisions={summary['recorded_decisions']}"
    )


if __name__ == "__main__":
    main()
