from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from services.config_loader import ROOT, load_pipeline_config
from services.data_source_preflight import DataSourcePreflight
from services.journal_store import load_json, write_json


class MockTradingRuntime:
    def __init__(self, output_root: Path | None = None, market_db: Path | None = None) -> None:
        config = load_pipeline_config()
        self.config = config
        self.output_root = output_root or Path(os.getenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(ROOT / config.get("output_root", "outputs"))))
        self.market_db = market_db or Path(os.getenv("TRADING_ORCHESTRATOR_MARKET_DB", str(ROOT / config.get("local_market_db", "data/market_data.db"))))

    def run(self, run_date: str) -> dict:
        data_source = DataSourcePreflight(self.output_root, self.market_db).run(run_date)
        checks = [
            self._paper_data(data_source),
            self._strategy_artifacts(run_date),
            self._paper_account(run_date),
            self._trade_lifecycle(run_date),
            self._journal_review(run_date),
            self._runner_freshness(run_date),
            self._dashboard_artifacts(),
        ]
        status = self._rollup(checks)
        failed = [item["name"] for item in checks if item["status"] == "fail"]
        warned = [item["name"] for item in checks if item["status"] == "warn"]
        runner = next((item for item in checks if item["name"] == "runner_freshness"), {})
        payload = {
            "run_date": run_date,
            "checked_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "status": status,
            "mock_ready": not failed,
            "mock_running": runner.get("status") == "pass",
            "summary": {
                "passed": sum(1 for item in checks if item["status"] == "pass"),
                "warned": len(warned),
                "failed": len(failed),
                "failed_checks": failed,
                "warned_checks": warned,
            },
            "checks": checks,
            "next_actions": self._next_actions(checks),
        }
        write_json(self.output_root / "mock_runtime" / "current.json", [payload])
        write_json(self.output_root / "mock_runtime" / f"{run_date}.json", [payload])
        return payload

    def _paper_data(self, data_source: dict) -> dict:
        if data_source.get("ready_for_paper"):
            return self._check("paper_data", "pass", "Paper market data is ready for GOLD 5m mock trading.", data_source)
        return self._check("paper_data", "fail", "Paper market data is not ready.", data_source)

    def _strategy_artifacts(self, run_date: str) -> dict:
        paths = {
            "signals": self.output_root / "signals" / f"{run_date}.json",
            "backtests": self.output_root / "backtests" / f"{run_date}.json",
            "tickets": self.output_root / "trade_tickets" / f"{run_date}.json",
            "risk_blocks": self.output_root / "risk_blocks" / f"{run_date}.json",
        }
        missing = [name for name, path in paths.items() if not path.exists()]
        signals = load_json(paths["signals"])
        backtests = load_json(paths["backtests"])
        if missing:
            return self._check("strategy_artifacts", "fail", "Signal/backtest/risk artifacts are missing.", {"missing": missing, "paths": {k: str(v) for k, v in paths.items()}})
        if not signals or not backtests:
            return self._check("strategy_artifacts", "fail", "Signal or backtest artifacts are empty.", {"signals": len(signals), "backtests": len(backtests)})
        return self._check("strategy_artifacts", "pass", "Signal, backtest, ticket, and risk artifacts are available.", {"signals": len(signals), "backtests": len(backtests), "tickets": len(load_json(paths["tickets"])), "risk_blocks": len(load_json(paths["risk_blocks"]))})

    def _paper_account(self, run_date: str) -> dict:
        orders = load_json(self.output_root / "paper_orders" / f"{run_date}.json")
        positions = self._load_mapping(self.output_root / "paper_positions" / "current.json")
        decisions = load_json(self.output_root / "journal_decisions" / f"{run_date}.json")
        executed = [item for item in decisions if item.get("decision_status") == "executed_paper"]
        if orders or executed or positions:
            return self._check("paper_account", "pass", "Paper account artifacts are readable and have activity.", {"orders": len(orders), "positions": len(positions), "executed_paper": len(executed)})
        return self._check("paper_account", "warn", "Paper account is readable but has no orders or positions for this run date.", {"orders": 0, "positions": 0, "executed_paper": 0})

    def _trade_lifecycle(self, run_date: str) -> dict:
        open_trades = load_json(self.output_root / "paper_trades" / "current.json")
        closed_trades = load_json(self.output_root / "paper_trades" / "closed" / f"{run_date}.json")
        blocks = load_json(self.output_root / "paper_execution_blocks" / f"{run_date}.json")
        preflight = self._latest_preflight(run_date)
        if blocks:
            if preflight.get("ready_for_paper"):
                return self._check(
                    "trade_lifecycle",
                    "pass",
                    "Paper trade lifecycle is currently unblocked; previous intraday blocks are retained as history.",
                    {"open_trades": len(open_trades), "closed_trades": len(closed_trades), "historical_blocks": blocks[-3:], "current_preflight": preflight},
                )
            return self._check("trade_lifecycle", "warn", "Paper trade lifecycle is blocked by a data or execution gate.", {"open_trades": len(open_trades), "closed_trades": len(closed_trades), "blocks": blocks[-3:]})
        return self._check("trade_lifecycle", "pass", "Paper trade lifecycle is readable and mark-to-market capable.", {"open_trades": len(open_trades), "closed_trades": len(closed_trades)})

    def _journal_review(self, run_date: str) -> dict:
        required = {
            "journal": self.output_root / "journals" / f"{run_date}.md",
            "review_notes": self.output_root / "review_notes" / f"{run_date}.md",
            "report": self.output_root / "reports" / f"{run_date}.md",
            "strategy_review": self.output_root / "strategy_reviews" / f"{run_date}.json",
            "learning_ledger": self.output_root / "learning_ledger" / f"{run_date}.json",
            "strategy_change_proposal": self.output_root / "strategy_change_proposals" / f"{run_date}.json",
        }
        missing = [name for name, path in required.items() if not path.exists()]
        if missing:
            return self._check("journal_review", "fail", "Daily journal/review/learning artifacts are missing.", {"missing": missing})
        return self._check("journal_review", "pass", "Daily journal, review notes, report, and learning artifacts exist.", {"artifacts": {name: str(path) for name, path in required.items()}})

    def _runner_freshness(self, run_date: str) -> dict:
        runner = self._load_mapping(self.output_root / "runner_status" / "current.json")
        if not runner:
            return self._check("runner_freshness", "warn", "Runner status is missing; mock runtime is ready but not proven active.", {})
        if runner.get("run_date") != run_date:
            return self._check("runner_freshness", "warn", "Runner status is for a different run date.", runner)
        interval = int(runner.get("interval_seconds", 300) or 300)
        if runner.get("state") == "running":
            started_at = self._parse_time(str(runner.get("started_at", "")))
            if not started_at:
                return self._check("runner_freshness", "warn", "Runner is running but has no started_at timestamp.", runner)
            age_seconds = (datetime.now(timezone.utc) - started_at).total_seconds()
            evidence = {**runner, "age_seconds": round(age_seconds, 2), "freshness_budget_seconds": interval * 2}
            if age_seconds <= interval * 2:
                return self._check("runner_freshness", "pass", "Runner is actively processing a fresh 5m mock trading cycle.", evidence)
            return self._check("runner_freshness", "warn", "Runner is marked running but appears stale; inspect runner_status/current.json.", evidence)
        if runner.get("state") != "ok":
            return self._check("runner_freshness", "fail", "Latest runner cycle is not ok.", runner)
        finished_at = self._parse_time(str(runner.get("finished_at", "")))
        if not finished_at:
            return self._check("runner_freshness", "warn", "Runner has no finished_at timestamp.", runner)
        age_seconds = (datetime.now(timezone.utc) - finished_at).total_seconds()
        evidence = {**runner, "age_seconds": round(age_seconds, 2), "freshness_budget_seconds": interval * 2}
        if age_seconds <= interval * 2:
            return self._check("runner_freshness", "pass", "Runner has completed a fresh 5m mock trading cycle.", evidence)
        return self._check("runner_freshness", "warn", "Runner completed successfully but is stale; start the continuous 5m runner for active mock trading.", evidence)

    def _dashboard_artifacts(self) -> dict:
        dashboard = ROOT / "dashboard-v4.html"
        server = ROOT / "pipelines" / "dashboard_server.py"
        if dashboard.exists() and server.exists():
            return self._check("dashboard_artifacts", "pass", "Dashboard page and local API server entrypoint exist.", {"dashboard": str(dashboard), "server": str(server), "url": "http://127.0.0.1:8765/dashboard-v4.html"})
        return self._check("dashboard_artifacts", "fail", "Dashboard file or server entrypoint is missing.", {"dashboard": str(dashboard), "server": str(server)})

    def _next_actions(self, checks: list[dict]) -> list[str]:
        by_name = {item["name"]: item for item in checks}
        actions: list[str] = []
        if by_name.get("paper_data", {}).get("status") == "fail":
            actions.append("Run python3 -m pipelines.bot --date <date> to refresh GOLD 5m data and paper preflight.")
        if by_name.get("strategy_artifacts", {}).get("status") == "fail":
            actions.append("Run python3 -m pipelines.daily --date <date> to regenerate signals, backtests, tickets, and risk blocks.")
        if by_name.get("journal_review", {}).get("status") == "fail":
            actions.append("Run python3 -m pipelines.report --date <date> to regenerate the daily Trading Journal and review artifacts.")
        if by_name.get("runner_freshness", {}).get("status") != "pass":
            actions.append("Start or refresh mock trading: python3 -m pipelines.runner --date <date> --paper-auto-approve --iterations 0 --interval-seconds 300.")
        if not actions:
            actions.append("Mock Trading runtime is ready and fresh.")
        return actions

    def _rollup(self, checks: list[dict]) -> str:
        states = {item["status"] for item in checks}
        if "fail" in states:
            return "fail"
        if "warn" in states:
            return "warn"
        return "pass"

    def _check(self, name: str, status: str, summary: str, evidence: dict) -> dict:
        return {"name": name, "status": status, "summary": summary, "evidence": evidence}

    def _parse_time(self, value: str) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _load_mapping(self, path: Path) -> dict:
        if not path.exists():
            return {}
        import json

        return json.loads(path.read_text(encoding="utf-8"))

    def _latest_preflight(self, run_date: str) -> dict:
        dated = load_json(self.output_root / "data_source_preflight" / f"{run_date}.json")
        if dated:
            return dated[-1]
        current = load_json(self.output_root / "data_source_preflight" / "current.json")
        return current[-1] if current else {}
