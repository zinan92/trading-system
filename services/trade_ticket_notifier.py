from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from services.config_loader import load_pipeline_config, load_risk_rules, load_strategy_config
from services.feishu_report_sender import FeishuReportSender, resolve_trade_sender
from services.journal_store import load_json, write_json
from services.trade_ticket_card import build_ticket_card


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

        sender = self.sender if self.sender is not None else resolve_trade_sender()
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
        total = len(ticket_rows)
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        for index, ticket in enumerate(ticket_rows):
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
            card = self._build_card(run_date, strategy_id, ticket, context, sequence=index + 1, total=total)
            if configured:
                result = FeishuReportSender(self.output_root, sender=sender).run(
                    run_date=run_date,
                    kind=KIND,
                    title=title,
                    message=message,
                    max_chars=DEFAULT_MAX_CHARS,
                    card=card,
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
        approval = self._approval_conclusion(ticket, trade_quality, pending, paper_order, demo_request)
        block_reason = self._execution_block_reason(demo_request)
        protective_status = self._protective_order_text(ticket, demo_request)
        entry_price = self._entry_midpoint(ticket.get("entry_zone", ""))
        requirements = trade_quality.get("requirements", {}) if isinstance(trade_quality.get("requirements"), dict) else {}
        lines = [
            "黄金开单审查卡",
            f"- 日期：{run_date}",
            f"- 审批结论：{approval}",
            f"- 执行状态：{execution_status}",
            f"- 阻断/风险原因：{block_reason or '无'}",
            f"- 策略：{self._strategy_title(strategy_id)}",
            f"- 策略 ID：{strategy_id}",
            f"- 开单追溯编号：{ticket.get('ticket_id', '')}",
            "",
            "1. 信号与开单逻辑",
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
            "4. 盈亏比与价格计划",
            f"- 入场区间：{ticket.get('entry_zone', '')}",
            f"- 估算开单价：{entry_price}",
            f"- 止盈：{self._targets(ticket.get('targets', []))}",
            f"- 止损：{ticket.get('stop_loss', '')}",
            f"- 价格移动：止盈 {self._fmt(trade_quality.get('target_price_move_pct'))}%；止损 {self._fmt(trade_quality.get('stop_price_move_pct'))}%",
            f"- 杠杆后仓位盈亏：止盈 {self._fmt(trade_quality.get('target_equity_return_pct'))}%；止损 {self._fmt(trade_quality.get('stop_equity_risk_pct'))}%；盈亏比 {self._fmt(trade_quality.get('reward_to_risk'))}",
            "",
            "5. 仓位计算",
            f"- 仓位比例：账户权益的 {ticket.get('position_size_pct', '')}%；单笔账户亏损上限：{ticket.get('max_loss_pct', '')}%",
            f"- 换算口径：{self._fmt(requirements.get('effective_leverage'))}x 杠杆；仓位收益/亏损 = 价格移动 × 杠杆。",
            f"- 账户影响估算：止盈 {self._fmt(self._account_impact_pct(ticket, trade_quality, 'target'))}%；止损 {self._fmt(self._account_impact_pct(ticket, trade_quality, 'stop'))}%。",
            f"- 数量公式：账户权益 × {ticket.get('position_size_pct', '')}% ÷ 估算开单价 {entry_price}；实际数量以成交回执为准。",
            "",
            "6. 执行与出场闭环",
            f"- 订单类型：{self._translate(ticket.get('order_type') or 'limit')}；有效期：{self._translate(ticket.get('time_in_force') or 'day')}",
            f"- 实际/请求成交：{self._actual_order_text(paper_order, demo_request)}",
            f"- 保护单状态：{protective_status}",
            f"- 正常出场：到达止盈或止损。",
            f"- 失效条件：{self._text_zh(ticket.get('invalid_if'))}",
            "- 后续操作：保护单未确认前，不允许继续扩大到 demo/live。",
            "",
            "7. 回测与证据",
            f"- 回测结论：{self._translate(backtest.get('verdict') or 'unknown')}；胜率：{self._fmt(backtest.get('win_rate'))}；平均 R：{self._fmt(backtest.get('avg_r'))}",
            f"- 证据文件：{context.get('strategy_root', '')}/trade_tickets/{run_date}.json",
            "",
            "8. 人工核对项",
            "- 方向是否和当前人工观点/机器过滤一致。",
            "- 入场价、止盈、止损是否匹配杠杆后的真实价格距离。",
            "- 账户层面的预计止损是否低于单笔账户亏损上限。",
            "- 如果是 demo/live，先确认对账和交易所保护单，再允许继续。",
        ]
        return "\n".join(lines)

    def _build_card(self, run_date: str, strategy_id: str, ticket: dict, context: dict, sequence: int, total: int) -> dict:
        return build_ticket_card(self._build_view(run_date, strategy_id, ticket, context, sequence, total))

    def _build_view(self, run_date: str, strategy_id: str, ticket: dict, context: dict, sequence: int, total: int) -> dict:
        trade_quality = ticket.get("trade_quality") if isinstance(ticket.get("trade_quality"), dict) else {}
        backtest = ticket.get("backtest") if isinstance(ticket.get("backtest"), dict) else {}
        requirements = trade_quality.get("requirements", {}) if isinstance(trade_quality.get("requirements"), dict) else {}
        pending = context.get("pending", {}) if isinstance(context.get("pending"), dict) else {}
        decision = context.get("decision", {}) if isinstance(context.get("decision"), dict) else {}
        paper_order = context.get("paper_order", {}) if isinstance(context.get("paper_order"), dict) else {}
        demo_request = context.get("demo_request", {}) if isinstance(context.get("demo_request"), dict) else {}

        approval = self._approval_conclusion(ticket, trade_quality, pending, paper_order, demo_request)
        block_reason = self._execution_block_reason(demo_request)
        config = load_pipeline_config()
        rules = load_risk_rules().get("default", {}) if isinstance(load_risk_rules(), dict) else {}
        badge = self._account_badge(strategy_id, demo_request, config)

        pos = self._num(ticket.get("position_size_pct"))
        cap = self._num(ticket.get("max_loss_pct"))
        lev = self._num(requirements.get("effective_leverage"))
        stop_move = self._num(trade_quality.get("stop_price_move_pct"))
        target_move = self._num(trade_quality.get("target_price_move_pct"))
        rr = self._num(trade_quality.get("reward_to_risk"))
        paper_account = config.get("paper_account", {}) if isinstance(config.get("paper_account"), dict) else {}
        equity = self._num(paper_account.get("starting_equity")) or 10000.0

        # Executor口径: services/paper_executor.py places notional = equity × pos% (no leverage).
        notional_usd = (equity * pos / 100) if pos is not None else None
        margin_usd = (notional_usd / lev) if (notional_usd is not None and lev) else None
        stop_usd = (notional_usd * stop_move / 100) if (notional_usd is not None and stop_move is not None) else None
        target_usd = (notional_usd * target_move / 100) if (notional_usd is not None and target_move is not None) else None
        cap_usd = (equity * cap / 100) if cap is not None else None
        account_stop_pct = (pos * stop_move / 100) if (pos is not None and stop_move is not None) else None
        risk_passed = (account_stop_pct <= cap) if (account_stop_pct is not None and cap is not None) else None

        # Guardrail口径 (risk_engine cap gate uses stop_equity_risk × pos%); surfaced as a note when it diverges.
        guardrail_stop = self._num(self._account_impact_pct(ticket, trade_quality, "stop"))
        divergence_note = ""
        if guardrail_stop is not None and account_stop_pct is not None and abs(guardrail_stop - account_stop_pct) > 1e-9:
            divergence_note = (
                f"风控引擎按保证金口径记为账户止损 {self._usd(equity * guardrail_stop / 100)}"
                f"（{self._fmt(guardrail_stop)}%），与执行器实际下单口径（{self._usd(stop_usd)}）差 {self._fmt(lev)}× 杠杆，已在排查统一。"
            )

        # Execution outcome drives the terminal node + header colour.
        demo_status = self._demo_status(demo_request)
        is_executed = (
            bool(paper_order)
            or str(decision.get("decision_status") or "") in {"executed_paper", "executed"}
            or demo_status in {"filled", "executed"}
        )
        is_blocked = (
            risk_passed is False
            or approval.startswith("禁止")
            or self._has_protective_failure(demo_request)
            or demo_status in {"blocked", "rejected", "failed"}
        )

        min_strength = self._num(rules.get("min_signal_strength")) or 60
        min_conf = self._num(rules.get("min_confidence")) or 55
        min_rr = self._num(rules.get("risk_reward_min")) or 1.5
        strength = ticket.get("signal_strength")
        confidence = ticket.get("signal_confidence")
        regime_label = self._translate(ticket.get("signal_regime") or "")

        flow = [
            {
                "step": 1,
                "name": "信号闸门",
                "passed": True,
                "detail": f"强度 {strength if strength not in (None, '') else '—'} ≥ {self._fmt(min_strength)}　·　"
                f"置信 {confidence if confidence not in (None, '') else '—'} ≥ {self._fmt(min_conf)}　·　方向 {self._direction(ticket)}",
            },
            {
                "step": 2,
                "name": "盈亏比 / 质量闸门",
                "passed": True,
                "detail": f"盈亏比 {self._fmt(rr)} ≥ {self._fmt(min_rr)}　·　目标涨幅 +{self._fmt(target_move)}%",
            },
            {
                "step": 3,
                "name": "账户风险闸门",
                "passed": risk_passed,
                "detail": f"账户止损 {self._usd(stop_usd)} {'≤' if risk_passed else '>' if risk_passed is False else '?'} 单笔上限 {self._usd(cap_usd)}",
            },
        ]
        if risk_passed is not False:
            if is_executed:
                node4 = {"passed": True, "detail": "风控 · 护栏 · 对账　全绿"}
            elif is_blocked:
                node4 = {"passed": False, "detail": block_reason or "被安全闸门拦下"}
            else:
                node4 = {"passed": None, "detail": "等待自动清算"}
            flow.append({"step": 4, "name": "自动批准闸门", **node4})

        if is_executed and risk_passed is not False:
            terminal = {"icon": "⚡", "text": "自动成交 · 已建仓"}
            header_template, title_suffix = "green", "自动成交"
        elif is_blocked or risk_passed is False:
            terminal = {"icon": "⛔", "text": "自动拒绝"}
            header_template, title_suffix = "red", ("账户止损超限" if risk_passed is False else "自动拒绝")
        else:
            terminal = {"icon": "⏳", "text": "待自动清算"}
            header_template, title_suffix = "orange", "待处理"
        if header_template == "green" and badge.get("label") == "实盘 REAL" and badge.get("armed"):
            header_template = "carmine"

        win_rate = self._num(backtest.get("win_rate"))
        sample = backtest.get("sample_size")
        ttn = config.get("trade_ticket_notifications", {}) if isinstance(config.get("trade_ticket_notifications"), dict) else {}
        base_url = str(ttn.get("dashboard_url", "")).strip().rstrip("/")
        dashboard_url = f"{base_url}/tickets/{run_date}" if base_url else ""

        latest_price = ticket.get("latest_price")
        price_line = f"开单时价 {self._trim_num(latest_price)}" if latest_price not in (None, "") else "开单时价 待补"

        return {
            "title": f"黄金开单 · {title_suffix}",
            "header_template": header_template,
            "subtitle": f"{ticket.get('asset', '')} {self._action_label(ticket)}　·　{regime_label}　·　今日第 {sequence}/{total} 张",
            "approval": "",
            "account": badge,
            "flow": flow,
            "flow_terminal": terminal,
            "risk_divergence": divergence_note,
            "position_usd": {
                "nominal": self._usd(notional_usd),
                "margin": self._usd(margin_usd),
                "leverage": self._fmt(lev) if lev is not None else "",
                "stop": f"−{self._usd(stop_usd)}" if stop_usd is not None else "—",
                "target": f"+{self._usd(target_usd)}" if target_usd is not None else "—",
                "equity": self._usd(equity),
            },
            "price": {
                "entry_zone": ticket.get("entry_zone", ""),
                "entry_price": self._trim_num(self._entry_midpoint(ticket.get("entry_zone", ""))),
                "targets": self._targets_trimmed(ticket.get("targets", [])),
                "stop_loss": self._trim_num(ticket.get("stop_loss")),
            },
            "backtest": {
                "verdict": self._translate(backtest.get("verdict") or "unknown"),
                "win_rate": f"{win_rate * 100:.1f}%" if win_rate is not None else "—",
                "avg_r": self._fmt(backtest.get("avg_r")),
                "sample": self._fmt(sample) if sample not in (None, "") else "—",
            },
            "dashboard_url": dashboard_url,
            "evidence_note": f"trade_tickets/{run_date}.json（本地 artifact）",
            "meta": {"generated_at": str(ticket.get("generated_at") or ""), "price_line": price_line},
        }

    def _usd(self, value) -> str:
        number = self._num(value)
        if number is None:
            return "—"
        if abs(number) >= 10:
            return f"${number:,.0f}"
        return f"${number:,.2f}"

    def _account_badge(self, strategy_id: str, demo_request: dict, config: dict) -> dict:
        # Mirrors the routing truth in services/multi_strategy_runner (_execution_profile_for):
        # demo is chosen by demo_trading.active_strategy_id; live by strategy.live / execution_mode.
        demo = config.get("demo_trading", {}) if isinstance(config.get("demo_trading"), dict) else {}
        if demo.get("enabled") is True and str(demo.get("active_strategy_id", "")) == strategy_id:
            return {"label": "模拟盘 DEMO", "mode": "binance_futures_demo", "armed": None}
        if demo_request:
            return {"label": "模拟盘 DEMO", "mode": "binance_futures_demo", "armed": None}
        strat = self.strategy_config.get(strategy_id, {}) if isinstance(self.strategy_config, dict) else {}
        strat = strat if isinstance(strat, dict) else {}
        execution_mode = str(config.get("execution_mode", "paper")).strip().lower()
        is_live = bool(strat.get("live")) or (strategy_id == "top_level" and execution_mode == "live")
        if is_live:
            broker = config.get("broker", {}) if isinstance(config.get("broker"), dict) else {}
            armed = bool(config.get("live_trading_enabled", False)) and not bool(broker.get("dry_run", True))
            return {"label": "实盘 REAL", "mode": "live_adapter_gated", "armed": armed}
        return {"label": "纸面 PAPER", "mode": "paper_sim", "armed": None}

    def _num(self, value):
        if value in (None, ""):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _trim_num(self, value) -> str:
        number = self._num(value)
        if number is None:
            text = str(value or "").strip()
            return text
        return f"{number:g}"

    def _targets_trimmed(self, targets) -> str:
        if isinstance(targets, (list, tuple)):
            return "，".join(self._trim_num(item) for item in targets) or "无"
        return self._trim_num(targets) or "无"

    def _approval_conclusion(self, ticket: dict, trade_quality: dict, pending: dict, paper_order: dict, demo_request: dict) -> str:
        if trade_quality and trade_quality.get("passes") is False:
            return "禁止开单：开单质量或仓位风险未通过"
        if self._has_protective_failure(demo_request):
            return "禁止继续：保护单缺失或挂单失败"
        demo_status = self._demo_status(demo_request)
        if demo_status in {"blocked", "rejected", "failed"}:
            return "禁止开单：demo 请求已阻断"
        if demo_request:
            return "已进入 demo 流程：必须确认保护单已覆盖整笔仓位"
        if paper_order:
            return "纸面已成交：未进入 demo/live"
        if pending or ticket.get("manual_execution_required", True):
            return "等待人工确认：先完成过滤与风险核对"
        return "等待审查：尚未进入订单执行"

    def _execution_status(self, pending: dict, decision: dict, paper_order: dict, demo_request: dict) -> str:
        if demo_request:
            status = self._demo_status(demo_request) or "requested"
            return f"Demo 请求：{self._translate(status)}"
        if paper_order:
            return f"纸面订单：{self._translate(paper_order.get('status', 'ordered'))}"
        if decision:
            return self._translate(decision.get("decision_status") or "decided")
        if pending:
            return self._translate(pending.get("decision_status") or "pending_manual_decision")
        return "已生成开单票，等待审查"

    def _actual_order_text(self, paper_order: dict, demo_request: dict) -> str:
        parts = []
        if paper_order:
            price = paper_order.get("fill_price") or paper_order.get("requested_price")
            parts.append(f"纸面：价格 {price}，数量 {paper_order.get('quantity')}，状态 {self._translate(paper_order.get('status'))}")
        if demo_request:
            receipt = demo_request.get("receipt", {}) if isinstance(demo_request.get("receipt"), dict) else {}
            price = receipt.get("fill_price") or receipt.get("requested_price")
            parts.append(f"Demo：价格 {price or '未成交'}，数量 {receipt.get('quantity') or '未知'}，状态 {self._translate(self._demo_status(demo_request))}")
        return "；".join(parts) if parts else "尚未进入订单执行"

    def _demo_status(self, demo_request: dict) -> str:
        if not demo_request:
            return ""
        receipt = demo_request.get("receipt", {}) if isinstance(demo_request.get("receipt"), dict) else {}
        return str(receipt.get("status") or demo_request.get("status") or "").strip()

    def _has_protective_failure(self, demo_request: dict) -> bool:
        if not demo_request:
            return False
        receipt = demo_request.get("receipt", {}) if isinstance(demo_request.get("receipt"), dict) else {}
        broker_response = demo_request.get("broker_response", {}) if isinstance(demo_request.get("broker_response"), dict) else {}
        status = self._demo_status(demo_request)
        protective_status = str(broker_response.get("protective_status") or "").strip()
        return (
            status in {"protective_order_missing", "protective_order_missing_closed"}
            or protective_status in {"failed", "partial"}
            or bool(broker_response.get("protective_errors"))
        )

    def _execution_block_reason(self, demo_request: dict) -> str:
        if not demo_request:
            return ""
        reasons = []
        protective = self._protective_error_summary(demo_request)
        if protective:
            reasons.append(protective)
        guard = demo_request.get("guard", {}) if isinstance(demo_request.get("guard"), dict) else {}
        reason = guard.get("block_reason") or demo_request.get("block_reason") or demo_request.get("reason")
        if reason:
            reasons.append(self._reason_zh(reason))
        receipt = demo_request.get("receipt", {}) if isinstance(demo_request.get("receipt"), dict) else {}
        rejection = receipt.get("rejection_reason")
        if rejection and not reasons:
            reasons.append(f"交易所拒绝码 {rejection}")
        return "；".join(item for item in reasons if item)

    def _protective_order_text(self, ticket: dict, demo_request: dict) -> str:
        targets = self._targets(ticket.get("targets", []))
        stop = ticket.get("stop_loss", "")
        plan = f"计划止盈 {targets}，计划止损 {stop}"
        if not demo_request:
            return f"{plan}；尚未提交交易所保护单。"
        if self._has_protective_failure(demo_request):
            reason = self._protective_error_summary(demo_request)
            return f"{plan}；交易所保护单失败，禁止继续。{reason}".rstrip()
        status = self._demo_status(demo_request)
        if status in {"blocked", "rejected", "failed"}:
            reason = self._execution_block_reason(demo_request)
            return f"{plan}；未提交或未确认保护单，原因：{reason or self._translate(status)}。"
        broker_response = demo_request.get("broker_response", {}) if isinstance(demo_request.get("broker_response"), dict) else {}
        protective_orders = broker_response.get("protective_orders") or []
        if protective_orders:
            return f"{plan}；交易所保护单已返回 {len(protective_orders)} 笔。"
        return f"{plan}；需要人工确认交易所侧保护单是否存在。"

    def _protective_error_summary(self, demo_request: dict) -> str:
        broker_response = demo_request.get("broker_response", {}) if isinstance(demo_request.get("broker_response"), dict) else {}
        errors = broker_response.get("protective_errors") or []
        summaries = []
        for item in errors[:2]:
            if not isinstance(item, dict):
                continue
            payload = item.get("payload", {}) if isinstance(item.get("payload"), dict) else {}
            order_type = self._protective_type_label(payload.get("type"))
            message = self._reason_zh(item.get("message") or item.get("error_type") or "保护单请求失败")
            summaries.append(f"{order_type}失败：{message}")
        return "；".join(summaries)

    def _protective_type_label(self, value) -> str:
        text = str(value or "").upper()
        if "STOP" in text and "TAKE" not in text:
            return "止损保护单"
        if "TAKE_PROFIT" in text:
            return "止盈保护单"
        return "保护单"

    def _account_impact_pct(self, ticket: dict, trade_quality: dict, kind: str):
        key = "estimated_account_target_return_pct" if kind == "target" else "estimated_account_stop_risk_pct"
        if trade_quality.get(key) not in (None, ""):
            return trade_quality.get(key)
        source_key = "target_equity_return_pct" if kind == "target" else "stop_equity_risk_pct"
        source = trade_quality.get(source_key)
        if source in (None, ""):
            return None
        try:
            return float(source) * float(ticket.get("position_size_pct", 0) or 0) / 100
        except (TypeError, ValueError):
            return None

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
        if strategy_id == "top_level":
            return "主流程"
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
            "Price closes back below the latest breakout/reference level.": "价格重新收回到最近的突破/参考位下方。",
            "Price closes back above the latest breakout/reference level.": "价格重新收回到最近的突破/参考位上方。",
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

    def _reason_zh(self, value) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        replacements = [
            (
                "Binance demo reconciliation cannot confirm venue state:",
                "Binance demo 对账无法确认交易所状态：",
            ),
            (
                "Binance demo reconciliation failed:",
                "Binance demo 对账失败：",
            ),
            (
                "Binance demo reconciliation drift:",
                "Binance demo 对账发现漂移：",
            ),
            (
                "timeout: The read operation timed out",
                "读取交易所状态超时",
            ),
            (
                "URLError: <urlopen error _ssl.c:1112: The handshake operation timed out>",
                "网络/SSL 握手超时",
            ),
            (
                "HTTP Error 400: Bad Request",
                "交易所拒绝请求（HTTP 400）",
            ),
        ]
        result = text
        for old, new in replacements:
            result = result.replace(old, new)
        return re.sub(r"：\s+", "：", result)

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
            "rejected": "已拒绝",
            "protective_order_missing": "保护单缺失",
            "protective_order_missing_closed": "保护单缺失，已紧急平仓",
            "cannot_confirm": "无法确认",
            "venue_state_unknown": "交易所状态未知",
            "partial": "部分完成",
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
            "no_trade": "不开仓",
            "pullback_long": "回调做多",
            "risk_off_reversal": "避险反转",
            "event_risk_reduction": "事件风险减仓",
            "adr_exhaustion_reversion": "ADR 极值回归",
            "adx_ema_pullback": "ADX/EMA 回调",
            "bollinger_reclaim_filter": "布林带重夺过滤",
            "breakout_retest_continuation": "突破回踩续势",
            "false_breakout_reversal": "假突破反转",
            "fibonacci_pullback": "斐波那契回调",
            "london_ny_compression_breakout": "伦敦/纽约收敛突破",
            "ny_opening_range_breakout": "纽约开盘区间突破",
            "vwap_extension_reversion": "VWAP 乖离回归",
            "range_breakout": "区间突破",
            "breakout": "突破",
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
