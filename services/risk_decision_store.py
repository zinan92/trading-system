"""Append-only audit persistence adapter for canonical risk decisions."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from schemas.risk import RiskDecision, validate_risk_decision
from services.journal_store import load_json, write_json


class FileRiskDecisionStore:
    """Persist audit evidence without exposing an authorization read API."""

    name = "file_risk_decision_audit"

    def __init__(self, output_root: Path) -> None:
        self.output_root = Path(output_root)

    def persist(
        self,
        decision: RiskDecision | Mapping[str, Any],
    ) -> dict[str, Any]:
        current = validate_risk_decision(decision).to_dict()
        root = self._root(str(current.get("scope") or ""))
        candidate = current.get("request", {}).get("candidate", {})
        identity = _safe_filename(
            str(candidate.get("cycle_id") or candidate.get("run_date") or "unscoped")
        )
        history_path = root / f"{identity}.json"
        rows = [row for row in load_json(history_path) if isinstance(row, dict)]
        if not any(
            str(row.get("decision_id") or "") == current["decision_id"]
            for row in rows
        ):
            rows.append(current)
            write_json(history_path, rows)
        write_json(root / "current.json", [current])
        return current

    def _root(self, scope: str) -> Path:
        if scope.startswith("paper_"):
            return self.output_root / "dualtrack" / "risk_decisions"
        return self.output_root / "risk_decisions"


def _safe_filename(value: str) -> str:
    rendered = "".join(
        character
        for character in value
        if character.isalnum() or character in {"-", "_"}
    )
    return rendered or "unscoped"
