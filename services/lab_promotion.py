"""Promotion gate helpers for Strategy Lab.

This module only reads lab evidence and can write a lab-scoped eligibility
artifact. It never mutates strategy config or enables production execution.
"""

from __future__ import annotations

from pathlib import Path

from services.journal_store import load_json, write_json
from services.lab_execution_conformance import (
    lab_execution_conformance_token_blockers,
    load_lab_execution_conformance,
)


def promotion_gate(
    entry: dict,
    *,
    execution_conformance: dict | None = None,
) -> dict:
    objective = entry.get("objective", {}) if isinstance(entry.get("objective", {}), dict) else {}
    walkforward = objective.get("walkforward") if isinstance(objective.get("walkforward"), dict) else objective
    holdout = objective.get("holdout") if isinstance(objective.get("holdout"), dict) else {}
    walkforward_passed = bool(walkforward.get("passed", False))
    holdout_passed = bool(holdout.get("passed", False))
    holdout_consumed = bool(entry.get("holdout_consumed", False))
    conformance = dict(execution_conformance or {})
    conformance_blockers = lab_execution_conformance_token_blockers(conformance, entry=entry)
    paper_eligible = (
        entry.get("status") == "valid"
        and walkforward_passed
        and holdout_passed
        and holdout_consumed
        and not conformance_blockers
    )
    blockers = []
    if entry.get("status") != "valid":
        blockers.append("lab_entry_not_valid")
    if not walkforward_passed:
        blockers.append("walkforward_objective_not_passed")
    if not holdout_consumed:
        blockers.append("holdout_not_consumed")
    if not holdout_passed:
        blockers.append("holdout_objective_not_passed")
    blockers.extend(conformance_blockers)
    return {
        "paper_eligible": paper_eligible,
        "status": "paper_eligible" if paper_eligible else "blocked",
        "blockers": blockers,
        "source_exp_id": entry.get("exp_id", ""),
        "family": entry.get("family", ""),
        "strategy_ref": entry.get("strategy_ref", {}),
        "metrics": walkforward.get("metrics", {}),
        "holdout_metrics": holdout.get("metrics", {}),
        "execution_conformance": conformance,
        **_lab_expectation_fields(entry),
    }


def lab_expectation_for_strategy(output_root: Path, strategy_id: str) -> dict:
    entries = _entries(output_root)
    candidates = [
        entry for entry in entries
        if (entry.get("strategy_ref", {}) or {}).get("strategy_id") == strategy_id
    ]
    if not candidates:
        return {"status": "missing", "paper_eligible": False, "blockers": ["no_lab_evidence"], "source_exp_id": ""}
    ranked = sorted(candidates, key=lambda item: str(item.get("updated_at", item.get("created_at", ""))), reverse=True)
    entry = ranked[0]
    return promotion_gate(
        entry,
        execution_conformance=load_lab_execution_conformance(output_root, entry),
    )


def record_paper_eligibility(output_root: Path, strategy_id: str) -> dict:
    payload = lab_expectation_for_strategy(output_root, strategy_id)
    write_json(Path(output_root) / "lab" / "promotion" / f"{strategy_id}.json", [payload])
    return payload


def _entries(output_root: Path) -> list[dict]:
    rows = load_json(Path(output_root) / "lab" / "registry.json")
    if not rows:
        return []
    refs = rows[0].get("experiments", [])
    entries = []
    for ref in refs:
        exp_id = ref.get("exp_id")
        if not exp_id:
            continue
        entry_rows = load_json(Path(output_root) / "lab" / "experiments" / f"{exp_id}.json")
        if entry_rows:
            entries.append(entry_rows[0])
    return entries


def _lab_expectation_fields(entry: dict) -> dict:
    expectation = entry.get("lab_expectation")
    return expectation if isinstance(expectation, dict) else {}
