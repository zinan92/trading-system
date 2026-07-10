from __future__ import annotations

import argparse
import json
from pathlib import Path

from services.config_loader import ROOT, load_pipeline_config
from services.dualtrack_binance_account_costs import BinanceAccountCostObserver
from services.journal_store import write_json


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Observe read-only Binance account fees and funding for paper shadow.")
    parser.add_argument("--symbol", default="XAUUSDT")
    parser.add_argument("--output-root", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    output_root = Path(args.output_root) if args.output_root else ROOT / str(load_pipeline_config().get("output_root", "outputs"))
    try:
        result = BinanceAccountCostObserver().observe(args.symbol)
    except Exception as exc:
        result = {
            "schema_version": "dualtrack-binance-account-costs-v1",
            "status": "blocked",
            "symbol": args.symbol,
            "blocker": str(exc),
            "read_only": True,
            "real_money_eligible": False,
        }
    write_json(output_root / "dualtrack" / "nautilus" / "account_costs" / "current.json", [result])
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"dualtrack_binance_account_costs: status={result['status']} symbol={args.symbol}")
    return 0 if result["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
