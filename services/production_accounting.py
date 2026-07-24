"""Canonical read-side accounting for versioned production StrategyPlans."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from services.accounting_projection_core import project_execution_accounting
from services.dualtrack_human import project_human_trades
from services.dualtrack_scoring import _trades_from_fills, apply_unrealized
from services.journal_store import load_json


def build_production_accounting_history(
    *,
    output_root: Path,
    mark_price: float | None,
    mark_fresh: bool,
    starting_cash: float,
    authoritative_engine: str = "legacy_paper",
    limit: int = 200,
) -> dict[str, Any]:
    """Project immutable production facts without mixing shadow/replay ledgers."""

    output = Path(output_root)
    production_fills: list[dict[str, Any]] = []
    production_positions: list[dict[str, Any]] = []
    fills_dir = output / "dualtrack" / "fills"
    if fills_dir.exists():
        for path in sorted(fills_dir.glob("*_human.json")):
            cycle_id = path.name.removesuffix("_human.json")
            selected, entries = _versioned_legacy_fills(load_json(path), cycle_id=cycle_id)
            production_fills.extend(selected)
            for trade in _project_legacy_trades(selected):
                entry = entries.get(str(trade.get("trade_id") or ""), {})
                production_positions.append({
                    **trade,
                    "track": "production",
                    "strategy_plan_id": entry.get("strategy_plan_id"),
                    "strategy_plan_version": entry.get("strategy_plan_version"),
                    "source_cycle_id": cycle_id,
                })

    engine = str(authoritative_engine or "legacy_paper")
    if engine == "nautilus_paper":
        _append_nautilus_authoritative(
            output,
            fills=production_fills,
            positions=production_positions,
        )

    production_fills = _dedupe_fills(production_fills)
    enriched = apply_unrealized(production_positions, mark_price, mark_fresh=mark_fresh)
    enriched.sort(key=lambda trade: str(trade.get("exit_ts") or trade.get("entry_ts") or ""))
    production_fills.sort(key=lambda fill: str(fill.get("ts") or ""))

    projected = project_execution_accounting(
        {
            "schema_version": "dualtrack-execution-v1",
            "engine": engine,
            "cycle_id": "strategy-plan-history",
            "orders": [],
            "fills": production_fills,
            "positions": enriched,
            "account": {
                "starting_cash": float(starting_cash),
                "funding": 0.0,
            },
            "pnl": {},
        },
        source_type="production_history",
        scope={
            "strategy_plan_scope": "all_versioned_production_plans",
            "authoritative_engine": engine,
        },
    ).to_dict()
    counts = projected["counts"]
    pnl = projected["pnl"]
    account = projected["account"]
    total_notional = round(sum(float(row.get("notional") or 0.0) for row in projected["fills"]), 8)
    source_name = (
        "versioned_strategy_plan_and_nautilus_authoritative"
        if engine == "nautilus_paper"
        else "versioned_strategy_plan_fills_only"
    )
    summary = {
        "trade_count": counts["trade_count"],
        "open_trade_count": counts["open_trade_count"],
        "closed_trade_count": counts["completed_trade_count"],
        "completed_trade_count": counts["completed_trade_count"],
        "realized_pnl": pnl["net_realized_pnl"],
        "unrealized_pnl": pnl["unrealized_pnl"],
        "fill_count": counts["fill_count"],
        "entry_fill_count": counts["entry_fill_count"],
        "exit_fill_count": counts["exit_fill_count"],
        "total_notional": total_notional,
    }
    return {
        "schema_version": "strategy-production-history-v1",
        "trades": enriched[-max(1, int(limit)):],
        "fills": production_fills[-max(1, int(limit * 2)):],
        "summary": summary,
        "pnl": {
            "realized": pnl["net_realized_pnl"],
            "unrealized": pnl["unrealized_pnl"],
        },
        "account": {
            "starting_cash": account["starting_balance"],
            "realized_pnl": pnl["net_realized_pnl"],
            "ending_cash": account["ending_cash"],
            "equity": account["equity"],
        },
        "accounting_snapshot": projected,
        "accounting_projection_receipt": {
            "schema_version": "production-accounting-projection-v1",
            "status": projected["reconciliation"]["status"],
            "snapshot_id": projected["snapshot_id"],
            "compatibility_fields_source": "accounting-snapshot-v1",
            "trade_count_semantics": "one_started_lifecycle_is_one_trade",
            "completed_trade_count_semantics": "fully_closed_lifecycles_only",
        },
        "history_contract": {
            "source": source_name,
            "authoritative_engine": engine,
            "nautilus_shadow_excluded": True,
            "legacy_dualtrack_history_preserved": True,
            "legacy_dualtrack_totals_mixed_into_production": False,
        },
    }


def _versioned_legacy_fills(rows: Any, *, cycle_id: str) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    source_rows = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
    production_trade_ids = {
        str(row.get("trade_id") or "")
        for row in source_rows
        if row.get("strategy_plan_id") not in (None, "") and row.get("trade_id") not in (None, "")
    }
    selected = [row for row in source_rows if str(row.get("trade_id") or "") in production_trade_ids]
    entries = {
        str(row.get("trade_id") or ""): row
        for row in selected
        if str(row.get("event") or "") == "entry"
    }
    normalized = []
    for row in selected:
        entry = entries.get(str(row.get("trade_id") or ""), {})
        normalized.append({
            **row,
            "strategy_plan_id": row.get("strategy_plan_id") or entry.get("strategy_plan_id"),
            "strategy_plan_version": row.get("strategy_plan_version") or entry.get("strategy_plan_version"),
            "source_cycle_id": cycle_id,
        })
    return normalized, entries


def _project_legacy_trades(fills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Route modern and historical Legacy fill shapes through honest adapters."""

    by_trade: dict[str, list[dict[str, Any]]] = {}
    for row in fills:
        by_trade.setdefault(str(row.get("trade_id") or row.get("fill_id") or ""), []).append(row)
    trades: list[dict[str, Any]] = []
    for trade_id in sorted(by_trade):
        rows = by_trade[trade_id]
        entries = [row for row in rows if str(row.get("event") or "entry") == "entry"]
        modern_remaining_state = bool(entries) and all(
            row.get("remaining_units") not in (None, "") or row.get("position_status") not in (None, "")
            for row in entries
        )
        if modern_remaining_state:
            trades.extend(project_human_trades(rows))
        else:
            trades.extend(_trades_from_fills(rows, track="production"))
    return trades


