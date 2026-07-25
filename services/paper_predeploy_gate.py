"""Non-actuating Paper release gate built on the launchd compatibility receipt."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import write_json
from services.paper_release_receipt import (
    DEFAULT_RELEASE_GATE_MAX_AGE_SECONDS,
    current_source_attestation,
)
from services.python_runtime_compatibility import LaunchdPythonCompatibility


class PaperPredeployGate:
    """Write one release receipt; never start, stop, or alter Paper runtime."""

    def __init__(
        self,
        output_root: Optional[Path] = None,
        *,
        interpreter: Optional[str] = None,
        compatibility: Optional[LaunchdPythonCompatibility] = None,
        source_sha_resolver: Optional[Callable[[], str]] = None,
        source_attestation_resolver: Optional[Callable[[], dict[str, Any]]] = None,
        max_age_seconds: int = DEFAULT_RELEASE_GATE_MAX_AGE_SECONDS,
    ) -> None:
        config = load_pipeline_config()
        self.output_root = Path(output_root or ROOT / str(config.get("output_root", "outputs")))
        self.compatibility = compatibility or LaunchdPythonCompatibility(
            self.output_root,
            interpreter=interpreter,
        )
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
            self.source_attestation_resolver = current_source_attestation
        self.max_age_seconds = int(max_age_seconds)

    def run(self) -> dict[str, Any]:
        checked_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        try:
            compatibility = self.compatibility.run()
            attestation = self.source_attestation_resolver()
            source_sha = str(attestation.get("source_sha") or "")
            source_tree_sha = str(attestation.get("source_tree_sha") or "")
            tracked_tree_clean = bool(attestation.get("tracked_tree_clean"))
            passed = compatibility.get("status") == "pass" and tracked_tree_clean
            reason = (
                ""
                if passed
                else (
                    "tracked_source_tree_dirty"
                    if not tracked_tree_clean
                    else "launchd_python_compatibility_failed"
                )
            )
        except Exception as exc:  # noqa: BLE001 - pre-deploy must always fail closed.
            compatibility = {
                "status": "failed",
                "reason": "paper_predeploy_exception",
                "detail": f"{type(exc).__name__}: {exc}",
            }
            source_sha = ""
            source_tree_sha = ""
            tracked_tree_clean = False
            passed = False
            reason = "paper_predeploy_exception"
        expires_at = (
            datetime.fromisoformat(checked_at) + timedelta(seconds=self.max_age_seconds)
        ).isoformat()
        payload: dict[str, Any] = {
            "schema_version": "paper-predeploy-gate-v3",
            "checked_at": checked_at,
            "expires_at": expires_at,
            "max_age_seconds": self.max_age_seconds,
            "source_sha": source_sha,
            "source_tree_sha": source_tree_sha,
            "tracked_tree_clean": tracked_tree_clean,
            "status": "pass" if passed else "blocked",
            "reason": reason,
            "next_action": (
                "Paper release may proceed; run the release health and browser checks next."
                if passed
                else "Fix the launchd Python compatibility receipt before restarting any Paper service."
            ),
            "compatibility_receipt": compatibility,
            "operations": {
                "starts_services": False,
                "stops_services": False,
                "submits_orders": False,
                "cancels_orders": False,
                "closes_positions": False,
                "uses_exchange_credentials": False,
            },
        }
        write_json(self.output_root / "release_gates" / "paper_predeploy_current.json", [payload])
        return payload
