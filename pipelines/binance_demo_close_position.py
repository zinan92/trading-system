from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
from services.run_date import utc_run_date

from services.binance_demo_broker_adapter import BinanceDemoBrokerAdapter
from services.config_loader import ROOT, load_pipeline_config


def _adapter() -> BinanceDemoBrokerAdapter:
    config = load_pipeline_config()
    output_root = Path(config.get("output_root", "outputs"))
    if not output_root.is_absolute():
        output_root = ROOT / output_root
    demo = config.get("demo_trading", {}) or {}
    profile_name = str(demo.get("broker_profile", config.get("broker", {}).get("provider", "binance_usdm")))
    broker_config = (config.get("broker_profiles", {}) or {}).get(profile_name) or config.get("broker", {})
    return BinanceDemoBrokerAdapter(output_root, broker_config, demo)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Inspect or explicitly close the current XAUUSDT Binance Futures Demo position."
    )
    parser.add_argument("--date", default=utc_run_date())
    parser.add_argument(
        "--confirm-close-demo-position",
        action="store_true",
        help="Submit a reduce-only MARKET close for the current XAUUSDT demo position. Omit for dry-run only.",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    result = _adapter().close_demo_position(
        args.date,
        confirm_close_demo_position=args.confirm_close_demo_position,
    )
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return

    position = result.get("position", {}) if isinstance(result, dict) else {}
    close_request = result.get("close_request", {}) if isinstance(result, dict) else {}
    print(f"binance_demo_close_position: {result.get('status')} date={result.get('run_date')} symbol={result.get('symbol')}")
    print(
        "position "
        f"qty={position.get('positionAmt')} "
        f"entry={position.get('entryPrice')} "
        f"unrealized_pnl={position.get('unRealizedProfit')}"
    )
    if close_request:
        print(
            "close_request "
            f"side={close_request.get('side')} "
            f"quantity={close_request.get('quantity')} "
            f"reduceOnly={close_request.get('reduceOnly')}"
        )
    if result.get("block_reason"):
        print(f"block_reason: {result.get('block_reason')}")
    print(f"network_order_created={bool(result.get('network_order_created'))}")


if __name__ == "__main__":
    main()
