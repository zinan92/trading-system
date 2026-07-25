"""Source-bound authorization receipts for Paper release and service boot."""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import load_json, write_json


DEFAULT_RELEASE_GATE_MAX_AGE_SECONDS = 900
# BSD sysexits reserves 64-78 (78 is EX_CONFIG). Keep boot attestation
# refusal outside that range so launchd diagnostics cannot misclassify it.
PAPER_SERVICE_BOOT_BLOCKED_EXIT_CODE = 79


def current_source_sha(
    repo_root: Path = ROOT,
    *,
    command_runner: Optional[Callable[..., subprocess.CompletedProcess]] = None,
) -> str:
    runner = command_runner or subprocess.run
    result = runner(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    sha = str(result.stdout or "").strip()
    if result.returncode != 0 or len(sha) != 40 or any(char not in "0123456789abcdef" for char in sha.lower()):
        raise RuntimeError("unable to resolve the current source SHA")
    return sha.lower()


def current_source_attestation(
    repo_root: Path = ROOT,
    *,
    command_runner: Optional[Callable[..., subprocess.CompletedProcess]] = None,
) -> dict[str, Any]:
    """Describe the exact committed source tree and reject tracked edits."""

    runner = command_runner or subprocess.run
    source_sha = current_source_sha(repo_root, command_runner=runner)
    tree_result = runner(
        ["git", "rev-parse", "HEAD^{tree}"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    tree_sha = str(tree_result.stdout or "").strip().lower()
    if (
        tree_result.returncode != 0
        or len(tree_sha) != 40
        or any(char not in "0123456789abcdef" for char in tree_sha)
    ):
        raise RuntimeError("unable to resolve the current source tree SHA")
    status_result = runner(
        ["git", "status", "--porcelain", "--untracked-files=no"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    if status_result.returncode != 0:
        raise RuntimeError("unable to inspect the current tracked source tree")
    return {
        "source_sha": source_sha,
        "source_tree_sha": tree_sha,
        "tracked_tree_clean": not bool(str(status_result.stdout or "").strip()),
    }


class PaperReleaseReceiptGate:
    """Validate a passing receipt immediately before a Paper service mutation."""

    def __init__(
        self,
        output_root: Optional[Path] = None,
        *,
        repo_root: Path = ROOT,
        max_age_seconds: int = DEFAULT_RELEASE_GATE_MAX_AGE_SECONDS,
        now: Optional[Callable[[], datetime]] = None,
        source_sha_resolver: Optional[Callable[[], str]] = None,
        source_attestation_resolver: Optional[Callable[[], dict[str, Any]]] = None,
    ) -> None:
        config = load_pipeline_config()
        self.output_root = Path(output_root or ROOT / str(config.get("output_root", "outputs")))
        self.repo_root = Path(repo_root)
        self.max_age_seconds = int(max_age_seconds)
        self.now = now or (lambda: datetime.now(timezone.utc))
        if source_attestation_resolver is not None:
            self.source_attestation_resolver = source_attestation_resolver
        elif source_sha_resolver is not None:
            def _legacy_attestation() -> dict[str, Any]:
                source_sha = source_sha_resolver()
                return {
                    "source_sha": source_sha,
                    "source_tree_sha": source_sha,
                    "tracked_tree_clean": True,
                }

            self.source_attestation_resolver = _legacy_attestation
        else:
            self.source_attestation_resolver = lambda: current_source_attestation(
                self.repo_root
            )

    def verify(self) -> dict[str, Any]:
        return self._verify(require_fresh=True)

    def _verify(self, *, require_fresh: bool) -> dict[str, Any]:
        path = self.output_root / "release_gates" / "paper_predeploy_current.json"
        rows = load_json(path)
        receipt = rows[-1] if rows else {}
        base = {
            "ok": False,
            "artifact": str(path),
            "max_age_seconds": self.max_age_seconds,
        }
        if not receipt:
            return {**base, "blocker": "missing_paper_predeploy_receipt"}
        if receipt.get("status") != "pass":
            return {
                **base,
                "blocker": "paper_predeploy_receipt_not_passing",
                "receipt_status": str(receipt.get("status") or ""),
            }
        compatibility = receipt.get("compatibility_receipt")
        if not isinstance(compatibility, dict) or compatibility.get("status") != "pass":
            return {**base, "blocker": "paper_predeploy_compatibility_not_passing"}
        checked_at = _parse_timestamp(str(receipt.get("checked_at") or ""))
        if checked_at is None:
            return {**base, "blocker": "paper_predeploy_receipt_time_invalid"}
        now = self.now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        age_seconds = max(0.0, (now.astimezone(timezone.utc) - checked_at).total_seconds())
        if require_fresh and age_seconds > self.max_age_seconds:
            return {
                **base,
                "blocker": "paper_predeploy_receipt_stale",
                "age_seconds": age_seconds,
                "checked_at": checked_at.isoformat(),
            }
        receipt_sha = str(receipt.get("source_sha") or "").strip().lower()
        receipt_tree_sha = str(receipt.get("source_tree_sha") or "").strip().lower()
        try:
            attestation = self.source_attestation_resolver()
            current_sha = str(attestation.get("source_sha") or "").strip().lower()
            current_tree_sha = str(attestation.get("source_tree_sha") or "").strip().lower()
            tracked_tree_clean = bool(attestation.get("tracked_tree_clean"))
        except Exception as exc:  # noqa: BLE001 - source uncertainty must block.
            return {
                **base,
                "blocker": "current_source_sha_unavailable",
                "detail": f"{type(exc).__name__}: {exc}",
            }
        if not receipt_sha:
            return {**base, "blocker": "paper_predeploy_source_sha_missing", "current_source_sha": current_sha}
        if not receipt_tree_sha:
            return {
                **base,
                "blocker": "paper_predeploy_source_tree_sha_missing",
                "current_source_sha": current_sha,
            }
        if not tracked_tree_clean:
            return {
                **base,
                "blocker": "current_tracked_source_tree_dirty",
                "current_source_sha": current_sha,
                "current_source_tree_sha": current_tree_sha,
            }
        if receipt_sha != current_sha:
            return {
                **base,
                "blocker": "paper_predeploy_source_sha_mismatch",
                "receipt_source_sha": receipt_sha,
                "current_source_sha": current_sha,
            }
        if receipt_tree_sha != current_tree_sha:
            return {
                **base,
                "blocker": "paper_predeploy_source_tree_sha_mismatch",
                "receipt_source_tree_sha": receipt_tree_sha,
                "current_source_tree_sha": current_tree_sha,
            }
        return {
            **base,
            "ok": True,
            "blocker": "",
            "checked_at": checked_at.isoformat(),
            "age_seconds": age_seconds,
            "source_sha": current_sha,
            "source_tree_sha": current_tree_sha,
            "receipt_schema_version": str(receipt.get("schema_version") or ""),
        }


class PaperServiceBootGate(PaperReleaseReceiptGate):
    """Verify deployed source at boot without expiring normal crash recovery."""

    def verify(self, service: str) -> dict[str, Any]:
        result = self._verify(require_fresh=False)
        checked_at = self.now()
        if checked_at.tzinfo is None:
            checked_at = checked_at.replace(tzinfo=timezone.utc)
        payload = {
            "schema_version": "paper-service-boot-v1",
            "checked_at": checked_at.astimezone(timezone.utc).replace(microsecond=0).isoformat(),
            "service": str(service),
            "status": "pass" if result.get("ok") else "blocked",
            "blocker": str(result.get("blocker") or ""),
            "source_sha": str(result.get("source_sha") or result.get("current_source_sha") or ""),
            "source_tree_sha": str(
                result.get("source_tree_sha") or result.get("current_source_tree_sha") or ""
            ),
            "predeploy_artifact": str(result.get("artifact") or ""),
        }
        write_json(
            self.output_root / "release_gates" / f"paper_service_boot_{service}_current.json",
            [payload],
        )
        return {**result, "boot_receipt": payload}


def _parse_timestamp(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
