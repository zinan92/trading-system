from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest

from services.dualtrack_execution_adapter import build_execution_engine_adapter
from services.strategy_control_plane import StrategyControlPlane


def proposal(cycle_id: str, source: str, direction: str = "long") -> dict:
    return {
        "cycle_id": cycle_id,
        "source": source,
        "direction": direction,
        "range": {"low": 100.0, "high": 120.0},
        "key_levels": [100.0, 110.0, 120.0],
        "grid": {"count": 4, "notional_per_grid": 250.0, "spacing": 5.0},
        "signal": {"name": "ema_trend", "confidence": 7},
        "tp_sl": {"tp": 118.0, "sl": 96.0, "r_multiple": 2.0},
        "risk_budget": {"max_loss": 100.0, "max_leverage": 3.0},
        "intraday_rules": [{"if": "range_break", "then": "stand_down"}],
    }


def market(*, close: float = 110.0, fresh: bool = True) -> dict:
    bars = []
    for index in range(40):
        bar_close = close - 2.0 + index * 0.05
        bars.append({
            "timestamp": f"2026-07-05T01:{index:02d}:00+00:00",
            "open": round(bar_close - 0.1, 4),
            "high": round(bar_close + 0.4, 4),
            "low": round(bar_close - 0.4, 4),
            "close": round(bar_close, 4),
        })
    def context_bars(timeframe: str, span: float) -> list[dict]:
        rows = []
        for index in range(20):
            bar_close = close - 1.0 + index * 0.05
            rows.append({
                "timestamp": f"2026-06-{index + 1:02d}T00:00:00+00:00" if timeframe == "1d" else f"2026-07-02T{(index % 6) * 4:02d}:00:00+00:00",
                "open": round(bar_close - 0.1, 4),
                "high": round(bar_close + span / 2.0, 4),
                "low": round(bar_close - span / 2.0, 4),
                "close": round(bar_close, 4),
            })
        return rows

    return {
        "status": "ready" if fresh else "stale",
        "fresh": fresh,
        "is_synthetic": False,
        "provider": "binance_usdm",
        "source_mode": "binance_usdm",
        "symbol": "GOLD",
        "timeframe": "1m",
        "latest_close": close,
        "latest_timestamp": "2026-07-05T01:39:00+00:00",
        "bars": bars,
        "strategy_timeframes": {
            "1d": {"timeframe": "1d", "provider": "derived:binance_usdm", "is_synthetic": False, "bars": context_bars("1d", 10.0)},
            "4h": {"timeframe": "4h", "provider": "derived:binance_usdm", "is_synthetic": False, "bars": context_bars("4h", 4.0)},
        },
    }


def test_plan_proposals_share_schema_and_active_plan_has_field_sources(tmp_path: Path) -> None:
    plane = StrategyControlPlane(tmp_path / "outputs")
    cycle_id = "2026-07-05_DAY"
    human = plane.upsert_proposal(proposal(cycle_id, "human"), now="2026-07-05T01:00:00+00:00")
    ai = plane.upsert_proposal(proposal(cycle_id, "ai", "short"), now="2026-07-05T01:00:01+00:00")

    active = plane.lock_production_plan(
        cycle_id,
        selected_proposal_id=human["proposal_id"],
        field_sources={"direction": "human", "range": "human", "grid": "human", "signal": "human"},
        now="2026-07-05T01:00:02+00:00",
    )

    assert human["schema_version"] == ai["schema_version"] == "strategy-plan-proposal-v1"
    assert active["status"] == "active"
    assert active["version"] == 1
    assert active["field_sources"]["direction"] == "human"
    diff = plane.proposal_diff(cycle_id)
    assert diff["fields"]["direction"]["different"] is True
    assert plane.active_plan(cycle_id)["strategy_plan_id"] == active["strategy_plan_id"]


