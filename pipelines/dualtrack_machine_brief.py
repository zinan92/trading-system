from __future__ import annotations

import argparse
import json
from pathlib import Path

from services.config_loader import ROOT, load_pipeline_config
from services.dualtrack_feishu import DualTrackMachineBriefSender


def main() -> None:
    parser = argparse.ArgumentParser(description="Send the 12-hour dualtrack machine plan to Feishu.")
    parser.add_argument("--cycle-id", default="")
    parser.add_argument("--as-of", default="")
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    config = load_pipeline_config()
    output_root = ROOT / str(config.get("output_root", "outputs"))
    market_db = ROOT / str(config.get("local_market_db", "data/market_data.db"))
    service = DualTrackMachineBriefSender(output_root=output_root, market_db=market_db)
    payload = service.build(args.cycle_id or None, as_of=args.as_of or None)
    result = {"status": "built", "cycle_id": payload["cycle_id"], "run_date": payload["run_date"], "sent": 0}
    if args.send:
        result = service.send(args.cycle_id or None, as_of=args.as_of or None, force=args.force)
    if args.verify and args.send and result.get("delivered") is not True:
        raise RuntimeError(f"dualtrack machine brief was not delivered: {result}")
    if args.json:
        print(json.dumps({"brief": payload, "delivery": result}, ensure_ascii=False, indent=2))
        return
    print(
        "dualtrack_machine_brief: "
        f"cycle_id={payload['cycle_id']} status={payload['status']} sent={result.get('sent', 0)} delivered={result.get('delivered')}"
    )


if __name__ == "__main__":
    main()
