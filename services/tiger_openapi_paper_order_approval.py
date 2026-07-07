from __future__ import annotations

import os
import shlex
import stat
from datetime import datetime, timezone
from pathlib import Path

from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import load_json, write_json
from services.tiger_openapi_paper_order_canary import ACKNOWLEDGEMENT, TigerOpenApiPaperOrderCanary


APPROVAL_SCHEMA_VERSION = "tiger-openapi-paper-order-approval-v1"


class TigerOpenApiPaperOrderApproval:
    """Artifact-only approval package for an attended Tiger paper canary.

    This service never calls Tiger SDK clients and never submits, previews,
    cancels, modifies, or closes orders. It converts a ready M15/M16 canary
    check into an operator-facing runbook artifact.
    """

    def __init__(self, output_root: Path | None = None, *, config: dict | None = None) -> None:
        self.config = config if config is not None else load_pipeline_config()
        self.output_root = Path(output_root) if output_root else ROOT / str(self.config.get("output_root", "outputs"))

    def build(
        self,
        run_date: str,
        *,
        ticket_id: str,
        asset: str,
        side: str,
        quantity: int,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        operator: str = "manual",
        props_path: str = "",
        use_attended_canary_risk_limits: bool = False,
        notes: str = "attended Tiger paper order canary approval package",
    ) -> dict:
        canary = TigerOpenApiPaperOrderCanary(self.output_root, config=self.config).check(
            run_date,
            ticket_id=ticket_id,
            asset=asset,
            side=side,
            quantity=quantity,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            operator=operator,
            notes=notes,
            use_attended_canary_risk_limits=use_attended_canary_risk_limits,
        )
        props_check = self._props_check(props_path)
        checks = [
            self._check(
                "canary_check",
                canary.get("status") == "ready_for_operator_authorization"
                and canary.get("submit_requested") is False
                and canary.get("real_tiger_network_call_attempted") is False,
                "Ticket-specific canary check is ready for operator authorization.",
                "Ticket-specific canary check is not ready.",
                canary,
            ),
            props_check,
            self._check(
                "operator_attendance",
                bool(str(operator).strip()),
                "Operator label is present for the attended runbook.",
                "Operator label is missing.",
                {"operator": operator},
            ),
        ]
        blockers = [item for item in checks if item["status"] != "pass"]
        ready = not blockers
        resolved_props_path = props_check["evidence"].get("props_path", "")
        submit_command = self._submit_command(
            run_date,
            ticket_id=ticket_id,
            asset=asset,
            side=side,
            quantity=quantity,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            operator=operator,
            props_path=resolved_props_path,
            use_attended_canary_risk_limits=use_attended_canary_risk_limits,
        )
        payload = {
            "schema_version": APPROVAL_SCHEMA_VERSION,
            "run_date": run_date,
            "checked_at": self._now(),
            "status": "ready_for_operator_approval" if ready else "blocked",
            "provider": "tiger_openapi",
            "environment": "paper",
            "operator": operator,
            "ticket_id": ticket_id,
            "can_submit_without_explicit_operator_authorization": False,
            "real_tiger_network_call_attempted": False,
            "submit_requested": False,
            "use_attended_canary_risk_limits": use_attended_canary_risk_limits,
            "checks": checks,
            "blockers": blockers,
            "canary_check": self._canary_summary(canary),
            "operator_authorization_required": {
                "required": True,
                "reason": "This approval package only prepares the attended Tiger paper canary; it does not authorize or submit it.",
                "must_confirm": [
                    "operator is at the machine and watching Tiger paper account UI",
                    "fresh read-only reconciliation/order sync is still flat immediately before submit",
                    "risk package and ticket prices are intentionally accepted",
                    "manual cancel/close path in Tiger UI is ready",
                    "the exact acknowledgement phrase is included in the submit command",
                ],
                "acknowledgement": ACKNOWLEDGEMENT,
            },
            "pre_submit_refresh_commands": self._pre_submit_refresh_commands(
                run_date,
                props_path=resolved_props_path,
                ticket_id=ticket_id,
                asset=asset,
                side=side,
                quantity=quantity,
                entry_price=entry_price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                operator=operator,
                use_attended_canary_risk_limits=use_attended_canary_risk_limits,
            ),
            "submit_command": submit_command,
            "post_submit_read_only_commands": self._post_submit_commands(run_date, props_path=resolved_props_path),
            "manual_kill_path": {
                "primary": "Use Tiger paper account UI to cancel the open parent/protective orders or close the paper futures position manually.",
                "local_halt_dry_run": f"python3 -m pipelines.tiger_openapi_kill_switch --date {shlex.quote(run_date)} --json",
                "network_kill_switch_enabled_by_default": False,
                "confirm_flat_after_manual_action": self._env_prefix(resolved_props_path)
                + f"python3 -m pipelines.tiger_openapi_reconciliation --date {shlex.quote(run_date)} --json",
            },
            "approval_record_path": str(self.output_root / "tiger_paper_order_approval" / f"{run_date}.json"),
            "evidence_paths": {
                "approval": str(self.output_root / "tiger_paper_order_approval" / f"{run_date}.json"),
                "approval_current": str(self.output_root / "tiger_paper_order_approval" / "current.json"),
                "canary": str(self.output_root / "tiger_paper_order_canary" / "current.json"),
                "readiness": str(self.output_root / "tiger_paper_order_readiness" / "current.json"),
                "reconciliation": str(self.output_root / "tiger_reconciliation" / "current.json"),
                "account_sync": str(self.output_root / "tiger_account_sync" / "current.json"),
                "order_sync": str(self.output_root / "tiger_order_sync" / "current.json"),
            },
        }
        self._write_payload(run_date, payload)
        return payload

    def _props_check(self, props_path: str) -> dict:
        env_name = str(self._profile().get("props_path_env", "TIGER_OPENAPI_CONFIG_PATH"))
        resolved = props_path or os.getenv(env_name, "")
        path = Path(resolved).expanduser() if resolved else None
        exists = bool(path and path.exists())
        mode = ""
        owner_only = False
        if exists and path is not None:
            mode_int = stat.S_IMODE(path.stat().st_mode)
            mode = oct(mode_int)
            owner_only = not bool(mode_int & 0o077)
        return self._check(
            "tiger_props_file",
            bool(resolved) and exists and owner_only,
            "Tiger OpenAPI properties file exists and is owner-only.",
            "Tiger OpenAPI properties file is missing or not owner-only.",
            {
                "props_path_env": env_name,
                "props_path": str(path) if path else "",
                "props_path_present": bool(resolved),
                "props_path_exists": exists,
                "props_path_mode": mode,
                "props_path_owner_only": owner_only,
            },
        )

    def _submit_command(
        self,
        run_date: str,
        *,
        ticket_id: str,
        asset: str,
        side: str,
        quantity: int,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        operator: str,
        props_path: str,
        use_attended_canary_risk_limits: bool,
    ) -> str:
        risk_flag = " --use-attended-canary-risk-limits" if use_attended_canary_risk_limits else ""
        return (
            self._env_prefix(props_path)
            + "python3 -m pipelines.tiger_openapi_paper_order_canary "
            f"--date {shlex.quote(run_date)} --ticket-id {shlex.quote(ticket_id)} --asset {shlex.quote(asset)} "
            f"--side {shlex.quote(side)} --quantity {int(quantity)} --entry-price {float(entry_price)} "
            f"--stop-loss {float(stop_loss)} --take-profit {float(take_profit)} --operator {shlex.quote(operator)}"
            f"{risk_flag} --submit-tiger-paper-canary --confirm-tiger-paper-canary "
            f"--acknowledge-tiger-paper-network-submission {ACKNOWLEDGEMENT} --json"
        )

    def _pre_submit_refresh_commands(
        self,
        run_date: str,
        *,
        props_path: str,
        ticket_id: str,
        asset: str,
        side: str,
        quantity: int,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        operator: str,
        use_attended_canary_risk_limits: bool,
    ) -> list[str]:
        prefix = self._env_prefix(props_path)
        risk_flag = " --use-attended-canary-risk-limits" if use_attended_canary_risk_limits else ""
        return [
            f"python3 -m pipelines.tiger_contract_status --date {shlex.quote(run_date)} --symbol MGCmain --json",
            prefix + f"python3 -m pipelines.tiger_openapi_reconciliation --date {shlex.quote(run_date)} --json",
            prefix + f"python3 -m pipelines.tiger_openapi_account_sync --date {shlex.quote(run_date)} --json",
            prefix + f"python3 -m pipelines.tiger_openapi_order_sync --date {shlex.quote(run_date)} --json",
            f"python3 -m pipelines.tiger_openapi_paper_order_readiness --date {shlex.quote(run_date)} --json",
            (
                "python3 -m pipelines.tiger_openapi_paper_order_canary "
                f"--date {shlex.quote(run_date)} --ticket-id {shlex.quote(ticket_id)} --asset {shlex.quote(asset)} "
                f"--side {shlex.quote(side)} --quantity {int(quantity)} --entry-price {float(entry_price)} "
                f"--stop-loss {float(stop_loss)} --take-profit {float(take_profit)} --operator {shlex.quote(operator)}"
                f"{risk_flag} --json"
            ),
        ]

    def _post_submit_commands(self, run_date: str, *, props_path: str) -> list[str]:
        prefix = self._env_prefix(props_path)
        return [
            prefix + f"python3 -m pipelines.tiger_openapi_order_sync --date {shlex.quote(run_date)} --json",
            prefix + f"python3 -m pipelines.tiger_openapi_reconciliation --date {shlex.quote(run_date)} --json",
            f"python3 -m pipelines.tiger_openapi_kill_switch --date {shlex.quote(run_date)} --json",
        ]

    def _canary_summary(self, canary: dict) -> dict:
        checks = {item.get("name"): item for item in canary.get("checks", []) if isinstance(item, dict)}
        money = checks.get("ticket_specific_money_guardrail", {}).get("evidence", {})
        risk = checks.get("attended_canary_risk_package", {}).get("evidence", {})
        return {
            "status": canary.get("status"),
            "submit_requested": canary.get("submit_requested"),
            "real_tiger_network_call_attempted": canary.get("real_tiger_network_call_attempted"),
            "use_attended_canary_risk_limits": canary.get("use_attended_canary_risk_limits"),
            "candidate_notional": money.get("candidate", {}).get("notional") if isinstance(money, dict) else None,
            "guardrail_status": money.get("status") if isinstance(money, dict) else "",
            "candidate_stop_loss": risk.get("candidate_stop_loss") if isinstance(risk, dict) else None,
            "candidate_stop_loss_pct_of_equity": risk.get("candidate_stop_loss_pct_of_equity") if isinstance(risk, dict) else None,
        }

    def _profile(self) -> dict:
        profile = (self.config.get("broker_profiles", {}) or {}).get("tiger_openapi_paper", {})
        if not isinstance(profile, dict) or not profile:
            raise RuntimeError("broker_profiles.tiger_openapi_paper is required")
        return dict(profile)

    def _env_prefix(self, props_path: str) -> str:
        if not props_path:
            return ""
        env_name = str(self._profile().get("props_path_env", "TIGER_OPENAPI_CONFIG_PATH"))
        return f"{env_name}={shlex.quote(props_path)} "

    def _check(self, name: str, passed: bool, pass_summary: str, fail_summary: str, evidence: dict) -> dict:
        return {
            "name": name,
            "status": "pass" if passed else "fail",
            "summary": pass_summary if passed else fail_summary,
            "evidence": evidence,
        }

    def _write_payload(self, run_date: str, payload: dict) -> None:
        base = self.output_root / "tiger_paper_order_approval"
        write_json(base / "current.json", [payload])
        dated = base / f"{run_date}.json"
        rows = load_json(dated)
        rows.append(payload)
        write_json(dated, rows)
        self._write_markdown(run_date, payload)

    def _write_markdown(self, run_date: str, payload: dict) -> None:
        lines = [
            f"# Tiger Paper Order Approval - {run_date}",
            "",
            f"- Status: {payload['status']}",
            f"- Submit requested: {payload['submit_requested']}",
            f"- Can submit without explicit operator authorization: {payload['can_submit_without_explicit_operator_authorization']}",
            f"- Real Tiger network call attempted: {payload['real_tiger_network_call_attempted']}",
            "",
            "## Checks",
        ]
        for item in payload["checks"]:
            lines.append(f"- {item['status']}: {item['name']} - {item['summary']}")
        lines.extend(["", "## Pre-submit refresh"])
        lines.extend(f"- `{command}`" for command in payload["pre_submit_refresh_commands"])
        lines.extend(["", "## Submit command"])
        lines.append(f"- `{payload['submit_command']}`")
        lines.extend(["", "## Post-submit read-only commands"])
        lines.extend(f"- `{command}`" for command in payload["post_submit_read_only_commands"])
        lines.extend(["", "## Manual kill path"])
        lines.append(f"- {payload['manual_kill_path']['primary']}")
        lines.append(f"- `{payload['manual_kill_path']['local_halt_dry_run']}`")
        path = self.output_root / "tiger_paper_order_approval" / f"{run_date}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _now(self) -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
