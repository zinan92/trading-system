"""#1407 (ADR 0009): silver and crude Grid plans, catalog rows and read-model instrument.

Sizing floors each rung's quantity to the venue increment, so the loss Park is shown is the bound. Every
datafeed read here is a fake.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import services.dashboard_control_plane as plane
from services.park_public_read_model import build_park_public_read_model
from services.park_strategy_plan import ParkStrategyPlanError, build_deterministic_risk_plan, normalize_park_input
from tests.test_paper_session_instrument import _definition
from tests.test_park_public_read_model import NOW, _append_jsonl, _fixture

MARKET = {"trusted": True, "fresh": True, "source": "okx_swap_public", "observed_at": "2026-08-14T10:00:00+08:00"}


def _grid(lower: float, upper: float, loss: float) -> dict:
    # Spacing 1 inside lower..upper: rungs lower+1, lower+2, lower+3, hard stop at the lower boundary.
    return normalize_park_input({"direction": "long", "strategy_type": "grid", "lower_price_boundary": lower,
                                 "upper_price_boundary": upper, "grid_spacing": 1, "maximum_leverage": 10,
                                 "maximum_acceptable_loss": loss})


def test_a_crude_plan_floors_a_quantity_that_rounding_would_push_over_the_loss() -> None:
    # By hand: rungs 61, 62, 63, stop 60, so each unit loses 1 + 2 + 3 = 6 if every rung fills and stops out.
    # The loss cap 50.2 allows 50.2 / 6 = 8.3667 units a rung. To the nearest 0.1 that is 8.4, losing 50.4,
    # more than 50.2. Floored it is 8.3: loses 49.8; notional 8.3 x 64 (the top boundary) = 531.2 a rung.
    plan = build_deterministic_risk_plan(_grid(60, 64, 50.2), market={**MARKET, "price": 62.0}, account_equity=1000,
                                         quantity_increment="0.1")
    risk = plan["risk"]
    assert risk["grid_rung_prices"] == [61.0, 62.0, 63.0] and risk["hard_stop"] == 60.0
    assert risk["per_order_quantity"] == 8.3 and risk["quantity_increment"] == "0.1"
    assert risk["theoretical_max_loss"] == pytest.approx(49.8)
    assert risk["per_order_notional"] == pytest.approx(531.2) and risk["maximum_notional"] == pytest.approx(1593.6)


def test_a_silver_plan_floors_to_its_hundredth() -> None:
    # Rungs 51, 52, 53, stop 50: 6 a unit. Cap 100: 16.667 units; nearest 0.01 is 16.67 (loses 100.02); floored 16.66.
    plan = build_deterministic_risk_plan(_grid(50, 54, 100), market={**MARKET, "price": 52.0}, account_equity=1000,
                                         quantity_increment="0.01")
    assert plan["risk"]["per_order_quantity"] == 16.66
    assert plan["risk"]["theoretical_max_loss"] == pytest.approx(99.96)
    assert plan["risk"]["theoretical_max_loss"] <= 100


def test_a_rung_smaller_than_one_increment_is_refused_not_rounded_up() -> None:
    with pytest.raises(ParkStrategyPlanError) as caught:  # 0.1 / 6 = 0.0167 units, below crude's 0.1
        build_deterministic_risk_plan(_grid(60, 64, 0.1), market={**MARKET, "price": 62.0}, account_equity=1000,
                                      quantity_increment="0.1")
    assert caught.value.code == "grid_quantity_below_increment"


def test_silver_and_crude_take_grid_plans_only() -> None:
    dca = normalize_park_input({"direction": "short", "strategy_type": "dca", "upper_price_boundary": 54,
                                "lower_price_boundary": 50, "maximum_leverage": 10, "stop_price": 54.5,
                                "take_profit_price": 49})
    with pytest.raises(ParkStrategyPlanError) as caught:
        build_deterministic_risk_plan(dca, market={**MARKET, "price": 52.0}, account_equity=1000, quantity_increment="0.01")
    assert caught.value.code == "paper_instrument_grid_only"


def test_a_gold_plan_is_sized_as_before() -> None:
    gold = _grid(4300, 4304, 50.2)
    assert build_deterministic_risk_plan(gold, market={**MARKET, "price": 4302.0}, account_equity=1000) == \
        build_deterministic_risk_plan(gold, market={**MARKET, "price": 4302.0}, account_equity=1000, quantity_increment=None)
    assert "quantity_increment" not in build_deterministic_risk_plan(gold, market={**MARKET, "price": 4302.0},
                                                                     account_equity=1000)["risk"]


# --- the GridMind catalog ----------------------------------------------------


@pytest.fixture
def swap_reads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    reads: list[str] = []

    def fetch(*, endpoint: str, source: str, **_kwargs) -> dict:
        reads.append(endpoint.rsplit("/", 1)[-1])
        return _definition(endpoint.rsplit("/", 1)[-1])

    monkeypatch.setattr(plane, "_paper_swap_fetch", fetch)
    monkeypatch.setattr(plane, "_paper_swap_cache", {})
    return reads


def _okx_rows() -> dict[str, dict]:
    catalog = plane.DashboardControlPlane(Path("/tmp/dashboard-control-test")).catalog()
    profiles = {row["id"]: row for row in catalog["venue_profiles"]}
    assert not [r for r in profiles["hyperliquid.testnet"]["instruments"] if "USDT-SWAP" in r["instrument_id"]]
    return {row["instrument_id"]: row for row in profiles["okx.paper"]["instruments"]}


def test_the_okx_paper_profile_lists_silver_and_crude_with_datafeed_specs(swap_reads: list[str]) -> None:
    rows = _okx_rows()
    assert {"XAU-USDT-SWAP.OKX", "XAG-USDT-SWAP.OKX", "CL-USDT-SWAP.OKX"} <= set(rows)
    assert rows["XAG-USDT-SWAP.OKX"]["price_decimals"] == 2 and rows["XAG-USDT-SWAP.OKX"]["size_decimals"] == 2
    assert rows["CL-USDT-SWAP.OKX"]["size_decimals"] == 1 and rows["CL-USDT-SWAP.OKX"]["eligibility"] == "eligible"
    _okx_rows()
    assert sorted(swap_reads) == ["CL-USDT-SWAP", "XAG-USDT-SWAP"]  # cached: the second catalog read asks nobody


def test_an_unreadable_definition_lists_the_instrument_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    def down(**_kwargs) -> dict:
        raise RuntimeError("execution instrument definition unavailable")

    monkeypatch.setattr(plane, "_paper_swap_fetch", down)
    monkeypatch.setattr(plane, "_paper_swap_cache", {})
    silver = _okx_rows()["XAG-USDT-SWAP.OKX"]
    assert silver["eligibility"] == "blocked" and silver["blockers"] == ["paper_instrument_unavailable"]
    assert silver["price_decimals"] is None and silver["size_decimals"] is None  # never guessed specs


# --- the read model ----------------------------------------------------------


def test_the_read_model_names_the_running_instrument(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    _fixture(output)
    identity = output / "park_strategy" / "identity.jsonl"
    rows = [json.loads(line) for line in identity.read_text().splitlines()]
    _append_jsonl(identity, [{**rows[0], "paper_instrument": "silver"}])

    assert build_park_public_read_model(output, now=lambda: NOW)["strategy"]["paper_instrument"] == "silver"


def test_a_session_from_before_1406_reads_as_gold(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    _fixture(output)
    assert build_park_public_read_model(output, now=lambda: NOW)["strategy"]["paper_instrument"] == "gold"


def test_the_terminal_names_the_instrument_the_ended_session_ran(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    _fixture(output)
    identity = output / "park_strategy" / "identity.jsonl"
    rows = [json.loads(line) for line in identity.read_text().splitlines()]
    _append_jsonl(identity, [{**rows[0], "paper_instrument": "crude"}])
    _append_jsonl(output / "park_strategy" / "executions.jsonl", [{
        "event": "terminal_paused", "strategy_session_id": "session-1", "strategy_revision_id": "revision-1",
        "plan_digest": "sha256:" + "a" * 64,
        "result": {"terminal_reason": "hard_stop", "reconciliation": {"status": "ok", "issues": []},
                   "next_action": "await_park_next_strategy"}}])

    assert build_park_public_read_model(output, now=lambda: NOW)["terminal"]["paper_instrument"] == "crude"
