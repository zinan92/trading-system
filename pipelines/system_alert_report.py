from __future__ import annotations

import argparse

from services.config_loader import ROOT, load_pipeline_config
from services.run_date import utc_run_date
from services.system_alert_report import SystemAlertReport, verify_system_alert_receipt


def main() -> None:
    parser = argparse.ArgumentParser(description="Send a concise DevOps-facing gold system alert report.")
    parser.add_argument("--date", default=utc_run_date(), help="Run date in YYYY-MM-DD format.")
    parser.add_argument("--send", action="store_true", help="Send to the configured alert channel.")
    parser.add_argument("--verify", action="store_true", help="Require a delivered system alert receipt.")
    args = parser.parse_args()

    config = load_pipeline_config()
    output_root = ROOT / str(config.get("output_root", "outputs"))
    result = SystemAlertReport(output_root).run(args.date, send=args.send)
    print(
        "system_alert_report:"
        f" status={result['status']}"
        f" delivered={result['delivered']}"
        f" hard={result['hard_issue_count']}"
        f" warnings={result['warning_count']}"
    )
    if args.verify:
        receipt = verify_system_alert_receipt(output_root, args.date)
        print(f"system_alert_report: verified receipt={output_root / 'system_alert_reports' / f'{args.date}.json'} generated_at={receipt.get('generated_at')}")


if __name__ == "__main__":
    main()
