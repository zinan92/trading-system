from __future__ import annotations

import argparse
import json

from services.run_history import RunHistory


def main() -> None:
    parser = argparse.ArgumentParser(description="Query the local run history (one record per runner cycle).")
    parser.add_argument("--recent", type=int, default=20, help="Show the N most recent cycles (newest first).")
    parser.add_argument("--since", help="Filter run_date >= YYYY-MM-DD.")
    parser.add_argument("--until", help="Filter run_date <= YYYY-MM-DD.")
    parser.add_argument("--state", help="Filter by state (e.g. ok / error).")
    args = parser.parse_args()

    history = RunHistory()
    if args.since or args.until or args.state:
        records = history.query(since=args.since, until=args.until, state=args.state)
    else:
        records = history.recent(limit=args.recent)
    print(json.dumps(records, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
