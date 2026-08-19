from __future__ import annotations

from services.park_conversation_contract import extract_explicit_strategy_patch
from services.park_telegram_runtime import ParkTelegramRouter


def test_strategy_conversation_retention_covers_one_recording_window(tmp_path) -> None:
    router = ParkTelegramRouter(tmp_path / "outputs", park_user_id="park", chat_id="chat")

    assert router.conversation_ledger.ttl_seconds >= 12 * 60 * 60


def test_repeated_explicit_dca_entry_text_is_deduplicated() -> None:
    patch = extract_explicit_strategy_patch(
        "DCA，从4370~4420开两单；4370开一单，4420开一单；"
        "DCA，从4370~4420开两单；4370开一单，4420开一单"
    )

    assert patch["entry_prices"] == [4370.0, 4420.0]
    assert patch["order_count"] == 2

