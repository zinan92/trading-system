"""Persist exact, engine-neutral execution parity reports for DualTrack."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from services.dualtrack_execution_contract import EXECUTION_RECONCILIATION_SCHEMA, compare_execution_snapshots
from services.journal_store import write_json


class DualTrackShadowReconciler:
    """Compare a candidate engine only after it has produced a real snapshot.

    A missing candidate is a blocked result, never a pass by absence.  This is
    the bridge from the current legacy ledger to a future Nautilus shadow run.
    """

    def __init__(self, output_root: Path) -> None:
        self.output_root = Path(output_root)

    def record(
        self,
        cycle_id: str,
        *,
        authoritative: dict[str, Any],
        candidate: dict[str, Any] | None,
        candidate_reason: str = "candidate_snapshot_missing",
    ) -> dict[str, Any]:
        if candidate is None:
            report = {
                "schema_version": EXECUTION_RECONCILIATION_SCHEMA,
                "cycle_id": cycle_id,
                "authoritative_engine": str(authoritative.get("engine") or ""),
                "candidate_engine": "",
                "status": "blocked",
                "blocker": candidate_reason,
                "tolerance": {"mode": "exact", "value": 0},
                "differences": [],
            }
        else:
            report = compare_execution_snapshots(authoritative, candidate, cycle_id=cycle_id)
            evidence = candidate.get("shadow_evidence")
            if isinstance(evidence, dict):
                report["shadow_evidence"] = dict(evidence)
        write_json(self.output_root / "dualtrack" / "reconciliation" / f"{cycle_id}.json", [report])
        write_json(self.output_root / "dualtrack" / "reconciliation" / "current.json", [report])
        return report
