from __future__ import annotations

import argparse
from datetime import date
from services.run_date import utc_run_date

from services.trading_journal import TradingJournalBuilder


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a human-readable daily Trading Journal.")
    parser.add_argument("--date", default=utc_run_date(), help="Run date in YYYY-MM-DD format.")
    args = parser.parse_args()

    path = TradingJournalBuilder().build(args.date)
    print("Trading Journal completed.")
    print(f"journal: {path}")


if __name__ == "__main__":
    main()
