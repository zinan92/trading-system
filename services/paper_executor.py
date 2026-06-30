from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from schemas.market_data import PaperOrder, PaperPosition
from services.config_loader import load_risk_rules
from services.config_loader import load_pipeline_config
from services.journal_store import load_json, write_json
from services.order_lifecycle import IllegalOrderTransition, OrderLifecycleStore


class PaperExecutor:
    def __init__(self, output_root: Path, account_equity: float | None = None) -> None:
        self.output_root = output_root
        if account_equity is None:
            account_equity = float(load_pipeline_config().get("paper_account", {}).get("starting_equity", 10_000.0))
        self.account_equity = account_equity
        self.cost_rules = load_risk_rules().get("default", {}).get("paper_execution_costs", {})
        self.lifecycle = OrderLifecycleStore(output_root)

    def execute_ticket(
        self,
        run_date: str,
        ticket: dict,
        latest_price: float | None = None,
        actual_size: float | None = None,
    ) -> PaperOrder:
        requested_price = latest_price or self._entry_midpoint(ticket["entry_zone"])
        entry_low, entry_high = self._entry_bounds(ticket["entry_zone"])
        order_type = ticket.get("order_type", "limit")
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        order_id = self._order_id(ticket["ticket_id"], run_date, requested_price)
        if order_type == "market" or entry_low <= requested_price <= entry_high:
            status = "filled"
            fill_price = self._effective_entry_price(ticket, requested_price)
            rejection_reason = ""
        else:
            status = "pending"
            fill_price = None
            rejection_reason = "latest price is outside entry zone"

        quantity = actual_size or self._quantity(ticket, fill_price or requested_price)
        costs = self._entry_costs(ticket, requested_price, fill_price, quantity) if fill_price is not None else {}
        existing = self._existing_order(run_date, order_id)
        if existing:
            return PaperOrder(
                order_id=str(existing["order_id"]),
                ticket_id=str(existing["ticket_id"]),
                status=str(existing["status"]),
                requested_price=float(existing["requested_price"]),
                fill_price=float(existing["fill_price"]) if existing.get("fill_price") is not None else None,
                quantity=float(existing["quantity"]),
                filled_at=str(existing.get("filled_at", "")),
                rejection_reason=str(existing.get("rejection_reason", "")),
                gross_fill_price=float(existing["gross_fill_price"]) if existing.get("gross_fill_price") is not None else None,
                slippage_cost=float(existing.get("slippage_cost", 0)),
                spread_cost=float(existing.get("spread_cost", 0)),
                commission=float(existing.get("commission", 0)),
                total_cost=float(existing.get("total_cost", 0)),
                cost_model=existing.get("cost_model", {}),
            )
        lifecycle, _ = self.lifecycle.write_intent(
            run_date,
            order_id=order_id,
            ticket_id=ticket["ticket_id"],
            idempotency_key=order_id,
            requested_quantity=quantity,
            requested_price=requested_price,
            source="paper_executor",
            metadata={"order_type": order_type, "asset": ticket.get("asset", "GOLD")},
        )
        self._transition_lifecycle(run_date, order_id, "submitting", reason="paper_submit_started")
        self._transition_lifecycle(run_date, order_id, "accepted", reason="paper_order_accepted", metadata={"previous_state": lifecycle.get("state")})
        order = PaperOrder(
            order_id=order_id,
            ticket_id=ticket["ticket_id"],
            status=status,
            requested_price=round(requested_price, 4),
            fill_price=round(fill_price, 4) if fill_price is not None else None,
            quantity=round(quantity, 6),
            filled_at=now if status == "filled" else "",
            rejection_reason=rejection_reason,
            gross_fill_price=round(requested_price, 4) if fill_price is not None else None,
            slippage_cost=round(float(costs.get("slippage_cost", 0)), 4),
            spread_cost=round(float(costs.get("spread_cost", 0)), 4),
            commission=round(float(costs.get("commission", 0)), 4),
            total_cost=round(float(costs.get("total_cost", 0)), 4),
            cost_model=costs.get("cost_model", {}),
        )
        self._append_order(run_date, order)
        if status == "filled" and fill_price is not None:
            self._transition_lifecycle(run_date, order_id, "filled", reason="paper_order_filled", filled_quantity=quantity)
            if ticket.get("stop_loss") is not None or ticket.get("targets"):
                self._transition_lifecycle(
                    run_date,
                    order_id,
                    "protective_attached",
                    reason="paper_tp_sl_recorded",
                    protective_quantity=quantity,
                    metadata={"stop_loss": ticket.get("stop_loss"), "targets": ticket.get("targets", [])},
                )
            self._append_open_trade(run_date, ticket, order)
            self.rebuild_positions_from_open_trades(run_date)
        return order

    # Clean-bar timeframes a paper account may have been fetched on, ordered so
    # the legacy 5m account resolves first (byte-identical). Each strategy
    # namespace holds exactly ONE tradable series per symbol (the runner fetches
    # only the strategy's own timeframe), so falling back to the present series
    # is unambiguous — it lets a 1m chan namespace mark-to-market and evaluate
    # exits even though every caller still asks for the default 5m.
    _CLEAN_BAR_TIMEFRAMES = ("5m", "1m", "15m", "1h", "4h", "1d")

    def _clean_bar_rows(self, run_date: str, symbol: str, timeframe: str = "5m") -> list[dict]:
        rows = load_json(self.output_root / "clean_bars" / run_date / f"{symbol}_{timeframe}.json")
        if rows:
            return rows
        for candidate in self._CLEAN_BAR_TIMEFRAMES:
            if candidate == timeframe:
                continue
            rows = load_json(self.output_root / "clean_bars" / run_date / f"{symbol}_{candidate}.json")
            if rows:
                return rows
        return []

    def record_external_fill(
        self,
        run_date: str,
        ticket: dict,
        fill_price: float,
        quantity: float,
        order_id: str,
        commission: float = 0.0,
        exchange_managed: bool = True,
    ) -> PaperOrder:
        """Mirror a REAL broker fill into local accounting at the actual fill
        price/qty (no re-modelling). Live (Binance) fills go through here so the
        system tracks its own live position — MTM, leaderboard, reconciliation.
        Tagged `exchange_managed` so local exit logic leaves the stop/target to
        the exchange's own protective orders."""
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        order = PaperOrder(
            order_id=order_id,
            ticket_id=ticket["ticket_id"],
            status="filled",
            requested_price=round(float(fill_price), 4),
            fill_price=round(float(fill_price), 4),
            quantity=round(float(quantity), 6),
            filled_at=now,
            rejection_reason="",
            gross_fill_price=round(float(fill_price), 4),
            slippage_cost=0.0,
            spread_cost=0.0,
            commission=round(float(commission), 4),
            total_cost=round(float(commission), 4),
            cost_model={"source": "exchange_fill"},
        )
        lifecycle, _ = self.lifecycle.write_intent(
            run_date,
            order_id=order_id,
            ticket_id=ticket["ticket_id"],
            idempotency_key=order_id,
            requested_quantity=quantity,
            requested_price=fill_price,
            source="external_exchange_fill",
            metadata={"exchange_managed": exchange_managed},
        )
        for state, reason in [("submitting", "external_fill_imported"), ("accepted", "external_fill_accepted")]:
            if lifecycle.get("state") == state:
                continue
            lifecycle = self._transition_lifecycle(run_date, order_id, state, reason=reason)
        lifecycle = self._transition_lifecycle(run_date, order_id, "filled", reason="external_fill_mirrored", filled_quantity=quantity)
        if exchange_managed and lifecycle.get("state") == "filled":
            self._transition_lifecycle(
                run_date,
                order_id,
                "protective_attached",
                reason="exchange_managed_protection_expected",
                protective_quantity=quantity,
            )
        self._append_order(run_date, order)
        trade = self._trade_from_order(run_date, ticket, order.to_dict(), backfilled=False)
        flags = list(trade.get("quality_flags", []))
        if exchange_managed:
            flags = [*flags, "exchange_managed", "live_fill"]
        trade["quality_flags"] = flags
        trades_path = self.output_root / "paper_trades" / "current.json"
        rows = [item for item in load_json(trades_path) if item.get("order_id") != order.order_id]
        rows.append(trade)
        write_json(trades_path, rows)
        self.rebuild_positions_from_open_trades(run_date)
        return order

    def latest_clean_close(self, run_date: str, symbol: str, timeframe: str = "5m") -> float | None:
        rows = self._clean_bar_rows(run_date, symbol, timeframe)
        if not rows:
            return None
        return float(rows[-1]["close"])

    def mark_to_market(self, run_date: str) -> dict:
        self.lifecycle.watchdog_tick(run_date)
        path = self.output_root / "paper_positions" / "current.json"
        if not path.exists():
            return {}
        current = json.loads(path.read_text(encoding="utf-8"))
        block = self._paper_data_block(run_date, "mark_to_market")
        if block:
            self._record_execution_block(run_date, block)
            return current
        updated = {}
        open_trades = load_json(self.output_root / "paper_trades" / "current.json")
        if open_trades:
            return self.rebuild_positions_from_open_trades(run_date)
        for symbol, item in current.items():
            latest = self.latest_clean_close(run_date, symbol)
            if latest is None:
                updated[symbol] = item
                continue
            quantity = float(item.get("quantity", 0))
            avg_price = float(item.get("avg_price", 0))
            side = item.get("side", "long")
            direction = 1 if side == "long" else -1
            unrealized = (latest - avg_price) * quantity * direction
            total_costs = float(item.get("total_costs", 0) or 0)
            updated[symbol] = {
                **item,
                "last_price": round(latest, 4),
                "gross_unrealized_pnl": round(unrealized, 4),
                "unrealized_pnl": round(unrealized - total_costs, 4),
                "marked_at_run_date": run_date,
            }
        path.write_text(json.dumps(updated, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return updated

    def rebuild_positions_from_open_trades(self, run_date: str | None = None, realized_pnl_delta: dict[str, float] | None = None) -> dict:
        """Rebuild the paper position summary from canonical open trades.

        The open trade ledger is the source of truth. A strategy can currently
        hold multiple paper legs for the same symbol, including simultaneous
        long and short experiments. The old position model could only hold the
        latest side, so a short leg followed by a long leg overwrote the short
        exposure and made reconciliation fail. This summary keeps the legacy
        top-level fields while exposing `legs` and net exposure for mixed books.
        """
        path = self.output_root / "paper_positions" / "current.json"
        existing = self._load_mapping(path)
        deltas = realized_pnl_delta or {}
        open_trades = [item for item in load_json(self.output_root / "paper_trades" / "current.json") if item.get("status", "open") == "open"]
        grouped: dict[str, dict] = {}
        for trade in open_trades:
            symbol = str(trade.get("symbol", "GOLD"))
            side = str(trade.get("side", "long"))
            if side not in {"long", "short"}:
                continue
            quantity = float(trade.get("quantity", 0) or 0)
            entry = float(trade.get("entry_price", 0) or 0)
            costs = float(trade.get("entry_total_cost", 0) or 0)
            risk = float(trade.get("max_loss_pct", trade.get("risk_used_pct", 0)) or 0)
            symbol_group = grouped.setdefault(symbol, {"legs": {}, "risk_used_pct": 0.0})
            leg = symbol_group["legs"].setdefault(side, {"quantity": 0.0, "notional": 0.0, "total_costs": 0.0})
            leg["quantity"] += quantity
            leg["notional"] += quantity * entry
            leg["total_costs"] += costs
            symbol_group["risk_used_pct"] += risk

        rebuilt: dict[str, dict] = {}
        for symbol, item in grouped.items():
            existing_position = existing.get(symbol, {})
            last_price = self.latest_clean_close(run_date, symbol) if run_date else None
            if last_price is None:
                last_price = float(existing_position.get("last_price") or 0)
            legs: dict[str, dict] = {}
            signed_net = 0.0
            gross_quantity = 0.0
            gross_notional = 0.0
            total_costs = 0.0
            gross_unrealized = 0.0
            for side, leg in sorted(item["legs"].items()):
                quantity = float(leg["quantity"])
                avg_price = float(leg["notional"]) / quantity if quantity else 0.0
                direction = 1 if side == "long" else -1
                signed_net += quantity * direction
                gross_quantity += quantity
                gross_notional += float(leg["notional"])
                total_costs += float(leg["total_costs"])
                leg_unrealized = (float(last_price or avg_price) - avg_price) * quantity * direction
                gross_unrealized += leg_unrealized
                legs[side] = {
                    "side": side,
                    "quantity": round(quantity, 6),
                    "avg_price": round(avg_price, 4),
                    "total_costs": round(float(leg["total_costs"]), 4),
                    "gross_unrealized_pnl": round(leg_unrealized, 4),
                    "unrealized_pnl": round(leg_unrealized - float(leg["total_costs"]), 4),
                }
            sides = sorted(legs)
            side = sides[0] if len(sides) == 1 else "mixed"
            net_side = "flat"
            if signed_net > 0:
                net_side = "long"
            elif signed_net < 0:
                net_side = "short"
            realized = float(existing_position.get("realized_pnl", 0) or 0) + float(deltas.get(symbol, 0) or 0)
            rebuilt[symbol] = {
                "symbol": symbol,
                "side": side,
                "quantity": round(gross_quantity, 6),
                "avg_price": round(gross_notional / gross_quantity, 4) if gross_quantity else 0.0,
                "unrealized_pnl": round(gross_unrealized - total_costs, 4),
                "realized_pnl": round(realized, 4),
                "risk_used_pct": round(float(item.get("risk_used_pct", 0) or 0), 4),
                "total_costs": round(total_costs, 4),
                "last_price": round(float(last_price), 4) if last_price else None,
                "gross_unrealized_pnl": round(gross_unrealized, 4),
                "net_quantity": round(signed_net, 6),
                "net_side": net_side,
                "legs": legs,
                "marked_at_run_date": run_date or "",
            }
        write_json(path, rebuilt)
        return rebuilt

    def ensure_trade_records(self, run_date: str) -> list[dict]:
        trades_path = self.output_root / "paper_trades" / "current.json"
        trades = load_json(trades_path)
        existing_order_ids = {item.get("order_id") for item in trades}
        orders = [item for item in load_json(self.output_root / "paper_orders" / f"{run_date}.json") if item.get("status") == "filled"]
        tickets = {item["ticket_id"]: item for item in load_json(self.output_root / "trade_tickets" / f"{run_date}.json") if item.get("ticket_id")}
        added = []
        for order in orders:
            if order.get("order_id") in existing_order_ids:
                continue
            ticket = tickets.get(order["ticket_id"], {})
            trade = self._trade_from_order(run_date, ticket, order, backfilled=True)
            trades.append(trade)
            added.append(trade)
        if added:
            write_json(trades_path, trades)
        return added

    def evaluate_exits(self, run_date: str) -> list[dict]:
        self.lifecycle.watchdog_tick(run_date)
        block = self._paper_data_block(run_date, "evaluate_exits")
        if block:
            self._record_execution_block(run_date, block)
            return []
        self.ensure_trade_records(run_date)
        trades_path = self.output_root / "paper_trades" / "current.json"
        trades = load_json(trades_path)
        if not trades:
            return []
        remaining = []
        closed = []
        realized_deltas: dict[str, float] = {}
        for trade in trades:
            if trade.get("status") != "open":
                remaining.append(trade)
                continue
            if "exchange_managed" in trade.get("quality_flags", []):
                # The exchange holds this position's STOP_MARKET / TAKE_PROFIT_MARKET
                # orders; it closes them, not us. Local exit logic must leave it
                # open and let reconciliation mirror the close when it happens.
                remaining.append(trade)
                continue
            if not trade.get("symbol"):
                remaining.append(trade)
                continue
            latest_bar = self._latest_clean_bar(run_date, trade["symbol"])
            if not latest_bar:
                remaining.append(trade)
                continue
            exit_price, exit_reason = self._exit_trigger(trade, latest_bar)
            if exit_price is None:
                remaining.append(trade)
                continue
            closed_trade = self._close_trade(trade, exit_price, exit_reason, latest_bar)
            closed.append(closed_trade)
            self._transition_lifecycle(
                run_date,
                str(closed_trade.get("order_id") or ""),
                "closed",
                reason=f"paper_exit_{exit_reason}",
                metadata={
                    "exit_price": closed_trade.get("exit_price"),
                    "realized_pnl": closed_trade.get("realized_pnl"),
                    "source": "evaluate_exits",
                },
            )
            symbol = str(closed_trade.get("symbol", "GOLD"))
            realized_deltas[symbol] = realized_deltas.get(symbol, 0.0) + float(closed_trade.get("realized_pnl", 0) or 0)
        write_json(trades_path, remaining)
        if closed:
            closed_path = self.output_root / "paper_trades" / "closed" / f"{run_date}.json"
            existing = load_json(closed_path)
            existing_ids = {item["trade_id"] for item in existing}
            write_json(closed_path, existing + [item for item in closed if item["trade_id"] not in existing_ids])
            self.rebuild_positions_from_open_trades(run_date, realized_deltas)
        return closed

    def close_trade_manual(
        self,
        run_date: str,
        trade_id: str,
        exit_price: float | None = None,
        exit_reason: str = "manual_exit",
        notes: str = "",
        decision_id: str = "",
    ) -> dict:
        block = self._paper_data_block(run_date, "close_trade_manual")
        if block:
            self._record_execution_block(run_date, block)
            raise ValueError(f"paper trade close blocked: {block.get('reason', 'unknown reason')}")
        trades_path = self.output_root / "paper_trades" / "current.json"
        trades = load_json(trades_path)
        trade = next((item for item in trades if item.get("trade_id") == trade_id and item.get("status", "open") == "open"), None)
        if not trade:
            raise ValueError(f"open trade_id not found: {trade_id}")
        latest_bar = self._latest_clean_bar(run_date, str(trade.get("symbol", "GOLD")))
        if not latest_bar:
            raise ValueError(f"latest clean bar missing for {trade.get('symbol', 'GOLD')} on {run_date}")
        resolved_exit_price = float(exit_price if exit_price is not None else latest_bar.get("close", trade.get("entry_price", 0)))
        closed_trade = self._close_trade(trade, resolved_exit_price, exit_reason, latest_bar)
        closed_trade["manual_exit"] = True
        closed_trade["exit_notes"] = notes
        closed_trade["exit_decision_id"] = decision_id
        self._transition_lifecycle(
            run_date,
            str(closed_trade.get("order_id") or ""),
            "closed",
            reason=exit_reason,
            metadata={"manual_exit": True, "exit_price": resolved_exit_price, "decision_id": decision_id},
        )
        remaining = [item for item in trades if item.get("trade_id") != trade_id]
        write_json(trades_path, remaining)
        closed_path = self.output_root / "paper_trades" / "closed" / f"{run_date}.json"
        existing = [item for item in load_json(closed_path) if item.get("trade_id") != trade_id]
        existing.append(closed_trade)
        write_json(closed_path, existing)
        self.rebuild_positions_from_open_trades(run_date, {str(closed_trade.get("symbol", "GOLD")): float(closed_trade.get("realized_pnl", 0) or 0)})
        return closed_trade

    def record_external_close(
        self,
        run_date: str,
        *,
        order_id: str,
        exit_price: float,
        quantity: float,
        exit_reason: str,
        close_order_id: str = "",
    ) -> dict:
        trades_path = self.output_root / "paper_trades" / "current.json"
        trades = load_json(trades_path)
        trade = next((item for item in trades if item.get("order_id") == order_id and item.get("status", "open") == "open"), None)
        if not trade:
            return {"closed": False, "error": f"open trade not found for order_id: {order_id}"}

        close_bar = self._latest_clean_bar(run_date, str(trade.get("symbol", "GOLD"))) or {
            "timestamp": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "close": exit_price,
        }
        close_quantity = float(quantity or 0)
        if close_quantity and abs(close_quantity - float(trade.get("quantity", 0) or 0)) > 1e-8:
            return {
                "closed": False,
                "error": "partial external close is not supported by local mirror",
                "requested_quantity": close_quantity,
                "open_quantity": float(trade.get("quantity", 0) or 0),
            }

        closed_trade = self._close_trade(trade, float(exit_price), exit_reason, close_bar)
        closed_trade["external_close"] = True
        closed_trade["exchange_close_order_id"] = close_order_id
        flags = list(closed_trade.get("quality_flags", []))
        closed_trade["quality_flags"] = [*flags, "exchange_emergency_close"]
        self._transition_lifecycle(
            run_date,
            order_id,
            "closed",
            reason=exit_reason,
            metadata={"external_close": True, "exit_price": exit_price, "quantity": quantity, "close_order_id": close_order_id},
        )
        remaining = [item for item in trades if item.get("order_id") != order_id]
        write_json(trades_path, remaining)
        closed_path = self.output_root / "paper_trades" / "closed" / f"{run_date}.json"
        existing = [item for item in load_json(closed_path) if item.get("trade_id") != closed_trade.get("trade_id")]
        existing.append(closed_trade)
        write_json(closed_path, existing)
        self.rebuild_positions_from_open_trades(run_date, {str(closed_trade.get("symbol", "GOLD")): float(closed_trade.get("realized_pnl", 0) or 0)})
        return {
            "closed": True,
            "trade_id": closed_trade.get("trade_id"),
            "close_order_id": close_order_id,
            "exit_price": closed_trade.get("exit_price"),
            "realized_pnl": closed_trade.get("realized_pnl"),
        }

    def _append_order(self, run_date: str, order: PaperOrder) -> None:
        path = self.output_root / "paper_orders" / f"{run_date}.json"
        rows = [item for item in load_json(path) if item["order_id"] != order.order_id]
        rows.append(order.to_dict())
        write_json(path, rows)

    def _append_open_trade(self, run_date: str, ticket: dict, order: PaperOrder) -> None:
        path = self.output_root / "paper_trades" / "current.json"
        rows = [item for item in load_json(path) if item.get("order_id") != order.order_id]
        rows.append(self._trade_from_order(run_date, ticket, order.to_dict(), backfilled=False))
        write_json(path, rows)

    def _trade_from_order(self, run_date: str, ticket: dict, order: dict, backfilled: bool) -> dict:
        entry_price = float(order.get("fill_price") or order["requested_price"])
        action = ticket.get("action", "prepare_buy")
        side = "long" if action == "prepare_buy" else "short"
        stop_loss = float(ticket.get("stop_loss") or (entry_price * (0.98 if side == "long" else 1.02)))
        target = float((ticket.get("targets") or [entry_price * (1.04 if side == "long" else 0.96)])[0])
        flags = ["backfilled_from_order"] if backfilled else []
        return {
            "trade_id": f"trade_{str(order['order_id']).removeprefix('paper_')}",
            "order_id": order["order_id"],
            "ticket_id": order["ticket_id"],
            "signal_id": ticket.get("signal_id", ""),
            "signal_regime": ticket.get("signal_regime", "unknown"),
            "signal_strength": int(ticket.get("signal_strength", 0) or 0),
            "signal_confidence": int(ticket.get("signal_confidence", 0) or 0),
            "factor_scores": ticket.get("factor_scores", {}),
            "backtest_verdict": (ticket.get("backtest") or {}).get("verdict", ""),
            "source_artifacts": ticket.get("source_artifacts", []),
            "symbol": ticket.get("asset", "GOLD"),
            "side": side,
            "status": "open",
            "quantity": float(order["quantity"]),
            "entry_price": entry_price,
            "requested_entry_price": float(order.get("gross_fill_price") or order.get("requested_price") or entry_price),
            "entry_slippage_cost": round(float(order.get("slippage_cost", 0) or 0), 4),
            "entry_spread_cost": round(float(order.get("spread_cost", 0) or 0), 4),
            "entry_commission": round(float(order.get("commission", 0) or 0), 4),
            "entry_total_cost": round(float(order.get("total_cost", 0) or 0), 4),
            "cost_model": order.get("cost_model", {}),
            "stop_loss": round(stop_loss, 4),
            "target": round(target, 4),
            "opened_at": order.get("filled_at", ""),
            "opened_run_date": run_date,
            "quality_flags": flags,
        }

    def _existing_order(self, run_date: str, order_id: str) -> dict | None:
        path = self.output_root / "paper_orders" / f"{run_date}.json"
        return next((item for item in load_json(path) if item["order_id"] == order_id), None)

    def _update_position(self, ticket: dict, order: PaperOrder) -> None:
        path = self.output_root / "paper_positions" / "current.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        current = {}
        if path.exists():
            current = json.loads(path.read_text(encoding="utf-8"))
        symbol = ticket["asset"]
        existing = current.get(symbol)
        side = "long" if ticket["action"] == "prepare_buy" else "short"
        fill_price = float(order.fill_price or order.requested_price)
        quantity = order.quantity
        entry_cost = float(order.total_cost or 0)
        if existing and existing.get("side") == side:
            old_quantity = float(existing["quantity"])
            total_quantity = old_quantity + quantity
            avg_price = ((float(existing["avg_price"]) * old_quantity) + (fill_price * quantity)) / total_quantity
            realized_pnl = float(existing.get("realized_pnl", 0))
            total_costs = float(existing.get("total_costs", 0) or 0) + entry_cost
        else:
            total_quantity = quantity
            avg_price = fill_price
            realized_pnl = float(existing.get("realized_pnl", 0)) if existing else 0.0
            total_costs = entry_cost
        position = PaperPosition(
            symbol=symbol,
            side=side,
            quantity=round(total_quantity, 6),
            avg_price=round(avg_price, 4),
            unrealized_pnl=0.0,
            realized_pnl=realized_pnl,
            risk_used_pct=float(ticket.get("max_loss_pct", 0)),
        )
        current[symbol] = position.to_dict()
        current[symbol]["total_costs"] = round(total_costs, 4)
        path.write_text(json.dumps(current, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def _apply_closed_trade_to_position(self, trade: dict) -> None:
        path = self.output_root / "paper_positions" / "current.json"
        if not path.exists():
            return
        current = json.loads(path.read_text(encoding="utf-8"))
        position = current.get(trade["symbol"])
        if not position:
            return
        old_quantity = float(position.get("quantity", 0))
        closed_quantity = float(trade.get("quantity", 0))
        remaining_quantity = max(0.0, old_quantity - closed_quantity)
        realized_pnl = float(position.get("realized_pnl", 0)) + float(trade.get("realized_pnl", 0))
        old_costs = float(position.get("total_costs", 0) or 0)
        allocated_costs = old_costs * (closed_quantity / old_quantity) if old_quantity else 0.0
        remaining_costs = max(0.0, old_costs - allocated_costs)
        if remaining_quantity <= 0:
            current.pop(trade["symbol"], None)
        else:
            current[trade["symbol"]] = {
                **position,
                "quantity": round(remaining_quantity, 6),
                "realized_pnl": round(realized_pnl, 4),
                "unrealized_pnl": 0.0,
                "total_costs": round(remaining_costs, 4),
            }
        path.write_text(json.dumps(current, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def _latest_clean_bar(self, run_date: str, symbol: str) -> dict | None:
        rows = self._clean_bar_rows(run_date, symbol)
        return rows[-1] if rows else None

    def _paper_data_block(self, run_date: str, operation: str) -> dict:
        quality = self._load_mapping(self.output_root / "data_quality" / f"{run_date}.json").get("GOLD", {})
        if quality and quality.get("allows_trading") is False:
            return {
                "operation": operation,
                "reason": "; ".join(quality.get("reasons", [])) or "data quality gate blocked paper trade lifecycle updates",
                "source": "data_quality",
                "details": quality,
            }
        preflight = self._latest_preflight(run_date)
        if preflight and preflight.get("ready_for_paper") is False:
            return {
                "operation": operation,
                "reason": preflight.get("message") or "data source preflight blocked paper trade lifecycle updates",
                "source": "data_source_preflight",
                "details": preflight,
            }
        return {}

    def _record_execution_block(self, run_date: str, block: dict) -> None:
        path = self.output_root / "paper_execution_blocks" / f"{run_date}.json"
        rows = load_json(path)
        key = (block.get("operation"), block.get("source"), block.get("reason"))
        if any((item.get("operation"), item.get("source"), item.get("reason")) == key for item in rows):
            return
        rows.append(
            {
                **block,
                "run_date": run_date,
                "blocked_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            }
        )
        write_json(path, rows)
        write_json(self.output_root / "paper_execution_blocks" / "current.json", rows[-5:])

    def _transition_lifecycle(
        self,
        run_date: str,
        order_id: str,
        state: str,
        *,
        reason: str,
        metadata: dict | None = None,
        filled_quantity: float | None = None,
        protective_quantity: float | None = None,
    ) -> dict:
        if not order_id:
            return {}
        try:
            return self.lifecycle.transition(
                run_date,
                order_id,
                state,
                reason=reason,
                metadata=metadata,
                filled_quantity=filled_quantity,
                protective_quantity=protective_quantity,
            )
        except IllegalOrderTransition:
            return self.lifecycle.current(run_date, order_id)

    def _latest_preflight(self, run_date: str) -> dict:
        dated = load_json(self.output_root / "data_source_preflight" / f"{run_date}.json")
        if dated:
            return dated[-1]
        current = load_json(self.output_root / "data_source_preflight" / "current.json")
        return current[-1] if current else {}

    def _load_mapping(self, path: Path) -> dict:
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def _exit_trigger(self, trade: dict, latest_bar: dict) -> tuple[float | None, str]:
        side = trade.get("side", "long")
        high = float(latest_bar.get("high", latest_bar.get("close", 0)))
        low = float(latest_bar.get("low", latest_bar.get("close", 0)))
        stop = float(trade["stop_loss"])
        target = float(trade["target"])
        if side == "long":
            if low <= stop:
                return stop, "stop_loss"
            if high >= target:
                return target, "target"
        else:
            if high >= stop:
                return stop, "stop_loss"
            if low <= target:
                return target, "target"
        return None, ""

    def _close_trade(self, trade: dict, exit_price: float, exit_reason: str, latest_bar: dict) -> dict:
        direction = 1 if trade.get("side") == "long" else -1
        quantity = float(trade["quantity"])
        entry_price = float(trade["entry_price"])
        gross_realized_pnl = (exit_price - entry_price) * quantity * direction
        exit_costs = self._exit_costs(trade, exit_price)
        total_cost = float(trade.get("entry_total_cost", 0) or 0) + float(exit_costs.get("total_cost", 0))
        realized_pnl = gross_realized_pnl - total_cost
        return {
            **trade,
            "status": "closed",
            "exit_price": round(exit_price, 4),
            "exit_reason": exit_reason,
            "closed_at": latest_bar.get("timestamp", datetime.now(timezone.utc).replace(microsecond=0).isoformat()),
            "gross_realized_pnl": round(gross_realized_pnl, 4),
            "exit_slippage_cost": round(float(exit_costs.get("slippage_cost", 0)), 4),
            "exit_spread_cost": round(float(exit_costs.get("spread_cost", 0)), 4),
            "exit_commission": round(float(exit_costs.get("commission", 0)), 4),
            "total_cost": round(total_cost, 4),
            "realized_pnl": round(realized_pnl, 4),
        }

    def _effective_entry_price(self, ticket: dict, requested_price: float) -> float:
        side = "long" if ticket.get("action") == "prepare_buy" else "short"
        spread_pct = float(self.cost_rules.get("spread_pct", 0) or 0)
        slippage_pct = float(self.cost_rules.get("slippage_pct", 0) or 0)
        adverse_pct = (spread_pct / 2) + slippage_pct
        direction = 1 if side == "long" else -1
        return requested_price * (1 + direction * adverse_pct / 100)

    def _entry_costs(self, ticket: dict, requested_price: float, fill_price: float, quantity: float) -> dict:
        side = "long" if ticket.get("action") == "prepare_buy" else "short"
        return self._costs(side, requested_price, fill_price, quantity)

    def _exit_costs(self, trade: dict, exit_price: float) -> dict:
        side = str(trade.get("side", "long"))
        spread_pct = float(self.cost_rules.get("spread_pct", 0) or 0)
        slippage_pct = float(self.cost_rules.get("slippage_pct", 0) or 0)
        adverse_pct = (spread_pct / 2) + slippage_pct
        direction = -1 if side == "long" else 1
        effective_price = exit_price * (1 + direction * adverse_pct / 100)
        return self._costs(side, exit_price, effective_price, float(trade.get("quantity", 0) or 0))

    def _costs(self, side: str, requested_price: float, effective_price: float, quantity: float) -> dict:
        spread_pct = float(self.cost_rules.get("spread_pct", 0) or 0)
        slippage_pct = float(self.cost_rules.get("slippage_pct", 0) or 0)
        spread_cost = abs(requested_price * (spread_pct / 2) / 100 * quantity)
        slippage_cost = abs(requested_price * slippage_pct / 100 * quantity)
        notional = abs(effective_price * quantity)
        commission = max(
            float(self.cost_rules.get("min_commission", 0) or 0),
            float(self.cost_rules.get("commission_per_order", 0) or 0) + notional * (float(self.cost_rules.get("commission_pct_notional", 0) or 0) / 100),
        )
        return {
            "side": side,
            "effective_price": effective_price,
            "slippage_cost": slippage_cost,
            "spread_cost": spread_cost,
            "commission": commission,
            "total_cost": slippage_cost + spread_cost + commission,
            "cost_model": {
                "spread_pct": spread_pct,
                "slippage_pct": slippage_pct,
                "commission_pct_notional": float(self.cost_rules.get("commission_pct_notional", 0) or 0),
                "commission_per_order": float(self.cost_rules.get("commission_per_order", 0) or 0),
                "min_commission": float(self.cost_rules.get("min_commission", 0) or 0),
            },
        }

    def _quantity(self, ticket: dict, price: float) -> float:
        notional = self.account_equity * (float(ticket.get("position_size_pct", 0)) / 100)
        return notional / price if price else 0.0

    def _entry_bounds(self, entry_zone: str) -> tuple[float, float]:
        left, right = entry_zone.split("-", 1)
        return float(left), float(right)

    def _entry_midpoint(self, entry_zone: str) -> float:
        left, right = self._entry_bounds(entry_zone)
        return (left + right) / 2

    def _order_id(self, ticket_id: str, run_date: str, requested_price: float) -> str:
        digest = hashlib.sha256(f"{ticket_id}:{run_date}:{requested_price}".encode("utf-8")).hexdigest()[:10]
        return f"paper_{digest}"
