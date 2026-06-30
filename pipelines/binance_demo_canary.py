from __future__ import annotations

import argparse
import json
from datetime import date

from services.binance_demo_canary import run_binance_demo_canary


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a locked Binance Futures Demo XAUUSDT canary.")
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--quantity", type=float, default=0.002, help="XAUUSDT canary quantity; default clears Binance's 5 USDT minimum notional.")
    parser.add_argument(
        "--execute-demo",
        action="store_true",
        help="Create a real Binance Futures Demo order, then immediately send a reduce-only close order. No mainnet endpoint is allowed.",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    result = run_binance_demo_canary(args.date, execute_demo=args.execute_demo, quantity=args.quantity)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return
    print(f"binance_demo_canary: {result['status']} date={result['run_date']} mode={result['mode']} symbol={result['symbol']}")
    for item in result["checks"]:
        print(f"- {item['status']}: {item['name']} - {item['summary']}")


if __name__ == "__main__":
    main()
