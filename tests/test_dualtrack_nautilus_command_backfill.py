from __future__ import annotations

from pathlib import Path

import pytest

from pipelines.dualtrack_nautilus_command_backfill import backfill_shadow_commands
from services.journal_store import write_json


class FakeAdapter:
    def __init__(self) -> None:
        self.commands = []
        self.flushes = []

    def submit_order(self, command: dict) -> dict:
        self.commands.append(dict(command))
        return {"state": "accepted", "order_id": command["source_fill_id"]}

    def flush(self, cycle_id: str) -> dict:
        self.flushes.append(cycle_id)
        return {"status": "replayed", "processed_command_count": len(self.commands)}


def _row(cycle_id: str, index: int) -> dict:
    return {
        "schema_version": "dualtrack-shadow-command-v1",
        "command_id": f"legacy-order-{index}",
        "cycle_id": cycle_id,
        "command": {
            "cycle_id": cycle_id,
            "ts": "2026-07-16T01:00:00+00:00",
            "side": "buy",
            "event": "entry",
            "order_type": "limit",
            "price": 4000.0 + index,
            "quantity": 0.1,
            "source_fill_id": f"strategy-grid:plan-1:{index}",
            "strategy_plan_id": "plan-1",
            "strategy_plan_version": 1,
        },
    }


def test_backfill_imports_exact_immutable_commands_and_flushes_once(tmp_path: Path) -> None:
    cycle_id = "2026-07-16_DAY"
    source = tmp_path / "commands.json"
    write_json(source, [_row(cycle_id, 1), _row(cycle_id, 2)])
    adapter = FakeAdapter()

    result = backfill_shadow_commands(adapter, source_path=source, cycle_id=cycle_id)

    assert result["status"] == "replayed"
    assert result["source_command_count"] == 2
    assert adapter.commands == [_row(cycle_id, 1)["command"], _row(cycle_id, 2)["command"]]
    assert adapter.flushes == [cycle_id]


def test_backfill_rejects_missing_payload_or_cross_cycle_command(tmp_path: Path) -> None:
    cycle_id = "2026-07-16_DAY"
    source = tmp_path / "commands.json"
    write_json(source, [{"cycle_id": cycle_id, "command_id": "missing"}])

    with pytest.raises(ValueError, match="missing command payload"):
        backfill_shadow_commands(FakeAdapter(), source_path=source, cycle_id=cycle_id)

    write_json(source, [_row("2026-07-16_NIGHT", 1)])
    with pytest.raises(ValueError, match="cycle_id mismatch"):
        backfill_shadow_commands(FakeAdapter(), source_path=source, cycle_id=cycle_id)
