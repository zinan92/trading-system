from __future__ import annotations

import argparse
import json
from datetime import date
from services.run_date import utc_run_date

from services.live_activation import LiveActivationGate
from services.live_cutover_package import run_live_cutover_package
from services.live_readiness import LiveReadiness
from services.live_switch_plan import LiveSwitchPlan


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the real-money live cutover package for the GOLD 5m Trading Bot.")
    parser.add_argument("--date", default=utc_run_date())
    parser.add_argument("--json", action="store_true", help="Print the full JSON payload.")
    args = parser.parse_args()

    LiveReadiness().run(args.date)
    LiveActivationGate().run(args.date)
    LiveSwitchPlan().run(args.date)
    result = run_live_cutover_package(args.date)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return
    print(f"live_cutover: {result['status']} date={result['run_date']} live_ready={result['live_ready']} real_money_ready={result['real_money_ready']}")
    print(f"blockers={len(result['blockers'])}")
    for item in result["blockers"][:5]:
        print(f"- {item['source']}/{item['name']}: {item['summary']}")


if __name__ == "__main__":
    main()
