from __future__ import annotations

import argparse
import json
from datetime import date
from services.run_date import utc_run_date

from services.binance_usdm_testnet_canary import run_binance_usdm_testnet_canary


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a locked Binance USDM testnet XAUUSDT canary.")
    parser.add_argument("--date", default=utc_run_date())
    parser.add_argument("--quantity", type=float, default=0.002, help="XAUUSDT canary quantity; default clears Binance's 5 USDT minimum notional.")
    parser.add_argument(
        "--execute-testnet",
        action="store_true",
        help="Create real Binance USDM testnet orders, attach server-side TP/SL, reconcile, then submit a reduce-only testnet close. No mainnet endpoint is allowed.",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    result = run_binance_usdm_testnet_canary(args.date, execute_testnet=args.execute_testnet, quantity=args.quantity)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return
    print(f"binance_usdm_testnet_canary: {result['status']} date={result['run_date']} mode={result['mode']} symbol={result['symbol']}")
    for item in result["checks"]:
        print(f"- {item['status']}: {item['name']} - {item['summary']}")


if __name__ == "__main__":
    main()
