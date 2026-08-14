"""Run one bounded Park Telegram control-plane polling pass.

The command is intentionally opt-in and does not enable the Park cutover.  It
is useful for local Paper integration drills once the target environment has
injected the Bot token/chat/user values through its secret store.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from services.telegram_bot_transport import TelegramBotTransport, TelegramBotTransportError
from services.park_telegram_runtime import ParkTelegramRouter, ParkTelegramRuntimeError, ParkTelegramWorker


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one Paper-only Park Telegram control pass.")
    parser.add_argument("--output-root", default=os.getenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", "outputs"))
    parser.add_argument("--park-user-id", default=os.getenv("TRADING_ORCHESTRATOR_TELEGRAM_PARK_USER_ID", ""))
    parser.add_argument("--chat-id", default=os.getenv("TRADING_ORCHESTRATOR_TELEGRAM_CHAT_ID", ""))
    parser.add_argument("--timeout-seconds", type=int, default=20)
    args = parser.parse_args(argv)
    if not args.park_user_id:
        print(json.dumps({"status": "blocked", "code": "park_user_id_missing", "paper_only": True}, ensure_ascii=False))
        return 2
    try:
        transport = TelegramBotTransport(chat_id=args.chat_id)
        router = ParkTelegramRouter(
            Path(args.output_root),
            park_user_id=args.park_user_id,
            chat_id=args.chat_id,
        )
        result = ParkTelegramWorker(router, timeout_seconds=args.timeout_seconds).run_once(transport)
    except (TelegramBotTransportError, ParkTelegramRuntimeError, ValueError) as exc:
        code = getattr(exc, "code", "park_telegram_blocked")
        print(json.dumps({"status": "blocked", "code": code, "paper_only": True}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("status") == "pass" else 2


if __name__ == "__main__":
    sys.exit(main())

