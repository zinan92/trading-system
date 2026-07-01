from __future__ import annotations

import argparse
from datetime import date
from services.run_date import utc_run_date

from services.bot_supervisor import BotSupervisor


def main() -> None:
    parser = argparse.ArgumentParser(description="Check the GOLD 5m mock trading bot heartbeat and daily review SLA.")
    parser.add_argument("--date", default=utc_run_date())
    args = parser.parse_args()
    result = BotSupervisor().run(args.date)
    summary = result.get("summary", {})
    print(
        f"bot_supervisor: {result['status']} date={args.date} "
        f"mock_running={result['mock_bot_running']} price={summary.get('latest_price')} "
        f"runner={summary.get('runner_state')}"
    )


if __name__ == "__main__":
    main()
