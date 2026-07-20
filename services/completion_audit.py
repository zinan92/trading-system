from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from services.config_loader import ROOT, load_pipeline_config, load_risk_rules, load_strategy_config
from services.bias_ledger import BiasLedger
from services.broker_adapter import LiveBrokerAdapter
from services.data_source_preflight import DataSourcePreflight
from services.data_archive_manifest import DataArchiveManifest
from services.dualtrack_cycle_heartbeat import DualTrackCycleHeartbeat
from services.journal_store import load_json, write_json
from services.market_data_access import market_data_repository
from services.mock_runtime import MockTradingRuntime
from services.mock_trading_uat import MockTradingUAT
from services.risk_monitor import RiskMonitor
from services.schedule_status import ScheduleStatus
from services.schedule_profiles import labels_for_profile, profile_from_config, profile_from_schedule, is_focus_profile
from services.live_activation import LiveActivationGate
from services.live_cutover_package import LiveCutoverPackage
from services.live_readiness import LiveReadiness
from services.live_submission_safety import LiveSubmissionSafetySmoke
from services.live_switch_plan import LiveSwitchPlan
from services.oanda_account_preflight import OandaAccountPreflight


class CompletionAudit:
    def __init__(self, output_root: Path | None = None, market_db: Path | None = None) -> None:
        config = load_pipeline_config()
        self.config = config
        self.output_root = output_root or Path(os.getenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(ROOT / config.get("output_root", "outputs"))))
        self.market_db = market_db or Path(os.getenv("TRADING_ORCHESTRATOR_MARKET_DB", str(ROOT / config.get("local_market_db", "data/market_data.db"))))

    def run(self, run_date: str) -> dict:
        requirements = [
            self._data_pipeline(run_date),
            self._market_data_source(run_date),
            self._five_minute_gold_strategy(run_date),
            self._local_storage(),
            self._data_archive(run_date),
            self._risk_system(run_date),
            self._risk_monitor(run_date),
            self._paper_trading(run_date),
            self._mock_runtime(run_date),
            self._mock_uat(run_date),
            self._paper_performance(run_date),
            self._paper_equity_curve(run_date),
            self._paper_reconciliation(run_date),
            self._paper_trade_attribution(run_date),
            self._journal_and_review(run_date),
            self._human_bias_ledger(run_date),
            self._strategy_guardrails(run_date),
            self._daily_review_run(run_date),
            self._schedule_artifacts(run_date),
            self._dualtrack_cycle_liveness(run_date),
            self._dashboard(),
            self._broker_feed_smoke(run_date),
            self._broker_bridge_smoke(run_date),
            self._oanda_broker_boundary(run_date),
            self._live_submission_safety(run_date),
            self._live_broker_boundary(),
            self._live_activation_gate(run_date),
            self._live_cutover_package(run_date),
        ]
        status = self._rollup(requirements)
        payload = {
            "run_date": run_date,
            "checked_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "status": status,
            "requirements": requirements,
        }
        write_json(self.output_root / "audits" / "current.json", [payload])
        write_json(self.output_root / "audits" / f"{run_date}.json", [payload])
        return payload

    def _data_pipeline(self, run_date: str) -> dict:
        raw = self.output_root / "raw_snapshots" / run_date / "GOLD_5m.json"
        quote_snapshots = self.output_root / "raw_snapshots" / run_date / "quote_snapshots.json"
        clean = self.output_root / "clean_bars" / run_date / "GOLD_5m.json"
        manifest = self.output_root / "clean_bars" / run_date / "manifest.json"
        quality = self._load_mapping(self.output_root / "data_quality" / f"{run_date}.json").get("GOLD", {})
        missing = [str(path) for path in [raw, quote_snapshots, clean, manifest] if not path.exists()]
        if missing:
            return self._requirement("data_pipeline", "fail", "数据获取/清洗/保存产物不完整", {"missing": missing})
        bars = load_json(clean)
        quote_rows = load_json(quote_snapshots)
        latest = bars[-1] if bars else {}
        if not bars:
            return self._requirement("data_pipeline", "fail", "清洗后的 GOLD 5m K 线为空", {"clean_path": str(clean)})
        if not quote_rows:
            return self._requirement("data_pipeline", "fail", "实时 quote raw snapshot 为空", {"quote_snapshots": str(quote_snapshots)})
        if quality and not quality.get("allows_trading", False):
            return self._requirement("data_pipeline", "fail", "数据质量门禁不允许交易", {"data_quality": quality})
        return self._requirement(
            "data_pipeline",
            "pass",
            "GOLD 5m 数据获取、清洗、保存链路可验证",
            {
                "raw_path": str(raw),
                "quote_snapshots_path": str(quote_snapshots),
                "clean_path": str(clean),
                "manifest_path": str(manifest),
                "clean_rows": len(bars),
                "quote_snapshot_rows": len(quote_rows),
                "latest_close": latest.get("close"),
                "latest_provider": latest.get("provider"),
                "data_quality": quality,
            },
        )

    def _five_minute_gold_strategy(self, run_date: str) -> dict:
        strategy = load_strategy_config().get("gold_5m_v1", {})
        signals = load_json(self.output_root / "signals" / f"{run_date}.json")
        backtests = load_json(self.output_root / "backtests" / f"{run_date}.json")
        signal = next((item for item in signals if item.get("asset") == "GOLD"), {})
        backtest = next((item for item in backtests if item.get("asset") == "GOLD"), {})
        timeframes = self.config.get("timeframes", [])
        if timeframes != ["5m"]:
            return self._requirement("five_minute_gold_strategy", "fail", "主交易周期不是单一 5m", {"timeframes": timeframes})
        if not strategy:
            return self._requirement("five_minute_gold_strategy", "fail", "缺少 gold_5m_v1 策略配置", {})
        if not signal or not backtest:
            return self._requirement("five_minute_gold_strategy", "fail", "缺少 GOLD 信号或本地回测证据", {"signals": len(signals), "backtests": len(backtests)})
        return self._requirement(
            "five_minute_gold_strategy",
            "pass",
            "黄金 5m 策略、信号和本地回测证据存在",
            {
                "strategy": "gold_5m_v1",
                "signal_direction": signal.get("direction"),
                "signal_strength": signal.get("strength"),
                "signal_regime": signal.get("regime"),
                "backtest_id": backtest.get("backtest_id"),
                "backtest_verdict": backtest.get("verdict"),
                "backtest_sample_size": backtest.get("sample_size"),
                "backtest_evaluated_bars": backtest.get("evaluated_bars"),
                "backtest_setup_count": backtest.get("setup_count"),
                "backtest_skipped_reason": backtest.get("skipped_reason"),
            },
        )

    def _market_data_source(self, run_date: str) -> dict:
        DataSourcePreflight(self.output_root, self.market_db).run(run_date)
        rows = load_json(self.output_root / "data_source_preflight" / f"{run_date}.json")
        latest = rows[-1] if rows else {}
        if not latest:
            return self._requirement("market_data_source", "fail", "缺少行情源 preflight 证据", {})
        if latest.get("ready_for_live"):
            return self._requirement("market_data_source", "pass", "GOLD 5m 正式 broker 行情源可用于 live", latest)
        if latest.get("ready_for_paper"):
            return self._requirement("market_data_source", "warn", "当前行情源可用于 paper，但不是正式 broker 行情源", latest)
        return self._requirement("market_data_source", "fail", "GOLD 5m 行情源不可用", latest)

    def _local_storage(self) -> dict:
        coverage = market_data_repository(self.market_db).coverage()
        gold_rows = [item for item in coverage if item["symbol"] == "GOLD" and item["timeframe"] == "5m"]
        total = sum(item["rows"] for item in gold_rows)
        non_seed = sum(item["rows"] for item in gold_rows if item["provider"] != "local_synthetic_seed")
        if non_seed < 200:
            return self._requirement("local_storage", "fail", "datafeed 的 GOLD 5m 非 seed 数据不足", {"backend": "datafeed", "total_rows": total, "non_seed_rows": non_seed, "coverage": gold_rows})
        return self._requirement("local_storage", "pass", "datafeed 已保存足够 GOLD 5m 行情", {"backend": "datafeed", "total_rows": total, "non_seed_rows": non_seed, "coverage": gold_rows})

    def _data_archive(self, run_date: str) -> dict:
        archive = DataArchiveManifest(self.output_root, self.market_db).run(run_date)
        if archive.get("status") == "pass":
            return self._requirement("data_archive", "pass", "每日本地数据归档清单已生成并带文件指纹", archive)
        return self._requirement("data_archive", "warn", "每日本地数据归档清单存在但有缺失项", archive)

    def _risk_system(self, run_date: str) -> dict:
        rules = load_risk_rules().get("default", {})
        risk_blocks = load_json(self.output_root / "risk_blocks" / f"{run_date}.json")
        tickets = load_json(self.output_root / "trade_tickets" / f"{run_date}.json")
        health_rows = load_json(self.output_root / "health" / "current.json")
        health = health_rows[-1] if health_rows else {}
        has_rules = "max_loss_pct" in rules and "daily_loss_stop_pct" in rules
        has_gate = any(check.get("name") == "data_quality" for check in health.get("checks", [])) if health else False
        if not has_rules:
            return self._requirement("risk_system", "fail", "缺少核心风控参数", {"rules": rules})
        return self._requirement(
            "risk_system",
            "pass" if has_gate else "warn",
            "风控参数、ticket/risk block 和健康检查可追踪",
            {
                "max_loss_pct": rules.get("max_loss_pct"),
                "daily_loss_stop_pct": rules.get("daily_loss_stop_pct"),
                "tickets": len(tickets),
                "risk_blocks": len(risk_blocks),
                "health_data_quality_gate": has_gate,
            },
        )

    def _risk_monitor(self, run_date: str) -> dict:
        monitor = RiskMonitor(self.output_root).run(run_date)
        evidence = {
            "status": monitor.get("status"),
            "kill_switch_active": monitor.get("kill_switch_active"),
            "allow_paper_auto_approve": monitor.get("allow_paper_auto_approve"),
            "summary": monitor.get("summary", {}),
            "source_artifacts": monitor.get("source_artifacts", {}),
        }
        if monitor.get("status") in {"pass", "warn", "block"} and "kill_switch_active" in monitor:
            return self._requirement(
                "risk_monitor",
                "pass",
                "运行中风控 monitor 已生成，可追踪 kill-switch 和 paper auto-approve 状态",
                evidence,
            )
        return self._requirement("risk_monitor", "fail", "运行中风控 monitor 产物缺失或结构不完整", evidence)

    def _paper_trading(self, run_date: str) -> dict:
        orders = load_json(self.output_root / "paper_orders" / f"{run_date}.json")
        positions = self._load_mapping(self.output_root / "paper_positions" / "current.json")
        open_trades = load_json(self.output_root / "paper_trades" / "current.json")
        decisions = load_json(self.output_root / "journal_decisions" / f"{run_date}.json")
        executed = [item for item in decisions if item.get("decision_status") == "executed_paper"]
        if not orders and not executed:
            return self._requirement("paper_trading", "warn", "Mock Trading 框架存在，但当天没有 paper 成交证据", {"orders": 0, "executed_paper_decisions": 0})
        return self._requirement(
            "paper_trading",
            "pass",
            "Mock Trading 已跑出 paper 决策、订单和持仓/交易状态",
            {"orders": len(orders), "positions": len(positions), "open_trades": len(open_trades), "executed_paper_decisions": len(executed)},
        )

    def _mock_runtime(self, run_date: str) -> dict:
        runtime = MockTradingRuntime(self.output_root, self.market_db).run(run_date)
        evidence = {
            "status": runtime.get("status"),
            "mock_ready": runtime.get("mock_ready"),
            "mock_running": runtime.get("mock_running"),
            "summary": runtime.get("summary", {}),
            "next_actions": runtime.get("next_actions", []),
        }
        if runtime.get("mock_ready") and runtime.get("mock_running"):
            status = "pass" if runtime.get("status") == "pass" else "warn"
            return self._requirement(status=status, name="mock_runtime", summary="Mock Trading 本地 5m 闭环已运行并可验证", evidence=evidence)
        if runtime.get("mock_ready"):
            return self._requirement("mock_runtime", "warn", "Mock Trading 本地闭环可用，但 runner 不新鲜或未持续运行", evidence)
        return self._requirement("mock_runtime", "fail", "Mock Trading 本地闭环未准备好", evidence)

    def _mock_uat(self, run_date: str) -> dict:
        uat = MockTradingUAT(self.output_root).run(run_date)
        evidence = {
            "status": uat.get("status"),
            "summary": uat.get("summary", {}),
            "evidence_paths": uat.get("evidence_paths", {}),
            "next_actions": uat.get("next_actions", []),
        }
        if uat.get("status") == "pass":
            return self._requirement("mock_uat", "pass", "Mock Trading UAT 证据包完整，模拟交易闭环可验收", evidence)
        if uat.get("status") == "warn":
            return self._requirement("mock_uat", "warn", "Mock Trading UAT 可用但仍有待补证据项", evidence)
        return self._requirement("mock_uat", "fail", "Mock Trading UAT 未通过，本地模拟闭环证据不完整", evidence)

    def _paper_performance(self, run_date: str) -> dict:
        rows = load_json(self.output_root / "performance" / f"{run_date}.json")
        current = load_json(self.output_root / "performance" / "current.json")
        performance = rows[-1] if rows else (current[-1] if current else {})
        summary = performance.get("summary", {}) if performance else {}
        required = ["open_trade_count", "closed_all_count", "realized_pnl_all", "unrealized_pnl", "net_pnl_marked", "open_unrealized_r", "expectancy_r", "profit_factor", "total_execution_costs"]
        missing = [key for key in required if key not in summary]
        if not performance:
            return self._requirement("paper_performance", "fail", "缺少 paper performance 量化复盘产物", {"path": str(self.output_root / "performance" / f"{run_date}.json")})
        if missing:
            return self._requirement("paper_performance", "fail", "paper performance 指标不完整", {"missing": missing, "summary": summary})
        return self._requirement(
            "paper_performance",
            "pass",
            "Paper Trading 表现指标已量化，可用于每日复盘",
            {
                "run_date": performance.get("run_date"),
                "generated_at": performance.get("generated_at"),
                "summary": summary,
                "open_trade_rows": len(performance.get("open_trades", [])),
                "closed_today_rows": len(performance.get("closed_today", [])),
            },
        )

    def _paper_reconciliation(self, run_date: str) -> dict:
        rows = load_json(self.output_root / "paper_reconciliation" / f"{run_date}.json")
        reconciliation = rows[-1] if rows else {}
        if not reconciliation:
            return self._requirement("paper_reconciliation", "fail", "缺少 paper 订单/交易/仓位/日志对账产物", {"path": str(self.output_root / "paper_reconciliation" / f"{run_date}.json")})
        if reconciliation.get("status") != "pass":
            return self._requirement("paper_reconciliation", "fail", "paper account 对账未通过", reconciliation)
        return self._requirement(
            "paper_reconciliation",
            "pass",
            "Paper 订单、交易、仓位和 journal 决策可对账",
            {
                "status": reconciliation.get("status"),
                "summary": reconciliation.get("summary", {}),
                "computed_positions": reconciliation.get("computed_positions", {}),
            },
        )

    def _paper_trade_attribution(self, run_date: str) -> dict:
        rows = load_json(self.output_root / "paper_trade_attribution" / f"{run_date}.json")
        attribution = rows[-1] if rows else {}
        if not attribution:
            return self._requirement("paper_trade_attribution", "fail", "缺少 paper trade 策略归因产物", {"path": str(self.output_root / "paper_trade_attribution" / f"{run_date}.json")})
        summary = attribution.get("summary", {})
        total = int(summary.get("open_trades", 0) or 0) + int(summary.get("closed_trades", 0) or 0)
        attributed = int(summary.get("attributed_open_trades", 0) or 0) + int(summary.get("attributed_closed_trades", 0) or 0)
        if attribution.get("status") != "pass":
            return self._requirement("paper_trade_attribution", "fail", "paper trade 策略归因运行失败", attribution)
        if total and attributed < total:
            return self._requirement(
                "paper_trade_attribution",
                "warn",
                "部分 paper trade 仍缺少可追溯策略归因",
                {"summary": summary, "source_artifacts": attribution.get("source_artifacts", {})},
            )
        return self._requirement(
            "paper_trade_attribution",
            "pass",
            "Paper trade 已纳入策略归因质量检查，可用于复盘分层统计",
            {"summary": summary, "source_artifacts": attribution.get("source_artifacts", {})},
        )

    def _paper_equity_curve(self, run_date: str) -> dict:
        rows = load_json(self.output_root / "equity_curve" / f"{run_date}.json")
        curve = rows[-1] if rows else {}
        if not curve:
            return self._requirement("paper_equity_curve", "fail", "缺少 paper equity curve 跨日权益曲线", {"path": str(self.output_root / "equity_curve" / f"{run_date}.json")})
        required = ["current_equity", "current_drawdown_pct", "max_drawdown_pct", "point_count", "points"]
        missing = [key for key in required if key not in curve]
        if missing:
            return self._requirement("paper_equity_curve", "fail", "paper equity curve 指标不完整", {"missing": missing, "curve": curve})
        return self._requirement(
            "paper_equity_curve",
            "pass",
            "Paper equity curve 已生成，可追踪跨日净值和回撤",
            {
                "current_equity": curve.get("current_equity"),
                "current_drawdown_pct": curve.get("current_drawdown_pct"),
                "max_drawdown_pct": curve.get("max_drawdown_pct"),
                "point_count": curve.get("point_count"),
            },
        )

    def _journal_and_review(self, run_date: str) -> dict:
        decisions = load_json(self.output_root / "journal_decisions" / f"{run_date}.json")
        pending = load_json(self.output_root / "journal_pending" / f"{run_date}.json")
        review = self.output_root / "review_notes" / f"{run_date}.md"
        journal = self.output_root / "journals" / f"{run_date}.md"
        strategy_review = load_json(self.output_root / "strategy_reviews" / f"{run_date}.json")
        strategy_snapshot = load_json(self.output_root / "strategy_snapshots" / f"{run_date}.json")
        learning_ledger = load_json(self.output_root / "learning_ledger" / f"{run_date}.json")
        strategy_proposal = load_json(self.output_root / "strategy_change_proposals" / f"{run_date}.json")
        report = self.output_root / "reports" / f"{run_date}.md"
        missing = [str(path) for path in [review, report, journal] if not path.exists()]
        if missing or not strategy_review or not strategy_snapshot or not learning_ledger or not strategy_proposal:
            return self._requirement(
                "journal_and_review",
                "fail",
                "Trading Journal、复盘笔记或学习闭环产物不完整",
                {
                    "missing": missing,
                    "strategy_review_rows": len(strategy_review),
                    "strategy_snapshot_rows": len(strategy_snapshot),
                    "learning_ledger_rows": len(learning_ledger),
                    "strategy_proposal_rows": len(strategy_proposal),
                },
            )
        return self._requirement(
            "journal_and_review",
            "pass",
            "每日 Trading Journal、报告、策略复盘和学习闭环存在",
            {
                "decisions": len(decisions),
                "pending": len(pending),
                "journal": str(journal),
                "review_notes": str(review),
                "report": str(report),
                "strategy_review_rows": len(strategy_review),
                "strategy_config_hash": (strategy_snapshot[-1] if strategy_snapshot else {}).get("config_hash", ""),
                "learning_ledger_rows": len(learning_ledger),
                "strategy_proposal_rows": len(strategy_proposal),
                "learning_state": (learning_ledger[-1] if learning_ledger else {}).get("learning_state", ""),
                "strategy_proposal_status": (strategy_proposal[-1] if strategy_proposal else {}).get("status", ""),
            },
        )

    def _strategy_guardrails(self, run_date: str) -> dict:
        rows = load_json(self.output_root / "strategy_guardrails" / f"{run_date}.json")
        guardrails = rows[-1] if rows else {}
        if not guardrails:
            return self._requirement("strategy_guardrails", "fail", "缺少由复盘生成的次日策略 guardrails", {"path": str(self.output_root / "strategy_guardrails" / f"{run_date}.json")})
        if guardrails.get("status") in {"pass", "warn", "block"} and "allow_new_paper_order" in guardrails:
            return self._requirement(
                "strategy_guardrails",
                "pass",
                "每日复盘已转化为下一轮 paper 交易 guardrails",
                {
                    "status": guardrails.get("status"),
                    "allow_new_paper_order": guardrails.get("allow_new_paper_order"),
                    "summary": guardrails.get("summary", {}),
                },
            )
        return self._requirement("strategy_guardrails", "fail", "strategy guardrails 产物结构不完整", guardrails)

    def _human_bias_ledger(self, run_date: str) -> dict:
        ledger = BiasLedger(self.output_root, self.market_db)
        if not ledger.ledger_path.exists():
            return self._requirement(
                "human_bias_ledger",
                "fail",
                "人轨方向分裁决账本不存在",
                {"ledger": str(ledger.ledger_path), "run_date": run_date},
            )
        if not ledger.summary_path.exists():
            return self._requirement(
                "human_bias_ledger",
                "fail",
                "人轨方向分 summary 缺失",
                {"ledger": str(ledger.ledger_path), "summary": str(ledger.summary_path)},
            )
        events = ledger.events()
        summary = self._load_mapping(ledger.summary_path)
        if int(summary.get("ledger_event_count", -1)) != len(events):
            return self._requirement(
                "human_bias_ledger",
                "fail",
                "人轨方向分 summary 不是最新派生结果",
                {
                    "ledger": str(ledger.ledger_path),
                    "summary": str(ledger.summary_path),
                    "summary_event_count": summary.get("ledger_event_count"),
                    "ledger_event_count": len(events),
                },
            )
        blockers = ledger.overdue_blockers()
        if blockers:
            return self._requirement(
                "human_bias_ledger",
                "fail",
                "存在已过期但未裁决的人轨方向分",
                {
                    "run_date": run_date,
                    "overdue_blockers": [
                        {
                            "view_id": item.get("view_id"),
                            "status": item.get("status"),
                            "pending_reason": item.get("pending_reason"),
                            "expires_at": item.get("expires_at"),
                        }
                        for item in blockers
                    ],
                },
            )
        return self._requirement(
            "human_bias_ledger",
            "pass",
            "人轨方向分账本存在，summary 新鲜，且没有 overdue-open/pending 条目",
            {
                "run_date": run_date,
                "ledger": str(ledger.ledger_path),
                "summary": str(ledger.summary_path),
                "total": summary.get("total"),
                "adjudicated": summary.get("adjudicated"),
                "pending_data": summary.get("pending_data"),
                "hit_rate": summary.get("hit_rate"),
                "mean_brier": summary.get("mean_brier"),
            },
        )

    def _daily_review_run(self, run_date: str) -> dict:
        profile = profile_from_config(self.config)
        if is_focus_profile(profile):
            return self._requirement(
                "daily_review_run",
                "warn",
                "parked_by_focus_mode: 每日复盘 runner 已按聚焦模式停放",
                {
                    "profile": profile,
                    "restore_path": "schedule.profile: full",
                    "command": f"python3 -m pipelines.daily_review --date {run_date}",
                },
            )
        rows = load_json(self.output_root / "daily_review_runs" / f"{run_date}.json")
        receipt = rows[-1] if rows else {}
        if not receipt:
            return self._requirement(
                "daily_review_run",
                "warn",
                "每日复盘 runner 尚未留下运行回执",
                {"command": f"python3 -m pipelines.daily_review --date {run_date}"},
            )
        if receipt.get("status") == "running":
            return self._requirement("daily_review_run", "warn", "每日复盘 runner 正在运行或上次未完成", receipt)
        required_artifacts = ["report", "journal", "review_notes", "performance", "paper_trade_attribution"]
        missing = [name for name in required_artifacts if not (receipt.get("artifacts") or {}).get(name)]
        if missing:
            return self._requirement("daily_review_run", "fail", "每日复盘 runner 回执不完整或失败", {"missing": missing, "receipt": receipt})
        if receipt.get("status") == "fail":
            return self._requirement("daily_review_run", "warn", "每日复盘 runner 已生成必要产物，但上次回执状态为 fail；需用本轮审计结果刷新", {"missing": missing, "receipt": receipt})
        return self._requirement(
            "daily_review_run",
            "pass",
            "每日复盘 runner 已生成 report、journal、review 和 performance 回执",
            {
                "status": receipt.get("status"),
                "started_at": receipt.get("started_at"),
                "finished_at": receipt.get("finished_at"),
                "summary": receipt.get("summary", {}),
                "artifacts": {key: (receipt.get("artifacts") or {}).get(key) for key in required_artifacts},
            },
        )

    def _schedule_artifacts(self, run_date: str) -> dict:
        rows = load_json(self.output_root / "schedules" / "current.json")
        schedule = rows[-1] if rows else {}
        schedule_status = ScheduleStatus(self.output_root).run(run_date)
        profile = profile_from_schedule(schedule, self.config)
        required = labels_for_profile(profile)
        if not schedule:
            return self._requirement(
                "schedule_artifacts",
                "warn",
                "本地 runner/plan/review/dashboard 调度配置尚未生成",
                {"command": "python3 -m pipelines.schedule"},
            )
        jobs = {item.get("label"): item for item in schedule.get("jobs", [])}
        missing = [label for label in required if label not in jobs]
        schedule_evidence = {
            "profile": profile,
            "required_labels": required,
            "generated_at": schedule.get("generated_at"),
            "launch_agents_dir": schedule.get("launch_agents_dir"),
            "jobs": schedule.get("jobs", []),
            "schedule_status": schedule_status,
        }
        if missing:
            return self._requirement("schedule_artifacts", "fail", "本地调度配置缺少必要任务", {"missing": missing, **schedule_evidence})
        if schedule_status.get("status") not in {"active", "installed"}:
            return self._requirement(
                "schedule_artifacts",
                "warn",
                "本地 launchd 调度配置已生成，但尚未安装/加载，不能证明每天自动运行",
                schedule_evidence,
            )
        return self._requirement(
            "schedule_artifacts",
            "pass",
            f"本地 launchd 调度配置已按 {profile} profile 生成并安装",
            schedule_evidence,
        )

    def _dualtrack_cycle_liveness(self, run_date: str, as_of: str | datetime | None = None) -> dict:
        heartbeat = DualTrackCycleHeartbeat(self.output_root).run(as_of=as_of)
        if heartbeat.get("status") in {"fresh", "not_scheduled"}:
            return self._requirement(
                "dualtrack_cycle_liveness",
                "pass",
                "DualTrack close-cycle 产物存活心跳正常或未启用双轨调度",
                heartbeat,
            )
        return self._requirement(
            "dualtrack_cycle_liveness",
            "fail",
            "DualTrack close-cycle 产物过期或缺失",
            {
                "run_date": run_date,
                "status": heartbeat.get("status"),
                "reason": heartbeat.get("reason"),
                "expected_boundary": heartbeat.get("expected_boundary"),
                "latest_artifact_at": heartbeat.get("latest_artifact_at"),
                "missed_boundaries": heartbeat.get("missed_boundaries", []),
                "close_grace_minutes": heartbeat.get("close_grace_minutes"),
            },
        )

    def _dashboard(self) -> dict:
        dashboard = ROOT / "dashboard-v4.html"
        server = ROOT / "pipelines" / "dashboard_server.py"
        if not dashboard.exists() or not server.exists():
            return self._requirement("dashboard", "fail", "Dashboard 文件或服务入口缺失", {"dashboard": str(dashboard), "server": str(server)})
        return self._requirement("dashboard", "pass", "实时 Dashboard 页面和 API 服务入口存在", {"dashboard": str(dashboard), "server": str(server), "url": "http://127.0.0.1:8765/dashboard-v4.html"})

    def _live_broker_boundary(self) -> dict:
        preflight_rows = load_json(self.output_root / "broker_preflight" / "current.json")
        latest = preflight_rows[-1] if preflight_rows else {}
        mode = str(self.config.get("execution_mode", "paper")).lower()
        live_enabled = bool(self.config.get("live_trading_enabled", False))
        if mode == "paper" and not live_enabled:
            return self._requirement(
                "live_broker_boundary",
                "warn",
                "当前是 paper/mock 交易；真实 broker 未启用，实盘对接只验证到受保护边界",
                {"execution_mode": mode, "live_trading_enabled": live_enabled, "broker_preflight": latest},
            )
        if latest.get("ready"):
            return self._requirement("live_broker_boundary", "pass", "broker preflight 通过", {"execution_mode": mode, "live_trading_enabled": live_enabled, "broker_preflight": latest})
        return self._requirement("live_broker_boundary", "fail", "broker preflight 未通过", {"execution_mode": mode, "live_trading_enabled": live_enabled, "broker_preflight": latest})

    def _live_activation_gate(self, run_date: str) -> dict:
        activation = LiveActivationGate(self.output_root).run(run_date)
        if activation.get("real_money_ready"):
            return self._requirement("live_activation_gate", "pass", "live activation gate 已允许真钱执行", activation)
        if activation.get("dry_run_ready"):
            return self._requirement("live_activation_gate", "warn", "live dry-run 已准备好，但真钱执行仍需 live readiness 和人工 approval", activation)
        return self._requirement("live_activation_gate", "warn", "live activation gate 仍阻止真钱执行", activation)

    def _live_cutover_package(self, run_date: str) -> dict:
        LiveReadiness(self.output_root, self.market_db).run(run_date)
        LiveActivationGate(self.output_root).run(run_date)
        LiveSwitchPlan(self.output_root).run(run_date)
        package = LiveCutoverPackage(self.output_root, self.market_db).run(run_date)
        evidence = {
            "status": package.get("status"),
            "live_ready": package.get("live_ready"),
            "dry_run_ready": package.get("dry_run_ready"),
            "real_money_ready": package.get("real_money_ready"),
            "blocker_count": len(package.get("blockers", [])),
            "evidence_paths": package.get("evidence_paths", {}),
        }
        if package.get("status") == "real_money_ready":
            return self._requirement("live_cutover_package", "pass", "Live cutover package 已证明真钱切换条件满足", evidence)
        if package.get("status") in {"blocked", "dry_run_ready"}:
            return self._requirement("live_cutover_package", "pass", "Live cutover package 已生成，真钱切换缺口和回滚边界可审计", evidence)
        return self._requirement("live_cutover_package", "fail", "Live cutover package 状态异常或缺失", evidence)

    def _oanda_broker_boundary(self, run_date: str) -> dict:
        feed_rows = load_json(self.output_root / "oanda_feed" / "current.json")
        feed = feed_rows[-1] if feed_rows else {}
        account = OandaAccountPreflight(output_root=self.output_root).run(run_date)
        preflight = LiveBrokerAdapter(
            self.output_root,
            live_trading_enabled=True,
            broker_config={
                "provider": "oanda_rest",
                "dry_run": True,
                "allowed_symbols": ["GOLD", "XAUUSD"],
            },
        ).preflight()
        evidence = {
            "feed": feed,
            "account_preflight": account,
            "dry_run_preflight": preflight,
            "live_submission_requires": ["execution_mode=live", "live_trading_enabled=true", "broker.provider=oanda_rest", "broker.dry_run=false", "OANDA_API_TOKEN", "OANDA_ACCOUNT_ID"],
        }
        if not preflight.get("ready"):
            return self._requirement("oanda_broker_boundary", "fail", "OANDA broker dry-run preflight 不可用", evidence)
        if account.get("status") == "pass" and account.get("instrument_ready") and feed and feed.get("status") == "pass":
            return self._requirement("oanda_broker_boundary", "pass", "OANDA 账号、品种、行情与下单 dry-run 边界可验证", evidence)
        if feed and feed.get("status") == "pass":
            return self._requirement("oanda_broker_boundary", "warn", "OANDA 行情与下单 dry-run 边界可验证；账号/品种只读预检尚未通过", evidence)
        return self._requirement("oanda_broker_boundary", "warn", "OANDA 下单 dry-run 边界可验证；当前缺少官方 OANDA 行情凭据或导入证据", evidence)

    def _live_submission_safety(self, run_date: str) -> dict:
        smoke = LiveSubmissionSafetySmoke(self.output_root).run(run_date)
        evidence = {
            "status": smoke.get("status"),
            "blocked_by_activation_gate": smoke.get("blocked_by_activation_gate"),
            "dry_run": smoke.get("dry_run"),
            "network_call_attempted": smoke.get("network_call_attempted"),
            "error": smoke.get("error"),
        }
        if smoke.get("status") == "pass" and smoke.get("blocked_by_activation_gate") and not smoke.get("network_call_attempted"):
            return self._requirement("live_submission_safety", "pass", "未获 live_activation.real_money_ready 时，真实 broker submission 会被 adapter 底层阻止", evidence)
        return self._requirement("live_submission_safety", "fail", "真实 broker submission 安全 smoke 未能证明 activation gate 生效", evidence)

    def _broker_bridge_smoke(self, run_date: str) -> dict:
        smoke_rows = load_json(self.output_root / "mt5_bridge_smoke" / "current.json")
        smoke = smoke_rows[-1] if smoke_rows else {}
        receipts = load_json(self.output_root / "broker_receipts" / "current.json")
        if not smoke:
            return self._requirement("broker_bridge_smoke", "warn", "MT5 bridge smoke 尚未运行", {"command": f"python3 -m pipelines.mt5_bridge_smoke --date {run_date}"})
        if smoke.get("status") != "pass":
            return self._requirement("broker_bridge_smoke", "fail", "MT5 bridge smoke 未通过", smoke)
        matching_receipt = next((item for item in receipts if item.get("order_id") == (smoke.get("order") or {}).get("order_id")), {})
        if not matching_receipt and smoke.get("has_imported_receipt"):
            return self._requirement("broker_bridge_smoke", "pass", "MT5 file bridge smoke 自带导入回执证据", {"smoke": smoke, "receipt_summary": smoke.get("receipt_summary", {})})
        if not matching_receipt:
            return self._requirement("broker_bridge_smoke", "warn", "MT5 bridge smoke 有 outbox 但 broker_receipts 当前汇总未找到对应回执", {"smoke": smoke, "receipt_count": len(receipts)})
        return self._requirement("broker_bridge_smoke", "pass", "MT5 file bridge 请求和回执导入闭环已验证", {"smoke": smoke, "matching_receipt": matching_receipt})

    def _broker_feed_smoke(self, run_date: str) -> dict:
        smoke_rows = load_json(self.output_root / "broker_feed_smoke" / "current.json")
        smoke = smoke_rows[-1] if smoke_rows else {}
        if not smoke:
            return self._requirement("broker_feed_smoke", "warn", "broker/MT5 行情 CSV 导入 smoke 尚未运行", {"command": f"python3 -m pipelines.broker_feed_smoke --date {run_date}"})
        if smoke.get("status") != "pass":
            return self._requirement("broker_feed_smoke", "fail", "broker/MT5 行情 CSV 导入 smoke 未通过", smoke)
        preflight = smoke.get("preflight") or {}
        if not preflight.get("ready_for_live"):
            return self._requirement("broker_feed_smoke", "fail", "smoke 导入完成但正式行情 preflight 未通过", smoke)
        return self._requirement(
            "broker_feed_smoke",
            "pass",
            "broker/MT5 GOLD 5m CSV 行情导入和 live-ready preflight 已验证",
            {
                "imported_rows": (smoke.get("import_summary") or {}).get("imported_rows"),
                "latest_provider": preflight.get("latest_provider"),
                "official_rows": preflight.get("official_rows"),
                "sandbox_db": smoke.get("sandbox_db"),
            },
        )

    def _rollup(self, requirements: list[dict]) -> str:
        states = {item["status"] for item in requirements}
        if "fail" in states:
            return "fail"
        if "warn" in states:
            return "warn"
        return "pass"

    def _requirement(self, name: str, status: str, summary: str, evidence: dict) -> dict:
        return {"name": name, "status": status, "summary": summary, "evidence": evidence}

    def _load_mapping(self, path: Path) -> dict:
        if not path.exists():
            return {}
        import json

        return json.loads(path.read_text(encoding="utf-8"))
