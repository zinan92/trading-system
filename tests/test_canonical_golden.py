from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trading_strategy.dca_plan import (
    build_dca_entry_commands,
    build_dca_preview,
    build_dca_strategy_plan,
    replay_dca_marks,
)
from trading_strategy.grid_core import (
    Bar,
    GridLineLifecycle,
    GridStop,
    simulate_conditional_grid,
    simulate_explicit_grid,
)
from trading_strategy.grid_sizing import build_grid_preview


FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "canonical_golden.json").read_text(
        encoding="utf-8"
    )
)


def _dca_config() -> dict:
    return {
        "capital_per_track_usd": 10_000,
        "max_leverage": 10,
        "cost_per_side_bp": 0.5,
        "execution_contract": {
            "price_increment": "0.01",
            "quantity_increment": "0.001",
        },
    }


def _dca_market(price: float = 4010.0) -> dict:
    return {
        "status": "ready",
        "fresh": True,
        "is_synthetic": False,
        "provider": "fixture-provider",
        "symbol": "GOLD",
        "timeframe": "1m",
        "latest_close": price,
        "latest_timestamp": "2026-07-22T00:19:00+00:00",
        "bars": [
            {
                "timestamp": f"2026-07-22T00:{index:02d}:00+00:00",
                "open": price,
                "high": price + 1,
                "low": price - 1,
                "close": price,
            }
            for index in range(20)
        ],
    }


def _dca_payload() -> dict:
    return {
        "direction": "long",
        "dca": {
            "entry_levels": [4004.0, 3996.0, 3988.0],
            "target_price": 4050.0,
            "stop_price": 3970.0,
            "notional_per_addition": 2000.0,
            "max_additions": 3,
            "loop_enabled": False,
        },
        "risk_budget": {"leverage": 10},
    }


def _grid_config() -> dict:
    return {
        "capital_per_track_usd": 10_000,
        "max_leverage": 10,
        "cost_per_side_bp": 0.5,
        "execution_contract": {
            "price_increment": "0.01",
            "quantity_increment": "0.001",
        },
        "strategy_grid": {
            "range_timeframe": "1d",
            "range_atr_period": 14,
            "spacing_timeframe": "4h",
            "spacing_atr_period": 14,
            "execution_timeframe": "1m",
            "min_grid_count": 2,
            "max_grid_count": 4,
            "cost_spacing_multiple": 5.0,
            "default_mode": "arithmetic",
            "capital_utilization_cap": 1.0,
            "required_leverage": 10.0,
            "min_net_profit_per_grid_usd": 1.0,
            "styles": {
                "steady": {
                    "range_atr_multiple": 2.0,
                    "spacing_atr_multiple": 0.25,
                },
                "aggressive": {
                    "range_atr_multiple": 1.0,
                    "spacing_atr_multiple": 0.125,
                },
            },
        },
    }


def _grid_market(close: float = 110.0) -> dict:
    def context(span: float) -> list[dict]:
        return [
            {
                "timestamp": f"2026-06-{index + 1:02d}T00:00:00+00:00",
                "open": close - 1 + index * 0.05 - 0.1,
                "high": close - 1 + index * 0.05 + span / 2,
                "low": close - 1 + index * 0.05 - span / 2,
                "close": close - 1 + index * 0.05,
            }
            for index in range(20)
        ]

    return {
        "status": "ready",
        "fresh": True,
        "is_synthetic": False,
        "provider": "fixture",
        "symbol": "GOLD",
        "timeframe": "1m",
        "latest_close": close,
        "latest_timestamp": "2026-07-05T01:39:00+00:00",
        "bars": context(1.0),
        "strategy_timeframes": {
            "1d": {
                "provider": "fixture",
                "is_synthetic": False,
                "bars": context(10.0),
            },
            "4h": {
                "provider": "fixture",
                "is_synthetic": False,
                "bars": context(4.0),
            },
        },
    }


