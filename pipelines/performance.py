from __future__ import annotations

import argparse
import json
from datetime import date

from services.config_loader import ROOT, load_pipeline_config
from services.paper_performance import PaperPerformanceAnalyzer


def main() -> None:
    parser = argparse.ArgumentParser(description="Build paper trading performance analytics.")
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--json", action="store_true", help="Print the full JSON payload.")
    args = parser.parse_args()

    output_root = ROOT / load_pipeline_config().get("output_root", "outputs")
    result = PaperPerformanceAnalyzer(output_root).build(args.date)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return
    summary = result["summary"]
    print(f"performance: date={args.date} open={summary['open_trade_count']} closed_all={summary['closed_all_count']}")
    print(f"net_marked={summary['net_pnl_marked']} open_R={summary['open_unrealized_r']} expectancy_R={summary['expectancy_r']}")


if __name__ == "__main__":
    main()
