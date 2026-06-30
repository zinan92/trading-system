from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import load_json, write_json


class BotSupervisor:
    def __init__(self, output_root: Path | None = None) -> None:
        config = load_pipeline_config()
        self.output_root = output_root or ROOT / config.get("output_root", "outputs")

    def run(self, run_date: str) -> dict:
        runner = self._load_mapping(self.output_root / "runner_status" / "current.json")
        data_source = self._latest("data_source_preflight", "current")
        mock_runtime = self._latest("mock_runtime", "current")
        daily_review = self._latest("daily_review_runs", "current")
        operation_runbook = self._latest("operation_runbooks", "current")
        checks = [
            self._runner_check(run_date, runner),
            self._data_check(data_source),
            self._mock_check(mock_runtime),
            self._review_check(run_date, daily_review),
            self._operation_gate_check(operation_runbook),
        ]
        payload = {
            "run_date": run_date,
            "checked_at": self._now(),
            "status": self._rollup(checks),
            "mock_bot_running": all(item["status"] == "pass" for item in checks[:4]),
            "live_trading_allowed": bool((operation_runbook.get("permissions") or {}).get("live_trading")),
            "summary": {
                "passed": sum(1 for item in checks if item["status"] == "pass"),
                "warned": sum(1 for item in checks if item["status"] == "warn"),
                "failed": sum(1 for item in checks if item["status"] == "fail"),
                "runner_state": runner.get("state", ""),
                "latest_price": data_source.get("latest_price"),
                "latest_provider": data_source.get("latest_provider", ""),
                "data_age_minutes": data_source.get("latest_record_age_minutes"),
                "operation_status": operation_runbook.get("status", ""),
            },
            "checks": checks,
            "artifacts": {
                "runner_status": str(self.output_root / "runner_status" / "current.json"),
                "data_source_preflight": str(self.output_root / "data_source_preflight" / "current.json"),
                "mock_runtime": str(self.output_root / "mock_runtime" / "current.json"),
                "daily_review": str(self.output_root / "daily_review_runs" / "current.json"),
                "operation_runbook": str(self.output_root / "operation_runbooks" / "current.json"),
            },
        }
        write_json(self.output_root / "bot_supervisor" / f"{run_date}.json", [payload])
        write_json(self.output_root / "bot_supervisor" / "current.json", [payload])
        return payload

    def _runner_check(self, run_date: str, runner: dict) -> dict:
        if not runner:
            return self._check("runner_5m", "fail", "runner_status/current.json is missing", {})
        interval = int(runner.get("interval_seconds", 300) or 300)
        timestamp = runner.get("started_at") if runner.get("state") == "running" else runner.get("finished_at")
        age = self._age_seconds(str(timestamp or runner.get("updated_at", "")))
        evidence = {**runner, "age_seconds": round(age, 2) if age is not None else None, "freshness_budget_seconds": interval * 2}
        if runner.get("run_date") != run_date:
            return self._check("runner_5m", "warn", "runner is for a different run date", evidence)
        if runner.get("state") not in {"ok", "running"}:
            return self._check("runner_5m", "fail", "runner state is not ok/running", evidence)
        if age is None or age > interval * 2:
            return self._check("runner_5m", "warn", "runner heartbeat is stale for the 5m cadence", evidence)
        return self._check("runner_5m", "pass", "runner heartbeat is fresh for the 5m cadence", evidence)

    def _data_check(self, data_source: dict) -> dict:
        if not data_source:
            return self._check("market_data", "fail", "data source preflight is missing", {})
        if not data_source.get("ready_for_paper"):
            return self._check("market_data", "fail", data_source.get("message", "paper data is not ready"), data_source)
        price_sanity = data_source.get("price_sanity") or {}
        if price_sanity and not price_sanity.get("passes", False):
            return self._check("market_data", "fail", "latest market price failed sanity checks", data_source)
        return self._check("market_data", "pass", "GOLD 5m paper data is fresh enough for mock trading", data_source)

    def _mock_check(self, mock_runtime: dict) -> dict:
        if not mock_runtime:
            return self._check("mock_runtime", "fail", "mock runtime receipt is missing", {})
        if mock_runtime.get("mock_ready") and mock_runtime.get("mock_running"):
            return self._check("mock_runtime", "pass", "mock runtime is ready and running", mock_runtime)
        if mock_runtime.get("mock_ready"):
            return self._check("mock_runtime", "warn", "mock runtime is ready but not proven running", mock_runtime)
        return self._check("mock_runtime", "fail", "mock runtime is not ready", mock_runtime)

    def _review_check(self, run_date: str, daily_review: dict) -> dict:
        if not daily_review:
            return self._check("daily_review", "fail", "daily review receipt is missing", {})
        artifacts = daily_review.get("artifacts") or {}
        missing = [name for name in ["journal", "review_notes", "report"] if not Path(artifacts.get(name, "")).exists()]
        if daily_review.get("run_date") != run_date:
            return self._check("daily_review", "warn", "daily review receipt is for a different run date", daily_review)
        if missing:
            return self._check("daily_review", "fail", "daily review artifacts are missing", {"missing": missing, "receipt": daily_review})
        if daily_review.get("status") == "fail":
            return self._check("daily_review", "fail", "daily review failed", daily_review)
        return self._check("daily_review", "pass", "daily journal, review notes, and report are present", daily_review)

    def _operation_gate_check(self, runbook: dict) -> dict:
        if not runbook:
            return self._check("operation_gate", "warn", "operation runbook is missing", {})
        permissions = runbook.get("permissions") or {}
        if permissions.get("live_trading"):
            return self._check("operation_gate", "pass", "live trading gate is open", runbook)
        if permissions.get("paper_manual_review"):
            return self._check("operation_gate", "pass", "paper manual trading gate is open and live remains blocked", runbook)
        return self._check("operation_gate", "fail", "paper and live trading gates are both blocked", runbook)

    def _latest(self, folder: str, name: str) -> dict:
        rows = load_json(self.output_root / folder / f"{name}.json")
        return rows[-1] if rows else {}

    def _load_mapping(self, path: Path) -> dict:
        if not path.exists():
            return {}
        import json

        return json.loads(path.read_text(encoding="utf-8"))

    def _rollup(self, checks: list[dict]) -> str:
        statuses = {item["status"] for item in checks}
        if "fail" in statuses:
            return "fail"
        if "warn" in statuses:
            return "warn"
        return "pass"

    def _check(self, name: str, status: str, summary: str, evidence: dict) -> dict:
        return {"name": name, "status": status, "summary": summary, "evidence": evidence}

    def _age_seconds(self, value: str) -> float | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds()

    def _now(self) -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
