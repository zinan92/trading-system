from __future__ import annotations

from pathlib import Path

from pipelines.dualtrack_machine_residual_check import build_residual_check
from services.journal_store import write_json


def test_historical_machine_target_without_explicit_units_closes_full_remaining_lot(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    cycle_id = "2026-07-09_DAY"
    write_json(output / "dualtrack" / "fills" / f"{cycle_id}_machine.json", [
        {"fill_id": "entry", "event": "entry", "side": "buy", "price": 100.0, "notional": 10_000.0, "layer": "grid", "rung": 0, "realized_pnl": -0.5},
        {"fill_id": "target", "event": "target", "side": "sell", "price": 110.0, "notional": 10_000.0, "layer": "grid", "rung": 0, "realized_pnl": 999.5},
    ])

    result = build_residual_check(output, cycle_id=cycle_id)

    assert result["status"] == "pass"
    assert result["residuals"] == []
    assert result["raw_fills_immutable"] is True
