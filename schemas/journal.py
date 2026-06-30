from __future__ import annotations

from dataclasses import dataclass

from .trade_ticket import TradeTicket


@dataclass(frozen=True)
class JournalPending:
    journal_id: str
    ticket_id: str
    signal_id: str
    asset: str
    decision_status: str
    created_at: str
    required_user_action: str
    notes: str = ""

    @classmethod
    def from_ticket(cls, ticket: TradeTicket, created_at: str) -> "JournalPending":
        return cls(
            journal_id=f"journal_{ticket.ticket_id}",
            ticket_id=ticket.ticket_id,
            signal_id=ticket.signal_id,
            asset=ticket.asset,
            decision_status="pending_manual_decision",
            created_at=created_at,
            required_user_action="Mark as executed, skipped, or rejected after review.",
        )

    def to_dict(self) -> dict:
        return {
            "journal_id": self.journal_id,
            "ticket_id": self.ticket_id,
            "signal_id": self.signal_id,
            "asset": self.asset,
            "decision_status": self.decision_status,
            "created_at": self.created_at,
            "required_user_action": self.required_user_action,
            "notes": self.notes,
        }
