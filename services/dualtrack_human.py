from __future__ import annotations

import math
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

from services.config_loader import ROOT, load_pipeline_config, load_risk_rules
from services.dualtrack_clock import cycle_window, cycle_window_from_id, parse_utc
from services.dualtrack_config import dualtrack_config
from services.dualtrack_costs import dualtrack_cost_descriptor, dualtrack_order_cost
from services.dualtrack_store import DualTrackPlanStore
from services.journal_store import load_json, write_json

ORDER_SIDES = {"buy", "sell"}
ORDER_TYPES = {"market", "limit", "stop"}
ORDER_EVENTS = {"entry", "exit", "stop", "target", "flatten"}
_EPSILON = 1e-9


class DualTrackHumanEngine:
    def __init__(self, output_root: Path | None = None, *, config: dict[str, Any] | None = None) -> None:
        pipeline_config = load_pipeline_config()
        self.output_root = Path(output_root) if output_root else ROOT / pipeline_config.get("output_root", "outputs")
        self.root = self.output_root / "dualtrack"
        self.config = config or dualtrack_config()
        self.cost_rules = load_risk_rules().get("default", {}).get("paper_execution_costs", {})
        self.store = DualTrackPlanStore(self.output_root, config=self.config)

    def submit_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        ts = parse_utc(payload.get("ts"))
        cycle_id = str(payload.get("cycle_id") or cycle_window(ts).cycle_id)
        timestamp_cycle_id = cycle_window(ts).cycle_id
        requested_event = str(payload.get("event") or "").lower()
        position_cycle_id = str(payload.get("position_cycle_id") or "")
        request_cycle_id = str(payload.get("request_cycle_id") or "")
        cross_cycle_exit = (
            requested_event in {"exit", "stop", "target", "flatten"}
            and position_cycle_id == cycle_id
            and request_cycle_id == timestamp_cycle_id
            and cycle_window_from_id(cycle_id).start <= cycle_window_from_id(request_cycle_id).start
        )
        if timestamp_cycle_id != cycle_id and not cross_cycle_exit:
            raise ValueError("order timestamp does not belong to cycle")
        side = str(payload.get("side") or "").lower()
        order_type = str(payload.get("order_type") or "market").lower()
        if side not in ORDER_SIDES:
            raise ValueError("side must be buy or sell")
        if order_type not in ORDER_TYPES:
            raise ValueError("order_type must be market or limit")
        price = _required_float(payload.get("price") or payload.get("market_price"), "price")
        fills_path = self._fills_path(cycle_id)
        rows = load_json(fills_path)
        source_fill_id = str(payload.get("source_fill_id") or payload.get("external_fill_id") or "")
        if source_fill_id:
            existing = next((row for row in rows if str(row.get("source_fill_id") or "") == source_fill_id), None)
            if existing:
                return existing
        position_id = str(payload.get("position_id") or payload.get("trade_id") or "manual")
        symbol = str(payload.get("symbol") or "")
        event = _resolve_event(payload, rows, side=side, position_id=position_id, symbol=symbol)
        sl = _optional_float(payload.get("sl"), "sl")
        tp = _optional_float(payload.get("tp"), "tp")
        if event == "entry":
            _validate_protective_geometry(side=side, entry=price, sl=sl, tp=tp)
        notional = _optional_float(payload.get("notional"), "notional")
        contracts = _optional_float(payload.get("contracts", payload.get("quantity")), "contracts")
        if event in {"exit", "stop", "target", "flatten"}:
            notional, contracts = self._infer_exit_size(
                rows,
                side=side,
                price=price,
                notional=notional,
                contracts=contracts,
                position_id=position_id,
                symbol=symbol,
                trade_id=str(payload.get("trade_id") or ""),
            )
        order_cost = dualtrack_order_cost(
            config=self.config,
            price=price,
            notional=notional,
            contracts=contracts,
            cost_rules=self.cost_rules,
            require_contracts=self._uses_venue_cost_model(),
            liquidity=_order_liquidity(payload, order_type=order_type),
        )
        cost_override = _optional_float(_first_present(payload, ["cost", "commission"]), "cost")
        realized_cost = cost_override if cost_override is not None else order_cost.cost
        if order_cost.notional <= 0:
            raise ValueError("notional must be positive")
        plan = self.store.load_plan(cycle_id, "human")
        fill_id = _next_fill_id(cycle_id, rows)
        fill = {
            "fill_id": fill_id,
            "cycle_id": cycle_id,
            "ts": ts.isoformat(),
            "side": side,
            "price": price,
            "sl": sl,
            "tp": tp,
            "layer": "manual",
            "event": event,
            "order_type": order_type,
            "position_id": position_id,
            "trade_id": str(payload.get("trade_id") or ""),
            "out_of_plan": _out_of_plan(plan, side=side, price=price, event=event),
            "realized_pnl": -realized_cost,
            "gross_pnl": 0.0,
            "track": "human",
        }
        fill.update(order_cost.fill_fields())
        if fill.get("liquidity") in (None, ""):
            model_liquidity = (fill.get("cost_model") or {}).get("liquidity")
            if model_liquidity not in (None, ""):
                fill["liquidity"] = model_liquidity
        fill["pnl_units"] = _pnl_units(fill)
        if not fill["trade_id"]:
            fill["trade_id"] = _default_trade_id(cycle_id, rows, event=event, position_id=position_id)
        if cost_override is not None:
            fill["cost"] = round(float(cost_override), 8)
            fill["cost_model"] = {
                **fill["cost_model"],
                "cost_source": "broker_reported",
                "estimated_cost": round(float(order_cost.cost), 8),
            }
        for key in (
            "source",
            "source_fill_id",
            "external_order_id",
            "external_parent_id",
            "broker",
            "broker_order_type",
            "broker_status",
            "symbol",
            "root_symbol",
            "currency",
            "imported_at",
            "trigger_price",
            "trigger_mark_price",
            "trigger_source",
            "trigger_open",
            "trigger_high",
            "trigger_low",
            "trigger_event_started_at",
            "liquidity",
            "requested_price",
            "market_price",
            "market_timestamp",
            "market_source",
            "market_fresh",
            "requested_at",
            "safe_action_market_gate",
            "request_cycle_id",
            "position_cycle_id",
            "strategy_plan_id",
            "strategy_plan_version",
        ):
            if payload.get(key) not in (None, ""):
                fill[key] = payload[key]
        if event in {"exit", "stop", "target", "flatten"}:
            self._apply_exit(rows, fill)
        else:
            fill["position_side"] = "long" if side == "buy" else "short"
            fill["remaining_units"] = fill["pnl_units"]
            fill["position_status"] = "open"
        rows.append(fill)
        write_json(fills_path, rows)
        self._write_trades(cycle_id, rows)
        self._write_account(cycle_id, rows)
        return fill

    def human_payload(self, cycle_id: str) -> dict[str, Any]:
        fills = load_json(self._fills_path(cycle_id))
        trades = load_json(self._trades_path(cycle_id))
        account = load_json(self._account_path(cycle_id))
        return {
            "cycle_id": cycle_id,
            "track": "human",
            "fills": fills,
            "trades": trades,
            "account": account[-1] if account else {},
            "realized_pnl": round(sum(float(fill.get("realized_pnl", 0.0)) for fill in fills), 8),
        }

    def sweep_protective_exits(
        self,
        cycle_id: str,
        *,
        mark_price: float | int | str | None,
        mark_open: float | int | str | None = None,
        mark_high: float | int | str | None = None,
        mark_low: float | int | str | None = None,
        event_started_at: str | datetime | None = None,
        ts: str | datetime | None = None,
        source: str = "dualtrack_protective_sweep",
    ) -> dict[str, Any]:
        mark = _finite_float(mark_price)
        if mark is None:
            return {"status": "skipped", "reason": "missing_mark_price", "triggered": []}
        event_open = _finite_float(mark_open)
        event_high = _finite_float(mark_high)
        event_low = _finite_float(mark_low)
        event_start = parse_utc(event_started_at) if event_started_at is not None else None
        event_end = parse_utc(ts) if ts is not None else None
        rows = load_json(self._fills_path(cycle_id))
        entries = {
            str(row.get("trade_id") or row.get("fill_id") or ""): row
            for row in rows
            if str(row.get("event") or "entry") == "entry"
        }
        triggered: list[dict[str, Any]] = []
        for trade in _build_trades(rows):
            if str(trade.get("status") or "open") != "open":
                continue
            if float(trade.get("remaining_units") or 0.0) <= _EPSILON:
                continue
            entry_ts = parse_utc(trade.get("entry_ts"))
            if event_end is not None and event_end <= entry_ts:
                continue
            range_is_post_entry = event_start is not None and event_start >= entry_ts
            trigger = _protective_trigger(
                trade,
                mark,
                mark_open=event_open if range_is_post_entry else None,
                mark_high=event_high if range_is_post_entry else None,
                mark_low=event_low if range_is_post_entry else None,
            )
            if not trigger:
                continue
            trade_id = str(trade.get("trade_id") or "")
            entry = entries.get(trade_id) or {}
            fill = self.submit_order({
                "cycle_id": cycle_id,
                "position_cycle_id": cycle_id,
                "request_cycle_id": cycle_window(ts).cycle_id,
                "ts": ts,
                "side": trigger["exit_side"],
                "event": trigger["event"],
                "order_type": "limit" if trigger["event"] == "target" else "market",
                "liquidity": "maker" if trigger["event"] == "target" else "taker",
                "price": trigger["price"],
                "trade_id": trade_id,
                "position_id": trade.get("position_id") or entry.get("position_id") or "manual",
                "symbol": trade.get("symbol") or entry.get("symbol") or "",
                "source": source,
                "source_fill_id": _protective_source_fill_id(cycle_id, trade_id, trigger["event"], trigger["price"]),
                "trigger_price": trigger["price"],
                "trigger_mark_price": mark,
                "trigger_source": source,
                "trigger_open": event_open,
                "trigger_high": event_high,
                "trigger_low": event_low,
                "trigger_event_started_at": event_start.isoformat() if event_start is not None else "",
            })
            triggered.append({
                "trade_id": trade_id,
                "event": trigger["event"],
                "price": trigger["price"],
                "mark_price": mark,
                "fill_id": fill.get("fill_id"),
                "realized_pnl": fill.get("realized_pnl"),
            })
            rows = load_json(self._fills_path(cycle_id))
        return {"status": "triggered" if triggered else "ok", "mark_price": mark, "triggered": triggered}

    def repair_pre_entry_protective_exits(self, cycle_id: str) -> dict[str, Any]:
        rows = load_json(self._fills_path(cycle_id))
        entry_times = {
            str(row.get("trade_id") or row.get("fill_id") or ""): parse_utc(row.get("ts"))
            for row in rows
            if str(row.get("event") or "entry") == "entry" and row.get("ts")
        }
        removed_fill_ids = {
            str(row.get("fill_id") or "")
            for row in rows
            if str(row.get("source_fill_id") or "").startswith("dualtrack-protective:")
            and str(row.get("event") or "") in {"stop", "target"}
            and str(row.get("trade_id") or "") in entry_times
            and parse_utc(row.get("ts")) <= entry_times[str(row.get("trade_id") or "")]
        }
        if not removed_fill_ids:
            return {"status": "ok", "cycle_id": cycle_id, "removed_fill_ids": [], "fill_count": len(rows)}

        rebuilt: list[dict[str, Any]] = []
        for original in rows:
            if str(original.get("fill_id") or "") in removed_fill_ids:
                continue
            row = deepcopy(original)
            event = str(row.get("event") or "entry")
            if event == "entry":
                row["remaining_units"] = row.get("pnl_units", _pnl_units(row))
                row["position_status"] = "open"
                row.pop("closed_units", None)
            else:
                for key in ("matched_entries", "gross_pnl", "position_side", "remaining_units", "position_status"):
                    row.pop(key, None)
                self._apply_exit(rebuilt, row)
            rebuilt.append(row)

        write_json(self._fills_path(cycle_id), rebuilt)
        self._write_trades(cycle_id, rebuilt)
        self._write_account(cycle_id, rebuilt)
        return {
            "status": "repaired",
            "cycle_id": cycle_id,
            "removed_fill_ids": sorted(removed_fill_ids),
            "fill_count": len(rebuilt),
        }

    def _fills_path(self, cycle_id: str) -> Path:
        return self.root / "fills" / f"{cycle_id}_human.json"

    def _account_path(self, cycle_id: str) -> Path:
        return self.root / "accounts" / f"{cycle_id}_human.json"

    def _trades_path(self, cycle_id: str) -> Path:
        return self.root / "trades" / f"{cycle_id}_human.json"

    def _infer_exit_size(
        self,
        rows: list[dict[str, Any]],
        *,
        side: str,
        price: float,
        notional: float | None,
        contracts: float | None,
        position_id: str,
        symbol: str,
        trade_id: str,
    ) -> tuple[float | None, float | None]:
        if notional is not None or contracts is not None:
            return notional, contracts
        lots = _matching_open_lots(rows, side=side, position_id=position_id, symbol=symbol, trade_id=trade_id)
        if not lots:
            return notional, contracts
        if self._uses_venue_cost_model():
            return notional, sum(float(lot["remaining_contracts"]) for lot in lots)
        units = sum(float(lot["remaining_units"]) for lot in lots)
        return units * float(price), contracts

    def _apply_exit(self, rows: list[dict[str, Any]], fill: dict[str, Any]) -> None:
        lots = _matching_open_lots(
            rows,
            side=str(fill["side"]),
            position_id=str(fill.get("position_id") or ""),
            symbol=str(fill.get("symbol") or ""),
            trade_id=str(fill.get("trade_id") or ""),
        )
        remaining = float(fill.get("pnl_units") or 0.0)
        if remaining <= 0:
            raise ValueError("exit size must be positive")
        if not lots:
            raise ValueError("exit order has no open human entry to close")
        exit_ts = parse_utc(fill.get("ts"))
        if any(exit_ts <= parse_utc(lot["row"].get("ts")) for lot in lots):
            raise ValueError("exit timestamp must be after entry timestamp")
        matched = []
        matched_plan_ids: set[str] = set()
        matched_plan_versions: set[int] = set()
        gross = 0.0
        for lot in lots:
            if remaining <= _EPSILON:
                break
            units = min(float(lot["remaining_units"]), remaining)
            if units <= _EPSILON:
                continue
            entry = lot["row"]
            sign = 1 if entry.get("side") == "buy" else -1
            match_gross = sign * (float(fill["price"]) - float(entry["price"])) * units
            gross += match_gross
            remaining -= units
            new_remaining = max(0.0, float(entry.get("remaining_units", lot["remaining_units"])) - units)
            entry["remaining_units"] = round(new_remaining, 10)
            entry["closed_units"] = round(float(entry.get("closed_units", 0.0) or 0.0) + units, 10)
            entry["position_status"] = "closed" if new_remaining <= _EPSILON else "open"
            if not fill.get("trade_id"):
                fill["trade_id"] = str(entry.get("trade_id") or "")
            match = {
                "fill_id": entry.get("fill_id", ""),
                "trade_id": entry.get("trade_id", ""),
                "units": round(units, 10),
                "entry_price": entry.get("price"),
                "gross_pnl": round(match_gross, 8),
            }
            if entry.get("strategy_plan_id") not in (None, ""):
                plan_id = str(entry["strategy_plan_id"])
                match["strategy_plan_id"] = plan_id
                matched_plan_ids.add(plan_id)
            if entry.get("strategy_plan_version") not in (None, ""):
                plan_version = int(entry["strategy_plan_version"])
                match["strategy_plan_version"] = plan_version
                matched_plan_versions.add(plan_version)
            matched.append(match)
        if remaining > _EPSILON:
            raise ValueError("exit order size exceeds open human position")
        total_units = sum(float(item["units"]) for item in matched)
        for item in matched:
            cost_share = float(fill.get("cost", 0.0) or 0.0) * (float(item["units"]) / total_units if total_units else 0.0)
            item["realized_pnl"] = round(float(item.get("gross_pnl", 0.0)) - cost_share, 8)
        if fill.get("strategy_plan_id") in (None, "") and len(matched_plan_ids) == 1:
            fill["strategy_plan_id"] = next(iter(matched_plan_ids))
        if fill.get("strategy_plan_version") in (None, "") and len(matched_plan_versions) == 1:
            fill["strategy_plan_version"] = next(iter(matched_plan_versions))
        fill["matched_entries"] = matched
        fill["gross_pnl"] = round(gross, 8)
        fill["realized_pnl"] = round(gross - float(fill.get("cost", 0.0) or 0.0), 8)
        fill["position_side"] = "short" if fill["side"] == "buy" else "long"
        fill["remaining_units"] = 0.0
        fill["position_status"] = "closed"

    def _write_trades(self, cycle_id: str, fills: list[dict[str, Any]]) -> None:
        write_json(self._trades_path(cycle_id), _build_trades(fills))

    def _write_account(self, cycle_id: str, fills: list[dict[str, Any]]) -> None:
        starting = float(self.config["capital_per_track_usd"])
        realized = sum(float(fill.get("realized_pnl", 0.0)) for fill in fills)
        write_json(self._account_path(cycle_id), [{
            "cycle_id": cycle_id,
            "track": "human",
            "starting_cash": starting,
            "realized_pnl": round(realized, 8),
            "ending_cash": round(starting + realized, 8),
            "cost_model": dualtrack_cost_descriptor(self.config, self.cost_rules),
        }])

    def _uses_venue_cost_model(self) -> bool:
        model = self.config.get("execution_cost_model")
        return isinstance(model, dict) and bool(model.get("venue"))


