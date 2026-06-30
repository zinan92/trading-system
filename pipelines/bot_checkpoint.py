from __future__ import annotations

import argparse

from services.bot_checkpoint import BotCheckpoint


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the local recovery checkpoint for the GOLD 5m Trading Bot.")
    parser.add_argument("--date", required=True)
    args = parser.parse_args()
    result = BotCheckpoint().build(args.date)
    print(f"bot_checkpoint: {result['status']} date={result['run_date']} actions={len(result['resume_actions'])}")


if __name__ == "__main__":
    main()
