from __future__ import annotations

import argparse
from datetime import datetime, timezone

from services.config_loader import ROOT, load_pipeline_config
from services.feishu_report_sender import FeishuReportSender
from services.trading_daily_24h_report import TradingDaily24hReportBuilder


REPORT_KIND = "trading_daily_24h"


def main() -> None:
    parser = argparse.ArgumentParser(description="Build and deliver the terminal 24-hour trading report.")
    parser.add_argument("--date", help="Beijing report date (YYYY-MM-DD); defaults to yesterday")
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()

    output_root = ROOT / str(load_pipeline_config().get("output_root", "outputs"))
    report_path = TradingDaily24hReportBuilder(output_root).build(
        now=datetime.now(timezone.utc),
        report_date=args.date,
    )
    report_date = report_path.name.removesuffix("-daily-24h.md")
    print(f"trading_daily_24h_report: built {report_path}")

    if args.send:
        result = FeishuReportSender(output_root).run(
            run_date=report_date,
            kind=REPORT_KIND,
            title=f"黄金交易 24 小时报告｜{report_date}",
            source_path=report_path,
        )
        print(f"trading_daily_24h_report: sent delivered={result['delivered']} status={result['status']}")
        if args.verify and not result["delivered"]:
            raise RuntimeError(f"Feishu delivery failed: {result.get('delivery')}")
    elif args.verify:
        raise RuntimeError("--verify requires --send")


if __name__ == "__main__":
    main()
