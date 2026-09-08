"""Read the durable Park confirmation projections used by Testnet proof CLIs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from services.journal_store import load_json


class DurableParkConfirmationError(ValueError):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


def _rows(path: Path) -> list[dict[str, Any]]:
    try:
        if path.suffix == ".jsonl":
            value = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        else:
            value = load_json(path)
    except Exception as exc:  # noqa: BLE001 - callers expose only a blocker.
        raise DurableParkConfirmationError("durable_confirmation_unavailable") from exc
    values = value if isinstance(value, list) else [value]
    return [dict(row) for row in values if isinstance(row, Mapping)]


def parse_durable_confirmation(
    output_root: Path,
    *,
    plan: Mapping[str, Any],
    confirmation: Mapping[str, Any],
) -> dict[str, Any]:
    """Return a canonical confirmation from Dashboard or the historical ledger."""

    if not isinstance(plan, Mapping) or not isinstance(confirmation, Mapping):
        raise DurableParkConfirmationError("confirmation_identity_invalid")
    digest = str(plan.get("plan_digest") or "")

    if confirmation.get("confirmation_source") == "dashboard":
        if str(confirmation.get("plan_digest") or "") != digest:
            raise DurableParkConfirmationError("dashboard_plan_digest_mismatch")
        if (
            confirmation.get("event") != "confirmed"
            or confirmation.get("execution_authorized") is not True
            or str(confirmation.get("execution_environment") or "").lower() != "testnet"
            or not str(confirmation.get("confirmation_id") or "").strip()
            or confirmation.get("confirmed_at") in (None, "")
            or str(confirmation.get("proposal_id") or "") != digest
            or not str(confirmation.get("receipt_digest") or "").strip()
        ):
            raise DurableParkConfirmationError("dashboard_confirmation_identity_invalid")
        rows = _rows(output_root / "dashboard_control_plane/confirmations.json")
        digest_rows = [
            item for item in rows
            if item.get("status") == "confirmed"
            and str(item.get("plan_digest") or item.get("preview_digest") or "") == digest
        ]
        row = next(
            (
                row for row in reversed(rows)
                if row.get("status") == "confirmed"
                and row.get("activation_id") == confirmation.get("activation_id")
                and str(row.get("plan_digest") or row.get("preview_digest") or "") == digest
            ),
            None,
        )
        if row is None:
            reason = "activation_identity_mismatch" if digest_rows else "dashboard_confirmation_not_durable"
            raise DurableParkConfirmationError(reason)
        if str(row.get("preview_digest") or row.get("plan_digest") or "") != digest:
            raise DurableParkConfirmationError("dashboard_plan_digest_mismatch")
        if row.get("acknowledged") is not True:
            raise DurableParkConfirmationError("dashboard_confirmation_not_acknowledged")
        if str(row.get("operator_id") or "").strip().lower() != "park":
            raise DurableParkConfirmationError("dashboard_operator_invalid")
        current = _rows(output_root / "testnet_automation/current.json")
        current_match = next((item for item in reversed(current) if item.get("activation_id") == row.get("activation_id")), None)
        if current_match is None or str(current_match.get("plan_digest") or "") != digest:
            raise DurableParkConfirmationError("activation_identity_mismatch")
        if str(confirmation.get("operator_id") or "").strip().lower() != "park":
            raise DurableParkConfirmationError("dashboard_operator_invalid")
        return dict(confirmation)

    from services.testnet_automation_coordinator import TestnetAutomationCoordinator

    try:
        TestnetAutomationCoordinator(output_root).verify_confirmation(plan, confirmation)
    except Exception as exc:  # noqa: BLE001 - normalize the shared boundary.
        reason = str(getattr(exc, "code", "") or "durable_confirmation_invalid")
        raise DurableParkConfirmationError(reason) from exc
    return dict(confirmation)


__all__ = ["DurableParkConfirmationError", "parse_durable_confirmation"]
