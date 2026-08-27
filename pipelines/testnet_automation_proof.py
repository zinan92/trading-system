"""Attended Hyperliquid Testnet proof entrypoint for the Coordinator.

``preflight`` is credential-free and never invokes a backend.  ``start`` is
the only exposure-changing action: it requires an explicit Testnet flag,
exact acknowledgement, a Park-confirmed plan, and a full market-fact document.
It creates one selected asset/Execution Slice, then stops after the canonical
DCA or Grid lifecycle submits its initial entry.  Scheduler/event progression
continues through the Coordinator API and remains fail-closed on unknowns.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from schemas.portfolio import PortfolioPolicy, PortfolioSnapshot
from services.broker_composition import BrokerBuildContext, build_broker_execution_port
from services.journal_store import load_json
from services.testnet_automation_coordinator import TestnetAutomationCoordinator
from services.standard_broker_external_execution import (
    PROTECTED_CAPABILITY_REVISION,
    PROTECTED_EXTERNAL_PROFILE,
    STANDARD_BROKER_RELEASE_SHA,
    StandardBrokerExternalExecutionAdapter,
)


ACKNOWLEDGEMENT = "I_UNDERSTAND_ONE_ATTENDED_TESTNET_STRATEGY_ACTION"
DEFAULT_OUTPUT_ROOT = Path("outputs")
_MARKET_REQUIRED = (
    "execution_ready",
    "fresh",
    "is_synthetic",
    "fallback_policy",
    "observed_at",
    "bid",
    "ask",
    "mid",
    "mark",
    "oracle",
    "impact",
    "depth_notional",
    "max_slippage",
    "max_oracle_deviation_bps",
    "source",
    "cursor",
    "broker_id",
    "environment",
    "instrument_id",
    "asset_index",
    "mapping_revision",
    "universe_revision",
    "connection_epoch",
)


class TestnetAutomationProofError(ValueError):
    """Redacted operator-facing blocker."""

    def __init__(self, reason_code: str, *, result: Mapping[str, Any] | None = None) -> None:
        self.reason_code = str(reason_code or "unknown_error")
        self.result = dict(result or {})
        super().__init__(self.reason_code)


def _load_json(path: Path) -> Any:
    try:
        rows = load_json(path)
    except Exception as exc:  # noqa: BLE001 - normalize input blockers.
        raise TestnetAutomationProofError("input_unavailable") from exc
    if isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], Mapping):
        return dict(rows[0])
    if isinstance(rows, list):
        return rows
    return rows


def _load_mapping(path: Path, field: str) -> dict[str, Any]:
    value = _load_json(path)
    if isinstance(value, Mapping) and isinstance(value.get(field), Mapping):
        value = value[field]
    if not isinstance(value, Mapping):
        raise TestnetAutomationProofError(f"{field}_must_be_object")
    return {str(key): item for key, item in value.items()}


def _fingerprint(account_address: str) -> str:
    return "sha256:" + hashlib.sha256(account_address.encode("utf-8")).hexdigest()


def _timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _decimal(value: Any, field: str, *, positive: bool = False) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise TestnetAutomationProofError(f"{field}_invalid") from exc
    if not number.is_finite() or (number <= 0 if positive else number < 0):
        raise TestnetAutomationProofError(f"{field}_invalid")
    return number


def _plan_family(plan: Mapping[str, Any]) -> str:
    family = str(plan.get("strategy_type") or plan.get("strategy_family") or "").strip().lower()
    if family not in {"dca", "grid"}:
        raise TestnetAutomationProofError("strategy_family_invalid")
    return family


def _market(path: Path, *, plan: Mapping[str, Any]) -> dict[str, Any]:
    value = _load_mapping(path, "market")
    missing = [field for field in _MARKET_REQUIRED if field not in value]
    if missing:
        raise TestnetAutomationProofError(
            "market_facts_missing",
            result={"fields": missing},
        )
    instrument_id = str(plan.get("instrument_id") or "").strip()
    if str(value.get("instrument_id") or "") != instrument_id:
        raise TestnetAutomationProofError("market_instrument_mismatch")
    return value


def _candidate(plan: Mapping[str, Any], market: Mapping[str, Any], family: str) -> dict[str, Any]:
    instrument_id = str(plan.get("instrument_id") or "").strip()
    if family == "dca":
        dca = plan.get("dca") if isinstance(plan.get("dca"), Mapping) else {}
        levels = dca.get("entry_levels") if isinstance(dca.get("entry_levels"), list) else []
        per_addition = _decimal(dca.get("notional_per_addition"), "notional_per_addition", positive=True)
        requested_quantity = sum(
            (per_addition / _decimal(level, "entry_level", positive=True) for level in levels),
            Decimal("0"),
        )
        requested_notional = per_addition * Decimal(str(max(1, len(levels))))
    else:
        grid = plan.get("grid") if isinstance(plan.get("grid"), Mapping) else {}
        rungs = grid.get("rungs") if isinstance(grid.get("rungs"), list) else []
        requested_quantity = sum(
            (_decimal(row.get("quantity"), "grid_quantity", positive=True) for row in rungs if isinstance(row, Mapping)),
            Decimal("0"),
        )
        requested_notional = sum(
            (
                _decimal(row.get("price"), "grid_price", positive=True)
                * _decimal(row.get("quantity"), "grid_quantity", positive=True)
                for row in rungs
                if isinstance(row, Mapping)
            ),
            Decimal("0"),
        )
    return {
        "candidate_id": f"coordinator-{family}-{instrument_id.lower()}",
        "asset": instrument_id.split("-", 1)[0],
        "instrument_id": instrument_id,
        "rank": 1,
        "status": "active",
        "mapping_valid": True,
        "price_tick": str(
            plan.get("price_tick")
            or (
                (plan.get("grid") or {}).get("price_tick")
                if isinstance(plan.get("grid"), Mapping)
                else None
            )
            or "0.001"
        ),
        "quantity_step": str(plan.get("quantity_step") or "0.001"),
        "minimum_quantity": str(plan.get("minimum_quantity") or "0.001"),
        "minimum_notional": str(plan.get("minimum_notional") or "1"),
        "strategy": {
            "direction": str(plan.get("direction") or "long").lower(),
            "requested_quantity": str(requested_quantity),
            "requested_notional": str(requested_notional),
            "position_action": "open",
            "position_management": {"mode": family, "instrument_id": instrument_id},
            "protection_intent": {"mode": "position_following"},
        },
        "market": dict(market),
    }


def _snapshot(plan: Mapping[str, Any], *, account_address: str, equity: Decimal, market: Mapping[str, Any]) -> PortfolioSnapshot:
    session_id = str(plan.get("strategy_session_id") or "").strip()
    if not session_id:
        raise TestnetAutomationProofError("strategy_session_id_required")
    return PortfolioSnapshot(
        snapshot_id=f"snapshot-{session_id}",
        portfolio_session_id=f"portfolio-{session_id}",
        account_id=account_address,
        observed_at=str(market["observed_at"]),
        equity=equity,
        available_cash=equity,
        coherent=True,
        fresh=True,
        provenance={
            "broker_id": "hyperliquid",
            "environment": "testnet",
            "account_fingerprint": _fingerprint(account_address),
            "cursor": str(market["cursor"]),
            "mapping_revision": str(market["mapping_revision"]),
            "universe_revision": str(market["universe_revision"]),
            "connection_epoch": str(market["connection_epoch"]),
        },
    )


def _policy(path: Path | None) -> PortfolioPolicy:
    value = _load_mapping(path, "policy") if path is not None else {}
    return PortfolioPolicy(
        policy_id=str(value.get("policy_id") or "testnet-proof-policy"),
        policy_revision=str(value.get("policy_revision") or "testnet-proof-policy-v1"),
        max_single_asset_aum_pct=value.get("max_single_asset_aum_pct", "30"),
        max_active_assets=int(value.get("max_active_assets", 10)),
        max_total_exposure_pct=value.get("max_total_exposure_pct", "100"),
        max_margin_pct=value.get("max_margin_pct", "100"),
        max_leverage=value.get("max_leverage", "5"),
    )


def _confirmation(path: Path, *, plan: Mapping[str, Any]) -> dict[str, Any]:
    document = _load_json(path)
    if isinstance(document, list):
        value = next(
            (
                dict(row)
                for row in reversed(document)
                if isinstance(row, Mapping) and row.get("event") == "confirmed"
            ),
            {},
        )
    elif isinstance(document, Mapping) and isinstance(document.get("confirmation"), Mapping):
        value = dict(document["confirmation"])
    elif isinstance(document, Mapping):
        value = dict(document)
    else:
        value = {}
    if (
        value.get("execution_authorized") is not True
        or str(value.get("execution_environment") or "").lower() != "testnet"
        or str(value.get("plan_digest") or "") != str(plan.get("plan_digest") or "")
        or not str(value.get("confirmation_id") or "").strip()
    ):
        raise TestnetAutomationProofError("confirmation_identity_invalid")
    return value


def _context(args: argparse.Namespace, *, family: str | None = None) -> BrokerBuildContext:
    capability_revision = str(args.capability_revision or PROTECTED_CAPABILITY_REVISION)
    return BrokerBuildContext(
        output_root=Path(args.output_root),
        execution_mode="live",
        live_trading_enabled=False,
        broker_config={
            "provider": "standard_broker",
            "broker_id": "hyperliquid",
            "environment": "testnet",
            "transport_profile": PROTECTED_EXTERNAL_PROFILE,
            "account_id": args.account_address,
            "runtime_id": args.runtime_id,
            "release_sha": args.release_sha,
            "standard_broker_release_sha": args.standard_broker_release_sha,
            "capability_revision": capability_revision,
            "approval_id": args.approval_id,
            "approved_by": args.approved_by,
            "secret_file": args.secret_file,
            "credential_reference": args.credential_reference,
            "instrument_binding": {"instrument_id": args.instrument_id} if args.instrument_id else {},
            "strategy_family": family,
        },
    )


def _preflight(args: argparse.Namespace) -> dict[str, Any]:
    result = StandardBrokerExternalExecutionAdapter.preflight_build_context(
        _context(args)
    )
    result["action"] = "preflight"
    return result


def _start(args: argparse.Namespace) -> dict[str, Any]:
    plan = _load_mapping(Path(args.strategy_plan), "plan")
    family = _plan_family(plan)
    if args.strategy_family and args.strategy_family != family:
        raise TestnetAutomationProofError("strategy_family_mismatch")
    if str(plan.get("plan_digest") or "").strip() == "":
        raise TestnetAutomationProofError("plan_digest_required")
    args.instrument_id = str(plan.get("instrument_id") or args.instrument_id or "").strip()
    if not args.instrument_id:
        raise TestnetAutomationProofError("instrument_id_required")
    market = _market(Path(args.market), plan=plan)
    confirmation = _confirmation(Path(args.confirmation), plan=plan)
    coordinator = TestnetAutomationCoordinator(Path(args.output_root))
    account_fingerprint = _fingerprint(args.account_address)
    activation = {
        "strategy_family": family,
        "strategy_session_id": plan.get("strategy_session_id"),
        "strategy_revision_id": plan.get("strategy_revision_id"),
        "plan_digest": plan.get("plan_digest"),
        "account_fingerprint": account_fingerprint,
        "broker_id": "hyperliquid",
        "environment": "testnet",
        "transport_profile": PROTECTED_EXTERNAL_PROFILE,
        "instrument_id": plan.get("instrument_id"),
        "runtime_id": args.runtime_id,
        "release_sha": args.release_sha,
        "capability_revision": args.capability_revision,
    }
    current = coordinator.status()
    if current.get("status") == "idle":
        current = coordinator.activate(activation, command_id=f"activate:{plan['plan_digest']}")
    if current.get("status") == "activated":
        equity = _decimal(args.equity, "equity", positive=True)
        snapshot = _snapshot(plan, account_address=args.account_address, equity=equity, market=market)
        selected = coordinator.command(
            "select_candidate",
            {
                "candidates": [_candidate(plan, market, family)],
                "snapshot": snapshot,
                "policy": _policy(Path(args.policy) if args.policy else None),
            },
            command_id=f"select:{plan['plan_digest']}",
        )
        current = selected
    if current.get("status") != "candidate_selected":
        raise TestnetAutomationProofError(
            "candidate_selection_blocked",
            result={"status": current.get("status"), "blocker": current.get("blocker")},
        )
    if str(confirmation.get("activation_id") or "") != str(current.get("activation_id") or ""):
        raise TestnetAutomationProofError("confirmation_activation_mismatch")
    broker = build_broker_execution_port(_context(args, family=family))
    try:
        preflight = broker.preflight(strategy_family=family)
        if preflight.get("ready") is not True:
            raise TestnetAutomationProofError(
                "broker_preflight_blocked",
                result={"blocker": preflight.get("capability_gaps") or preflight.get("blocker")},
            )
        if family == "dca":
            result = coordinator.start_dca_session(
                plan,
                confirmation=confirmation,
                market=market,
                broker=broker,
            )
        else:
            result = coordinator.start_grid_session(
                plan,
                confirmation=confirmation,
                market=market,
                broker=broker,
            )
        return {
            "action": "start",
            "strategy_family": family,
            "status": result.get("status"),
            "next_action": result.get("next_action"),
            "execution_slice_id": result.get("selected_execution_slice_id"),
            "lifecycle_status": result.get("lifecycle", {}).get("status"),
            "execution_mutation": result.get("execution_mutation"),
            "network_operation_invoked": result.get("network_operation_invoked"),
        }
    finally:
        close = getattr(broker, "close", None)
        if callable(close):
            close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Attended Hyperliquid Testnet DCA/Grid proof")
    parser.add_argument("--action", choices=("status", "preflight", "start"), default="status")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--strategy-plan", type=Path)
    parser.add_argument("--market", type=Path)
    parser.add_argument("--confirmation", type=Path)
    parser.add_argument("--policy", type=Path)
    parser.add_argument("--strategy-family", choices=("dca", "grid"))
    parser.add_argument("--instrument-id", default="")
    parser.add_argument("--account-address", default="")
    parser.add_argument("--runtime-id", default="")
    parser.add_argument("--release-sha", default="")
    parser.add_argument("--standard-broker-release-sha", default=STANDARD_BROKER_RELEASE_SHA)
    parser.add_argument("--capability-revision", default=PROTECTED_CAPABILITY_REVISION)
    parser.add_argument("--approval-id", default="")
    parser.add_argument("--approved-by", default="")
    parser.add_argument("--secret-file", type=Path)
    parser.add_argument("--credential-reference", default="file-secret://hyperliquid-testnet")
    parser.add_argument("--equity", default="1000")
    parser.add_argument("--execute-testnet", action="store_true")
    parser.add_argument("--acknowledge", default="")
    return parser


def _validate_args(args: argparse.Namespace) -> None:
    if args.action == "status":
        return
    if not args.account_address or not args.runtime_id or not args.release_sha:
        raise TestnetAutomationProofError("activation_identity_required")
    if args.standard_broker_release_sha != STANDARD_BROKER_RELEASE_SHA:
        raise TestnetAutomationProofError("standard_broker_dependency_sha_mismatch")
    if args.action == "preflight":
        if not args.approval_id or not args.approved_by or args.secret_file is None:
            raise TestnetAutomationProofError("preflight_identity_required")
        return
    if not args.strategy_plan or not args.market or not args.confirmation:
        raise TestnetAutomationProofError("start_inputs_required")
    if not args.approval_id or not args.approved_by or args.secret_file is None:
        raise TestnetAutomationProofError("start_identity_required")
    if not args.execute_testnet:
        raise TestnetAutomationProofError("explicit_execute_flag_required")
    if args.acknowledge != ACKNOWLEDGEMENT:
        raise TestnetAutomationProofError("exact_acknowledgement_required")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        _validate_args(args)
        if args.action == "status":
            result = TestnetAutomationCoordinator(Path(args.output_root)).status()
        elif args.action == "preflight":
            result = _preflight(args)
        else:
            result = _start(args)
        print(json.dumps(result, sort_keys=True, ensure_ascii=False, default=str))
        return 0
    except TestnetAutomationProofError as exc:
        result = {"status": "BLOCKED", "reason_code": exc.reason_code, "secret_resolved": False, **exc.result}
        print(json.dumps(result, sort_keys=True, ensure_ascii=False, default=str))
        return 2
    except Exception as exc:  # noqa: BLE001 - redact provider/path details.
        print(
            json.dumps(
                {"status": "BLOCKED", "reason_code": type(exc).__name__.lower(), "secret_resolved": False},
                sort_keys=True,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ACKNOWLEDGEMENT",
    "PROTECTED_CAPABILITY_REVISION",
    "PROTECTED_EXTERNAL_PROFILE",
    "TestnetAutomationProofError",
    "build_parser",
    "main",
]
