"""Source-bound readiness contract for the unattended Cloud Paper provider."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

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

PROVIDER_READINESS_UNAVAILABLE = (
    "cloud_ai_provider_readiness_unavailable"
)
PROVIDER_READINESS_INVALID = "cloud_ai_provider_readiness_invalid"

_TRANSIENT_READINESS_BLOCKERS = frozenset(
    {
        "cloud_ai_provider_readiness_missing",
        "cloud_ai_provider_readiness_stale",
    }
)
_STRUCTURAL_READINESS_BLOCKERS = frozenset(
    {
        "cloud_ai_provider_readiness_json_invalid",
        "cloud_ai_provider_readiness_shape_invalid",
        "cloud_ai_provider_readiness_schema_invalid",
        "cloud_ai_provider_readiness_time_invalid",
        "cloud_ai_provider_readiness_time_in_future",
        "cloud_ai_provider_source_unavailable",
        "cloud_ai_provider_source_tree_dirty",
        "cloud_ai_provider_source_sha_mismatch",
        "cloud_ai_provider_source_tree_sha_mismatch",
        "cloud_ai_provider_auth_not_ready",
        "cloud_ai_provider_executable_missing",
        "cloud_ai_provider_executable_unreadable",
        "cloud_ai_provider_executable_changed",
        "cloud_ai_provider_response_contract_invalid",
        "cloud_ai_provider_side_effect_contract_invalid",
        "cloud_ai_provider_readiness_digest_invalid",
        "cloud_ai_provider_readiness_unreadable",
    }
)


class CloudAIProviderReadinessGateError(ValueError):
    """Typed pre-entry refusal from the Cloud provider readiness gate."""

    def __init__(
        self,
        code: str,
        *,
        readiness_blocker: str,
        evidence: Mapping[str, Any] | None = None,
    ) -> None:
        self.code = str(code)
        self.evidence = {
            "readiness_blocker": str(readiness_blocker),
            **(
                dict(evidence)
                if isinstance(evidence, Mapping)
                else {}
            ),
        }
        super().__init__(self.code)


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
        base = {"ok": False, "artifact": str(self.path)}
        if not self.path.exists():
            return {**base, "blocker": "cloud_ai_provider_readiness_missing"}
        try:
            rows = load_json(self.path)
        except json.JSONDecodeError:
            return {
                **base,
                "blocker": "cloud_ai_provider_readiness_json_invalid",
            }
        except OSError:
            return {
                **base,
                "blocker": "cloud_ai_provider_readiness_unreadable",
            }
        if (
            not isinstance(rows, list)
            or len(rows) != 1
            or not isinstance(rows[0], dict)
        ):
            return {
                **base,
                "blocker": "cloud_ai_provider_readiness_shape_invalid",
            }
        receipt = rows[0]
        expected_digest = _digest(
            {
                key: value
                for key, value in receipt.items()
                if key != "readiness_digest"
            }
        )
        if expected_digest != str(
            receipt.get("readiness_digest") or ""
        ):
            return {
                **base,
                "blocker": "cloud_ai_provider_readiness_digest_invalid",
            }
        if receipt.get("schema_version") != READINESS_SCHEMA:
            return {**base, "blocker": "cloud_ai_provider_readiness_schema_invalid"}
        if receipt.get("status") != "pass":
            return {
                **base,
                "blocker": "cloud_ai_provider_readiness_not_passing",
                "receipt_status": str(receipt.get("status") or ""),
                "failure_code": str(receipt.get("failure_code") or ""),
                "readiness_digest": str(
                    receipt.get("readiness_digest") or ""
                ),
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
        try:
            executable_ready = (
                executable.is_file()
                and bool(executable.stat().st_mode & 0o111)
            )
        except OSError:
            return {
                **base,
                "blocker": "cloud_ai_provider_executable_unreadable",
            }
        if not executable_ready:
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
                "executable_sha256": provider.get(
                    "executable_sha256"
                ),
            },
            "checked_at": checked_at.isoformat(),
            "expires_at": expires_at.isoformat(),
            "readiness_digest": str(
                receipt.get("readiness_digest") or ""
            ),
        }


def cloud_provider_readiness_required() -> bool:
    """Return true only for the canonical Cloud service composition."""

    return str(os.getenv("GRIDMIND_RUNTIME_MODE") or "").lower() == "cloud"


def current_cloud_ai_provider_readiness(
    output_root: Path,
    *,
    verifier: Callable[[], Mapping[str, Any]] | None = None,
    expected_digest: str | None = None,
) -> dict[str, Any] | None:
    """Return the current Cloud proof, while keeping local/test mode inert.

    An injected verifier is an explicit test/composition seam and is therefore
    always enforced.  Normal local runtimes do not acquire a Cloud-only gate.
    """

    if verifier is None and not cloud_provider_readiness_required():
        return None
    selected = verifier or CloudAIProviderReadiness(output_root).verify
    return require_cloud_ai_provider_readiness(
        selected,
        expected_digest=expected_digest,
    )


def require_cloud_ai_provider_readiness(
    verifier: Callable[[], Mapping[str, Any]],
    *,
    expected_digest: str | None = None,
) -> dict[str, Any]:
    """Return one bounded proof or raise an explicit fail-closed refusal."""

    try:
        result = verifier()
    except Exception as exc:  # noqa: BLE001 - untyped verifier failure is structural.
        raise CloudAIProviderReadinessGateError(
            "unknown_blocker",
            readiness_blocker=(
                "cloud_ai_provider_readiness_verifier_failed"
            ),
        ) from exc
    if not isinstance(result, Mapping):
        raise CloudAIProviderReadinessGateError(
            "unknown_blocker",
            readiness_blocker=(
                "cloud_ai_provider_readiness_result_invalid"
            ),
        )
    row = dict(result)
    if row.get("ok") is True:
        provider = (
            dict(row.get("provider") or {})
            if isinstance(row.get("provider"), Mapping)
            else {}
        )
        proof = {
            "readiness_digest": str(
                row.get("readiness_digest") or ""
            ),
            "source_sha": str(row.get("source_sha") or ""),
            "source_tree_sha": str(
                row.get("source_tree_sha") or ""
            ),
            "executable_sha256": str(
                provider.get("executable_sha256") or ""
            ),
            "checked_at": str(row.get("checked_at") or ""),
            "expires_at": str(row.get("expires_at") or ""),
        }
        try:
            proof = validate_provider_readiness_proof(proof)
        except ValueError as exc:
            raise CloudAIProviderReadinessGateError(
                "unknown_blocker",
                readiness_blocker=(
                    "cloud_ai_provider_readiness_proof_invalid"
                ),
            ) from exc
        if (
            expected_digest is not None
            and proof["readiness_digest"] != expected_digest
        ):
            raise CloudAIProviderReadinessGateError(
                PROVIDER_READINESS_UNAVAILABLE,
                readiness_blocker=(
                    "cloud_ai_provider_readiness_changed"
                ),
            )
        return proof

    blocker = str(row.get("blocker") or "")
    evidence = {
        key: str(row.get(key) or "")
        for key in (
            "failure_code",
            "readiness_digest",
            "checked_at",
            "expires_at",
        )
        if row.get(key) is not None
    }
    if blocker in _TRANSIENT_READINESS_BLOCKERS:
        raise CloudAIProviderReadinessGateError(
            PROVIDER_READINESS_UNAVAILABLE,
            readiness_blocker=blocker,
            evidence=evidence,
        )
    if blocker == "cloud_ai_provider_readiness_not_passing":
        failure_code = str(row.get("failure_code") or "")
        if failure_code in RECOVERABLE_PROVIDER_CODES:
            raise CloudAIProviderReadinessGateError(
                PROVIDER_READINESS_UNAVAILABLE,
                readiness_blocker=blocker,
                evidence=evidence,
            )
        if failure_code:
            raise CloudAIProviderReadinessGateError(
                PROVIDER_READINESS_INVALID,
                readiness_blocker=blocker,
                evidence=evidence,
            )
    if blocker in _STRUCTURAL_READINESS_BLOCKERS:
        raise CloudAIProviderReadinessGateError(
            PROVIDER_READINESS_INVALID,
            readiness_blocker=blocker,
            evidence=evidence,
        )
    raise CloudAIProviderReadinessGateError(
        "unknown_blocker",
        readiness_blocker=(
            blocker or "cloud_ai_provider_readiness_result_invalid"
        ),
    )


def validate_provider_readiness_proof(
    value: Mapping[str, Any],
) -> dict[str, str]:
    """Validate the bounded proof copied into proposal/entry artifacts."""

    expected = {
        "readiness_digest",
        "source_sha",
        "source_tree_sha",
        "executable_sha256",
        "checked_at",
        "expires_at",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError("cloud_ai_provider_readiness_proof_invalid")
    proof = {key: str(value.get(key) or "") for key in expected}
    for key, size in (
        ("readiness_digest", 64),
        ("source_sha", 40),
        ("source_tree_sha", 40),
        ("executable_sha256", 64),
    ):
        text = proof[key].lower()
        if len(text) != size or any(char not in "0123456789abcdef" for char in text):
            raise ValueError("cloud_ai_provider_readiness_proof_invalid")
        proof[key] = text
    checked_at = _timestamp(proof["checked_at"])
    expires_at = _timestamp(proof["expires_at"])
    if expires_at <= checked_at:
        raise ValueError("cloud_ai_provider_readiness_proof_invalid")
    proof["checked_at"] = checked_at.isoformat()
    proof["expires_at"] = expires_at.isoformat()
    return proof


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
