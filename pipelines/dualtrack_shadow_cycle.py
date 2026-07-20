"""Run the complete no-order DualTrack shadow-validation sequence for one cycle."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
from pathlib import Path

from pipelines import dualtrack_nautilus_shadow_replay, dualtrack_shadow_cutover_status, dualtrack_shadow_input_prepare
from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import load_json, write_json


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare, replay and reconcile one DualTrack Nautilus shadow cycle.")
    parser.add_argument("--cycle-id", required=True)
    parser.add_argument("--nautilus-python", required=True)
    parser.add_argument("--output-root", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    output_root = Path(args.output_root) if args.output_root else ROOT / str(load_pipeline_config().get("output_root", "outputs"))
    common = ["--cycle-id", args.cycle_id, "--output-root", str(output_root)]
    with contextlib.redirect_stdout(io.StringIO()):
        prepared = dualtrack_shadow_input_prepare.main(common)
    replayed = 2
    if prepared == 0:
        with contextlib.redirect_stdout(io.StringIO()):
            replayed = dualtrack_nautilus_shadow_replay.main([
                *common,
                "--nautilus-python", args.nautilus_python,
            ])
    with contextlib.redirect_stdout(io.StringIO()):
        gate = dualtrack_shadow_cutover_status.main(["--output-root", str(output_root)])
    candidate_rows = load_json(output_root / "dualtrack" / "nautilus" / "candidates" / f"{args.cycle_id}.json")
    reconciliation_rows = load_json(output_root / "dualtrack" / "reconciliation" / f"{args.cycle_id}.json")
    gate_rows = load_json(output_root / "dualtrack" / "cutover" / "shadow_gate_current.json")
    result = {
        "schema_version": "dualtrack-shadow-cycle-run-v1",
        "cycle_id": args.cycle_id,
        "status": "replayed" if prepared == 0 and replayed == 0 else "blocked",
        "prepare_exit_code": prepared,
        "replay_exit_code": replayed,
        "cutover_gate_exit_code": gate,
        "candidate": candidate_rows[-1] if candidate_rows else {},
        "reconciliation": reconciliation_rows[-1] if reconciliation_rows else {},
        "cutover_gate": gate_rows[-1] if gate_rows else {},
        "real_money_eligible": False,
    }
    write_json(output_root / "dualtrack" / "shadow_runs" / f"{args.cycle_id}.json", [result])
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"dualtrack_shadow_cycle: cycle_id={args.cycle_id} status={result['status']}")
    return 0 if result["status"] == "replayed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