def _append_nautilus_authoritative(
    output: Path,
    *,
    fills: list[dict[str, Any]],
    positions: list[dict[str, Any]],
) -> None:
    snapshots_dir = output / "dualtrack" / "nautilus_authoritative" / "snapshots"
    if not snapshots_dir.exists():
        return
    for path in sorted(snapshots_dir.glob("*.json")):
        cycle_id = path.stem
        snapshots = load_json(path)
        snapshot = snapshots[-1] if snapshots and isinstance(snapshots[-1], dict) else {}
        snapshot_fills = [
            {**row, "source_cycle_id": cycle_id}
            for row in snapshot.get("fills") or []
            if isinstance(row, dict) and row.get("strategy_plan_id") not in (None, "")
        ]
        snapshot_positions = [
            {**row, "source_cycle_id": cycle_id}
            for row in snapshot.get("positions") or []
            if isinstance(row, dict) and row.get("strategy_plan_id") not in (None, "")
        ]
        snapshot_fills, snapshot_positions = _reconcile_nautilus_dca_aggregate_rounds(
            output,
            cycle_id=cycle_id,
            fills=snapshot_fills,
            positions=snapshot_positions,
        )
        fills.extend(
            snapshot_fills
        )
        positions.extend(
            snapshot_positions
        )


