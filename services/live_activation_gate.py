"""Read-only Live identity, preflight, and exact attended activation gate."""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Mapping

from services.journal_store import load_json, write_json


LIVE_GATE_SCHEMA = "live-activation-gate-v1"
_HEX = re.compile(r"^[0-9a-fA-F]{40,64}$")
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{2,127}$")


class LiveActivationError(RuntimeError):
    def __init__(self, code: str, message: str, evidence: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = str(code)
        self.evidence = dict(evidence or {})


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


class LiveActivationGate:
    """Persist Live activation intent without enabling any Live write path."""

    def __init__(self, output_root: Path, *, park_user_id: str) -> None:
        self.output_root = Path(output_root)
        self.root = self.output_root / "dualtrack" / "live_activation"
        self.path = self.root / "journal.json"
        self.park_user_id = str(park_user_id or "").strip()

    def rows(self) -> list[dict[str, Any]]:
        return [dict(row) for row in load_json(self.path) if isinstance(row, dict)]

    def preflight(
        self,
        *,
        broker_id: str,
        account_id: str,
        environment_fingerprint: str,
        release_sha: str,
        credential_source: str,
        instrument_scope: str,
        strategy_scope: str,
        readiness: Mapping[str, Any],
        capabilities: Mapping[str, Any],
    ) -> dict[str, Any]:
        blockers: list[str] = []
        if str(broker_id).lower() != "hyperliquid":
            blockers.append("unsupported_broker")
        if not str(account_id).strip():
            blockers.append("account_id_missing")
        if not str(environment_fingerprint).strip() or "mainnet" not in str(environment_fingerprint).lower():
            blockers.append("mainnet_environment_fingerprint_missing")
        if not _HEX.fullmatch(str(release_sha or "")):
            blockers.append("release_sha_invalid")
        if not _ENV_NAME.fullmatch(str(credential_source or "")):
            blockers.append("credential_source_must_be_env_name")
        if str(instrument_scope) != "default_perpetuals":
            blockers.append("unsupported_instrument_scope")
        if str(strategy_scope) != "dca":
            blockers.append("live_scope_must_be_dca")
        if str(readiness.get("status") or "") != "ready" or readiness.get("environment") != "testnet":
            blockers.append("testnet_readiness_missing_or_not_ready")
        if int(readiness.get("window_count") or 0) < 14 or int(readiness.get("required_window_count") or 0) != 14:
            blockers.append("testnet_soak_evidence_incomplete")
        if readiness.get("live_enabled") is not False or readiness.get("live_writes_enabled") is not False:
            blockers.append("testnet_readiness_live_flags_invalid")
        required = {"preflight", "submit_order", "cancel_order", "protection_order", "reconciliation"}
        declared = set(str(item) for item in (capabilities.get("operations") or []))
        blockers.extend(f"capability_missing:{name}" for name in sorted(required - declared))
        return {
            "schema_version": LIVE_GATE_SCHEMA,
            "status": "blocked" if blockers else "ready_for_activation",
            "environment": "mainnet",
            "broker_id": "hyperliquid",
            "account_id": str(account_id),
            "environment_fingerprint": str(environment_fingerprint),
            "release_sha": str(release_sha),
            "credential_source": str(credential_source),
            "instrument_scope": str(instrument_scope),
            "strategy_scope": str(strategy_scope),
            "capabilities": {"operations": sorted(declared)},
            "readiness_receipt_digest": str(readiness.get("receipt_digest") or ""),
            "network_io": False,
            "real_money_eligible": False,
            "live_writes_enabled": False,
            "blockers": blockers,
            "next_action": "notify_park_and_wait" if blockers else "await_exact_live_activation_confirmation",
        }

    def prepare_activation(self, preflight: Mapping[str, Any], *, plan_digest: str, expires_at: float) -> dict[str, Any]:
        if preflight.get("status") != "ready_for_activation":
            raise LiveActivationError("live_preflight_blocked", "Live preflight is not ready", preflight)
        if not _HEX.fullmatch(str(plan_digest or "").removeprefix("sha256:")):
            raise LiveActivationError("plan_digest_invalid", "exact DCA plan digest is required")
        if float(expires_at) <= time.time():
            raise LiveActivationError("activation_expired", "activation expiry must be in the future")
        payload = {"preflight": dict(preflight), "plan_digest": str(plan_digest), "expires_at": float(expires_at)}
        activation_digest = _digest(payload)
        existing = next((row for row in reversed(self.rows()) if row.get("event") == "activation_proposed"), None)
        if existing:
            if existing.get("activation_digest") == activation_digest:
                return dict(existing)
            raise LiveActivationError("activation_proposal_immutable", "a different Live activation is already pending")
        row = {"schema_version": LIVE_GATE_SCHEMA, "event": "activation_proposed", "activation_digest": activation_digest, **payload, "status": "awaiting_confirmation", "live_writes_enabled": False, "next_action": "await_exact_live_confirmation"}
        self._append(row)
        return dict(row)

    def confirm(self, *, activation_digest: str, command_text: str, park_user_id: str, now: float | None = None) -> dict[str, Any]:
        proposal = next((row for row in reversed(self.rows()) if row.get("event") == "activation_proposed" and row.get("activation_digest") == activation_digest), None)
        if proposal is None:
            return self._record_rejected("activation_missing", activation_digest)
        if str(park_user_id) != self.park_user_id:
            return self._record_rejected("unauthorized_user", activation_digest)
        command = str(command_text or "").strip()
        if command not in {f"confirm {activation_digest}", f"确认 {activation_digest}"}:
            return self._record_rejected("confirmation_digest_mismatch", activation_digest)
        timestamp = float(now if now is not None else time.time())
        if timestamp > float(proposal.get("expires_at") or 0):
            return self._record_rejected("activation_expired", activation_digest)
        existing = next((row for row in reversed(self.rows()) if row.get("event") == "activation_confirmed" and row.get("activation_digest") == activation_digest), None)
        if existing:
            return dict(existing)
        row = {"schema_version": LIVE_GATE_SCHEMA, "event": "activation_confirmed", "activation_digest": activation_digest, "plan_digest": proposal["plan_digest"], "release_sha": proposal["preflight"]["release_sha"], "account_id": proposal["preflight"]["account_id"], "environment_fingerprint": proposal["preflight"]["environment_fingerprint"], "environment": "mainnet", "park_user_id": self.park_user_id, "confirmed_at": timestamp, "execution_authorized": True, "live_writes_enabled": False, "canary_required": True, "next_action": "await_attended_live_dca_canary"}
        self._append(row)
        return dict(row)

    def public_status(self) -> dict[str, Any]:
        rows = self.rows()
        if not rows:
            return {"status": "missing", "environment": "mainnet", "live_writes_enabled": False, "next_action": "run_read_only_live_preflight"}
        latest = rows[-1]
        if latest.get("event") == "activation_confirmed":
            return {"status": "activated_pending_canary", "environment": "mainnet", "activation_digest": latest.get("activation_digest"), "plan_digest": latest.get("plan_digest"), "release_sha": latest.get("release_sha"), "account_id": latest.get("account_id"), "live_writes_enabled": False, "next_action": latest.get("next_action")}
        return {"status": str(latest.get("status") or "blocked"), "environment": "mainnet", "activation_digest": latest.get("activation_digest"), "blockers": list(latest.get("blockers") or []), "live_writes_enabled": False, "next_action": latest.get("next_action")}

    def _record_rejected(self, code: str, activation_digest: str) -> dict[str, Any]:
        row = {"schema_version": LIVE_GATE_SCHEMA, "event": "activation_rejected", "activation_digest": activation_digest, "code": code, "execution_authorized": False, "live_writes_enabled": False, "next_action": "notify_park_and_wait"}
        self._append(row)
        return dict(row)

    def _append(self, row: Mapping[str, Any]) -> None:
        rows = self.rows()
        rows.append(dict(row))
        write_json(self.path, rows)
