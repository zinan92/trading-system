from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from services.config_loader import ROOT, load_pipeline_config
from services.dualtrack_clock import BJ_TZ, cycle_window, cycle_window_from_id, parse_utc
from services.dualtrack_config import dualtrack_config, track_notional_budget
from services.dualtrack_scoring import _trades_from_fills
from services.dualtrack_store import DualTrackPlanStore
from services.feishu_report_sender import FeishuReportSender, resolve_trade_sender
from services.journal_store import load_json, write_json
from services.market_store import MarketStore
from services.trade_record_card import TradeRecordCardBuilder
from services.trade_ticket_card import build_ticket_card


MACHINE_STRATEGY_ID = "dualtrack_machine_grid"
MACHINE_STRATEGY_NAME = "机器轨网格"
BRIEF_KIND = "dualtrack_machine_brief"
TRADE_KIND = "dualtrack_trade_record"
TRADE_EVENTS = {"entry", "target", "stop", "flatten", "exit"}

MACHINE_STRATEGY_CONFIG = {
    "trader_id": "machine_track",
    "portfolio_id": "dualtrack_gold",
    "strategy_variant": "grid_machine",
    "classification": {
        "family": "dualtrack_machine",
        "style": "grid",
        "trader_id": "machine_track",
        "portfolio_id": "dualtrack_gold",
    },
}


