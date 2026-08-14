from __future__ import annotations

from pathlib import Path

import pytest

from services.park_telegram_control import ParkTelegramControlError, ParkTelegramLedger, strategy_binding


def _update(update_id: int = 1, *, sender: str = "park-user", chat: str = "park-chat", text: str = "做空 DCA") -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id + 100,
            "from": {"id": sender},
            "chat": {"id": chat},
            "text": text,
        },
    }


def _ledger(tmp_path: Path, attempts: int = 3) -> ParkTelegramLedger:
    return ParkTelegramLedger(tmp_path / "outputs", park_user_id="park-user", chat_id="park-chat", max_send_attempts=attempts)


def test_only_park_and_one_chat_are_accepted(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    with pytest.raises(ParkTelegramControlError, match="not Park") as user_error:
        ledger.ingest_update(_update(sender="other"))
    assert user_error.value.code == "unauthorized_user"
    with pytest.raises(ParkTelegramControlError, match="configured Park chat"):
        ledger.ingest_update(_update(chat="other-chat"))


def test_inbox_is_durable_and_update_idempotent(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    first = ledger.ingest_update(_update(), received_at=1.0)
    second = ledger.ingest_update(_update(), received_at=2.0)
    assert first == second
    assert len(ledger.inbox_rows()) == 1
    assert first["execution_authorized"] is False
    assert first["text_digest"].startswith("sha256:")


def test_strategy_binding_requires_session_and_revision_not_record_window_only() -> None:
    with pytest.raises(ParkTelegramControlError, match="requires"):
        strategy_binding({"record_window_id": "2026-08-14_DAY"})
    assert strategy_binding({"strategy_session_id": "s1", "strategy_revision_id": "r1", "record_window_id": "old"}) == {
        "strategy_session_id": "s1",
        "strategy_revision_id": "r1",
    }


def test_outbox_persists_before_send_and_is_idempotent(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    first = ledger.queue_outbound(idempotency_key="plan-1", message_type="plan", text="plan", binding={"strategy_session_id": "s1", "strategy_revision_id": "r1"})
    second = ledger.queue_outbound(idempotency_key="plan-1", message_type="plan", text="different", binding={"strategy_session_id": "s1", "strategy_revision_id": "r1"})
    assert first == second
    assert first["status"] == "queued"
    assert ledger.pending_outbound() == [first]


def test_explicit_transport_receipt_is_required_for_delivery(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    queued = ledger.queue_outbound(idempotency_key="m1", message_type="notice", text="hello")
    failed = ledger.record_send_result(message_id=queued["message_id"], transport_result={"ok": True})
    assert failed["status"] == "failed"
    assert failed["next_action"] == "retry_send"
    delivered = ledger.record_send_result(message_id=queued["message_id"], transport_result={"ok": True, "message_id": "tg-1"})
    assert delivered["status"] == "delivered"
    assert delivered["transport_message_id"] == "tg-1"
    assert ledger.record_send_result(message_id=queued["message_id"], transport_result={"ok": False}) == delivered


def test_failures_retry_then_dead_letter_with_next_action(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path, attempts=2)
    queued = ledger.queue_outbound(idempotency_key="m1", message_type="notice", text="hello")
    retry = ledger.record_send_result(message_id=queued["message_id"], transport_result={"ok": False, "reason": "timeout"})
    dead = ledger.record_send_result(message_id=queued["message_id"], transport_result={"ok": False, "reason": "timeout"})
    assert retry["status"] == "failed"
    assert dead["status"] == "dead_letter"
    assert dead["next_action"] == "notify_park_and_wait"
    assert ledger.pending_outbound() == []


def test_stale_binding_is_rejected(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    with pytest.raises(ParkTelegramControlError, match="stale") as error:
        ledger.assert_current_binding(
            {"strategy_session_id": "s-old", "strategy_revision_id": "r-old"},
            {"strategy_session_id": "s-new", "strategy_revision_id": "r-new"},
        )
    assert error.value.code == "stale_binding"
