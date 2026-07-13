from __future__ import annotations

import argparse
import json

from services.dualtrack_evidence_gap import DualTrackEvidenceGapStore


def main() -> None:
    parser = argparse.ArgumentParser(description="Declare closed DualTrack cycles whose execution evidence is missing.")
    parser.add_argument("--cycle", action="append", required=True, help="Cycle id; may be supplied more than once.")
    parser.add_argument("--reason", required=True)
    parser.add_argument("--as-of", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    store = DualTrackEvidenceGapStore()
    rows = [store.record(cycle_id, reason=args.reason, detected_at=args.as_of or None) for cycle_id in args.cycle]
    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return
    for row in rows:
        print(f"evidence_gap: {row['cycle_id']} reason={row['reason']}")


if __name__ == "__main__":
    main()
