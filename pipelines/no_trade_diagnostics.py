from __future__ import annotations

import argparse
import json
from datetime import date
from services.run_date import utc_run_date

from services.no_trade_diagnostics import NoTradeDiagnostics


def main() -> None:
    parser = argparse.ArgumentParser(description="Explain why the bot did or did not open paper trades today.")
    parser.add_argument("--date", default=utc_run_date())
    args = parser.parse_args()

    print(json.dumps(NoTradeDiagnostics().run(args.date), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
