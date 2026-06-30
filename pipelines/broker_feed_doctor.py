from __future__ import annotations

import argparse
import json
from datetime import date

from services.broker_feed_doctor import BrokerFeedDoctor


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate broker/MT5 GOLD 5m CSV feed files before importing them.")
    parser.add_argument("--date", default=date.today().isoformat())
    args = parser.parse_args()

    print(json.dumps(BrokerFeedDoctor().run(args.date), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
