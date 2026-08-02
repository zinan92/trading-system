"""Source-bound readiness contract for the unattended Cloud Paper provider."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from services.config_loader import ROOT
from services.journal_store import load_json
from services.paper_release_receipt import current_source_attestation


READINESS_SCHEMA = "cloud-ai-provider-readiness-v1"
DEFAULT_MAX_AGE_SECONDS = 24 * 60 * 60
RECOVERABLE_PROVIDER_CODES = frozenset(
    {
        "strategy_recommendation_provider_timeout",
        "strategy_recommendation_provider_unavailable",
        "strategy_recommendation_provider_failed",
    }
)


class CloudAIProviderReadiness:
    """Verify a deploy-time provider receipt without calling the model."""

    def __init__(
        self,
        output_root: Path,
        *,
        repo_root: Path = ROOT,
        now: Callable[[], datetime] | None = None,
        max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
        source_attestation: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self.output_root = Path(output_root)
        self.repo_root = Path(repo_root)
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.max_age_seconds = int(max_age_seconds)
        self.source_attestation = source_attestation or (
            lambda: current_source_attestation(self.repo_root)
        )
        self.path = self.output_root / "cloud" / "provider" / "readiness_current.json"

    def verify(self) -> dict[str, Any]:
        rows = load_json(self.path)
        receipt = rows[-1] if rows and isinstance(rows[-1], dict) else None
        base = {"ok": False, "artifact": str(self.path)}
        if receipt is None:
            return {**base, "blocker": "cloud_ai_provider_readiness_missing"}
        if receipt.get("schema_version") != READINESS_SCHEMA:
            return {**base, "blocker": "cloud_ai_provider_readiness_schema_invalid"}
        if receipt.get("status") != "pass":
            return {
                **base,
                "blocker": "cloud_ai_provider_readiness_not_passing",
                "receipt_status": str(receipt.get("status") or ""),
            }
        try:
            checked_at = _timestamp(receipt.get("checked_at"))
            expires_at = _timestamp(receipt.get("expires_at"))
        except ValueError:
            return {**base, "blocker": "cloud_ai_provider_readiness_time_invalid"}
        now = self.now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        now = now.astimezone(timezone.utc)
        age = (now - checked_at).total_seconds()
        if age < -300:
            return {**base, "blocker": "cloud_ai_provider_readiness_time_in_future"}
        if age > self.max_age_seconds or now >= expires_at:
            return {
                **base,
                "blocker": "cloud_ai_provider_readiness_stale",
                "checked_at": checked_at.isoformat(),
                "expires_at": expires_at.isoformat(),
            }
        try:
            current = self.source_attestation()
        except Exception:
            return {**base, "blocker": "cloud_ai_provider_source_unavailable"}
        if not current.get("tracked_tree_clean"):
            return {**base, "blocker": "cloud_ai_provider_source_tree_dirty"}
        if str(receipt.get("source_sha") or "").lower() != str(current.get("source_sha") or "").lower():
            return {**base, "blocker": "cloud_ai_provider_source_sha_mismatch"}
        if str(receipt.get("source_tree_sha") or "").lower() != str(current.get("source_tree_sha") or "").lower():
            return {**base, "blocker": "cloud_ai_provider_source_tree_sha_mismatch"}

        provider = receipt.get("provider")
        contract = receipt.get("response_contract")
        operations = receipt.get("operations")
        if not isinstance(provider, dict) or provider.get("auth_status") != "logged_in":
            return {**base, "blocker": "cloud_ai_provider_auth_not_ready"}
        executable = Path(str(provider.get("executable") or ""))
        if not executable.is_file() or not executable.stat().st_mode & 0o111:
            return {**base, "blocker": "cloud_ai_provider_executable_missing"}
        try:
            current_hash = _sha256(executable)
        except OSError:
            return {**base, "blocker": "cloud_ai_provider_executable_unreadable"}
        if current_hash != str(provider.get("executable_sha256") or "").lower():
            return {**base, "blocker": "cloud_ai_provider_executable_changed"}
        if not isinstance(contract, dict) or contract.get("status") != "pass":
            return {**base, "blocker": "cloud_ai_provider_response_contract_invalid"}
        if not isinstance(operations, dict) or any(
            operations.get(key) is not False
            for key in ("orders_allowed", "production_mutation_allowed", "uses_exchange_credentials")
        ):
            return {**base, "blocker": "cloud_ai_provider_side_effect_contract_invalid"}
        expected_digest = _digest({key: value for key, value in receipt.items() if key != "readiness_digest"})
        if expected_digest != str(receipt.get("readiness_digest") or ""):
            return {**base, "blocker": "cloud_ai_provider_readiness_digest_invalid"}
        return {
            **base,
            "ok": True,
            "status": "pass",
            "source_sha": str(receipt.get("source_sha") or ""),
            "source_tree_sha": str(receipt.get("source_tree_sha") or ""),
            "provider": {
                "name": provider.get("name"),
                "version": provider.get("version"),
                "auth_status": provider.get("auth_status"),
            },
            "checked_at": checked_at.isoformat(),
            "expires_at": expires_at.isoformat(),
        }


def _timestamp(value: Any) -> datetime:
    text = str(value or "")
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _digest(value: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
