from __future__ import annotations

from pathlib import Path

import pytest

from services.park_grid_track import ParkGridLifecycle, ParkGridLifecycleError, _risk_digest


def _plan() -> dict:
    return {
        "plan_digest": "sha256:" + "a" * 64,
        "normalized_input": {
            "strategy_session_id": "session-1",
            "strategy_revision_id": "revision-1",
            "strategy_type": "grid",
            "direction": "long",
            "upper_price_boundary": 4444.0,
            "lower_price_boundary": 4200.0,
            "order_count": 4,
        },
        "risk": {"order_count": 4, "per_order_quantity": 0.1},
    }


def _receipt() -> dict:
    return {
        "execution_authorized": True,
        "start_or_order_submitted": False,
        "plan_digest": "sha256:" + "a" * 64,
        "strategy_session_id": "session-1",
        "strategy_revision_id": "revision-1",
    }


def _grid(tmp_path: Path) -> ParkGridLifecycle:
    return ParkGridLifecycle(_plan(), confirmation_receipt=_receipt(), output_root=tmp_path / "outputs", park_user_id="park", chat_id="chat")


def _neutral_plan() -> dict:
    return {
        "plan_digest": "sha256:" + "a" * 64,
        "market": {"price": 4300.0},
        "normalized_input": {
            "strategy_session_id": "session-1",
            "strategy_revision_id": "revision-1",
            "strategy_type": "grid",
            "direction": "neutral",
            "upper_price_boundary": 4450.0,
            "lower_price_boundary": 4100.0,
            "order_count": 30,
        },
        "risk": {"order_count": 30, "per_order_quantity": 0.1},
    }


def _neutral_grid(tmp_path: Path) -> ParkGridLifecycle:
    return ParkGridLifecycle(
        _neutral_plan(),
        confirmation_receipt=_receipt(),
        output_root=tmp_path / "outputs",
        park_user_id="park",
        chat_id="chat",
    )


def test_levels_are_deterministic_inside_range_and_owned(tmp_path: Path) -> None:
    grid = _grid(tmp_path)
    first = grid.levels()
    second = grid.observe(price=4300, trusted=True, fresh=True)["levels"]
    assert first == second
    assert len(first) == 4
    assert all(4200 < row["price"] < 4444 for row in first)
    assert all(row["geometry_locked"] is True for row in first)
    assert all(row["strategy_session_id"] == "session-1" for row in first)


def test_in_range_observation_has_no_reassessment_or_direction_change(tmp_path: Path) -> None:
    grid = _grid(tmp_path)
    result = grid.observe(price=4300, trusted=True, fresh=True)
    assert result["status"] == "active"
    assert result["reassessment"] == "none"
    proposal = grid.propose_geometry_change(direction="short", upper=4500, lower=4100)
    assert proposal["status"] == "proposal_only"
    assert proposal["current_strategy_unchanged"] is True
    assert proposal["requires_clean_slate"] is True


def test_neutral_grid_levels_are_explicitly_bilateral_and_protected(tmp_path: Path) -> None:
    grid = _neutral_grid(tmp_path)
    levels = grid.levels()
    assert len(levels) == 30
    assert {row["side"] for row in levels} == {"buy", "sell"}
    assert all(4100 < row["price"] < 4450 for row in levels)
    assert all(row["sl"] == (4100.0 if row["side"] == "buy" else 4450.0) for row in levels)
    assert all(
        row["tp"] > row["price"] if row["side"] == "buy" else row["tp"] < row["price"]
        for row in levels
    )


def test_neutral_boundary_preserves_positions_in_action_plan(tmp_path: Path) -> None:
    grid = _neutral_grid(tmp_path)
    result = grid.observe(price=4450, trusted=True, fresh=True)
    assert result["action_plan"]["position_authority"] == "preserve_strategy_owned_positions"
    assert "preserve_strategy_owned_positions" in result["action_plan"]["ordered_actions"]


def test_both_boundaries_terminal_and_notification_idempotent(tmp_path: Path) -> None:
    upper = _grid(tmp_path / "upper")
    first = upper.observe(price=4444, trusted=True, fresh=True)
    second = upper.observe(price=4500, trusted=True, fresh=True)
    assert first["status"] == "terminal"
    assert first["action_plan"]["boundary"] == "upper"
    assert first["notification"] == second["notification"]
    assert len(upper.telegram.outbox_rows()) == 1

    lower = _grid(tmp_path / "lower")
    assert lower.observe(price=4199, trusted=True, fresh=True)["action_plan"]["boundary"] == "lower"


def test_stale_market_cannot_reassess_or_close(tmp_path: Path) -> None:
    grid = _grid(tmp_path)
    with pytest.raises(ParkGridLifecycleError, match="trusted fresh"):
        grid.observe(price=4444, trusted=False, fresh=True)
    with pytest.raises(ParkGridLifecycleError, match="confirmation"):
        ParkGridLifecycle(_plan(), confirmation_receipt={}, output_root=tmp_path / "bad", park_user_id="park", chat_id="chat")


def test_authoritative_grid_geometry_omits_local_stops_by_default_and_checks_risk_digest(tmp_path: Path) -> None:
    plan = _plan()
    plan["risk"].update(
        {
            "grid_spacing": 10.0,
            "grid_entry_range": {"lower": 4210.0, "upper": 4430.0},
            "grid_rungs": [
                {"rung": 1, "price": 4210.0, "side": "buy", "take_profit": 4220.0, "hard_stop": 4200.0},
                {"rung": 2, "price": 4220.0, "side": "buy", "take_profit": 4230.0, "hard_stop": 4200.0},
            ],
            "local_stop_authorized": False,
        }
    )
    receipt = _receipt()
    receipt["risk_digest"] = _risk_digest(plan["risk"])
    grid = ParkGridLifecycle(plan, confirmation_receipt=receipt, output_root=tmp_path / "outputs", park_user_id="park", chat_id="chat")
    levels = grid.levels()
    assert all("sl" not in row for row in levels)
    assert [row["tp"] for row in levels] == [4220.0, 4230.0]

    bad_receipt = {**receipt, "risk_digest": "sha256:" + "b" * 64}
    with pytest.raises(ParkGridLifecycleError, match="exact Park plan"):
        ParkGridLifecycle(plan, confirmation_receipt=bad_receipt, output_root=tmp_path / "bad", park_user_id="park", chat_id="chat")
