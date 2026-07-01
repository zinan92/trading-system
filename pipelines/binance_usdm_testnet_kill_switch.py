from __future__ import annotations

import argparse
import json
from datetime import date
from services.run_date import utc_run_date

from services.binance_usdm_testnet_kill_switch import run_binance_usdm_testnet_kill_switch


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the locked Binance USDM testnet kill switch for XAUUSDT.")
    parser.add_argument("--date", default=utc_run_date())
    parser.add_argument(
        "--confirm-testnet-kill",
        action="store_true",
        help="Submit reduce-only close orders and cancel all XAUUSDT testnet open/algo orders. No mainnet endpoint is allowed.",
    )
    parser.add_argument("--clear-halt", action="store_true", help="Clear the persistent HALT state after manual verification.")
    parser.add_argument("--confirm-clear", action="store_true", help="Required with --clear-halt to actually clear HALT.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    result = run_binance_usdm_testnet_kill_switch(
        args.date,
        confirm_testnet_kill=args.confirm_testnet_kill,
        clear_halt=args.clear_halt,
        confirm_clear=args.confirm_clear,
    )
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return
    print(f"binance_usdm_testnet_kill_switch: {result['status']} date={result['run_date']}")
    if result.get("block_reason"):
        print(f"- block_reason: {result['block_reason']}")
    if result.get("halt"):
        print(f"- halt_active: {result['halt'].get('active')}")
    if result.get("confirmed_flat") is not None:
        print(f"- confirmed_flat: {result.get('confirmed_flat')}")


if __name__ == "__main__":
    main()