def _out_of_plan(plan: dict[str, Any] | None, *, side: str, price: float, event: str = "entry") -> bool:
    if event in {"exit", "stop", "target", "flatten"}:
        return False
    if not plan:
        return True
    bounds = plan.get("range") or {}
    low = bounds.get("low")
    high = bounds.get("high")
    if low is not None and price < float(low):
        return True
    if high is not None and price > float(high):
        return True
    direction = str(plan.get("direction") or "").lower()
    if direction == "long":
        return side == "sell"
    if direction == "short":
        return side == "buy"
    return True


def _resolve_event(
    payload: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    side: str,
    position_id: str,
    symbol: str,
) -> str:
    raw = str(
        payload.get("event")
        or payload.get("trade_event")
        or payload.get("intent")
        or payload.get("role")
        or ""
    ).lower()
    aliases = {"open": "entry", "close": "exit", "closed": "exit", "take_profit": "target", "tp": "target", "sl": "stop"}
    event = aliases.get(raw, raw)
    if event:
        if event not in ORDER_EVENTS:
            raise ValueError("event must be entry, exit, stop, target, or flatten")
        return event
    return "exit" if _matching_open_lots(rows, side=side, position_id=position_id, symbol=symbol) else "entry"


def _matching_open_lots(
    rows: list[dict[str, Any]],
    *,
    side: str,
    position_id: str,
    symbol: str,
    trade_id: str = "",
) -> list[dict[str, Any]]:
    wanted_entry_side = "sell" if side == "buy" else "buy"
    lots = []
    for row in rows:
        if str(row.get("event") or "entry") not in {"entry"}:
            continue
        if row.get("side") != wanted_entry_side:
            continue
        if trade_id and str(row.get("trade_id") or row.get("fill_id") or "") != trade_id:
            continue
        if position_id and str(row.get("position_id") or "manual") != position_id:
            continue
        if symbol and str(row.get("symbol") or "") not in {"", symbol}:
            continue
        units = float(row.get("remaining_units", row.get("pnl_units", _pnl_units(row))) or 0.0)
        if units <= _EPSILON:
            continue
        contracts = float(row.get("contracts", 0.0) or 0.0)
        multiplier = _contract_multiplier(row)
        lots.append({
            "row": row,
            "remaining_units": units,
            "remaining_contracts": units / multiplier if contracts else 0.0,
        })
    return lots


