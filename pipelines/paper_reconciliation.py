from __future__ import annotations

import argparse
import json
import os
from datetime import date
from pathlib import Path
from services.run_date import utc_run_date

from services.config_loader import ROOT, load_pipeline_config
from services.paper_reconciliation import PaperReconciliation


def main() -> None:
    parser = argparse.ArgumentParser(description="Reconcile paper orders, trades, positions, and journal decisions.")
    parser.add_argument("--date", default=utc_run_date())
    parser.add_argument("--json", action="store_true", help="Print the full JSON payload.")
    args = parser.parse_args()

    config = load_pipeline_config()
    output_root = Path(os.getenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(ROOT / config.get("output_root", "outputs"))))
    result = PaperReconciliation(output_root).run(args.date)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return
    print(f"paper_reconciliation: {result['status']} date={result['run_date']}")
    summary = result["summary"]
    print(f"orders={summary['orders']} filled={summary['filled_orders']} open_trades={summary['open_trades']} positions={summary['positions']}")
    for item in result["checks"]:
        print(f"- {item['name']}: {item['status']} {item['summary']}")


if __name__ == "__main__":
    main()
