from __future__ import annotations

import argparse
import json
from datetime import date
from services.run_date import utc_run_date

from services.broker_feed_smoke import BrokerFeedSmoke


def main() -> None:
    parser = argparse.ArgumentParser(description="Smoke-test the official GOLD 5m broker/MT5 CSV market-data import path.")
    parser.add_argument("--date", default=utc_run_date())
    args = parser.parse_args()

    print(json.dumps(BrokerFeedSmoke().run(args.date), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
