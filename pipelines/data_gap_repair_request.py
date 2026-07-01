from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
from services.run_date import utc_run_date

from services.config_loader import ROOT, load_pipeline_config
from services.data_gap_repair import DataGapRepairRequest


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a human-readable request and CSV template for repairing GOLD 5m data gaps.")
    parser.add_argument("--date", default=utc_run_date())
    args = parser.parse_args()
    config = load_pipeline_config()
    output_root = Path(config.get("output_root", "outputs"))
    if not output_root.is_absolute():
        output_root = ROOT / output_root
    feed_config = config.get("broker_feed", {})
    feed_dir = Path(feed_config.get("input_dir", "data/broker_feeds/gold_5m"))
    if not feed_dir.is_absolute():
        feed_dir = ROOT / feed_dir
    print(json.dumps(DataGapRepairRequest(output_root, feed_dir).build(args.date), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
