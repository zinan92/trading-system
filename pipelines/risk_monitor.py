from __future__ import annotations

import argparse
import json
from datetime import date

from services.risk_monitor import RiskMonitor


def main() -> None:
    parser = argparse.ArgumentParser(description="Run intraday risk monitor and paper kill-switch checks.")
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = RiskMonitor().run(args.date)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return
    print(
        f"risk_monitor: {result['status']} date={args.date} "
        f"kill_switch={result['kill_switch_active']} paper_auto={result['allow_paper_auto_approve']}"
    )


if __name__ == "__main__":
    main()
