"""Execute the fixed Nautilus parity fixtures and publish their cutover gate."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import load_json, write_json


FIXTURE_CLASSES: dict[str, tuple[str, ...]] = {
    "market_entry_long_short": ("long_stop", "short_stop"),
    "limit_entry_waits_for_touch": ("limit_entry_waits_for_touch",),
    "scale_in_weighted_average": ("scale_in_weighted_average",),
    "partial_reduction_then_close": ("partial_reduction_then_close",),
    "stop_loss_and_take_profit": ("long_stop", "long_target", "short_stop", "short_target"),
    "same_bar_conservative_priority": ("long_same_bar_stop_first",),
    "fees_slippage_margin_exposure_pnl": ("scale_in_weighted_average", "partial_reduction_then_close"),
    "duplicate_command_event_replay": ("duplicate_command_event_replay",),
    "restart_and_reconciliation": ("restart_replay_and_reconciliation",),
    "historical_machine_residual_units": (),
}


def build_fixture_gate(output_root: Path) -> dict[str, Any]:
    root = Path(output_root) / "dualtrack" / "nautilus" / "parity"
    classes: list[dict[str, Any]] = []
    for name, scenarios in FIXTURE_CLASSES.items():
        if name == "historical_machine_residual_units":
            rows = load_json(root / "historical_machine_residual_units.json")
            row = rows[-1] if rows else {}
            classes.append({"class": name, "status": str(row.get("status") or "missing"), "scenarios": []})
            continue
        if not scenarios:
            classes.append({"class": name, "status": "not_run", "scenarios": []})
            continue
        results = []
        for scenario in scenarios:
            rows = load_json(root / f"{scenario}.json")
            row = rows[-1] if rows else {}
            results.append({"scenario": scenario, "status": str((row.get("parity") or {}).get("status") or "missing")})
        status = "pass" if all(row["status"] == "pass" for row in results) else "drift"
        classes.append({"class": name, "status": status, "scenarios": results})
    blockers = [row["class"] for row in classes if row["status"] != "pass"]
    return {
        "schema_version": "dualtrack-nautilus-parity-fixture-gate-v1",
        "scope": "paper_shadow_only",
        "status": "pass" if not blockers else "blocked",
        "blockers": blockers,
        "classes": classes,
        "real_money_eligible": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run fixed DualTrack Nautilus parity fixtures.")
    parser.add_argument("--nautilus-python", required=True)
    parser.add_argument("--output-root", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    output_root = Path(args.output_root) if args.output_root else ROOT / str(load_pipeline_config().get("output_root", "outputs"))
    preflight = output_root / "dualtrack" / "nautilus" / "instrument_preflight.json"
    if not preflight.exists():
        result = _blocked("instrument_preflight_missing")
        return _write(output_root, result, args.json)

    script = ROOT / "spikes" / "dualtrack_nautilus_gold_parity_fixture.py"
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(ROOT) if not environment.get("PYTHONPATH") else f"{ROOT}{os.pathsep}{environment['PYTHONPATH']}"
    for scenarios in FIXTURE_CLASSES.values():
        for scenario in scenarios:
            output = output_root / "dualtrack" / "nautilus" / "parity" / f"{scenario}.json"
            command = [str(args.nautilus_python), str(script), "--preflight", str(preflight), "--scenario", scenario, "--output", str(output)]
            completed = subprocess.run(command, cwd=ROOT, env=environment, capture_output=True, text=True, check=False)
            if completed.returncode:
                result = _blocked(f"fixture_runtime_failed:{scenario}", detail=completed.stderr[-1000:])
                return _write(output_root, result, args.json)
    residual_check = ROOT / "pipelines" / "dualtrack_machine_residual_check.py"
    residual = subprocess.run([os.environ.get("PYTHON", "python3"), str(residual_check), "--output-root", str(output_root)],
                              cwd=ROOT, env=environment, capture_output=True, text=True, check=False)
    if residual.returncode:
        result = _blocked("historical_machine_residual_check_failed", detail=residual.stderr[-1000:])
        return _write(output_root, result, args.json)
    return _write(output_root, build_fixture_gate(output_root), args.json)


def _blocked(blocker: str, *, detail: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema_version": "dualtrack-nautilus-parity-fixture-gate-v1",
        "scope": "paper_shadow_only",
        "status": "blocked",
        "blockers": [blocker],
        "classes": [],
        "real_money_eligible": False,
    }
    if detail:
        result["detail"] = detail
    return result


def _write(output_root: Path, result: dict[str, Any], as_json: bool) -> int:
    write_json(Path(output_root) / "dualtrack" / "nautilus" / "parity" / "current.json", [result])
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"dualtrack_nautilus_parity_gate: status={result['status']} blockers={','.join(result['blockers'])}")
    return 0 if result["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
