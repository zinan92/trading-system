from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.config_loader import load_risk_rules
from services.journal_store import load_json, write_json
from services.trade_ticket_notifier import TradeTicketNotifier


TRACE_GATE_NAMES = (
    "信号闸门",
    "数据质量闸门",
    "盈亏比 / 质量闸门",
    "账户风险闸门",
    "自动批准闸门",
)


class DecisionTrace:
    """Build replayable, per-signal decision traces from persisted artifacts."""

    def __init__(self, output_root: Path) -> None:
        self.output_root = Path(output_root)
        self.notifier = TradeTicketNotifier(self.output_root)
        risk_rules = load_risk_rules()
        self.rules = risk_rules.get("default", {}) if isinstance(risk_rules, dict) else {}

    def build(self, run_date: str, strategy_id: str = "") -> list[dict]:
        signals_path = self.output_root / "signals" / f"{run_date}.json"
        risk_blocks_path = self.output_root / "risk_blocks" / f"{run_date}.json"
        tickets_path = self.output_root / "trade_tickets" / f"{run_date}.json"
        pending_path = self.output_root / "journal_pending" / f"{run_date}.json"
        decisions_path = self.output_root / "journal_decisions" / f"{run_date}.json"
        snapshots_path = self.output_root / "decision_snapshots" / f"{run_date}.json"
        paper_orders_path = self.output_root / "paper_orders" / f"{run_date}.json"
        demo_requests_path = self.output_root / "demo_order_requests" / f"{run_date}.json"
        data_quality_path = self.output_root / "data_quality" / f"{run_date}.json"

        signals = [item for item in load_json(signals_path) if isinstance(item, dict)]
        risk_blocks = [item for item in load_json(risk_blocks_path) if isinstance(item, dict)]
        tickets = [item for item in load_json(tickets_path) if isinstance(item, dict)]
        pending = [item for item in load_json(pending_path) if isinstance(item, dict)]
        decisions = [item for item in load_json(decisions_path) if isinstance(item, dict)]
        snapshots = [item for item in load_json(snapshots_path) if isinstance(item, dict)]
        paper_orders = [item for item in load_json(paper_orders_path) if isinstance(item, dict)]
        demo_requests = [item for item in load_json(demo_requests_path) if isinstance(item, dict)]

        risk_by_signal = self._by_signal(risk_blocks)
        tickets_by_signal = self._by_signal(tickets)
        snapshots_by_signal = self._by_signal(snapshots)
        pending_by_ticket = self._by_ticket(pending)
        decisions_by_ticket = self._by_ticket(decisions)
        decisions_by_signal = self._by_signal(decisions)
        paper_orders_by_ticket = self._by_ticket(paper_orders)
        paper_orders_by_signal = self._by_signal(paper_orders)
        demo_requests_by_ticket = self._demo_requests_by_ticket(demo_requests)
        data_quality = self._load_mapping(data_quality_path)

        source_artifacts = {
            "signals": str(signals_path),
            "risk_blocks": str(risk_blocks_path),
            "trade_tickets": str(tickets_path),
            "journal_decisions": str(decisions_path),
            "decision_snapshots": str(snapshots_path),
            "data_quality": str(data_quality_path),
        }
        total = len([item for item in signals if self._is_traceable(item, risk_by_signal)])
        records: list[dict] = []
        sequence = 0
        for signal in signals:
            if not self._is_traceable(signal, risk_by_signal):
                continue
            sequence += 1
            signal_id = str(signal.get("signal_id") or "")
            ticket = tickets_by_signal.get(signal_id, {})
            ticket_id = str(ticket.get("ticket_id") or "")
            risk_block = risk_by_signal.get(signal_id, {})
            snapshot = snapshots_by_signal.get(signal_id, {})
            decision = decisions_by_ticket.get(ticket_id, {}) if ticket_id else decisions_by_signal.get(signal_id, {})
            paper_order = paper_orders_by_ticket.get(ticket_id, {}) if ticket_id else paper_orders_by_signal.get(signal_id, {})
            demo_request = demo_requests_by_ticket.get(ticket_id, {}) if ticket_id else {}
            context = {
                "pending": pending_by_ticket.get(ticket_id, {}) if ticket_id else {},
                "decision": decision,
                "paper_order": paper_order,
                "demo_request": demo_request,
                "strategy_root": str(self.output_root),
            }
            records.append(
                self._record(
                    run_date=run_date,
                    strategy_id=strategy_id or str(snapshot.get("strategy_id") or ""),
                    signal=signal,
                    ticket=ticket,
                    risk_block=risk_block,
                    snapshot=snapshot,
                    context=context,
                    data_quality=self._data_quality_for_signal(signal, ticket, data_quality),
                    sequence=sequence,
                    total=max(total, 1),
                    source_artifacts=source_artifacts,
                )
            )

        trace_path = self.output_root / "decision_traces" / f"{run_date}.json"
        merged = self._merge(load_json(trace_path), records)
        write_json(trace_path, merged)
        write_json(self.output_root / "decision_traces" / "current.json", merged)
        return merged

    def _record(
        self,
        *,
        run_date: str,
        strategy_id: str,
        signal: dict,
        ticket: dict,
        risk_block: dict,
        snapshot: dict,
        context: dict,
        data_quality: dict,
        sequence: int,
        total: int,
        source_artifacts: dict,
    ) -> dict:
        if ticket:
            gates = self._with_data_quality_gate(
                self._ticket_gates(run_date, strategy_id, ticket, context, sequence, total),
                data_quality,
            )
            outcome = self._ticket_outcome(context)
            reason = self._ticket_outcome_reason(context)
        else:
            gates = self._filtered_gates(signal, risk_block, data_quality)
            outcome = "filtered"
            reason = self._filtered_reason(risk_block, gates)
        first_fail_step = self._first_fail_step(gates)
        signal_id = str(signal.get("signal_id") or ticket.get("signal_id") or "")
        evaluated_at = self._evaluated_at(signal, ticket, snapshot, context)
        return {
            "run_date": run_date,
            "strategy_id": strategy_id,
            "cycle_id": self._cycle_id(run_date, strategy_id, signal_id, evaluated_at, snapshot),
            "evaluated_at": evaluated_at,
            "asset": signal.get("asset") or ticket.get("asset", ""),
            "signal_id": signal_id,
            "signal": {
                "regime": signal.get("regime") or ticket.get("signal_regime", ""),
                "strength": signal.get("strength") if signal.get("strength") not in (None, "") else ticket.get("signal_strength"),
                "confidence": signal.get("confidence") if signal.get("confidence") not in (None, "") else ticket.get("signal_confidence"),
                "close": self._close(signal, ticket, snapshot),
            },
            "gates": gates,
            "first_fail_step": first_fail_step,
            "outcome": outcome,
            "outcome_reason": reason,
            "source_artifacts": source_artifacts,
        }

    def _ticket_gates(
        self,
        run_date: str,
        strategy_id: str,
        ticket: dict,
        context: dict,
        sequence: int,
        total: int,
    ) -> list[dict]:
        view = self.notifier._build_view(run_date, strategy_id, ticket, context, sequence, total)
        return [dict(item) for item in view.get("flow", []) if isinstance(item, dict)]

    def _with_data_quality_gate(self, gates: list[dict], data_quality: dict) -> list[dict]:
        if not gates:
            return [self._data_quality_gate(data_quality, step=1, prior_failed=False)]
        result = [dict(gates[0], step=1, name=TRACE_GATE_NAMES[0])]
        prior_failed = result[0].get("passed") is False
        result.append(self._data_quality_gate(data_quality, step=2, prior_failed=prior_failed))
        for gate in gates[1:]:
            original_step = int(gate.get("step") or len(result))
            mapped = dict(gate)
            mapped["step"] = original_step + 1
            if mapped["step"] - 1 < len(TRACE_GATE_NAMES):
                mapped["name"] = TRACE_GATE_NAMES[mapped["step"] - 1]
            result.append(mapped)
        return result

    def _filtered_gates(self, signal: dict, risk_block: dict, data_quality: dict) -> list[dict]:
        signal_gate = self._signal_gate(signal, risk_block)
        quality = risk_block.get("trade_quality") if isinstance(risk_block.get("trade_quality"), dict) else {}
        market_data_gate = risk_block.get("market_data_gate") if isinstance(risk_block.get("market_data_gate"), dict) else {}
        signal_passed = bool(signal_gate.get("passes"))
        fail_step = self._filtered_fail_step(signal_gate, quality, market_data_gate, risk_block, data_quality)
        gates = [
            {
                "step": 1,
                "name": TRACE_GATE_NAMES[0],
                "passed": signal_passed,
                "detail": self._signal_detail(signal, signal_gate),
            },
            self._data_quality_gate(data_quality, step=2, prior_failed=not signal_passed),
            {
                "step": 3,
                "name": TRACE_GATE_NAMES[2],
                "passed": self._step_passed(3, fail_step),
                "detail": self._quality_detail(quality, market_data_gate, fail_step),
            },
            {
                "step": 4,
                "name": TRACE_GATE_NAMES[3],
                "passed": self._step_passed(4, fail_step),
                "detail": self._account_risk_detail(quality, risk_block, fail_step),
            },
            {
                "step": 5,
                "name": TRACE_GATE_NAMES[4],
                "passed": self._step_passed(5, fail_step),
                "detail": self._auto_gate_detail(risk_block, fail_step),
            },
        ]
        return gates

    def _filtered_fail_step(self, signal_gate: dict, quality: dict, market_data_gate: dict, risk_block: dict, data_quality: dict) -> int:
        if signal_gate.get("passes") is False:
            return 1
        if self._data_quality_blocks(data_quality) or risk_block.get("data_quality") or market_data_gate.get("passes") is False:
            return 2
        if quality and quality.get("passes") is False:
            return 4 if self._quality_is_account_risk_failure(quality) else 3
        if risk_block.get("portfolio_risk"):
            return 4
        if risk_block:
            return 5
        return 3

    def _step_passed(self, step: int, fail_step: int) -> bool | None:
        if step < fail_step:
            return True
        if step == fail_step:
            return False
        return None

    def _signal_gate(self, signal: dict, risk_block: dict) -> dict:
        existing = risk_block.get("signal_gate") if isinstance(risk_block.get("signal_gate"), dict) else {}
        if existing:
            return existing
        if risk_block.get("data_quality"):
            return {
                "passes": True,
                "direction": signal.get("direction"),
                "strength": signal.get("strength"),
                "confidence": signal.get("confidence"),
                "min_signal_strength": self.rules.get("min_signal_strength", 60),
                "min_confidence": self.rules.get("min_confidence", 55),
                "detail": "原始信号进入质量检查；数据质量在下一闸门判定",
                "reasons": [],
            }
        min_strength = int(self.rules.get("min_signal_strength", 60) or 60)
        min_confidence = int(self.rules.get("min_confidence", 55) or 55)
        direction = str(signal.get("direction") or "")
        strength = int(float(signal.get("strength") or 0))
        confidence = int(float(signal.get("confidence") or 0))
        direction_passes = direction in {"long", "short"}
        strength_passes = strength >= min_strength
        confidence_passes = confidence >= min_confidence
        return {
            "passes": direction_passes and strength_passes and confidence_passes,
            "direction": direction,
            "strength": strength,
            "confidence": confidence,
            "min_signal_strength": min_strength,
            "min_confidence": min_confidence,
            "direction_passes": direction_passes,
            "strength_passes": strength_passes,
            "confidence_passes": confidence_passes,
            "reasons": [],
        }

    def _data_quality_gate(self, data_quality: dict, *, step: int, prior_failed: bool) -> dict:
        if prior_failed:
            return {"step": step, "name": TRACE_GATE_NAMES[1], "passed": None, "detail": "未评估（前置已失败）"}
        if not data_quality:
            return {"step": step, "name": TRACE_GATE_NAMES[1], "passed": None, "detail": "未找到数据质量产物"}
        if data_quality.get("enabled") is False:
            return {"step": step, "name": TRACE_GATE_NAMES[1], "passed": True, "detail": "数据质量闸门已关闭"}
        passed = data_quality.get("allows_trading") is not False and not data_quality.get("reasons")
        return {
            "step": step,
            "name": TRACE_GATE_NAMES[1],
            "passed": passed,
            "detail": self._data_quality_detail(data_quality),
        }

    def _data_quality_detail(self, data_quality: dict) -> str:
        if self._data_quality_blocks(data_quality):
            return self._join_reasons(data_quality) or "数据质量不允许交易"
        clean_rows = data_quality.get("clean_rows")
        missing = self._pct(data_quality.get("missing_ratio"))
        synthetic = self._pct(data_quality.get("synthetic_ratio"))
        provider = data_quality.get("latest_provider") or "未知来源"
        return f"{self.notifier._fmt(clean_rows)} 根 K 线　·　缺失 {missing}　·　合成 {synthetic}　·　{provider}"

    def _data_quality_blocks(self, data_quality: dict) -> bool:
        return bool(data_quality) and data_quality.get("allows_trading") is False

    def _signal_detail(self, signal: dict, signal_gate: dict) -> str:
        if signal_gate.get("detail"):
            return str(signal_gate["detail"])
        strength = signal_gate.get("strength", signal.get("strength"))
        confidence = signal_gate.get("confidence", signal.get("confidence"))
        min_strength = signal_gate.get("min_signal_strength") or self.rules.get("min_signal_strength", 60)
        min_confidence = signal_gate.get("min_confidence") or self.rules.get("min_confidence", 55)
        return (
            f"强度 {strength if strength not in (None, '') else '—'} ≥ {self.notifier._fmt(min_strength)}　·　"
            f"置信 {confidence if confidence not in (None, '') else '—'} ≥ {self.notifier._fmt(min_confidence)}　·　"
            f"方向 {self._direction_label(signal_gate.get('direction') or signal.get('direction'))}"
        )

    def _quality_detail(self, quality: dict, market_data_gate: dict, fail_step: int) -> str:
        if fail_step < 3:
            return "未评估（前置已失败）"
        if market_data_gate.get("passes") is False:
            return self._join_reasons(market_data_gate) or "行情数据不可用"
        min_rr = self.rules.get("risk_reward_min", 1.5)
        if quality:
            return (
                f"盈亏比 {self.notifier._fmt(quality.get('reward_to_risk'))} ≥ {self.notifier._fmt(min_rr)}　·　"
                f"目标涨幅 +{self.notifier._fmt(quality.get('target_price_move_pct'))}%"
            )
        if fail_step == 3:
            return "未生成开单票"
        return "盈亏比 无 ≥ " + self.notifier._fmt(min_rr) + "　·　目标涨幅 +无%"

    def _account_risk_detail(self, quality: dict, risk_block: dict, fail_step: int) -> str:
        if fail_step < 4:
            return "未评估（前置已失败）"
        stop = quality.get("estimated_account_stop_risk_pct")
        cap = quality.get("max_loss_pct") or risk_block.get("max_loss_pct") or self.rules.get("max_loss_pct")
        if stop not in (None, "") and cap not in (None, ""):
            stop_num = self._num(stop)
            cap_num = self._num(cap)
            comparator = "≤" if stop_num is not None and cap_num is not None and stop_num <= cap_num else ">"
            return f"账户止损 {self.notifier._fmt(stop)}% {comparator} 单笔上限 {self.notifier._fmt(cap)}%"
        if risk_block.get("portfolio_risk"):
            risk = risk_block.get("portfolio_risk") if isinstance(risk_block.get("portfolio_risk"), dict) else {}
            projected = risk.get("projected_loss_pct")
            cap = risk.get("daily_loss_stop_pct")
            if projected not in (None, "") and cap not in (None, ""):
                return f"组合风险 {self.notifier._fmt(projected)}% > 日内上限 {self.notifier._fmt(cap)}%"
        return "账户止损 — ? 单笔上限 —"

    def _auto_gate_detail(self, risk_block: dict, fail_step: int) -> str:
        if fail_step < 5:
            return "未评估（前置已失败）"
        return str(risk_block.get("reason") or self._join_reasons(risk_block) or "被安全闸门拦下")

    def _quality_is_account_risk_failure(self, quality: dict) -> bool:
        stop = self._num(quality.get("estimated_account_stop_risk_pct"))
        cap = self._num(quality.get("max_loss_pct"))
        if stop is not None and cap is not None and stop > cap:
            return True
        reasons = " ".join(str(item) for item in quality.get("reasons", []) if item)
        return "estimated account stop risk" in reasons or "max loss" in reasons

    def _ticket_outcome(self, context: dict) -> str:
        if self.notifier._is_executed(context):
            return "executed"
        decision = context.get("decision") if isinstance(context.get("decision"), dict) else {}
        demo_request = context.get("demo_request") if isinstance(context.get("demo_request"), dict) else {}
        if str(decision.get("decision_status") or "") in {"rejected", "skipped"} or self.notifier._execution_block_reason(demo_request):
            return "rejected"
        pending = context.get("pending") if isinstance(context.get("pending"), dict) else {}
        if pending or not decision:
            return "pending"
        return "rejected"

    def _ticket_outcome_reason(self, context: dict) -> str:
        decision = context.get("decision") if isinstance(context.get("decision"), dict) else {}
        if decision.get("notes"):
            return str(decision.get("notes"))
        demo_request = context.get("demo_request") if isinstance(context.get("demo_request"), dict) else {}
        block = self.notifier._execution_block_reason(demo_request)
        if block:
            return block
        if self.notifier._is_executed(context):
            return "executed"
        if self._ticket_outcome(context) == "pending":
            return "ticket reached auto gate but has no executed or rejected decision evidence"
        return "ticket reached terminal rejection or remains unresolved"

    def _filtered_reason(self, risk_block: dict, gates: list[dict]) -> str:
        if risk_block.get("reason"):
            return str(risk_block.get("reason"))
        for gate in gates:
            if gate.get("passed") is False:
                detail = str(gate.get("detail") or "")
                return detail if detail != "未生成开单票" else "ticket was not generated"
        return "ticket was not generated"

    def _first_fail_step(self, gates: list[dict]) -> int | None:
        for gate in gates:
            if gate.get("passed") is False:
                return int(gate.get("step"))
        return None

    def _evaluated_at(self, signal: dict, ticket: dict, snapshot: dict, context: dict) -> str:
        decision = context.get("decision") if isinstance(context.get("decision"), dict) else {}
        for value in (
            snapshot.get("bar_timestamp"),
            signal.get("generated_at"),
            ticket.get("generated_at"),
            decision.get("decided_at"),
        ):
            if value:
                return str(value)
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    def _cycle_id(self, run_date: str, strategy_id: str, signal_id: str, evaluated_at: str, snapshot: dict) -> str:
        timeframe = str(snapshot.get("timeframe") or "")
        if timeframe and evaluated_at:
            return f"{run_date}|{strategy_id}|{timeframe}|{evaluated_at}"
        return f"{run_date}|{strategy_id}|{signal_id or evaluated_at}"

    def _close(self, signal: dict, ticket: dict, snapshot: dict) -> Any:
        for value in (
            signal.get("close"),
            signal.get("latest_price"),
            snapshot.get("price"),
            ticket.get("latest_price"),
        ):
            if value not in (None, ""):
                return value
        return None

    def _merge(self, existing: list[dict], records: list[dict]) -> list[dict]:
        merged: list[dict] = []
        index: dict[str, int] = {}
        for item in [*(existing if isinstance(existing, list) else []), *records]:
            if not isinstance(item, dict):
                continue
            key = self._merge_key(item)
            if key in index:
                merged[index[key]] = item
            else:
                index[key] = len(merged)
                merged.append(item)
        return merged

    def _merge_key(self, item: dict) -> str:
        strategy_id = str(item.get("strategy_id") or "")
        signal_id = str(item.get("signal_id") or "")
        cycle_id = str(item.get("cycle_id") or "")
        return f"{strategy_id}|{signal_id}|{cycle_id}"

    def _by_signal(self, rows: list[dict]) -> dict[str, dict]:
        result: dict[str, dict] = {}
        for row in rows:
            signal_id = str(row.get("signal_id") or "")
            if signal_id:
                result[signal_id] = row
        return result

    def _by_ticket(self, rows: list[dict]) -> dict[str, dict]:
        result: dict[str, dict] = {}
        for row in rows:
            ticket_id = str(row.get("ticket_id") or "")
            if ticket_id:
                result[ticket_id] = row
        return result

    def _demo_requests_by_ticket(self, rows: list[dict]) -> dict[str, dict]:
        result: dict[str, dict] = {}
        for row in rows:
            ticket = row.get("ticket", {}) if isinstance(row.get("ticket"), dict) else {}
            ticket_id = str(ticket.get("ticket_id") or row.get("ticket_id") or "")
            if ticket_id:
                result[ticket_id] = row
        return result

    def _is_traceable(self, signal: dict, risk_by_signal: dict[str, dict]) -> bool:
        signal_id = str(signal.get("signal_id") or "")
        return str(signal.get("direction") or "") in {"long", "short"} or bool(risk_by_signal.get(signal_id))

    def _data_quality_for_signal(self, signal: dict, ticket: dict, data_quality: dict) -> dict:
        asset = str(signal.get("asset") or ticket.get("asset") or "")
        row = data_quality.get(asset) if isinstance(data_quality, dict) else {}
        return row if isinstance(row, dict) else {}

    def _load_mapping(self, path: Path) -> dict:
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if isinstance(payload, dict):
            return payload
        if isinstance(payload, list) and payload and isinstance(payload[-1], dict):
            return payload[-1]
        return {}

    def _direction_label(self, value) -> str:
        direction = str(value or "").lower()
        if direction == "long":
            return "做多"
        if direction == "short":
            return "做空"
        return "方向未知"

    def _join_reasons(self, payload: dict) -> str:
        reasons = payload.get("reasons", []) if isinstance(payload, dict) else []
        if isinstance(reasons, list):
            return "；".join(str(item) for item in reasons if item)
        return str(reasons or "")

    def _pct(self, value) -> str:
        number = self._num(value)
        if number is None:
            return "未知"
        return f"{number * 100:.1f}%"

    def _num(self, value) -> float | None:
        if value in (None, ""):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
