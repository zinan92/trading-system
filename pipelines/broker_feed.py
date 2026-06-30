from __future__ import annotations

import argparse
import json
from datetime import date

from services.broker_feed_bridge import BrokerFeedBridge


def main() -> None:
    parser = argparse.ArgumentParser(description="Import pending broker/MT5 GOLD 5m CSV files into the local market database.")
    parser.add_argument("--date", default=date.today().isoformat())
    args = parser.parse_args()

    print(json.dumps(BrokerFeedBridge().import_pending(args.date), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
