from __future__ import annotations

import argparse
import json
from datetime import date

from services.config_loader import ROOT, load_pipeline_config
from services.market_analysis_prompt import send_market_analysis_prompt


def main() -> None:
    parser = argparse.ArgumentParser(description="Send the daily gold market-analysis prompt to Feishu.")
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    cfg = load_pipeline_config()
    output_root = ROOT / str(cfg.get("output_root", "outputs"))
    result = send_market_analysis_prompt(output_root, run_date=args.date)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    print(f"market_analysis_prompt: {result['status']} delivered={result['delivered']} channel={result['channel']}")
    if not result["delivered"]:
        delivery = result.get("delivery", {})
        print(delivery.get("reason") or delivery.get("message") or "delivery failed")


if __name__ == "__main__":
    main()
