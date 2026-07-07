from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from schemas.market_data import Bar
from services.lab_r5_grid import Cycle
from services.lab_registry import LabRegistry
from services.lab_tiger_contract_grid import TigerContractGridConfig, run_tiger_contract_grid, simulate_contract_cycle


def _bar(ts: datetime, o: float, h: float, low: float, c: float) -> Bar:
    return Bar("MGCmain", "1m", ts.isoformat(), o, h, low, c, 1, "tiger_openapi:COMEX", [])


def _cycle(closes: list[float], prev_range: float = 20.0) -> Cycle:
    start = datetime(2026, 6, 1, 1, 0, tzinfo=timezone.utc)
    bars = []
    prev = closes[0]
    for idx, close in enumerate(closes):
        bars.append(_bar(start + timedelta(minutes=idx), prev, max(prev, close), min(prev, close), close))
        prev = close
    return Cycle("2026-06-01_DAY", "DAY", tuple(bars), prev_range)


def _cost_rules() -> dict:
    return {
        "venues": {
            "tiger_mgc": {
                "type": "fixed_per_contract",
                "contract_multiplier": 10,
                "spread_pct": 0,
                "slippage_pct": 0,
                "commission_per_contract_side": 2.7,
                "exchange_fee_per_contract_side": 0,
                "clearing_fee_per_contract_side": 0,
            }
        }
    }


def test_contract_cycle_uses_fixed_mgc_contract_costs() -> None:
    cycle = _cycle([4000.0, 3997.0, 4000.0])
    result = simulate_contract_cycle(
        cycle,
        1,
        spacing_usd=2.5,
        range_k=1.0,
        max_contracts=1,
        config=TigerContractGridConfig(spacing_usd=(2.5,), range_k=(1.0,), max_contracts=(1,)),
        cost_rules=_cost_rules(),
    )

    assert result["round_trips"] == 1
    assert result["gross_pnl"] == pytest.approx(25.0)
    assert result["total_cost"] == pytest.approx(5.4)
    assert result["net_pnl"] == pytest.approx(19.6)
    assert result["max_inventory"] == 1


def test_contract_cycle_caps_inventory_by_integer_contracts() -> None:
    cycle = _cycle([4000.0, 3997.0, 3994.0, 4000.0], prev_range=30.0)
    result = simulate_contract_cycle(
        cycle,
        1,
        spacing_usd=2.5,
        range_k=1.0,
        max_contracts=2,
        config=TigerContractGridConfig(spacing_usd=(2.5,), range_k=(1.0,), max_contracts=(2,)),
        cost_rules=_cost_rules(),
    )

    assert result["round_trips"] == 2
    assert result["max_inventory"] == 2
    assert result["total_cost"] == pytest.approx(10.8)


def test_run_tiger_contract_grid_writes_report(tmp_path: Path) -> None:
    bars = []
    start = datetime(2026, 6, 1, 1, 0, tzinfo=timezone.utc)
    price = 4000.0
    for idx in range(720 * 3):
        close = price - 3.0 if idx % 12 == 3 else price + 3.2 if idx % 12 == 6 else price
        bars.append(_bar(start + timedelta(minutes=idx), price, max(price, close), min(price, close), close))
        price = close

    report = run_tiger_contract_grid(
        tmp_path,
        LabRegistry(tmp_path),
        bars,
        config=TigerContractGridConfig(spacing_usd=(2.5,), range_k=(1.0,), max_contracts=(1,), min_cycles=1),
    )

    assert report["trial_count"] == 4
    assert (tmp_path / "lab" / "reports" / "R5_tiger_mgc_contract_grid.md").exists()
    assert "decision_gate" in report
