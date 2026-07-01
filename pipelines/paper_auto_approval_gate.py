from __future__ import annotations

import argparse
from datetime import date
from services.run_date import utc_run_date

from services.paper_auto_approval_gate import PaperAutoApprovalGate


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate whether the runner may auto-approve a local paper ticket.")
    parser.add_argument("--date", default=utc_run_date())
    parser.add_argument("--auto-requested", action="store_true")
    args = parser.parse_args()
    result = PaperAutoApprovalGate().evaluate(args.date, auto_requested=args.auto_requested)
    print(
        f"paper_auto_approval_gate: {result['status']} date={args.date} "
        f"allow={result['allow_auto_approve']} pending={result['pending_count']} "
        f"ticket={result['selected_ticket_id'] or 'n/a'}"
    )
    for reason in result.get("reasons", []):
        print(f"- {reason}")


if __name__ == "__main__":
    main()
