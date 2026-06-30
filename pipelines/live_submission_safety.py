from __future__ import annotations

import argparse
import json
from datetime import date

from services.config_loader import ROOT, load_pipeline_config
from services.live_submission_safety import LiveSubmissionSafetySmoke


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify real broker submission is blocked without live_activation.real_money_ready.")
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    output_root = ROOT / load_pipeline_config().get("output_root", "outputs")
    result = LiveSubmissionSafetySmoke(output_root).run(args.date)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return
    print(f"live_submission_safety: {result['status']} date={args.date} blocked={result['blocked_by_activation_gate']}")


if __name__ == "__main__":
    main()
