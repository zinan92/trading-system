"""Canonical read-side accounting for versioned production StrategyPlans."""

from __future__ import annotations

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
        fills.extend(
            {**row, "source_cycle_id": cycle_id}
            for row in snapshot.get("fills") or []
            if isinstance(row, dict) and row.get("strategy_plan_id") not in (None, "")
        )
        positions.extend(
            {**row, "source_cycle_id": cycle_id}
            for row in snapshot.get("positions") or []
            if isinstance(row, dict) and row.get("strategy_plan_id") not in (None, "")
        )


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
