from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import load_json, write_json


class LiveApprovalStore:
    def __init__(self, output_root: Path | None = None) -> None:
        config = load_pipeline_config()
        self.output_root = output_root or ROOT / config.get("output_root", "outputs")

    def request(self, run_date: str, notes: str = "") -> dict:
        activation = self._latest("live_activation", run_date)
        payload = {
            "run_date": run_date,
            "requested_at": self._now(),
            "status": "requested",
            "approved": False,
            "notes": notes,
            "activation_status": activation.get("status", ""),
            "dry_run_ready": bool(activation.get("dry_run_ready")),
            "real_money_ready": bool(activation.get("real_money_ready")),
            "failed_checks": [item["name"] for item in activation.get("real_money_checks", []) if item.get("status") != "pass"],
            "review_required": [
                "Dashboard Live Activation panel",
                "Trading Journal",
                "Risk Envelope",
                "Live Readiness",
                "Broker preflight and receipt loop",
            ],
        }
        write_json(self._request_path(run_date), [payload])
        self._write_markdown_request(run_date, payload)
        return self.status(run_date)

    def approve(self, run_date: str, approver: str, notes: str = "", force: bool = False) -> dict:
        activation = self._latest("live_activation", run_date)
        if not force and not activation.get("dry_run_ready"):
            raise ValueError("live dry-run gate is not ready; approval requires dry_run_ready=true unless --force is used")
        payload = {
            "run_date": run_date,
            "approved_at": self._now(),
            "status": "approved",
            "approved": True,
            "approver": approver,
            "notes": notes,
            "force": force,
            "activation_status_at_approval": activation.get("status", ""),
            "dry_run_ready_at_approval": bool(activation.get("dry_run_ready")),
            "real_money_ready_at_approval": bool(activation.get("real_money_ready")),
        }
        write_json(self._approved_path(run_date), [payload])
        return self.status(run_date)

    def revoke(self, run_date: str, notes: str = "") -> dict:
        payload = {
            "run_date": run_date,
            "revoked_at": self._now(),
            "status": "revoked",
            "approved": False,
            "notes": notes,
        }
        write_json(self._revoked_path(run_date), [payload])
        approved_path = self._approved_path(run_date)
        if approved_path.exists():
            approved_path.unlink()
        return self.status(run_date)

    def status(self, run_date: str) -> dict:
        request = self._read_one(self._request_path(run_date))
        approval = self._read_one(self._approved_path(run_date))
        revoked = self._read_one(self._revoked_path(run_date))
        if approval.get("approved"):
            status = "approved"
        elif request:
            status = "requested"
        elif revoked:
            status = "revoked"
        else:
            status = "missing"
        payload = {
            "run_date": run_date,
            "checked_at": self._now(),
            "status": status,
            "approved": bool(approval.get("approved")),
            "request": request,
            "approval": approval,
            "revoked": revoked,
            "request_path": str(self._request_path(run_date)),
            "approved_path": str(self._approved_path(run_date)),
            "markdown_request": str(self._markdown_request_path(run_date)),
        }
        write_json(self.output_root / "live_approvals" / "current.json", [payload])
        write_json(self.output_root / "live_approvals" / f"{run_date}.json", [payload])
        return payload

    def _latest(self, name: str, run_date: str) -> dict:
        rows = load_json(self.output_root / name / f"{run_date}.json")
        if not rows:
            rows = load_json(self.output_root / name / "current.json")
        return rows[-1] if rows else {}

    def _read_one(self, path: Path) -> dict:
        rows = load_json(path)
        return rows[-1] if rows else {}

    def _request_path(self, run_date: str) -> Path:
        return self.output_root / "live_approvals" / f"{run_date}.request.json"

    def _approved_path(self, run_date: str) -> Path:
        return self.output_root / "live_approvals" / f"{run_date}.approved.json"

    def _revoked_path(self, run_date: str) -> Path:
        return self.output_root / "live_approvals" / f"{run_date}.revoked.json"

    def _markdown_request_path(self, run_date: str) -> Path:
        return self.output_root / "live_approvals" / f"{run_date}.request.md"

    def _write_markdown_request(self, run_date: str, request: dict) -> None:
        lines = [
            f"# Live Approval Request - {run_date}",
            "",
            f"- Status: {request['status']}",
            f"- Activation status: {request['activation_status']}",
            f"- Dry-run ready: {request['dry_run_ready']}",
            f"- Real-money ready: {request['real_money_ready']}",
            f"- Failed checks: {', '.join(request['failed_checks']) or 'none'}",
            f"- Notes: {request['notes'] or 'n/a'}",
            "",
            "## Review Required",
            "",
        ]
        lines.extend([f"- {item}" for item in request["review_required"]])
        lines.extend(
            [
                "",
                "## Approval Command",
                "",
                "```bash",
                f"python3 -m pipelines.live_approval --date {run_date} --action approve --approver <name> --notes \"reviewed dashboard, journal, risk, broker state\"",
                "```",
                "",
                "Approval should only be recorded after live activation is dry-run ready and the human reviewer accepts the risk.",
            ]
        )
        path = self._markdown_request_path(run_date)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _now(self) -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
