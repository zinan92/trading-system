"""Experiment registry for Strategy Lab.

Every experiment is written as ``pending`` before results are attached. JSON
writes go through ``journal_store.write_json`` for atomic replacement.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from services.journal_store import load_json, write_json


class LabRegistry:
    def __init__(self, output_root: Path) -> None:
        self.output_root = Path(output_root)
        self.lab_root = self.output_root / "lab"
        self.registry_path = self.lab_root / "registry.json"
        self.experiments_dir = self.lab_root / "experiments"

    def start(self, spec: dict, exp_id: str | None = None) -> dict:
        stable_spec = _canonical(spec)
        exp_id = exp_id or f"lab_{hashlib.sha256(stable_spec.encode('utf-8')).hexdigest()[:16]}"
        now = _now()
        entry = {
            "exp_id": exp_id,
            "created_at": now,
            "updated_at": now,
            "status": "pending",
            "hypothesis": str(spec.get("hypothesis", "")),
            "family": str(spec.get("family", "unknown")),
            "strategy_ref": spec.get("strategy_ref", {}),
            "params_diff": spec.get("params_diff", {}),
            "data_range": spec.get("data_range", {}),
            "windows": spec.get("windows", {}),
            "cost_grid_results": {},
            "regime_slice_results": {},
            "objective": {},
            "notes": list(spec.get("notes", [])),
            "spec_hash": hashlib.sha256(stable_spec.encode("utf-8")).hexdigest(),
            "holdout_consumed": False,
        }
        self._write_entry(entry)
        self._upsert_index(entry)
        return entry

    def finalize(self, exp_id: str, *, status: str, results: dict | None = None, notes: list[str] | None = None) -> dict:
        if status not in {"valid", "invalid", "error"}:
            raise ValueError("final status must be valid, invalid, or error")
        entry = self.load(exp_id)
        if not entry:
            raise ValueError(f"unknown experiment: {exp_id}")
        results = results or {}
        updated = {
            **entry,
            "updated_at": _now(),
            "status": status,
            "cost_grid_results": results.get("cost_grid_results", entry.get("cost_grid_results", {})),
            "regime_slice_results": results.get("regime_slice_results", entry.get("regime_slice_results", {})),
            "objective": results.get("objective", entry.get("objective", {})),
            "reports": results.get("reports", entry.get("reports", {})),
            "notes": [*list(entry.get("notes", [])), *list(notes or [])],
        }
        for key in (
            "break_even_bp",
            "binance_reality",
            "data_coverage",
            "holdout",
            "paper_eligibility",
            "r1_summary",
            "r2_summary",
            "r3_summary",
            "r4_summary",
            "chan_summary",
            "replay",
            "model_report",
            "lab_expectation",
        ):
            if key in results:
                updated[key] = results[key]
        self._write_entry(updated)
        self._upsert_index(updated)
        return updated

    def record_holdout_consumption(self, exp_id: str, data_range: dict) -> dict:
        entry = self.load(exp_id)
        if not entry:
            raise ValueError(f"unknown experiment: {exp_id}")
        updated = {
            **entry,
            "updated_at": _now(),
            "holdout_consumed": True,
            "holdout_consumption": {
                "consumed_at": _now(),
                "family": entry.get("family", "unknown"),
                "data_range": data_range,
            },
        }
        self._write_entry(updated)
        self._upsert_index(updated)
        return updated

    def load(self, exp_id: str) -> dict:
        rows = load_json(self.experiments_dir / f"{exp_id}.json")
        return rows[0] if rows else {}

    def entries(self) -> list[dict]:
        rows = load_json(self.registry_path)
        if not rows:
            return []
        return list(rows[0].get("experiments", []))

    def trial_count(self, family: str) -> int:
        return sum(1 for item in self.entries() if item.get("family") == family)

    def _write_entry(self, entry: dict) -> None:
        write_json(self.experiments_dir / f"{entry['exp_id']}.json", [entry])

    def _upsert_index(self, entry: dict) -> None:
        rows = load_json(self.registry_path)
        index = rows[0] if rows else {"experiments": []}
        experiments = [item for item in index.get("experiments", []) if item.get("exp_id") != entry["exp_id"]]
        experiments.append({
            "exp_id": entry["exp_id"],
            "family": entry.get("family", "unknown"),
            "hypothesis": entry.get("hypothesis", ""),
            "status": entry.get("status", "pending"),
            "created_at": entry.get("created_at", ""),
            "updated_at": entry.get("updated_at", ""),
            "holdout_consumed": bool(entry.get("holdout_consumed", False)),
        })
        index["experiments"] = sorted(experiments, key=lambda item: item["exp_id"])
        index["updated_at"] = _now()
        write_json(self.registry_path, [index])


def _canonical(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
