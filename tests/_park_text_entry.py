"""Test harness: create Park Paper proposals from text, as Telegram once did.

Since 2026-09-08 (20db303) production Telegram refuses strategy text before
any parser or ledger write; proposals now come from the Dashboard AI copilot,
which writes the same ``ParkConfirmationLedger`` the Paper runtime reads.

The runtime, parser and cutover tests below still need a short way to put a
proposal and a confirmation into that ledger. This subclass re-opens the text
entry **only inside tests**. The production guard itself stays covered by
``tests/test_park_telegram_runtime.py`` (``entry_disabled`` cases).
"""

from __future__ import annotations

from services.park_telegram_runtime import ParkTelegramRouter


class TextEntryRouter(ParkTelegramRouter):
    @classmethod
    def _telegram_entry_disabled(cls, text: str) -> bool:  # noqa: ARG003
        return False
