from __future__ import annotations

import argparse
import json
from datetime import date
from services.run_date import utc_run_date

from services.alert_delivery_drill import AlertDeliveryDrill
from services.config_loader import ROOT, load_pipeline_config


def main() -> None:
    parser = argparse.ArgumentParser(description="Send a test alert and persist delivery proof.")
    parser.add_argument("--date", default=utc_run_date())
    parser.add_argument("--message", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    cfg = load_pipeline_config()
    output_root = ROOT / str(cfg.get("output_root", "outputs"))
    result = AlertDeliveryDrill(output_root).run(args.date, message=args.message)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return
    print(f"alert_delivery_drill: {result['status']} delivered={result['delivered']} channel={result['channel']}")
    if not result["delivered"]:
        print(result.get("delivery", {}).get("reason") or result.get("delivery", {}).get("message") or "delivery failed")


if __name__ == "__main__":
    main()
