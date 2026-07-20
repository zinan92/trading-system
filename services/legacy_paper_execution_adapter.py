from __future__ import annotations

from pathlib import Path
from typing import Any

from services.dualtrack_execution_contract import canonical_market_event, normalize_execution_command
from services.dualtrack_human import DualTrackHumanEngine
from services.dualtrack_scoring import _trades_from_fills, apply_unrealized
from services.journal_store import load_json, write_json


class LegacyPaperExecutionAdapter:
    name = "legacy_paper"

    def __init__(self, output_root: Path, *, config: dict[str, Any] | None = None) -> None:
        self.output_root = Path(output_root)
        self.config = dict(config or {})
        self.engine = DualTrackHumanEngine(self.output_root, config=config)

    def submit_order(self, command: dict[str, Any]) -> dict[str, Any]:
        command = normalize_execution_command(command, self.config)
        if _is_pending_limit_entry(command):
            if _is_marketable_limit(command):
                fill_command = {
                    **command,
                    "requested_price": float(command["price"]),
                    "price": float(command["market_price"]),
                    "liquidity": "taker",
                }
                fill = self.engine.submit_order(fill_command)
                self._record_shadow_command(command, fill)
                return fill
            receipt = self._accept_limit_entry(command)
            self._record_shadow_command(command, receipt)
            return receipt
        fill = self.engine.submit_order(command)
        self._record_shadow_command(command, fill)
        return fill

    def cancel_orders(
        self,
        cycle_id: str,
        *,
        order_ids: list[str] | None = None,
        strategy_plan_id: str | None = None,
        ts: str | None = None,
        reason: str = "",
    ) -> dict[str, Any]:
        selected_ids = {str(value) for value in (order_ids or []) if str(value)}
        rows = load_json(self._orders_path(cycle_id))
        cancelled_ids: list[str] = []
        for row in rows:
            row_id = str(row.get("order_id") or "")
            row_command = row.get("command") if isinstance(row.get("command"), dict) else {}
            row_plan_id = str(row.get("strategy_plan_id") or row_command.get("strategy_plan_id") or "")
            if row.get("state") != "accepted":
                continue
            if selected_ids and row_id not in selected_ids:
                continue
            if strategy_plan_id and row_plan_id != str(strategy_plan_id):
                continue
            row["state"] = "cancelled"
            row["cancelled_at"] = ts
            row["cancel_reason"] = str(reason or "")
            cancelled_ids.append(row_id)
        if cancelled_ids:
            write_json(self._orders_path(cycle_id), rows)
        return {
            "status": "cancelled" if cancelled_ids else "idempotent",
            "cycle_id": cycle_id,
            "cancelled_order_ids": cancelled_ids,
            "cancelled_order_count": len(cancelled_ids),
        }

    def process_market_event(self, event: dict[str, Any]) -> dict[str, Any]:
        event = canonical_market_event(event)
        # Existing positions see this bar first. A limit entry accepted from
        # this same OHLC range cannot be safely assumed to have preceded the
        # bar's stop/target wick.
        protective = self.engine.sweep_protective_exits(
            str(event.get("cycle_id") or ""),
            mark_price=event.get("price"),
            mark_open=event.get("open"),
            mark_high=event.get("high"),
            mark_low=event.get("low"),
            event_started_at=event.get("event_started_at"),
            ts=event.get("ts_event"),
            source=str(event["source"]),
        )
        limit_fills = self._fill_pending_limit_entries(event)
        return {
            **protective,
            "accepted_limit_fills": limit_fills,
            "accepted_limit_fill_count": len(limit_fills),
        }

    def snapshot(
        self,
        cycle_id: str,
        *,
        mark_price: float | None = None,
        mark_fresh: bool = False,
        mark_source: str = "",
    ) -> dict[str, Any]:
        payload = self.engine.human_payload(cycle_id)
        fills = list(payload.get("fills") or [])
        positions = apply_unrealized(
            list(payload.get("trades") or _trades_from_fills(fills, track="human")),
            mark_price,
            mark_fresh=mark_fresh,
        )
        open_positions = [position for position in positions if position.get("status") == "open"]
        unrealized_values = [position.get("unrealized_pnl") for position in open_positions]
        unrealized = (
            sum(float(value or 0.0) for value in unrealized_values)
            if all(value is not None for value in unrealized_values)
            else None
        )
        account = dict(payload.get("account") or {})
        starting_cash = float(self.engine.config.get("capital_per_track_usd") or 0.0)
        realized = round(sum(float(fill.get("realized_pnl") or 0.0) for fill in fills), 8)
        account.setdefault("starting_cash", starting_cash)
        account.setdefault("realized_pnl", realized)
        account.setdefault("ending_cash", round(starting_cash + realized, 8))
        exposure = round(sum(
            float(position.get("remaining_units") or 0.0) * float(position.get("entry_price") or 0.0)
            for position in positions
            if str(position.get("status") or "") == "open"
        ), 8)
        account["exposure"] = exposure
        account["margin"] = round(exposure / float(self.engine.config.get("max_leverage") or 1.0), 8)
        account["slippage"] = round(sum(float(fill.get("slippage") or 0.0) for fill in fills), 8)
        account["fees"] = round(sum(float(fill.get("cost") or 0.0) for fill in fills), 8)
        account.setdefault("funding", 0.0)
        account["equity"] = round(float(account["ending_cash"]) + float(unrealized or 0.0), 8)
        return {
            "schema_version": "dualtrack-execution-v1",
            "engine": self.name,
            "cycle_id": cycle_id,
            "orders": self._orders(cycle_id, fills),
            "fills": fills,
            "positions": positions,
            "account": account,
            "pnl": {
                "realized": realized,
                "unrealized": None if unrealized is None else round(unrealized, 8),
            },
            "mark": {
                "price": mark_price,
                "fresh": bool(mark_fresh),
                "source": mark_source,
            },
            "capabilities": {
                "native_order_lifecycle": False,
                "order_lifecycle": "derived_from_fill_ledger",
                "protective_orders": "compatibility_sweep",
                "restart_reconciliation": "local_ledger",
            },
        }

    def reconcile(self, cycle_id: str) -> dict[str, Any]:
        snapshot = self.snapshot(cycle_id)
        fills = snapshot["fills"]
        positions = snapshot["positions"]
        issues: list[dict[str, Any]] = []
        fill_ids = [str(fill.get("fill_id") or "") for fill in fills]
        duplicate_ids = sorted({fill_id for fill_id in fill_ids if fill_id and fill_ids.count(fill_id) > 1})
        if duplicate_ids:
            issues.append({"code": "duplicate_fill_id", "fill_ids": duplicate_ids})
        matched_exit_ids = {
            str(exit_fill.get("fill_id") or "")
            for position in positions
            for exit_fill in position.get("exit_fills") or []
        }
        orphan_exit_ids = [
            str(fill.get("fill_id") or "")
            for fill in fills
            if str(fill.get("event") or "") in {"exit", "stop", "target", "flatten"}
            and str(fill.get("fill_id") or "") not in matched_exit_ids
        ]
        if orphan_exit_ids:
            issues.append({"code": "orphan_exit_fill", "fill_ids": orphan_exit_ids})
        negative_positions = [
            str(position.get("trade_id") or "")
            for position in positions
            if float(position.get("remaining_units") or 0.0) < 0
        ]
        if negative_positions:
            issues.append({"code": "negative_position_units", "trade_ids": negative_positions})
        account_realized = snapshot["account"].get("realized_pnl")
        if account_realized not in (None, "") and abs(float(account_realized) - float(snapshot["pnl"]["realized"])) > 1e-6:
            issues.append({
                "code": "account_realized_pnl_mismatch",
                "account": float(account_realized),
                "fills": float(snapshot["pnl"]["realized"]),
            })
        return {
            "schema_version": "dualtrack-execution-reconciliation-v1",
            "engine": self.name,
            "cycle_id": cycle_id,
            "status": "ok" if not issues else "drift",
            "issues": issues,
            "counts": {
                "fills": len(fills),
                "positions": len(positions),
                "open_positions": sum(1 for position in positions if position.get("status") == "open"),
            },
        }

    def _record_shadow_command(self, command: dict[str, Any], fill: dict[str, Any]) -> None:
        """Persist accepted command evidence for a future isolated replay.

        The command journal is separate from legacy fills: it preserves the
        requested order semantics while the legacy fill remains authoritative.
        Retried commands resolve to the same legacy fill ID and are idempotent.
        """

        cycle_id = str(fill.get("cycle_id") or command.get("cycle_id") or "")
        command_id = str(fill.get("fill_id") or fill.get("order_id") or "")
        if not cycle_id or not command_id:
            return
        path = self.output_root / "dualtrack" / "shadow_commands" / f"{cycle_id}.json"
        rows = load_json(path)
        if any(str(row.get("command_id") or "") == command_id for row in rows):
            return
        safe_command = {
            key: value
            for key, value in command.items()
            if key not in {"authorization", "credential", "secret", "token", "password"}
        }
        rows.append({
            "schema_version": "dualtrack-shadow-command-v1",
            "command_id": command_id,
            "cycle_id": cycle_id,
            "accepted_fill_id": str(fill.get("fill_id") or ""),
            "accepted_order_id": str(fill.get("order_id") or ""),
            "command": safe_command,
        })
        write_json(path, rows)

    def _orders(self, cycle_id: str, fills: list[dict[str, Any]]) -> list[dict[str, Any]]:
        pending_rows = load_json(self._orders_path(cycle_id))
        pending_by_fill = {str(row.get("fill_id") or ""): row for row in pending_rows if row.get("fill_id")}
        orders = [_order_row(row) for row in pending_rows]
        orders.extend(_orders_from_fills([fill for fill in fills if str(fill.get("fill_id") or "") not in pending_by_fill]))
        return orders

    def _orders_path(self, cycle_id: str) -> Path:
        return self.output_root / "dualtrack" / "orders" / f"{cycle_id}_human.json"

    def _accept_limit_entry(self, command: dict[str, Any]) -> dict[str, Any]:
        cycle_id = str(command.get("cycle_id") or "")
        if not cycle_id:
            raise ValueError("limit entry cycle_id is required")
        source_id = str(command.get("source_fill_id") or command.get("external_fill_id") or "")
        rows = load_json(self._orders_path(cycle_id))
        if source_id:
            existing = next((row for row in rows if str(row.get("source_id") or "") == source_id), None)
            if existing:
                return _order_row(existing)
        price = float(command.get("price") or 0.0)
        notional = float(command.get("notional") or 0.0)
        if price <= 0 or notional <= 0:
            raise ValueError("limit entry requires positive price and notional")
        side = str(command.get("side") or "").lower()
        if side not in {"buy", "sell"}:
            raise ValueError("limit entry side must be buy or sell")
        order_id = f"{cycle_id}_human_order_{len(rows) + 1:04d}"
        row = {
            "order_id": order_id,
            "cycle_id": cycle_id,
            "state": "accepted",
            "side": side,
            "event": "entry",
            "order_type": "limit",
            "price": price,
            "quantity": float(command.get("quantity") or command.get("contracts") or (notional / price)),
            "notional": notional,
            "sl": command.get("sl"),
            "tp": command.get("tp"),
            "ts": command.get("ts"),
            "source": command.get("source"),
            "source_id": source_id,
            "strategy_plan_id": command.get("strategy_plan_id"),
            "strategy_plan_version": command.get("strategy_plan_version"),
            "command": dict(command),
        }
        rows.append(row)
        write_json(self._orders_path(cycle_id), rows)
        return _order_row(row)

    def _fill_pending_limit_entries(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        cycle_id = str(event["cycle_id"])
        rows = load_json(self._orders_path(cycle_id))
        filled: list[dict[str, Any]] = []
        changed = False
        for row in rows:
            if row.get("state") != "accepted" or row.get("order_type") != "limit" or row.get("event") != "entry":
                continue
            price = float(row["price"])
            low = float(event["low"] if event.get("low") is not None else event["price"])
            high = float(event["high"] if event.get("high") is not None else event["price"])
            accepted_at = _parse_timestamp(row.get("ts"))
            event_at = _parse_timestamp(event.get("ts_event"))
            if accepted_at is not None and event_at is not None and event_at <= accepted_at:
                continue
            event_started_at = _parse_timestamp(event.get("event_started_at"))
            if accepted_at is not None and event_started_at is not None and event_started_at < accepted_at:
                # The accepted order did not exist for the whole OHLC interval.
                # Only the observed event price is chronology-safe for that partial bar.
                low = high = float(event["price"])
            touched = low <= price if row.get("side") == "buy" else high >= price
            if not touched:
                continue
            command = dict(row.get("command") or {})
            command.update({
                "cycle_id": cycle_id,
                "ts": event["ts_event"],
                "price": price,
                "notional": float(row["quantity"]) * price,
                "contracts": row["quantity"],
                "order_type": "limit",
                "source": str(row.get("source") or event["source"]),
                "source_fill_id": f"legacy-limit:{row['order_id']}",
            })
            fill = self.engine.submit_order(command)
            row["state"] = "filled"
            row["fill_id"] = fill["fill_id"]
            row["fill_price"] = fill["price"]
            row["fill_quantity"] = fill.get("pnl_units")
            changed = True
            filled.append(fill)
        if changed:
            write_json(self._orders_path(cycle_id), rows)
        return filled


def _orders_from_fills(fills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Expose legacy order terminal evidence without claiming a native OMS."""

    orders = []
    for fill in fills:
        order = {
            "order_id": str(fill.get("fill_id") or ""),
            "state": "filled",
            "side": str(fill.get("side") or "").lower(),
            "event": str(fill.get("event") or "entry").lower(),
            "order_type": str(fill.get("order_type") or "").lower(),
            "price": float(fill.get("price") or 0.0),
            "quantity": float(fill.get("pnl_units") or fill.get("units") or 0.0),
        }
        for key in ("ts", "strategy_plan_id", "strategy_plan_version"):
            if fill.get(key) not in (None, ""):
                order[key] = fill[key]
        orders.append(order)
    return orders


def _order_row(row: dict[str, Any]) -> dict[str, Any]:
    order = {
        "order_id": str(row.get("order_id") or ""),
        "state": str(row.get("state") or ""),
        "side": str(row.get("side") or "").lower(),
        "event": str(row.get("event") or "entry").lower(),
        "order_type": str(row.get("order_type") or "").lower(),
        "price": float(row.get("fill_price") or row.get("price") or 0.0),
        "quantity": float(row.get("fill_quantity") or row.get("quantity") or 0.0),
    }
    command = row.get("command") if isinstance(row.get("command"), dict) else {}
    for key in ("notional", "sl", "tp", "ts", "source", "strategy_plan_id", "strategy_plan_version"):
        if row.get(key) in (None, "") and command.get(key) not in (None, ""):
            order[key] = command[key]
        if row.get(key) not in (None, ""):
            order[key] = row[key]
    return order


def _is_pending_limit_entry(command: dict[str, Any]) -> bool:
    return (
        str(command.get("order_type") or "").lower() == "limit"
        and str(command.get("event") or "entry").lower() == "entry"
    )


def _is_marketable_limit(command: dict[str, Any]) -> bool:
    try:
        limit_price = float(command.get("price"))
        market_price = float(command.get("market_price"))
    except (TypeError, ValueError):
        return False
    side = str(command.get("side") or "").lower()
    if limit_price <= 0 or market_price <= 0:
        return False
    if side == "buy":
        return market_price <= limit_price
    if side == "sell":
        return market_price >= limit_price
    return False


def _parse_timestamp(value: Any):
    if value in (None, ""):
        return None
    try:
        from services.dualtrack_clock import parse_utc

        return parse_utc(value)
    except (TypeError, ValueError):
        return None