def test_legacy_plans_are_read_as_compatible_proposals_without_erasing_history(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    plane = StrategyControlPlane(output)
    cycle_id = "2026-07-05_DAY"
    legacy = proposal(cycle_id, "human") | {"author": "human", "locked_at": "2026-07-05T01:00:00+00:00"}
    from services.journal_store import write_json

    write_json(output / "dualtrack" / "plans" / f"{cycle_id}_human.json", [legacy])
    result = plane.read_model(cycle_id, as_of="2026-07-05T01:01:00+00:00")

    assert result["migration"]["legacy_compatible"] is True
    assert result["proposals"][0]["source"] == "human"
    assert result["production_plan"]["field_sources"]["direction"] == "human"
    assert (output / "dualtrack" / "plans" / f"{cycle_id}_human.json").exists()


def test_production_controls_are_durable_and_preserve_history(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    plane = StrategyControlPlane(output)
    cycle_id = "2026-07-05_DAY"
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])

    assert plane.control(
        cycle_id,
        "start",
        {"direction": "long", "style": "steady"},
        market=market(),
        account={"ending_cash": 10_000.0},
        now="2026-07-05T01:40:00+00:00",
    )["runtime"]["desired_state"] == "running"
    revised = plane.control(cycle_id, "adjust_plan", {"range": {"low": 98.0, "high": 122.0}})["plan"]
    assert revised["version"] == 3
    assert plane.active_plan(cycle_id)["range"]["low"] == 98.0
    assert plane.control(cycle_id, "reset_statistics")["historical_records_preserved"] is True
    assert plane.control(cycle_id, "stop", now="2026-07-05T01:41:00+00:00")["runtime"]["desired_state"] == "stopped"


def test_preview_direction_and_style_change_grid_geometry_and_order_sides(tmp_path: Path) -> None:
    plane = StrategyControlPlane(tmp_path / "outputs")
    cycle_id = "2026-07-05_DAY"
    account = {"ending_cash": 10_000.0}

    neutral = plane.preview(cycle_id, {"direction": "neutral", "style": "steady"}, market=market(), account=account)
    long = plane.preview(cycle_id, {"direction": "long", "style": "steady"}, market=market(), account=account)
    short = plane.preview(cycle_id, {"direction": "short", "style": "steady"}, market=market(), account=account)
    aggressive = plane.preview(cycle_id, {"direction": "neutral", "style": "aggressive"}, market=market(), account=account)

    assert neutral["grid"]["count"] == long["grid"]["count"] == short["grid"]["count"] >= 24
    assert aggressive["grid"]["count"] >= 24
    assert neutral["range"]["high"] - neutral["range"]["low"] > aggressive["range"]["high"] - aggressive["range"]["low"]
    assert neutral["range"] == long["range"] == short["range"]
    assert neutral["range"]["source_timeframe"] == "1d"
    assert neutral["grid"]["spacing_source_timeframe"] == "4h"
    assert {order["side"] for order in neutral["orders"]} == {"buy", "sell"}
    assert {order["side"] for order in long["orders"]} == {"buy"}
    assert {order["side"] for order in short["orders"]} == {"sell"}
    assert all(order["price"] < market()["latest_close"] for order in long["orders"])
    assert all(order["price"] > market()["latest_close"] for order in short["orders"])
    assert neutral["risk"]["estimated_margin"] > 0
    assert neutral["risk"]["max_loss"] > 0
    assert neutral["grid"]["notional_per_grid"] > 100
    assert neutral["risk"]["absolute_notional_ceiling"] == 100_000
    assert neutral["risk"]["capital_budget"] == 50_000
    assert neutral["risk"]["calibration_status"] == "shadow_candidate"


def test_preview_strategy_geometry_is_independent_of_chart_timeframe(tmp_path: Path) -> None:
    plane = StrategyControlPlane(tmp_path / "outputs")
    one_minute = market()
    thirty_minute = market()
    thirty_minute["timeframe"] = "30m"
    request = {"direction": "neutral", "style": "steady"}

    left = plane.preview("2026-07-05_DAY", request, market=one_minute, account={"ending_cash": 10_000.0})
    right = plane.preview("2026-07-05_DAY", request, market=thirty_minute, account={"ending_cash": 10_000.0})

    assert left["range"] == right["range"]
    assert left["grid"] == right["grid"]
    assert left["orders"] == right["orders"]
    assert left["strategy_timeframes"] == {"range": "1d", "spacing": "4h", "execution": "1m"}


