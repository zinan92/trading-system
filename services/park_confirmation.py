"""Exact Park confirmation capability ledger.

The receipt is a capability for a later adapter to verify.  It is not a
start/order instruction and has no execution side effects.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping


PARK_CONFIRMATION_SCHEMA = "park-confirmation-v1"
_COMMAND = re.compile(r"^\s*(confirm|确认|reject|拒绝)\s+(sha256:[0-9a-f]{64})\s*$", re.IGNORECASE)


class ParkConfirmationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _text(value: Any, field: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ParkConfirmationError("invalid_input", f"{field} is required")
    return result


def parse_confirmation_command(text: str) -> tuple[str, str]:
    match = _COMMAND.fullmatch(str(text or ""))
    if not match:
        raise ParkConfirmationError("confirmation_incomplete", "use exactly confirm|确认 <sha256:plan_digest>")
    return ("confirm" if match.group(1).lower() in {"confirm", "确认"} else "reject", match.group(2).lower())


def _append(path: Path, row: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(dict(row), sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        with path.open("ab") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        os.unlink(temp_name)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def _rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    result: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ParkConfirmationError("journal_corrupt", "confirmation row is not an object")
            result.append(value)
    return result


def _binding(value: Mapping[str, Any]) -> tuple[str, str]:
    if value.get("record_window_id") and not value.get("strategy_session_id"):
        raise ParkConfirmationError(
            "stale_binding",
            "stale binding: a recording window cannot bind a confirmation without the active strategy revision",
        )
    session_id = _text(value.get("strategy_session_id"), "strategy_session_id")
    revision_id = _text(value.get("strategy_revision_id"), "strategy_revision_id")
    return session_id, revision_id


class ParkConfirmationLedger:
    def __init__(self, output_root: Path, *, park_user_id: str) -> None:
        self.path = Path(output_root) / "park_strategy" / "confirmations.jsonl"
        self.park_user_id = _text(park_user_id, "park_user_id")

    def rows(self) -> list[dict[str, Any]]:
        return _rows(self.path)

    def _proposal(self, proposal_id: str) -> dict[str, Any] | None:
        return next((row for row in self.rows() if row.get("event") == "proposal" and row.get("proposal_id") == proposal_id), None)

    def _decision(self, proposal_id: str) -> dict[str, Any] | None:
        decision: dict[str, Any] | None = None
        for row in self.rows():
            if row.get("proposal_id") == proposal_id and row.get("event") in {"confirmed", "rejected"}:
                decision = dict(row)
        return decision

    def create_proposal(
        self,
        *,
        proposal_id: str,
        strategy_session_id: str,
        strategy_revision_id: str,
        plan_digest: str,
        risk_digest: str,
        expires_at: float,
    ) -> dict[str, Any]:
        proposal_key = _text(proposal_id, "proposal_id")
        session_id, revision_id = _binding({"strategy_session_id": strategy_session_id, "strategy_revision_id": strategy_revision_id})
        digest = _text(plan_digest, "plan_digest")
        risk = _text(risk_digest, "risk_digest")
        if float(expires_at) <= 0:
            raise ParkConfirmationError("invalid_expiry", "confirmation expiry is required")
        existing = self._proposal(proposal_key)
        if existing:
            expected = (session_id, revision_id, digest, risk, float(expires_at))
            actual = (
                existing.get("strategy_session_id"), existing.get("strategy_revision_id"),
                existing.get("plan_digest"), existing.get("risk_digest"), float(existing.get("expires_at")),
            )
            if actual != expected:
                raise ParkConfirmationError("proposal_immutable", "proposal identity or digest cannot be changed")
            return dict(existing)
        row = {
            "schema_version": PARK_CONFIRMATION_SCHEMA,
            "event": "proposal",
            "proposal_id": proposal_key,
            "strategy_session_id": session_id,
            "strategy_revision_id": revision_id,
            "plan_digest": digest,
            "risk_digest": risk,
            "expires_at": float(expires_at),
            "execution_authorized": False,
            "next_action": "await_exact_park_confirmation",
        }
        _append(self.path, row)
        return dict(row)

    def decide(
        self,
        *,
        proposal_id: str,
        park_user_id: str,
        command_text: str,
        current_binding: Mapping[str, Any],
        now: float | None = None,
    ) -> dict[str, Any]:
        proposal = self._proposal(_text(proposal_id, "proposal_id"))
        if not proposal:
            raise ParkConfirmationError("proposal_missing", "proposal does not exist")
        if str(park_user_id) != self.park_user_id:
            raise ParkConfirmationError("unauthorized_user", "only Park may confirm")
        existing = self._decision(proposal["proposal_id"])
        if existing:
            return dict(existing)
        try:
            verb, digest = parse_confirmation_command(command_text)
        except ParkConfirmationError:
            raise
        if digest != str(proposal["plan_digest"]).lower():
            raise ParkConfirmationError("plan_digest_mismatch", "confirmation digest does not match proposal")
        if _binding(current_binding) != (proposal["strategy_session_id"], proposal["strategy_revision_id"]):
            raise ParkConfirmationError("stale_binding", "confirmation is bound to a stale strategy revision")
        timestamp = float(now if now is not None else time.time())
        if timestamp > float(proposal["expires_at"]):
            raise ParkConfirmationError("confirmation_expired", "confirmation proposal has expired")
        event = "confirmed" if verb == "confirm" else "rejected"
        row = {
            "schema_version": PARK_CONFIRMATION_SCHEMA,
            "event": event,
            "proposal_id": proposal["proposal_id"],
            "strategy_session_id": proposal["strategy_session_id"],
            "strategy_revision_id": proposal["strategy_revision_id"],
            "plan_digest": proposal["plan_digest"],
            "risk_digest": proposal["risk_digest"],
            "park_user_id": self.park_user_id,
            "confirmed_at": timestamp,
            "execution_authorized": event == "confirmed",
            "start_or_order_submitted": False,
            "next_action": "verify_exact_receipt_before_execution" if event == "confirmed" else "await_new_proposal",
            "receipt_digest": "sha256:" + hashlib.sha256(
                f"{proposal['proposal_id']}|{proposal['plan_digest']}|{event}|{timestamp}".encode("utf-8")
            ).hexdigest(),
        }
        _append(self.path, row)
        return row
