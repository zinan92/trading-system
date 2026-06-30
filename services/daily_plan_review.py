from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from services.config_loader import ROOT, load_pipeline_config, load_risk_rules, load_strategy_config
from services.journal_store import load_json, write_json
from services.position_map import GoldPositionMap
from services.trade_quality import DailyTradeSampler, TradeQualityGate


def _chan_profile(strategy_id: str, strategy_config: dict) -> dict:
    timeframe = str(strategy_config.get("timeframe", "5m"))
    engine = str(strategy_config.get("engine", "ma"))
    return {
        "profile_id": f"{strategy_id}_daily_loop",
        "source_repo": "zinan92/chancode" if engine == "chan" else "local_strategy",
        "source_basis": "Chan theory BSP signal loop plus controlled daily planning/review loop",
        "current_engine": strategy_id,
        "integration_status": "active_strategy_from_demo_trading_config",
        "timeframe": timeframe,
        "notes": [
            "Freqtrade 只作为设计参考，不作为运行时依赖或 sidecar。",
            "早盘计划、盘中执行、晚盘复盘统一锚定 demo_trading.active_strategy_id。",
        ],
    }


class DailyPlanReview:
    def __init__(self, output_root: Path | None = None) -> None:
        config = load_pipeline_config()
        self.config = config
        self.output_root = output_root or Path(os.getenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", str(ROOT / config.get("output_root", "outputs"))))

    def morning_plan(self, run_date: str) -> dict:
        strategy_id = self._active_strategy_id()
        strategy_config = self._strategy_config(strategy_id)
        timeframe = str(strategy_config.get("timeframe", "5m"))
        profile = _chan_profile(strategy_id, strategy_config)

        signals = self._with_position_context(
            self._load_rows("signals", run_date, strategy_id),
            run_date,
            strategy_id,
        )
        tickets = self._with_position_context(
            self._load_rows("trade_tickets", run_date, strategy_id),
            run_date,
            strategy_id,
        )
        risk_monitor = self._latest_rows("risk_monitor", strategy_id)
        data_source = self._latest_rows("data_source_preflight", strategy_id)
        broker = self._latest_rows("broker_preflight", strategy_id)
        binance_feed = self._latest_rows("binance_usdm_feed", strategy_id)
        live_readiness = self._latest_rows("live_readiness", strategy_id)
        live_activation = self._latest_rows("live_activation", strategy_id)
        previous_review = self._previous_evening_review(run_date, strategy_id)

        position_service = GoldPositionMap(self.output_root)
        position_map = position_service.build(run_date, strategy_id)
        primary_signal = self._primary_signal(signals)
        signal_gate = position_service.gate_signal(primary_signal, position_map)
        position_reference = self._position_reference(position_map, signal_gate)
        signals = [self._attach_position(item, position_reference, signal_gate if item is primary_signal else None) for item in signals]
        tickets = [self._attach_position(item, position_reference, None) for item in tickets]

        strategy_config_all = load_strategy_config()
        risk_rules = load_risk_rules()
        trade_plan = self._trade_plan(
            signals,
            tickets,
            data_source,
            broker,
            risk_monitor,
            live_readiness,
            live_activation,
            signal_gate,
            timeframe,
        )
        execution_playbook = self._execution_playbook(
            timeframe=timeframe,
            position_map=position_map,
            signal_gate=signal_gate,
            trade_plan=trade_plan,
            risk_rules=risk_rules,
            data_source=data_source,
            broker=broker,
        )
        decision = "TRADE_REVIEW" if trade_plan["allowed_to_trade"] else "NO_TRADE"
        payload = {
            "run_date": run_date,
            "generated_at": self._now(),
            "plan_type": "gold_trading_morning_plan",
            "active_strategy_id": strategy_id,
            "timeframe": timeframe,
            "status": trade_plan["status"],
            "mode": trade_plan["mode"],
            "decision": decision,
            "allowed_to_trade": trade_plan["allowed_to_trade"],
            "current_signal": signals[0] if signals else {},
            "blockers": trade_plan["blocks"],
            "strategy_profile": profile,
            "strategy_runtime": {
                "active_strategy_id": strategy_id,
                "timeframe": timeframe,
                "engine": strategy_config.get("engine", "ma"),
                "config": strategy_config_all.get(strategy_id, {}),
                "scoped_output_root": str(self._strategy_root(strategy_id)),
            },
            "position_map": position_map,
            "signal_gate": signal_gate,
            "previous_review": previous_review,
            "broker_plan": {
                "active_provider": broker.get("provider", self.config.get("broker", {}).get("provider", "manual_gateway")),
                "dry_run": broker.get("dry_run", self.config.get("broker", {}).get("dry_run", True)),
                "ready": broker.get("ready", False),
                "block_reason": broker.get("block_reason", ""),
                "binance_usdm_feed": binance_feed,
                "live_gate_context": {
                    "live_ready": live_readiness.get("live_ready", False),
                    "real_money_ready": live_activation.get("real_money_ready", False),
                },
            },
            "data_plan": {
                "ready_for_paper": data_source.get("ready_for_paper", False),
                "ready_for_live": data_source.get("ready_for_live", False),
                "latest_provider": data_source.get("latest_provider", ""),
                "latest_price": data_source.get("latest_price"),
                "message": data_source.get("message", ""),
            },
            "risk_envelope": {
                "max_loss_pct": risk_rules.get("default", {}).get("max_loss_pct", 0.5),
                "daily_loss_stop_pct": risk_rules.get("default", {}).get("daily_loss_stop_pct", 1.25),
                "max_open_trades": 1,
                "max_trades_today": 3,
                "allow_paper_auto_approve": risk_monitor.get("allow_paper_auto_approve", False),
                "kill_switch_active": risk_monitor.get("kill_switch_active", False),
            },
            "signals": signals,
            "tickets": tickets,
            "trade_plan": trade_plan,
            "execution_playbook": execution_playbook,
            "today_focus": self._today_focus(position_map, signal_gate, previous_review),
            "artifacts": {
                "json": str(self.output_root / "trading_plans" / f"{run_date}.json"),
                "markdown": str(self.output_root / "trading_plans" / f"{run_date}.md"),
                "position_map": str(self.output_root / "position_maps" / f"{run_date}.json"),
                "daily_trade_samples": str(self.output_root / "daily_trade_samples" / f"{run_date}.json"),
            },
        }
        write_json(self.output_root / "trading_plans" / "current.json", [payload])
        write_json(self.output_root / "trading_plans" / f"{run_date}.json", [payload])
        self._write_markdown(self.output_root / "trading_plans" / f"{run_date}.md", self._morning_markdown(payload))
        return payload

    def evening_review(self, run_date: str, notes: str = "") -> dict:
        strategy_id = self._active_strategy_id()
        strategy_config = self._strategy_config(strategy_id)
        profile = _chan_profile(strategy_id, strategy_config)

        decisions = self._load_rows("journal_decisions", run_date, strategy_id)
        pending = self._load_rows("journal_pending", run_date, strategy_id)
        orders = self._load_rows("paper_orders", run_date, strategy_id)
        demo_requests = self._load_rows(str((self.config.get("demo_trading", {}) or {}).get("request_dir", "demo_order_requests")), run_date, strategy_id)
        live_requests = self._load_rows("live_order_requests", run_date, strategy_id)
        performance = self._latest_rows("performance", strategy_id)
        strategy_review_rows = self._load_rows("strategy_reviews", run_date, strategy_id)
        plan = self._load_plan(run_date)
        position_map = self._load_position_map(run_date)
        adherence = self._adherence(decisions, pending, orders, live_requests, demo_requests)
        attribution = self._attribution(adherence, performance, plan, position_map)
        hypotheses = self._hypotheses(run_date, strategy_id, attribution, plan, performance, notes)
        improvement_queue = self._improvement_queue(adherence, performance, notes, attribution)
        daily_samples = DailyTradeSampler(self.output_root).build(run_date, active_strategy_id=strategy_id)
        trade_reviews = self._trade_reviews(run_date, strategy_id, plan, daily_samples, decisions, pending, orders, demo_requests, live_requests)
        payload = {
            "run_date": run_date,
            "generated_at": self._now(),
            "status": "review_ready",
            "active_strategy_id": strategy_id,
            "strategy_profile": profile,
            "strategy_runtime": {
                "active_strategy_id": strategy_id,
                "timeframe": strategy_config.get("timeframe", "5m"),
                "engine": strategy_config.get("engine", "ma"),
                "config": strategy_config,
                "scoped_output_root": str(self._strategy_root(strategy_id)),
            },
            "previous_plan": plan,
            "position_map": position_map,
            "adherence": adherence,
            "attribution": attribution,
            "daily_trade_samples": daily_samples,
            "trade_reviews": trade_reviews,
            "hypotheses": hypotheses,
            "decisions": decisions,
            "pending": pending,
            "paper_orders": orders,
            "demo_order_requests": demo_requests,
            "live_order_requests": live_requests,
            "performance": performance,
            "strategy_review": strategy_review_rows[-1] if strategy_review_rows else {},
            "improvement_queue": improvement_queue,
            "notes": notes,
            "artifacts": {
                "json": str(self.output_root / "evening_reviews" / f"{run_date}.json"),
                "markdown": str(self.output_root / "evening_reviews" / f"{run_date}.md"),
                "strategy_hypotheses": str(self.output_root / "strategy_hypotheses" / f"{run_date}.json"),
                "learning_ledger": str(self.output_root / "learning_ledger" / f"{run_date}.json"),
                "trade_reviews": str(self.output_root / "trade_reviews" / f"{run_date}.json"),
            },
        }
        write_json(self.output_root / "evening_reviews" / "current.json", [payload])
        write_json(self.output_root / "evening_reviews" / f"{run_date}.json", [payload])
        write_json(self.output_root / "strategy_hypotheses" / "current.json", hypotheses)
        write_json(self.output_root / "strategy_hypotheses" / f"{run_date}.json", hypotheses)
        write_json(self.output_root / "trade_reviews" / "current.json", [trade_reviews])
        write_json(self.output_root / "trade_reviews" / f"{run_date}.json", [trade_reviews])
        self._write_learning_ledger(run_date, strategy_id, attribution, hypotheses, performance)
        self._write_markdown(self.output_root / "evening_reviews" / f"{run_date}.md", self._evening_markdown(payload))
        return payload

    def _trade_plan(
        self,
        signals: list[dict],
        tickets: list[dict],
        data_source: dict,
        broker: dict,
        risk_monitor: dict,
        live_readiness: dict,
        live_activation: dict,
        signal_gate: dict,
        timeframe: str,
    ) -> dict:
        candidates = [item for item in signals if item.get("asset") == "GOLD" and item.get("direction") in {"long", "short"}]
        ready_for_paper = bool(data_source.get("ready_for_paper", False))
        live_possible = bool(data_source.get("ready_for_live") and broker.get("ready") and live_readiness.get("live_ready") and live_activation.get("real_money_ready"))
        mode = "live_ready" if live_possible and not broker.get("dry_run", True) else ("demo_or_paper" if ready_for_paper else "data_blocked")
        blocks = []
        if risk_monitor.get("kill_switch_active"):
            blocks.append("kill switch active")
        if not ready_for_paper:
            blocks.append(data_source.get("message", "market data not ready"))
        if not candidates:
            blocks.append("no actionable signal")
        elif not signal_gate.get("allow_candidate", False):
            blocks.append(f"position gate: {signal_gate.get('reason', 'blocked')}")
        if not tickets:
            blocks.append("no approved trade ticket")
        allowed_to_trade = bool(ready_for_paper and candidates and tickets and signal_gate.get("allow_candidate", False) and not risk_monitor.get("kill_switch_active"))
        return {
            "status": "ready_for_review" if allowed_to_trade else "no_trade",
            "mode": mode,
            "allowed_to_trade": allowed_to_trade,
            "primary_rule": f"只交易 active strategy GOLD/{timeframe}；小级别信号必须靠近 Position Map 关键位，否则降级为 watch。",
            "candidate_count": len(candidates),
            "ticket_count": len(tickets),
            "blocks": blocks,
            "setups": [
                {
                    "ticket_id": ticket.get("ticket_id", ""),
                    "asset": ticket.get("asset", ""),
                    "action": ticket.get("action", ""),
                    "entry_zone": ticket.get("entry_zone", ""),
                    "stop_loss": ticket.get("stop_loss"),
                    "targets": ticket.get("targets", []),
                    "max_loss_pct": ticket.get("max_loss_pct", 0),
                    "trade_quality": ticket.get("trade_quality", {}),
                    "position_context": ticket.get("position_context", {}),
                }
                for ticket in tickets[:3]
            ],
        }

    def _execution_playbook(
        self,
        *,
        timeframe: str,
        position_map: dict,
        signal_gate: dict,
        trade_plan: dict,
        risk_rules: dict,
        data_source: dict,
        broker: dict,
    ) -> dict:
        quality_gate = TradeQualityGate(risk_rules)
        return {
            "cadence": f"Evaluate on every closed {timeframe} GOLD candle.",
            "active_market": "GOLD/XAUUSDT",
            "quality_control": quality_gate.config.to_dict(),
            "current_context": {
                "latest_price": data_source.get("latest_price") or position_map.get("latest_price"),
                "position_location": position_map.get("location", "unknown"),
                "primary_timeframe": position_map.get("primary_timeframe", ""),
                "near_trade_level": (position_map.get("trade_zone") or {}).get("near_key_level", False),
                "signal_gate_direction": signal_gate.get("effective_direction", "watch"),
                "broker_ready": broker.get("ready", False),
            },
            "decision_tree": [
                {"step": 1, "name": "data_ready", "rule": "Skip if market data is not ready_for_paper/live."},
                {"step": 2, "name": "risk_ready", "rule": "Skip if kill switch is active or daily loss limits block new exposure."},
                {"step": 3, "name": "position_map", "rule": "Only promote small-timeframe triggers when higher-timeframe EMA/Fibonacci/range location allows it."},
                {"step": 4, "name": "signal_trigger", "rule": "For gold_1m_chan, require a fresh 1m Chan 二买/类二买 within configured fresh_bars."},
                {"step": 5, "name": "entry", "rule": "Create a ticket only after entry zone, stop loss, and take profit are all present."},
                {"step": 6, "name": "quality_gate", "rule": "Skip if take-profit does not imply at least 1% equity return at configured leverage or reward/risk is too low."},
                {"step": 7, "name": "execution_gate", "rule": "Demo order can be submitted only when the active demo account is flat and broker guard passes."},
                {"step": 8, "name": "review", "rule": "Every trigger, skip, ticket, order, and block must appear in evening trade_reviews."},
            ],
            "sample_plan": {
                "minimum_daily_samples": quality_gate.config.daily_min_trade_samples,
                "target_daily_samples": [quality_gate.config.daily_target_trade_samples_low, quality_gate.config.daily_target_trade_samples_high],
                "active_strategy_rule": "Do not force trades; use shadow/paper strategies to collect samples when active strategy has no qualified setup.",
            },
            "setup_count": trade_plan.get("ticket_count", 0),
        }

    def _trade_reviews(
        self,
        run_date: str,
        strategy_id: str,
        plan: dict,
        daily_samples: dict,
        decisions: list[dict],
        pending: list[dict],
        orders: list[dict],
        demo_requests: list[dict],
        live_requests: list[dict],
    ) -> dict:
        decision_by_ticket = {item.get("ticket_id"): item for item in decisions if item.get("ticket_id")}
        pending_by_ticket = {item.get("ticket_id"): item for item in pending if item.get("ticket_id")}
        paper_by_ticket = {item.get("ticket_id"): item for item in orders if item.get("ticket_id")}
        demo_by_ticket = {item.get("ticket_id"): item for item in demo_requests if item.get("ticket_id")}
        live_by_ticket = {item.get("ticket_id"): item for item in live_requests if item.get("ticket_id")}
        rows = []
        for sample in daily_samples.get("samples", []):
            ticket_id = sample.get("ticket_id", "")
            quality = sample.get("trade_quality") or {}
            tags = []
            if not sample.get("candidate"):
                tags.append("no_trigger")
            if sample.get("candidate") and not sample.get("ticket_created"):
                tags.append("candidate_without_ticket")
            if quality and not quality.get("passes", False):
                tags.append("quality_gate_failed")
            if sample.get("is_active_strategy") and plan.get("decision") == "NO_TRADE":
                tags.append("plan_no_trade")
            if ticket_id in pending_by_ticket:
                tags.append("pending_manual_review")
            if ticket_id in paper_by_ticket or ticket_id in demo_by_ticket or ticket_id in live_by_ticket:
                tags.append("executed_or_requested")
            rows.append(
                {
                    "sample_id": sample.get("sample_id"),
                    "run_date": run_date,
                    "strategy_id": sample.get("strategy_id"),
                    "is_active_strategy": sample.get("is_active_strategy", False),
                    "signal_id": sample.get("signal_id", ""),
                    "ticket_id": ticket_id,
                    "direction": sample.get("direction", ""),
                    "execution_status": sample.get("execution_status", ""),
                    "entry_zone": sample.get("entry_zone", ""),
                    "stop_loss": sample.get("stop_loss"),
                    "targets": sample.get("targets", []),
                    "trade_quality": quality,
                    "decision": decision_by_ticket.get(ticket_id, {}),
                    "paper_order": paper_by_ticket.get(ticket_id, {}),
                    "demo_order": demo_by_ticket.get(ticket_id, {}),
                    "live_request": live_by_ticket.get(ticket_id, {}),
                    "block_reasons": sample.get("block_reasons", []),
                    "review_tags": tags,
                    "review_summary": self._trade_review_summary(sample, quality, tags),
                }
            )
        summary = {
            "reviewed_samples": len(rows),
            "active_strategy_reviews": sum(1 for item in rows if item.get("is_active_strategy")),
            "quality_pass": sum(1 for item in rows if (item.get("trade_quality") or {}).get("passes")),
            "quality_failed": sum(1 for item in rows if "quality_gate_failed" in item.get("review_tags", [])),
            "executed_or_requested": sum(1 for item in rows if "executed_or_requested" in item.get("review_tags", [])),
            "candidate_without_ticket": sum(1 for item in rows if "candidate_without_ticket" in item.get("review_tags", [])),
            "sample_status": daily_samples.get("status"),
            "sample_summary": daily_samples.get("summary", {}),
        }
        return {
            "run_date": run_date,
            "generated_at": self._now(),
            "active_strategy_id": strategy_id,
            "status": "review_ready",
            "summary": summary,
            "reviews": rows,
        }

    def _trade_review_summary(self, sample: dict, quality: dict, tags: list[str]) -> str:
        if "no_trigger" in tags:
            return "No actionable trigger; keep as no-trade sample."
        if "quality_gate_failed" in tags:
            return f"Candidate rejected by TP/SL quality gate: {'; '.join(quality.get('reasons', []))}"
        if "candidate_without_ticket" in tags:
            reasons = sample.get("block_reasons") or []
            if reasons:
                return f"Actionable signal did not become a ticket: {'; '.join(reasons)}."
            return "Actionable signal did not become a ticket; inspect risk, position, and guardrail blocks."
        if "executed_or_requested" in tags:
            return "Trade was executed/requested; compare fill, TP, SL, and plan adherence."
        return "Candidate reviewed; no execution drift detected."

    def _adherence(self, decisions: list[dict], pending: list[dict], orders: list[dict], live_requests: list[dict], demo_requests: list[dict]) -> dict:
        executed = [item for item in decisions if item.get("decision_status") in {"executed", "executed_paper"}]
        rejected = [item for item in decisions if item.get("decision_status") == "rejected"]
        skipped = [item for item in decisions if item.get("decision_status") == "skipped"]
        return {
            "executed_count": len(executed),
            "rejected_count": len(rejected),
            "skipped_count": len(skipped),
            "pending_count": len(pending),
            "paper_order_count": len(orders),
            "demo_order_request_count": len(demo_requests),
            "live_request_count": len(live_requests),
            "strict_execution": len(pending) == 0 and len(live_requests) <= len(executed) + len(orders) + len(demo_requests),
        }

    def _attribution(self, adherence: dict, performance: dict, plan: dict, position_map: dict) -> dict:
        categories: list[dict] = []
        trade_plan = plan.get("trade_plan", {}) if isinstance(plan, dict) else {}
        signal_gate = plan.get("signal_gate", {}) if isinstance(plan, dict) else {}
        data_plan = plan.get("data_plan", {}) if isinstance(plan, dict) else {}
        if position_map.get("status") != "ready":
            categories.append({"key": "position_map_missing", "label": "位置判断缺失", "severity": "high", "evidence": position_map.get("status", "missing")})
        elif signal_gate and not signal_gate.get("allow_candidate", False) and signal_gate.get("raw_direction") in {"long", "short"}:
            categories.append({"key": "position_filter_blocked", "label": "不在关键位置附近", "severity": "normal", "evidence": signal_gate.get("reason", "")})
        if adherence["pending_count"]:
            categories.append({"key": "missed_trade", "label": "该交易没交易", "severity": "high", "evidence": {"pending_count": adherence["pending_count"]}})
        if adherence["paper_order_count"] + adherence["demo_order_request_count"] > 0 and trade_plan.get("allowed_to_trade") is False:
            categories.append({"key": "unnecessary_trade", "label": "不该交易却交易", "severity": "high", "evidence": {"orders": adherence["paper_order_count"], "demo_orders": adherence["demo_order_request_count"]}})
        if not data_plan.get("ready_for_paper", True):
            categories.append({"key": "data_broker_system_issue", "label": "数据/broker/系统问题", "severity": "high", "evidence": data_plan.get("message", "")})
        summary = performance.get("summary", {}) if isinstance(performance.get("summary"), dict) else {}
        pnl = float(summary.get("net_pnl_marked", summary.get("total_pnl", 0)) or 0)
        if pnl < 0:
            categories.append({"key": "risk_review", "label": "风控太紧/太松", "severity": "normal", "evidence": {"net_pnl": pnl}})
        if not categories:
            categories.append({"key": "valid_no_trade_or_execution", "label": "计划执行无明显偏差", "severity": "info", "evidence": {"decision": plan.get("decision", "unknown") if isinstance(plan, dict) else "unknown"}})
        return {
            "categories": categories,
            "primary": categories[0],
            "position_location": position_map.get("location", "unknown"),
            "nearest_level": (position_map.get("nearest_levels") or [{}])[0],
        }

    def _hypotheses(self, run_date: str, strategy_id: str, attribution: dict, plan: dict, performance: dict, notes: str) -> list[dict]:
        hypotheses = []
        for index, item in enumerate(attribution.get("categories", []), start=1):
            key = item.get("key", "review")
            hypotheses.append(
                {
                    "hypothesis_id": f"hyp_{run_date.replace('-', '')}_{index}_{key}",
                    "run_date": run_date,
                    "strategy_id": strategy_id,
                    "source": "evening_review",
                    "status": "shadow_only",
                    "auto_apply": False,
                    "category": key,
                    "thesis": self._hypothesis_thesis(key, item, plan, performance, notes),
                    "metrics_required": ["sample_size", "trigger_frequency", "win_rate", "pnl", "max_drawdown", "no_trade_quality"],
                    "promotion_rule": "Only propose a demo-active change after sufficient shadow samples and explicit promotion gate approval.",
                }
            )
        return hypotheses

    def _hypothesis_thesis(self, key: str, item: dict, plan: dict, performance: dict, notes: str) -> str:
        if key == "position_filter_blocked":
            return "Track whether rejected small-timeframe Chan triggers would have worked when price was not near EMA50/Fib/swing levels."
        if key == "missed_trade":
            return "Measure whether pending tickets are caused by trigger freshness, manual delay, or broker/demo gate friction."
        if key == "unnecessary_trade":
            return "Audit any order created while the morning plan said NO_TRADE; tighten execution gate if repeated."
        if key == "data_broker_system_issue":
            return "Separate strategy no-trade from system no-trade by hardening data freshness and broker preflight evidence."
        if key == "risk_review":
            return "Compare current stop/target/risk sizing against closed-trade PnL attribution before changing production risk."
        if notes:
            return f"Human review note should be tested in shadow mode before strategy changes: {notes}"
        return f"Collect more evidence for {item.get('label', key)} without changing production strategy."

    def _improvement_queue(self, adherence: dict, performance: dict, notes: str, attribution: dict) -> list[dict]:
        queue = []
        if adherence["pending_count"]:
            queue.append({"priority": "high", "item": "收盘前处理所有 pending ticket，避免信号没有闭环。"})
        if adherence["executed_count"] == 0 and adherence["paper_order_count"] == 0 and adherence["demo_order_request_count"] == 0:
            queue.append({"priority": "normal", "item": "今天没有执行成交，复盘重点放在是否过度过滤、信号不足或数据/风控阻塞。"})
        summary = performance.get("summary", {}) if isinstance(performance.get("summary"), dict) else {}
        if float(summary.get("net_pnl_marked", summary.get("total_pnl", 0)) or 0) < 0:
            queue.append({"priority": "high", "item": "亏损日必须检查是否按计划入场、止损和仓位。"})
        primary = attribution.get("primary", {})
        if primary:
            queue.append({"priority": "normal", "item": f"明早计划必须引用今晚主归因：{primary.get('label', primary.get('key'))}。"})
        if notes:
            queue.append({"priority": "normal", "item": "人工备注已记录，下一轮策略 review 需要引用。"})
        return queue

    def _write_learning_ledger(self, run_date: str, strategy_id: str, attribution: dict, hypotheses: list[dict], performance: dict) -> None:
        previous = self._latest_rows("learning_ledger", strategy_id)
        ledger = {
            **previous,
            "run_date": run_date,
            "generated_at": self._now(),
            "strategy_id": strategy_id,
            "learning_state": "review_logged",
            "auto_apply": False,
            "review_attribution": attribution,
            "latest_hypotheses": hypotheses,
            "performance_summary": performance.get("summary", {}) if isinstance(performance, dict) else {},
        }
        write_json(self.output_root / "learning_ledger" / "current.json", [ledger])
        write_json(self.output_root / "learning_ledger" / f"{run_date}.json", [ledger])

    def _active_strategy_id(self) -> str:
        demo = self.config.get("demo_trading", {}) or {}
        configured = str(demo.get("active_strategy_id", "")).strip()
        if configured:
            return configured
        strategy_config = load_strategy_config()
        for strategy_id, block in strategy_config.items():
            if isinstance(block, dict) and block.get("enabled", True):
                return strategy_id
        return "gold_5m_v1"

    def _strategy_config(self, strategy_id: str) -> dict:
        return dict(load_strategy_config().get(strategy_id, {}))

    def _strategy_root(self, strategy_id: str) -> Path:
        return self.output_root / "strategies" / strategy_id

    def _load_rows(self, folder: str, run_date: str, strategy_id: str) -> list[dict]:
        scoped = self._strategy_root(strategy_id) / folder / f"{run_date}.json"
        if scoped.exists():
            return load_json(scoped)
        return load_json(self.output_root / folder / f"{run_date}.json")

    def _latest_rows(self, folder: str, strategy_id: str) -> dict:
        for path in (
            self._strategy_root(strategy_id) / folder / "current.json",
            self.output_root / folder / "current.json",
        ):
            rows = load_json(path)
            if rows:
                return rows[-1]
        return {}

    def _load_plan(self, run_date: str) -> dict:
        rows = load_json(self.output_root / "trading_plans" / f"{run_date}.json")
        if not rows:
            rows = load_json(self.output_root / "trading_plans" / "current.json")
        return rows[-1] if rows else {}

    def _load_position_map(self, run_date: str) -> dict:
        rows = load_json(self.output_root / "position_maps" / f"{run_date}.json")
        if not rows:
            rows = load_json(self.output_root / "position_maps" / "current.json")
        return rows[-1] if rows else {}

    def _previous_evening_review(self, run_date: str, strategy_id: str) -> dict:
        for root in (self._strategy_root(strategy_id) / "evening_reviews", self.output_root / "evening_reviews"):
            if not root.exists():
                continue
            dated = sorted(path for path in root.glob("*.json") if path.stem != "current" and path.stem < run_date)
            for path in reversed(dated):
                rows = load_json(path)
                if rows:
                    return self._review_reference(rows[-1], str(path))
        rows = load_json(self.output_root / "evening_reviews" / "current.json")
        if rows and rows[-1].get("run_date") != run_date:
            return self._review_reference(rows[-1], str(self.output_root / "evening_reviews" / "current.json"))
        return {}

    def _review_reference(self, review: dict, path: str) -> dict:
        attribution = review.get("attribution", {}) if isinstance(review, dict) else {}
        primary = attribution.get("primary", {}) if isinstance(attribution, dict) else {}
        return {
            "run_date": review.get("run_date", ""),
            "status": review.get("status", ""),
            "primary_attribution": primary,
            "improvement_queue": review.get("improvement_queue", [])[:3],
            "artifact_path": path,
        }

    def _primary_signal(self, signals: list[dict]) -> dict:
        return next((item for item in signals if item.get("asset") == "GOLD"), signals[0] if signals else {})

    def _with_position_context(self, rows: list[dict], run_date: str, strategy_id: str) -> list[dict]:
        enriched = []
        for item in rows:
            artifacts = item.get("source_artifacts") or []
            enriched.append(
                dict(
                    item,
                    source_artifacts=[*artifacts, str(self.output_root / "position_maps" / f"{run_date}.json")],
                    strategy_id=item.get("strategy_id", strategy_id),
                )
            )
        return enriched

    def _attach_position(self, item: dict, position_reference: dict, signal_gate: dict | None) -> dict:
        out = dict(item)
        out["position_context"] = position_reference
        if signal_gate is not None:
            out["position_gate"] = signal_gate
        return out

    def _position_reference(self, position_map: dict, signal_gate: dict) -> dict:
        return {
            "latest_price": position_map.get("latest_price"),
            "location": position_map.get("location", "unknown"),
            "primary_timeframe": position_map.get("primary_timeframe", ""),
            "nearest_level": (position_map.get("nearest_levels") or [{}])[0],
            "bias": (position_map.get("trade_zone") or {}).get("bias", "neutral"),
            "gate_reason": signal_gate.get("reason", ""),
            "position_map_artifact": str(self.output_root / "position_maps" / f"{position_map.get('run_date', 'current')}.json"),
        }

    def _today_focus(self, position_map: dict, signal_gate: dict, previous_review: dict) -> list[str]:
        focus = [
            f"Position Map: {position_map.get('location', 'unknown')} zone, nearest level {(position_map.get('nearest_levels') or [{}])[0].get('name', '--')}",
            f"Signal gate: {signal_gate.get('effective_direction', 'watch')} ({signal_gate.get('reason', 'no reason')})",
        ]
        primary = previous_review.get("primary_attribution", {}) if isinstance(previous_review, dict) else {}
        if primary:
            focus.append(f"Carry-over from review: {primary.get('label', primary.get('key', 'review'))}")
        return focus

    def _now(self) -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    def _write_markdown(self, path: Path, body: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

    def _morning_markdown(self, payload: dict) -> str:
        plan = payload["trade_plan"]
        playbook = payload.get("execution_playbook", {})
        risk = payload["risk_envelope"]
        data = payload["data_plan"]
        broker = payload["broker_plan"]
        position = payload["position_map"]
        signal_gate = payload["signal_gate"]
        previous = payload.get("previous_review", {})
        lines = [
            f"# Morning Trading Plan - {payload['run_date']}",
            "",
            f"- Decision: {payload['decision']}",
            f"- Mode: {payload['mode']}",
            f"- Strategy: {payload['strategy_runtime']['active_strategy_id']} on {payload['strategy_runtime']['timeframe']}",
            f"- Data: {data.get('latest_provider') or '--'} price {data.get('latest_price')}",
            f"- Broker: {broker.get('active_provider')} dry_run={broker.get('dry_run')}",
            f"- Risk: {risk.get('max_loss_pct')}% per trade, {risk.get('daily_loss_stop_pct')}% daily stop, max {risk.get('max_trades_today')} trades",
            "",
            "## Position Map",
            "",
            f"- Location: {position.get('location', 'unknown')} on {position.get('primary_timeframe', '--')}",
            f"- Latest price: {position.get('latest_price')}",
            f"- Nearest level: {(position.get('nearest_levels') or [{}])[0]}",
            f"- Signal gate: {signal_gate.get('effective_direction', 'watch')} / {signal_gate.get('reason', '')}",
            "",
            "## Today",
            "",
            f"- Primary rule: {plan['primary_rule']}",
            f"- Candidate signals: {plan['candidate_count']}",
            f"- Tickets: {plan['ticket_count']}",
        ]
        quality = playbook.get("quality_control", {})
        lines.extend(
            [
                "",
                "## Execution Playbook",
                "",
                f"- Cadence: {playbook.get('cadence', 'n/a')}",
                f"- Quality gate: target equity return >= {quality.get('min_target_equity_return_pct')}%, target price move >= {quality.get('min_target_price_move_pct')}%, reward/risk >= {quality.get('min_reward_to_risk')}",
                f"- Sample target: at least {quality.get('daily_min_trade_samples')} samples, target {quality.get('daily_target_trade_samples_low')}-{quality.get('daily_target_trade_samples_high')}",
            ]
        )
        for item in playbook.get("decision_tree", []):
            lines.append(f"- Step {item.get('step')}: {item.get('name')} - {item.get('rule')}")
        if previous:
            lines.extend(["", "## Carry-over From Last Review", "", f"- {previous.get('run_date')}: {previous.get('primary_attribution', {})}"])
        if plan["blocks"]:
            lines.extend(["", "## Blocks", ""])
            lines.extend(f"- {item}" for item in plan["blocks"])
        if plan["setups"]:
            lines.extend(["", "## Setups", ""])
            lines.extend(
                f"- {item['ticket_id']}: {item['action']} {item['asset']} {item['entry_zone']} SL {item['stop_loss']} targets {item['targets']} quality={item.get('trade_quality', {}).get('passes')}"
                for item in plan["setups"]
            )
        return "\n".join(lines) + "\n"

    def _evening_markdown(self, payload: dict) -> str:
        adherence = payload["adherence"]
        lines = [
            f"# Evening Trading Review - {payload['run_date']}",
            "",
            f"- Strategy: {payload['strategy_runtime']['active_strategy_id']} on {payload['strategy_runtime']['timeframe']}",
            f"- Executed: {adherence['executed_count']}",
            f"- Rejected: {adherence['rejected_count']}",
            f"- Skipped: {adherence['skipped_count']}",
            f"- Pending: {adherence['pending_count']}",
            f"- Paper orders: {adherence['paper_order_count']}",
            f"- Demo orders: {adherence['demo_order_request_count']}",
            f"- Live requests: {adherence['live_request_count']}",
            f"- Strict execution: {adherence['strict_execution']}",
            "",
            "## Attribution",
            "",
        ]
        for item in payload["attribution"].get("categories", []):
            lines.append(f"- [{item.get('severity')}] {item.get('label')} ({item.get('key')}): {item.get('evidence')}")
        lines.extend(["", "## Shadow Hypotheses", ""])
        if payload["hypotheses"]:
            lines.extend(f"- {item['hypothesis_id']}: {item['thesis']}" for item in payload["hypotheses"])
        else:
            lines.append("- No hypothesis.")
        lines.extend(["", "## Improvement Queue", ""])
        if payload["improvement_queue"]:
            lines.extend(f"- [{item['priority']}] {item['item']}" for item in payload["improvement_queue"])
        else:
            lines.append("- No new improvement item.")
        if payload.get("notes"):
            lines.extend(["", "## Notes", "", payload["notes"]])
        trade_reviews = payload.get("trade_reviews", {})
        if trade_reviews:
            summary = trade_reviews.get("summary", {})
            lines.extend(
                [
                    "",
                    "## Trade-by-Trade Review",
                    "",
                    f"- Reviewed samples: {summary.get('reviewed_samples')}",
                    f"- Quality pass / fail: {summary.get('quality_pass')} / {summary.get('quality_failed')}",
                    f"- Executed/requested: {summary.get('executed_or_requested')}",
                    f"- Candidate without ticket: {summary.get('candidate_without_ticket')}",
                ]
            )
            for item in trade_reviews.get("reviews", [])[:8]:
                lines.append(
                    f"- {item.get('strategy_id')} {item.get('signal_id') or '--'} ticket={item.get('ticket_id') or '--'} status={item.get('execution_status')} tags={','.join(item.get('review_tags', [])) or 'none'}"
                )
        return "\n".join(lines) + "\n"


def run_morning_plan(run_date: str, output_root: Path | None = None) -> dict:
    return DailyPlanReview(output_root).morning_plan(run_date)


def run_evening_review(run_date: str, notes: str = "", output_root: Path | None = None) -> dict:
    return DailyPlanReview(output_root).evening_review(run_date, notes=notes)
