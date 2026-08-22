"""Read-only Live identity, preflight, and exact attended activation gate."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from pathlib import Path
from typing import Any, Mapping

from services.journal_store import load_json, write_json
from services.paper_release_receipt import current_source_attestation


LIVE_GATE_SCHEMA = "live-activation-gate-v1"
_HEX = re.compile(r"^[0-9a-fA-F]{40,64}$")
_SHA256 = re.compile(r"^sha256:[0-9a-fA-F]{64}$")
_ACCOUNT = re.compile(r"^0x[0-9a-fA-F]{40}$")
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{2,127}$")

REQUIRED_CAPABILITIES = frozenset(
    {
        "account.read",
        "order_execution.submit",
        "order_execution.cancel",
        "order_execution.replace",
        "order_execution.query",
        "order_execution.open_orders",
        "order_execution.fills",
        "protection_order.submit",
        "protection_order.cancel",
        "protection_order.replace",
        "protection_order.query",
        "protection_order.reduce_only",
        "protection_order.position_following",
        "protection_order.position_level_tpsl",
        "protection_order.take_profit_market",
        "protection_order.stop_loss_market",
        "reconciliation",
    }
)

REQUIRED_READINESS_CATEGORIES = frozenset(
    {
        "orders_fills_positions_reconciliation",
        "protection_coverage",
        "capability_status",
        "market_freshness_trust",
        "runtime_health",
        "retry_outcomes",
        "release_account_environment_identity",
        "recording_package",
    }
)


class LiveActivationError(RuntimeError):
    def __init__(self, code: str, message: str, evidence: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = str(code)
        self.evidence = dict(evidence or {})


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


class LiveActivationGate:
    """Persist Live activation intent without enabling any Live write path."""

    def __init__(
        self,
        output_root: Path,
        *,
        park_user_id: str,
        park_chat_id: str | int | None = None,
        repo_root: Path | None = None,
        source_attestation_resolver: Any | None = None,
        readiness_receipt_resolver: Any | None = None,
        readiness_windows_resolver: Any | None = None,
        readiness_reviews_resolver: Any | None = None,
        credential_presence_resolver: Any | None = None,
    ) -> None:
        self.output_root = Path(output_root)
        self.root = self.output_root / "dualtrack" / "live_activation"
        self.path = self.root / "journal.json"
        self.park_user_id = str(park_user_id or "").strip()
        self.park_chat_id = str(park_chat_id or "").strip()
        self.repo_root = Path(repo_root) if repo_root is not None else Path(__file__).resolve().parents[1]
        self._source_attestation_resolver = source_attestation_resolver or (
            lambda: current_source_attestation(self.repo_root)
        )
        self._readiness_receipt_resolver = readiness_receipt_resolver or self._default_readiness_receipt
        self._readiness_windows_resolver = readiness_windows_resolver or self._default_readiness_windows
        self._readiness_reviews_resolver = readiness_reviews_resolver or self._default_readiness_reviews
        self._credential_presence_resolver = credential_presence_resolver or (lambda name: bool(os.getenv(name)))

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
        risk_limits: Mapping[str, Any] | None = None,
        source_attestation: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        blockers: list[str] = []
        source = dict(source_attestation or {})
        try:
            current_source = dict(self._source_attestation_resolver())
        except Exception as exc:  # source uncertainty must fail closed.
            current_source = {}
            blockers.append(f"current_source_attestation_unavailable:{type(exc).__name__}")
        if not source:
            source = dict(current_source)
        source_sha = str(source.get("source_sha") or "").lower()
        source_tree_sha = str(source.get("source_tree_sha") or source.get("tree_sha") or "").lower()
        current_sha = str(current_source.get("source_sha") or "").lower()
        current_tree_sha = str(current_source.get("source_tree_sha") or current_source.get("tree_sha") or "").lower()
        if not _HEX.fullmatch(source_sha) or not _HEX.fullmatch(source_tree_sha):
            blockers.append("source_attestation_incomplete")
        if source_sha != current_sha or source_tree_sha != current_tree_sha:
            blockers.append("source_attestation_does_not_match_current_tree")
        if source.get("tracked_tree_clean") is not True or current_source.get("tracked_tree_clean") is not True:
            blockers.append("tracked_source_tree_dirty")
        if str(broker_id).lower() != "hyperliquid":
            blockers.append("unsupported_broker")
        normalized_account = str(account_id or "").strip()
        if not _ACCOUNT.fullmatch(normalized_account):
            blockers.append("account_id_missing")
        normalized_fingerprint = str(environment_fingerprint or "").strip()
        expected_fingerprint = f"hyperliquid:mainnet:{normalized_account.lower()}"
        if normalized_fingerprint != expected_fingerprint:
            blockers.append("mainnet_environment_fingerprint_missing")
        normalized_release_sha = str(release_sha or "").lower()
        if not _HEX.fullmatch(normalized_release_sha):
            blockers.append("release_sha_invalid")
        if normalized_release_sha != current_sha or normalized_release_sha != source_sha:
            blockers.append("release_sha_does_not_match_source")
        if not _ENV_NAME.fullmatch(str(credential_source or "")):
            blockers.append("credential_source_must_be_env_name")
        elif not bool(self._credential_presence_resolver(str(credential_source))):
            blockers.append("credential_source_unavailable")
        if str(instrument_scope) != "default_perpetuals":
            blockers.append("unsupported_instrument_scope")
        if str(strategy_scope) != "dca":
            blockers.append("live_scope_must_be_dca")
        if str(readiness.get("status") or "") != "ready" or readiness.get("environment") != "testnet":
            blockers.append("testnet_readiness_missing_or_not_ready")
        if int(readiness.get("window_count") or 0) != 14 or int(readiness.get("required_window_count") or 0) != 14:
            blockers.append("testnet_soak_evidence_incomplete")
        if int(readiness.get("day_count") or 0) != 7 or int(readiness.get("required_day_count") or 0) != 7:
            blockers.append("testnet_soak_day_evidence_incomplete")
        if not _SHA256.fullmatch(str(readiness.get("receipt_digest") or "")):
            blockers.append("testnet_readiness_receipt_digest_missing_or_invalid")
        if list(readiness.get("blockers") or []):
            blockers.append("testnet_readiness_has_blockers")
        if not self._readiness_is_current(readiness):
            blockers.append("testnet_readiness_receipt_not_current")
        if readiness.get("live_enabled") is not False or readiness.get("live_writes_enabled") is not False:
            blockers.append("testnet_readiness_live_flags_invalid")
        readiness_source = readiness.get("source_attestation")
        if isinstance(readiness_source, Mapping):
            if str(readiness_source.get("source_sha") or "").lower() != source_sha:
                blockers.append("testnet_readiness_source_sha_mismatch")
            if readiness_source.get("tracked_tree_clean") is not True:
                blockers.append("testnet_readiness_source_dirty")
        critical = readiness.get("critical_gate_results")
        if isinstance(critical, Mapping):
            blockers.extend(
                f"critical_gate_failed:{name}"
                for name, value in sorted(critical.items())
                if not self._gate_passes(value)
            )
        # The canonical #860 receipt does not duplicate per-window gate
        # evidence.  _readiness_is_current() verifies every persisted window
        # and its required evidence categories before this point, so that
        # receipt shape is itself the critical-gate proof.
        declared = self._declared_capabilities(capabilities)
        blockers.extend(f"capability_missing:{name}" for name in sorted(REQUIRED_CAPABILITIES - declared))
        declared_gaps = capabilities.get("gaps") if isinstance(capabilities, Mapping) else None
        if declared_gaps:
            blockers.append("capability_gaps_present")
        normalized_risk_limits = self._risk_limits(risk_limits)
        if normalized_risk_limits is None:
            blockers.append("risk_limits_missing_or_invalid")
        result = {
            "schema_version": LIVE_GATE_SCHEMA,
            "status": "blocked" if blockers else "ready_for_activation",
            "environment": "mainnet",
            "broker_id": "hyperliquid",
            "account_id": normalized_account,
            "environment_fingerprint": normalized_fingerprint,
            "release_sha": normalized_release_sha,
            "credential_source": str(credential_source),
            "instrument_scope": str(instrument_scope),
            "strategy_scope": str(strategy_scope),
            "capabilities": {"operations": sorted(declared), "required": sorted(REQUIRED_CAPABILITIES)},
            "risk_limits": normalized_risk_limits or {},
            "readiness_receipt_digest": str(readiness.get("receipt_digest") or ""),
            "readiness_snapshot": dict(readiness),
            "critical_gate_results": dict(critical) if isinstance(critical, Mapping) else {
                "receipt_status": "pass",
                "window_chain": "pass",
                "required_gate_evidence": "pass",
            },
            "source_attestation": source,
            "network_io": False,
            "real_money_eligible": False,
            "live_writes_enabled": False,
            "blockers": blockers,
            "next_action": "notify_park_and_wait" if blockers else "await_exact_live_activation_confirmation",
        }
        result["preflight_digest"] = _digest({key: value for key, value in result.items() if key != "preflight_digest"})
        if result["status"] == "ready_for_activation":
            existing = next(
                (
                    row
                    for row in reversed(self.rows())
                    if row.get("event") == "preflight_completed"
                    and row.get("preflight_digest") == result["preflight_digest"]
                ),
                None,
            )
            if existing is None:
                self._append(
                    {
                        "schema_version": LIVE_GATE_SCHEMA,
                        "event": "preflight_completed",
                        "preflight_digest": result["preflight_digest"],
                        "preflight": dict(result),
                        "network_io": False,
                        "live_writes_enabled": False,
                    }
                )
        return result

    def prepare_activation(self, preflight: Mapping[str, Any], *, plan_digest: str, expires_at: float) -> dict[str, Any]:
        if not self._preflight_is_current(preflight):
            raise LiveActivationError("live_preflight_blocked", "Live preflight is not ready", preflight)
        if not _HEX.fullmatch(str(plan_digest or "").removeprefix("sha256:")):
            raise LiveActivationError("plan_digest_invalid", "exact DCA plan digest is required")
        if float(expires_at) <= time.time():
            raise LiveActivationError("activation_expired", "activation expiry must be in the future")
        payload = {
            "preflight": dict(preflight),
            "preflight_digest": str(preflight.get("preflight_digest") or ""),
            "plan_digest": str(plan_digest),
            "expires_at": float(expires_at),
        }
        activation_digest = _digest(payload)
        existing = next((row for row in reversed(self.rows()) if row.get("event") == "activation_proposed"), None)
        if existing:
            if existing.get("activation_digest") == activation_digest:
                return dict(existing)
            raise LiveActivationError("activation_proposal_immutable", "a different Live activation is already pending")
        row = {"schema_version": LIVE_GATE_SCHEMA, "event": "activation_proposed", "activation_digest": activation_digest, **payload, "status": "awaiting_confirmation", "live_writes_enabled": False, "next_action": "await_exact_live_confirmation"}
        self._append(row)
        return dict(row)

    def confirm(
        self,
        *,
        activation_digest: str,
        command_text: str,
        park_user_id: str,
        telegram_update_id: str | int | None = None,
        telegram_message_id: str | int | None = None,
        telegram_chat_id: str | int | None = None,
        telegram_receipt: Mapping[str, Any] | None = None,
        current_preflight: Mapping[str, Any] | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        proposal = next((row for row in reversed(self.rows()) if row.get("event") == "activation_proposed" and row.get("activation_digest") == activation_digest), None)
        if proposal is None:
            return self._record_rejected("activation_missing", activation_digest)
        if str(park_user_id) != self.park_user_id:
            return self._record_rejected("unauthorized_user", activation_digest)
        existing = next((row for row in reversed(self.rows()) if row.get("event") == "activation_confirmed" and row.get("activation_digest") == activation_digest), None)
        if existing:
            return dict(existing)
        if not self._telegram_receipt_is_complete(
            telegram_update_id,
            telegram_message_id,
            telegram_chat_id,
            telegram_receipt,
            command_text,
        ):
            return self._record_rejected("telegram_receipt_incomplete", activation_digest)
        expected = self._confirmation_tokens(proposal)
        command_tokens = str(command_text or "").strip().split()
        if command_tokens not in [
            ["confirm", "live", *expected],
            ["确认", "live", *expected],
        ]:
            return self._record_rejected("confirmation_digest_mismatch", activation_digest)
        if current_preflight is None or not self._preflight_is_current(current_preflight):
            return self._record_rejected("preflight_recheck_failed", activation_digest)
        if str(current_preflight.get("preflight_digest") or "") != str(proposal.get("preflight_digest") or ""):
            return self._record_rejected("preflight_changed", activation_digest)
        timestamp = float(now if now is not None else time.time())
        if timestamp > float(proposal.get("expires_at") or 0):
            return self._record_rejected("activation_expired", activation_digest)
        row = {
            "schema_version": LIVE_GATE_SCHEMA,
            "event": "activation_confirmed",
            "activation_digest": activation_digest,
            "preflight_digest": proposal["preflight_digest"],
            "plan_digest": proposal["plan_digest"],
            "release_sha": proposal["preflight"]["release_sha"],
            "account_id": proposal["preflight"]["account_id"],
            "environment_fingerprint": proposal["preflight"]["environment_fingerprint"],
            "strategy_scope": proposal["preflight"]["strategy_scope"],
            "environment": "mainnet",
            "park_user_id": self.park_user_id,
            "telegram_update_id": str(telegram_update_id),
            "telegram_message_id": str(telegram_message_id),
            "telegram_chat_id": str(telegram_chat_id or ""),
            "confirmed_at": timestamp,
            "execution_authorized": False,
            "activation_intent": True,
            "live_writes_enabled": False,
            "canary_required": True,
            "next_action": "await_attended_live_dca_canary",
        }
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

    def canary_status(self) -> dict[str, Any]:
        """Verify the durable source-bound activation + attended-canary chain.

        This is deliberately independent of the legacy ``live_activation``
        JSON artifact.  Live adapters call it immediately before a write.
        No event can make it ready until a later attended-canary story records
        a complete, identity-bound ``canary_passed`` receipt.
        """

        try:
            rows = self.rows()
        except Exception as exc:  # journal uncertainty must block writes.
            return {"ready": False, "status": "blocked", "blockers": [f"activation_journal_unreadable:{type(exc).__name__}"], "live_writes_enabled": False}
        confirmed = next((row for row in reversed(rows) if row.get("event") == "activation_confirmed"), None)
        if confirmed is None:
            return {"ready": False, "status": "blocked", "blockers": ["activation_confirmation_missing"], "live_writes_enabled": False}
        preflight_row = next(
            (
                row
                for row in reversed(rows)
                if row.get("event") == "preflight_completed"
                and row.get("preflight_digest") == confirmed.get("preflight_digest")
            ),
            None,
        )
        preflight = preflight_row.get("preflight") if isinstance(preflight_row, Mapping) else None
        if not isinstance(preflight, Mapping) or not self._preflight_is_current(preflight):
            return {"ready": False, "status": "blocked", "blockers": ["activation_preflight_not_current"], "activation_digest": confirmed.get("activation_digest"), "live_writes_enabled": False}
        canary = next(
            (
                row
                for row in reversed(rows)
                if row.get("event") == "canary_passed"
                and row.get("activation_digest") == confirmed.get("activation_digest")
            ),
            None,
        )
        if not isinstance(canary, Mapping):
            return {"ready": False, "status": "blocked", "blockers": ["attended_canary_missing"], "activation_digest": confirmed.get("activation_digest"), "live_writes_enabled": False}
        identity_fields = ("preflight_digest", "plan_digest", "release_sha", "account_id", "environment_fingerprint", "strategy_scope")
        if any(canary.get(field) != confirmed.get(field) for field in identity_fields):
            return {"ready": False, "status": "blocked", "blockers": ["attended_canary_identity_mismatch"], "activation_digest": confirmed.get("activation_digest"), "live_writes_enabled": False}
        if canary.get("execution_authorized") is not True or canary.get("live_writes_enabled") is not True:
            return {"ready": False, "status": "blocked", "blockers": ["attended_canary_not_authorized"], "activation_digest": confirmed.get("activation_digest"), "live_writes_enabled": False}
        canary_digest = str(canary.get("canary_receipt_digest") or "")
        if not _SHA256.fullmatch(canary_digest) or canary_digest != _digest({key: value for key, value in canary.items() if key != "canary_receipt_digest"}):
            return {"ready": False, "status": "blocked", "blockers": ["attended_canary_receipt_missing"], "activation_digest": confirmed.get("activation_digest"), "live_writes_enabled": False}
        if canary.get("risk_limits_digest") != _digest(preflight.get("risk_limits") or {}):
            return {"ready": False, "status": "blocked", "blockers": ["attended_canary_risk_limits_mismatch"], "activation_digest": confirmed.get("activation_digest"), "live_writes_enabled": False}
        source = canary.get("source_attestation") if isinstance(canary.get("source_attestation"), Mapping) else {}
        try:
            current = dict(self._source_attestation_resolver())
        except Exception:
            current = {}
        if source.get("source_sha") != current.get("source_sha") or source.get("source_tree_sha") != current.get("source_tree_sha") or source.get("tracked_tree_clean") is not True or current.get("tracked_tree_clean") is not True:
            return {"ready": False, "status": "blocked", "blockers": ["attended_canary_source_mismatch"], "activation_digest": confirmed.get("activation_digest"), "live_writes_enabled": False}
        return {
            "ready": True,
            "status": "ready",
            "activation_digest": confirmed.get("activation_digest"),
            "preflight_digest": confirmed.get("preflight_digest"),
            "plan_digest": confirmed.get("plan_digest"),
            "release_sha": confirmed.get("release_sha"),
            "account_id": confirmed.get("account_id"),
            "environment_fingerprint": confirmed.get("environment_fingerprint"),
            "strategy_scope": confirmed.get("strategy_scope"),
            "live_writes_enabled": True,
            "execution_authorized": True,
            "canary_receipt_digest": canary.get("canary_receipt_digest"),
            "blockers": [],
        }

    def _record_rejected(self, code: str, activation_digest: str) -> dict[str, Any]:
        row = {"schema_version": LIVE_GATE_SCHEMA, "event": "activation_rejected", "activation_digest": activation_digest, "code": code, "execution_authorized": False, "live_writes_enabled": False, "next_action": "notify_park_and_wait"}
        self._append(row)
        return dict(row)

    def _preflight_is_current(self, preflight: Mapping[str, Any]) -> bool:
        if not isinstance(preflight, Mapping) or preflight.get("status") != "ready_for_activation":
            return False
        supplied = str(preflight.get("preflight_digest") or "")
        if supplied != _digest({key: value for key, value in preflight.items() if key != "preflight_digest"}):
            return False
        recorded = next(
            (
                row
                for row in reversed(self.rows())
                if row.get("event") == "preflight_completed"
                and row.get("preflight_digest") == supplied
            ),
            None,
        )
        if recorded is None or recorded.get("preflight") != dict(preflight):
            return False
        try:
            current = dict(self._source_attestation_resolver())
        except Exception:
            return False
        source = preflight.get("source_attestation") if isinstance(preflight.get("source_attestation"), Mapping) else {}
        account_id = str(preflight.get("account_id") or "")
        capabilities = preflight.get("capabilities") if isinstance(preflight.get("capabilities"), Mapping) else {}
        readiness = preflight.get("readiness_snapshot")
        risk_limits = self._risk_limits(preflight.get("risk_limits"))
        expected_fingerprint = f"hyperliquid:mainnet:{account_id.lower()}"
        return (
            source.get("source_sha") == current.get("source_sha")
            and (source.get("source_tree_sha") or source.get("tree_sha")) == (current.get("source_tree_sha") or current.get("tree_sha"))
            and source.get("tracked_tree_clean") is True
            and current.get("tracked_tree_clean") is True
            and preflight.get("release_sha") == current.get("source_sha")
            and preflight.get("environment") == "mainnet"
            and preflight.get("broker_id") == "hyperliquid"
            and _ACCOUNT.fullmatch(account_id) is not None
            and preflight.get("environment_fingerprint") == expected_fingerprint
            and _ENV_NAME.fullmatch(str(preflight.get("credential_source") or "")) is not None
            and bool(self._credential_presence_resolver(str(preflight.get("credential_source") or "")))
            and preflight.get("instrument_scope") == "default_perpetuals"
            and preflight.get("strategy_scope") == "dca"
            and _SHA256.fullmatch(str(preflight.get("readiness_receipt_digest") or "")) is not None
            and isinstance(readiness, Mapping)
            and preflight.get("readiness_receipt_digest") == readiness.get("receipt_digest")
            and self._declared_capabilities(capabilities) >= REQUIRED_CAPABILITIES
            and not capabilities.get("gaps")
            and risk_limits is not None
            and isinstance(preflight.get("critical_gate_results"), Mapping)
            and all(self._gate_passes(value) for value in preflight["critical_gate_results"].values())
            and preflight.get("network_io") is False
            and preflight.get("real_money_eligible") is False
            and preflight.get("live_writes_enabled") is False
            and not preflight.get("blockers")
            and self._readiness_is_current(readiness)
        )

    def _readiness_is_current(self, readiness: Any) -> bool:
        if not isinstance(readiness, Mapping):
            return False
        try:
            current = self._readiness_receipt_resolver()
        except Exception:
            return False
        if not isinstance(current, Mapping):
            return False
        supplied_digest = str(readiness.get("receipt_digest") or "")
        current_digest = str(current.get("receipt_digest") or "")
        if not _SHA256.fullmatch(supplied_digest) or supplied_digest != current_digest:
            return False
        if current_digest != _digest({key: value for key, value in current.items() if key != "receipt_digest"}):
            return False
        if (
            current.get("status") != "ready"
            or current.get("environment") != "testnet"
            or int(current.get("window_count") or 0) != 14
            or int(current.get("required_window_count") or 0) != 14
            or int(current.get("day_count") or 0) != 7
            or int(current.get("required_day_count") or 0) != 7
            or list(current.get("blockers") or [])
            or current.get("live_enabled") is not False
            or current.get("live_writes_enabled") is not False
        ):
            return False
        source = current.get("source_attestation") if isinstance(current.get("source_attestation"), Mapping) else {}
        try:
            live_source = self._source_attestation_resolver()
        except Exception:
            return False
        if (
            source.get("source_sha") != live_source.get("source_sha")
            or (source.get("source_tree_sha") or source.get("tree_sha")) != (live_source.get("source_tree_sha") or live_source.get("tree_sha"))
            or source.get("tracked_tree_clean") is not True
            or live_source.get("tracked_tree_clean") is not True
        ):
            return False
        window_digests = [str(item or "") for item in current.get("window_digests") or []]
        if len(window_digests) != 14 or any(not _SHA256.fullmatch(item) for item in window_digests):
            return False
        try:
            windows = list(self._readiness_windows_resolver())
            reviews = list(self._readiness_reviews_resolver())
        except Exception:
            return False
        ordered = sorted((item for item in windows if isinstance(item, Mapping)), key=lambda item: int(item.get("window_index") or 0))
        if len(ordered) != 14 or [str(item.get("row_digest") or "") for item in ordered] != window_digests:
            return False
        if any(
            item.get("status") != "pass"
            or item.get("blockers")
            or item.get("package_status") != "complete"
            or not item.get("review_digest")
            or not REQUIRED_READINESS_CATEGORIES.issubset(set((item.get("gate_evidence") or {}).keys()))
            or any(str(item.get("row_digest") or "") != _digest({key: value for key, value in item.items() if key != "row_digest"}) for item in ordered)
            for item in ordered
        ):
            return False
        review_by_window = {
            str(item.get("record_window_id") or ""): item
            for item in reviews
            if isinstance(item, Mapping) and item.get("event") == "window_review"
        }
        for item in ordered:
            review = review_by_window.get(str(item.get("record_window_id") or ""))
            if not isinstance(review, Mapping) or review.get("review_digest") != _digest(review.get("review") or {}):
                return False
            if str(item.get("review_digest") or "") != str(review.get("review_digest") or ""):
                return False
            evidence = item.get("gate_evidence") if isinstance(item.get("gate_evidence"), Mapping) else {}
            for category in REQUIRED_READINESS_CATEGORIES:
                payload = evidence.get(category) if isinstance(evidence.get(category), Mapping) else {}
                artifact_ref = str(payload.get("artifact_ref") or "")
                artifact_sha = str(payload.get("artifact_sha256") or "").lower()
                if not artifact_ref or not re.fullmatch(r"[0-9a-f]{64}", artifact_sha):
                    return False
                artifact_path = Path(artifact_ref)
                if not artifact_path.is_absolute():
                    artifact_path = self.output_root / artifact_path
                try:
                    if not artifact_path.is_file() or hashlib.sha256(artifact_path.read_bytes()).hexdigest() != artifact_sha:
                        return False
                except OSError:
                    return False
        return True

    def _default_readiness_receipt(self) -> Mapping[str, Any]:
        rows = load_json(self.output_root / "dualtrack" / "testnet_soak" / "readiness_receipts.json")
        return rows[-1] if rows and isinstance(rows[-1], Mapping) else {}

    def _default_readiness_windows(self) -> list[Mapping[str, Any]]:
        return load_json(self.output_root / "dualtrack" / "testnet_soak" / "windows.json")

    def _default_readiness_reviews(self) -> list[Mapping[str, Any]]:
        return load_json(self.output_root / "dualtrack" / "testnet_soak" / "reviews.json")

    @staticmethod
    def _declared_capabilities(capabilities: Mapping[str, Any]) -> set[str]:
        declared: set[str] = set()
        for item in capabilities.get("operations") or []:
            value = str(item)
            declared.add(value)
            # Legacy flat names are normalized only where the mapping is unambiguous.
            aliases = {
                "preflight": "preflight",
                "reconciliation": "reconciliation",
                "submit_order": "order_execution.submit",
                "cancel_order": "order_execution.cancel",
                "replace_order": "order_execution.replace",
                "query_order": "order_execution.query",
                "open_orders": "order_execution.open_orders",
                "protection_order": "protection_order.submit",
            }
            if value in aliases:
                declared.add(aliases[value])
        for port, operations in (capabilities.get("ports") or {}).items():
            if isinstance(operations, Mapping):
                for operation, supported in operations.items():
                    if supported is True:
                        declared.add(f"{port}.{operation}")
        return declared

    @staticmethod
    def _risk_limits(value: Mapping[str, Any] | None) -> dict[str, float] | None:
        if not isinstance(value, Mapping):
            return None
        result: dict[str, float] = {}
        for key in ("max_acceptable_loss", "max_notional", "max_leverage", "max_open_orders", "max_positions"):
            try:
                number = float(value.get(key))
            except (TypeError, ValueError):
                return None
            if not math.isfinite(number) or number <= 0:
                return None
            result[key] = number
        return result

    def _telegram_receipt_is_complete(
        self,
        update_id: str | int | None,
        message_id: str | int | None,
        chat_id: str | int | None,
        receipt: Mapping[str, Any] | None,
        command_text: str,
    ) -> bool:
        if not self.park_chat_id or not str(update_id or "").strip() or not str(message_id or "").strip():
            return False
        if str(chat_id or "") != self.park_chat_id or not isinstance(receipt, Mapping):
            return False
        return (
            str(receipt.get("event") or "") == "inbound_received"
            and str(receipt.get("update_id") or "") == str(update_id)
            and str(receipt.get("message_id") or "") == str(message_id)
            and str(receipt.get("chat_id") or "") == self.park_chat_id
            and str(receipt.get("sender_id") or "") == self.park_user_id
            and str(receipt.get("text") or "") == str(command_text).strip()
            and str(receipt.get("text_digest") or "")
            == "sha256:" + hashlib.sha256(str(command_text).strip().encode("utf-8")).hexdigest()
        )

    @staticmethod
    def _gate_passes(value: Any) -> bool:
        return value is True or value in {"pass", "ready"}

    @staticmethod
    def _confirmation_tokens(proposal: Mapping[str, Any]) -> list[str]:
        preflight = proposal["preflight"]
        return [
            str(proposal["activation_digest"]),
            str(preflight["release_sha"]),
            str(preflight["account_id"]),
            str(preflight["environment_fingerprint"]),
            str(preflight["strategy_scope"]),
            str(proposal["plan_digest"]),
        ]

    def _append(self, row: Mapping[str, Any]) -> None:
        rows = self.rows()
        rows.append(dict(row))
        write_json(self.path, rows)
