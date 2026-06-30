from pathlib import Path

import pytest

from services.journal_store import load_json
from services.order_lifecycle import IllegalOrderTransition, OrderLifecycleStore


def test_order_lifecycle_rejects_illegal_transition(tmp_path: Path):
    root = tmp_path / "outputs"
    store = OrderLifecycleStore(root)
    store.write_intent(
        "2026-06-30",
        order_id="order_1",
        ticket_id="ticket_1",
        idempotency_key="stable_1",
        requested_quantity=1,
        requested_price=4500,
        source="test",
    )

    with pytest.raises(IllegalOrderTransition, match="entry -> filled"):
        store.transition("2026-06-30", "order_1", "filled", reason="skip_required_states")


def test_order_lifecycle_watchdog_blocks_stuck_accepted_order(tmp_path: Path):
    root = tmp_path / "outputs"
    store = OrderLifecycleStore(root)
    store.write_intent(
        "2026-06-30",
        order_id="order_stuck",
        ticket_id="ticket_stuck",
        idempotency_key="stable_stuck",
        requested_quantity=1,
        requested_price=4500,
        source="test",
    )
    store.transition("2026-06-30", "order_stuck", "submitting", reason="submit_started")
    store.transition("2026-06-30", "order_stuck", "accepted", reason="venue_accepted")

    assert store.watchdog_tick("2026-06-30", max_cycles=2) == []
    assert store.watchdog_tick("2026-06-30", max_cycles=2) == []
    blockers = store.watchdog_tick("2026-06-30", max_cycles=2)

    assert blockers[0]["source"] == "order_lifecycle_watchdog"
    assert "accepted" in blockers[0]["reason"]
    current = store.current("2026-06-30", "order_stuck")
    assert current["blocked"] is True
    paper_blocks = load_json(root / "paper_execution_blocks" / "2026-06-30.json")
    assert paper_blocks[0]["action"] == "halt_new_orders_until_order_state_reconciled"
