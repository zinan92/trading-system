from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from services.config_loader import load_pipeline_config, load_strategy_config
from services.feishu_report_sender import FeishuReportSender, resolve_report_sender
from services.journal_store import load_json, write_json


KIND = "trade_ticket_open"
DEFAULT_MAX_CHARS = 2200


class TradeTicketNotifier:
    """Send trade-ticket review cards to the report Feishu channel.

    This is intentionally a report-channel flow, not an alert-channel flow. It is
    for human trade review: entry reason, entry zone, TP/SL, risk, and evidence.
    """

    def __init__(self, output_root: Path, sender=None) -> None:
        self.output_root = Path(output_root)
        self.sender = sender
        self.strategy_config = load_strategy_config()

    def notify_namespace(self, run_date: str, strategy_id: str, strategy_output_root: Path, force: bool = False) -> dict:
        strategy_root = Path(strategy_output_root)
        tickets = load_json(strategy_root / "trade_tickets" / f"{run_date}.json")
        return self.notify_tickets(run_date, strategy_id, strategy_root, tickets, force=force)

    def notify_tickets(self, run_date: str, strategy_id: str, strategy_root: Path, tickets: Iterable[dict], force: bool = False) -> dict:
        ticket_rows = [item for item in tickets if isinstance(item, dict) and item.get("ticket_id")]
        if not ticket_rows:
            return {"status": "no_tickets", "run_date": run_date, "strategy_id": strategy_id, "sent": 0, "skipped": 0}
        if not self._enabled():
            return {"status": "disabled", "run_date": run_date, "strategy_id": strategy_id, "sent": 0, "skipped": len(ticket_rows)}
        if os.getenv("PYTEST_CURRENT_TEST") and self.sender is None:
            return {"status": "pytest_skipped", "run_date": run_date, "strategy_id": strategy_id, "sent": 0, "skipped": len(ticket_rows)}

        sender = self.sender if self.sender is not None else resolve_report_sender()
        configured = bool(getattr(sender, "configured", False))
        notifications_path = self.output_root / "trade_ticket_notifications" / f"{run_date}.json"
        rows = load_json(notifications_path)
        delivered_keys = {
            self._key(row.get("strategy_id", ""), row.get("ticket_id", ""))
            for row in rows
            if row.get("delivered") is True
        }
        existing_rows = [row for row in rows if isinstance(row, dict)]
        pending_by_ticket = self._by_ticket(strategy_root / "journal_pending" / f"{run_date}.json")
        decisions_by_ticket = self._by_ticket(strategy_root / "journal_decisions" / f"{run_date}.json")
        paper_orders_by_ticket = self._by_ticket(strategy_root / "paper_orders" / f"{run_date}.json")
        demo_requests_by_ticket = self._demo_requests_by_ticket(strategy_root / "demo_order_requests" / f"{run_date}.json")

        sent = 0
        skipped = 0
        failed = 0
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        for ticket in ticket_rows:
            ticket_id = str(ticket.get("ticket_id") or "")
            key = self._key(strategy_id, ticket_id)
            if key in delivered_keys and not force:
                skipped += 1
                continue
            context = {
                "pending": pending_by_ticket.get(ticket_id, {}),
                "decision": decisions_by_ticket.get(ticket_id, {}),
                "paper_order": paper_orders_by_ticket.get(ticket_id, {}),
                "demo_request": demo_requests_by_ticket.get(ticket_id, {}),
                "strategy_root": str(strategy_root),
            }
            title = f"黄金开单审查卡｜{self._strategy_title(strategy_id)}"
            message = self._format_message(run_date, strategy_id, ticket, context)
            if configured:
                result = FeishuReportSender(self.output_root, sender=sender).run(
                    run_date=run_date,
                    kind=KIND,
                    title=title,
                    message=message,
                    max_chars=DEFAULT_MAX_CHARS,
                )
            else:
                result = {
                    "run_date": run_date,
                    "kind": KIND,
                    "title": title,
                    "status": "fail",
                    "delivered": False,
                    "channel": "log",
                    "delivery": {"ok": False, "channel": "log", "reason": "report_feishu_not_configured"},
                }
            delivered = bool(result.get("delivered"))
            existing_rows = [
                row
                for row in existing_rows
                if self._key(row.get("strategy_id", ""), row.get("ticket_id", "")) != key
            ]
            existing_rows.append(
                {
                    "run_date": run_date,
                    "strategy_id": strategy_id,
                    "ticket_id": ticket_id,
                    "notified_at": now,
                    "status": "pass" if delivered else "fail",
                    "delivered": delivered,
                    "channel": result.get("channel", ""),
                    "title": title,
                    "delivery": result.get("delivery", {}),
                }
            )
            if delivered:
                sent += 1
                delivered_keys.add(key)
            else:
                failed += 1
        write_json(notifications_path, existing_rows)
        write_json(self.output_root / "trade_ticket_notifications" / "current.json", existing_rows[-1:] if existing_rows else [])
        status = "pass" if sent and not failed else "fail" if failed else "noop"
        return {"status": status, "run_date": run_date, "strategy_id": strategy_id, "sent": sent, "skipped": skipped, "failed": failed}

    def _enabled(self) -> bool:
        if os.getenv("TRADING_ORCHESTRATOR_DISABLE_TRADE_TICKET_FEISHU"):
            return False
        config = load_pipeline_config().get("trade_ticket_notifications", {})
        if isinstance(config, dict) and config.get("enabled") is False:
            return False
        return True

    def _format_message(self, run_date: str, strategy_id: str, ticket: dict, context: dict) -> str:
        trade_quality = ticket.get("trade_quality") if isinstance(ticket.get("trade_quality"), dict) else {}
        backtest = ticket.get("backtest") if isinstance(ticket.get("backtest"), dict) else {}
        classification = self._classification(strategy_id)
        pending = context.get("pending", {}) if isinstance(context.get("pending"), dict) else {}
        decision = context.get("decision", {}) if isinstance(context.get("decision"), dict) else {}
        paper_order = context.get("paper_order", {}) if isinstance(context.get("paper_order"), dict) else {}
        demo_request = context.get("demo_request", {}) if isinstance(context.get("demo_request"), dict) else {}
        execution_status = self._execution_status(pending, decision, paper_order, demo_request)
        lines = [
            "黄金开单审查卡",
            f"- 日期：{run_date}",
            f"- 策略：{self._strategy_title(strategy_id)}",
            f"- 策略 ID：{strategy_id}",
            f"- 开单追溯编号：{ticket.get('ticket_id', '')}",
            f"- 当前状态：{execution_status}",
            "",
            "1. 发现信号",
            f"- 信号编号：{ticket.get('signal_id', '')}",
            f"- 标的与方向：{ticket.get('asset', '')} / {self._action_label(ticket)}",
            f"- 原始触发：{self._text_zh(ticket.get('trigger'))}",
            f"- 开单理由：{self._text_zh(ticket.get('rationale') or ticket.get('trigger'))}",
            "",
            "2. 策略类别",
            f"- 类型：{self._classification_label(classification)}",
            f"- 交易节奏：{self._translate(classification.get('frequency_bucket'))}；持仓周期：{self._translate(classification.get('holding_period'))}",
            f"- 策略角色：{self._translate(classification.get('role'))}；风险画像：{self._translate(classification.get('risk_profile'))}",
            "",
            "3. 过滤检查",
            f"- 机器过滤：开单质量 {'通过' if trade_quality.get('passes') is True else '未通过或缺失'}；系统结论：{self._translate(ticket.get('verdict') or 'unknown')}",
            f"- 过滤方法：{self._join_zh(ticket.get('methods', []))}",
            f"- 过滤原因：{self._join_zh(trade_quality.get('reasons', []))}",
            f"- 人工过滤：{'需要人工确认' if ticket.get('manual_execution_required', True) else '不要求人工确认'}；{'只允许纸面交易' if ticket.get('paper_only', True) else '允许进入配置的执行路径'}",
            f"- 反方理由：{self._text_zh(ticket.get('counter_rationale'))}",
            "",
            "4. 盈亏比检查",
            f"- 入场区间：{ticket.get('entry_zone', '')}",
            f"- 估算开单价：{self._entry_midpoint(ticket.get('entry_zone', ''))}",
            f"- 止盈：{self._targets(ticket.get('targets', []))}",
            f"- 止损：{ticket.get('stop_loss', '')}",
            f"- 盈亏比：{self._fmt(trade_quality.get('reward_to_risk'))}；目标收益：{self._fmt(trade_quality.get('target_equity_return_pct'))}%；止损风险：{self._fmt(trade_quality.get('stop_equity_risk_pct'))}%",
            "",
            "5. 仓位与订单计划",
            f"- 仓位比例：{ticket.get('position_size_pct', '')}%；最大亏损：{ticket.get('max_loss_pct', '')}%",
            f"- 订单类型：{self._translate(ticket.get('order_type') or 'limit')}；有效期：{self._translate(ticket.get('time_in_force') or 'day')}",
            f"- 实际/请求成交：{self._actual_order_text(paper_order, demo_request)}",
            "",
            "6. 出场与失效条件",
            f"- 正常出场：到达止盈或止损。",
            f"- 失效条件：{self._text_zh(ticket.get('invalid_if'))}",
            "- 后续操作：开仓后必须确认止盈/止损保护覆盖整笔仓位；进入 demo/live 时还要确认交易所侧保护单存在。",
            "",
            "7. 回测与证据",
            f"- 回测结论：{self._translate(backtest.get('verdict') or 'unknown')}；胜率：{self._fmt(backtest.get('win_rate'))}；平均 R：{self._fmt(backtest.get('avg_r'))}",
            f"- 证据文件：{context.get('strategy_root', '')}/trade_tickets/{run_date}.json",
            "",
            "8. 人工核对项",
            "- 方向是否和当前人工观点/机器过滤一致。",
            "- 入场价到止损的距离是否真的对应最大亏损。",
            "- 止盈空间是否覆盖成本，并且盈亏比达标。",
            "- 如果是 demo/live，先确认对账与保护单，再允许继续。",
        ]
        return "\n".join(lines)

    def _execution_status(self, pending: dict, decision: dict, paper_order: dict, demo_request: dict) -> str:
        if demo_request:
            receipt = demo_request.get("receipt", {}) if isinstance(demo_request.get("receipt"), dict) else {}
            status = receipt.get("status") or demo_request.get("status") or "requested"
            return f"Demo 请求：{self._translate(status)}"
        if paper_order:
            return f"纸面订单：{self._translate(paper_order.get('status', 'ordered'))}"
        if decision:
            return self._translate(decision.get("decision_status") or "decided")
        if pending:
            return self._translate(pending.get("decision_status") or "pending_manual_decision")
        return "已生成开单票，等待审查"

    def _actual_order_text(self, paper_order: dict, demo_request: dict) -> str:
        if paper_order:
            price = paper_order.get("fill_price") or paper_order.get("requested_price")
            return f"价格 {price}，数量 {paper_order.get('quantity')}，状态 {self._translate(paper_order.get('status'))}"
        if demo_request:
            receipt = demo_request.get("receipt", {}) if isinstance(demo_request.get("receipt"), dict) else {}
            price = receipt.get("fill_price") or receipt.get("requested_price")
            return f"价格 {price or '未成交'}，数量 {receipt.get('quantity') or '未知'}，状态 {self._translate(receipt.get('status') or demo_request.get('status'))}"
        return "尚未进入订单执行"

    def _direction(self, ticket: dict) -> str:
        action = str(ticket.get("action", "")).lower()
        if "buy" in action or action == "long":
            return "做多"
        if "sell" in action or action == "short":
            return "做空"
        return "方向未知"

    def _action_label(self, ticket: dict) -> str:
        action = str(ticket.get("action", "")).lower()
        if action in {"prepare_buy", "buy", "long"}:
            return "准备做多"
        if action in {"prepare_sell", "sell", "short"}:
            return "准备做空"
        return self._direction(ticket)

    def _classification(self, strategy_id: str) -> dict:
        config = self.strategy_config.get(strategy_id, {}) if isinstance(self.strategy_config, dict) else {}
        classification = config.get("classification", {}) if isinstance(config, dict) else {}
        return classification if isinstance(classification, dict) else {}

    def _classification_label(self, classification: dict) -> str:
        family = classification.get("family_label") or self._translate(classification.get("family")) or "未分类"
        style = classification.get("style_label") or self._translate(classification.get("style")) or "未标注形态"
        directionality = self._translate(classification.get("directionality"))
        return f"{family} / {style} / {directionality}"

    def _strategy_title(self, strategy_id: str) -> str:
        classification = self._classification(strategy_id)
        family = classification.get("family_label") or self._translate(classification.get("family"))
        style = classification.get("style_label") or self._translate(classification.get("style"))
        if family and style and family != "无" and style != "无":
            return f"{family}｜{style}"
        return strategy_id

    def _entry_midpoint(self, entry_zone) -> str:
        text = str(entry_zone or "").strip()
        numbers = [float(item) for item in re.findall(r"\d+(?:\.\d+)?", text)]
        if len(numbers) >= 2:
            return f"{(numbers[0] + numbers[1]) / 2:.4f}"
        if len(numbers) == 1:
            return f"{numbers[0]:.4f}"
        return "n/a"

    def _targets(self, targets) -> str:
        if isinstance(targets, (list, tuple)):
            return "，".join(str(item) for item in targets) or "无"
        return str(targets or "无")

    def _join_zh(self, values) -> str:
        if isinstance(values, (list, tuple)):
            return "；".join(self._translate(item) for item in values) or "无"
        if isinstance(values, dict):
            return "；".join(f"{self._translate(key)}={self._translate(value)}" for key, value in values.items()) or "无"
        return self._translate(values) or "无"

    def _text_zh(self, value) -> str:
        text = str(value or "").strip()
        if not text:
            return "无"
        exact = {
            "Review GOLD if signal remains above strength/confidence thresholds.": "如果信号强度和置信度仍高于阈值，则审查黄金。",
            "MACD golden cross with volatility filter": "MACD 金叉，并通过波动过滤。",
            "Momentum aligned with the current range break.": "动量与当前区间突破方向一致。",
            "Market view may be stale.": "人工市场观点可能已经过期。",
            "Signal should be ignored if price confirmation fails, macro pressure reverses, or event risk dominates.": "如果价格确认失败、宏观压力反转，或事件风险主导，则忽略该信号。",
        }
        if text in exact:
            return exact[text]
        review_match = re.match(r"Review ([A-Z0-9_]+) if signal remains above strength/confidence thresholds\.", text)
        if review_match:
            return f"如果信号强度和置信度仍高于阈值，则审查{self._asset_label(review_match.group(1))}。"
        close_below_match = re.match(r"price closes below ([0-9]+(?:\.[0-9]+)?)", text, flags=re.IGNORECASE)
        if close_below_match:
            return f"价格收在 {close_below_match.group(1)} 下方。"
        regime_match = re.match(r"([A-Z0-9_]+) is best interpreted through ([^;]+); current regime is ([A-Za-z0-9_]+)\.", text)
        if regime_match:
            asset = self._asset_label(regime_match.group(1))
            framework = self._framework_label(regime_match.group(2))
            regime = self._translate(regime_match.group(3))
            return f"当前用{framework}框架解读{asset}；当前信号状态为 {regime}。"
        return self._translate(text)

    def _asset_label(self, value: str) -> str:
        return {"GOLD": "黄金", "XAUUSD": "黄金"}.get(str(value or "").upper(), str(value or ""))

    def _framework_label(self, value: str) -> str:
        parts = [part.strip() for part in re.split(r"[,/]+", str(value or "")) if part.strip()]
        return "和".join(self._translate(part) for part in parts) or "未知"

    def _fmt(self, value) -> str:
        if value in (None, ""):
            return "无"
        try:
            number = float(value)
        except (TypeError, ValueError):
            return str(value)
        return f"{number:.4g}"

    def _translate(self, value) -> str:
        text = str(value or "").strip()
        if not text:
            return "无"
        mapping = {
            "long_short": "多空都可",
            "long": "做多",
            "short": "做空",
            "prepare_buy": "准备做多",
            "prepare_sell": "准备做空",
            "medium": "中等",
            "medium_high": "中高",
            "low": "低",
            "high": "高",
            "low_to_medium": "低到中等",
            "scalp_to_intraday": "短线到日内",
            "intraday": "日内",
            "intraday_to_swing": "日内到波段",
            "momentum_baseline": "动量基线",
            "sample_volume_strategy": "样本量策略",
            "active_position_gated_strategy": "主动策略，带位置门控",
            "shadow_momentum_quality_filter": "影子动量质量过滤",
            "shadow_variant": "影子变体",
            "legacy_ungated_baseline": "旧版未门控基线",
            "momentum": "动量",
            "trend_macro": "趋势/宏观",
            "mean_reversion": "均值回归",
            "breakout": "突破",
            "grid": "网格",
            "chan": "缠论",
            "fib": "斐波那契",
            "position": "位置",
            "approved": "已通过",
            "rejected": "已拒绝",
            "thin": "样本薄",
            "insufficient_sample": "样本不足",
            "supportive": "支持性",
            "pass": "通过",
            "warn": "注意",
            "fail": "失败",
            "unknown": "未知",
            "filled": "已成交",
            "ordered": "已下单",
            "requested": "已请求",
            "blocked": "已阻断",
            "decided": "已决策",
            "pending_manual_decision": "等待人工确认",
            "executed_paper": "已执行纸面订单",
            "executed": "已执行",
            "skipped": "已跳过",
            "limit": "限价单",
            "market": "市价单",
            "day": "当日有效",
            "gtc": "撤销前有效",
            "macd": "MACD",
            "macd_death_cross": "MACD 死叉",
            "macd_golden_cross": "MACD 金叉",
            "macd_trend_volatility_filter": "MACD 趋势/波动过滤",
            "bollinger_reversion": "布林带回归",
            "range_breakout": "区间突破",
            "grid_mean_reversion": "网格均值回归",
            "ema50_bounce": "EMA50 反弹",
            "ema50_rejection": "EMA50 压制/拒绝",
            "trend_following": "趋势跟随",
            "trend-following": "趋势跟随",
            "macro-liquidity": "宏观流动性",
            "volatility_filter": "波动过滤",
            "risk-management": "风险管理",
            "no-trade": "不开仓过滤",
        }
        return mapping.get(text, text.replace("_", " "))

    def _by_ticket(self, path: Path) -> dict[str, dict]:
        rows = load_json(path)
        result = {}
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict) and row.get("ticket_id"):
                result[str(row["ticket_id"])] = row
        return result

    def _demo_requests_by_ticket(self, path: Path) -> dict[str, dict]:
        rows = load_json(path)
        result = {}
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            ticket = row.get("ticket", {}) if isinstance(row.get("ticket"), dict) else {}
            ticket_id = str(ticket.get("ticket_id") or row.get("ticket_id") or "")
            if ticket_id:
                result[ticket_id] = row
        return result

    def _key(self, strategy_id: str, ticket_id: str) -> str:
        return f"{strategy_id}:{ticket_id}"
