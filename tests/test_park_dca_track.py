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


def test_dca_entries_are_finite_owned_and_idempotent(tmp_path: Path) -> None:
    dca = _dca(tmp_path)
    first = dca.entry_commands()
    second = dca.entry_commands()
    assert first == second
    assert len(first) == 3
    assert all(row["strategy_session_id"] == "session-1" and row["strategy_revision_id"] == "revision-1" for row in first)
    assert all(row["loop_enabled"] is False for row in first)


def test_both_authorized_boundaries_terminal_and_notify_once(tmp_path: Path) -> None:
    upper = _dca(tmp_path / "upper")
    first = upper.on_market(price=4444, trusted=True, fresh=True)
    second = upper.on_market(price=4500, trusted=True, fresh=True)
    assert first["status"] == "terminal"
    assert first["action_plan"]["boundary"] == "upper"
    assert first["notification"] == second["notification"]
    assert len(upper.telegram.outbox_rows()) == 1

    lower = _dca(tmp_path / "lower")
    result = lower.on_market(price=4199, trusted=True, fresh=True)
    assert result["action_plan"]["boundary"] == "lower"


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
