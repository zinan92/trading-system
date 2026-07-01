from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
from services.run_date import utc_run_date

from services.config_loader import ROOT, load_pipeline_config
from services.feishu_report_sender import FeishuReportSender


def main() -> None:
    parser = argparse.ArgumentParser(description="Send a trading report summary to Feishu and persist a receipt.")
    parser.add_argument("--date", default=utc_run_date())
    parser.add_argument("--kind", required=True, help="pm_morning, pm_evening, strategy_research, health_check, etc.")
    parser.add_argument("--title", required=True)
    parser.add_argument("--file", type=Path, default=None, help="Markdown/text artifact to send.")
    parser.add_argument("--message", default=None, help="Inline message to send.")
    parser.add_argument("--max-chars", type=int, default=3500)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    cfg = load_pipeline_config()
    output_root = ROOT / str(cfg.get("output_root", "outputs"))
    result = FeishuReportSender(output_root).run(
        run_date=args.date,
        kind=args.kind,
        title=args.title,
        source_path=args.file,
        message=args.message,
        max_chars=args.max_chars,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    print(f"feishu_report: {result['status']} delivered={result['delivered']} channel={result['channel']}")
    if not result["delivered"]:
        delivery = result.get("delivery", {})
        print(delivery.get("reason") or delivery.get("message") or "delivery failed")


if __name__ == "__main__":
    main()
