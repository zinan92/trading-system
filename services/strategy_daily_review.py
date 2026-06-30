from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from services.config_loader import load_strategy_config
from services.journal_store import load_json, write_json


class StrategyDailyReview:
    def __init__(self, output_root: Path) -> None:
        self.output_root = Path(output_root)
        self.strategy_config = load_strategy_config()

    def build(self, run_date: str, leaderboard: dict | None = None, frequency: dict | None = None) -> dict:
        leaderboard = leaderboard or self._latest("strategy_leaderboard")
        frequency = frequency or self._latest("strategy_frequency")
        by_leader = {
            row.get("strategy_id"): row
            for row in (leaderboard.get("strategies", []) if isinstance(leaderboard, dict) else [])
            if isinstance(row, dict)
        }
        by_frequency = {
            row.get("strategy_id"): row
            for row in (frequency.get("strategies", []) if isinstance(frequency, dict) else [])
            if isinstance(row, dict)
        }
        strategy_ids = self._strategy_ids(by_leader, by_frequency)
        rows = [
            self._review_one(run_date, strategy_id, by_leader.get(strategy_id, {}), by_frequency.get(strategy_id, {}))
            for strategy_id in strategy_ids
        ]
        payload = {
            "schema_version": "strategy-daily-review-v1",
            "run_date": run_date,
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "strategy_count": len(rows),
            "status_counts": self._status_counts(rows),
            "strategies": rows,
        }
        write_json(self.output_root / "strategy_daily_reviews" / f"{run_date}.json", [payload])
        write_json(self.output_root / "strategy_daily_reviews" / "current.json", [payload])
        md = self._markdown(payload)
        md_path = self.output_root / "strategy_daily_reviews" / f"{run_date}.md"
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(md, encoding="utf-8")
        return payload

    def _review_one(self, run_date: str, strategy_id: str, leaderboard_row: dict, frequency_row: dict) -> dict:
        namespace = self.output_root / "strategies" / strategy_id
        config = self.strategy_config.get(strategy_id, {}) if isinstance(self.strategy_config, dict) else {}
        classification = config.get("classification") or leaderboard_row.get("classification") or frequency_row.get("classification") or {}
        signals = load_json(namespace / "signals" / f"{run_date}.json")
        tickets = load_json(namespace / "trade_tickets" / f"{run_date}.json")
        orders = load_json(namespace / "paper_orders" / f"{run_date}.json")
        demo_orders = load_json(namespace / "demo_order_requests" / f"{run_date}.json")
        risk_blocks = load_json(namespace / "risk_blocks" / f"{run_date}.json")
        snapshots = load_json(namespace / "decision_snapshots" / f"{run_date}.json")
        bias_decisions = load_json(namespace / "direction_bias_decisions" / f"{run_date}.json")
        closed_today = load_json(namespace / "paper_trades" / "closed" / f"{run_date}.json")
        open_trades = load_json(namespace / "paper_trades" / "current.json")
        expected_min = int(classification.get("expected_trades_per_day_min") or 1)
        executed = int((leaderboard_row.get("daily_execution") or {}).get("executed_trade_count") or len(orders) + len(demo_orders))
        realized_today = round(sum(float(item.get("realized_pnl", 0) or 0) for item in closed_today), 4)
        closed_count = len(closed_today)
        open_count = len(open_trades)
        attribution = self._attribution(
            expected_min=expected_min,
            executed=executed,
            signals=signals,
            tickets=tickets,
            risk_blocks=risk_blocks,
            snapshots=snapshots,
            bias_decisions=bias_decisions,
            closed_today=closed_today,
            realized_today=realized_today,
        )
        verdict = self._verdict(attribution, realized_today, closed_count, executed, expected_min, leaderboard_row)
        cursor = self._review_cursor(attribution, closed_today, risk_blocks, snapshots)
        replay_date = self._date_from_cursor(cursor) or run_date
        pm_action = self._pm_action(verdict, attribution)
        review_trade = self._review_trade(attribution, closed_today)
        replay_context = {
            "strategy_id": strategy_id,
            "cursor": cursor,
            "reason": attribution.get("primary", ""),
            "action": pm_action,
            "artifact_path": str(namespace / "decision_snapshots" / f"{replay_date}.json"),
        }
        if review_trade:
            replay_context["review_trade"] = review_trade
        return {
            "strategy_id": strategy_id,
            "family": classification.get("family", "unknown"),
            "style": classification.get("style", "unknown"),
            "timeframe": config.get("timeframe", leaderboard_row.get("timeframe", "5m")),
            "classification": classification,
            "frequency": {
                "expected_min": expected_min,
                "expected_max": int(classification.get("expected_trades_per_day_max") or expected_min),
                "executed_today": executed,
                "signal_count": len(signals),
                "directional_signal_count": sum(1 for item in signals if item.get("direction") in {"long", "short"}),
                "ticket_count": len(tickets),
                "risk_block_count": len(risk_blocks),
                "frequency_stage": frequency_row.get("stage", ""),
                "frequency_reason": frequency_row.get("reason", ""),
            },
            "pnl": {
                "realized_today": realized_today,
                "closed_today": closed_count,
                "open_trades": open_count,
                "return_pct": leaderboard_row.get("return_pct"),
                "current_equity": leaderboard_row.get("current_equity"),
                "win_rate": leaderboard_row.get("win_rate"),
                "profit_factor": leaderboard_row.get("profit_factor"),
            },
            "tp_sl": self._tp_sl(closed_today, tickets),
            "direction_bias": self._bias_summary(bias_decisions, snapshots),
            "attribution": attribution,
            "pm_verdict": verdict,
            "pm_action": pm_action,
            "pm_summary": self._pm_summary(verdict, attribution, executed, expected_min),
            "review_priority": self._review_priority(verdict, attribution, executed, expected_min),
            "review_priority_label": self._review_priority_label(verdict, attribution),
            "review_question": self._review_question(verdict, attribution),
            "next_review_cursor": cursor,
            "replay_context": replay_context,
            "sample_quality": {
                "closed_trades_all": int(leaderboard_row.get("closed_trades") or 0),
                "closed_trades_today": closed_count,
                "thin_sample": int(leaderboard_row.get("closed_trades") or 0) < 20,
                "realized_only_for_edge": True,
            },
            "evidence": {
                "signals": str(namespace / "signals" / f"{run_date}.json"),
                "tickets": str(namespace / "trade_tickets" / f"{run_date}.json"),
                "orders": str(namespace / "paper_orders" / f"{run_date}.json"),
                "closed_trades": str(namespace / "paper_trades" / "closed" / f"{run_date}.json"),
                "decision_snapshots": str(namespace / "decision_snapshots" / f"{run_date}.json"),
            },
        }

    def _attribution(
        self,
        expected_min: int,
        executed: int,
        signals: list[dict],
        tickets: list[dict],
        risk_blocks: list[dict],
        snapshots: list[dict],
        bias_decisions: list[dict],
        closed_today: list[dict],
        realized_today: float,
    ) -> dict:
        reasons: list[str] = []
        if executed < expected_min:
            reasons.append("frequency_below_expected")
        if not signals:
            reasons.append("system_no_signal_artifact")
        elif not any(item.get("direction") in {"long", "short"} for item in signals):
            reasons.append("market_no_signal")
        if any(item.get("action") == "block" for item in bias_decisions):
            reasons.append("park_bias_filtered")
        if any("position map" in str(item.get("reason", "")) for item in risk_blocks):
            reasons.append("position_gate_filtered")
        if risk_blocks:
            reasons.append("risk_or_quality_block")
        if tickets and not executed:
            reasons.append("ticket_without_execution")
        if closed_today and realized_today < 0:
            reasons.append("loss_day")
        if any(item.get("exit_reason") == "stop_loss" for item in closed_today):
            reasons.append("stop_loss_hit")
        if any(item.get("exit_reason") in {"target", "take_profit"} for item in closed_today):
            reasons.append("take_profit_hit")
        if snapshots and not tickets and not risk_blocks:
            reasons.append("candidate_rejected_before_ticket")
        primary = self._primary_attribution(reasons)
        return {
            "primary": primary,
            "reasons": reasons or ["normal"],
        }

    def _primary_attribution(self, reasons: list[str]) -> str:
        if not reasons:
            return "normal"
        priority = [
            "stop_loss_hit",
            "loss_day",
            "risk_or_quality_block",
            "ticket_without_execution",
            "position_gate_filtered",
            "park_bias_filtered",
            "candidate_rejected_before_ticket",
            "market_no_signal",
            "system_no_signal_artifact",
            "frequency_below_expected",
            "take_profit_hit",
        ]
        for item in priority:
            if item in reasons:
                return item
        return reasons[0]

    def _tp_sl(self, closed_today: list[dict], tickets: list[dict]) -> dict:
        stop_hits = sum(1 for item in closed_today if item.get("exit_reason") == "stop_loss")
        target_hits = sum(1 for item in closed_today if item.get("exit_reason") in {"target", "take_profit"})
        planned = [
            {
                "ticket_id": item.get("ticket_id"),
                "entry_zone": item.get("entry_zone"),
                "stop_loss": item.get("stop_loss"),
                "take_profit": (item.get("targets") or [None])[0],
                "target_equity_return_pct": (item.get("trade_quality") or {}).get("target_equity_return_pct"),
                "reward_to_risk": (item.get("trade_quality") or {}).get("reward_to_risk"),
            }
            for item in tickets
        ]
        return {
            "planned_ticket_count": len(planned),
            "stop_loss_hits": stop_hits,
            "take_profit_hits": target_hits,
            "open_question": self._tp_sl_question(stop_hits, target_hits, closed_today),
            "planned": planned,
        }

    def _tp_sl_question(self, stop_hits: int, target_hits: int, closed_today: list[dict]) -> str:
        if stop_hits and not target_hits:
            return "检查 SL 是否太近，或方向/位置是否错误。"
        if target_hits:
            return "检查 TP 是否合理，以及是否应该让利润继续奔跑。"
        if closed_today:
            return "检查出场原因是否来自时间、手动或系统规则。"
        return "无已平仓样本，等待后续 trade lifecycle。"

    def _bias_summary(self, bias_decisions: list[dict], snapshots: list[dict]) -> dict:
        actions = {}
        for item in bias_decisions:
            actions[item.get("action", "unknown")] = actions.get(item.get("action", "unknown"), 0) + 1
        if not actions:
            for snap in snapshots:
                action = ((snap.get("direction_bias") or {}).get("action") or "unknown")
                actions[action] = actions.get(action, 0) + 1
        return {
            "decision_count": sum(actions.values()),
            "action_counts": actions,
            "latest_bias": (bias_decisions[-1].get("direction_bias") if bias_decisions else ""),
            "latest_score": (bias_decisions[-1].get("direction_score") if bias_decisions else None),
        }

    def _verdict(self, attribution: dict, realized_today: float, closed_count: int, executed: int, expected_min: int, leaderboard_row: dict) -> str:
        closed_all = int(leaderboard_row.get("closed_trades") or 0)
        if closed_all < 20 and realized_today > 0:
            return "watch_thin_positive"
        if closed_count and realized_today < 0:
            return "loss_driver_review"
        if attribution["primary"] in {"risk_or_quality_block", "ticket_without_execution"} or "risk_or_quality_block" in attribution.get("reasons", []):
            return "ops_or_risk_review"
        if executed < expected_min:
            return "low_frequency_review"
        return "continue_collecting_evidence"

    def _review_cursor(self, attribution: dict, closed_today: list[dict], risk_blocks: list[dict], snapshots: list[dict]) -> str:
        reasons = set(attribution.get("reasons", []))
        if {"loss_day", "stop_loss_hit", "take_profit_hit"} & reasons:
            for trade in closed_today:
                cursor = self._timestamp_from(trade, ["opened_at", "entry_timestamp", "filled_at", "closed_at"])
                if cursor:
                    return cursor
        if risk_blocks:
            for block in reversed(risk_blocks):
                cursor = self._timestamp_from(block, ["bar_timestamp", "timestamp", "generated_at", "created_at"])
                if cursor:
                    return cursor
        for snap in reversed(snapshots):
            signal = snap.get("signal") or {}
            if signal.get("direction") in {"long", "short"} or snap.get("final_decision") in {"go", "blocked", "candidate"}:
                cursor = self._timestamp_from(snap, ["bar_timestamp", "timestamp", "generated_at"])
                if cursor:
                    return cursor
        for snap in reversed(snapshots):
            cursor = self._timestamp_from(snap, ["bar_timestamp", "timestamp", "generated_at"])
            if cursor:
                return cursor
        return ""

    def _review_trade(self, attribution: dict, closed_today: list[dict]) -> dict:
        reasons = set(attribution.get("reasons", []))
        if not closed_today or not ({"loss_day", "stop_loss_hit", "take_profit_hit"} & reasons):
            return {}
        trade = self._primary_review_trade(closed_today, reasons)
        if not trade:
            return {}
        take_profit = trade.get("take_profit")
        if take_profit is None:
            take_profit = trade.get("target")
        if take_profit is None:
            take_profit = trade.get("target_price")
        return {
            "trade_id": trade.get("trade_id", ""),
            "ticket_id": trade.get("ticket_id", ""),
            "strategy_id": trade.get("strategy_id", ""),
            "side": trade.get("side", ""),
            "outcome_status": trade.get("status") or "closed",
            "replay_anchor": "entry",
            "future_outcome_hidden_until_exit": True,
            "opened_at": self._timestamp_from(trade, ["opened_at", "entry_timestamp", "filled_at"]),
            "closed_at": self._timestamp_from(trade, ["closed_at", "exit_timestamp"]),
            "exit_reason": trade.get("exit_reason", ""),
            "entry_price": trade.get("entry_price"),
            "exit_price": trade.get("exit_price"),
            "stop_loss": trade.get("stop_loss"),
            "take_profit": take_profit,
            "realized_pnl": trade.get("realized_pnl"),
            "review_note": "Replay starts at the entry candle; the closed outcome is PM task context, not future chart data.",
        }

    def _primary_review_trade(self, closed_today: list[dict], reasons: set[str]) -> dict:
        if "stop_loss_hit" in reasons:
            for trade in closed_today:
                if trade.get("exit_reason") == "stop_loss":
                    return trade
        if "take_profit_hit" in reasons:
            for trade in closed_today:
                if trade.get("exit_reason") in {"target", "take_profit"}:
                    return trade
        if "loss_day" in reasons:
            losses = [
                trade for trade in closed_today
                if self._safe_float(trade.get("realized_pnl")) is not None and self._safe_float(trade.get("realized_pnl")) < 0
            ]
            if losses:
                return sorted(losses, key=lambda item: self._safe_float(item.get("realized_pnl")) or 0)[0]
        return closed_today[0]

    def _timestamp_from(self, row: dict, keys: list[str]) -> str:
        if not isinstance(row, dict):
            return ""
        for key in keys:
            value = row.get(key)
            if value:
                return str(value)
        return ""

    def _safe_float(self, value) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _date_from_cursor(self, cursor: str) -> str:
        value = str(cursor or "")
        if len(value) >= 10 and value[4] == "-" and value[7] == "-":
            return value[:10]
        return ""

    def _pm_action(self, verdict: str, attribution: dict) -> str:
        primary = attribution.get("primary", "normal")
        reasons = set(attribution.get("reasons", []))
        if verdict == "loss_driver_review" or primary in {"loss_day", "stop_loss_hit"}:
            return "review_loss_driver"
        if "risk_or_quality_block" in reasons or primary == "risk_or_quality_block":
            return "inspect_risk_or_quality_block"
        if primary in {"ticket_without_execution", "candidate_rejected_before_ticket"}:
            return "inspect_signal_to_ticket"
        if primary in {"position_gate_filtered", "park_bias_filtered"}:
            return "review_filter_block"
        if primary in {"market_no_signal", "system_no_signal_artifact"}:
            return "validate_no_signal"
        if verdict == "watch_thin_positive":
            return "continue_collecting_thin_positive"
        return "continue_collecting"

    def _pm_summary(self, verdict: str, attribution: dict, executed: int, expected_min: int) -> str:
        primary = attribution.get("primary", "normal")
        reasons = set(attribution.get("reasons", []))
        if verdict == "loss_driver_review" or primary in {"loss_day", "stop_loss_hit"}:
            return "今天亏损且/或触发止损，优先复盘方向、入场位置、SL 是否过近，以及 TP 是否离场前过远。"
        if primary == "risk_or_quality_block" or "risk_or_quality_block" in reasons:
            return "有机会但被风险/质量门挡住，先确认阻断原因是否合理；不要把它误判成没有信号。"
        if primary in {"ticket_without_execution", "candidate_rejected_before_ticket"}:
            return "有方向信号但未形成可执行订单，优先检查质量门、位置门、1% TP/RR、风控门和 broker 条件。"
        if primary in {"position_gate_filtered", "park_bias_filtered"}:
            return "信号被位置或人工方向过滤器拦住，复盘过滤是否符合当日大方向和关键位置。"
        if primary in {"market_no_signal", "system_no_signal_artifact"}:
            return "策略按节奏评估但没有 Go；这是可解释的 No-Go 样本，不算策略失效。"
        if executed < expected_min:
            return "真实成交低于观察目标；先确认是市场淡、门槛过严，还是信号到出票链路断点。"
        if verdict == "watch_thin_positive":
            return "有正向表现但样本不足，只能继续观察，不能晋级或加权。"
        return "已有样本，继续收集；不基于薄样本做晋级、降权或参数变更。"

    def _review_question(self, verdict: str, attribution: dict) -> str:
        primary = attribution.get("primary", "normal")
        if verdict == "loss_driver_review" or primary in {"loss_day", "stop_loss_hit"}:
            return "这笔止损是方向错、位置错、入场太早，还是 SL 太近？"
        if primary == "risk_or_quality_block":
            return "风控/质量门挡住的是坏交易，还是错过了应做的机会？"
        if primary in {"ticket_without_execution", "candidate_rejected_before_ticket"}:
            return "方向信号为什么没有变成 ticket：TP/RR 不够、位置不对、强度不够，还是执行门阻断？"
        if primary in {"position_gate_filtered", "park_bias_filtered"}:
            return "过滤器是否真的提高了质量，还是过滤掉了今天应该做的方向？"
        if primary in {"market_no_signal", "system_no_signal_artifact"}:
            return "这一天无交易是否符合策略设计，还是策略在当前波动环境下太迟钝？"
        return "今天的样本能否提供可行动的策略改进假设？"

    def _review_priority(self, verdict: str, attribution: dict, executed: int, expected_min: int) -> int:
        primary = attribution.get("primary", "normal")
        if verdict == "loss_driver_review" or primary in {"loss_day", "stop_loss_hit"}:
            return 95
        if primary == "risk_or_quality_block":
            return 85
        if primary in {"ticket_without_execution", "candidate_rejected_before_ticket"}:
            return 75
        if primary in {"position_gate_filtered", "park_bias_filtered"}:
            return 70
        if executed < expected_min:
            return 60
        if verdict == "watch_thin_positive":
            return 45
        return 25

    def _review_priority_label(self, verdict: str, attribution: dict) -> str:
        primary = attribution.get("primary", "normal")
        if verdict == "loss_driver_review" or primary in {"loss_day", "stop_loss_hit"}:
            return "高优先级：亏损/止损归因"
        if primary == "risk_or_quality_block":
            return "高优先级：风控/质量门复核"
        if primary in {"ticket_without_execution", "candidate_rejected_before_ticket"}:
            return "中高优先级：信号到出票漏斗"
        if primary in {"position_gate_filtered", "park_bias_filtered"}:
            return "中高优先级：过滤器复核"
        if primary in {"market_no_signal", "system_no_signal_artifact"}:
            return "观察：无合格信号"
        return "观察：继续收集样本"

    def _strategy_ids(self, by_leader: dict, by_frequency: dict) -> list[str]:
        ids: list[str] = []
        for source in (self.strategy_config, by_leader, by_frequency):
            for strategy_id in source:
                if strategy_id not in ids:
                    ids.append(strategy_id)
        strategies_dir = self.output_root / "strategies"
        if strategies_dir.exists():
            for namespace in sorted(path for path in strategies_dir.iterdir() if path.is_dir()):
                if namespace.name not in ids:
                    ids.append(namespace.name)
        return ids

    def _status_counts(self, rows: list[dict]) -> dict:
        out: dict[str, int] = {}
        for row in rows:
            verdict = str(row.get("pm_verdict", "unknown"))
            out[verdict] = out.get(verdict, 0) + 1
        return out

    def _latest(self, name: str) -> dict:
        rows = load_json(self.output_root / name / "current.json")
        return rows[-1] if rows else {}

    def _markdown(self, payload: dict) -> str:
        lines = [
            f"# Strategy Daily Review - {payload['run_date']}",
            "",
            f"- Strategy count: {payload['strategy_count']}",
            f"- Verdict counts: {payload['status_counts']}",
            "",
            "| Strategy | Family | Trades | PnL | Verdict | PM Action | Primary Attribution | PM Question |",
            "|---|---:|---:|---:|---|---|---|---|",
        ]
        for row in payload["strategies"]:
            lines.append(
                "| {strategy} | {family} | {trades} | {pnl} | {verdict} | {action} | {attr} | {question} |".format(
                    strategy=row["strategy_id"],
                    family=row["family"],
                    trades=row["frequency"]["executed_today"],
                    pnl=row["pnl"]["realized_today"],
                    verdict=row["pm_verdict"],
                    action=row.get("pm_action", ""),
                    attr=row["attribution"]["primary"],
                    question=row.get("review_question", row["tp_sl"]["open_question"]),
                )
            )
        lines.append("")
        return "\n".join(lines)