def test_auto_notional_revalidates_against_start_market_while_manual_notional_remains_strict(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    plane = StrategyControlPlane(output)
    cycle_id = "2026-07-05_DAY"
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])
    account = {"ending_cash": 10_000.0}
    geometry = {"low": 90.0, "high": 130.0}

    earlier = plane.preview(
        cycle_id,
        {"direction": "neutral", "style": "steady", "range": geometry, "grid": {"count": 24}},
        market=market(close=110.0),
        account=account,
    )
    stale_notional = earlier["grid"]["notional_per_grid"]

    with pytest.raises(ValueError, match="exceeds safe cap"):
        plane.preview(
            cycle_id,
            {
                "direction": "neutral",
                "style": "steady",
                "range": geometry,
                "grid": {"count": 24, "notional_per_grid": stale_notional, "notional_mode": "manual"},
            },
            market=market(close=120.0),
            account=account,
        )

    started = plane.control(
        cycle_id,
        "start",
        {
            "direction": "neutral",
            "style": "steady",
            "range": geometry,
            "grid": {"count": 24, "notional_per_grid": stale_notional, "notional_mode": "auto"},
            "risk_budget": {"leverage": 10.0},
        },
        market=market(close=120.0),
        account=account,
        now="2026-07-05T01:40:00+00:00",
    )

    assert started["preview"]["grid"]["notional_mode"] == "auto"
    assert started["preview"]["grid"]["notional_per_grid"] < stale_notional
    assert started["preview"]["grid"]["notional_per_grid"] == started["preview"]["risk"]["risk_notional_cap_per_grid"]
    assert started["plan"]["grid"]["notional_per_grid"] == started["preview"]["grid"]["notional_per_grid"]
    assert started["accepted_orders"] > 0


def test_preview_fails_closed_without_fixed_strategy_timeframes(tmp_path: Path) -> None:
    plane = StrategyControlPlane(tmp_path / "outputs")
    invalid = market()
    invalid.pop("strategy_timeframes")

    with pytest.raises(ValueError, match="strategy timeframe 1d is unavailable"):
        plane.preview("2026-07-05_DAY", {"direction": "neutral", "style": "steady"}, market=invalid, account={"ending_cash": 10_000.0})


def test_start_commits_plan_and_real_versioned_paper_orders_idempotently(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    plane = StrategyControlPlane(output)
    cycle_id = "2026-07-05_DAY"
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])
    payload = {"direction": "short", "style": "aggressive"}

    started = plane.control(cycle_id, "start", payload, market=market(), account={"ending_cash": 10_000.0}, now="2026-07-05T01:40:00+00:00")
    orders = build_execution_engine_adapter(output).snapshot(cycle_id)["orders"]

    assert started["runtime"]["desired_state"] == "running"
    assert started["runtime"]["actual_state"] == "running"
    assert started["created_orders"] == len(orders) > 0
    assert started["plan"]["direction"] == "short"
    assert started["plan"]["version"] == 2
    assert all(order["state"] == "accepted" for order in orders)
    assert all(order["side"] == "sell" for order in orders)
    assert all(order["strategy_plan_id"] == started["plan"]["strategy_plan_id"] for order in orders)
    assert all(order["strategy_plan_version"] == started["plan"]["version"] for order in orders)

    repeated = plane.control(cycle_id, "start", payload, market=market(), account={"ending_cash": 10_000.0}, now="2026-07-05T01:41:00+00:00")
    repeated_orders = build_execution_engine_adapter(output).snapshot(cycle_id)["orders"]
    assert repeated["created_orders"] == 0
    assert repeated["idempotent"] is True
    assert repeated["plan"]["strategy_plan_id"] == started["plan"]["strategy_plan_id"]
    assert [order["order_id"] for order in repeated_orders] == [order["order_id"] for order in orders]


def test_start_is_fail_closed_for_stale_market(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    plane = StrategyControlPlane(output)
    cycle_id = "2026-07-05_DAY"
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])

    with pytest.raises(ValueError, match="market data is stale"):
        plane.control(cycle_id, "start", {"direction": "neutral", "style": "steady"}, market=market(fresh=False), account={"ending_cash": 10_000.0})

    assert plane.runtime_state(cycle_id)["desired_state"] == "stopped"
    assert build_execution_engine_adapter(output).snapshot(cycle_id)["orders"] == []


