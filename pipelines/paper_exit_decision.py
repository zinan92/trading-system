from __future__ import annotations

import argparse
import json
from datetime import date
from services.run_date import utc_run_date

from services.config_loader import ROOT, load_pipeline_config
from services.paper_exit_decisions import PaperExitDecisionQueue


def main() -> None:
    parser = argparse.ArgumentParser(description="Record a manual paper trade exit decision.")
    parser.add_argument("--date", default=utc_run_date())
    parser.add_argument("--trade-id", required=True)
    parser.add_argument("--decision", required=True, choices=["approve_exit", "reject_exit", "hold"])
    parser.add_argument("--notes", default="")
    parser.add_argument("--exit-price", type=float, default=None)
    parser.add_argument("--exit-reason", default=None)
    parser.add_argument("--json", action="store_true", help="Print the full JSON payload.")
    args = parser.parse_args()

    output_root = ROOT / load_pipeline_config().get("output_root", "outputs")
    result = PaperExitDecisionQueue(output_root).record_decision(
        run_date=args.date,
        trade_id=args.trade_id,
        decision=args.decision,
        notes=args.notes,
        exit_price=args.exit_price,
        exit_reason=args.exit_reason,
    )
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return
    closed = result.get("closed_trade") or {}
    print(f"paper_exit_decision: {result['decision']} trade={result['trade_id']} decision_id={result['decision_id']}")
    if closed:
        print(f"closed: exit={closed.get('exit_price')} pnl={closed.get('realized_pnl')} reason={closed.get('exit_reason')}")


if __name__ == "__main__":
    main()
