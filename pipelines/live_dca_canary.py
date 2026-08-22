"""Attended Live DCA canary operator seam.

There is intentionally no default network transport.  A reviewed adapter must
be injected by the attended release procedure; without one this command
records a durable blocker and exits non-zero.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from services.journal_store import write_json
from services.live_dca_canary import LiveDcaCanary, LiveDcaCanaryError


def _timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def run_attended_canary(
    *,
    output_root: Path,
    plan: Mapping[str, Any] | None,
    park_user_id: str,
    park_chat_id: str,
    action: str,
    transport_factory: Callable[[], Any] | None = None,
    entry_index: int | None = None,
    order_id: str | None = None,
    quantity: float | None = None,
    timestamp: str | None = None,
) -> dict[str, Any]:
    root = Path(output_root)
    observed_at = str(timestamp or _timestamp())
    if transport_factory is None:
        payload = {
            "schema_version": "live-dca-canary-v1",
            "status": "blocked",
            "blocker": "live_transport_not_registered",
            "next_action": "notify_park_and_wait",
            "environment": "mainnet",
            "broker_id": "hyperliquid",
            "strategy_scope": "dca",
            "network_io": False,
            "live_writes_enabled": False,
            "real_money_eligible": False,
            "observed_at": observed_at,
        }
        write_json(root / "dualtrack" / "live_dca_canary" / "current.json", [payload])
        return payload
    canary = None
    try:
        transport = transport_factory()
        canary = LiveDcaCanary(root, transport=transport, park_user_id=park_user_id, park_chat_id=park_chat_id)
        if action == "start":
            if not isinstance(plan, Mapping):
                raise LiveDcaCanaryError("plan_missing", "DCA plan is required for start")
            result = canary.start(plan, timestamp=observed_at)
        elif action == "entry":
            if entry_index is None:
                raise LiveDcaCanaryError("entry_index_missing", "entry index is required")
            result = canary.submit_entry(entry_index, timestamp=observed_at)
        elif action == "protection":
            if quantity is None:
                raise LiveDcaCanaryError("protection_quantity_missing", "protection quantity is required")
            result = canary.replace_protection(quantity=quantity, timestamp=observed_at)
        elif action == "cancel":
            result = canary.cancel(str(order_id or ""), timestamp=observed_at)
        elif action == "flatten":
            result = canary.flatten(timestamp=observed_at)
        elif action == "stop":
            result = canary.stop(timestamp=observed_at)
        elif action == "kill":
            result = canary.kill(timestamp=observed_at)
        else:
            raise LiveDcaCanaryError("action_invalid", f"unsupported attended action: {action}")
        return dict(result)
    except LiveDcaCanaryError as exc:
        if canary is not None and canary.snapshot():
            state = canary.snapshot()
            state["status"] = "blocked"
            state["blocker"] = exc.code
            state["next_action"] = "notify_park_and_wait"
            canary._event(state, "operator_blocked", timestamp=observed_at, code=exc.code)
            canary._save(state)
            return state
        payload = {
            "schema_version": "live-dca-canary-v1",
            "status": "blocked",
            "blocker": exc.code,
            "next_action": "notify_park_and_wait",
            "environment": "mainnet",
            "broker_id": "hyperliquid",
            "strategy_scope": "dca",
            "network_io": False,
            "live_writes_enabled": False,
            "real_money_eligible": False,
            "observed_at": observed_at,
        }
        write_json(root / "dualtrack" / "live_dca_canary" / "current.json", [payload])
        return payload
    except Exception as exc:  # unknown transport/factory state is fail-closed and redacted.
        if canary is not None and canary.snapshot():
            state = canary.snapshot()
            state["status"] = "blocked"
            state["blocker"] = f"transport_factory_failed:{type(exc).__name__}"
            state["next_action"] = "notify_park_and_wait"
            canary._event(state, "operator_blocked", timestamp=observed_at, code=state["blocker"])
            canary._save(state)
            return state
        payload = {
            "schema_version": "live-dca-canary-v1",
            "status": "blocked",
            "blocker": f"transport_factory_failed:{type(exc).__name__}",
            "next_action": "notify_park_and_wait",
            "environment": "mainnet",
            "broker_id": "hyperliquid",
            "strategy_scope": "dca",
            "network_io": False,
            "live_writes_enabled": False,
            "real_money_eligible": False,
            "observed_at": observed_at,
        }
        write_json(root / "dualtrack" / "live_dca_canary" / "current.json", [payload])
        return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one attended DCA canary action; no default transport is provided.")
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--action", choices=("start", "entry", "protection", "cancel", "flatten", "stop", "kill"), required=True)
    parser.add_argument("--plan-file", type=Path)
    parser.add_argument("--park-user-id", required=True)
    parser.add_argument("--park-chat-id", required=True)
    parser.add_argument("--entry-index", type=int)
    parser.add_argument("--order-id")
    parser.add_argument("--quantity", type=float)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    plan = None
    if args.plan_file is not None:
        value = json.loads(args.plan_file.read_text(encoding="utf-8"))
        if not isinstance(value, Mapping):
            raise SystemExit("plan file must contain a JSON object")
        plan = value
    result = run_attended_canary(
        output_root=args.output_root,
        plan=plan,
        park_user_id=args.park_user_id,
        park_chat_id=args.park_chat_id,
        action=args.action,
        entry_index=args.entry_index,
        order_id=args.order_id,
        quantity=args.quantity,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"live_dca_canary: {result.get('status')} next_action={result.get('next_action')}")
    return 0 if result.get("status") not in {"blocked", "failed"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
