"""Non-actuating Paper release gate built on the launchd compatibility receipt."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import write_json
from services.paper_release_receipt import DEFAULT_RELEASE_GATE_MAX_AGE_SECONDS, current_source_sha
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
        max_age_seconds: int = DEFAULT_RELEASE_GATE_MAX_AGE_SECONDS,
    ) -> None:
        config = load_pipeline_config()
        self.output_root = Path(output_root or ROOT / str(config.get("output_root", "outputs")))
        self.compatibility = compatibility or LaunchdPythonCompatibility(
            self.output_root,
            interpreter=interpreter,
        )
        self.source_sha_resolver = source_sha_resolver or current_source_sha
        self.max_age_seconds = int(max_age_seconds)

    def run(self) -> dict[str, Any]:
        checked_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        try:
            compatibility = self.compatibility.run()
            source_sha = self.source_sha_resolver()
            passed = compatibility.get("status") == "pass"
            reason = "" if passed else "launchd_python_compatibility_failed"
        except Exception as exc:  # noqa: BLE001 - pre-deploy must always fail closed.
            compatibility = {
                "status": "failed",
                "reason": "paper_predeploy_exception",
                "detail": f"{type(exc).__name__}: {exc}",
            }
            source_sha = ""
            passed = False
            reason = "paper_predeploy_exception"
        expires_at = (
            datetime.fromisoformat(checked_at) + timedelta(seconds=self.max_age_seconds)
        ).isoformat()
        payload: dict[str, Any] = {
            "schema_version": "paper-predeploy-gate-v2",
            "checked_at": checked_at,
            "expires_at": expires_at,
            "max_age_seconds": self.max_age_seconds,
            "source_sha": source_sha,
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
