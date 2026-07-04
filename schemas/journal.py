from __future__ import annotations

from dataclasses import dataclass, field

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
    order_type: str = ""
    entry_order_limit_price: float | None = None
    entry_order_ttl_bars: int = 0
    entry_order_timeframe: str = ""
    entry_order_created_bar_timestamp: str = ""
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_ticket(cls, ticket: TradeTicket, created_at: str) -> "JournalPending":
        decision_status = "pending_entry_order" if ticket.order_type == "limit" else "pending_manual_decision"
        return cls(
            journal_id=f"journal_{ticket.ticket_id}",
            ticket_id=ticket.ticket_id,
            signal_id=ticket.signal_id,
            asset=ticket.asset,
            decision_status=decision_status,
            created_at=created_at,
            required_user_action="None; waiting for limit entry or expiry." if ticket.order_type == "limit" else "Mark as executed, skipped, or rejected after review.",
            order_type=ticket.order_type,
            entry_order_limit_price=ticket.entry_order_limit_price,
            entry_order_ttl_bars=ticket.entry_order_ttl_bars,
            entry_order_timeframe=ticket.entry_order_timeframe,
            entry_order_created_bar_timestamp=ticket.entry_order_created_bar_timestamp,
        )

    @classmethod
    def from_dict(cls, row: dict) -> "JournalPending":
        known = {
            "journal_id",
            "ticket_id",
            "signal_id",
            "asset",
            "decision_status",
            "created_at",
            "required_user_action",
            "notes",
            "order_type",
            "entry_order_limit_price",
            "entry_order_ttl_bars",
            "entry_order_timeframe",
            "entry_order_created_bar_timestamp",
        }
        return cls(
            journal_id=str(row.get("journal_id") or ""),
            ticket_id=str(row.get("ticket_id") or ""),
            signal_id=str(row.get("signal_id") or ""),
            asset=str(row.get("asset") or ""),
            decision_status=str(row.get("decision_status") or "pending_manual_decision"),
            created_at=str(row.get("created_at") or ""),
            required_user_action=str(row.get("required_user_action") or ""),
            notes=str(row.get("notes") or ""),
            order_type=str(row.get("order_type") or ""),
            entry_order_limit_price=_float_or_none(row.get("entry_order_limit_price")),
            entry_order_ttl_bars=int(row.get("entry_order_ttl_bars", 0) or 0),
            entry_order_timeframe=str(row.get("entry_order_timeframe") or ""),
            entry_order_created_bar_timestamp=str(row.get("entry_order_created_bar_timestamp") or ""),
            extra={key: value for key, value in row.items() if key not in known},
        )

    def to_dict(self) -> dict:
        return {
            **self.extra,
            "journal_id": self.journal_id,
            "ticket_id": self.ticket_id,
            "signal_id": self.signal_id,
            "asset": self.asset,
            "decision_status": self.decision_status,
            "created_at": self.created_at,
            "required_user_action": self.required_user_action,
            "notes": self.notes,
            "order_type": self.order_type,
            "entry_order_limit_price": self.entry_order_limit_price,
            "entry_order_ttl_bars": self.entry_order_ttl_bars,
            "entry_order_timeframe": self.entry_order_timeframe,
            "entry_order_created_bar_timestamp": self.entry_order_created_bar_timestamp,
        }


def _float_or_none(value) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
