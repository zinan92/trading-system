from __future__ import annotations

import argparse
import json
from pathlib import Path

from services.config_loader import ROOT, load_pipeline_config
from services.bias_ledger import BiasLedger
from services.run_date import utc_run_date


def main() -> None:
    parser = argparse.ArgumentParser(description="Settle expired human bias ledger entries.")
    parser.add_argument("--run-date", default=utc_run_date())
    parser.add_argument("--output-root", type=Path, default=None)
    parser.add_argument("--market-db", type=Path, default=None)
    parser.add_argument("--as-of", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    cfg = load_pipeline_config()
    output_root = args.output_root or ROOT / str(cfg.get("output_root", "outputs"))
    market_db = args.market_db or ROOT / str(cfg.get("local_market_db", "data/market_data.db"))
    result = BiasLedger(output_root, market_db).settle_expired(as_of=args.as_of, run_date=args.run_date)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    summary = result["summary"]
    print(
        "bias_ledger_settle: "
        f"run_date={args.run_date} settled={result['settled']} "
        f"pending_data={result['pending_data']} total={summary['total']}"
    )


if __name__ == "__main__":
    main()