class DualTrackMachineBriefSender:
    """Send the 12-hour machine-track plan to the trading-record channel."""

    def __init__(self, output_root: Path | None = None, market_db: Path | None = None, sender=None, config: dict[str, Any] | None = None) -> None:
        pipeline_config = load_pipeline_config()
        self.output_root = Path(output_root) if output_root else ROOT / str(pipeline_config.get("output_root", "outputs"))
        self.market_db = Path(market_db) if market_db else ROOT / str(pipeline_config.get("local_market_db", "data/market_data.db"))
        self.config = config or dualtrack_config()
        self.sender = sender
        self.store = DualTrackPlanStore(self.output_root, config=self.config)

    def build(self, cycle_id: str | None = None, *, as_of: str | datetime | None = None) -> dict[str, Any]:
        now = parse_utc(as_of)
        window = cycle_window(now) if not cycle_id else cycle_window_from_id(cycle_id)
        cycle_id = window.cycle_id
        ai_plan = self.store.load_plan(cycle_id, "ai")
        human_plan = self.store.load_plan(cycle_id, "human")
        cycle_state = _latest_row(self.output_root / "dualtrack" / "cycles" / f"{cycle_id}.json")
        market = self._latest_market()
        anchor = _number(cycle_state.get("open_price")) or _number(market.get("close")) or _number(market.get("price"))
        trend_gate_armed = _trend_gate_armed(self.output_root, cycle_state)
        payload = {
            "schema_version": "dualtrack-machine-brief-v1",
            "generated_at": now.isoformat(),
            "cycle_id": cycle_id,
            "run_date": cycle_id.split("_", 1)[0],
            "cycle_kind": window.kind,
            "window": window.to_dict(),
            "strategy_id": MACHINE_STRATEGY_ID,
            "strategy_name": MACHINE_STRATEGY_NAME,
            "status": "decision_error" if (ai_plan or {}).get("degraded") else "ready" if ai_plan else "blocked",
            "reason": (ai_plan or {}).get("planning_error", "") if (ai_plan or {}).get("degraded") else "" if ai_plan else "AI 作战单缺失，机器轨不能定义方向",
            "ai_plan": ai_plan or {},
            "human_plan_present": bool(human_plan),
            "market": market,
            "anchor_price": anchor,
            "trend_gate_armed": trend_gate_armed,
            "grid": self._grid_payload(ai_plan or {}, anchor=anchor, trend_gate_armed=trend_gate_armed),
            "message": "",
        }
        payload["message"] = self._format_message(payload)
        self._record_artifact(payload)
        return payload

    def send(self, cycle_id: str | None = None, *, as_of: str | datetime | None = None, force: bool = False) -> dict[str, Any]:
        payload = self.build(cycle_id, as_of=as_of)
        run_date = str(payload["run_date"])
        notification_path = self.output_root / "dualtrack_machine_brief_notifications" / f"{run_date}.json"
        rows = [row for row in load_json(notification_path) if isinstance(row, dict)]
        already_delivered = any(row.get("cycle_id") == payload["cycle_id"] and row.get("delivered") is True for row in rows)
        if already_delivered and not force:
            return {
                "status": "skipped",
                "reason": "already_delivered",
                "run_date": run_date,
                "cycle_id": payload["cycle_id"],
                "sent": 0,
                "delivered": True,
                "artifact": self._artifact_path(str(payload["cycle_id"])).as_posix(),
            }
        if os.getenv("PYTEST_CURRENT_TEST") and self.sender is None:
            return {"status": "pytest_skipped", "run_date": run_date, "cycle_id": payload["cycle_id"], "sent": 0, "delivered": False}

        sender = self.sender if self.sender is not None else resolve_trade_sender()
        title = f"黄金机器轨作战单｜{_cycle_kind_zh(str(payload['cycle_kind']))}"
        result = FeishuReportSender(self.output_root, sender=sender).run(
            run_date=run_date,
            kind=BRIEF_KIND,
            title=title,
            message=str(payload["message"]),
            max_chars=2200,
        )
        delivered = bool(result.get("delivered"))
        rows = [row for row in rows if row.get("cycle_id") != payload["cycle_id"]]
        rows.append({
            "run_date": run_date,
            "cycle_id": payload["cycle_id"],
            "notified_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "status": "pass" if delivered else "fail",
            "delivered": delivered,
            "channel": result.get("channel", ""),
            "title": title,
            "delivery": result.get("delivery", {}),
            "artifact": self._artifact_path(str(payload["cycle_id"])).as_posix(),
        })
        write_json(notification_path, rows)
        write_json(self.output_root / "dualtrack_machine_brief_notifications" / "current.json", rows[-1:])
        return {
            "status": "pass" if delivered else "fail",
            "run_date": run_date,
            "cycle_id": payload["cycle_id"],
            "sent": 1 if delivered else 0,
            "delivered": delivered,
            "channel": result.get("channel", ""),
            "artifact": self._artifact_path(str(payload["cycle_id"])).as_posix(),
        }

    def _grid_payload(self, plan: dict[str, Any], *, anchor: float | None, trend_gate_armed: bool) -> dict[str, Any]:
        total_budget = track_notional_budget(self.config)
        direction = str(plan.get("direction") or "absent")
        orders = plan.get("grid_orders") if isinstance(plan.get("grid_orders"), list) else []
        invalidation = plan.get("invalidation") if isinstance(plan.get("invalidation"), list) else []
        return {
            "direction": direction,
            "total_notional_budget": round(total_budget, 2),
            "orders": orders,
            "entry_levels_preview": [float(order["entry"]) for order in orders],
            "invalidation": invalidation,
        }

    def _latest_market(self) -> dict[str, Any]:
        market_data = self.config.get("market_data") if isinstance(self.config.get("market_data"), dict) else {}
        symbol = str(market_data.get("symbol") or "GOLD")
        timeframe = str(market_data.get("timeframe") or "1m")
        try:
            store = MarketStore(self.market_db)
            latest = store.load_latest_bar(symbol, timeframe)
            if latest:
                return latest
            return store.load_latest_quote(symbol)
        except Exception as exc:  # noqa: BLE001 - Feishu brief should explain missing market data, not crash.
            return {"status": "missing", "reason": f"{exc.__class__.__name__}: {exc}"}

    def _format_message(self, payload: dict[str, Any]) -> str:
        plan = payload.get("ai_plan") if isinstance(payload.get("ai_plan"), dict) else {}
        grid = payload.get("grid") if isinstance(payload.get("grid"), dict) else {}
        market = payload.get("market") if isinstance(payload.get("market"), dict) else {}
        direction = str(plan.get("direction") or "absent")
        invalidation = _format_invalidation(plan.get("invalidation"))
        key_levels = _format_levels(plan.get("key_levels"))
        entry_levels = _format_levels(grid.get("entry_levels_preview"))
        latest = _number(market.get("close"))
        lines = [
            "黄金机器轨作战单",
            f"- 周期：{payload.get('cycle_id')}（北京时间 {payload['window']['start_cst'][11:16]}-{payload['window']['end_cst'][11:16]}）",
            f"- 结论：{_direction_zh(direction)}；置信度 {plan.get('confidence', '缺失')}/10。",
        ]
        if payload.get("status") != "ready":
            lines.append(f"- 状态：不可执行。原因：{payload.get('reason')}")
            return "\n".join(lines)
        range_payload = plan.get("range") if isinstance(plan.get("range"), dict) else {}
        orders = grid.get("orders") if isinstance(grid.get("orders"), list) else []
        order_lines = [
            f"  {index + 1}. 入场 {_fmt_price(order.get('entry'))} -> 止盈 {_fmt_price(order.get('take_profit'))}，权重 {order.get('weight', 1)}，名义 {_money(float(grid.get('total_notional_budget') or 0) * float(order.get('weight', 1)))}"
            for index, order in enumerate(orders)
        ]
        lines.extend(
            [
                f"- 最新价：{_fmt_price(latest)}。",
                f"- 预期区间：{_fmt_price(range_payload.get('low'))}-{_fmt_price(range_payload.get('high'))}。",
                f"- 关键位：{key_levels or '缺失'}。",
                f"- 失效条件：{invalidation or '缺失'}。",
                f"- 判断：{plan.get('rationale') or '缺失'}",
                "",
                "机器轨明确网格",
                *(order_lines or ["  本周期中立，不挂入场单。"]),
                f"- 机器轨最大名义预算：{_money(grid.get('total_notional_budget'))}。",
                "",
                "你需要看什么",
                "- 这份计划由机器轨独立生成，不读取或继承人工计划。",
                "- 开仓和平仓会单独发送；即使中立或零成交，收盘后也会生成复盘。",
            ]
        )
        return "\n".join(lines)

    def _record_artifact(self, payload: dict[str, Any]) -> None:
        write_json(self._artifact_path(str(payload["cycle_id"])), [payload])
        write_json(self.output_root / "dualtrack" / "machine_briefs" / "current.json", [payload])

    def _artifact_path(self, cycle_id: str) -> Path:
        return self.output_root / "dualtrack" / "machine_briefs" / f"{cycle_id}.json"


