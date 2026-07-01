from __future__ import annotations

import argparse
import json
from datetime import date
from services.run_date import utc_run_date

from services.config_loader import ROOT, load_pipeline_config
from services.paper_trade_attribution import PaperTradeAttributor


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill strategy attribution onto paper trade lifecycle records.")
    parser.add_argument("--date", default=utc_run_date())
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    output_root = ROOT / load_pipeline_config().get("output_root", "outputs")
    result = PaperTradeAttributor(output_root).run(args.date)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return
    summary = result["summary"]
    print(f"paper_trade_attribution: {result['status']} date={args.date}")
    print(f"updated_open={summary['updated_open_trades']} updated_closed={summary['updated_closed_trades']}")


if __name__ == "__main__":
    main()