def _pnl_units(fill: dict[str, Any]) -> float:
    contracts = fill.get("contracts")
    if contracts not in (None, ""):
        return float(contracts) * _contract_multiplier(fill)
    price = float(fill.get("price") or 0.0)
    if price <= 0:
        return 0.0
    return float(fill.get("notional") or 0.0) / price


def _contract_multiplier(fill: dict[str, Any]) -> float:
    model = fill.get("cost_model") if isinstance(fill.get("cost_model"), dict) else {}
    multiplier = model.get("contract_multiplier")
    if multiplier is None and isinstance(model.get("side_cost"), dict):
        multiplier = model["side_cost"].get("contract_multiplier")
    return float(multiplier or 1.0)


def _default_trade_id(cycle_id: str, rows: list[dict[str, Any]], *, event: str, position_id: str) -> str:
    if event in {"exit", "stop", "target", "flatten"}:
        return ""
    count = sum(1 for row in rows if str(row.get("event") or "entry") == "entry") + 1
    return f"{cycle_id}_{position_id}_trade_{count:04d}"


def _next_fill_id(cycle_id: str, rows: list[dict[str, Any]]) -> str:
    prefix = f"{cycle_id}_human_"
    suffixes: list[int] = []
    for row in rows:
        fill_id = str(row.get("fill_id") or "")
        if not fill_id.startswith(prefix):
            continue
        try:
            suffixes.append(int(fill_id.removeprefix(prefix)))
        except ValueError:
            continue
    return f"{prefix}{max(suffixes, default=0) + 1:04d}"