def test_runtime_state_is_scoped_to_the_requested_cycle(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    plane = StrategyControlPlane(output)
    from services.journal_store import write_json

    write_json(plane.root / "runtime.json", [{
        "cycle_id": "2026-07-04_NIGHT",
        "desired_state": "running",
        "actual_state": "running",
        "accepted_order_count": 8,
        "strategy_plan_id": "strategy-plan-old",
        "strategy_plan_version": 7,
    }])

    current = plane.runtime_state("2026-07-05_DAY")

    assert current["cycle_id"] == "2026-07-05_DAY"
    assert current["desired_state"] == "stopped"
    assert current["actual_state"] == "stopped"
    assert current["accepted_order_count"] == 0
    assert current["stale_cycle"] is True
    assert current["previous_cycle_id"] == "2026-07-04_NIGHT"
    assert current["strategy_plan_id"] is None


def test_stop_cancels_pending_orders_flattens_position_and_reconciles(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    plane = StrategyControlPlane(output)
    cycle_id = "2026-07-05_DAY"
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])
    started = plane.control(cycle_id, "start", {"direction": "long", "style": "steady"}, market=market(), account={"ending_cash": 10_000.0}, now="2026-07-05T01:40:00+00:00")
    adapter = build_execution_engine_adapter(output)
    adapter.submit_order({
        "cycle_id": cycle_id,
        "ts": "2026-07-05T01:45:00+00:00",
        "side": "buy",
        "event": "entry",
        "order_type": "market",
        "price": 110.0,
        "market_price": 110.0,
        "notional": 100.0,
        "sl": 105.0,
        "tp": 115.0,
        "strategy_plan_id": started["plan"]["strategy_plan_id"],
        "strategy_plan_version": started["plan"]["version"],
        "source": "strategy_production_console",
    })

    stopped = plane.control(cycle_id, "stop", {}, market=market(close=111.0), now="2026-07-05T01:46:00+00:00")
    snapshot = adapter.snapshot(cycle_id)

    assert stopped["runtime"]["desired_state"] == "stopped"
    assert stopped["runtime"]["actual_state"] == "stopped"
    assert stopped["cancelled_orders"] > 0
    assert stopped["flattened_positions"] == 1
    assert stopped["reconciliation"]["status"] == "ok"
    assert not [order for order in snapshot["orders"] if order["state"] == "accepted"]
    assert not [position for position in snapshot["positions"] if position["status"] == "open"]


def test_running_adjustment_replaces_pending_grid_without_stopping_runtime(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    plane = StrategyControlPlane(output)
    cycle_id = "2026-07-05_DAY"
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])
    started = plane.control(cycle_id, "start", {"direction": "neutral", "style": "steady"}, market=market(), account={"ending_cash": 10_000.0}, now="2026-07-05T01:40:00+00:00")
    original_orders = [order for order in build_execution_engine_adapter(output).snapshot(cycle_id)["orders"] if order["state"] == "accepted"]

    adjusted = plane.control(
        cycle_id,
        "adjust_plan",
        {
            "direction": "short",
            "style": "aggressive",
            "range": {"low": 108.0, "high": 116.0},
            "grid": {"count": 12, "notional_per_grid": 50.0},
            "risk_budget": {"leverage": 2.0},
        },
        market=market(),
        account={"ending_cash": 10_000.0},
        now="2026-07-05T01:42:00+00:00",
    )
    snapshot = build_execution_engine_adapter(output).snapshot(cycle_id)
    current_orders = [order for order in snapshot["orders"] if order["state"] == "accepted"]

    assert adjusted["runtime"]["desired_state"] == "running"
    assert adjusted["runtime"]["actual_state"] == "running"
    assert adjusted["plan"]["version"] == started["plan"]["version"] + 1
    assert adjusted["cancelled_orders"] == len(original_orders)
    assert adjusted["created_orders"] == len(current_orders) > 0
    assert all(order["side"] == "sell" for order in current_orders)
    assert all(order["strategy_plan_id"] == adjusted["plan"]["strategy_plan_id"] for order in current_orders)


def test_concurrent_refresh_cannot_override_started_plan_or_create_two_active_plans(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    plane = StrategyControlPlane(output)
    cycle_id = "2026-07-05_DAY"
    saved = plane.upsert_proposal(proposal(cycle_id, "ai"))
    plane.lock_production_plan(cycle_id, selected_proposal_id=saved["proposal_id"])

    def work(index: int):
        if index % 2:
            return plane.read_model(cycle_id, as_of="2026-07-05T01:40:00+00:00")["production_plan"]
        return plane.control(
            cycle_id,
            "start",
            {"direction": "short", "style": "aggressive"},
            market=market(),
            account={"ending_cash": 10_000.0},
            now="2026-07-05T01:40:00+00:00",
        )["plan"]

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(work, range(16)))

    active = plane._all_active_plans()
    runtime = plane.runtime_state(cycle_id)
    orders = [order for order in build_execution_engine_adapter(output).snapshot(cycle_id)["orders"] if order["state"] == "accepted"]
    assert len(active) == 1
    assert runtime["strategy_plan_id"] == active[0]["strategy_plan_id"]
    assert orders
    assert all(order["strategy_plan_id"] == active[0]["strategy_plan_id"] for order in orders)
    assert all(result.get("strategy_plan_id") for result in results)
