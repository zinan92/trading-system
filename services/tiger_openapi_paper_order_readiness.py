from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import load_json, write_json
from services.tiger_venue_status import TigerVenueStatus


PAPER_ORDER_REFRESH_RUNBOOK_SCHEMA_VERSION = "tiger-paper-order-readiness-refresh-runbook-v1"


class TigerOpenApiPaperOrderReadiness:
    """Artifact-only readiness gate for an explicitly attended Tiger paper order.

    This gate never opens Tiger SDK clients and never submits, previews, cancels,
    modifies, or closes orders. It only evaluates whether the local evidence
    surface is strong enough for the operator to explicitly authorize the next
    attended paper-order canary.
    """

    def __init__(self, output_root: Path | None = None) -> None:
        config = load_pipeline_config()
        self.config = config
        self.output_root = Path(output_root) if output_root else ROOT / str(config.get("output_root", "outputs"))

    def run(self, run_date: str) -> dict:
        venue = TigerVenueStatus(self.output_root).snapshot()
        profile = self._profile()
        checks = [
            self._venue_ready(venue),
            self._contract_ready(venue, run_date),
            self._reconciliation_flat(venue, run_date),
            self._account_sync_ready(venue, run_date),
            self._order_sync_ready(venue, run_date),
            self._kill_switch_clear(venue, run_date),
            self._paper_order_drill_passed(venue, run_date),
            self._checked_in_profile_safe(profile),
            self._operational_invariants_configured(profile),
        ]
        blockers = [item for item in checks if item["status"] != "pass"]
        ready = not blockers
        payload = {
            "schema_version": "tiger-openapi-paper-order-readiness-v1",
            "run_date": run_date,
            "checked_at": self._now(),
            "status": "ready_for_attended_paper_order" if ready else "blocked",
            "ready_for_attended_paper_order": ready,
            "can_submit_without_explicit_operator_authorization": False,
            "real_tiger_network_call_attempted": False,
            "provider": "tiger_openapi",
            "environment": "paper",
            "checks": checks,
            "blockers": blockers,
            "venue_status": venue.get("status"),
            "required_runtime_overrides_for_attended_canary": {
                "dry_run": False,
                "network_order_submission": "paper_tradeclient",
                "confirm_tiger_paper_orders": True,
                "require_reconciliation_before_entry": True,
                "require_live_money_guardrails_before_entry": True,
                "require_attached_protection_before_entry": True,
            },
            "operator_authorization_required": {
                "required": True,
                "reason": "Real Tiger paper preview/place is intentionally not run by this readiness gate.",
                "must_include": [
                    "explicit request to run a Tiger paper order canary",
                    "ticket id and dated contract",
                    "limit entry, stop_loss, and take-profit target",
                    "confirmation that paper TradeClient network submission is intended",
                ],
            },
            "next_commands": self._next_commands(run_date),
            "evidence_paths": self._evidence_paths(run_date),
            "venue_summary": {
                "contract": venue.get("contract", {}),
                "reconciliation": venue.get("reconciliation", {}),
                "account_sync": venue.get("account_sync", {}),
                "order_sync": venue.get("order_sync", {}),
                "kill_switch": venue.get("kill_switch", {}),
                "paper_order_drill": venue.get("paper_order_drill", {}),
            },
        }
        write_json(self.output_root / "tiger_paper_order_readiness" / "current.json", [payload])
        write_json(self.output_root / "tiger_paper_order_readiness" / f"{run_date}.json", [payload])
        self._write_markdown(run_date, payload)
        return payload

    def refresh_runbook(self, run_date: str, *, persist: bool = True) -> dict:
        readiness = self._latest_readiness_receipt()
        blockers = [row for row in (readiness.get("blockers") or []) if isinstance(row, dict)]
        stale_blockers = [
            row
            for row in blockers
            if isinstance(row.get("evidence"), dict) and row["evidence"].get("current_for_run_date") is False
        ]
        command_sequence = [self._refresh_step(str(command)) for command in (readiness.get("next_commands") or []) if str(command)]
        status = "ready_for_operator_refresh" if blockers and command_sequence else "not_required"
        if not readiness:
            status = "missing_readiness"
        receipt = {
            "schema_version": PAPER_ORDER_REFRESH_RUNBOOK_SCHEMA_VERSION,
            "runbook_id": self._runbook_id(),
            "checked_at": self._now(),
            "run_date": run_date,
            "status": status,
            "readiness_status": str(readiness.get("status") or "missing"),
            "readiness_checked_at": str(readiness.get("checked_at") or ""),
            "ready_for_attended_paper_order": readiness.get("ready_for_attended_paper_order") is True,
            "can_enter_attended_paper_order": readiness.get("ready_for_attended_paper_order") is True,
            "blocker_count": len(blockers),
            "stale_evidence_count": len(stale_blockers),
            "blocker_names": [str(row.get("name") or "") for row in blockers if row.get("name")],
            "command_sequence": command_sequence,
            "generation_safety": {
                "artifact_only": True,
                "opens_quote_client": False,
                "opens_trade_client": False,
                "submits_orders": False,
                "cancels_orders": False,
                "closes_positions": False,
                "writes_runtime_config": False,
                "credential_values_exposed": False,
            },
            "command_sequence_safety": {
                "opens_trade_client_read_only": any(step["opens_trade_client"] for step in command_sequence),
                "submits_orders": any(step["submits_orders"] for step in command_sequence),
                "writes_runtime_config": any(step["writes_runtime_config"] for step in command_sequence),
            },
            "evidence_paths": {
                "readiness": str(self.output_root / "tiger_paper_order_readiness" / "current.json"),
                "runbook_json": str(self.output_root / "tiger_paper_order_readiness" / "refresh_runbook_current.json"),
                "runbook_markdown": str(self.output_root / "tiger_paper_order_readiness" / "refresh_runbook_current.md"),
            },
        }
        if persist:
            self._write_refresh_runbook(receipt)
        return receipt

    def _venue_ready(self, venue: dict) -> dict:
        return self._check(
            "venue_status",
            venue.get("status") == "ready",
            "Tiger venue artifact read model is ready.",
            "Tiger venue is not ready.",
            {"status": venue.get("status"), "headline": venue.get("headline")},
        )

    def _contract_ready(self, venue: dict, run_date: str) -> dict:
        contract = venue.get("contract", {}) if isinstance(venue.get("contract"), dict) else {}
        evidence = self._evidence_with_run_date(contract, run_date)
        return self._check(
            "dated_contract",
            evidence["current_for_run_date"] is True and contract.get("ready") is True and bool(contract.get("execution_symbol")),
            "Tiger execution contract is dated and outside rollover block.",
            "Tiger execution contract evidence is missing, blocked, or stale.",
            evidence,
        )

    def _reconciliation_flat(self, venue: dict, run_date: str) -> dict:
        reconciliation = venue.get("reconciliation", {}) if isinstance(venue.get("reconciliation"), dict) else {}
        evidence = self._evidence_with_run_date(reconciliation, run_date)
        return self._check(
            "reconciliation_flat",
            evidence["current_for_run_date"] is True
            and reconciliation.get("confirmation_status") == "confirmed_flat"
            and int(reconciliation.get("position_count") or 0) == 0
            and int(reconciliation.get("open_order_count") or 0) == 0,
            "Tiger reconciliation confirms no positions and no open orders.",
            "Tiger reconciliation is stale or not flat.",
            evidence,
        )

    def _account_sync_ready(self, venue: dict, run_date: str) -> dict:
        account = venue.get("account_sync", {}) if isinstance(venue.get("account_sync"), dict) else {}
        evidence = self._evidence_with_run_date(account, run_date)
        return self._check(
            "account_sync",
            evidence["current_for_run_date"] is True
            and account.get("sync_status") == "synced"
            and account.get("balance_present") is True
            and account.get("accounting_observed") is True,
            "Tiger account sync has balance and accounting evidence.",
            "Tiger account sync is stale or missing balance/accounting evidence.",
            evidence,
        )

    def _order_sync_ready(self, venue: dict, run_date: str) -> dict:
        sync = venue.get("order_sync", {}) if isinstance(venue.get("order_sync"), dict) else {}
        evidence = self._evidence_with_run_date(sync, run_date)
        return self._check(
            "order_sync",
            evidence["current_for_run_date"] is True and sync.get("sync_status") == "synced" and int(sync.get("open_order_count") or 0) == 0,
            "Tiger order/fill sync is current with no open orders.",
            "Tiger order/fill sync is stale, missing, or still has open orders.",
            evidence,
        )

    def _kill_switch_clear(self, venue: dict, run_date: str) -> dict:
        safety = venue.get("safety", {}) if isinstance(venue.get("safety"), dict) else {}
        kill = venue.get("kill_switch", {}) if isinstance(venue.get("kill_switch"), dict) else {}
        evidence = self._evidence_with_run_date(kill, run_date)
        evidence["safety"] = safety
        return self._check(
            "kill_switch_clear",
            evidence["current_for_run_date"] is True
            and safety.get("network_modification_observed") is False
            and kill.get("network_cancel_created") is False
            and kill.get("network_order_created") is False,
            "No Tiger kill-switch network modification is observed.",
            "Tiger kill-switch evidence is stale or a network modification requires manual review.",
            evidence,
        )

    def _paper_order_drill_passed(self, venue: dict, run_date: str) -> dict:
        drill = venue.get("paper_order_drill", {}) if isinstance(venue.get("paper_order_drill"), dict) else {}
        evidence = self._evidence_with_run_date(drill, run_date)
        return self._check(
            "paper_order_drill",
            evidence["current_for_run_date"] is True
            and drill.get("status") == "pass"
            and drill.get("real_tiger_network_call_attempted") is False
            and int(drill.get("scenario_count") or 0) >= 2,
            "Local fake-client Tiger paper order drill passed without real Tiger network calls.",
            "Local fake-client Tiger paper order drill is stale, missing, or not passing.",
            evidence,
        )

    def _checked_in_profile_safe(self, profile: dict) -> dict:
        evidence = {
            "dry_run": profile.get("dry_run"),
            "network_order_submission": profile.get("network_order_submission"),
            "confirm_tiger_paper_orders": profile.get("confirm_tiger_paper_orders"),
        }
        safe = (
            profile.get("dry_run") is True
            and str(profile.get("network_order_submission") or "") == "not_implemented_fail_closed"
            and profile.get("confirm_tiger_paper_orders") is False
        )
        return self._check(
            "checked_in_profile_safe",
            safe,
            "Checked-in Tiger profile remains non-network by default.",
            "Checked-in Tiger profile is no longer fail-closed by default.",
            evidence,
        )

    def _operational_invariants_configured(self, profile: dict) -> dict:
        required = {
            "require_reconciliation_before_entry": True,
            "require_live_money_guardrails_before_entry": True,
            "require_attached_protection_before_entry": True,
            "enable_tiger_kill_switch_network_actions": False,
        }
        evidence = {key: profile.get(key) for key in required}
        return self._check(
            "operational_invariants",
            all(profile.get(key) is value for key, value in required.items()),
            "Tiger profile keeps reconciliation, money guardrails, protection, and kill-switch invariants configured.",
            "Tiger profile is missing one or more required operational invariants.",
            {"expected": required, "actual": evidence},
        )

    def _profile(self) -> dict:
        profile = (self.config.get("broker_profiles", {}) or {}).get("tiger_openapi_paper", {})
        if not isinstance(profile, dict):
            return {}
        return dict(profile)

    def _check(self, name: str, passed: bool, pass_summary: str, fail_summary: str, evidence: dict) -> dict:
        return {
            "name": name,
            "status": "pass" if passed else "fail",
            "summary": pass_summary if passed else fail_summary,
            "evidence": evidence,
        }

    def _evidence_with_run_date(self, evidence: dict[str, Any], run_date: str) -> dict[str, Any]:
        enriched = dict(evidence)
        checked_at = str(enriched.get("checked_at") or "")
        enriched["required_run_date"] = run_date
        enriched["current_for_run_date"] = checked_at.startswith(run_date)
        return enriched

    def _next_commands(self, run_date: str) -> list[str]:
        return [
            f"python3 -m pipelines.tiger_contract_status --date {run_date} --symbol MGCmain --json",
            f"python3 -m pipelines.tiger_openapi_reconciliation --date {run_date} --json",
            f"python3 -m pipelines.tiger_openapi_account_sync --date {run_date} --json",
            f"python3 -m pipelines.tiger_openapi_order_sync --date {run_date} --json",
            f"python3 -m pipelines.tiger_openapi_paper_order_drill --date {run_date} --json",
            f"python3 -m pipelines.tiger_openapi_paper_order_readiness --date {run_date} --json",
        ]

    def _latest_readiness_receipt(self) -> dict[str, Any]:
        rows = load_json(self.output_root / "tiger_paper_order_readiness" / "current.json")
        if not isinstance(rows, list) or not rows or not isinstance(rows[-1], dict):
            return {}
        return rows[-1]

    def _refresh_step(self, command: str) -> dict[str, Any]:
        if "tiger_contract_status" in command:
            name = "refresh_contract_status"
            label = "refresh contract"
            opens_trade_client = False
        elif "tiger_openapi_reconciliation" in command:
            name = "refresh_reconciliation"
            label = "refresh reconciliation"
            opens_trade_client = True
        elif "tiger_openapi_account_sync" in command:
            name = "refresh_account_sync"
            label = "refresh account"
            opens_trade_client = True
        elif "tiger_openapi_order_sync" in command:
            name = "refresh_order_sync"
            label = "refresh orders/fills"
            opens_trade_client = True
        elif "tiger_openapi_paper_order_drill" in command:
            name = "refresh_local_drill"
            label = "run local drill"
            opens_trade_client = False
        elif "tiger_openapi_paper_order_readiness" in command:
            name = "refresh_readiness"
            label = "rerun readiness"
            opens_trade_client = False
        else:
            name = "refresh_evidence"
            label = "refresh evidence"
            opens_trade_client = True
        return {
            "name": name,
            "label": label,
            "command": command,
            "opens_trade_client": opens_trade_client,
            "opens_trade_client_mode": "read_only" if opens_trade_client else "none",
            "submits_orders": False,
            "writes_runtime_config": False,
        }

    def _evidence_paths(self, run_date: str) -> dict:
        return {
            "readiness": str(self.output_root / "tiger_paper_order_readiness" / f"{run_date}.json"),
            "contract": str(self.output_root / "tiger_contracts" / "current.json"),
            "reconciliation": str(self.output_root / "tiger_reconciliation" / "current.json"),
            "account_sync": str(self.output_root / "tiger_account_sync" / "current.json"),
            "order_sync": str(self.output_root / "tiger_order_sync" / "current.json"),
            "kill_switch": str(self.output_root / "tiger_kill_switch" / "current.json"),
            "paper_order_drill": str(self.output_root / "tiger_paper_order_drill" / "current.json"),
            "venue_status": str(self.output_root / "tiger_venue_status_derived_from_artifacts"),
        }

    def _write_markdown(self, run_date: str, payload: dict) -> None:
        lines = [
            f"# Tiger Paper Order Readiness - {run_date}",
            "",
            f"- Status: {payload['status']}",
            f"- Ready for attended paper order: {payload['ready_for_attended_paper_order']}",
            f"- Can submit without explicit operator authorization: {payload['can_submit_without_explicit_operator_authorization']}",
            f"- Real Tiger network call attempted: {payload['real_tiger_network_call_attempted']}",
            "",
            "## Checks",
        ]
        for item in payload["checks"]:
            lines.append(f"- {item['status']}: {item['name']} - {item['summary']}")
        lines.extend(["", "## Blockers"])
        if payload["blockers"]:
            for item in payload["blockers"]:
                lines.append(f"- {item['name']}: {item['summary']}")
        else:
            lines.append("- none")
        lines.extend(["", "## Next Commands"])
        lines.extend(f"- `{command}`" for command in payload["next_commands"])
        path = self.output_root / "tiger_paper_order_readiness" / f"{run_date}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _write_refresh_runbook(self, receipt: dict[str, Any]) -> None:
        base = self.output_root / "tiger_paper_order_readiness"
        write_json(base / "refresh_runbook_current.json", [receipt])
        write_json(base / f"{receipt['runbook_id']}.json", [receipt])
        (base / "refresh_runbook_current.md").write_text(self._refresh_runbook_markdown(receipt), encoding="utf-8")
        history = load_json(base / "refresh_runbook_history.json")
        history.append(receipt)
        write_json(base / "refresh_runbook_history.json", history[-50:])

    def _refresh_runbook_markdown(self, receipt: dict[str, Any]) -> str:
        generation = receipt.get("generation_safety", {}) if isinstance(receipt.get("generation_safety"), dict) else {}
        sequence_safety = receipt.get("command_sequence_safety", {}) if isinstance(receipt.get("command_sequence_safety"), dict) else {}
        lines = [
            "# Tiger Paper-Order Evidence Refresh Runbook",
            "",
            f"- Status: `{receipt.get('status', '')}`",
            f"- Runbook ID: `{receipt.get('runbook_id', '')}`",
            f"- Run date: `{receipt.get('run_date', '')}`",
            f"- Readiness status: `{receipt.get('readiness_status', '')}`",
            f"- Blockers: `{receipt.get('blocker_count', 0)}`",
            f"- Stale evidence: `{receipt.get('stale_evidence_count', 0)}`",
            "",
            "## Objective",
            "",
            "- Refresh Tiger paper-order evidence before any explicitly attended TradeClient canary.",
            "- Keep evidence refresh, paper canary authorization, and real-money execution as separate gates.",
            "",
            "## Generation Safety",
            "",
        ]
        for key in [
            "artifact_only",
            "opens_quote_client",
            "opens_trade_client",
            "submits_orders",
            "cancels_orders",
            "closes_positions",
            "writes_runtime_config",
            "credential_values_exposed",
        ]:
            lines.append(f"- {key}: `{generation.get(key) is True}`")
        lines.extend(
            [
                "",
                "## Command Sequence Safety",
                "",
                f"- opens_trade_client_read_only: `{sequence_safety.get('opens_trade_client_read_only') is True}`",
                f"- submits_orders: `{sequence_safety.get('submits_orders') is True}`",
                f"- writes_runtime_config: `{sequence_safety.get('writes_runtime_config') is True}`",
                "",
                "## Command Sequence",
                "",
            ]
        )
        commands = [row for row in (receipt.get("command_sequence") or []) if isinstance(row, dict)]
        if not commands:
            lines.append("- No evidence refresh command is required from the current readiness receipt.")
        for index, row in enumerate(commands, start=1):
            lines.extend(
                [
                    f"{index}. {row.get('label', '')}",
                    f"   - command: `{row.get('command', '')}`",
                    f"   - opens_trade_client: `{row.get('opens_trade_client') is True}`",
                    f"   - opens_trade_client_mode: `{row.get('opens_trade_client_mode', '')}`",
                    f"   - submits_orders: `{row.get('submits_orders') is True}`",
                    f"   - writes_runtime_config: `{row.get('writes_runtime_config') is True}`",
                ]
            )
        lines.extend(["", "## Blockers", ""])
        blocker_names = [str(name) for name in (receipt.get("blocker_names") or []) if name]
        if blocker_names:
            lines.extend(f"- `{name}`" for name in blocker_names)
        else:
            lines.append("- none")
        return "\n".join(lines) + "\n"

    def _now(self) -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    def _runbook_id(self) -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return f"paper_order_refresh_runbook_{stamp}"
