from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from services.config_loader import ROOT, load_pipeline_config
from services.dualtrack_clock import cycle_window_from_id, parse_utc
from services.journal_store import load_json, write_json


class DualTrackEvidenceGapStore:
    def __init__(self, output_root: Path | None = None) -> None:
        config = load_pipeline_config()
        self.output_root = Path(output_root) if output_root else ROOT / str(config.get("output_root", "outputs"))
        self.root = self.output_root / "dualtrack"

    def record(
        self,
        cycle_id: str,
        *,
        reason: str,
        detected_at: str | datetime | None = None,
    ) -> dict[str, Any]:
        now = parse_utc(detected_at)
        window = cycle_window_from_id(cycle_id)
        if now < window.end:
            raise ValueError("cannot declare evidence gap before cycle end")
        if load_json(self.root / "attribution" / f"{cycle_id}.json"):
            raise ValueError("cannot declare evidence gap when attribution exists")
        reason_code = str(reason or "").strip()
        if not reason_code:
            raise ValueError("reason is required")

        artifact_paths = {
            "runner": self.root / "runner" / f"{cycle_id}.json",
            "machine_fills": self.root / "fills" / f"{cycle_id}_machine.json",
            "machine_account": self.root / "accounts" / f"{cycle_id}_machine.json",
            "machine_review": self.root / "reviews" / f"{cycle_id}_machine.json",
            "attribution": self.root / "attribution" / f"{cycle_id}.json",
        }
        artifact_status = {
            name: {
                "path": str(path),
                "exists": path.exists(),
                "row_count": len(load_json(path)) if path.exists() else 0,
            }
            for name, path in artifact_paths.items()
        }
        payload = {
            "schema_version": "dualtrack-evidence-gap-v1",
            "cycle_id": cycle_id,
            "status": "evidence_gap",
            "reason": reason_code,
            "detected_at": now.isoformat(),
            "cycle_start": window.start.isoformat(),
            "cycle_end": window.end.isoformat(),
            "counts_as_closed_loop": False,
            "eligible_for_paper_pnl": False,
            "eligible_for_self_evolution": False,
            "reconstruction_policy": "do_not_synthesize_missing_execution_evidence",
            "artifacts": artifact_status,
        }
        write_json(self.root / "evidence_gaps" / f"{cycle_id}.json", [payload])
        return payload

    def load(self, cycle_id: str) -> dict[str, Any]:
        rows = load_json(self.root / "evidence_gaps" / f"{cycle_id}.json")
        return rows[-1] if rows else {}
