from __future__ import annotations

from pathlib import Path

import pytest

from services.park_dca_track import ParkDcaLifecycle, ParkDcaLifecycleError


def _plan() -> dict:
    return {
        "plan_digest": "sha256:" + "a" * 64,
        "normalized_input": {
            "strategy_session_id": "session-1",
            "strategy_revision_id": "revision-1",
            "strategy_type": "dca",
            "direction": "short",
            "upper_price_boundary": 4444.0,
            "lower_price_boundary": 4200.0,
            "order_count": 3,
            "stop_price": 4450.0,
            "take_profit_price": 4210.0,
        },
        "market": {"price": 4300.0},
        "risk": {"order_count": 3, "per_order_quantity": 0.25},
    }


def _receipt() -> dict:
    return {
        "execution_authorized": True,
        "start_or_order_submitted": False,
        "plan_digest": "sha256:" + "a" * 64,
        "strategy_session_id": "session-1",
        "strategy_revision_id": "revision-1",
    }


def _dca(tmp_path: Path) -> ParkDcaLifecycle:
    return ParkDcaLifecycle(_plan(), confirmation_receipt=_receipt(), output_root=tmp_path / "outputs", park_user_id="park", chat_id="chat")


def _plan_without_exits() -> dict:
    plan = _plan()
    plan["normalized_input"] = {
        key: value
        for key, value in plan["normalized_input"].items()
        if key not in {"stop_price", "take_profit_price"}
    }
    return plan


def test_dca_entries_are_finite_owned_and_idempotent(tmp_path: Path) -> None:
    dca = _dca(tmp_path)
    first = dca.entry_commands()
    second = dca.entry_commands()
    assert first == second
    assert len(first) == 3
    assert all(row["strategy_session_id"] == "session-1" and row["strategy_revision_id"] == "revision-1" for row in first)
    assert all(row["loop_enabled"] is False for row in first)
    assert all("tp" not in row and "sl" not in row for row in first)

    with pytest.raises(ParkDcaLifecycleError, match="explicit strategy-level"):
        ParkDcaLifecycle(_plan_without_exits(), confirmation_receipt=_receipt(), output_root=tmp_path / "missing", park_user_id="park", chat_id="chat")


def test_explicit_entry_prices_are_preserved_with_per_entry_quantities(tmp_path: Path) -> None:
    plan = _plan()
    plan["normalized_input"] = {
        **plan["normalized_input"],
        "order_count": 2,
        "entry_prices": [4370.0, 4420.0],
    }
    plan["risk"] = {
        "order_count": 2,
        "per_order_quantity": 10.0,
        "per_order_quantities": [11.4416476, 11.3122172],
    }

    entries = ParkDcaLifecycle(
        plan,
        confirmation_receipt=_receipt(),
        output_root=tmp_path / "outputs",
        park_user_id="park",
        chat_id="chat",
    ).entry_commands()

    assert [row["price"] for row in entries] == [4370.0, 4420.0]
    assert [row["quantity"] for row in entries] == [11.4416476, 11.3122172]


def test_both_authorized_boundaries_terminal_and_notify_once(tmp_path: Path) -> None:
    upper = _dca(tmp_path / "upper")
    first = upper.on_market(price=4444, trusted=True, fresh=True)
    second = upper.on_market(price=4500, trusted=True, fresh=True)
    assert first["status"] == "active"
    assert second["status"] == "terminal"
    assert second["trigger"] == "stop_price"
    assert second["action_plan"]["trigger"] == "stop_price"
    assert len(upper.telegram.outbox_rows()) == 1

    lower = _dca(tmp_path / "lower")
    result = lower.on_market(price=4199, trusted=True, fresh=True)
    assert result["status"] == "terminal"
    assert result["trigger"] == "take_profit_price"


def test_stale_market_does_not_trigger_closure_and_no_reopen_api_exists(tmp_path: Path) -> None:
    dca = _dca(tmp_path)
    with pytest.raises(ParkDcaLifecycleError, match="trusted fresh"):
        dca.on_market(price=4444, trusted=False, fresh=True)
    active = dca.on_market(price=4300, trusted=True, fresh=True)
    assert active["status"] == "active"
    assert not hasattr(dca, "reopen")


def test_exact_confirmation_is_required(tmp_path: Path) -> None:
    with pytest.raises(ParkDcaLifecycleError, match="confirmation"):
        ParkDcaLifecycle(_plan(), confirmation_receipt={}, output_root=tmp_path / "outputs", park_user_id="park", chat_id="chat")


def test_explicit_dca_tp_sl_are_owned_and_terminal_once(tmp_path: Path) -> None:
    plan = _plan()
    plan["normalized_input"] = {**plan["normalized_input"], "stop_price": 4450.0, "take_profit_price": 4210.0}
    dca = ParkDcaLifecycle(
        plan,
        confirmation_receipt=_receipt(),
        output_root=tmp_path / "outputs",
        park_user_id="park",
        chat_id="chat",
    )
    assert all("sl" not in row and "tp" not in row for row in dca.entry_commands())
    terminal = dca.on_market(price=4210.0, trusted=True, fresh=True)
    assert terminal["status"] == "terminal"
    assert terminal["trigger"] == "take_profit_price"
    assert terminal["action_plan"]["automatic_reopen"] is False
    repeated = dca.on_market(price=4200.0, trusted=True, fresh=True)
    assert repeated["action_plan"] == terminal["action_plan"]