class DualTrackTradeRecordNotifier:
    """Send machine-track entry/exit fills to the trading-record Feishu channel."""

    def __init__(self, output_root: Path | None = None, sender=None, config: dict[str, Any] | None = None) -> None:
        pipeline_config = load_pipeline_config()
        self.output_root = Path(output_root) if output_root else ROOT / str(pipeline_config.get("output_root", "outputs"))
        self.config = config or dualtrack_config()
        self.sender = sender

    def notify_current(self, *, as_of: str | datetime | None = None, force: bool = False, backfill_existing: bool = False) -> dict[str, Any]:
        return self.notify_cycle(cycle_window(as_of).cycle_id, force=force, backfill_existing=backfill_existing)

    def notify_cycle(self, cycle_id: str, *, force: bool = False, backfill_existing: bool = False) -> dict[str, Any]:
        run_date = cycle_id.split("_", 1)[0]
        fills = [row for row in load_json(self._fills_path(cycle_id)) if isinstance(row, dict)]
        event_rows = self._real_trade_events(cycle_id, fills)
        if not event_rows:
            return {"status": "no_events", "run_date": run_date, "cycle_id": cycle_id, "sent": 0, "skipped": 0, "failed": 0}
        if os.getenv("TRADING_ORCHESTRATOR_DISABLE_DUALTRACK_TRADE_FEISHU"):
            return {"status": "disabled", "run_date": run_date, "cycle_id": cycle_id, "sent": 0, "skipped": len(event_rows), "failed": 0}
        if os.getenv("PYTEST_CURRENT_TEST") and self.sender is None:
            return {"status": "pytest_skipped", "run_date": run_date, "cycle_id": cycle_id, "sent": 0, "skipped": len(event_rows), "failed": 0}

        notification_path = self.output_root / "dualtrack_trade_notifications" / f"{run_date}.json"
        existing = [row for row in load_json(notification_path) if isinstance(row, dict)]
        seen_keys = {
            self._key(row.get("cycle_id", ""), row.get("fill_id", ""))
            for row in existing
            if row.get("delivered") is True or row.get("suppressed") is True
        }
        first_seen_cycle = not any(row.get("cycle_id") == cycle_id for row in existing)
        if first_seen_cycle and not force and not backfill_existing:
            now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            records = list(existing)
            for fill in event_rows:
                records.append({
                    "run_date": run_date,
                    "cycle_id": cycle_id,
                    "fill_id": fill.get("fill_id", ""),
                    "event": fill.get("event", ""),
                    "notified_at": now,
                    "status": "suppressed",
                    "delivered": False,
                    "suppressed": True,
                    "reason": "baseline_existing_fills_on_first_notifier_run",
                })
            write_json(notification_path, records)
            write_json(self.output_root / "dualtrack_trade_notifications" / "current.json", records[-1:] if records else [])
            return {
                "status": "baseline",
                "run_date": run_date,
                "cycle_id": cycle_id,
                "sent": 0,
                "skipped": len(event_rows),
                "failed": 0,
                "baselined": len(event_rows),
            }
        sender = self.sender if self.sender is not None else resolve_trade_sender()

        sent = 0
        skipped = 0
        failed = 0
        records = list(existing)
        for index, fill in enumerate(event_rows):
            fill_id = str(fill.get("fill_id") or "")
            key = self._key(cycle_id, fill_id)
            if key in seen_keys and not force:
                skipped += 1
                continue
            prefix = event_rows[: index + 1]
            record = self._record_for_fill(cycle_id, fill, prefix)
            message = self._format_trade_message(record)
            title = (
                "黄金开单 · 自动成交"
                if str(record.get("event") or "") == "entry"
                else f"黄金交易记录｜{record['event_label']}｜{MACHINE_STRATEGY_NAME}"
            )
            result = FeishuReportSender(self.output_root, sender=sender).run(
                run_date=run_date,
                kind=TRADE_KIND,
                title=title,
                message=message,
                max_chars=2200,
                card=self._build_entry_feishu_card(record) if str(record.get("event") or "") == "entry" else None,
            )
            delivered = bool(result.get("delivered"))
            records = [row for row in records if self._key(row.get("cycle_id", ""), row.get("fill_id", "")) != key]
            records.append({
                "run_date": run_date,
                "cycle_id": cycle_id,
                "fill_id": fill_id,
                "event": fill.get("event", ""),
                "notified_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                "status": "pass" if delivered else "fail",
                "delivered": delivered,
                "channel": result.get("channel", ""),
                "title": title,
                "delivery": result.get("delivery", {}),
                "record_card": record.get("record_card", {}),
            })
            if delivered:
                sent += 1
                seen_keys.add(key)
            else:
                failed += 1
        write_json(notification_path, records)
        write_json(self.output_root / "dualtrack_trade_notifications" / "current.json", records[-1:] if records else [])
        return {
            "status": "pass" if sent and not failed else "fail" if failed else "noop",
            "run_date": run_date,
            "cycle_id": cycle_id,
            "sent": sent,
            "skipped": skipped,
            "failed": failed,
        }

    def _record_for_fill(self, cycle_id: str, fill: dict[str, Any], prefix: list[dict[str, Any]]) -> dict[str, Any]:
        event = str(fill.get("event") or "")
        trade = self._trade_dict_for_fill(cycle_id, fill, prefix)
        card = TradeRecordCardBuilder(
            run_date=cycle_id.split("_", 1)[0],
            strategy_id=MACHINE_STRATEGY_ID,
            strategy_config=MACHINE_STRATEGY_CONFIG,
            latest_price=_number(fill.get("price")),
            account_equity=float(self.config.get("capital_per_track_usd") or 10_000.0),
        ).build(trade)
        return {
            "cycle_id": cycle_id,
            "event": event,
            "event_label": _event_label(event),
            "entry_index": _entry_index(prefix),
            "plan": _latest_row(self.output_root / "dualtrack" / "plans" / f"{cycle_id}_ai.json"),
            "fill": fill,
            "trade": trade,
            "record_card": card,
        }

    def _trade_dict_for_fill(self, cycle_id: str, fill: dict[str, Any], prefix: list[dict[str, Any]]) -> dict[str, Any]:
        event = str(fill.get("event") or "")
        source = str(self._fills_path(cycle_id).resolve())
        if event == "entry":
            side = "long" if str(fill.get("side")) == "buy" else "short"
            quantity = _fill_units(fill, price=_number(fill.get("price")), config=self.config)
            cost = _number(fill.get("cost")) or 0.0
            return {
                "trade_id": str(fill.get("fill_id") or ""),
                "strategy_id": MACHINE_STRATEGY_ID,
                "symbol": "GOLD",
                "side": side,
                "quantity": quantity,
                "status": "open",
                "entry_reason": _entry_reason(fill),
                "entry_price": fill.get("price"),
                "requested_entry_price": fill.get("price"),
                "opened_at": fill.get("ts"),
                "order_id": fill.get("fill_id"),
                "ticket_id": cycle_id,
                "signal_id": f"{cycle_id}_ai_plan",
                "target": fill.get("tp"),
                "stop_loss": fill.get("sl"),
                "latest_price": fill.get("price"),
                "entry_total_cost": cost,
                "gross_unrealized_pnl": 0.0,
                "unrealized_pnl": -cost,
                "source_artifacts": [source],
            }

        trades = _trades_from_fills(prefix, track="machine")
        matched = _matching_trade_for_exit(trades, str(fill.get("fill_id") or "")) or {}
        exit_match = _matching_exit(matched, str(fill.get("fill_id") or "")) or {}
        side = str(matched.get("side") or ("short" if str(fill.get("side")) == "buy" else "long"))
        entry_price = _number(matched.get("entry_price")) or _number(fill.get("price"))
        exit_price = _number(fill.get("price"))
        quantity = _number(exit_match.get("units")) or _fill_units(fill, price=exit_price, config=self.config)
        entry_units = _number(matched.get("units")) or quantity or 0.0
        entry_cost_total = _number(matched.get("entry_cost")) or 0.0
        entry_cost = entry_cost_total * min(1.0, float(quantity or 0.0) / float(entry_units or quantity or 1.0))
        exit_cost = _number(fill.get("cost")) or 0.0
        direction = 1 if side == "long" else -1
        gross = ((exit_price or 0.0) - (entry_price or 0.0)) * direction * float(quantity or 0.0)
        total_cost = entry_cost + exit_cost
        return {
            "trade_id": str(matched.get("trade_id") or fill.get("fill_id") or ""),
            "strategy_id": MACHINE_STRATEGY_ID,
            "symbol": "GOLD",
            "side": side,
            "quantity": quantity,
            "status": "closed",
            "entry_reason": _entry_reason({"layer": matched.get("layer") or fill.get("layer"), "rung": matched.get("rung", fill.get("rung")), "side": "buy" if side == "long" else "sell"}),
            "entry_price": entry_price,
            "requested_entry_price": entry_price,
            "opened_at": matched.get("entry_ts") or fill.get("ts"),
            "order_id": matched.get("entry_fill_id") or fill.get("fill_id"),
            "ticket_id": cycle_id,
            "signal_id": f"{cycle_id}_ai_plan",
            "target": matched.get("tp") or fill.get("tp"),
            "stop_loss": matched.get("sl") or fill.get("sl"),
            "exit_reason": _event_reason(event),
            "exit_price": exit_price,
            "closed_at": fill.get("ts"),
            "entry_total_cost": round(entry_cost, 6),
            "exit_total_cost": exit_cost,
            "total_cost": round(total_cost, 6),
            "gross_realized_pnl": round(gross, 6),
            "realized_pnl": round(gross - total_cost, 6),
            "source_artifacts": [source],
        }

    def _format_trade_message(self, record: dict[str, Any]) -> str:
        if str(record.get("event") or "") == "entry":
            return self._format_entry_message(record)

        card = record.get("record_card") if isinstance(record.get("record_card"), dict) else {}
        fill = record.get("fill") if isinstance(record.get("fill"), dict) else {}
        entry = card.get("entry", {}) if isinstance(card.get("entry"), dict) else {}
        exit_plan = card.get("exit", {}) if isinstance(card.get("exit"), dict) else {}
        protection = card.get("protection", {}) if isinstance(card.get("protection"), dict) else {}
        pnl = card.get("pnl", {}) if isinstance(card.get("pnl"), dict) else {}
        audit = card.get("audit", {}) if isinstance(card.get("audit"), dict) else {}
        event = str(record.get("event") or "")
        lines = [
            "黄金交易记录",
            f"- 事件：{record.get('event_label')}。",
            f"- 策略：{MACHINE_STRATEGY_NAME}（机器轨 / 网格）。",
            f"- 周期：{record.get('cycle_id')}。",
            f"- 层级：{_layer_label(fill)}，第 {int(fill.get('rung') or 0) + 1} 层。",
            f"- 方向：{_side_zh(card.get('side'))}。",
            f"- 开仓：{_fmt_price(entry.get('price'))}，时间 {entry.get('opened_at')}。",
            f"- 止盈 / 止损：{_fmt_price(protection.get('take_profit'))} / {_fmt_price(protection.get('stop_loss'))}。",
            f"- 仓位：数量 {_fmt_number(card.get('quantity'))}，名义 ${_fmt_number(fill.get('notional'))}。",
        ]
        if event != "entry":
            lines.extend(
                [
                    f"- 平仓：{_fmt_price(exit_plan.get('price'))}，原因 {_event_reason(event)}，时间 {exit_plan.get('closed_at')}。",
                    f"- 本次 PnL：{_signed_money(pnl.get('realized_pnl'))}，R 倍数 {_fmt_number(pnl.get('r_multiple'))}。",
                ]
            )
        else:
            lines.append(f"- 当前 PnL：开仓成本 {_signed_money(trade.get('unrealized_pnl'))}；等待止盈或止损闭环。")
        lines.extend(
            [
                f"- 保护状态：{_protection_zh(protection.get('status'))}。",
                f"- 审计：{'完整' if audit.get('status') == 'complete' else '需检查'}；缺项 {', '.join(audit.get('missing_fields') or []) or '无'}。",
                f"- 追溯编号：{fill.get('fill_id')}",
            ]
        )
        return "\n".join(lines)

    def _format_entry_message(self, record: dict[str, Any]) -> str:
        card = record.get("record_card") if isinstance(record.get("record_card"), dict) else {}
        trade = record.get("trade") if isinstance(record.get("trade"), dict) else {}
        fill = record.get("fill") if isinstance(record.get("fill"), dict) else {}
        plan = record.get("plan") if isinstance(record.get("plan"), dict) else {}
        entry = card.get("entry", {}) if isinstance(card.get("entry"), dict) else {}
        protection = card.get("protection", {}) if isinstance(card.get("protection"), dict) else {}
        pnl = card.get("pnl", {}) if isinstance(card.get("pnl"), dict) else {}
        audit = card.get("audit", {}) if isinstance(card.get("audit"), dict) else {}

        entry_price = _number(entry.get("price"))
        tp = _number(protection.get("take_profit"))
        sl = _number(protection.get("stop_loss"))
        quantity = _number(card.get("quantity")) or 0.0
        notional = _number(fill.get("notional"))
        if notional is None and entry_price is not None:
            notional = entry_price * quantity
        account_equity = _number(pnl.get("account_equity")) or float(self.config.get("capital_per_track_usd") or 10_000.0)
        max_leverage = max(1.0, float(self.config.get("max_leverage") or 1.0))
        margin = (notional or 0.0) / max_leverage if notional is not None else None
        exposure = (notional / account_equity) if notional is not None and account_equity else None
        reward, risk, rr = _entry_reward_risk(
            side=card.get("side"),
            entry_price=entry_price,
            take_profit=tp,
            stop_loss=sl,
            quantity=quantity,
            entry_cost=_number(fill.get("cost")) or 0.0,
            notional=notional,
            cost_per_side_bp=_cost_per_side_bp(self.config),
        )
        audit_ok = audit.get("status") == "complete" and protection.get("status") == "protected"
        layer = _layer_label(fill)
        rung = int(fill.get("rung") or 0) + 1
        direction = _side_zh(card.get("side"))
        plan_direction = _direction_zh(plan.get("direction") or ("long" if card.get("side") == "long" else "short"))
        confidence = plan.get("confidence", "缺失")
        source = str(plan.get("source") or "作战单").strip() or "作战单"
        source_label = "Obsidian 作战单" if source.lower() == "obsidian" else source
        path = _display_path(str((self._fills_path(str(record.get("cycle_id") or ""))).resolve()))
        protection_line = (
            f"TP {_fmt_price(tp)} / SL {_fmt_price(sl)} · 盈亏比 {_fmt_rr(rr)}"
            if tp is not None and sl is not None
            else "TP/SL 缺失，需要检查"
        )

        lines = [
            f"GOLD 准备{direction} · {MACHINE_STRATEGY_NAME} · 本周期第 {record.get('entry_index') or 1} 张",
            "机器轨已模拟成交：未进入 demo/live",
            "",
            "账户",
            f"机器轨账户 {_money(account_equity)}",
            "",
            "执行模式",
            f"dualtrack_sim / {_order_type_zh(fill.get('order_type'))}",
            "",
            "决策路径",
            f"✅ 1. 作战单方向 — {plan_direction} · 置信度 {confidence}/10",
            f"✅ 2. 人工/机器过滤 — {source_label}已锁定；机器{layer}第 {rung} 层触价",
            f"{'✅' if protection.get('status') == 'protected' else '⚠️'} 3. 风险闭环 — {protection_line}",
            f"✅ 4. 仓位检查 — 名义 {_money(notional)} · 账户暴露 {_fmt_leverage(exposure)} · 估算保证金 {_money(margin)}",
            f"⚡ 自动成交：{'已建仓' if audit_ok else '已建仓，但审计需检查'}",
            "",
            "仓位（美元名义）",
            f"名义仓位 {_money(notional)}（按 {max_leverage:g}x 估算保证金 {_money(margin)}）",
            f"止损预估 {_signed_usd(risk)} · 止盈预估 {_signed_usd(reward)}",
            "",
            "入场区间",
            f"{_fmt_price(entry_price)}（{layer}第 {rung} 层触发价）",
            "",
            "估算开单价",
            _fmt_price(entry_price),
            "",
            "止盈",
            _fmt_price(tp),
            "",
            "止损",
            _fmt_price(sl),
            "",
            "策略统计",
            "旧 ticket 回测不适用；本单纳入机器轨 cycle/fill 复盘样本。",
            "",
            f"证据：{path}",
            f"生成时间 {entry.get('opened_at')} · 开单时价 {_fmt_price(entry_price)}",
        ]
        if audit.get("missing_fields"):
            lines.insert(-2, f"审计缺项：{', '.join(audit.get('missing_fields') or [])}")
        return "\n".join(lines)

    def _build_entry_feishu_card(self, record: dict[str, Any]) -> dict[str, Any]:
        card = record.get("record_card") if isinstance(record.get("record_card"), dict) else {}
        fill = record.get("fill") if isinstance(record.get("fill"), dict) else {}
        plan = record.get("plan") if isinstance(record.get("plan"), dict) else {}
        entry = card.get("entry", {}) if isinstance(card.get("entry"), dict) else {}
        protection = card.get("protection", {}) if isinstance(card.get("protection"), dict) else {}
        pnl = card.get("pnl", {}) if isinstance(card.get("pnl"), dict) else {}
        audit = card.get("audit", {}) if isinstance(card.get("audit"), dict) else {}

        entry_price = _number(entry.get("price"))
        tp = _number(protection.get("take_profit"))
        sl = _number(protection.get("stop_loss"))
        quantity = _number(card.get("quantity")) or 0.0
        notional = _number(fill.get("notional"))
        if notional is None and entry_price is not None:
            notional = entry_price * quantity
        account_equity = _number(pnl.get("account_equity")) or float(self.config.get("capital_per_track_usd") or 10_000.0)
        max_leverage = max(1.0, float(self.config.get("max_leverage") or 1.0))
        margin = (notional or 0.0) / max_leverage if notional is not None else None
        exposure = (notional / account_equity) if notional is not None and account_equity else None
        reward, risk, rr = _entry_reward_risk(
            side=card.get("side"),
            entry_price=entry_price,
            take_profit=tp,
            stop_loss=sl,
            quantity=quantity,
            entry_cost=_number(fill.get("cost")) or 0.0,
            notional=notional,
            cost_per_side_bp=_cost_per_side_bp(self.config),
        )
        layer = _layer_label(fill)
        rung = int(fill.get("rung") or 0) + 1
        direction = _side_zh(card.get("side"))
        plan_direction = _direction_zh(plan.get("direction") or ("long" if card.get("side") == "long" else "short"))
        confidence = plan.get("confidence", "缺失")
        source = str(plan.get("source") or "作战单").strip() or "作战单"
        source_label = "Obsidian 作战单" if source.lower() == "obsidian" else source
        protection_ok = protection.get("status") == "protected"
        audit_ok = audit.get("status") == "complete" and protection_ok
        evidence = _display_path(str(self._fills_path(str(record.get("cycle_id") or "")).resolve()))

        view = {
            "title": "黄金开单 · 自动成交",
            "header_template": "green" if audit_ok else "orange",
            "subtitle": f"GOLD 准备{direction}　·　{MACHINE_STRATEGY_NAME}　·　本周期第 {record.get('entry_index') or 1} 张",
            "approval": "机器轨已模拟成交：未进入 demo/live",
            "account": {
                "label": f"机器轨账户 {_money(account_equity)}",
                "mode": f"dualtrack_sim / {_order_type_zh(fill.get('order_type'))}",
                "armed": None,
            },
            "flow": [
                {"step": 1, "name": "作战单方向", "passed": True, "detail": f"{plan_direction}　·　置信度 {confidence}/10"},
                {"step": 2, "name": "人工/机器过滤", "passed": True, "detail": f"{source_label}已锁定　·　机器{layer}第 {rung} 层触价"},
                {"step": 3, "name": "风险闭环", "passed": protection_ok, "detail": f"TP {_fmt_price(tp)}　·　SL {_fmt_price(sl)}　·　盈亏比 {_fmt_rr(rr)}"},
                {"step": 4, "name": "仓位检查", "passed": notional is not None, "detail": f"名义 {_money(notional)}　·　账户暴露 {_fmt_leverage(exposure)}　·　保证金 {_money(margin)}"},
            ],
            "flow_terminal": {
                "icon": "⚡" if audit_ok else "⚠️",
                "text": "自动成交 · 已建仓" if audit_ok else "已建仓 · 审计需检查",
            },
            "risk_divergence": "" if protection_ok else "止盈或止损不完整，请立即检查机器轨保护参数。",
            "position_usd": {
                "nominal": _money(notional),
                "margin": _money(margin),
                "leverage": f"{max_leverage:g}",
                "stop": _signed_usd(risk),
                "target": _signed_usd(reward),
            },
            "price": {
                "entry_zone": f"{_fmt_price(entry_price)}（{layer}第 {rung} 层触发价）",
                "entry_price": _fmt_price(entry_price),
                "targets": _fmt_price(tp),
                "stop_loss": _fmt_price(sl),
            },
            "backtest": {},
            "evidence_note": f"{evidence}（机器轨 cycle/fill 复盘样本）",
            "meta": {
                "generated_at": str(entry.get("opened_at") or ""),
                "price_line": f"开单时价 {_fmt_price(entry_price)}",
            },
        }
        return build_ticket_card(view)

    def _fills_path(self, cycle_id: str) -> Path:
        return self.output_root / "dualtrack" / "fills" / f"{cycle_id}_machine.json"

    def _key(self, cycle_id: Any, fill_id: Any) -> str:
        return f"{cycle_id}:{fill_id}"

    def _real_trade_events(self, cycle_id: str, fills: list[dict[str, Any]]) -> list[dict[str, Any]]:
        window = cycle_window_from_id(cycle_id)
        rows = []
        for fill in fills:
            event = str(fill.get("event") or "")
            if event not in TRADE_EVENTS:
                continue
            # Intraday machine simulation marks open inventory to the latest bar
            # with flatten events. Those are not real closing records until the
            # 12-hour cycle is actually at its final bar.
            if event == "flatten":
                try:
                    if parse_utc(fill.get("ts")) < window.end - timedelta(minutes=2):
                        continue
                except (TypeError, ValueError):
                    continue
            rows.append(fill)
        return rows


