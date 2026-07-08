from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import load_json, write_json


class OperationRunbook:
    def __init__(self, output_root: Path | None = None) -> None:
        config = load_pipeline_config()
        self.output_root = output_root or ROOT / config.get("output_root", "outputs")

    def run(self, run_date: str) -> dict:
        data_source = self._latest_artifact("data_source_preflight", run_date)
        official_feed = self._latest_artifact("official_feed_receipts", run_date)
        risk_monitor = self._latest_artifact("risk_monitor", run_date)
        live_readiness = self._latest_artifact("live_readiness", run_date)
        live_activation = self._latest_artifact("live_activation", run_date)
        paper_risk_actions = self._latest_artifact("paper_risk_action_plan", run_date)
        daily_review = self._latest_artifact("daily_review_runs", run_date)
        runner = self._runner_for_date(run_date)
        pending = load_json(self.output_root / "journal_pending" / f"{run_date}.json")
        decisions = load_json(self.output_root / "journal_decisions" / f"{run_date}.json")
        journal_path = self.output_root / "journals" / f"{run_date}.md"
        review_path = self.output_root / "review_notes" / f"{run_date}.md"

        permissions = {
            "paper_manual_review": bool(data_source.get("ready_for_paper")),
            "paper_auto_approve": bool(data_source.get("ready_for_paper"))
            and bool(risk_monitor.get("allow_paper_auto_approve"))
            and paper_risk_actions.get("status") != "action_required",
            "live_trading": bool(live_readiness.get("live_ready")) and bool(live_activation.get("real_money_ready")),
        }
        blocks = self._blocks(data_source, official_feed, risk_monitor, paper_risk_actions, live_readiness, live_activation, journal_path, review_path)
        status = "live_ready" if permissions["live_trading"] else "blocked"
        if permissions["live_trading"]:
            status = "live_ready"
        elif permissions["paper_manual_review"] and not permissions["paper_auto_approve"]:
            status = "paper_manual_only"
        elif permissions["paper_auto_approve"]:
            status = "paper_auto_ready"
        if not permissions["paper_manual_review"]:
            status = "data_blocked"

        payload = {
            "run_date": run_date,
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "status": status,
            "mode": "paper" if not permissions["live_trading"] else "live",
            "permissions": permissions,
            "summary": {
                "runner_state": runner.get("state", ""),
                "latest_price": data_source.get("latest_price"),
                "latest_provider": data_source.get("latest_provider", ""),
                "price_sanity": (data_source.get("price_sanity") or {}).get("passes"),
                "data_source_status": data_source.get("status", ""),
                "official_rows": official_feed.get("official_rows", data_source.get("official_rows", 0)),
                "official_feed_status": official_feed.get("status", ""),
                "risk_status": risk_monitor.get("status", ""),
                "kill_switch_active": risk_monitor.get("kill_switch_active"),
                "paper_risk_action_plan": paper_risk_actions.get("status", ""),
                "paper_risk_actions": (paper_risk_actions.get("summary") or {}).get("action_count", 0),
                "paper_risk_high_priority": (paper_risk_actions.get("summary") or {}).get("high_priority", 0),
                "live_readiness": live_readiness.get("status", ""),
                "live_ready": live_readiness.get("live_ready"),
                "daily_review": daily_review.get("status", ""),
                "pending_decisions": len(pending),
                "decisions": len(decisions),
                "journal_exists": journal_path.exists(),
                "review_notes_exists": review_path.exists(),
            },
            "blocks": blocks,
            "next_actions": self._next_actions(blocks, official_feed, live_readiness, risk_monitor, paper_risk_actions, journal_path, review_path),
            "artifacts": {
                "dashboard": "http://127.0.0.1:8765/dashboard-v4.html",
                "journal": str(journal_path),
                "review_notes": str(review_path),
                "daily_review": str(self.output_root / "daily_review_runs" / f"{run_date}.json"),
                "risk_monitor": str(self.output_root / "risk_monitor" / f"{run_date}.json"),
                "paper_risk_action_plan": str(self.output_root / "paper_risk_action_plan" / f"{run_date}.json"),
                "official_feed_receipt": str(self.output_root / "official_feed_receipts" / f"{run_date}.json"),
                "live_readiness": str(self.output_root / "live_readiness" / f"{run_date}.json"),
            },
        }
        write_json(self.output_root / "operation_runbooks" / "current.json", [payload])
        write_json(self.output_root / "operation_runbooks" / f"{run_date}.json", [payload])
        self._write_markdown(payload)
        return payload

    def _blocks(
        self,
        data_source: dict,
        official_feed: dict,
        risk_monitor: dict,
        paper_risk_actions: dict,
        live_readiness: dict,
        live_activation: dict,
        journal_path: Path,
        review_path: Path,
    ) -> list[dict]:
        blocks: list[dict] = []
        if not data_source.get("ready_for_paper"):
            blocks.append({"scope": "paper", "name": "data_source", "summary": data_source.get("message") or "paper data is not ready"})
        if not official_feed.get("ready_for_live"):
            blocks.append({"scope": "live", "name": "official_feed", "summary": "official XAUUSD 5m feed is not live-ready"})
        if risk_monitor.get("kill_switch_active"):
            blocks.append({"scope": "paper", "name": "risk_kill_switch", "summary": "; ".join(risk_monitor.get("summary", {}).get("block_reasons", [])) or "risk kill switch is active"})
        elif not risk_monitor.get("allow_paper_auto_approve", False):
            blocks.append({"scope": "paper_auto", "name": "risk_auto_approve", "summary": "; ".join(risk_monitor.get("summary", {}).get("auto_approval_block_reasons", [])) or "manual review required"})
        risk_action_summary = paper_risk_actions.get("summary") or {}
        if paper_risk_actions.get("status") == "action_required":
            blocks.append(
                {
                    "scope": "paper",
                    "name": "paper_risk_actions",
                    "summary": (
                        f"{risk_action_summary.get('high_priority', 0)} high-priority paper risk action(s) "
                        f"must be reviewed before auto approval or new exposure."
                    ),
                }
            )
        if not live_readiness.get("live_ready"):
            blocks.append({"scope": "live", "name": "live_readiness", "summary": ", ".join(live_readiness.get("summary", {}).get("failed_checks", [])) or "live readiness has not passed"})
        if not live_activation.get("real_money_ready"):
            blocks.append({"scope": "live", "name": "live_activation", "summary": "real-money activation gate is not ready"})
        if not journal_path.exists() or not review_path.exists():
            blocks.append({"scope": "review", "name": "journal_review", "summary": "daily Trading Journal or review notes are missing"})
        return blocks

    def _next_actions(self, blocks: list[dict], official_feed: dict, live_readiness: dict, risk_monitor: dict, paper_risk_actions: dict, journal_path: Path, review_path: Path) -> list[str]:
        actions: list[str] = []
        if any(item["name"] == "official_feed" for item in blocks):
            actions.extend(official_feed.get("next_actions", [])[:2])
        if any(item["name"] == "live_readiness" for item in blocks):
            for item in live_readiness.get("next_actions", [])[:2]:
                if item not in actions:
                    actions.append(item)
        if any(item["name"] in {"risk_kill_switch", "risk_auto_approve"} for item in blocks):
            reasons = risk_monitor.get("summary", {}).get("auto_approval_block_reasons") or risk_monitor.get("summary", {}).get("block_reasons") or []
            actions.append(f"Manual risk review required before new paper exposure: {'; '.join(reasons) or 'risk monitor is not pass'}.")
        if any(item["name"] == "paper_risk_actions" for item in blocks):
            for item in (paper_risk_actions.get("actions") or [])[:3]:
                command = item.get("command", "")
                summary = item.get("summary") or item.get("action_id") or "paper risk action"
                if command:
                    actions.append(f"{summary}: {command}")
                else:
                    actions.append(summary)
        if not journal_path.exists() or not review_path.exists():
            actions.append("Run python3 -m pipelines.daily_review --date <date> to regenerate Trading Journal and review notes.")
        return actions or ["Paper and live gates are clear for the configured execution mode; keep daily journal review active."]

    def _latest_artifact(self, folder: str, run_date: str) -> dict:
        dated = self.output_root / folder / f"{run_date}.json"
        candidates: list[dict] = []
        rows = load_json(dated)
        if rows:
            candidates.append(rows[-1])
        current = self.output_root / folder / "current.json"
        rows = load_json(current)
        if rows:
            latest = rows[-1]
            if latest.get("run_date") in {None, "", run_date}:
                candidates.append(latest)
        if not candidates:
            return {}
        return max(candidates, key=self._artifact_time)

    def _artifact_time(self, item: dict) -> str:
        return str(item.get("checked_at") or item.get("generated_at") or item.get("finished_at") or item.get("started_at") or "")

    def _runner_for_date(self, run_date: str) -> dict:
        dated = self.output_root / "runner_status" / f"{run_date}.json"
        rows = load_json(dated)
        if rows:
            return rows[-1]
        current = self.output_root / "runner_status" / "current.json"
        if not current.exists():
            return {}
        data = json.loads(current.read_text(encoding="utf-8"))
        return data if data.get("run_date") == run_date else {}

    def _write_markdown(self, payload: dict) -> None:
        lines = [
            f"# Operation Runbook - {payload['run_date']}",
            "",
            f"- Status: {payload['status']}",
            f"- Mode: {payload['mode']}",
            f"- Latest price: {payload['summary']['latest_price']} ({payload['summary']['latest_provider']})",
            f"- Paper manual review: {payload['permissions']['paper_manual_review']}",
            f"- Paper auto approve: {payload['permissions']['paper_auto_approve']}",
            f"- Live trading: {payload['permissions']['live_trading']}",
            f"- Paper risk actions: {payload['summary']['paper_risk_action_plan']} / high={payload['summary']['paper_risk_high_priority']}",
            "",
            "## Blocks",
        ]
        if payload["blocks"]:
            lines.extend(f"- {item['scope']}/{item['name']}: {item['summary']}" for item in payload["blocks"])
        else:
            lines.append("- none")
        lines.extend(["", "## Next Actions"])
        lines.extend(f"- {item}" for item in payload["next_actions"])
        path = self.output_root / "operation_runbooks" / f"{payload['run_date']}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