def _grid_bar(index: int, close: float, previous: float) -> Bar:
    timestamp = (
        datetime(2026, 7, 5, 1, 0, tzinfo=timezone.utc)
        + timedelta(minutes=index)
    ).isoformat()
    return Bar(
        symbol="GOLD",
        timeframe="1m",
        timestamp=timestamp,
        open=previous,
        high=max(previous, close),
        low=min(previous, close),
        close=close,
        volume=1,
        provider="fixture",
    )


def _grid_bars(closes: list[float]) -> list[Bar]:
    rows: list[Bar] = []
    previous = closes[0]
    for index, close in enumerate(closes):
        rows.append(_grid_bar(index, close, previous))
        previous = close
    return rows


def _grid_preview_summary(preview: dict) -> dict:
    return {
        "schema_version": preview["schema_version"],
        "cycle_id": preview["cycle_id"],
        "preview_id": preview["preview_id"],
        "direction": preview["direction"],
        "style": preview["style"],
        "range": preview["range"],
        "grid": preview["grid"],
        "orders": preview["orders"],
        "risk": preview["risk"],
    }


def test_dca_extraction_matches_locked_golden_plan_commands_and_terminal_semantics() -> None:
    preview = build_dca_preview(
        "golden-dca",
        _dca_payload(),
        market=_dca_market(),
        account={"equity": 10_000},
        config=_dca_config(),
    )
    plan = build_dca_strategy_plan(
        preview,
        strategy_plan_id="strategy-plan-dca-golden",
        version=1,
        locked_at="2026-07-22T16:00:00+00:00",
    )
    commands = build_dca_entry_commands(
        plan,
        timestamp="2026-07-22T16:00:00+00:00",
    )

    assert preview == FIXTURE["dca"]["preview"]
    assert plan == FIXTURE["dca"]["plan"]
    assert commands == FIXTURE["dca"]["commands"]
    assert preview["dca"]["loop_enabled"] is False
    assert preview["aggregate_take_profit"]["one_active_order_required"] is True
    assert preview["aggregate_take_profit"]["replace_after_each_entry_fill"] is True
    assert replay_dca_marks(preview, [4004.0, 3996.0, 4050.0, 3970.0]) == FIXTURE["dca"]["target_replay"]
    assert replay_dca_marks(preview, [4004.0, 3970.0]) == FIXTURE["dca"]["stop_replay"]
    assert FIXTURE["dca"]["target_replay"]["status"] == "target_closed"
    assert FIXTURE["dca"]["stop_replay"]["status"] == "stop_closed"


def test_grid_plan_geometry_matches_locked_golden_levels_quantities_and_risk() -> None:
    preview = build_grid_preview(
        "golden-grid-preview",
        {
            "direction": "neutral",
            "style": "steady",
            "grid": {
                "count": 4,
                "mode": "arithmetic",
                "notional_per_grid": 1000.0,
                "notional_mode": "manual",
            },
        },
        market=_grid_market(),
        account={"equity": 10_000},
        config=_grid_config(),
        allow_unsafe_manual_preview=True,
    )

    assert _grid_preview_summary(preview) == FIXTURE["grid"]["preview"]
    assert preview["grid"]["levels"] == [90.0, 100.0, 110.0, 120.0, 130.0]
    assert [order["quantity"] for order in preview["orders"]] == [11.111, 10.0, 8.333, 7.692]
    assert [order["tp"] for order in preview["orders"]] == [100.0, 110.0, 110.0, 120.0]
    assert [order["sl"] for order in preview["orders"]] == [80.0, 80.0, 140.0, 140.0]