def _latest_row(path: Path) -> dict[str, Any]:
    rows = load_json(path)
    if isinstance(rows, list) and rows:
        last = rows[-1]
        return last if isinstance(last, dict) else {}
    if isinstance(rows, dict):
        return rows
    return {}


def _trend_gate_armed(output_root: Path, cycle_state: dict[str, Any]) -> bool:
    if "trend_gate_armed" in cycle_state:
        return bool(cycle_state.get("trend_gate_armed"))
    board = _latest_row(output_root / "dualtrack" / "scoreboard.json")
    gate = board.get("trend_leg_gate") if isinstance(board.get("trend_leg_gate"), dict) else {}
    return bool(gate.get("armed", False))


def _format_levels(values: Any) -> str:
    rows = []
    for value in values or []:
        number = _number(value)
        rows.append(_fmt_price(number) if number is not None else str(value))
    return "、".join(rows)


def _format_invalidation(rows: Any) -> str:
    parts = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        side = "跌破" if row.get("side") == "below" else "上破"
        confirm = "1 分钟收盘确认" if row.get("confirm") == "close_1m" else "触及即失效"
        parts.append(f"{side} {_fmt_price(row.get('price'))}（{confirm}）")
    return "；".join(parts)


def _direction_zh(value: Any) -> str:
    direction = str(value or "").lower()
    return {
        "long": "只做多",
        "short": "只做空",
        "flat": "不主动开仓",
        "absent": "缺少方向",
    }.get(direction, direction or "缺少方向")


