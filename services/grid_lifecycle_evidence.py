"""Read-only, fail-closed evidence packages for Nautilus Paper Grid lines."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from services.journal_store import load_json


GRID_LIFECYCLE_EVIDENCE_SCHEMA = "grid-line-lifecycle-evidence-v1"


def build_grid_lifecycle_evidence(
    output_root: Path,
    *,
    cycle_id: str,
    execution_snapshot: Mapping[str, Any],
    reconciliation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Link completed Grid lines only when immutable facts agree.

    This is an observational package.  It never infers a fill from a candle,
    repairs identities, or changes engine state.  Incomplete linkage remains
    visible as `unverified` rather than becoming a completed lifecycle claim.
    """

    output = Path(output_root)
    commands = _commands(output, cycle_id)
    transitions = _rows(output / "dualtrack" / "grid_lifecycle" / f"{cycle_id}_nautilus.json")
    fills = [dict(row) for row in execution_snapshot.get("fills") or [] if isinstance(row, Mapping)]
    engine = str(execution_snapshot.get("engine") or "")
    reconciliation_status = str((reconciliation or {}).get("status") or "unknown")
    lines = _grid_lines(commands)
    packages: list[dict[str, Any]] = []
    for line_id, generations in sorted(lines.items()):
        for generation, command_id, command in generations:
            package = _line_generation_package(
                line_id=line_id,
                generation=generation,
                command_id=command_id,
                command=command,
                generations=generations,
                fills=fills,
                transitions=transitions,
                reconciliation_status=reconciliation_status,
            )
            if package is not None:
                packages.append(package)
    verified = [row for row in packages if row["status"] == "completed_rearmed"]
    incomplete = [row for row in packages if row["status"] == "unverified"]
    return {
        "schema_version": GRID_LIFECYCLE_EVIDENCE_SCHEMA,
        "cycle_id": cycle_id,
        "engine": engine or None,
        "reconciliation_status": reconciliation_status,
        "status": (
            "verified"
            if verified
            else "unverified"
            if incomplete
            else "unavailable"
        ),
        "completed_rearmed_count": len(verified),
        "unverified_count": len(incomplete),
        "lines": packages,
        "source": {
            "commands_path": str(output / "dualtrack" / "nautilus_authoritative" / "commands" / f"{cycle_id}.json"),
            "lifecycle_path": str(output / "dualtrack" / "grid_lifecycle" / f"{cycle_id}_nautilus.json"),
            "fill_source": "current_execution_snapshot",
            "synthetic_candle_fill_inference": False,
        },
    }


def _commands(output: Path, cycle_id: str) -> list[tuple[str, dict[str, Any]]]:
    rows = _rows(output / "dualtrack" / "nautilus_authoritative" / "commands" / f"{cycle_id}.json")
    result = []
    for row in rows:
        command_id = str(row.get("command_id") or "")
        command = row.get("command") if isinstance(row.get("command"), Mapping) else {}
        if command_id and isinstance(command, Mapping):
            result.append((command_id, dict(command)))
    return result


def _grid_lines(commands: list[tuple[str, dict[str, Any]]]) -> dict[str, list[tuple[int, str, dict[str, Any]]]]:
    lines: dict[str, list[tuple[int, str, dict[str, Any]]]] = {}
    for command_id, command in commands:
        if not _is_grid_entry(command):
            continue
        line_id = str(command.get("grid_line_id") or command_id)
        generation = int(command.get("grid_generation") or 1)
        lines.setdefault(line_id, []).append((generation, command_id, command))
    for generations in lines.values():
        generations.sort(key=lambda row: row[0])
    return lines


def _is_grid_entry(command: Mapping[str, Any]) -> bool:
    if str(command.get("event") or "entry").lower() != "entry":
        return False
    if str(command.get("order_type") or "market").lower() != "limit":
        return False
    if command.get("sl") in (None, "") or command.get("tp") in (None, ""):
        return False
    if command.get("grid_rearm_enabled") is True:
        return bool(str(command.get("grid_line_id") or ""))
    return (
        str(command.get("source") or "") == "strategy_production_console"
        and str(command.get("source_fill_id") or "").startswith("strategy-grid:")
        and bool(str(command.get("strategy_plan_id") or ""))
    )


