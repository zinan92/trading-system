from __future__ import annotations

import argparse
import json
from datetime import date

from services.trade_record_acceptance import TradeRecordAcceptanceAudit


def main() -> None:
    parser = argparse.ArgumentParser(description="Run M1 representative trade-record-card acceptance audit.")
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--sample-size", type=int, default=5)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    result = TradeRecordAcceptanceAudit(sample_size=args.sample_size).run(args.date)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return
    print(f"trade_record_acceptance: {result['status']} date={args.date} sample={result['sample_size_actual']}/{result['sample_size_requested']}")
    for row in result.get("samples", []):
        print(f"- {row.get('strategy_id')} {row.get('run_date')} {row.get('trade_id')}: {row.get('status')} protection={row.get('protection_status')} pnl={row.get('pnl_check', {}).get('status')}")


if __name__ == "__main__":
    main()