def _side_zh(value: Any) -> str:
    return "做空" if str(value or "").lower() == "short" else "做多"


def _cycle_kind_zh(value: str) -> str:
    return "早盘 9-21" if value == "DAY" else "晚盘 21-9"


def _event_label(event: str) -> str:
    return {
        "entry": "开仓",
        "target": "止盈平仓",
        "stop": "止损平仓",
        "flatten": "周期结束平仓",
        "exit": "平仓",
    }.get(event, event or "交易事件")


def _event_reason(event: str) -> str:
    return {
        "target": "止盈",
        "stop": "止损",
        "flatten": "周期结束",
        "exit": "主动平仓",
        "entry": "开仓",
    }.get(event, event or "未知")


def _entry_reason(fill: dict[str, Any]) -> str:
    layer = _layer_label(fill)
    side = "做多" if str(fill.get("side")) == "buy" else "做空"
    rung = int(fill.get("rung") or 0) + 1
    return f"机器轨{layer}触价，{side}第 {rung} 层"


def _layer_label(fill: dict[str, Any]) -> str:
    layer = str(fill.get("layer") or "")
    return "趋势腿" if layer == "trend" else "基础网格"


def _protection_zh(value: Any) -> str:
    return {
        "protected": "已定义止盈止损",
        "exchange_managed": "交易所托管保护单",
        "partial": "保护不完整",
        "missing": "缺少保护",
    }.get(str(value or ""), str(value or "未知"))