def _build_trades(fills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    trades: dict[str, dict[str, Any]] = {}
    for fill in fills:
        event = str(fill.get("event") or "entry")
        if event == "entry":
            trade_id = str(fill.get("trade_id") or fill.get("fill_id") or "")
            existing = trades.get(trade_id)
            if existing:
                if existing.get("side") != ("long" if fill.get("side") == "buy" else "short"):
                    raise ValueError("cannot scale a trade with the opposite entry side")
                prior_units = float(existing.get("units", 0.0) or 0.0)
                added_units = float(fill.get("pnl_units", 0.0) or 0.0)
                added_remaining = float(
                    fill.get("remaining_units", added_units) or 0.0
                )
                total_units = prior_units + added_units
                if total_units <= _EPSILON:
                    raise ValueError("scaled entry units must be positive")
                existing["entry_price"] = round(
                    ((float(existing.get("entry_price", 0.0) or 0.0) * prior_units) + (float(fill.get("price", 0.0) or 0.0) * added_units)) / total_units,
                    10,
                )
                existing["units"] = round(total_units, 10)
                existing["remaining_units"] = round(
                    float(existing.get("remaining_units", 0.0) or 0.0)
                    + added_remaining,
                    10,
                )
                existing["entry_cost"] = round(float(existing.get("entry_cost", 0.0) or 0.0) + float(fill.get("cost", 0.0) or 0.0), 8)
                existing["realized_pnl"] = round(float(existing.get("realized_pnl", 0.0) or 0.0) + float(fill.get("realized_pnl", 0.0) or 0.0), 8)
                existing.setdefault("entry_fills", []).append({
                    "fill_id": fill.get("fill_id", ""),
                    "ts": fill.get("ts", ""),
                    "price": fill.get("price"),
                    "units": added_units,
                })
                continue
            trades[trade_id] = {
                "trade_id": trade_id,
                "position_id": str(fill.get("position_id") or "manual"),
                "symbol": str(fill.get("symbol") or ""),
                "track": "human",
                "side": "long" if fill.get("side") == "buy" else "short",
                "entry_fill_id": fill.get("fill_id", ""),
                "entry_fills": [{
                    "fill_id": fill.get("fill_id", ""),
                    "ts": fill.get("ts", ""),
                    "price": fill.get("price"),
                    "units": fill.get("pnl_units", 0.0),
                }],
                "entry_ts": fill.get("ts", ""),
                "entry_price": fill.get("price"),
                "sl": fill.get("sl"),
                "tp": fill.get("tp"),
                "units": fill.get("pnl_units", 0.0),
                "remaining_units": fill.get("remaining_units", fill.get("pnl_units", 0.0)),
                "entry_cost": fill.get("cost", 0.0),
                "exit_fills": [],
                "gross_pnl": 0.0,
                "realized_pnl": round(float(fill.get("realized_pnl", 0.0) or 0.0), 8),
                "status": fill.get("position_status", "open"),
            }
            if fill.get("strategy_plan_id") not in (None, ""):
                trades[trade_id]["strategy_plan_id"] = fill["strategy_plan_id"]
            if fill.get("strategy_plan_version") not in (None, ""):
                trades[trade_id]["strategy_plan_version"] = fill["strategy_plan_version"]
            continue
        for match in fill.get("matched_entries") or []:
            trade_id = str(match.get("trade_id") or "")
            trade = trades.get(trade_id)
            if not trade:
                continue
            trade["exit_fills"].append({
                "fill_id": fill.get("fill_id", ""),
                "event": event,
                "ts": fill.get("ts", ""),
                "price": fill.get("price"),
                "units": match.get("units"),
                "realized_pnl": match.get("realized_pnl", fill.get("realized_pnl", 0.0)),
            })
            trade["gross_pnl"] = round(float(trade.get("gross_pnl", 0.0)) + float(match.get("gross_pnl", fill.get("gross_pnl", 0.0))), 8)
            trade["realized_pnl"] = round(float(trade.get("realized_pnl", 0.0)) + float(match.get("realized_pnl", fill.get("realized_pnl", 0.0))), 8)
            trade["exit_ts"] = fill.get("ts", "")
            trade["exit_price"] = fill.get("price")
            trade["status"] = "closed" if float(trade.get("remaining_units", 0.0) or 0.0) <= _EPSILON else "open"
    return list(trades.values())


def project_human_trades(fills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project immutable Legacy human fills into trade lifecycles.

    Legacy entry rows persist their current remaining quantity. This adapter
    deliberately preserves that source semantic instead of subtracting exits a
    second time in a generic historical-fill reconstruction.
    """

    return _build_trades(fills)


def _protective_trigger(
    trade: dict[str, Any],
    mark_price: float,
    *,
    mark_open: float | None = None,
    mark_high: float | None = None,
    mark_low: float | None = None,
) -> dict[str, Any] | None:
    side = str(trade.get("side") or "").lower()
    sl = _finite_float(trade.get("sl"))
    tp = _finite_float(trade.get("tp"))
    event_high = mark_price if mark_high is None else mark_high
    event_low = mark_price if mark_low is None else mark_low
    if side == "long":
        if sl is not None and event_low <= sl:
            return {"event": "stop", "exit_side": "sell", "price": mark_open if mark_open is not None and mark_open <= sl else sl}
        if tp is not None and event_high >= tp:
            return {"event": "target", "exit_side": "sell", "price": tp}
    if side == "short":
        if sl is not None and event_high >= sl:
            return {"event": "stop", "exit_side": "buy", "price": mark_open if mark_open is not None and mark_open >= sl else sl}
        if tp is not None and event_low <= tp:
            return {"event": "target", "exit_side": "buy", "price": tp}
    return None


def _protective_source_fill_id(cycle_id: str, trade_id: str, event: str, price: float) -> str:
    return f"dualtrack-protective:{cycle_id}:{trade_id}:{event}:{float(price):.4f}"


def _validate_protective_geometry(*, side: str, entry: float, sl: float | None, tp: float | None) -> None:
    if side == "buy":
        if sl is not None and sl >= entry:
            raise ValueError("long stop must be below entry")
        if tp is not None and tp <= entry:
            raise ValueError("long target must be above entry")
        return
    if sl is not None and sl <= entry:
        raise ValueError("short stop must be above entry")
    if tp is not None and tp >= entry:
        raise ValueError("short target must be below entry")


def _finite_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _required_float(value: Any, field: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a number") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{field} must be finite")
    return parsed


def _optional_float(value: Any, field: str = "optional price") -> float | None:
    if value in (None, ""):
        return None
    return _required_float(value, field)


def _first_present(payload: dict[str, Any], keys: list[str]) -> Any:
    for key in keys:
        if payload.get(key) not in (None, ""):
            return payload.get(key)
    return None


def _cost(notional: float, cost_per_side_bp: float) -> float:
    return float(notional) * float(cost_per_side_bp) / 10_000.0


def _order_liquidity(payload: dict[str, Any], *, order_type: str) -> str:
    explicit = str(payload.get("liquidity") or "").lower()
    if explicit:
        if explicit not in {"maker", "taker"}:
            raise ValueError("liquidity must be maker or taker")
        return explicit
    return "maker" if order_type == "limit" else "taker"
