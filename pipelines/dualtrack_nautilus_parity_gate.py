"""Execute the fixed Nautilus parity fixtures and publish their cutover gate."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from services.config_loader import ROOT, load_pipeline_config
from services.dualtrack_nautilus_parity_contract import (
    EXPECTED_NAUTILUS_VERSION,
    FIXTURE_CLASSES,
    PLATFORM_PARITY_MAX_AGE_SECONDS,
    PLATFORM_PARITY_SCHEMA,
    platform_parity_code_hash,
)
from services.journal_store import load_json, write_json


def build_fixture_gate(output_root: Path) -> dict[str, Any]:
    root = Path(output_root) / "dualtrack" / "nautilus" / "parity"
    scenario_names = sorted({scenario for scenarios in FIXTURE_CLASSES.values() for scenario in scenarios})
    artifacts = {
        scenario: (rows[-1] if (rows := load_json(root / f"{scenario}.json")) else {})
        for scenario in scenario_names
    }
    residual_rows = load_json(root / "historical_machine_residual_units.json")
    residual_artifact = residual_rows[-1] if residual_rows else {}
    classes: list[dict[str, Any]] = []
    for name, scenarios in FIXTURE_CLASSES.items():
        if name == "historical_machine_residual_units":
            row = residual_artifact
            classes.append({"class": name, "status": str(row.get("status") or "missing"), "scenarios": []})
            continue
        if not scenarios:
            classes.append({"class": name, "status": "not_run", "scenarios": []})
            continue
        results = []
        for scenario in scenarios:
            row = artifacts[scenario]
            results.append({"scenario": scenario, "status": str((row.get("parity") or {}).get("status") or "missing")})
        status = "pass" if all(row["status"] == "pass" for row in results) else "drift"
        classes.append({"class": name, "status": status, "scenarios": results})
    blockers = [row["class"] for row in classes if row["status"] != "pass"]
    contract_rows = [
        row.get("execution_contract")
        for row in artifacts.values()
        if isinstance(row.get("execution_contract"), dict)
    ]
    contract_pairs = {
        (
            str(row.get("contract_hash") or ""),
            str(row.get("fee_contract_hash") or ""),
        )
        for row in contract_rows
        if row.get("contract_hash") and row.get("fee_contract_hash")
    }
    version_rows = [str(row.get("nautilus_version") or "") for row in artifacts.values()]
    versions = {version for version in version_rows if version}
    try:
        current_code_hash = platform_parity_code_hash()
    except OSError:
        current_code_hash = ""
    evidence_artifacts = [*artifacts.values(), residual_artifact]
    evidence_code_hashes = [str(row.get("platform_code_hash") or "") for row in evidence_artifacts]
    evidence_times = [_timestamp(row.get("generated_at")) for row in evidence_artifacts]
    now = datetime.now(timezone.utc)
    if len(contract_rows) != len(artifacts) or len(contract_pairs) != 1:
        blockers.append("execution_contract_evidence")
    if (
        len(version_rows) != len(artifacts)
        or any(not version for version in version_rows)
        or versions != {EXPECTED_NAUTILUS_VERSION}
    ):
        blockers.append("nautilus_runtime_evidence")
    if not current_code_hash or any(code_hash != current_code_hash for code_hash in evidence_code_hashes):
        blockers.append("platform_code_evidence")
    if any(generated_at is None for generated_at in evidence_times):
        blockers.append("fixture_timestamp_evidence")
    else:
        timestamps = [generated_at for generated_at in evidence_times if generated_at is not None]
        if any(generated_at > now + timedelta(minutes=5) for generated_at in timestamps):
            blockers.append("fixture_timestamp_future")
        if any(
            (now - generated_at).total_seconds() > PLATFORM_PARITY_MAX_AGE_SECONDS
            for generated_at in timestamps
        ):
            blockers.append("fixture_timestamp_stale")
    contracts = {}
    if len(contract_pairs) == 1:
        execution_hash, fee_hash = next(iter(contract_pairs))
        contracts = {
            "execution_contract_hash": execution_hash,
            "fee_contract_hash": fee_hash,
        }
    return {
        "schema_version": PLATFORM_PARITY_SCHEMA,
        "scope": "paper_shadow_only",
        "status": "pass" if not blockers else "blocked",
        "blockers": sorted(set(blockers)),
        "classes": classes,
        "generated_at": (
            min(generated_at for generated_at in evidence_times if generated_at is not None).isoformat()
            if all(generated_at is not None for generated_at in evidence_times)
            else ""
        ),
        "platform_code_hash": current_code_hash,
        "nautilus_version": next(iter(versions)) if len(versions) == 1 else "",
        "contracts": contracts,
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
        "schema_version": PLATFORM_PARITY_SCHEMA,
        "scope": "paper_shadow_only",
        "status": "blocked",
        "blockers": [blocker],
        "classes": [],
        "real_money_eligible": False,
    }
    if detail:
        result["detail"] = detail
    return result


def _timestamp(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _write(output_root: Path, result: dict[str, Any], as_json: bool) -> int:
    write_json(Path(output_root) / "dualtrack" / "nautilus" / "parity" / "current.json", [result])
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"dualtrack_nautilus_parity_gate: status={result['status']} blockers={','.join(result['blockers'])}")
    return 0 if result["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
