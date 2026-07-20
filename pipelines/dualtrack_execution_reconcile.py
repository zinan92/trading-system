"""Run one safe, file-backed DualTrack execution parity comparison."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from services.config_loader import ROOT, load_pipeline_config
from services.dualtrack_execution_adapter import build_execution_engine_adapter
from services.dualtrack_shadow_reconciliation import DualTrackShadowReconciler
from services.journal_store import load_json


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Record an exact DualTrack execution shadow-parity report.")
    parser.add_argument("--cycle-id", required=True, help="YYYY-MM-DD_DAY or YYYY-MM-DD_NIGHT")
    parser.add_argument("--candidate-path", default="", help="Optional JSON candidate snapshot from the shadow engine")
    parser.add_argument("--mark-price", type=float, default=None)
    parser.add_argument("--mark-fresh", action="store_true")
    parser.add_argument("--mark-source", default="")
    parser.add_argument("--output-root", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    output_root = Path(args.output_root) if args.output_root else ROOT / str(load_pipeline_config().get("output_root", "outputs"))
    adapter = build_execution_engine_adapter(output_root)
    authoritative = adapter.snapshot(
        args.cycle_id,
        mark_price=args.mark_price,
        mark_fresh=args.mark_fresh,
        mark_source=args.mark_source,
    )
    authoritative["reconciliation"] = adapter.reconcile(args.cycle_id)
    candidate = _load_snapshot(args.candidate_path) if args.candidate_path else None
    report = DualTrackShadowReconciler(output_root).record(
        args.cycle_id,
        authoritative=authoritative,
        candidate=candidate,
        candidate_reason="candidate_snapshot_missing" if not args.candidate_path else "candidate_snapshot_invalid",
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(
            "dualtrack_execution_reconcile: "
            f"cycle_id={report['cycle_id']} status={report['status']} "
            f"authoritative={report['authoritative_engine']} candidate={report['candidate_engine'] or 'missing'}"
        )
    return 0 if report["status"] == "pass" else 2


def _load_snapshot(path_value: str) -> dict[str, Any]:
    rows = load_json(Path(path_value))
    if isinstance(rows, list):
        if not rows:
            raise ValueError("candidate snapshot file is empty")
        rows = rows[-1]
    if not isinstance(rows, dict):
        raise ValueError("candidate snapshot must be a JSON object")
    return rows


if __name__ == "__main__":
    raise SystemExit(main())
