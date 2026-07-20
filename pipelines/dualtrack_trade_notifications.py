from __future__ import annotations

import argparse
import json

from services.config_loader import ROOT, load_pipeline_config
from services.dualtrack_feishu import DualTrackTradeRecordNotifier


def main() -> None:
    parser = argparse.ArgumentParser(description="Send dualtrack machine entry/exit records to Feishu.")
    parser.add_argument("--cycle-id", default="")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--backfill-existing", action="store_true", help="Send existing fills on the first notifier run instead of baselining them.")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()

    config = load_pipeline_config()
    output_root = ROOT / str(config.get("output_root", "outputs"))
    notifier = DualTrackTradeRecordNotifier(output_root=output_root)
    result = (
        notifier.notify_cycle(args.cycle_id, force=args.force, backfill_existing=args.backfill_existing)
        if args.cycle_id
        else notifier.notify_current(force=args.force, backfill_existing=args.backfill_existing)
    )
    if args.verify and result.get("failed", 0):
        raise RuntimeError(f"dualtrack trade notification failed: {result}")
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    print(
        "dualtrack_trade_notifications: "
        f"cycle_id={result.get('cycle_id')} sent={result.get('sent', 0)} "
        f"skipped={result.get('skipped', 0)} failed={result.get('failed', 0)} status={result.get('status')}"
    )


if __name__ == "__main__":
    main()
