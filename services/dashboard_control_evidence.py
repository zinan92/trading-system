"""Deterministic acceptance/proof projector for Dashboard V5 control flow."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from services.journal_store import load_json, write_json


EVIDENCE_PATH = "dashboard_control_plane/acceptance.json"


def _digest(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _public(value: Any) -> Any:
    forbidden = {
        "private_key",
        "secret",
        "api_key",
        "api_secret",
        "signer",
        "signature",
        "signed_payload",
        "authorization",
        "account_address",
    }
    if isinstance(value, Mapping):
        return {
            str(key): _public(item)
            for key, item in value.items()
            if str(key).strip().lower() not in forbidden
        }
    if isinstance(value, (list, tuple)):
        return [_public(item) for item in value]
    return value


def build_dashboard_control_evidence(
    *,
    catalog: Mapping[str, Any],
    selection: Mapping[str, Any],
    preview: Mapping[str, Any],
    confirmation: Mapping[str, Any],
    runtime: Mapping[str, Any],
) -> dict[str, Any]:
    """Evaluate one selection-to-runtime snapshot at the public seam."""

    blockers: list[str] = []
    checks: list[dict[str, Any]] = []

    def check(name: str, passed: bool, reason: str = "") -> None:
        row = {"name": name, "status": "pass" if passed else "blocked"}
        if reason:
            row["reason"] = reason
            if not passed:
                blockers.append(reason)
        checks.append(row)

    venue_id = str(selection.get("venue_profile_id") or "").strip()
    instrument_id = str(selection.get("instrument_id") or "").strip()
    family = str(selection.get("strategy_family") or "").strip().lower()
    profiles = [row for row in (catalog.get("venue_profiles") or []) if isinstance(row, Mapping)]
    profile = next((row for row in profiles if str(row.get("id") or "") == venue_id), None)
    instrument = next(
        (
            row
            for row in (profile.get("instruments") or [] if isinstance(profile, Mapping) else [])
            if isinstance(row, Mapping) and str(row.get("instrument_id") or "") == instrument_id
        ),
        None,
    )
    check("venue_profile", profile is not None, "venue_profile_missing")
    check("instrument", instrument is not None, "instrument_missing")
    check("instrument_eligible", str((instrument or {}).get("eligibility") or "") == "eligible", "instrument_not_eligible")
    check("strategy_family", family in {"dca", "grid"}, "strategy_family_invalid")
    check("selection_identity", str(preview.get("venue_profile_id") or "") == venue_id, "preview_venue_mismatch")
    check("preview_instrument", str(preview.get("instrument_id") or "") == instrument_id, "preview_instrument_mismatch")
    check("preview_family", str(preview.get("strategy_family") or "").lower() == family, "preview_strategy_mismatch")
    preview_digest = str(preview.get("preview_digest") or "").strip().lower()
    check("preview_ready", preview.get("execution_ready") is True, "preview_not_ready")
    check("preview_non_authorizing", preview.get("authorizing") is False, "preview_authorizing")
    check("preview_digest", bool(preview_digest), "preview_digest_missing")
    check("confirmation_status", str(confirmation.get("status") or "") == "confirmed", "confirmation_missing")
    check("confirmation_digest", str(confirmation.get("preview_digest") or "").lower() == preview_digest, "confirmation_digest_mismatch")
    check("confirmation_instrument", str(confirmation.get("instrument_id") or "") == instrument_id, "confirmation_instrument_mismatch")
    check("confirmation_environment", str(confirmation.get("environment") or "").lower() == "testnet", "confirmation_not_testnet")
    check("runtime_identity", str(runtime.get("instrument_id") or "") == instrument_id, "runtime_instrument_mismatch")
    check("runtime_family", str(runtime.get("strategy_family") or "").lower() == family, "runtime_strategy_mismatch")
    check("runtime_non_mutating", runtime.get("execution_mutation") is False, "runtime_mutation_unproven")

    if any(check["status"] == "blocked" for check in checks):
        status = "blocked"
        next_action = "fix_identity_or_rebuild_preview"
    elif confirmation.get("status") != "confirmed":
        status = "incomplete"
        next_action = "await_operator_confirmation"
    else:
        status = "ready"
        next_action = "await_attended_testnet_proof"
    return {
        "schema_version": "dashboard-control-acceptance-v1",
        "status": status,
        "checks": checks,
        "blockers": sorted(set(blockers)),
        "next_action": next_action,
        "selection": _public(selection),
        "preview": _public(preview),
        "confirmation": _public(confirmation),
        "runtime": _public(runtime),
        "external_testnet_proof": {
            "status": "human_required",
            "strategy_families": ["dca", "grid"],
            "issues": [1050, 1051],
            "orders_submitted_by_this_projector": 0,
        },
        "safety": {
            "orders_submitted": False,
            "credentials_exposed": False,
            "mainnet_live": False,
        },
    }


class DashboardControlEvidenceStore:
    """Append-only local receipt store for deterministic acceptance evidence."""

    def __init__(self, output_root: Path) -> None:
        self.output_root = Path(output_root)
        self.path = self.output_root / EVIDENCE_PATH

    def record(self, snapshot: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(snapshot, Mapping):
            raise ValueError("dashboard_acceptance_snapshot_invalid")
        evidence = build_dashboard_control_evidence(**snapshot)
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        receipt = {
            **evidence,
            "receipt_id": f"dashboard-acceptance:{_digest({**evidence, 'recorded_at': now})[7:19]}",
            "recorded_at": now,
        }
        rows = load_json(self.path)
        if rows and isinstance(rows[-1], Mapping) and rows[-1].get("receipt_id") == receipt["receipt_id"]:
            return dict(rows[-1])
        write_json(self.path, [*rows, receipt])
        return receipt


__all__ = ["DashboardControlEvidenceStore", "build_dashboard_control_evidence"]