def _entry_index(rows: list[dict[str, Any]]) -> int:
    return sum(1 for row in rows if str(row.get("event") or "") == "entry")


def _entry_reward_risk(
    *,
    side: Any,
    entry_price: float | None,
    take_profit: float | None,
    stop_loss: float | None,
    quantity: float,
    entry_cost: float,
    notional: float | None,
    cost_per_side_bp: float,
) -> tuple[float | None, float | None, float | None]:
    if entry_price is None or take_profit is None or stop_loss is None or not quantity:
        return None, None, None
    direction = 1 if str(side or "").lower() == "long" else -1
    exit_cost_estimate = float(notional or 0.0) * float(cost_per_side_bp or 0.0) / 10_000.0
    reward = ((take_profit - entry_price) * direction * quantity) - entry_cost - exit_cost_estimate
    risk = ((stop_loss - entry_price) * direction * quantity) - entry_cost - exit_cost_estimate
    rr = reward / abs(risk) if risk else None
    return round(reward, 4), round(risk, 4), round(rr, 4) if rr is not None else None


def _cost_per_side_bp(config: dict[str, Any]) -> float:
    model = config.get("execution_cost_model") if isinstance(config.get("execution_cost_model"), dict) else {}
    return float(model.get("cost_per_side_bp") or config.get("cost_per_side_bp") or 0.0)


