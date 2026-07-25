"""Fresh, source-bound authorization receipt for Paper service modification."""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import load_json


DEFAULT_RELEASE_GATE_MAX_AGE_SECONDS = 900


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
    ) -> None:
        config = load_pipeline_config()
        self.output_root = Path(output_root or ROOT / str(config.get("output_root", "outputs")))
        self.repo_root = Path(repo_root)
        self.max_age_seconds = int(max_age_seconds)
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.source_sha_resolver = source_sha_resolver or (
            lambda: current_source_sha(self.repo_root)
        )

    def verify(self) -> dict[str, Any]:
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
        if age_seconds > self.max_age_seconds:
            return {
                **base,
                "blocker": "paper_predeploy_receipt_stale",
                "age_seconds": age_seconds,
                "checked_at": checked_at.isoformat(),
            }
        receipt_sha = str(receipt.get("source_sha") or "").strip().lower()
        try:
            current_sha = self.source_sha_resolver().strip().lower()
        except Exception as exc:  # noqa: BLE001 - source uncertainty must block.
            return {
                **base,
                "blocker": "current_source_sha_unavailable",
                "detail": f"{type(exc).__name__}: {exc}",
            }
        if not receipt_sha:
            return {**base, "blocker": "paper_predeploy_source_sha_missing", "current_source_sha": current_sha}
        if receipt_sha != current_sha:
            return {
                **base,
                "blocker": "paper_predeploy_source_sha_mismatch",
                "receipt_source_sha": receipt_sha,
                "current_source_sha": current_sha,
            }
        return {
            **base,
            "ok": True,
            "blocker": "",
            "checked_at": checked_at.isoformat(),
            "age_seconds": age_seconds,
            "source_sha": current_sha,
            "receipt_schema_version": str(receipt.get("schema_version") or ""),
        }


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
