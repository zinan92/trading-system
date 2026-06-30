from __future__ import annotations

import argparse
import json
from datetime import date

from services.oanda_feed_client import run_oanda_feed_import


def main() -> None:
    parser = argparse.ArgumentParser(description="Import official OANDA XAU_USD M5 candles into the local market database.")
    parser.add_argument("--date", default=date.today().isoformat())
    args = parser.parse_args()

    print(json.dumps(run_oanda_feed_import(args.date), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