def _order_type_zh(value: Any) -> str:
    order_type = str(value or "").lower()
    return {
        "limit": "限价单",
        "market": "市价单",
        "stop": "止损触发单",
    }.get(order_type, order_type or "缺失")


def _display_path(value: str) -> str:
    try:
        return str(Path(value).resolve().relative_to(ROOT))
    except (OSError, ValueError):
        return value


def _matching_trade_for_exit(trades: list[dict[str, Any]], fill_id: str) -> dict[str, Any] | None:
    for trade in trades:
        if _matching_exit(trade, fill_id):
            return trade
    return None


def _matching_exit(trade: dict[str, Any], fill_id: str) -> dict[str, Any] | None:
    for row in trade.get("exit_fills") or []:
        if str(row.get("fill_id") or "") == fill_id:
            return row
    return None


def _fill_units(fill: dict[str, Any], *, price: float | None, config: dict[str, Any]) -> float:
    direct = _number(fill.get("pnl_units"))
    if direct is not None:
        return direct
    contracts = _number(fill.get("contracts"))
    if contracts is not None:
        model = config.get("execution_cost_model") if isinstance(config.get("execution_cost_model"), dict) else {}
        multiplier = float(model.get("contract_multiplier") or 1.0)
        return round(contracts * multiplier, 10)
    notional = _number(fill.get("notional")) or 0.0
    px = price or _number(fill.get("price")) or 0.0
    return round(notional / px, 10) if px else 0.0


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt_price(value: Any) -> str:
    number = _number(value)
    return "缺失" if number is None else f"{number:.2f}"


def _fmt_number(value: Any) -> str:
    number = _number(value)
    if number is None:
        return "缺失"
    if abs(number) >= 100:
        return f"{number:.2f}"
    return f"{number:.6f}".rstrip("0").rstrip(".")


def _money(value: Any) -> str:
    number = _number(value)
    return "缺失" if number is None else f"${number:,.2f}"


def _signed_money(value: Any) -> str:
    number = _number(value)
    return "缺失" if number is None else f"{number:+.2f}"


def _signed_usd(value: Any) -> str:
    number = _number(value)
    if number is None:
        return "缺失"
    sign = "+" if number >= 0 else "-"
    return f"{sign}${abs(number):,.2f}"


def _fmt_rr(value: Any) -> str:
    number = _number(value)
    return "缺失" if number is None else f"{number:.2f}:1"


def _fmt_leverage(value: Any) -> str:
    number = _number(value)
    return "缺失" if number is None else f"{number:.2f}x"
