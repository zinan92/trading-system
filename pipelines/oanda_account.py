from __future__ import annotations

import argparse
import json
from datetime import date

from services.oanda_account_preflight import run_oanda_account_preflight


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a read-only OANDA account and XAU_USD instrument preflight.")
    parser.add_argument("--date", default=date.today().isoformat())
    args = parser.parse_args()

    print(json.dumps(run_oanda_account_preflight(args.date), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
