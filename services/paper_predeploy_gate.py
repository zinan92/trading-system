"""Non-actuating Paper release gate built on the launchd compatibility receipt."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import write_json
from services.python_runtime_compatibility import LaunchdPythonCompatibility


class PaperPredeployGate:
    """Write one release receipt; never start, stop, or alter Paper runtime."""

    def __init__(
        self,
        output_root: Optional[Path] = None,
        *,
        interpreter: Optional[str] = None,
        compatibility: Optional[LaunchdPythonCompatibility] = None,
    ) -> None:
        config = load_pipeline_config()
        self.output_root = Path(output_root or ROOT / str(config.get("output_root", "outputs")))
        self.compatibility = compatibility or LaunchdPythonCompatibility(
            self.output_root,
            interpreter=interpreter,
        )

    def run(self) -> dict[str, Any]:
        checked_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        compatibility = self.compatibility.run()
        passed = compatibility.get("status") == "pass"
        payload: dict[str, Any] = {
            "schema_version": "paper-predeploy-gate-v1",
            "checked_at": checked_at,
            "status": "pass" if passed else "blocked",
            "reason": "" if passed else "launchd_python_compatibility_failed",
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
