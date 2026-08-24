"""Print the source-baseline canonical golden fixture to stdout.

This is a provenance tool, not a runtime dependency of ``trading_strategy``.
It imports only the frozen source checkout named in the extraction contract and
prints deterministic JSON. The committed fixture is checked with ``diff``.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path


SOURCE_ROOT = Path("/Users/wendy/work/trading-system-testnet")
SOURCE_BASELINE_SHA = "b841800ee03fd98107063c0cbbf5144096a5c4c0"
sys.path.insert(0, str(SOURCE_ROOT))

from services.dca_plan import (  # noqa: E402
    build_dca_entry_commands,
    build_dca_preview,
    build_dca_strategy_plan,
    replay_dca_marks,
)
from services.dualtrack_grid_core import (  # noqa: E402
    Bar,
    GridLineLifecycle,
    GridStop,
    simulate_conditional_grid,
    simulate_explicit_grid,
)
from services.grid_sizing import build_grid_preview  # noqa: E402


DCA_CONFIG = {
    "capital_per_track_usd": 10000,
    "max_leverage": 10,
    "cost_per_side_bp": 0.5,
    "execution_contract": {"price_increment": "0.01", "quantity_increment": "0.001"},
}


def dca_market(price: float = 4010.0) -> dict:
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


def dca_payload() -> dict:
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


def grid_bar(index: int, close: float, previous: float) -> Bar:
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


def grid_bars(closes: list[float]) -> list[Bar]:
    rows: list[Bar] = []
    previous = closes[0]
    for index, close in enumerate(closes):
        rows.append(grid_bar(index, close, previous))
        previous = close
    return rows


GRID_CONFIG = {
    "capital_per_track_usd": 10000,
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


def grid_market(close: float = 110.0) -> dict:
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


def grid_preview_summary(preview: dict) -> dict:
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


def main() -> None:
    preview = build_dca_preview(
        "golden-dca",
        dca_payload(),
        market=dca_market(),
        account={"equity": 10000},
        config=DCA_CONFIG,
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
    explicit = simulate_explicit_grid(
        cycle_id="golden-grid",
        bars=grid_bars([4000.0, 4010.0, 4000.0, 4010.0]),
        direction=1,
        orders=[{"entry": 4000.0, "take_profit": 4010.0, "weight": 1.0}],
        stop=GridStop(side="below", price=3990.0),
        rung_notional=1000.0,
        cost_per_side_bp=0.5,
        finalize=False,
    )
    hard_stop = simulate_explicit_grid(
        cycle_id="golden-grid-stop",
        bars=grid_bars([4000.0, 3990.0, 3980.0]),
        direction=1,
        orders=[{"entry": 4000.0, "take_profit": 4010.0, "weight": 1.0}],
        stop=GridStop(side="below", price=3990.0),
        rung_notional=1000.0,
        cost_per_side_bp=0.5,
        finalize=False,
    )
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
        bars=grid_bars([4000.0, 3940.0]),
        prev_range=100.0,
        re_arm_max=1,
        stop=GridStop(side="below", price=3950.0),
        **common,
    )
    synthetic_rearm = simulate_conditional_grid(
        cycle_id="golden-conditional-rearm",
        bars=grid_bars([4000.0, 3920.0, 4000.0]),
        prev_range=80.0,
        re_arm_max=1,
        stop=None,
        **common,
    )
    terminal_flatten = simulate_conditional_grid(
        cycle_id="golden-conditional-flatten",
        bars=grid_bars([4000.0, 3990.0]),
        prev_range=80.0,
        re_arm_max=0,
        stop=None,
        **common,
    )
    grid_preview = build_grid_preview(
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
        market=grid_market(),
        account={"equity": 10000},
        config=GRID_CONFIG,
        allow_unsafe_manual_preview=True,
    )
    output = {
        "dca": {
            "preview": preview,
            "plan": plan,
            "commands": commands,
            "target_replay": replay_dca_marks(
                preview,
                [4004.0, 3996.0, 4050.0, 3970.0],
            ),
            "stop_replay": replay_dca_marks(preview, [4004.0, 3970.0]),
        },
        "grid": {
            "preview": grid_preview_summary(grid_preview),
            "explicit_rearm": asdict(explicit),
            "hard_stop": asdict(hard_stop),
            "line_after_partial_close_cancel": line.snapshot(),
        },
        "conditional_grid": {
            "same_bar_hard_stop": asdict(same_bar),
            "synthetic_range_rearm": asdict(synthetic_rearm),
            "terminal_flatten": asdict(terminal_flatten),
        },
        "source_baseline_sha": SOURCE_BASELINE_SHA,
    }
    print(json.dumps(output, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