def _line_generation_package(
    *,
    line_id: str,
    generation: int,
    command_id: str,
    command: Mapping[str, Any],
    generations: list[tuple[int, str, dict[str, Any]]],
    fills: list[dict[str, Any]],
    transitions: list[dict[str, Any]],
    reconciliation_status: str,
) -> dict[str, Any] | None:
    entries = _fills(fills, command_id, "entry")
    exits = _fills(fills, command_id, "target")
    if not entries and not exits:
        return None
    entry_quantity = sum(float(row.get("quantity") or 0.0) for row in entries)
    exit_quantity = sum(float(row.get("quantity") or 0.0) for row in exits)
    entry_ids = {str(row.get("fill_id") or "") for row in entries} - {""}
    exit_ids = {str(row.get("fill_id") or "") for row in exits} - {""}
    entry_transition = _has_transition(
        transitions, line_id, generation, "entry_fill_confirmed", entry_ids
    )
    close_transition = _has_transition(
        transitions, line_id, generation, "close_fill_confirmed_rearm", exit_ids
    )
    next_generation = next(
        (row for row in generations if row[0] == generation + 1),
        None,
    )
    rearm_matches = bool(
        next_generation
        and str(next_generation[2].get("rearm_of_order_id") or "") == command_id
        and _same_number(next_generation[2].get("price"), command.get("price"))
    )
    evidence_missing = []
    if entry_quantity <= 0 or not entry_ids:
        evidence_missing.append("entry_fill")
    if exit_quantity + 1e-9 < entry_quantity or not exit_ids:
        evidence_missing.append("target_fill")
    if not entry_transition:
        evidence_missing.append("entry_transition")
    if not close_transition:
        evidence_missing.append("target_rearm_transition")
    if not rearm_matches:
        evidence_missing.append("original_price_reorder")
    if reconciliation_status not in {"ok", "pass"}:
        evidence_missing.append("reconciliation_pass")
    return {
        "line_id": line_id,
        "generation": generation,
        "status": "completed_rearmed" if not evidence_missing else "unverified",
        "evidence_missing": evidence_missing,
        "strategy_plan_id": str(command.get("strategy_plan_id") or "") or None,
        "strategy_plan_version": command.get("strategy_plan_version"),
        "entry": {
            "order_id": command_id,
            "trade_id": command_id,
            "fill_ids": sorted(entry_ids),
            "quantity": entry_quantity,
            "price": command.get("price"),
        },
        "target": {
            "order_ids": sorted({str(row.get("order_id") or "") for row in exits} - {""}),
            "fill_ids": sorted(exit_ids),
            "quantity": exit_quantity,
        },
        "reorder": (
            {
                "order_id": next_generation[1],
                "generation": next_generation[0],
                "price": next_generation[2].get("price"),
                "original_price": command.get("price"),
            }
            if next_generation
            else None
        ),
        "reconciliation_status": reconciliation_status,
    }


def _fills(rows: list[dict[str, Any]], command_id: str, event: str) -> list[dict[str, Any]]:
    return [
        row for row in rows
        if str(row.get("trade_id") or "") == command_id
        and str(row.get("event") or "").lower() == event
    ]


def _has_transition(
    rows: list[dict[str, Any]],
    line_id: str,
    generation: int,
    event: str,
    fill_ids: set[str],
) -> bool:
    return any(
        str(row.get("line_id") or "") == line_id
        and int(row.get("generation") or 0) == generation
        and str(row.get("event") or "") == event
        and str(row.get("fill_id") or "") in fill_ids
        for row in rows
    )


def _rows(path: Path) -> list[dict[str, Any]]:
    rows = load_json(path)
    return [dict(row) for row in rows if isinstance(row, Mapping)] if isinstance(rows, list) else []


def _same_number(left: Any, right: Any) -> bool:
    try:
        return abs(float(left) - float(right)) <= 1e-9
    except (TypeError, ValueError):
        return False
