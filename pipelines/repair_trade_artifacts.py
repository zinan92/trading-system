from __future__ import annotations

import argparse
import json
from datetime import date
from services.run_date import utc_run_date

from services.config_loader import ROOT, load_pipeline_config
from services.trade_artifact_repair import TradeArtifactRepair


def main() -> None:
    parser = argparse.ArgumentParser(description="Repair missing signal/ticket artifacts referenced by paper trades.")
    parser.add_argument("--date", default=utc_run_date())
    parser.add_argument("--strategy", default="", help="Optional strategy id under outputs/strategies.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    output_root = ROOT / load_pipeline_config().get("output_root", "outputs")
    result = TradeArtifactRepair(output_root).run(args.date, strategy_id=args.strategy or None)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return
    summary = result["summary"]
    print(
        "trade_artifact_repair: "
        f"status={result['status']} date={args.date} strategy={args.strategy or 'all'} "
        f"signals={summary['repaired_signals']} tickets={summary['repaired_tickets']}"
    )


if __name__ == "__main__":
    main()
