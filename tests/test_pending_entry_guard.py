from pathlib import Path

from schemas.market_data import Bar
from services.journal_store import write_json
from services.order_lifecycle import OrderLifecycleStore
from services.pending_entry_guard import build_pending_entry_block, evaluate_limit_entry_status, evaluate_pending_entry_state


RUN_DATE = "2026-07-03"


def _bars(count: int = 3) -> list[Bar]:
    return [
        Bar(
            "GOLD",
            "1m",
            f"2026-07-03T00:{minute:02d}:00+00:00",
            100.0,
            100.2,
            99.9,
            100.0,
            1000,
            "mock",
        )
        for minute in range(count)
    ]


def _ticket(ticket_id: str = "ticket_pending") -> dict:
    return {
        "ticket_id": ticket_id,
        "signal_id": f"sig_{ticket_id}",
        "asset": "GOLD",
        "asset_class": "commodity",
        "action": "prepare_buy",
        "entry_zone": "99.80-99.80",
        "entry_order_limit_price": 99.8,
        "entry_order_ttl_bars": 10,
        "entry_order_timeframe": "1m",
        "entry_order_created_bar_timestamp": "2026-07-03T00:00:00+00:00",
        "stop_loss": 99.6,
        "targets": [100.6],
        "position_size_pct": 50,
        "max_loss_pct": 2.0,
        "order_type": "limit",
        "time_in_force": "gtc",
    }


def _bar(minute: int, *, high: float = 100.2, low: float = 99.9, close: float = 100.0) -> Bar:
    return Bar(
        "GOLD",
        "1m",
        f"2026-07-03T00:{minute:02d}:00+00:00",
        close,
        high,
        low,
        close,
        1000,
        "mock",
    )


def _seed_pending(root: Path, ticket: dict | None = None) -> dict:
    ticket = ticket or _ticket()
    write_json(root / "trade_tickets" / f"{RUN_DATE}.json", [ticket])
    write_json(
        root / "journal_pending" / f"{RUN_DATE}.json",
        [
            {
                "journal_id": f"journal_{ticket['ticket_id']}",
                "ticket_id": ticket["ticket_id"],
                "signal_id": ticket["signal_id"],
                "asset": "GOLD",
                "decision_status": "pending_entry_order",
                "created_at": ticket["entry_order_created_bar_timestamp"],
                "required_user_action": "None; waiting for limit entry or expiry.",
                "entry_order_limit_price": ticket["entry_order_limit_price"],
                "entry_order_ttl_bars": ticket["entry_order_ttl_bars"],
                "entry_order_timeframe": ticket["entry_order_timeframe"],
                "entry_order_created_bar_timestamp": ticket["entry_order_created_bar_timestamp"],
                "order_type": "limit",
            }
        ],
    )
    return ticket


def test_active_journal_pending_blocks_new_signal(tmp_path: Path):
    root = tmp_path / "outputs"
    ticket = _seed_pending(root)

    state = evaluate_pending_entry_state(root, RUN_DATE, _bars(), "GOLD")
    block = build_pending_entry_block("GOLD", "sig_new", state)

    assert state["has_active"] is True
    assert state["active_pending"][0]["ticket_id"] == ticket["ticket_id"]
    assert block["reason"] == "pending_entry_exists"
    assert block["pending_entry"]["ticket_id"] == ticket["ticket_id"]


def test_signal_close_bar_touch_does_not_fill_new_limit_order():
    ticket = _ticket()
    bars = [
        _bar(0, low=99.7),  # signal bar closed before the limit was placed
        _bar(1, low=99.9),
    ]

    status = evaluate_limit_entry_status(ticket, bars)

    assert status["status"] == "waiting"


def test_missed_middle_bar_touch_still_fills_before_latest_bar():
    ticket = _ticket()
    bars = [
        _bar(0, low=99.9),
        _bar(1, low=99.79),
        _bar(2, low=99.9),
    ]

    status = evaluate_limit_entry_status(ticket, bars)

    assert status["status"] == "fillable"
    assert status["actual_entry"] == 99.8
    assert status["touched_bar_timestamp"] == "2026-07-03T00:01:00+00:00"


def test_limit_touch_after_ttl_is_expired_not_fillable():
    ticket = {**_ticket(), "entry_order_ttl_bars": 2}
    bars = [
        _bar(0, low=99.9),
        _bar(1, low=99.9),
        _bar(2, low=99.9),
        _bar(3, low=99.79),
    ]

    status = evaluate_limit_entry_status(ticket, bars)

    assert status["status"] == "expired"


def test_expired_journal_pending_does_not_block_new_signal(tmp_path: Path):
    root = tmp_path / "outputs"
    _seed_pending(root)

    state = evaluate_pending_entry_state(root, RUN_DATE, _bars(11), "GOLD")

    assert state["has_active"] is False
    assert state["active_pending"] == []
    assert state["expired_pending"][0]["ticket_id"] == "ticket_pending"


def test_active_order_lifecycle_blocks_new_signal_after_journal_pending_is_gone(tmp_path: Path):
    root = tmp_path / "outputs"
    ticket = _ticket("ticket_submitted_limit")
    write_json(root / "trade_tickets" / f"{RUN_DATE}.json", [ticket])
    store = OrderLifecycleStore(root)
    store.write_intent(
        RUN_DATE,
        order_id="demo_order_limit",
        ticket_id=ticket["ticket_id"],
        idempotency_key="demo_order_limit",
        requested_quantity=1.0,
        requested_price=99.8,
        source="binance_usdm:demo",
        metadata={"ticket": ticket, "symbol": "XAUUSDT"},
    )
    store.transition(RUN_DATE, "demo_order_limit", "submitting", reason="submit_started")
    store.transition(RUN_DATE, "demo_order_limit", "accepted", reason="entry_accepted")

    state = evaluate_pending_entry_state(root, RUN_DATE, _bars(), "GOLD")
    block = build_pending_entry_block("GOLD", "sig_new", state)

    assert state["has_active"] is True
    assert state["active_orders"][0]["ticket_id"] == ticket["ticket_id"]
    assert block["reason"] == "pending_entry_exists"
    assert block["pending_entry"]["source"] == "order_lifecycle"
