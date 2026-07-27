from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from services.cloud_daily_self_review import BJ_TZ, CloudDailySelfReview
from services.config_loader import ROOT, load_pipeline_config


def _source_sha() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    return str(result.stdout or "").strip() if result.returncode == 0 else ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the focused Paper daily self-review.")
    parser.add_argument("--date", help="Beijing report date; defaults to yesterday")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    config = load_pipeline_config()
    output_root = Path(
        os.getenv(
            "TRADING_ORCHESTRATOR_OUTPUT_ROOT",
            str(ROOT / config.get("output_root", "outputs")),
        )
    )
    report_date = args.date or (
        datetime.now(BJ_TZ).date() - timedelta(days=1)
    ).isoformat()
    result = CloudDailySelfReview(
        output_root,
        deployed_sha=_source_sha(),
    ).build(report_date)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(
            f"cloud_daily_self_review: {result['status']} "
            f"date={result['report_date']} revision={result['evidence_revision']}"
        )
    return 0 if result["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
