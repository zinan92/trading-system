"""Trade lifecycle timelines, explainability gaps, and review event streams."""

from __future__ import annotations


from services.trade_record_card import TradeRecordCardBuilder


class TradeLifecycleMixin:
    def _decorated_trades(
        self,
        open_trades: list[dict],
        closed_trades: list[dict],
        signals: list[dict],
        tickets: list[dict],
        decisions: list[dict],
        risk_blocks: list[dict],
        orders: list[dict],
        paper_exit_decisions: dict,
        risk_monitor: dict,
        review_events: list[dict],
        strategy_id: str = "",
        strategy_config: dict | None = None,
        run_date: str = "",
        latest_price: float | None = None,
    ) -> list[dict]:
        signal_by_id = {item.get("signal_id"): item for item in signals if isinstance(item, dict)}
        ticket_by_id = {item.get("ticket_id"): item for item in tickets if isinstance(item, dict)}
        decision_by_ticket = {item.get("ticket_id"): item for item in decisions if isinstance(item, dict)}
        risk_by_ticket = {item.get("ticket_id"): item for item in risk_blocks if isinstance(item, dict)}
        order_by_id = {item.get("order_id"): item for item in orders if isinstance(item, dict)}
        order_by_ticket = {}
        for item in orders:
            if isinstance(item, dict) and item.get("ticket_id") and item.get("ticket_id") not in order_by_ticket:
                order_by_ticket[item.get("ticket_id")] = item
        exit_items = paper_exit_decisions.get("queue", []) if isinstance(paper_exit_decisions, dict) else []
        exit_by_trade = {item.get("trade_id"): item for item in exit_items if isinstance(item, dict)}
        out = []
        for trade in [*(open_trades or []), *(closed_trades or [])]:
            if not isinstance(trade, dict):
                continue
            ticket = ticket_by_id.get(trade.get("ticket_id"), {})
            signal = signal_by_id.get(trade.get("signal_id"), {})
            decision = decision_by_ticket.get(trade.get("ticket_id"), {})
            risk_block = risk_by_ticket.get(trade.get("ticket_id"), {})
            embedded_order = decision.get("paper_order") if isinstance(decision.get("paper_order"), dict) else {}
            order = order_by_id.get(trade.get("order_id"), {}) or order_by_ticket.get(trade.get("ticket_id"), {}) or embedded_order
            exit_item = exit_by_trade.get(trade.get("trade_id"), {})
            entry_reason = (
                ticket.get("rationale")
                or signal.get("thesis")
                or trade.get("signal_regime")
                or decision.get("notes")
                or ""
            )
            lifecycle = self._trade_lifecycle(
                trade=trade,
                signal=signal,
                ticket=ticket,
                decision=decision,
                risk_block=risk_block,
                order=order,
                exit_item=exit_item,
                risk_monitor=risk_monitor,
                review_events=review_events,
            )
            enriched = {
                **trade,
                "strategy_id": strategy_id or trade.get("strategy_id", ""),
                "entry_reason": entry_reason,
                "exit_reason": trade.get("exit_reason") or exit_item.get("exit_reason", ""),
                "strategy_signal": signal,
                "ticket": ticket,
                "risk_block": risk_block,
                "decision": decision,
                "order": order,
                "exit_decision": exit_item,
                "lifecycle": lifecycle,
            }
            enriched["record_card"] = TradeRecordCardBuilder(
                run_date=run_date or self._date_from_timestamp(trade.get("opened_at")) or "",
                strategy_id=strategy_id,
                strategy_config=strategy_config or {},
                latest_price=latest_price,
            ).build(enriched)
            out.append(enriched)
        return out

    def _trade_lifecycle(
        self,
        *,
        trade: dict,
        signal: dict,
        ticket: dict,
        decision: dict,
        risk_block: dict,
        order: dict,
        exit_item: dict,
        risk_monitor: dict,
        review_events: list[dict],
    ) -> list[dict]:
        ticket_id = str(trade.get("ticket_id") or "")
        signal_id = str(trade.get("signal_id") or "")
        related_review_events = [
            item
            for item in review_events
            if item.get("ticket_id") == ticket_id or item.get("signal_id") == signal_id
        ]
        risk_snapshot = decision.get("risk_snapshot", {}) if isinstance(decision.get("risk_snapshot"), dict) else {}
        if risk_block:
            gate_event = self._lifecycle_event(
                "gate",
                True,
                risk_block.get("status", "block"),
                risk_block.get("generated_at", ""),
                risk_block.get("reason", "risk gate blocked the candidate"),
                "risk_blocks",
            )
        elif risk_snapshot:
            gate_event = self._lifecycle_event(
                "gate",
                True,
                "passed",
                decision.get("decided_at") or decision.get("created_at", ""),
                "max_loss_pct={max_loss_pct} daily_loss_stop_pct={daily_loss_stop_pct}".format(
                    max_loss_pct=risk_snapshot.get("max_loss_pct", "unknown"),
                    daily_loss_stop_pct=risk_snapshot.get("daily_loss_stop_pct", "unknown"),
                ),
                "journal_decisions.risk_snapshot",
            )
        elif risk_monitor:
            gate_event = self._lifecycle_event(
                "gate",
                True,
                risk_monitor.get("status", "unknown"),
                risk_monitor.get("generated_at", ""),
                f"kill_switch={risk_monitor.get('kill_switch_active', 'unknown')}",
                "risk_monitor",
            )
        else:
            gate_event = self._lifecycle_event("gate", False, "missing", "", "no gate snapshot found", "")

        order_summary = "order artifact missing"
        if order:
            order_summary = "fill_price={fill_price} quantity={quantity}".format(
                fill_price=order.get("fill_price", order.get("requested_price", "unknown")),
                quantity=order.get("quantity", "unknown"),
            )
        position_summary = "{side} qty={quantity} entry={entry} TP={target} SL={stop}".format(
            side=trade.get("side", ""),
            quantity=trade.get("quantity", "unknown"),
            entry=trade.get("entry_price", "unknown"),
            target=trade.get("target", "unknown"),
            stop=trade.get("stop_loss", "unknown"),
        )
        closed = bool(trade.get("closed_at") or trade.get("status") == "closed")
        if closed:
            exit_event = self._lifecycle_event(
                "exit",
                True,
                "closed",
                trade.get("closed_at", ""),
                "{reason} pnl={pnl}".format(
                    reason=trade.get("exit_reason") or exit_item.get("exit_reason", "closed"),
                    pnl=trade.get("realized_pnl", "unknown"),
                ),
                "paper_trades.closed",
            )
        elif exit_item:
            exit_event = self._lifecycle_event(
                "exit",
                True,
                exit_item.get("required_user_action") or exit_item.get("exit_type", "review"),
                exit_item.get("latest_timestamp", ""),
                "{exit_type}: {reason}".format(
                    exit_type=exit_item.get("exit_type", "exit_review"),
                    reason=exit_item.get("exit_reason", ""),
                ),
                "paper_exit_decisions",
            )
        else:
            exit_event = self._lifecycle_event("exit", False, "open", "", "open trade; no exit decision artifact", "")

        review_summary = "; ".join(
            f"{item.get('type', 'event')}:{item.get('status', '')}" for item in related_review_events[:4]
        )
        return [
            self._lifecycle_event(
                "signal",
                bool(signal),
                signal.get("status") or ("present" if signal else "missing_artifact"),
                signal.get("generated_at", ""),
                signal.get("thesis") or trade.get("signal_regime") or "signal artifact missing",
                "signals",
            ),
            self._lifecycle_event(
                "ticket",
                bool(ticket),
                ticket.get("status") or ticket.get("verdict") or ("created" if ticket else "missing_artifact"),
                ticket.get("created_at") or ticket.get("generated_at", ""),
                ticket.get("rationale") or ticket.get("trigger") or "ticket artifact missing",
                "trade_tickets",
            ),
            gate_event,
            self._lifecycle_event(
                "order",
                bool(order),
                order.get("status", "missing_artifact") if order else "missing_artifact",
                order.get("filled_at") or order.get("submitted_at") or decision.get("decided_at", ""),
                order_summary,
                "paper_orders",
            ),
            self._lifecycle_event(
                "position",
                True,
                trade.get("status", "open"),
                trade.get("opened_at", ""),
                position_summary,
                "paper_trades",
            ),
            exit_event,
            self._lifecycle_event(
                "review",
                bool(related_review_events),
                f"{len(related_review_events)} events" if related_review_events else "missing",
                related_review_events[-1].get("timestamp", "") if related_review_events else "",
                review_summary or "no related review event",
                "review_events",
            ),
        ]

    def _lifecycle_event(self, stage: str, present: bool, status: str, timestamp: str, summary: str, source: str) -> dict:
        return {
            "stage": stage,
            "present": bool(present),
            "status": status or "",
            "timestamp": timestamp or "",
            "summary": summary or "",
            "source": source or "",
        }

    def _explainability_gaps(
        self,
        trades: list[dict],
        signals: list[dict],
        tickets: list[dict],
        orders: list[dict],
        decisions: list[dict],
    ) -> list[dict]:
        gaps = []
        signal_ids = {item.get("signal_id") for item in signals if isinstance(item, dict)}
        ticket_ids = {item.get("ticket_id") for item in tickets if isinstance(item, dict)}
        trade_order_ids = {item.get("order_id") for item in trades if isinstance(item, dict)}
        trade_ticket_ids = {item.get("ticket_id") for item in trades if isinstance(item, dict)}
        order_ids = {item.get("order_id") for item in orders if isinstance(item, dict)}
        order_ticket_ids = {item.get("ticket_id") for item in orders if isinstance(item, dict)}

        for trade in trades:
            if not isinstance(trade, dict):
                continue
            trade_id = trade.get("trade_id", "")
            if not trade.get("entry_reason"):
                gaps.append(self._gap("missing_entry_reason", "warn", "trade has no entry reason", trade_id, trade.get("ticket_id", ""), trade.get("signal_id", "")))
            if trade.get("signal_id") and trade.get("signal_id") not in signal_ids:
                gaps.append(self._gap("missing_signal_artifact", "warn", "trade references a signal not present in trade trace artifacts", trade_id, trade.get("ticket_id", ""), trade.get("signal_id", "")))
            if trade.get("ticket_id") and trade.get("ticket_id") not in ticket_ids:
                gaps.append(self._gap("missing_ticket_artifact", "warn", "trade references a ticket not present in trade trace artifacts", trade_id, trade.get("ticket_id", ""), trade.get("signal_id", "")))
            if (trade.get("order_id") or trade.get("ticket_id")) and not trade.get("order"):
                gaps.append(self._gap("missing_order_artifact", "warn", "trade has no matching paper order artifact", trade_id, trade.get("ticket_id", ""), trade.get("signal_id", "")))
            if (trade.get("closed_at") or trade.get("status") == "closed") and not trade.get("exit_reason"):
                gaps.append(self._gap("missing_exit_reason", "warn", "closed trade has no exit reason", trade_id, trade.get("ticket_id", ""), trade.get("signal_id", "")))
            gate = next((item for item in trade.get("lifecycle", []) if item.get("stage") == "gate"), {})
            if not gate.get("present"):
                gaps.append(self._gap("missing_gate_context", "info", "trade has no risk gate snapshot", trade_id, trade.get("ticket_id", ""), trade.get("signal_id", "")))

        for order in orders:
            if not isinstance(order, dict):
                continue
            status = str(order.get("status") or "").lower()
            if status not in {"filled", "executed", "submitted"}:
                continue
            if order.get("order_id") not in trade_order_ids and order.get("ticket_id") not in trade_ticket_ids:
                gaps.append(self._gap("filled_order_without_trade", "warn", "filled order has no matching trade record", "", order.get("ticket_id", ""), ""))

        for decision in decisions:
            if not isinstance(decision, dict) or decision.get("decision_status") not in {"executed", "executed_paper"}:
                continue
            embedded = decision.get("paper_order") if isinstance(decision.get("paper_order"), dict) else {}
            decision_order_id = embedded.get("order_id") or decision.get("order_id")
            if decision_order_id and decision_order_id not in order_ids:
                gaps.append(self._gap("executed_decision_without_order_artifact", "warn", "executed decision embeds an order missing from paper_orders", "", decision.get("ticket_id", ""), decision.get("signal_id", "")))
            if decision.get("ticket_id") and decision.get("ticket_id") not in order_ticket_ids and decision.get("ticket_id") not in trade_ticket_ids:
                gaps.append(self._gap("executed_decision_without_trade", "warn", "executed decision has no matching order or trade", "", decision.get("ticket_id", ""), decision.get("signal_id", "")))

        for signal in signals:
            if not isinstance(signal, dict):
                continue
            if signal.get("direction") in {"long", "short"} and signal.get("signal_id") not in {item.get("signal_id") for item in tickets if isinstance(item, dict)}:
                gaps.append(self._gap("directional_signal_without_ticket", "info", "directional signal did not produce a same-day ticket", "", "", signal.get("signal_id", "")))
        return gaps

    def _gap(self, gap_type: str, severity: str, message: str, trade_id: str, ticket_id: str, signal_id: str) -> dict:
        return {
            "type": gap_type,
            "severity": severity,
            "message": message,
            "trade_id": trade_id or "",
            "ticket_id": ticket_id or "",
            "signal_id": signal_id or "",
        }

    def _latest_entry_reason(self, signals: list[dict], tickets: list[dict], decisions: list[dict]) -> str:
        if tickets:
            return tickets[-1].get("rationale") or tickets[-1].get("trigger") or ""
        if signals:
            return signals[-1].get("thesis", "")
        if decisions:
            return decisions[-1].get("notes", "")
        return ""

    def _latest_exit_reason(self, trades: list[dict], paper_exit_decisions: dict) -> str:
        for trade in reversed(trades):
            if trade.get("exit_reason"):
                return str(trade["exit_reason"])
        queue = paper_exit_decisions.get("queue", []) if isinstance(paper_exit_decisions, dict) else []
        if queue:
            item = queue[0]
            return item.get("exit_type") or item.get("exit_reason", "")
        return ""

    def _review_events(self, signals: list[dict], decisions: list[dict], risk_blocks: list[dict]) -> list[dict]:
        events = []
        for signal in signals:
            if not isinstance(signal, dict):
                continue
            events.append({
                "type": "signal",
                "signal_id": signal.get("signal_id", ""),
                "ticket_id": "",
                "timestamp": signal.get("generated_at", ""),
                "status": signal.get("status", ""),
                "direction": signal.get("direction", ""),
                "summary": signal.get("thesis", ""),
                "counts_as_trade_sample": False,
            })
        for decision in decisions:
            if not isinstance(decision, dict):
                continue
            executed = decision.get("decision_status") in {"executed", "executed_paper"}
            events.append({
                "type": "decision",
                "signal_id": decision.get("signal_id", ""),
                "ticket_id": decision.get("ticket_id", ""),
                "timestamp": decision.get("decided_at") or decision.get("created_at", ""),
                "status": decision.get("decision_status", ""),
                "summary": decision.get("notes", ""),
                "counts_as_trade_sample": executed,
            })
        for block in risk_blocks:
            if not isinstance(block, dict):
                continue
            events.append({
                "type": "risk_block",
                "signal_id": block.get("signal_id", ""),
                "ticket_id": block.get("ticket_id", ""),
                "timestamp": block.get("generated_at", ""),
                "status": block.get("status", "block"),
                "summary": block.get("reason", ""),
                "counts_as_trade_sample": False,
            })
        return sorted(events, key=lambda item: item.get("timestamp") or "")
