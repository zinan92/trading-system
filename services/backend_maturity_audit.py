from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from services.config_loader import ROOT, load_pipeline_config
from services.dashboard_state import DashboardState
from services.journal_store import load_json, write_json
from services.trade_record_acceptance import TradeRecordAcceptanceAudit


MATURITY_AUDIT_VERSION = "backend-maturity-m0-m6-v1"


class BackendMaturityAudit:
    """User-facing M0-M6 maturity audit.

    This deliberately audits the "platform axis" only: trusted records,
    per-strategy books, edge labels, frequency diagnostics, and execution
    safety, then surfaces the PM allocation/promotion lock as M6. It does not
    require live-money readiness, OANDA credentials, or a human live approval
    artifact; those belong to the separate live-readiness gate.
    """

    def __init__(self, output_root: Path | None = None, market_db: Path | None = None) -> None:
        config = load_pipeline_config()
        self.output_root = Path(output_root or ROOT / config.get("output_root", "outputs"))
        self.market_db = Path(market_db or ROOT / config.get("local_market_db", "data/market_data.db"))

    def run(self, run_date: str) -> dict:
        state = self._base_snapshot(run_date)
        strategy_rows = ((state.get("performance_board") or {}).get("strategies") or [])
        strategy_ids = [str(row.get("strategy_id") or "") for row in strategy_rows if row.get("strategy_id")]
        strategy_details = {
            sid: self._strategy_snapshot(run_date, sid).get("strategy_detail", {})
            for sid in strategy_ids
        }
        checks = [
            self._m0_system_vitals(state),
            self._m1_trade_record_cards(run_date, strategy_details),
            self._m2_strategy_books(strategy_rows, strategy_details),
            self._m3_edge_judgment(strategy_rows, strategy_details),
            self._m4_frequency_diagnostics(state, strategy_rows),
            self._m5_execution_safety(state),
            self._m6_pm_allocation_gate(),
        ]
        platform_checks = [check for check in checks if not check["name"].startswith("M6_")]
        m6 = next((check for check in checks if check["name"].startswith("M6_")), {})
        status = "fail" if m6.get("status") == "fail" else ("pass" if all(check["status"] == "pass" for check in platform_checks) else "warn")
        payload = {
            "schema_version": MATURITY_AUDIT_VERSION,
            "run_date": run_date,
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "status": status,
            "summary": {
                "checks": len(checks),
                "passed": sum(1 for check in checks if check["status"] == "pass"),
                "locked": sum(1 for check in checks if check["status"] == "locked"),
                "reviewable": sum(1 for check in checks if check["status"] == "reviewable"),
                "warnings": sum(1 for check in checks if check["status"] == "warn"),
                "failed": sum(1 for check in checks if check["status"] == "fail"),
                "strategy_count": len(strategy_ids),
                "platform_status": "pass" if all(check["status"] == "pass" for check in platform_checks) else "warn",
                "pm_allocation_status": m6.get("status", "unknown"),
                "scope": "M0-M5 platform maturity plus M6 PM allocation gate; live-money approval is intentionally excluded.",
            },
            "checks": checks,
        }
        write_json(self.output_root / "backend_maturity" / "current.json", [payload])
        write_json(self.output_root / "backend_maturity" / f"{run_date}.json", [payload])
        return payload

    def _base_snapshot(self, run_date: str) -> dict:
        return DashboardState(output_root=self.output_root, market_db=self.market_db).snapshot(run_date)

    def _strategy_snapshot(self, run_date: str, strategy_id: str) -> dict:
        return DashboardState(output_root=self.output_root / "strategies" / strategy_id, market_db=self.market_db).snapshot(run_date)

    def _m0_system_vitals(self, state: dict) -> dict:
        vitals = state.get("system_vitals") or {}
        health = self._latest("health", "current")
        health_attention = (state.get("dashboard_health") or {}).get("health_attention") or []
        required = {
            "data_feed": "up",
            "runner_liveness": "up",
            "strategy_evaluation": "up",
            "tp_sl_coverage": "up",
            "execution_blocker": "up",
            "no_trade_attribution": "up",
        }
        actual = {item.get("name"): item.get("status") for item in vitals.get("vitals", []) if isinstance(item, dict)}
        missing_or_down = [
            {"name": name, "expected": expected, "actual": actual.get(name)}
            for name, expected in required.items()
            if actual.get(name) != expected
        ]
        if vitals.get("overall") != "alive" or missing_or_down or health.get("status") not in {"ok", "pass"} or health_attention:
            return self._check(
                "M0_system_vitals",
                "fail",
                "System vitals are not fully green from the user view.",
                {"overall": vitals.get("overall"), "missing_or_down": missing_or_down, "health_status": health.get("status"), "health_attention": health_attention},
            )
        return self._check("M0_system_vitals", "pass", "Data feed, runner, strategy evaluation, TP/SL coverage, and no-trade attribution are green.", {"overall": vitals.get("overall")})

    def _m1_trade_record_cards(self, run_date: str, strategy_details: dict[str, dict]) -> dict:
        bad = []
        total_cards = 0
        for sid, detail in strategy_details.items():
            audit = detail.get("trade_record_audit") or {}
            total_cards += int(audit.get("trade_count") or len(detail.get("trade_record_cards") or []) or 0)
            if audit.get("status") != "pass":
                bad.append({"strategy_id": sid, "audit": audit})
        acceptance = TradeRecordAcceptanceAudit(self.output_root, self.market_db).run(run_date, persist=True)
        acceptance_failure = acceptance.get("status") == "fail"
        if bad:
            return self._check(
                "M1_trade_record_cards",
                "fail",
                "At least one strategy has incomplete trade record cards.",
                {"bad": bad, "total_cards": total_cards, "sample_acceptance": acceptance},
            )
        if acceptance_failure:
            return self._check(
                "M1_trade_record_cards",
                "fail",
                "Representative trade-card samples failed the manual acceptance standard.",
                {"bad": acceptance.get("failures", []), "total_cards": total_cards, "sample_acceptance": acceptance},
            )
        return self._check(
            "M1_trade_record_cards",
            "pass",
            "Every strategy exposes complete per-trade record cards; representative samples are hand-checkable.",
            {"strategy_count": len(strategy_details), "total_cards": total_cards, "sample_acceptance": acceptance},
        )

    def _m2_strategy_books(self, strategy_rows: list[dict], strategy_details: dict[str, dict]) -> dict:
        bad = []
        for row in strategy_rows:
            sid = str(row.get("strategy_id") or "")
            book = row.get("strategy_book") or (strategy_details.get(sid, {}).get("strategy_book") or {})
            if (book.get("audit") or {}).get("status") != "pass":
                bad.append({"strategy_id": sid, "audit": book.get("audit") or {}})
        if bad:
            return self._check("M2_strategy_books", "fail", "At least one strategy book is not internally reconciled.", {"bad": bad})
        return self._check("M2_strategy_books", "pass", "Every strategy has a reconciled NAV/accounting book.", {"strategy_count": len(strategy_rows)})

    def _m3_edge_judgment(self, strategy_rows: list[dict], strategy_details: dict[str, dict]) -> dict:
        allowed = {"insufficient_sample", "winner", "loser", "neutral"}
        bad = []
        thin_as_winner = []
        labels = {}
        for row in strategy_rows:
            sid = str(row.get("strategy_id") or "")
            edge = row.get("edge_judgment") or (strategy_details.get(sid, {}).get("edge_judgment") or {})
            label = edge.get("label")
            labels[sid] = label
            if label not in allowed:
                bad.append({"strategy_id": sid, "label": label})
            sample = int(edge.get("closed_trade_count") or row.get("closed_trades") or 0)
            min_sample = int(edge.get("min_closed_trades") or 20)
            if sample < min_sample and label in {"winner", "loser", "neutral"}:
                thin_as_winner.append({"strategy_id": sid, "label": label, "closed_trade_count": sample, "min_closed_trades": min_sample})
        if bad or thin_as_winner:
            return self._check("M3_edge_judgment", "fail", "Edge judgment is missing or thin samples are being over-claimed.", {"bad": bad, "thin_overclaims": thin_as_winner, "labels": labels})
        return self._check("M3_edge_judgment", "pass", "Edge labels use closed trades only and keep thin samples as insufficient.", {"labels": labels})

    def _m4_frequency_diagnostics(self, state: dict, strategy_rows: list[dict]) -> dict:
        frequency = self._latest("strategy_frequency", "current")
        board = ((state.get("performance_board") or {}).get("frequency_board") or {})
        bad = []
        for row in strategy_rows:
            diag = row.get("frequency_diagnostics") or {}
            attribution = diag.get("attribution") or {}
            if attribution.get("frequency_is_diagnostic_not_sla") is not True:
                bad.append({"strategy_id": row.get("strategy_id"), "reason": "frequency diagnostic flag missing"})
            daily = row.get("daily_execution") or {}
            if "executed_trade_count" not in daily:
                bad.append({"strategy_id": row.get("strategy_id"), "reason": "executed_trade_count missing"})
        if frequency.get("schema_version") != "strategy-frequency-governance-v2" or bad:
            return self._check("M4_frequency_attribution", "fail", "Frequency diagnostics are missing or can still confuse signals with executions.", {"frequency_schema": frequency.get("schema_version"), "bad": bad})
        return self._check(
            "M4_frequency_attribution",
            "pass",
            "Frequency is diagnosed from executed trade counts and attributed to market/risk/quality/system causes.",
            {"status": frequency.get("status"), "portfolio_executed": (frequency.get("summary") or {}).get("portfolio_executed_count"), "board": board},
        )

    def _m5_execution_safety(self, state: dict) -> dict:
        health = self._latest("health", "current")
        checks = {item.get("name"): item for item in health.get("checks", []) if isinstance(item, dict)}
        drill = self._latest("alert_delivery_drill", "current")
        required_ok = [
            "active_demo_reconciliation",
            "alert_delivery",
            "vitals_tp_sl_coverage",
            "vitals_runner_liveness",
            "vitals_data_feed",
        ]
        bad = [
            {"name": name, "status": (checks.get(name) or {}).get("status"), "message": (checks.get(name) or {}).get("message")}
            for name in required_ok
            if (checks.get(name) or {}).get("status") != "ok"
        ]
        active_blocker = ((state.get("performance_board") or {}).get("active_demo_blocker") or {})
        if active_blocker.get("status") not in {"clear", "ok", "", None} and active_blocker.get("blocked") is True:
            bad.append({"name": "active_demo_blocker", "status": active_blocker.get("status"), "message": active_blocker.get("reason")})
        if drill.get("status") != "pass" or drill.get("delivered") is not True:
            bad.append({"name": "alert_delivery_drill", "status": drill.get("status"), "message": "delivery drill has not passed"})
        if bad:
            return self._check("M5_execution_safety", "fail", "Execution safety or proactive alerting is not fully green.", {"bad": bad})
        return self._check("M5_execution_safety", "pass", "TP/SL coverage, demo reconciliation, and Feishu alert delivery are verified.", {"alert_channel": drill.get("channel"), "active_demo_blocker": active_blocker})

    def _m6_pm_allocation_gate(self) -> dict:
        gate = self._latest("strategy_promotion_gate", "current")
        if not gate:
            return self._check(
                "M6_pm_allocation_gate",
                "warn",
                "M6 PM allocation gate artifact is missing; do not allocate or auto-tune.",
                {"promotion_allowed": False, "blockers": [{"name": "missing_gate", "summary": "strategy_promotion_gate/current.json is missing"}]},
            )
        blockers = gate.get("blockers", []) if isinstance(gate.get("blockers", []), list) else []
        evidence = {
            "strategy_id": gate.get("strategy_id", ""),
            "status": gate.get("status", ""),
            "promotion_allowed": gate.get("promotion_allowed") is True,
            "auto_apply": gate.get("auto_apply") is True,
            "paper_only": gate.get("paper_only") is True,
            "live_config_change_allowed": gate.get("live_config_change_allowed") is True,
            "closed_trade_count": gate.get("closed_trade_count", 0),
            "official_rows": gate.get("official_rows", 0),
            "data_truth_level": gate.get("data_truth_level", ""),
            "blockers": blockers,
        }
        if evidence["auto_apply"] or evidence["live_config_change_allowed"]:
            return self._check(
                "M6_pm_allocation_gate",
                "fail",
                "M6 red line breached: strategy changes must not auto-apply or alter live config.",
                evidence,
            )
        if evidence["promotion_allowed"]:
            return self._check(
                "M6_pm_allocation_gate",
                "reviewable",
                "M6 promotion gate is reviewable; PM must still approve any allocation or parameter change.",
                evidence,
            )
        first = blockers[0].get("summary") if blockers and isinstance(blockers[0], dict) else "No proven winner; allocation and auto-tuning stay locked."
        return self._check(
            "M6_pm_allocation_gate",
            "locked",
            str(first),
            evidence,
        )

    def _latest(self, folder: str, name: str) -> dict:
        rows = load_json(self.output_root / folder / f"{name}.json")
        return rows[-1] if rows else {}

    @staticmethod
    def _check(name: str, status: str, summary: str, evidence: dict) -> dict:
        return {"name": name, "status": status, "summary": summary, "evidence": evidence}
