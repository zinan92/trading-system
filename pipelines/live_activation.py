from __future__ import annotations

import argparse
import json
from datetime import date
from services.run_date import utc_run_date

from services.live_activation import LiveActivationGate


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate the live activation gate without switching execution mode.")
    parser.add_argument("--date", default=utc_run_date())
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    result = LiveActivationGate().run(args.date)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return
    print(f"live_activation: {result['status']} date={result['run_date']}")
    print(f"dry_run_ready={result['dry_run_ready']} real_money_ready={result['real_money_ready']}")
    for action in result["next_actions"][:5]:
        print(f"- {action}")


if __name__ == "__main__":
    main()
