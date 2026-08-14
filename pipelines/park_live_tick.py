"""Run one bounded Park Paper execution tick.

This command is deliberately separate from the legacy DualTrack cycle runner.
It is opt-in, Paper-only, and fails closed unless the target release has
explicitly enabled the Park contract and supplied source-bound safety evidence.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from services.journal_store import load_json
from services.park_paper_runtime import (
    ParkPaperRuntime,
    ParkPaperRuntimeError,
    build_park_authoritative_adapter,
)


def _load_config(path: str) -> dict[str, Any]:
    target = Path(path)
    return json.loads(target.read_text(encoding="utf-8"))


def _latest(path: Path) -> dict[str, Any]:
    rows = load_json(path)
    return dict(rows[-1]) if rows and isinstance(rows[-1], dict) else {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one Park Strategy Track Paper tick.")
    parser.add_argument("--output-root", default=os.getenv("TRADING_ORCHESTRATOR_OUTPUT_ROOT", "outputs"))
    parser.add_argument(
        "--park-config",
        default=os.getenv("TRADING_ORCHESTRATOR_PARK_CONFIG", "configs/park_strategy_track.json"),
    )
    parser.add_argument("--park-user-id", default=os.getenv("TRADING_ORCHESTRATOR_TELEGRAM_PARK_USER_ID", ""))
    parser.add_argument("--chat-id", default=os.getenv("TRADING_ORCHESTRATOR_TELEGRAM_CHAT_ID", ""))
    args = parser.parse_args(argv)
    output_root = Path(args.output_root)
    try:
        config = _load_config(args.park_config)
        binding = build_park_authoritative_adapter(output_root, config=config)
        evidence = _latest(output_root / "park_strategy" / "safety_evidence.json")
        runtime = ParkPaperRuntime(
            output_root,
            adapter=binding.adapter,
            park_user_id=args.park_user_id,
            chat_id=args.chat_id,
            config=config,
            safety_evidence_reader=lambda: evidence,
            mutation_authorizer=binding.authorize,
            mutation_revoker=binding.revoke,
        )
        result = runtime.run_once()
    except (ParkPaperRuntimeError, OSError, ValueError, json.JSONDecodeError) as exc:
        result = {
            "schema_version": "park-paper-runtime-v1",
            "status": "blocked",
            "code": getattr(exc, "code", "park_runtime_blocked"),
            "paper_only": True,
        }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("status") in {"idle", "awaiting_confirmation", "active", "paused"} else 2


if __name__ == "__main__":
    sys.exit(main())
