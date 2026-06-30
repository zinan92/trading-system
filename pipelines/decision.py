from __future__ import annotations

import argparse

from services.journal_store import JournalStore, VALID_DECISIONS


def main() -> None:
    parser = argparse.ArgumentParser(description="Record a manual trading decision.")
    parser.add_argument("--date", required=True, help="Pipeline run date in YYYY-MM-DD format.")
    parser.add_argument("--ticket-id", required=True, help="Ticket id from outputs/trade_tickets.")
    parser.add_argument("--decision", required=True, choices=sorted(VALID_DECISIONS))
    parser.add_argument("--notes", default="", help="Manual notes for the decision. Use executed_paper to create a local paper order.")
    parser.add_argument("--actual-entry", type=float, default=None, help="Actual fill/entry price if executed.")
    parser.add_argument("--actual-size", type=float, default=None, help="Actual position size if executed.")
    args = parser.parse_args()

    record = JournalStore().record_decision(
        run_date=args.date,
        ticket_id=args.ticket_id,
        decision=args.decision,
        notes=args.notes,
        actual_entry=args.actual_entry,
        actual_size=args.actual_size,
    )
    print("Journal decision recorded.")
    print(f"ticket_id: {record['ticket_id']}")
    print(f"decision: {record['decision_status']}")
    print(f"asset: {record['asset']}")


if __name__ == "__main__":
    main()
