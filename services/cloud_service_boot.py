"""Source-bound boot gate for Cloud Paper services."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import load_json, write_json
from services.paper_release_receipt import current_source_attestation
from services.cloud_timer_contract import CloudTimerContract
from services.cloud_ai_provider import CloudAIProviderReadiness


CLOUD_PAPER_SERVICES = {
    "datafeed",
    "dashboard",
    "dualtrack-live-tick",
    "daily-24h",
    "daily-self-review",
    "backup",
    "deadman-ping",
    "access-gateway",
}


class CloudPaperServiceBootGate:
    def __init__(
        self,
        output_root: Path | None = None,
        *,
        repo_root: Path = ROOT,
        source_attestation: Callable[[], dict[str, Any]] | None = None,
        timer_contract: Callable[[], dict[str, Any]] | None = None,
        provider_readiness: Callable[[], dict[str, Any]] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        config = load_pipeline_config()
        self.output_root = Path(
            output_root
            or os.getenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT")
            or ROOT / str(config.get("output_root", "outputs"))
        )
        self.repo_root = Path(repo_root)
        self.source_attestation = source_attestation or (
            lambda: current_source_attestation(self.repo_root)
        )
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.timer_contract = timer_contract or (
            lambda: CloudTimerContract(self.output_root, now=self.now).run()
        )
        self.provider_readiness = provider_readiness or (
            lambda: CloudAIProviderReadiness(
                self.output_root,
                repo_root=self.repo_root,
                now=self.now,
                source_attestation=self.source_attestation,
            ).verify()
        )

    def verify(self, service: str) -> dict[str, Any]:
        checked_at = self.now().astimezone(timezone.utc).replace(microsecond=0).isoformat()
        payload: dict[str, Any] = {
            "schema_version": "cloud-paper-service-boot-v1",
            "checked_at": checked_at,
            "service": str(service),
            "status": "blocked",
            "ok": False,
            "blocker": "",
        }
        try:
            if service not in CLOUD_PAPER_SERVICES:
                raise ValueError("cloud_paper_service_not_allowlisted")
            rows = load_json(
                self.output_root / "cloud" / "preflight" / "current.json"
            )
            preflight = rows[-1] if rows and isinstance(rows[-1], dict) else {}
            if preflight.get("status") != "pass":
                raise ValueError("cloud_paper_preflight_not_passing")
            if preflight.get("paper_only") is not True:
                raise ValueError("cloud_paper_preflight_not_paper_only")
            if int(preflight.get("control_actions_executed", -1)) != 0:
                raise ValueError("cloud_paper_preflight_control_side_effect")
            source_check = next(
                (
                    row
                    for row in preflight.get("checks", [])
                    if isinstance(row, dict) and row.get("id") == "source_attestation"
                ),
                {},
            )
            receipt_sha = str(source_check.get("source_sha") or "").lower()
            receipt_tree = str(source_check.get("source_tree_sha") or "").lower()
            current = self.source_attestation()
            current_sha = str(current.get("source_sha") or "").lower()
            current_tree = str(current.get("source_tree_sha") or "").lower()
            if not current.get("tracked_tree_clean"):
                raise ValueError("cloud_paper_source_tree_dirty")
            if not receipt_sha or receipt_sha != current_sha:
                raise ValueError("cloud_paper_source_sha_mismatch")
            if not receipt_tree or receipt_tree != current_tree:
                raise ValueError("cloud_paper_source_tree_sha_mismatch")
            if service in {"daily-24h", "deadman-ping"}:
                timers = self.timer_contract()
                if timers.get("status") != "pass":
                    raise ValueError("cloud_paper_timer_contract_not_passing")
            if service == "dualtrack-live-tick":
                provider = self.provider_readiness()
                if provider.get("ok") is not True:
                    raise ValueError(
                        str(provider.get("blocker") or "cloud_ai_provider_readiness_not_passing")
                    )
            payload.update(
                {
                    "status": "pass",
                    "ok": True,
                    "source_sha": current_sha,
                    "source_tree_sha": current_tree,
                }
            )
        except Exception as exc:  # noqa: BLE001 - boot uncertainty must be receipted.
            payload["blocker"] = str(exc)
            payload["next_action"] = (
                "Run the Cloud Paper preflight from the exact clean deployed SHA "
                "before starting this service."
            )
        write_json(
            self.output_root
            / "cloud"
            / "service_boot"
            / f"{service}_current.json",
            [payload],
        )
        return payload