def _reconcile_nautilus_dca_aggregate_rounds(
    output: Path,
    *,
    cycle_id: str,
    fills: list[dict[str, Any]],
    positions: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Project only fully evidenced DCA child execution as one round trade.

    A Nautilus DCA entry is intentionally submitted with the strategy-level
    round ID, while the engine materializes one child position per fill and
    one exact reduce-only target command per child.  Production accounting is
    one lifecycle per trade, so it needs a read-side aggregate identity.  Raw
    commands, fills, and positions remain immutable; this function merely
    creates the accounting view when all three sources agree.
    """

    lifecycle_rows = load_json(output / "dualtrack" / "dca_lifecycle" / f"{cycle_id}.json")
    command_rows = load_json(output / "dualtrack" / "nautilus_authoritative" / "commands" / f"{cycle_id}.json")
    if not isinstance(lifecycle_rows, list) or not isinstance(command_rows, list):
        return fills, positions
    commands = {
        str(row.get("command_id") or ""): dict(row.get("command") or {})
        for row in command_rows
        if isinstance(row, dict) and isinstance(row.get("command"), dict)
    }
    normalized_fills = [dict(row) for row in fills]
    normalized_positions = [dict(row) for row in positions]
    for lifecycle in lifecycle_rows:
        if not isinstance(lifecycle, dict):
            continue
        resolved = _resolved_dca_round(
            lifecycle,
            fills=normalized_fills,
            positions=normalized_positions,
            commands=commands,
        )
        if resolved is None:
            continue
        plan_id, aggregate, exit_order_ids = resolved
        normalized_fills = [
            {
                **row,
                "source_trade_id": row.get("trade_id"),
                "trade_id": aggregate["trade_id"],
                "accounting_identity_resolution": "nautilus_dca_aggregate_round",
            }
            if str(row.get("order_id") or "") in exit_order_ids
            else row
            for row in normalized_fills
        ]
        normalized_positions = [
            row
            for row in normalized_positions
            if str(row.get("strategy_plan_id") or "") != plan_id
        ]
        normalized_positions.append(aggregate)
    return normalized_fills, normalized_positions


def _resolved_dca_round(
    lifecycle: dict[str, Any],
    *,
    fills: list[dict[str, Any]],
    positions: list[dict[str, Any]],
    commands: dict[str, dict[str, Any]],
) -> tuple[str, dict[str, Any], set[str]] | None:
    """Return an aggregate round only when every child identity agrees."""

    plan_id = str(lifecycle.get("strategy_plan_id") or "")
    round_id = str(lifecycle.get("round_id") or "")
    terminal = str(lifecycle.get("status") or "")
    if not plan_id or not round_id or terminal not in {"target_closed", "stop_closed", "flattened"}:
        return None
    entries = [
        row for row in fills
        if str(row.get("strategy_plan_id") or "") == plan_id
        and str(row.get("event") or "") == "entry"
        and str(row.get("trade_id") or "") == round_id
        and str(row.get("order_id") or "")
    ]
    children = [
        row for row in positions
        if str(row.get("strategy_plan_id") or "") == plan_id
    ]
    child_by_trade = {str(row.get("trade_id") or ""): row for row in children}
    entry_order_ids = {str(row.get("order_id") or "") for row in entries}
    if not entries or len(child_by_trade) != len(children) or set(child_by_trade) != entry_order_ids:
        return None
    if any(
        str(row.get("position_id") or "") != f"POS-{trade_id}"
        or str(row.get("status") or "").lower() != "closed"
        or not math.isclose(float(row.get("remaining_units") or 0.0), 0.0, abs_tol=1e-10)
        for trade_id, row in child_by_trade.items()
    ):
        return None
    exits = [
        row for row in fills
        if str(row.get("strategy_plan_id") or "") == plan_id
        and str(row.get("event") or "") in {"target", "stop", "flatten"}
    ]
    exit_order_ids = {str(row.get("order_id") or "") for row in exits}
    if not exits or any(not order_id for order_id in exit_order_ids):
        return None
    for row in exits:
        command = commands.get(str(row.get("order_id") or ""))
        position_id = str(command.get("position_id") or command.get("target_position_id") or "") if command else ""
        if (
            not command
            or str(command.get("strategy_plan_id") or "") != plan_id
            or position_id not in {str(item.get("position_id") or "") for item in children}
        ):
            return None
    if terminal == "target_closed":
        generations = lifecycle.get("target_generations") if isinstance(lifecycle.get("target_generations"), list) else []
        target_ids = {
            str(order_id)
            for generation in generations if isinstance(generation, dict)
            for order_id in generation.get("execution_order_ids") or []
            if str(generation.get("status") or "") == "filled"
        }
        if target_ids != exit_order_ids or any(str(row.get("event") or "") != "target" for row in exits):
            return None
    entry_quantity = sum(float(row.get("quantity") or 0.0) for row in entries)
    exit_quantity = sum(float(row.get("quantity") or 0.0) for row in exits)
    if entry_quantity <= 0 or not math.isclose(entry_quantity, exit_quantity, abs_tol=1e-10):
        return None
    side = "long" if all(str(row.get("side") or "").lower() == "buy" for row in entries) else "short" if all(str(row.get("side") or "").lower() == "sell" for row in entries) else ""
    if not side:
        return None
    entry_value = sum(float(row.get("quantity") or 0.0) * float(row.get("price") or 0.0) for row in entries)
    exit_value = sum(float(row.get("quantity") or 0.0) * float(row.get("price") or 0.0) for row in exits)
    realized_values = [row.get("realized_pnl") for row in children]
    realized_pnl = (
        sum(float(value) for value in realized_values)
        if all(value not in (None, "") for value in realized_values)
        else None
    )
    aggregate = {
        "trade_id": round_id,
        "position_id": f"aggregate:{round_id}",
        "status": "closed",
        "side": side,
        "quantity": entry_quantity,
        "remaining_units": 0.0,
        "entry_price": entry_value / entry_quantity,
        "exit_price": exit_value / exit_quantity,
        "entry_ts": min(str(row.get("ts") or "") for row in entries),
        "exit_ts": max(str(row.get("ts") or "") for row in exits),
        "realized_pnl": realized_pnl,
        "strategy_plan_id": plan_id,
        "strategy_plan_version": lifecycle.get("strategy_plan_version"),
        "accounting_identity_resolution": "nautilus_dca_aggregate_round",
    }
    return plan_id, aggregate, exit_order_ids


def _dedupe_fills(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse byte-equivalent retries; conflicting IDs fail in the projector."""

    by_id: dict[str, dict[str, Any]] = {}
    result: list[dict[str, Any]] = []
    for row in rows:
        fill_id = str(row.get("fill_id") or "")
        previous = by_id.get(fill_id)
        if previous == row:
            continue
        if previous is None:
            by_id[fill_id] = row
        result.append(row)
    return result
