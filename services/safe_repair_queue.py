"""Bounded, auditable recovery proposals with no trading actuator.

This module deliberately owns *only* diagnosis-to-receipt evidence.  It does
not import a broker, control plane, risk port, or process supervisor, so a
queued repair cannot turn into a trading mutation by accident.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from services.journal_store import load_json, write_json


SAFE_REPAIR_DOMAINS = frozenset({"service", "cache", "read_model"})
HUMAN_CONFIRMATION_DOMAINS = frozenset({
    "orders",
    "positions",
    "risk",
    "strategy_plan",
    "execution_engine",
    "market_provider",
})
REPAIR_QUEUE_SCHEMA = "safe-repair-queue-v1"


def build_repair_proposal(
    *,
    diagnosis: str,
    domain: str,
    requested_action: str,
    before_evidence: Mapping[str, Any],
    observed_at: str | None = None,
) -> dict[str, Any]:
    """Build a deterministic, non-executable recovery proposal."""

    clean_diagnosis = _required_text("diagnosis", diagnosis)
    clean_domain = _required_text("domain", domain).lower()
    clean_action = _required_text("requested_action", requested_action)
    evidence = _json_object(before_evidence, "before_evidence")
    authority = (
        "automatic_candidate"
        if clean_domain in SAFE_REPAIR_DOMAINS
        else "requires_human_confirmation"
    )
    receipt_id = _stable_id({
        "diagnosis": clean_diagnosis,
        "domain": clean_domain,
        "requested_action": clean_action,
        "before_evidence": evidence,
    })
    return {
        "schema_version": REPAIR_QUEUE_SCHEMA,
        "repair_id": receipt_id,
        "status": "queued",
        "diagnosis": clean_diagnosis,
        "domain": clean_domain,
        "requested_action": clean_action,
        "before_evidence": evidence,
        "observed_at": observed_at or _now(),
        "authority": {
            "classification": authority,
            "requires_human_confirmation": authority == "requires_human_confirmation",
            "automatic_candidate": authority == "automatic_candidate",
            "execution_allowed": False,
        },
        "safety": {
            "read_only": True,
            "changes_orders": False,
            "changes_positions": False,
            "changes_risk": False,
            "changes_strategy_plan": False,
            "changes_execution_engine": False,
            "changes_market_provider": False,
        },
    }


class SafeRepairQueue:
    """Persist append-only proposals and independent verification receipts."""

    def __init__(self, output_root: Path) -> None:
        self.output_root = Path(output_root)

    @property
    def _folder(self) -> Path:
        return self.output_root / "dualtrack" / "safe_repair_queue"

    @property
    def _proposal_path(self) -> Path:
        return self._folder / "proposals.json"

    @property
    def _receipt_path(self) -> Path:
        return self._folder / "verification_receipts.json"

    def enqueue(self, proposal: Mapping[str, Any]) -> dict[str, Any]:
        """Append one proposal unless its deterministic receipt already exists."""

        row = _json_object(proposal, "proposal")
        _validate_proposal(row)
        rows = load_json(self._proposal_path)
        existing = next((item for item in rows if item.get("repair_id") == row["repair_id"]), None)
        if existing is not None:
            return dict(existing)
        rows.append(row)
        write_json(self._proposal_path, rows)
        return row

    def record_verification(
        self,
        repair_id: str,
        *,
        after_evidence: Mapping[str, Any],
        verified: bool,
        verified_at: str | None = None,
        failure_reason: str = "",
    ) -> dict[str, Any]:
        """Append post-check evidence; this never performs the requested action."""

        clean_id = _required_text("repair_id", repair_id)
        proposals = load_json(self._proposal_path)
        if not any(row.get("repair_id") == clean_id for row in proposals):
            raise ValueError("repair_id is not queued")
        after = _json_object(after_evidence, "after_evidence")
        if not verified and not str(failure_reason).strip():
            raise ValueError("failure_reason is required when verification fails")
        receipt = {
            "schema_version": REPAIR_QUEUE_SCHEMA,
            "verification_id": _stable_id({
                "repair_id": clean_id,
                "after_evidence": after,
                "verified": bool(verified),
                "failure_reason": str(failure_reason).strip(),
            }),
            "repair_id": clean_id,
            "status": "verified" if verified else "verification_failed",
            "after_evidence": after,
            "failure_reason": str(failure_reason).strip(),
            "verified_at": verified_at or _now(),
            "safety": {"read_only": True, "execution_allowed": False},
        }
        rows = load_json(self._receipt_path)
        existing = next((item for item in rows if item.get("verification_id") == receipt["verification_id"]), None)
        if existing is not None:
            return dict(existing)
        rows.append(receipt)
        write_json(self._receipt_path, rows)
        return receipt

    def read_model(self, *, limit: int = 20) -> dict[str, Any]:
        """Return the current queue state without creating, changing, or acting."""

        proposals = [row for row in load_json(self._proposal_path) if isinstance(row, dict)]
        receipts = [row for row in load_json(self._receipt_path) if isinstance(row, dict)]
        latest_receipt = {
            str(row.get("repair_id") or ""): row
            for row in receipts
            if str(row.get("repair_id") or "")
        }
        projected = []
        for proposal in proposals:
            receipt = latest_receipt.get(str(proposal.get("repair_id") or ""))
            projected.append({
                **proposal,
                "verification": receipt or {"status": "pending_verification"},
                "display_status": str((receipt or proposal).get("status") or "queued"),
            })
        records = projected[-max(1, int(limit)):]
        return {
            "schema_version": REPAIR_QUEUE_SCHEMA,
            "records": records,
            "counts": {
                "queued": sum(1 for row in projected if row["display_status"] == "queued"),
                "verified": sum(1 for row in projected if row["display_status"] == "verified"),
                "verification_failed": sum(1 for row in projected if row["display_status"] == "verification_failed"),
                "requires_human_confirmation": sum(
                    1
                    for row in projected
                    if (row.get("authority") or {}).get("requires_human_confirmation") is True
                ),
            },
            "safety": {
                "read_only": True,
                "command_authority": False,
                "automatic_actuator_present": False,
            },
        }


def _validate_proposal(row: dict[str, Any]) -> None:
    required = ("repair_id", "diagnosis", "domain", "requested_action", "before_evidence", "authority", "safety")
    missing = [key for key in required if key not in row]
    if missing:
        raise ValueError(f"repair proposal missing fields: {', '.join(missing)}")
    if (row.get("authority") or {}).get("execution_allowed") is not False:
        raise ValueError("safe repair queue cannot grant execution authority")


def _json_object(value: Mapping[str, Any], name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    try:
        return json.loads(json.dumps(dict(value), sort_keys=True, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be JSON-safe") from exc


def _required_text(name: str, value: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ValueError(f"{name} is required")
    return result


def _stable_id(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(dict(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return f"repair-{hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:20]}"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
