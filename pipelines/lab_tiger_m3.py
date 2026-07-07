from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from services.config_loader import ROOT, load_pipeline_config
from services.lab_registry import LabRegistry
from services.lab_tiger_contract_grid import TigerContractGridConfig, run_tiger_contract_grid
from services.market_store import MarketStore


def main() -> None:
    parser = argparse.ArgumentParser(description="Run exploratory Tiger MGC integer-contract grid lab.")
    parser.add_argument("--symbol", default="MGCmain")
    parser.add_argument("--timeframe", default="1m")
    parser.add_argument("--min-cycles", type=int, default=5)
    args = parser.parse_args()

    config = load_pipeline_config()
    output_root = Path(os.getenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(ROOT / config.get("output_root", "outputs"))))
    market_db = Path(os.getenv("TRADING_ORCHESTRATOR_MARKET_DB", str(ROOT / config.get("local_market_db", "data/market_data.db"))))
    bars = MarketStore(market_db).load_bars(args.symbol, args.timeframe, 2_000_000)
    report = run_tiger_contract_grid(
        output_root,
        LabRegistry(output_root),
        bars,
        config=TigerContractGridConfig(min_cycles=args.min_cycles),
    )
    print(json.dumps({
        "status": "pass" if report["trial_count"] else "warn",
        "trial_count": report["trial_count"],
        "coverage_note": report["coverage_note"],
        "decision_gate": report["decision_gate"],
        "report": str(output_root / "lab" / "reports" / "R5_tiger_mgc_contract_grid.md"),
    }, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
