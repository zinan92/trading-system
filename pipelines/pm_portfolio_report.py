from __future__ import annotations

import argparse
from pathlib import Path

from services.config_loader import ROOT, load_pipeline_config
from services.feishu_report_sender import FeishuReportSender
from services.pm_portfolio_report import PM_REPORT_KINDS, PMPortfolioReportBuilder, report_title, verify_report_receipt
from services.run_date import utc_run_date


def main() -> None:
    parser = argparse.ArgumentParser(description="Build and optionally deliver a deterministic PM portfolio report.")
    parser.add_argument("--date", default=utc_run_date(), help="Run date in YYYY-MM-DD format.")
    parser.add_argument("--kind", default="pm_morning", choices=sorted(PM_REPORT_KINDS))
    parser.add_argument("--send", action="store_true", help="Send the report digest to Feishu.")
    parser.add_argument("--verify", action="store_true", help="Require a delivered Feishu receipt for this report.")
    parser.add_argument("--max-chars", type=int, default=3500)
    args = parser.parse_args()

    pipeline_config = load_pipeline_config()
    output_root = ROOT / str(pipeline_config.get("output_root", "outputs"))
    title = report_title(args.date, args.kind)
    report_path = PMPortfolioReportBuilder(output_root).build(args.date, args.kind)
    print(f"pm_portfolio_report: built {report_path}")

    if args.send:
        result = FeishuReportSender(output_root).run(
            run_date=args.date,
            kind=args.kind,
            title=title,
            source_path=Path(report_path),
            max_chars=args.max_chars,
        )
        print(f"pm_portfolio_report: sent delivered={result['delivered']} status={result['status']} channel={result['channel']}")

    if args.verify:
        receipt = verify_report_receipt(output_root, args.date, args.kind, report_path)
        print(f"pm_portfolio_report: verified receipt={output_root / 'feishu_reports' / f'{args.date}.json'} generated_at={receipt.get('generated_at')}")


if __name__ == "__main__":
    main()
