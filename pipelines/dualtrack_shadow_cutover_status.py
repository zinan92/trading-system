"""Report whether shadow execution has met its paper-only cutover evidence gate.

This command is intentionally read-only with respect to execution. It only
reads per-cycle reconciliation reports and writes an operator-facing gate
artifact; it never changes the configured engine or submits an order.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import load_json, write_json


REQUIRED_CONSECUTIVE_PASSES = 7


def build_cutover_status(output_root: Path, *, required_passes: int = REQUIRED_CONSECUTIVE_PASSES) -> dict[str, Any]:
    fixture_rows = load_json(Path(output_root) / "dualtrack" / "nautilus" / "parity" / "current.json")
    fixture_gate = fixture_rows[-1] if fixture_rows else {}
    reports = _cycle_reports(Path(output_root) / "dualtrack" / "reconciliation")
    consecutive_passes = 0
    for report in reversed(reports):
        if report.get("status") != "pass" or not bool((report.get("shadow_evidence") or {}).get("qualifies_for_cutover")):
            break
        consecutive_passes += 1

    latest = reports[-1] if reports else {}
    fixtures_pass = fixture_gate.get("status") == "pass"
    ready = fixtures_pass and bool(reports) and consecutive_passes >= required_passes
    blocker = "" if ready else _blocker(latest, consecutive_passes, required_passes, fixtures_pass=fixtures_pass)
    return {
        "schema_version": "dualtrack-shadow-cutover-gate-v1",
        "scope": "paper_only",
        "configured_engine_changed": False,
        "required_consecutive_passes": required_passes,
        "observed_consecutive_passes": consecutive_passes,
        "status": "ready_for_attended_paper_switch" if ready else "blocked",
        "blocker": blocker,
        "latest_cycle_id": str(latest.get("cycle_id") or ""),
        "latest_reconciliation_status": str(latest.get("status") or "missing"),
        "fixture_gate_status": str(fixture_gate.get("status") or "missing"),
        "fixture_gate_blockers": list(fixture_gate.get("blockers") or []),
        "considered_cycles": [str(report.get("cycle_id") or "") for report in reports],
        "real_money_eligible": False,
    }


def _cycle_reports(directory: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not directory.exists():
        return rows
    for path in sorted(directory.glob("*.json")):
        if path.name == "current.json":
            continue
        payload = load_json(path)
        if payload and isinstance(payload[-1], dict):
            rows.append(payload[-1])
    return sorted(rows, key=lambda report: str(report.get("cycle_id") or ""))


def _blocker(latest: dict[str, Any], consecutive_passes: int, required_passes: int, *, fixtures_pass: bool) -> str:
    if not fixtures_pass:
        return "fixed_parity_fixtures_not_passed"
    if not latest:
        return "reconciliation_history_missing"
    if latest.get("status") != "pass":
        return str(latest.get("blocker") or f"latest_reconciliation_{latest.get('status') or 'missing'}")
    if not bool((latest.get("shadow_evidence") or {}).get("qualifies_for_cutover")):
        return "candidate_activity_insufficient"
    return f"requires_{required_passes}_consecutive_passes_observed_{consecutive_passes}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate the read-only DualTrack shadow cutover gate.")
    parser.add_argument("--output-root", default="")
    parser.add_argument("--required-passes", type=int, default=REQUIRED_CONSECUTIVE_PASSES)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.required_passes < 1:
        raise ValueError("--required-passes must be positive")

    output_root = Path(args.output_root) if args.output_root else ROOT / str(load_pipeline_config().get("output_root", "outputs"))
    result = build_cutover_status(output_root, required_passes=args.required_passes)
    write_json(output_root / "dualtrack" / "cutover" / "shadow_gate_current.json", [result])
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"dualtrack_shadow_cutover_status: status={result['status']} blocker={result['blocker'] or 'none'}")
    return 0 if result["status"] == "ready_for_attended_paper_switch" else 2


if __name__ == "__main__":
    raise SystemExit(main())
