from __future__ import annotations

import argparse
import json
from datetime import date

from services.live_approval import LiveApprovalStore


def main() -> None:
    parser = argparse.ArgumentParser(description="Create, approve, revoke, or inspect dated live approval artifacts.")
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--action", choices=["request", "approve", "revoke", "status"], default="status")
    parser.add_argument("--approver", default="")
    parser.add_argument("--notes", default="")
    parser.add_argument("--force", action="store_true", help="Allow approval even when dry-run gate is not ready.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    store = LiveApprovalStore()
    if args.action == "request":
        result = store.request(args.date, args.notes)
    elif args.action == "approve":
        if not args.approver:
            raise SystemExit("--approver is required for approval")
        result = store.approve(args.date, args.approver, args.notes, force=args.force)
    elif args.action == "revoke":
        result = store.revoke(args.date, args.notes)
    else:
        result = store.status(args.date)

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return
    print(f"live_approval: {result['status']} date={result['run_date']} approved={result['approved']}")
    print(f"request: {result['request_path']}")
    print(f"approval: {result['approved_path']}")


if __name__ == "__main__":
    main()
