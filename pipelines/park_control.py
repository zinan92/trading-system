"""Run one Telegram ingress + Park Paper execution pass.

The command is suitable for a short systemd/launchd timer.  It never calls
the legacy cycle runner, autonomous supervisor, Shadow wrapper, Feishu, or a
live adapter.  Missing release evidence or secrets produces a typed blocked
receipt rather than a best-effort start.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from services.journal_store import load_json
from services.park_ai_provider_gateway import ParkAiProviderGateway
from services.park_legacy_cutover import load_effective_park_config
from services.park_legacy_cutover_runtime import run_legacy_cutover_once
from services.park_paper_runtime import (
    ParkPaperRuntime,
    ParkPaperRuntimeError,
    build_park_authoritative_adapter,
    prepare_park_paper_config,
)
from services.park_safety_evidence import build_park_safety_evidence
from services.park_telegram_runtime import (
    ParkTelegramRouter,
    ParkTelegramRuntimeError,
    ParkTelegramWorker,
)
from services.scheduler_ownership import SchedulerOwnershipGuard
from services.telegram_bot_transport import (
    TelegramBotTransport,
    TelegramBotTransportError,
)


def _latest(path: Path) -> dict[str, Any]:
    rows = load_json(path)
    return dict(rows[-1]) if rows and isinstance(rows[-1], dict) else {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one Park Telegram + Paper pass.")
    parser.add_argument("--output-root", default=os.getenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", "outputs"))
    parser.add_argument("--park-config", default=os.getenv("TRADING_ORCHESTRATOR_PARK_CONFIG", "configs/park_strategy_track.json"))
    parser.add_argument("--park-user-id", default=os.getenv("TRADING_ORCHESTRATOR_TELEGRAM_PARK_USER_ID", ""))
    parser.add_argument("--chat-id", default=os.getenv("TRADING_ORCHESTRATOR_TELEGRAM_CHAT_ID", ""))
    parser.add_argument("--timeout-seconds", type=int, default=20)
    args = parser.parse_args(argv)
    output_root = Path(args.output_root)
    try:
        ownership = SchedulerOwnershipGuard(output_root).verify()
        if not ownership.get("ok"):
            result = {
                "schema_version": "park-control-v1",
                "status": "blocked",
                "code": ownership.get("blocker") or "scheduler_ownership_blocked",
                "ownership": ownership,
                "paper_only": True,
            }
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 79
        config = json.loads(Path(args.park_config).read_text(encoding="utf-8"))
        config = load_effective_park_config(output_root, config)
        try:
            config = prepare_park_paper_config(config)
        except Exception:
            # The read-only router will persist the typed contract blocker;
            # never invent an instrument or fee model at the process boundary.
            pass
        transport = TelegramBotTransport(chat_id=args.chat_id)
        intent_parser = ParkAiProviderGateway()
        router = ParkTelegramRouter(
            output_root,
            park_user_id=args.park_user_id,
            chat_id=args.chat_id,
            intent_parser=intent_parser,
            config=config,
        )
        telegram = ParkTelegramWorker(router, timeout_seconds=args.timeout_seconds).run_once(transport)
        legacy_cutover = run_legacy_cutover_once(
            output_root,
            config=config,
            park_user_id=args.park_user_id,
            chat_id=args.chat_id,
            repo_root=Path(__file__).resolve().parents[1],
            interpreter=os.getenv("TRADING_ORCHESTRATOR_NAUTILUS_PYTHON") or None,
        )
        # A completed exact-set receipt is the only way the default-off static
        # config becomes effective for the next Park runtime pass.
        config = load_effective_park_config(output_root, config)
        try:
            binding = build_park_authoritative_adapter(output_root, config=config)
            build_park_safety_evidence(
                output_root,
                config=config,
                repo_root=Path(__file__).resolve().parents[1],
                interpreter=os.getenv("TRADING_ORCHESTRATOR_NAUTILUS_PYTHON") or None,
                adapter=binding.adapter,
            )
            evidence = _latest(output_root / "park_strategy" / "safety_evidence.json")
            execution = ParkPaperRuntime(
                output_root,
                adapter=binding.adapter,
                park_user_id=args.park_user_id,
                chat_id=args.chat_id,
                config=config,
                safety_evidence_reader=lambda: evidence,
                mutation_authorizer=binding.authorize,
                mutation_revoker=binding.revoke,
            ).run_once()
        except ParkPaperRuntimeError as exc:
            execution = {
                "schema_version": "park-paper-runtime-v1",
                "status": "blocked",
                "code": exc.code,
                "paper_only": True,
            }
        delivery = router.drain_outbound(transport)
        result = {
            "schema_version": "park-control-v1",
            "status": "pass" if execution.get("status") != "blocked" else "blocked",
            "telegram": telegram,
            "legacy_cutover": legacy_cutover,
            "execution": execution,
            "delivery": delivery,
            "paper_only": True,
        }
    except (TelegramBotTransportError, ParkTelegramRuntimeError, ValueError, OSError, json.JSONDecodeError) as exc:
        result = {
            "schema_version": "park-control-v1",
            "status": "blocked",
            "code": getattr(exc, "code", "park_control_blocked"),
            "paper_only": True,
        }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("status") == "pass" else 2


if __name__ == "__main__":
    sys.exit(main())
