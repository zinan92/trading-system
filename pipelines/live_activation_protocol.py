"""Attended, read-only Live activation proposal producer.

This command assembles a source-bound preflight from already-persisted
Testnet evidence and operator-supplied non-secret identity references.  It
only writes an activation proposal to the local journal; it never contacts a
venue, reads credential values, enables Live writes, or sends Telegram text.
The subsequent exact ``confirm live ...`` command is handled by
``ParkTelegramRouter``.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any, Mapping

from services.config_loader import ROOT, load_pipeline_config
from services.journal_store import load_json
from services.live_activation_gate import LiveActivationGate
from services.testnet_soak_readiness import TestnetSoakReadiness


def _object(path: Path) -> Mapping[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def _readiness(output_root: Path, path: Path | None) -> Mapping[str, Any]:
    if path is not None:
        return _object(path)
    rows = TestnetSoakReadiness(output_root).receipts()
    if not rows:
        raise ValueError("Testnet readiness receipt is missing")
    return rows[-1]


def create_proposal(
    *,
    output_root: Path,
    park_user_id: str,
    park_chat_id: str,
    account_id: str,
    environment_fingerprint: str,
    release_sha: str,
    credential_source: str,
    plan_digest: str,
    expires_at: float,
    capabilities: Mapping[str, Any],
    risk_limits: Mapping[str, Any],
    readiness: Mapping[str, Any],
    approved_plan: Mapping[str, Any],
) -> dict[str, Any]:
    gate = LiveActivationGate(
        output_root,
        park_user_id=park_user_id,
        park_chat_id=park_chat_id,
        approved_plan_resolver=lambda: approved_plan,
    )
    preflight = gate.preflight(
        broker_id="hyperliquid",
        account_id=account_id,
        environment_fingerprint=environment_fingerprint,
        release_sha=release_sha,
        credential_source=credential_source,
        instrument_scope="default_perpetuals",
        strategy_scope="dca",
        readiness=readiness,
        capabilities=capabilities,
        risk_limits=risk_limits,
    )
    if preflight.get("status") != "ready_for_activation":
        return {"status": "blocked", "preflight": preflight, "proposal": None, "network_io": False, "live_writes_enabled": False}
    approval_path = output_root / "dualtrack" / "live_activation" / "approved_dca_plan.json"
    existing_approval = load_json(approval_path)
    if existing_approval and existing_approval[-1] != dict(approved_plan):
        return {
            "status": "blocked",
            "code": "approved_dca_plan_immutable",
            "preflight": preflight,
            "proposal": None,
            "network_io": False,
            "live_writes_enabled": False,
        }
    if not existing_approval:
        from services.journal_store import write_json

        write_json(approval_path, [dict(approved_plan)])
    proposal = gate.prepare_activation(preflight, plan_digest=plan_digest, expires_at=expires_at)
    return {"status": "awaiting_confirmation", "preflight": preflight, "proposal": proposal, "network_io": False, "live_writes_enabled": False}


def main(argv: list[str] | None = None) -> int:
    config = load_pipeline_config()
    parser = argparse.ArgumentParser(description="Create a read-only source-bound Live activation proposal.")
    parser.add_argument("--output-root", type=Path, default=ROOT / str(config.get("output_root", "outputs")))
    parser.add_argument("--readiness-file", type=Path)
    parser.add_argument("--capabilities-file", type=Path, required=True)
    parser.add_argument("--risk-limits-file", type=Path, required=True)
    parser.add_argument("--approved-plan-file", type=Path, required=True)
    parser.add_argument("--park-user-id", required=True)
    parser.add_argument("--park-chat-id", required=True)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--environment-fingerprint", required=True)
    parser.add_argument("--release-sha", required=True)
    parser.add_argument("--credential-source", required=True, help="Environment-variable name only; never a credential value.")
    parser.add_argument("--plan-digest", required=True)
    parser.add_argument("--expires-at", type=float, default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    output_root = Path(args.output_root)
    result = create_proposal(
        output_root=output_root,
        park_user_id=args.park_user_id,
        park_chat_id=args.park_chat_id,
        account_id=args.account_id,
        environment_fingerprint=args.environment_fingerprint,
        release_sha=args.release_sha,
        credential_source=args.credential_source,
        plan_digest=args.plan_digest,
        expires_at=float(args.expires_at if args.expires_at is not None else time.time() + 900),
        capabilities=_object(args.capabilities_file),
        risk_limits=_object(args.risk_limits_file),
        readiness=_readiness(output_root, args.readiness_file),
        approved_plan=_object(args.approved_plan_file),
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        proposal = result.get("proposal") or {}
        print(f"live_activation: {result['status']}")
        print(f"activation_digest={proposal.get('activation_digest', '')}")
        print(f"next_action={'confirm live <activation_digest> <release_sha> <account_id> <environment_fingerprint> dca <plan_digest>' if proposal else 'notify_park_and_wait'}")
    return 0 if result.get("status") == "awaiting_confirmation" else 2


if __name__ == "__main__":
    raise SystemExit(main())
