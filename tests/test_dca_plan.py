from __future__ import annotations

from copy import deepcopy

import pytest

from services.dca_plan import (
    DETERMINISTIC_DCA_CANDIDATE_VERSION,
    build_dca_preview,
    build_dca_strategy_plan,
    build_deterministic_dca_candidate_payload_v1,
    replay_dca_marks,
)
from services.dualtrack_config import DEFAULT_DUALTRACK_CONFIG


def _market(price: float = 4_010.0) -> dict:
    bars = [
        {
            "timestamp": f"2026-07-22T00:{index:02d}:00+00:00",
            "open": price,
            "high": price + 1,
            "low": price - 1,
            "close": price,
        }
        for index in range(20)
    ]
    return {
        "status": "ready",
        "fresh": True,
        "is_synthetic": False,
        "provider": "binance_usdm_futures",
        "symbol": "GOLD",
        "timeframe": "1m",
        "latest_close": price,
        "latest_timestamp": "2026-07-22T00:19:00+00:00",
        "bars": bars,
    }


def _config() -> dict:
    config = deepcopy(DEFAULT_DUALTRACK_CONFIG)
    config["execution_contract"] = {
        **config["execution_contract"],
        "price_increment": "0.01",
        "quantity_increment": "0.001",
    }
    return config


def _payload(direction: str = "long") -> dict:
    if direction == "long":
        levels, target, stop = [4_004.0, 3_996.0, 3_988.0], 4_050.0, 3_970.0
    else:
        levels, target, stop = [4_016.0, 4_024.0, 4_032.0], 3_970.0, 4_050.0
    return {
        "direction": direction,
        "dca": {
            "entry_levels": levels,
            "target_price": target,
            "stop_price": stop,
            "notional_per_addition": 2_000.0,
            "max_additions": 3,
            "loop_enabled": False,
        },
        "risk_budget": {"leverage": 10},
    }


@pytest.mark.parametrize(
    ("direction", "levels", "target", "stop"),
    [
        (
            "long",
            [3994.0, 3979.2, 3964.4, 3949.6, 3934.8, 3920.0],
            4040.0,
            3880.0,
        ),
        (
            "short",
            [4006.0, 4020.8, 4035.6, 4050.4, 4065.2, 4080.0],
            3960.0,
            4120.0,
        ),
    ],
)
def test_deterministic_candidate_matches_dashboard_smart_fill_contract(
    direction: str,
    levels: list[float],
    target: float,
    stop: float,
) -> None:
    candidate = build_deterministic_dca_candidate_payload_v1(
        direction=direction,
        market_price=4_000.0,
    )

    assert (
        candidate["candidate_builder_version"]
        == DETERMINISTIC_DCA_CANDIDATE_VERSION
    )
    assert candidate["dca"]["entry_levels"] == pytest.approx(levels)
    assert candidate["dca"]["target_price"] == target
    assert candidate["dca"]["stop_price"] == stop
    assert candidate["dca"]["notional_per_addition"] == 2_000.0
    assert candidate["dca"]["max_additions"] == 6
    assert candidate["risk_budget"]["leverage"] == 10.0


def test_deterministic_candidate_rejects_neutral_direction() -> None:
    with pytest.raises(
        ValueError,
        match="requires long or short",
    ):
        build_deterministic_dca_candidate_payload_v1(
            direction="neutral",
            market_price=4_000.0,
        )


def test_deterministic_candidate_matches_browser_half_cent_boundary() -> None:
    candidate = build_deterministic_dca_candidate_payload_v1(
        direction="long",
        market_price=3_906.25,
    )

    assert candidate["dca"]["entry_levels"] == pytest.approx(
        [
            3900.39,
            3885.938,
            3871.486,
            3857.034,
            3842.582,
            3828.13,
        ]
    )
    assert candidate["dca"]["target_price"] == 3945.31
    assert candidate["dca"]["stop_price"] == 3789.06


def test_deterministic_candidate_matches_opposite_float_boundary() -> None:
    candidate = build_deterministic_dca_candidate_payload_v1(
        direction="long",
        market_price=3_900.25,
    )

    assert candidate["dca"]["entry_levels"] == pytest.approx(
        [
            3894.4,
            3879.968,
            3865.536,
            3851.104,
            3836.672,
            3822.24,
        ]
    )


