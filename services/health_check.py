from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from services.config_loader import ROOT, load_pipeline_config
from services.alert_notifier import resolve_alert_sender
from services.data_gap_doctor import DataGapDoctor
from services.data_source_preflight import DataSourcePreflight
from services.journal_store import load_json, write_json
from services.market_store import MarketStore
from services.secrets_audit import SecretsAudit


class HealthCheck:
    def __init__(self, output_root: Path | None = None, market_db: Path | None = None) -> None:
        config = load_pipeline_config()
        self.config = config
        self.output_root = output_root or Path(os.getenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(ROOT / config.get("output_root", "outputs"))))
        self.market_db = market_db or Path(os.getenv("TRADING_ORCHESTRATOR_MARKET_DB", str(ROOT / config.get("local_market_db", "data/market_data.db"))))

    def run(self, run_date: str) -> dict:
        checks = [
            self._market_db_check(),
            self._data_source_check(run_date),
            self._data_quality_check(run_date),
            self._data_gap_check(run_date),
            self._pipeline_artifacts_check(run_date),
            self._paper_account_check(run_date),
            self._paper_performance_check(run_date),
            self._paper_reconciliation_check(run_date),
            self._paper_trade_attribution_check(run_date),
            self._journal_review_check(run_date),
            self._strategy_guardrails_check(run_date),
            self._daily_review_check(run_date),
            self._broker_check(),
            self._oanda_feed_check(),
            self._broker_receipts_check(),
            self._active_demo_reconciliation_check(),
            self._alert_delivery_check(),
            self._runner_check(),
            self._secrets_check(run_date),
            *self._system_vitals_checks(run_date),
        ]
        status = self._rollup(checks)
        payload = {
            "run_date": run_date,
            "checked_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "status": status,
            "checks": checks,
        }
        write_json(self.output_root / "health" / "current.json", [payload])
        write_json(self.output_root / "health" / f"{run_date}.json", [payload])
        return payload

    def _market_db_check(self) -> dict:
        if not self.market_db.exists():
            return self._check("market_db", "error", f"market db missing: {self.market_db}", {})
        coverage = MarketStore(self.market_db).coverage()
        gold_rows = sum(item["rows"] for item in coverage if item["symbol"] == "GOLD" and item["timeframe"] == "5m")
        non_seed_rows = sum(
            item["rows"]
            for item in coverage
            if item["symbol"] == "GOLD" and item["timeframe"] == "5m" and item["provider"] != "local_synthetic_seed"
        )
        if non_seed_rows < 200:
            return self._check("market_db", "warn", f"GOLD 5m non-seed rows low: {non_seed_rows}", {"gold_5m_rows": gold_rows, "gold_5m_non_seed_rows": non_seed_rows})
        return self._check("market_db", "ok", "local market database has GOLD 5m coverage", {"gold_5m_rows": gold_rows, "gold_5m_non_seed_rows": non_seed_rows})

    def _data_quality_check(self, run_date: str) -> dict:
        data_quality = self._load_mapping(self.output_root / "data_quality" / f"{run_date}.json")
        gold = data_quality.get("GOLD", {})
        if not gold:
            return self._check("data_quality", "warn", "no GOLD data quality output", {})
        if not gold.get("allows_trading", False):
            return self._check("data_quality", "error", "; ".join(gold.get("reasons", [])) or "data quality gate blocked trading", gold)
        return self._check("data_quality", "ok", "data quality gate allows trading", gold)

    def _data_source_check(self, run_date: str) -> dict:
        source = DataSourcePreflight(self.output_root, self.market_db).run(run_date)
        status = "ok" if source["status"] == "pass" else ("error" if source["status"] == "fail" else "warn")
        return self._check("data_source", status, source["message"], source)

    def _data_gap_check(self, run_date: str) -> dict:
        gaps = DataGapDoctor(self.output_root).run(run_date)
        status = "ok" if gaps["status"] == "pass" else ("error" if gaps["status"] == "fail" else "warn")
        latest = gaps.get("latest_gap", {})
        if status == "ok":
            message = "GOLD 5m data is continuous"
        elif gaps.get("gap_count", 0) == 0 and gaps.get("paper_snapshot_gap_count", 0):
            snapshot = gaps.get("latest_paper_snapshot_gap", {})
            message = f"paper mode uses quote-derived latest snapshot after {snapshot.get('gap_minutes', 'n/a')}m non-broker gap"
        else:
            message = f"{gaps['gap_count']} GOLD 5m gap(s), latest {latest.get('from_timestamp', 'n/a')} -> {latest.get('to_timestamp', 'n/a')}"
        return self._check("data_gaps", status, message, gaps)

    def _pipeline_artifacts_check(self, run_date: str) -> dict:
        # Per-cycle artifacts: every item below is produced by the per-5min
        # bot/runner cycle (pipelines.bot -> run_daily_pipeline -> build_daily_report).
        # Evening-only artifacts (e.g. paper_trade_attribution, produced solely by
        # the daily review runner) are intentionally NOT listed here so a mid-day
        # cycle does not roll up to "error" while the evening review is still
        # pending. They are asserted by their own checks (see
        # _paper_trade_attribution_check), which only escalate after the review runs.
        required = [
            self.output_root / "signals" / f"{run_date}.json",
            self.output_root / "backtests" / f"{run_date}.json",
            self.output_root / "clean_bars" / run_date / "GOLD_5m.json",
            self.output_root / "clean_bars" / run_date / "manifest.json",
            self.output_root / "reports" / f"{run_date}.md",
            self.output_root / "journals" / f"{run_date}.md",
            self.output_root / "review_notes" / f"{run_date}.md",
            self.output_root / "strategy_reviews" / f"{run_date}.json",
            self.output_root / "strategy_snapshots" / f"{run_date}.json",
            self.output_root / "learning_ledger" / f"{run_date}.json",
            self.output_root / "strategy_change_proposals" / f"{run_date}.json",
            self.output_root / "performance" / f"{run_date}.json",
        ]
        missing = [str(path) for path in required if not path.exists()]
        if missing:
            return self._check("pipeline_artifacts", "error", "missing required artifacts", {"missing": missing})
        return self._check("pipeline_artifacts", "ok", "required pipeline artifacts exist", {"count": len(required)})

    def _paper_account_check(self, run_date: str) -> dict:
        orders = load_json(self.output_root / "paper_orders" / f"{run_date}.json")
        positions = self._load_mapping(self.output_root / "paper_positions" / "current.json")
        open_trades = load_json(self.output_root / "paper_trades" / "current.json")
        return self._check(
            "paper_account",
            "ok",
            "paper account artifacts readable",
            {"orders": len(orders), "positions": len(positions), "open_trades": len(open_trades)},
        )

    def _paper_performance_check(self, run_date: str) -> dict:
        rows = load_json(self.output_root / "performance" / f"{run_date}.json")
        latest = rows[-1] if rows else {}
        if not latest:
            return self._check("paper_performance", "warn", "paper performance artifact missing", {"path": str(self.output_root / "performance" / f"{run_date}.json")})
        summary = latest.get("summary", {})
        required = ["net_pnl_marked", "open_unrealized_r", "expectancy_r", "profit_factor"]
        missing = [key for key in required if key not in summary]
        if missing:
            return self._check("paper_performance", "error", "paper performance summary missing required metrics", {"missing": missing, "summary": summary})
        return self._check(
            "paper_performance",
            "ok",
            "paper performance metrics readable",
            {
                "net_pnl_marked": summary.get("net_pnl_marked"),
                "open_unrealized_r": summary.get("open_unrealized_r"),
                "expectancy_r": summary.get("expectancy_r"),
                "profit_factor": summary.get("profit_factor"),
            },
        )

    def _paper_reconciliation_check(self, run_date: str) -> dict:
        rows = load_json(self.output_root / "paper_reconciliation" / f"{run_date}.json")
        reconciliation = rows[-1] if rows else {}
        if not reconciliation:
            return self._check("paper_reconciliation", "warn", "paper reconciliation has not run", {"command": f"python3 -m pipelines.paper_reconciliation --date {run_date}"})
        if reconciliation.get("status") == "pass":
            return self._check("paper_reconciliation", "ok", "paper account reconciles", reconciliation)
        return self._check("paper_reconciliation", "error", "paper account reconciliation failed", reconciliation)

    def _paper_trade_attribution_check(self, run_date: str) -> dict:
        rows = load_json(self.output_root / "paper_trade_attribution" / f"{run_date}.json")
        attribution = rows[-1] if rows else {}
        if not attribution:
            # Attribution is produced only by the evening daily review, never by
            # the per-cycle pipeline. Before the review runs the artifact is
            # legitimately absent, so report "ok" (not yet due) rather than firing
            # a false alert all day. Once the review has completed for the date the
            # artifact must exist, so a missing file is then a genuine error.
            command = {"command": f"python3 -m pipelines.paper_trade_attribution --date {run_date}"}
            if self._daily_review_completed(run_date):
                return self._check("paper_trade_attribution", "error", "paper trade attribution missing after evening review", command)
            return self._check("paper_trade_attribution", "ok", "paper trade attribution pending evening review", command)
        summary = attribution.get("summary", {})
        total = int(summary.get("open_trades", 0) or 0) + int(summary.get("closed_trades", 0) or 0)
        attributed = int(summary.get("attributed_open_trades", 0) or 0) + int(summary.get("attributed_closed_trades", 0) or 0)
        if attribution.get("status") != "pass":
            return self._check("paper_trade_attribution", "error", "paper trade attribution failed", attribution)
        if total and attributed < total:
            return self._check("paper_trade_attribution", "warn", f"paper trade attribution incomplete: {attributed}/{total}", attribution)
        return self._check("paper_trade_attribution", "ok", f"paper trade attribution coverage {attributed}/{total}", attribution)

    def _journal_review_check(self, run_date: str) -> dict:
        decisions = load_json(self.output_root / "journal_decisions" / f"{run_date}.json")
        pending = load_json(self.output_root / "journal_pending" / f"{run_date}.json")
        review = self.output_root / "review_notes" / f"{run_date}.md"
        journal = self.output_root / "journals" / f"{run_date}.md"
        strategy_review = self.output_root / "strategy_reviews" / f"{run_date}.json"
        strategy_snapshot = self.output_root / "strategy_snapshots" / f"{run_date}.json"
        learning_ledger = self.output_root / "learning_ledger" / f"{run_date}.json"
        strategy_proposal = self.output_root / "strategy_change_proposals" / f"{run_date}.json"
        missing = [str(path) for path in [review, journal, strategy_review, strategy_snapshot, learning_ledger, strategy_proposal] if not path.exists()]
        if missing:
            return self._check("journal_review", "error", "journal/review learning artifacts missing", {"decisions": len(decisions), "pending": len(pending), "missing": missing})
        return self._check(
            "journal_review",
            "ok",
            "journal, review, learning ledger, and strategy proposal artifacts readable",
            {
                "decisions": len(decisions),
                "pending": len(pending),
                "journal": str(journal),
                "review": str(review),
                "strategy_review": str(strategy_review),
                "strategy_snapshot": str(strategy_snapshot),
                "learning_ledger": str(learning_ledger),
                "strategy_proposal": str(strategy_proposal),
            },
        )

    def _strategy_guardrails_check(self, run_date: str) -> dict:
        rows = load_json(self.output_root / "strategy_guardrails" / f"{run_date}.json")
        guardrails = rows[-1] if rows else {}
        if not guardrails:
            return self._check("strategy_guardrails", "warn", "strategy guardrails have not been generated", {"command": f"python3 -m pipelines.strategy_guardrails --date {run_date}"})
        status = str(guardrails.get("status", ""))
        if status == "block":
            return self._check("strategy_guardrails", "warn", "strategy guardrails block new paper exposure", guardrails)
        if status in {"pass", "warn"}:
            return self._check("strategy_guardrails", "ok", "strategy guardrails are readable", guardrails)
        return self._check("strategy_guardrails", "error", "strategy guardrails status is invalid", guardrails)

    def _daily_review_check(self, run_date: str) -> dict:
        rows = load_json(self.output_root / "daily_review_runs" / f"{run_date}.json")
        receipt = rows[-1] if rows else {}
        if not receipt:
            return self._check("daily_review", "warn", "daily review runner has not recorded a receipt", {"command": f"python3 -m pipelines.daily_review --date {run_date}"})
        if receipt.get("status") == "fail":
            return self._check("daily_review", "error", "daily review runner failed", receipt)
        if receipt.get("status") == "running":
            return self._check("daily_review", "warn", "daily review runner is still running or did not finish", receipt)
        return self._check("daily_review", "ok", "daily review runner receipt readable", receipt)

    def _daily_review_completed(self, run_date: str) -> bool:
        """True once the evening daily review has finished for the date.

        The runner writes a "running" receipt at start and overwrites it with a
        terminal status (pass/warn/fail) when done, after producing the
        evening-only artifacts. A terminal receipt therefore means those
        artifacts should already exist.
        """
        rows = load_json(self.output_root / "daily_review_runs" / f"{run_date}.json")
        receipt = rows[-1] if rows else {}
        return bool(receipt) and receipt.get("status") != "running"

    def _broker_check(self) -> dict:
        rows = load_json(self.output_root / "broker_preflight" / "current.json")
        latest = rows[-1] if rows else {}
        if not latest:
            return self._check("broker", "warn", "broker preflight missing", {})
        status = "ok" if latest.get("ready") else "error"
        return self._check("broker", status, latest.get("block_reason") or "broker preflight ready", latest)

    def _oanda_feed_check(self) -> dict:
        active_broker = str((self.config.get("broker") or {}).get("provider", ""))
        rows = load_json(self.output_root / "oanda_feed" / "current.json")
        latest = rows[-1] if rows else {}
        if active_broker != "oanda_rest":
            details = latest or {}
            details = {**details, "active_broker": active_broker}
            return self._check("oanda_feed", "ok", f"OANDA feed inactive; current broker is {active_broker or 'unknown'}", details)
        if not latest:
            return self._check("oanda_feed", "warn", "OANDA feed importer has not run", {})
        if latest.get("status") == "pass":
            return self._check("oanda_feed", "ok", f"imported {latest.get('imported_rows', 0)} OANDA GOLD 5m bars", latest)
        if latest.get("status") == "skipped":
            missing = ", ".join(latest.get("missing_env", [])) or "credentials"
            return self._check("oanda_feed", "warn", f"OANDA feed skipped; missing {missing}", latest)
        if latest.get("status") == "warn":
            return self._check("oanda_feed", "warn", latest.get("message", "OANDA feed warning"), latest)
        return self._check("oanda_feed", "error", latest.get("message", "OANDA feed failed"), latest)

    def _broker_receipts_check(self) -> dict:
        summary_rows = load_json(self.output_root / "broker_receipts" / "summary_current.json")
        summary = summary_rows[-1] if summary_rows else {}
        smoke_rows = load_json(self.output_root / "mt5_bridge_smoke" / "current.json")
        smoke = smoke_rows[-1] if smoke_rows else {}
        if not summary and not smoke:
            return self._check("broker_receipts", "warn", "broker receipt importer has not run", {})
        if summary.get("errors"):
            return self._check("broker_receipts", "error", "broker receipt import errors present", summary)
        if smoke and smoke.get("status") != "pass":
            return self._check("broker_receipts", "error", "MT5 bridge smoke failed", smoke)
        return self._check(
            "broker_receipts",
            "ok",
            "broker receipt importer readable",
            {"receipt_summary": summary, "mt5_bridge_smoke": {"status": smoke.get("status", ""), "order_id": (smoke.get("order") or {}).get("order_id", "")}},
        )

    def _active_demo_reconciliation_check(self) -> dict:
        demo = self.config.get("demo_trading", {}) or {}
        if demo.get("enabled") is not True:
            return self._check("active_demo_reconciliation", "ok", "Binance demo trading inactive", {"enabled": False})
        strategy_id = str(demo.get("active_strategy_id", ""))
        if not strategy_id:
            return self._check("active_demo_reconciliation", "warn", "demo trading enabled but active_strategy_id is missing", demo)
        path = self.output_root / "strategies" / strategy_id / "live_reconciliation" / "current.json"
        rows = load_json(path)
        reconciliation = rows[-1] if rows else {}
        if not reconciliation:
            return self._check(
                "active_demo_reconciliation",
                "warn",
                f"active demo reconciliation missing for {strategy_id}",
                {"strategy_id": strategy_id, "artifact": str(path)},
            )
        if reconciliation.get("confirmation_status") == "cannot_confirm":
            naked = bool(reconciliation.get("suspected_naked_position"))
            prefix = "suspected naked position; " if naked else ""
            return self._check(
                "active_demo_reconciliation",
                "error",
                f"active demo reconciliation cannot confirm venue state for {strategy_id}: {prefix}{reconciliation.get('error')}",
                {"strategy_id": strategy_id, "artifact": str(path), "reconciliation": reconciliation},
            )
        if reconciliation.get("error"):
            return self._check(
                "active_demo_reconciliation",
                "error",
                f"active demo reconciliation errored for {strategy_id}: {reconciliation.get('error')}",
                {"strategy_id": strategy_id, "artifact": str(path), "reconciliation": reconciliation},
            )
        if reconciliation.get("suspected_naked_position"):
            return self._check(
                "active_demo_reconciliation",
                "error",
                f"active demo suspected naked position for {strategy_id}: {reconciliation.get('escalation_action')}",
                {"strategy_id": strategy_id, "artifact": str(path), "reconciliation": reconciliation},
            )
        if int(reconciliation.get("drift_count") or 0) > 0:
            reasons = sorted({str(item.get("reason", "reconciliation drift")) for item in reconciliation.get("drifts", [])})
            return self._check(
                "active_demo_reconciliation",
                "error",
                f"active demo reconciliation drift for {strategy_id}: {'; '.join(reasons) or 'unknown drift'}",
                {"strategy_id": strategy_id, "artifact": str(path), "reconciliation": reconciliation},
            )
        protective_block = self._active_demo_protective_block(strategy_id)
        if protective_block:
            return self._check(
                "active_demo_reconciliation",
                "error",
                f"active demo protective orders missing for {strategy_id}: {protective_block['reason']}",
                {
                    "strategy_id": strategy_id,
                    "artifact": protective_block["artifact"],
                    "request": protective_block["request"],
                    "reconciliation": reconciliation,
                },
            )
        return self._check(
            "active_demo_reconciliation",
            "ok",
            f"active demo strategy {strategy_id} reconciles with exchange",
            {"strategy_id": strategy_id, "artifact": str(path), "reconciliation": reconciliation},
        )

    def _active_demo_protective_block(self, strategy_id: str) -> dict:
        request_dir = self.output_root / "strategies" / strategy_id / "demo_order_requests"
        candidates = []
        if request_dir.exists():
            candidates = sorted(request_dir.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
        for path in candidates:
            rows = load_json(path)
            for request in reversed([item for item in rows if isinstance(item, dict)]):
                block = self._protective_block_from_request(request)
                if block:
                    return {"artifact": str(path), "request": request, **block}
        return {}

    def _protective_block_from_request(self, request: dict) -> dict:
        receipt = request.get("receipt", {}) if isinstance(request.get("receipt"), dict) else {}
        broker_response = request.get("broker_response", {}) if isinstance(request.get("broker_response"), dict) else {}
        protective_status = str(broker_response.get("protective_status") or "")
        receipt_status = str(receipt.get("status") or request.get("status") or "")
        if protective_status not in {"failed", "partial"} and receipt_status != "protective_order_missing":
            return {}
        emergency = broker_response.get("emergency_close", {}) if isinstance(broker_response.get("emergency_close"), dict) else {}
        local = emergency.get("local_mirror", {}) if isinstance(emergency.get("local_mirror"), dict) else {}
        if emergency.get("status") == "closed" and local.get("closed") is True:
            return {}
        reason = f"protective_status={protective_status or 'unknown'} receipt_status={receipt_status or 'unknown'}"
        if emergency:
            reason += f" emergency_close={emergency.get('status', 'unknown')}"
        return {"reason": reason}

    def _alert_delivery_check(self) -> dict:
        sender = resolve_alert_sender()
        channel = getattr(sender, "channel", "unknown")
        if sender.configured:
            drill_rows = load_json(self.output_root / "alert_delivery_drill" / "current.json")
            drill = drill_rows[-1] if drill_rows else {}
            if drill.get("status") == "pass" and drill.get("delivered") is True and drill.get("channel") == channel:
                return self._check(
                    "alert_delivery",
                    "ok",
                    f"{channel} alert delivery is configured and last drill delivered",
                    {"channel": channel, "drill": drill},
                )
            return self._check(
                "alert_delivery",
                "warn",
                f"{channel} alert delivery is configured but no successful delivery drill is recorded",
                {
                    "channel": channel,
                    "command": "python3 -m pipelines.alert_delivery_drill --date <date>",
                    "last_drill": drill,
                },
            )
        return self._check(
            "alert_delivery",
            "warn",
            f"{channel} alert delivery is not configured; alerts are recorded locally only",
            {
                "channel": "log",
                "required_env": list(getattr(sender, "required_env", [])),
            },
        )

    def _runner_check(self) -> dict:
        current = self._load_mapping(self.output_root / "runner_status" / "current.json")
        if not current:
            return self._check("runner", "warn", "runner status missing", {})
        state = current.get("state")
        if state in {"ok", "running"}:
            message = "runner active" if state == "running" else "runner ok"
            return self._check("runner", "ok", message, current)
        return self._check("runner", "error", current.get("error") or current.get("state", "unknown"), current)

    def _system_vitals_checks(self, run_date: str) -> list[dict]:
        """M0 liveness — runs SystemVitals (writes its contract) and surfaces the
        alerting vitals as health checks so a dead machine pages itself.

        Liveness is judged from the persisted summary itself (present + fresh
        run_date + heartbeat age), not a strategy headcount — a missing summary is
        a warn, a stale one is down. The execution_blocker vital is dropped here
        because _active_demo_reconciliation_check already pages on demo drift —
        surfacing both would double-alert the same condition."""
        from services.system_vitals import SystemVitals, vitals_to_health_checks

        payload = SystemVitals(self.output_root, self.market_db).run(run_date)
        return [check for check in vitals_to_health_checks(payload) if check["name"] != "vitals_execution_blocker"]

    def _rollup(self, checks: list[dict]) -> str:
        states = {item["status"] for item in checks}
        if "error" in states:
            return "error"
        if "warn" in states:
            return "warn"
        return "ok"

    def _secrets_check(self, run_date: str) -> dict:
        audit = SecretsAudit(output_root=self.output_root).run(run_date)
        status = {"pass": "ok", "warn": "warn", "fail": "error"}.get(audit.get("status", "warn"), "warn")
        failing = [c["name"] for c in audit.get("checks", []) if c["status"] in {"fail", "warn"}]
        message = "secrets posture ok" if status == "ok" else f"secrets posture issues: {', '.join(failing) or 'see audit'}"
        return self._check("secrets_posture", status, message, {"audit_status": audit.get("status"), "checks": audit.get("checks", [])})

    def _check(self, name: str, status: str, message: str, details: dict) -> dict:
        return {"name": name, "status": status, "message": message, "details": details}

    def _load_mapping(self, path: Path) -> dict:
        if not path.exists():
            return {}
        import json

        return json.loads(path.read_text(encoding="utf-8"))
