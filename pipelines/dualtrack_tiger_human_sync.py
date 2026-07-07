from __future__ import annotations

import argparse
import json
from pathlib import Path

from services.dualtrack_tiger_human_sync import DualTrackTigerHumanSync
from services.run_date import utc_run_date


def main() -> None:
    parser = argparse.ArgumentParser(description="Import Tiger filled orders into the dualtrack human ledger.")
    parser.add_argument("--date", default=utc_run_date())
    parser.add_argument("--order-sync-path", default="")
    parser.add_argument("--output-root", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    result = DualTrackTigerHumanSync(
        Path(args.output_root) if args.output_root else None,
    ).run(
        args.date,
        order_sync_path=Path(args.order_sync_path) if args.order_sync_path else None,
    )
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(
            "dualtrack_tiger_human_sync: "
            f"{result['status']} date={result['run_date']} "
            f"imported={result['imported_count']} duplicates={result['duplicate_count']} skipped={result['skipped_count']}"
        )


if __name__ == "__main__":
    main()