def test_long_dca_preview_resizes_one_fixed_target_at_every_fill_depth() -> None:
    preview = build_dca_preview(
        "2026-07-22_NIGHT",
        _payload(),
        market=_market(),
        account={"equity": 10_000},
        config=_config(),
    )

    assert preview["schema_version"] == "strategy-dca-preview-v1"
    assert preview["strategy_type"] == "dca"
    assert [row["price"] for row in preview["entries"]] == [4_004.0, 3_996.0, 3_988.0]
    assert preview["aggregate_take_profit"] == {
        "side": "sell",
        "event": "target",
        "order_type": "limit",
        "reduce_only": True,
        "price": 4_050.0,
        "quantity_source": "reconciled_open_dca_round_quantity",
        "replace_after_each_entry_fill": True,
        "one_active_order_required": True,
    }
    depths = preview["depth_economics"]
    assert len(depths) == 3
    assert depths[1]["aggregate_target_quantity"] == pytest.approx(
        preview["entries"][0]["quantity"] + preview["entries"][1]["quantity"]
    )
    assert depths[1]["weighted_average_entry"] < depths[0]["weighted_average_entry"]
    assert depths[2]["target_net_pnl_usd"] > depths[1]["target_net_pnl_usd"] > 0
    assert preview["risk"]["capacity_exceeded"] is False
    assert preview["risk"]["maximum_loss_at_full_depth"] > 0


def test_dca_plan_projection_is_versioned_and_does_not_mutate_preview() -> None:
    preview = build_dca_preview(
        "2026-07-22_NIGHT",
        _payload(),
        market=_market(),
        account={"equity": 10_000},
        config=_config(),
    )
    original = deepcopy(preview)

    plan = build_dca_strategy_plan(
        preview,
        strategy_plan_id="strategy-plan-dca-1",
        version=1,
        locked_at="2026-07-22T16:00:00+00:00",
    )

    assert plan["schema_version"] == "strategy-plan-v1"
    assert plan["strategy_type"] == "dca"
    assert plan["dca"]["aggregate_take_profit"]["quantity_source"] == (
        "reconciled_open_dca_round_quantity"
    )
    assert "grid" not in plan
    assert preview == original


@pytest.mark.parametrize(
    ("direction", "marks", "expected_prices", "target_side"),
    [
        ("long", [4_010.0, 4_004.0, 3_996.0, 4_050.0], [4_004.0, 3_996.0], "sell"),
        ("short", [4_010.0, 4_016.0, 4_024.0, 3_970.0], [4_016.0, 4_024.0], "buy"),
    ],
)
def test_dca_replay_accumulates_two_entries_then_closes_the_full_quantity(
    direction: str,
    marks: list[float],
    expected_prices: list[float],
    target_side: str,
) -> None:
    preview = build_dca_preview(
        "2026-07-22_NIGHT",
        _payload(direction),
        market=_market(),
        account={"equity": 10_000},
        config=_config(),
    )

    replay = replay_dca_marks(preview, marks)
    entries = [row for row in replay["events"] if row["event"] == "entry"]
    target = [row for row in replay["events"] if row["event"] == "target"]

    assert [row["price"] for row in entries] == expected_prices
    assert replay["status"] == "target_closed"
    assert replay["open_quantity"] == 0
    assert len(target) == 1
    assert target[0]["quantity"] == entries[-1]["aggregate_quantity"]
    assert preview["aggregate_take_profit"]["side"] == target_side


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (lambda body: body.update(direction="neutral"), "direction must be long or short"),
        (
            lambda body: body["dca"].update(
                entry_levels=[4_004.0, 4_006.0], max_additions=2
            ),
            "long DCA entry levels must be strictly descending",
        ),
        (
            lambda body: body["dca"].update(target_price=4_000.0),
            "long DCA target must be above every entry",
        ),
        (
            lambda body: body["dca"].update(stop_price=4_000.0),
            "long DCA stop must be below every entry",
        ),
        (
            lambda body: body["dca"].update(
                entry_levels=[4_004.001, 4_004.002], max_additions=2
            ),
            "collapse to duplicates",
        ),
        (
            lambda body: body["dca"].update(loop_enabled=True),
            "loop_enabled=true is not supported in v1",
        ),
    ],
)
def test_dca_preview_rejects_ambiguous_or_unsafe_geometry(mutator, message: str) -> None:
    body = _payload()
    mutator(body)
    with pytest.raises(ValueError, match=message):
        build_dca_preview(
            "2026-07-22_NIGHT",
            body,
            market=_market(),
            account={"equity": 10_000},
            config=_config(),
        )


def test_dca_preview_is_deterministic_and_has_no_external_side_effects(tmp_path) -> None:
    before = list(tmp_path.iterdir())
    first = build_dca_preview(
        "2026-07-22_NIGHT",
        _payload(),
        market=_market(),
        account={"equity": 10_000},
        config=_config(),
    )
    second = build_dca_preview(
        "2026-07-22_NIGHT",
        _payload(),
        market=_market(),
        account={"equity": 10_000},
        config=_config(),
    )

    assert first == second
    assert first["preview_id"].startswith("dca-preview-")
    assert list(tmp_path.iterdir()) == before