def test_grid_rung_rearm_and_hard_stop_match_locked_golden_lifecycle() -> None:
    rearm_result = simulate_explicit_grid(
        cycle_id="golden-grid",
        bars=_grid_bars([4000.0, 4010.0, 4000.0, 4010.0]),
        direction=1,
        orders=[{"entry": 4000.0, "take_profit": 4010.0, "weight": 1.0}],
        stop=GridStop(side="below", price=3990.0),
        rung_notional=1000.0,
        cost_per_side_bp=0.5,
        finalize=False,
    )
    hard_stop_result = simulate_explicit_grid(
        cycle_id="golden-grid-stop",
        bars=_grid_bars([4000.0, 3990.0, 3980.0]),
        direction=1,
        orders=[{"entry": 4000.0, "take_profit": 4010.0, "weight": 1.0}],
        stop=GridStop(side="below", price=3990.0),
        rung_notional=1000.0,
        cost_per_side_bp=0.5,
        finalize=False,
    )

    assert asdict(rearm_result) == FIXTURE["grid"]["explicit_rearm"]
    assert asdict(hard_stop_result) == FIXTURE["grid"]["hard_stop"]
    assert rearm_result.rearms == 2
    assert hard_stop_result.stop_hit is True
    assert hard_stop_result.rearms == 0

    line = GridLineLifecycle(
        line_id="golden-line",
        armed_at="2026-07-05T01:00:00+00:00",
        requested_quantity=10.0,
    )
    line.apply_entry_fill(
        fill_id="entry-partial",
        quantity=4.0,
        at="2026-07-05T01:01:00+00:00",
    )
    line.apply_close_fill(
        fill_id="close-partial",
        quantity=4.0,
        at="2026-07-05T01:02:00+00:00",
        rearm=True,
    )
    line.confirm_entry_cancelled(
        at="2026-07-05T01:03:00+00:00",
        reason="cancel remainder",
    )
    assert line.snapshot() == FIXTURE["grid"]["line_after_partial_close_cancel"]
    assert line.state == "rearmed"
    assert line.generation == 2
    assert line.can_enter is True


def test_dca_loop_reopen_flag_remains_rejected() -> None:
    payload = _dca_payload()
    payload["dca"]["loop_enabled"] = True
    with pytest.raises(ValueError, match="loop_enabled=true is not supported"):
        build_dca_preview(
            "golden-dca-loop-rejected",
            payload,
            market=_dca_market(),
            account={"equity": 10_000},
            config=_dca_config(),
        )


def test_grid_conditional_replay_matches_locked_golden_stop_rearm_and_flatten() -> None:
    common = {
        "direction": 1,
        "spacing_bp": 20.0,
        "range_k": 1.0,
        "rung_notional": 1000.0,
        "max_rungs": 10,
        "cost_per_side_bp": 0.5,
    }
    same_bar = simulate_conditional_grid(
        cycle_id="golden-conditional-hard-stop",
        bars=_grid_bars([4000.0, 3940.0]),
        prev_range=100.0,
        re_arm_max=1,
        stop=GridStop(side="below", price=3950.0),
        **common,
    )
    synthetic_rearm = simulate_conditional_grid(
        cycle_id="golden-conditional-rearm",
        bars=_grid_bars([4000.0, 3920.0, 4000.0]),
        prev_range=80.0,
        re_arm_max=1,
        stop=None,
        **common,
    )
    terminal_flatten = simulate_conditional_grid(
        cycle_id="golden-conditional-flatten",
        bars=_grid_bars([4000.0, 3990.0]),
        prev_range=80.0,
        re_arm_max=0,
        stop=None,
        **common,
    )

    expected = FIXTURE["conditional_grid"]
    assert asdict(same_bar) == expected["same_bar_hard_stop"]
    assert asdict(synthetic_rearm) == expected["synthetic_range_rearm"]
    assert asdict(terminal_flatten) == expected["terminal_flatten"]
    assert same_bar.stop_hit is True
    assert same_bar.rearms == 0
    assert same_bar.fills == []
    assert synthetic_rearm.rearms == 1
    assert any(fill["event"] == "flatten" for fill in terminal_flatten.fills)


def test_grid_line_duplicate_fill_is_idempotent_across_snapshot_restore() -> None:
    line = GridLineLifecycle(
        line_id="golden-idempotent-line",
        armed_at="2026-07-05T01:00:00+00:00",
        requested_quantity=10.0,
    )
    line.apply_entry_fill(
        fill_id="entry-1",
        quantity=10.0,
        at="2026-07-05T01:01:00+00:00",
    )
    restored = GridLineLifecycle.from_snapshot(line.snapshot())
    replay = restored.apply_entry_fill(
        fill_id="entry-1",
        quantity=10.0,
        at="2026-07-05T01:01:00+00:00",
    )
    assert replay["idempotent"] is True
    assert restored.snapshot() == line.snapshot()
