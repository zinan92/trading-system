"""Evidence-backed daily self-review for the focused Grid/DCA Paper system."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from services.file_hash import sha256_file
from zoneinfo import ZoneInfo

from services.journal_store import load_json, write_json


BJ_TZ = ZoneInfo("Asia/Shanghai")
EXPECTED_TICKS_PER_DAY = 24 * 60


class CloudDailySelfReview:
    def __init__(
        self,
        output_root: Path,
        *,
        now: Callable[[], datetime] | None = None,
        deployed_sha: str = "",
    ) -> None:
        self.output_root = Path(output_root)
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.deployed_sha = str(deployed_sha)

    def build(self, report_date: str | date) -> dict[str, Any]:
        target = (
            report_date
            if isinstance(report_date, date)
            else date.fromisoformat(str(report_date))
        )
        start = datetime.combine(target, time.min, tzinfo=BJ_TZ).astimezone(timezone.utc)
        end = start + timedelta(days=1)
        evidence = self._evidence(target, start, end)
        evidence_hash = _hash_json(evidence)
        revision_dir = (
            self.output_root
            / "dualtrack"
            / "daily_self_reviews"
            / target.isoformat()
            / "revisions"
        )
        revision_path = revision_dir / f"{evidence_hash}.json"
        existing = load_json(revision_path)
        if existing and isinstance(existing[-1], dict):
            return existing[-1]

        operational = self._operational(evidence)
        execution = self._execution(evidence)
        strategy = self._strategy(evidence)
        went_well, went_poorly = self._findings(operational, execution, strategy)
        actions = self._actions(operational, execution, strategy)
        payload = {
            "schema_version": "cloud-daily-self-review-v1",
            "status": (
                "complete"
                if execution["status"] == "complete"
                and operational["status"] != "unknown"
                else "incomplete"
            ),
            "report_date": target.isoformat(),
            "timezone": "Asia/Shanghai",
            "window": {"start": start.isoformat(), "end": end.isoformat()},
            "generated_at": self.now().astimezone(timezone.utc).replace(microsecond=0).isoformat(),
            "deployed_sha": self.deployed_sha or None,
            "evidence_revision": evidence_hash,
            "control_actions_executed": 0,
            "operational_truth": operational,
            "execution_truth": execution,
            "strategy_truth": strategy,
            "went_well": went_well,
            "went_poorly": went_poorly,
            "tomorrow_actions": actions,
            "evidence": evidence,
        }
        payload["review_hash"] = _hash_json(payload)
        write_json(revision_path, [payload])
        current_path = revision_dir.parent / "current.json"
        write_json(current_path, [payload])
        write_json(
            self.output_root / "dualtrack" / "daily_self_reviews" / "current.json",
            [payload],
        )
        markdown_path = revision_dir.parent / f"{evidence_hash}.md"
        markdown_path.write_text(self._markdown(payload), encoding="utf-8")
        return payload

    def _evidence(
        self,
        target: date,
        start: datetime,
        end: datetime,
    ) -> dict[str, Any]:
        report_path = (
            self.output_root
            / "dualtrack"
            / "daily_reports"
            / f"{target.isoformat()}.json"
        )
        report_rows = load_json(report_path)
        report = report_rows[-1] if report_rows and isinstance(report_rows[-1], dict) else None
        report_integrity = "missing"
        if report is not None:
            observed_hash = str(report.get("report_hash") or "")
            candidate = dict(report)
            candidate.pop("report_hash", None)
            report_integrity = (
                "pass" if observed_hash and observed_hash == _hash_json(candidate) else "invalid"
            )
            if report_integrity != "pass":
                report = None
        heartbeat_rows: list[dict[str, Any]] = []
        runner_files: list[dict[str, str]] = []
        runner_dir = self.output_root / "dualtrack" / "runner"
        for path in sorted(runner_dir.glob("*.json")) if runner_dir.exists() else []:
            file_rows = load_json(path)
            selected = [
                row
                for row in file_rows
                if isinstance(row, dict)
                and row.get("event") == "live_tick_heartbeat"
                and _inside(str(row.get("ts") or ""), start, end)
            ]
            if selected:
                heartbeat_rows.extend(selected)
                runner_files.append({"path": str(path), "sha256": _hash_file(path)})
        package_rows: list[dict[str, Any]] = []
        package_files: list[dict[str, str]] = []
        provenance = (report or {}).get("provenance") or {}
        for reference in provenance.get("cycle_packages") or []:
            if not isinstance(reference, dict):
                continue
            cycle_id = str(reference.get("cycle_id") or "")
            path = (
                self.output_root
                / "dualtrack"
                / "strategy_cycle_packages"
                / f"{cycle_id}.json"
            )
            rows = load_json(path)
            if rows and isinstance(rows[-1], dict):
                package_rows.append(rows[-1])
                package_files.append({"path": str(path), "sha256": _hash_file(path)})
        failure_path = (
            self.output_root
            / "dualtrack"
            / "strategy_control"
            / "live_tick_failure.json"
        )
        failure_rows = [
            row
            for row in load_json(failure_path)
            if isinstance(row, dict)
            and _inside(
                str(row.get("recorded_at") or row.get("ts") or ""),
                start,
                end,
            )
        ]
        return {
            "daily_report": {
                "path": str(report_path),
                "available": report is not None,
                "integrity": report_integrity,
                "sha256": _hash_file(report_path) if report_path.is_file() else None,
                "payload": report,
            },
            "runner": {
                "heartbeat_count": len(heartbeat_rows),
                "heartbeat_timestamps": sorted(
                    {str(row.get("ts") or "") for row in heartbeat_rows}
                ),
                "files": runner_files,
            },
            "cycle_packages": {
                "count": len(package_rows),
                "files": package_files,
                "payloads": package_rows,
            },
            "live_tick_failures": {
                "path": str(failure_path),
                "sha256": _hash_file(failure_path) if failure_path.is_file() else None,
                "rows": failure_rows,
            },
        }

    def _operational(self, evidence: dict[str, Any]) -> dict[str, Any]:
        count = len(evidence["runner"]["heartbeat_timestamps"])
        coverage = count / EXPECTED_TICKS_PER_DAY
        if count == 0:
            status = "unknown"
        elif coverage >= 0.95:
            status = "healthy"
        else:
            status = "degraded"
        failures = evidence["live_tick_failures"]["rows"]
        datafeed_failures = [
            row for row in failures if str(row.get("failure_phase") or "") == "route_datafeed"
        ]
        return {
            "status": status,
            "successful_tick_count": count,
            "expected_tick_count": EXPECTED_TICKS_PER_DAY,
            "tick_coverage_pct": round(coverage * 100, 4),
            "missing_tick_count": max(0, EXPECTED_TICKS_PER_DAY - count),
            "datafeed_failure_count": len(datafeed_failures),
            "tick_failure_count": len(failures),
            "service_restart_count": None,
            "unknowns": (
                ["service_restart_count_not_available"]
                + (["tick_history_missing"] if count == 0 else [])
            ),
        }

    def _execution(self, evidence: dict[str, Any]) -> dict[str, Any]:
        report = evidence["daily_report"]["payload"]
        if not isinstance(report, dict):
            return {
                "status": "unknown",
                "trade_count": None,
                "fill_count": None,
                "realized_pnl": None,
                "reconciliation": "unknown",
                "reason": "terminal_daily_report_missing",
            }
        execution = report.get("execution") if isinstance(report.get("execution"), dict) else {}
        reconciliation = str(execution.get("reconciliation_status") or "pass")
        status = "complete" if report.get("status") == "complete" and reconciliation == "pass" else "blocked"
        return {
            "status": status,
            "trade_count": _optional_int(execution.get("trade_count")),
            "fill_count": _optional_int(execution.get("fill_count")),
            "total_notional": _optional_float(execution.get("total_notional")),
            "realized_pnl": _optional_float(execution.get("realized_pnl")),
            "reconciliation": reconciliation,
            "report_hash": report.get("report_hash"),
        }

    def _strategy(self, evidence: dict[str, Any]) -> dict[str, Any]:
        strategies: list[dict[str, Any]] = []
        for package in evidence["cycle_packages"]["payloads"]:
            plan = package.get("strategy_plan") if isinstance(package.get("strategy_plan"), dict) else {}
            if not plan:
                continue
            grid = plan.get("grid") if isinstance(plan.get("grid"), dict) else {}
            strategies.append(
                {
                    "cycle_id": package.get("cycle_id"),
                    "strategy_plan_id": plan.get("strategy_plan_id"),
                    "strategy_plan_version": plan.get("version"),
                    "strategy_type": plan.get("strategy_type") or "grid",
                    "direction": plan.get("direction"),
                    "style": plan.get("style"),
                    "range": plan.get("range"),
                    "grid_count": grid.get("count"),
                    "target_net_profit_per_grid_usd": grid.get(
                        "target_net_profit_per_grid_usd"
                    ),
                    "actual_leverage": grid.get("actual_leverage"),
                    "lifecycle_status": package.get("status"),
                }
            )
        return {
            "status": "available" if strategies else "unknown",
            "strategy_count": len(strategies) if strategies else None,
            "strategies": strategies,
            "shadow_comparison": "unknown",
            "unknowns": [] if strategies else ["terminal_strategy_plan_missing"],
        }

    def _findings(
        self,
        operational: dict[str, Any],
        execution: dict[str, Any],
        strategy: dict[str, Any],
    ) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
        good: list[dict[str, str]] = []
        poor: list[dict[str, str]] = []
        if operational["status"] == "healthy":
            good.append(_finding("tick_coverage_healthy", "行情与 Paper tick 覆盖达到 95% 以上。", "runner"))
        elif operational["status"] == "degraded":
            poor.append(_finding("tick_coverage_degraded", "Paper tick 覆盖不足 95%，连续运行不达标。", "runner"))
        else:
            poor.append(_finding("tick_history_unknown", "没有足够 tick 历史，无法证明系统持续运行。", "runner"))
        if operational["datafeed_failure_count"]:
            poor.append(
                _finding(
                    "datafeed_failures",
                    f"当日记录到 {operational['datafeed_failure_count']} 次 datafeed 路由失败。",
                    "live_tick_failures",
                )
            )
        if execution["status"] == "complete":
            good.append(_finding("terminal_reconciliation_pass", "终态交易报告与对账证据完整。", "daily_report"))
        else:
            poor.append(_finding("execution_truth_unavailable", "终态交易或对账证据不完整。", "daily_report"))
        pnl = execution.get("realized_pnl")
        if isinstance(pnl, (int, float)) and pnl > 0:
            good.append(_finding("profitable_day", f"当日已实现收益 {pnl:+.2f} USD。", "daily_report"))
        elif isinstance(pnl, (int, float)) and pnl < 0:
            poor.append(_finding("loss_day", f"当日已实现亏损 {pnl:+.2f} USD。", "daily_report"))
        if execution.get("trade_count") == 0:
            poor.append(_finding("no_trade_sample", "当日没有完成交易，无法形成新的策略效果样本。", "daily_report"))
        if execution.get("reconciliation") not in {"pass", "unknown"}:
            poor.append(_finding("reconciliation_failed", "执行对账未通过，收益结论不可用于策略判断。", "daily_report"))
        if strategy["status"] == "unknown":
            poor.append(_finding("strategy_truth_unknown", "缺少终态 StrategyPlan，无法评价当日策略设定。", "cycle_packages"))
        return good, poor

    def _actions(
        self,
        operational: dict[str, Any],
        execution: dict[str, Any],
        strategy: dict[str, Any],
    ) -> list[dict[str, Any]]:
        actions: list[dict[str, Any]] = []
        if operational["status"] != "healthy":
            actions.append(
                _action(
                    1,
                    "restore_tick_continuity",
                    "恢复并验证 datafeed/live-tick 连续性；只允许服务级恢复。",
                    "automatic_service_recovery",
                    "runner heartbeat coverage >= 95%",
                )
            )
        if execution["status"] != "complete":
            actions.append(
                _action(
                    2,
                    "repair_terminal_evidence",
                    "补齐终态周期报告与对账证据，在通过前不要评价策略收益。",
                    "automatic_service_recovery",
                    "terminal daily report and reconciliation pass",
                )
            )
        if execution.get("reconciliation") not in {"pass", "unknown"}:
            actions.append(
                _action(
                    3,
                    "review_reconciliation_drift",
                    "人工核对订单、成交、持仓和账本身份；不得自动重放控制动作。",
                    "human_strategy_decision",
                    "reconciliation pass with zero unexplained issues",
                )
            )
        pnl = execution.get("realized_pnl")
        if isinstance(pnl, (int, float)) and pnl < 0:
            actions.append(
                _action(
                    4,
                    "review_loss_attribution",
                    "比较 Range、方向、格距、费用和退出原因，形成下一轮 Shadow 假设。",
                    "human_strategy_decision",
                    "evidence-linked loss attribution or explicit unknown",
                )
            )
        if execution.get("trade_count") == 0 and execution["status"] == "complete":
            actions.append(
                _action(
                    5,
                    "review_no_trade_market_fit",
                    "检查当日市场是否适合当前 Grid/DCA；只提出 What-if，不自动改主策略。",
                    "human_strategy_decision",
                    "one comparable Shadow hypothesis or documented no-change decision",
                )
            )
        if strategy["status"] == "unknown":
            actions.append(
                _action(
                    6,
                    "restore_strategy_traceability",
                    "补齐 StrategyPlan 与终态周期包关联后再做策略复盘。",
                    "automatic_service_recovery",
                    "terminal package links exact StrategyPlan id/version",
                )
            )
        return sorted(actions, key=lambda row: (row["priority"], row["action_id"]))

    def _markdown(self, payload: dict[str, Any]) -> str:
        execution = payload["execution_truth"]
        operational = payload["operational_truth"]
        lines = [
            f"# GridMind 每日自复盘｜{payload['report_date']}",
            "",
            "## 今日事实",
            "",
            (
                f"系统：{operational['status']}｜成功 tick "
                f"{operational['successful_tick_count']}/{operational['expected_tick_count']}｜"
                f"覆盖 {operational['tick_coverage_pct']:.2f}%"
            ),
            (
                f"交易：{execution['status']}｜完成轮次 "
                f"{_display(execution.get('trade_count'))}｜已实现 PnL "
                f"{_display_money(execution.get('realized_pnl'))}｜"
                f"对账 {execution.get('reconciliation')}"
            ),
            "",
            "## 做对了什么",
            "",
            *[f"- {row['summary']}" for row in payload["went_well"]],
            "",
            "## 哪里做得不好",
            "",
            *[f"- {row['summary']}" for row in payload["went_poorly"]],
            "",
            "## 明天怎么改",
            "",
            *[
                f"{row['priority']}. {row['summary']}｜权限：{row['permission_class']}"
                for row in payload["tomorrow_actions"]
            ],
            "",
            "## 证据",
            "",
            f"- evidence revision: `{payload['evidence_revision']}`",
            f"- review hash: `{payload['review_hash']}`",
            f"- deployed SHA: `{payload.get('deployed_sha') or 'unknown'}`",
            "- 本复盘执行的控制动作：0",
            "",
        ]
        if not payload["went_well"]:
            lines.insert(lines.index("## 哪里做得不好") - 1, "- 无可证实的正向结论。")
        if not payload["tomorrow_actions"]:
            index = lines.index("## 证据") - 1
            lines.insert(index, "1. 保持当前 Paper 配置，继续收集样本｜权限：human_strategy_decision")
        return "\n".join(lines)


def load_daily_self_review(output_root: Path, report_date: str | None = None) -> dict[str, Any]:
    base = Path(output_root) / "dualtrack" / "daily_self_reviews"
    path = base / report_date / "current.json" if report_date else base / "current.json"
    rows = load_json(path)
    if not rows or not isinstance(rows[-1], dict):
        raise ValueError("daily self-review not found")
    payload = rows[-1]
    observed = str(payload.get("review_hash") or "")
    candidate = dict(payload)
    candidate.pop("review_hash", None)
    if not observed or observed != _hash_json(candidate):
        raise ValueError("daily self-review hash is invalid")
    return payload


def _inside(value: str, start: datetime, end: datetime) -> bool:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return start <= parsed.astimezone(timezone.utc) < end
    except ValueError:
        return False


def _hash_file(path: Path) -> str:
    return sha256_file(path)


def _hash_json(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return int(value)


def _finding(code: str, summary: str, evidence: str) -> dict[str, str]:
    return {"code": code, "summary": summary, "evidence": evidence}


def _action(
    priority: int,
    action_id: str,
    summary: str,
    permission_class: str,
    expected_evidence: str,
) -> dict[str, Any]:
    return {
        "priority": priority,
        "action_id": action_id,
        "summary": summary,
        "owner": "system" if permission_class == "automatic_service_recovery" else "Park",
        "reason": action_id,
        "permission_class": permission_class,
        "expected_evidence": expected_evidence,
        "executed": False,
    }


def _display(value: Any) -> str:
    return "未知" if value is None else str(value)


def _display_money(value: Any) -> str:
    return "未知" if value is None else f"{float(value):+.2f} USD"
