"""Run an isolated Nautilus shadow replay and record its exact parity result."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

from services.config_loader import ROOT, load_pipeline_config
from services.dualtrack_execution_adapter import build_execution_engine_adapter
from services.dualtrack_shadow_reconciliation import DualTrackShadowReconciler
from services.journal_store import load_json


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay one prepared DualTrack cycle in an isolated Nautilus runtime.")
    parser.add_argument("--cycle-id", required=True)
    parser.add_argument("--nautilus-python", required=True, help="Path to the isolated Python containing NautilusTrader")
    parser.add_argument("--output-root", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    output_root = Path(args.output_root) if args.output_root else ROOT / str(load_pipeline_config().get("output_root", "outputs"))
    input_path = output_root / "dualtrack" / "shadow_inputs" / f"{args.cycle_id}.json"
    preflight_path = output_root / "dualtrack" / "nautilus" / "instrument_preflight.json"
    candidate_path = output_root / "dualtrack" / "nautilus" / "candidates" / f"{args.cycle_id}.json"
    if not input_path.exists() or not preflight_path.exists():
        return _blocked(args, "shadow_input_or_instrument_preflight_missing")
    replay_script = ROOT / "spikes" / "dualtrack_nautilus_shadow_replay.py"
    command = [str(args.nautilus_python), str(replay_script), "--preflight", str(preflight_path), "--input", str(input_path), "--output", str(candidate_path)]
    environment = dict(os.environ)
    existing_pythonpath = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = str(ROOT) if not existing_pythonpath else f"{ROOT}{os.pathsep}{existing_pythonpath}"
    result = subprocess.run(command, cwd=ROOT, env=environment, capture_output=True, text=True, check=False)
    if result.returncode:
        return _blocked(args, "nautilus_shadow_replay_failed", detail=result.stderr[-1000:])
    rows = load_json(candidate_path)
    if not rows or not isinstance(rows[-1], dict):
        return _blocked(args, "nautilus_candidate_missing_after_replay")
    candidate = rows[-1]
    last_event = (load_json(input_path)[-1].get("market_events") or [])[-1]
    adapter = build_execution_engine_adapter(output_root)
    authoritative = adapter.snapshot(
        args.cycle_id,
        mark_price=last_event["price"],
        mark_fresh=True,
        mark_source=last_event["source"],
    )
    authoritative["reconciliation"] = adapter.reconcile(args.cycle_id)
    report = DualTrackShadowReconciler(output_root).record(args.cycle_id, authoritative=authoritative, candidate=candidate)
    payload = {"cycle_id": args.cycle_id, "candidate": candidate, "reconciliation": report}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"dualtrack_nautilus_shadow_replay: cycle_id={args.cycle_id} status={report['status']}")
    return 0 if report["status"] == "pass" else 2


def _blocked(args: argparse.Namespace, blocker: str, *, detail: str = "") -> int:
    payload = {"cycle_id": args.cycle_id, "status": "blocked", "blocker": blocker}
    if detail:
        payload["detail"] = detail
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"dualtrack_nautilus_shadow_replay: cycle_id={args.cycle_id} status=blocked blocker={blocker}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
