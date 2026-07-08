from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import load_json, write_json


class MockTradingUAT:
    def __init__(self, output_root: Path | None = None) -> None:
        config = load_pipeline_config()
        self.output_root = output_root or ROOT / config.get("output_root", "outputs")

    def run(self, run_date: str) -> dict:
        checks = [
            self._market_data(run_date),
            self._strategy_evidence(run_date),
            self._paper_execution(run_date),
            self._trade_attribution(run_date),
            self._risk_monitor(run_date),
            self._journal_review(run_date),
            self._health_archive(run_date),
            self._runtime(run_date),
            self._dashboard(),
        ]
        status = self._rollup(checks)
        payload = {
            "run_date": run_date,
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "status": status,
            "summary": {
                "passed": sum(1 for item in checks if item["status"] == "pass"),
                "warned": sum(1 for item in checks if item["status"] == "warn"),
                "failed": sum(1 for item in checks if item["status"] == "fail"),
                "failed_checks": [item["name"] for item in checks if item["status"] == "fail"],
                "warned_checks": [item["name"] for item in checks if item["status"] == "warn"],
            },
            "checks": checks,
            "evidence_paths": self._evidence_paths(run_date),
            "next_actions": self._next_actions(checks, run_date),
        }
        write_json(self.output_root / "mock_uat" / "current.json", [payload])
        write_json(self.output_root / "mock_uat" / f"{run_date}.json", [payload])
        self._write_markdown(run_date, payload)
        return payload

    def _market_data(self, run_date: str) -> dict:
        preflight = self._latest(self.output_root / "data_source_preflight" / f"{run_date}.json") or self._latest(self.output_root / "data_source_preflight" / "current.json")
        clean = load_json(self.output_root / "clean_bars" / run_date / "GOLD_5m.json")
        manifest = load_json(self.output_root / "clean_bars" / run_date / "manifest.json")
        if not clean or not manifest:
            return self._check("market_data", "fail", "GOLD 5m clean bars or manifest are missing.", {"clean_rows": len(clean), "manifest_rows": len(manifest)})
        if not preflight:
            return self._check("market_data", "fail", "Data source preflight is missing.", {"clean_rows": len(clean)})
        fresh = preflight.get("latest_record_is_fresh")
        if preflight.get("ready_for_paper") and fresh is not False:
            return self._check("market_data", "pass", "Paper market data is available for GOLD 5m.", {"clean_rows": len(clean), "provider": preflight.get("latest_provider"), "fresh": fresh, "mode": "paper"})
        return self._check("market_data", "fail", "Data source preflight blocks paper trading.", {"preflight": preflight, "clean_rows": len(clean)})

    def _strategy_evidence(self, run_date: str) -> dict:
        signals = load_json(self.output_root / "signals" / f"{run_date}.json")
        backtests = load_json(self.output_root / "backtests" / f"{run_date}.json")
        tickets = load_json(self.output_root / "trade_tickets" / f"{run_date}.json")
        risk_blocks = load_json(self.output_root / "risk_blocks" / f"{run_date}.json")
        gold_signal = next((item for item in signals if item.get("asset") == "GOLD"), {})
        gold_backtest = next((item for item in backtests if item.get("asset") == "GOLD"), {})
        if not gold_signal or not gold_backtest:
            return self._check("strategy_evidence", "fail", "GOLD signal or backtest evidence is missing.", {"signals": len(signals), "backtests": len(backtests)})
        if self._is_no_trade_decision(gold_signal, gold_backtest):
            return self._check(
                "strategy_evidence",
                "pass",
                "GOLD signal and backtest explicitly route today to no-trade/watch.",
                {
                    "direction": gold_signal.get("direction"),
                    "status": gold_signal.get("status"),
                    "regime": gold_signal.get("regime"),
                    "methods": gold_signal.get("methods", []),
                    "backtest_verdict": gold_backtest.get("verdict"),
                    "tickets": len(tickets),
                    "risk_blocks": len(risk_blocks),
                },
            )
        if not tickets and not risk_blocks:
            return self._check("strategy_evidence", "warn", "Signal and backtest exist, but no ticket or risk block explains the decision path.", {"signal": gold_signal, "backtest": gold_backtest})
        return self._check(
            "strategy_evidence",
            "pass",
            "GOLD signal, backtest, and strategy routing evidence are present.",
            {"direction": gold_signal.get("direction"), "strength": gold_signal.get("strength"), "backtest_verdict": gold_backtest.get("verdict"), "tickets": len(tickets), "risk_blocks": len(risk_blocks)},
        )

    def _is_no_trade_decision(self, signal: dict, backtest: dict) -> bool:
        methods = set(signal.get("methods", []) or [])
        values = {
            str(signal.get("direction", "")),
            str(signal.get("status", "")),
            str(signal.get("regime", "")),
            str(signal.get("backtest_verdict", "")),
            str(backtest.get("verdict", "")),
        }
        no_trade_values = {"watch", "no_trade", "no_signal"}
        return bool(values & no_trade_values) or "no-trade" in methods

    def _paper_execution(self, run_date: str) -> dict:
        orders = load_json(self.output_root / "paper_orders" / f"{run_date}.json")
        positions = self._load_mapping(self.output_root / "paper_positions" / "current.json")
        open_trades = load_json(self.output_root / "paper_trades" / "current.json")
        closed_trades = load_json(self.output_root / "paper_trades" / "closed" / f"{run_date}.json")
        decisions = load_json(self.output_root / "journal_decisions" / f"{run_date}.json")
        executed = [item for item in decisions if item.get("decision_status") == "executed_paper"]
        if orders or executed or positions or open_trades or closed_trades:
            return self._check(
                "paper_execution",
                "pass",
                "Paper order/trade/position artifacts prove the mock execution path.",
                {"orders": len(orders), "executed_paper_decisions": len(executed), "positions": len(positions), "open_trades": len(open_trades), "closed_trades": len(closed_trades)},
            )
        return self._check("paper_execution", "warn", "Paper executor is wired, but no paper activity exists for this run date.", {"orders": 0, "executed_paper_decisions": 0, "positions": 0, "open_trades": 0})

    def _trade_attribution(self, run_date: str) -> dict:
        rows = load_json(self.output_root / "paper_trade_attribution" / f"{run_date}.json")
        attribution = rows[-1] if rows else {}
        if not attribution:
            return self._check("trade_attribution", "fail", "Paper trade attribution artifact is missing.", {"path": str(self.output_root / "paper_trade_attribution" / f"{run_date}.json")})
        summary = attribution.get("summary", {})
        total = int(summary.get("open_trades", 0) or 0) + int(summary.get("closed_trades", 0) or 0)
        attributed = int(summary.get("attributed_open_trades", 0) or 0) + int(summary.get("attributed_closed_trades", 0) or 0)
        if attribution.get("status") != "pass":
            return self._check("trade_attribution", "fail", "Paper trade attribution did not pass.", attribution)
        if total and attributed < total:
            return self._check("trade_attribution", "warn", f"Paper trade attribution is incomplete: {attributed}/{total}.", attribution)
        return self._check("trade_attribution", "pass", f"Paper trade attribution coverage is {attributed}/{total}.", attribution)

    def _risk_monitor(self, run_date: str) -> dict:
        monitor = self._latest(self.output_root / "risk_monitor" / f"{run_date}.json") or self._latest(self.output_root / "risk_monitor" / "current.json")
        if not monitor:
            return self._check("risk_monitor", "fail", "Risk monitor artifact is missing.", {"path": str(self.output_root / "risk_monitor" / f"{run_date}.json")})
        if monitor.get("status") not in {"pass", "warn", "block"}:
            return self._check("risk_monitor", "fail", "Risk monitor status is invalid.", monitor)
        if monitor.get("kill_switch_active"):
            return self._check("risk_monitor", "warn", "Risk monitor kill-switch is active; mock trading should not auto-approve new paper orders.", monitor)
        return self._check("risk_monitor", "pass", "Risk monitor is available and kill-switch is inactive.", monitor)

    def _journal_review(self, run_date: str) -> dict:
        required_files = {
            "journal": self.output_root / "journals" / f"{run_date}.md",
            "review_notes": self.output_root / "review_notes" / f"{run_date}.md",
            "report": self.output_root / "reports" / f"{run_date}.md",
        }
        required_json = {
            "performance": self.output_root / "performance" / f"{run_date}.json",
            "paper_trade_attribution": self.output_root / "paper_trade_attribution" / f"{run_date}.json",
            "strategy_review": self.output_root / "strategy_reviews" / f"{run_date}.json",
            "strategy_snapshot": self.output_root / "strategy_snapshots" / f"{run_date}.json",
            "learning_ledger": self.output_root / "learning_ledger" / f"{run_date}.json",
        }
        missing_files = [name for name, path in required_files.items() if not path.exists()]
        empty_json = [name for name, path in required_json.items() if not load_json(path)]
        if missing_files or empty_json:
            return self._check("journal_review", "fail", "Journal, review, performance, or learning artifacts are missing.", {"missing_files": missing_files, "empty_json": empty_json})
        return self._check("journal_review", "pass", "Journal, daily review, performance, and learning artifacts are complete.", {"files": {name: str(path) for name, path in required_files.items()}, "json": {name: str(path) for name, path in required_json.items()}})

    def _health_archive(self, run_date: str) -> dict:
        health = self._latest(self.output_root / "health" / f"{run_date}.json") or self._latest(self.output_root / "health" / "current.json")
        archive = self._latest(self.output_root / "data_archive" / f"{run_date}.json") or self._latest(self.output_root / "data_archive" / "current.json")
        if not health or not archive:
            return self._check(
                "health_archive",
                "fail",
                "Health or data archive artifact is missing.",
                {"health": bool(health), "archive": bool(archive)},
            )
        health_status = str(health.get("status", ""))
        archive_status = str(archive.get("status", ""))
        if health_status == "error" or archive_status not in {"pass", "warn"}:
            return self._check("health_archive", "fail", "Health or archive status blocks Mock UAT.", {"health": health, "archive": archive})
        if archive_status == "pass":
            return self._check(
                "health_archive",
                "pass",
                "Health and local archive artifacts are complete for the mock trading run.",
                {"health_status": health_status, "archive_status": archive_status, "archive_files": f"{archive.get('present_file_count', 0)}/{archive.get('file_count', 0)}"},
            )
        return self._check(
            "health_archive",
            "warn",
            "Health is readable but the local archive still has missing optional evidence.",
            {"health_status": health_status, "archive_status": archive_status, "missing_files": archive.get("missing_files", [])},
        )

    def _runtime(self, run_date: str) -> dict:
        runtime = self._latest(self.output_root / "mock_runtime" / f"{run_date}.json") or self._latest(self.output_root / "mock_runtime" / "current.json")
        if not runtime:
            return self._check("runtime", "fail", "Mock runtime receipt is missing.", {})
        if runtime.get("mock_ready") and runtime.get("status") in {"pass", "warn"}:
            status = "pass" if runtime.get("mock_running") else "warn"
            return self._check(status=status, name="runtime", summary="Mock runtime is ready; runner freshness is tracked separately.", evidence={"status": runtime.get("status"), "mock_ready": runtime.get("mock_ready"), "mock_running": runtime.get("mock_running"), "summary": runtime.get("summary", {})})
        return self._check("runtime", "fail", "Mock runtime is not ready.", runtime)

    def _dashboard(self) -> dict:
        dashboard = ROOT / "dashboard-v4.html"
        server = ROOT / "pipelines" / "dashboard_server.py"
        if dashboard.exists() and server.exists():
            return self._check("dashboard", "pass", "Dashboard and API server entrypoint are present.", {"dashboard": str(dashboard), "server": str(server), "url": "http://127.0.0.1:8765/dashboard-v4.html"})
        return self._check("dashboard", "fail", "Dashboard or API server entrypoint is missing.", {"dashboard": str(dashboard), "server": str(server)})

    def _evidence_paths(self, run_date: str) -> dict:
        paths = {
            "data_source_preflight": self.output_root / "data_source_preflight" / f"{run_date}.json",
            "clean_bars": self.output_root / "clean_bars" / run_date / "GOLD_5m.json",
            "signals": self.output_root / "signals" / f"{run_date}.json",
            "backtests": self.output_root / "backtests" / f"{run_date}.json",
            "trade_tickets": self.output_root / "trade_tickets" / f"{run_date}.json",
            "risk_blocks": self.output_root / "risk_blocks" / f"{run_date}.json",
            "paper_orders": self.output_root / "paper_orders" / f"{run_date}.json",
            "paper_positions": self.output_root / "paper_positions" / "current.json",
            "paper_trades": self.output_root / "paper_trades" / "current.json",
            "journal_decisions": self.output_root / "journal_decisions" / f"{run_date}.json",
            "performance": self.output_root / "performance" / f"{run_date}.json",
            "paper_trade_attribution": self.output_root / "paper_trade_attribution" / f"{run_date}.json",
            "risk_monitor": self.output_root / "risk_monitor" / f"{run_date}.json",
            "health": self.output_root / "health" / f"{run_date}.json",
            "data_archive": self.output_root / "data_archive" / f"{run_date}.json",
            "mock_runtime": self.output_root / "mock_runtime" / f"{run_date}.json",
            "dashboard": ROOT / "dashboard-v4.html",
        }
        return {name: str(path) for name, path in paths.items()}

    def _next_actions(self, checks: list[dict], run_date: str) -> list[str]:
        actions: list[str] = []
        by_name = {item["name"]: item for item in checks}
        if by_name.get("market_data", {}).get("status") == "fail":
            actions.append(f"Run python3 -m pipelines.runner --date {run_date} --paper-auto-approve --iterations 1 to refresh GOLD 5m data.")
        if by_name.get("strategy_evidence", {}).get("status") in {"fail", "warn"}:
            actions.append(f"Run python3 -m pipelines.daily --date {run_date} to regenerate signal/backtest/risk evidence.")
        if by_name.get("journal_review", {}).get("status") == "fail":
            actions.append(f"Run python3 -m pipelines.daily_review --date {run_date} to regenerate journal/review artifacts.")
        if by_name.get("paper_execution", {}).get("status") == "warn":
            actions.append("Approve a valid pending ticket with executed_paper when the risk gate allows it.")
        if not actions:
            actions.append("Mock Trading UAT evidence is complete for local paper trading.")
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

    def _latest(self, path: Path) -> dict:
        rows = load_json(path)
        return rows[-1] if rows else {}

    def _load_mapping(self, path: Path) -> dict:
        if not path.exists():
            return {}
        import json

        return json.loads(path.read_text(encoding="utf-8"))

    def _write_markdown(self, run_date: str, payload: dict) -> None:
        lines = [
            f"# Mock Trading UAT - {run_date}",
            "",
            f"- Status: {payload['status']}",
            f"- Passed: {payload['summary']['passed']}",
            f"- Warned: {payload['summary']['warned']}",
            f"- Failed: {payload['summary']['failed']}",
            "",
            "## Checks",
        ]
        for check in payload["checks"]:
            lines.append(f"- {check['status']}: {check['name']} - {check['summary']}")
        lines.extend(["", "## Evidence Paths"])
        for name, path in payload["evidence_paths"].items():
            lines.append(f"- {name}: `{path}`")
        path = self.output_root / "mock_uat" / f"{run_date}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_mock_trading_uat(run_date: str, output_root: Path | None = None) -> dict:
    return MockTradingUAT(output_root).run(run_date)
