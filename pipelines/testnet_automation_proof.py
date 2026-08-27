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
import time
from typing import Any, Mapping, Sequence

from schemas.portfolio import PortfolioPolicy, PortfolioSnapshot
from services.broker_composition import BrokerBuildContext, build_broker_execution_port
from services.journal_store import load_json
from services.park_confirmation import ParkConfirmationLedger
from services.testnet_automation_coordinator import TestnetAutomationCoordinator
from services.standard_broker_external_execution import (
    PROTECTED_CAPABILITY_REVISION,
    PROTECTED_EXTERNAL_PROFILE,
    STANDARD_BROKER_RELEASE_SHA,
    StandardBrokerExternalExecutionAdapter,
)


ACKNOWLEDGEMENT = "I_UNDERSTAND_ONE_ATTENDED_TESTNET_STRATEGY_ACTION"
DEFAULT_OUTPUT_ROOT = Path("outputs")
MAX_CONFIRMATION_AGE_SECONDS = 900
_APPROVED_MARKET_SOURCES = frozenset(
    {"hyperliquid.external_testnet", "nautilus-hyperliquid.testnet"}
)
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


def _epoch(value: Any, field: str) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        rendered = float(value)
    else:
        text = str(value or "").strip()
        try:
            rendered = float(text)
        except (TypeError, ValueError):
            try:
                rendered = datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
            except ValueError as exc:
                raise TestnetAutomationProofError(f"{field}_invalid") from exc
    if not rendered == rendered or rendered in {float("inf"), float("-inf")}:
        raise TestnetAutomationProofError(f"{field}_invalid")
    return rendered


def _aware_datetime(value: Any, field: str) -> datetime:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TestnetAutomationProofError(f"{field}_invalid") from exc
    if parsed.tzinfo is None:
        raise TestnetAutomationProofError(f"{field}_timezone_missing")
    return parsed.astimezone(timezone.utc)


def _verify_durable_confirmation(
    output_root: Path,
    *,
    plan: Mapping[str, Any],
    confirmation: Mapping[str, Any],
) -> None:
    """Require the supplied projection to match the current Park ledger."""

    ledger = ParkConfirmationLedger(output_root, park_user_id="park")
    rows = ledger.rows()
    proposal_id = str(
        confirmation.get("proposal_id")
        or confirmation.get("confirmation_id")
        or ""
    ).strip()
    proposal = next(
        (
            row
            for row in rows
            if row.get("event") == "proposal"
            and str(row.get("proposal_id") or "") == proposal_id
        ),
        None,
    )
    decision = next(
        (
            row
            for row in reversed(rows)
            if row.get("event") == "confirmed"
            and str(row.get("proposal_id") or "") == proposal_id
        ),
        None,
    )
    if proposal is None or decision is None:
        raise TestnetAutomationProofError("durable_confirmation_missing")
    try:
        if _epoch(proposal.get("expires_at"), "confirmation_expiry") <= time.time():
            raise TestnetAutomationProofError("confirmation_expired")
        confirmed_at = _epoch(decision.get("confirmed_at"), "confirmed_at")
    except TestnetAutomationProofError:
        raise
    age = time.time() - confirmed_at
    if age < 0 or age > MAX_CONFIRMATION_AGE_SECONDS:
        raise TestnetAutomationProofError("confirmation_not_fresh")
    if (
        proposal.get("execution_environment") != "testnet"
        or decision.get("execution_environment") != "testnet"
        or proposal.get("plan_digest") != plan.get("plan_digest")
        or decision.get("plan_digest") != plan.get("plan_digest")
        or decision.get("execution_authorized") is not True
        or str(decision.get("receipt_digest") or "")
        != str(confirmation.get("receipt_digest") or "")
        or decision.get("park_user_id") != "park"
        or str(confirmation.get("operator_id") or confirmation.get("park_user_id") or "") != "park"
        or abs(confirmed_at - _epoch(confirmation.get("confirmed_at"), "confirmed_at")) > 0.001
    ):
        raise TestnetAutomationProofError("durable_confirmation_mismatch")


def _fact_data(reconciliation: object, name: str) -> tuple[object, ...]:
    observation = getattr(reconciliation, name, None)
    fact = getattr(observation, "fact", None)
    data = getattr(fact, "data", ())
    if isinstance(data, tuple):
        return data
    if isinstance(data, (list, set)):
        return tuple(data)
    return ()


def _position_row(position: object, *, account_address: str, portfolio_session_id: str) -> dict[str, Any]:
    instrument_id = str(getattr(position, "instrument_id", "") or "").strip()
    signed_quantity = _decimal(getattr(position, "signed_quantity", None), "account_position_quantity")
    value = getattr(position, "position_value", None)
    notional = _decimal(value, "account_position_value") if value is not None else Decimal("0")
    return {
        "portfolio_session_id": portfolio_session_id,
        "account_id": account_address,
        "asset": instrument_id.split("-", 1)[0],
        "instrument_id": instrument_id,
        "signed_quantity": str(signed_quantity),
        "quantity": str(abs(signed_quantity)),
        "notional": str(abs(notional)),
    }


def _open_order_row(order: object, *, account_address: str, portfolio_session_id: str) -> dict[str, Any]:
    instrument_id = str(getattr(order, "instrument_id", "") or "").strip()
    return {
        "portfolio_session_id": portfolio_session_id,
        "account_id": account_address,
        "asset": instrument_id.split("-", 1)[0],
        "instrument_id": instrument_id,
        "order_id": str(getattr(order, "order_id", "") or "").strip(),
        "state": str(getattr(getattr(order, "state", None), "value", getattr(order, "state", "")) or "").lower(),
    }


def _authoritative_account_snapshot(
    broker: object,
    *,
    plan: Mapping[str, Any],
    account_address: str,
) -> tuple[object, object]:
    instrument_id = str(plan.get("instrument_id") or "").strip()
    try:
        reconciliation = broker.request("order_execution", "reconcile", instrument_id)
        if callable(getattr(reconciliation, "require_coherent", None)):
            reconciliation.require_coherent()
        elif getattr(reconciliation, "passed", False) is not True:
            raise TestnetAutomationProofError("account_reconciliation_not_coherent")
        account = broker.request("account", "read", account_address)
    except TestnetAutomationProofError:
        raise
    except Exception as exc:  # noqa: BLE001 - redact upstream details at the proof boundary.
        raise TestnetAutomationProofError("account_facts_unavailable") from exc
    observed = getattr(reconciliation, "observed_at", None)
    if not isinstance(observed, datetime) or observed.tzinfo is None:
        raise TestnetAutomationProofError("account_observation_invalid")
    age = (datetime.now(timezone.utc) - observed.astimezone(timezone.utc)).total_seconds()
    if age < 0 or age > 120:
        raise TestnetAutomationProofError("account_snapshot_stale")
    actual_address = str(getattr(account, "account_address", "") or "").strip()
    environment = str(
        getattr(getattr(account, "environment", None), "value", getattr(account, "environment", ""))
        or ""
    ).lower()
    broker_id = str(getattr(account, "broker_id", "") or "").strip().lower()
    provenance = getattr(account, "provenance", None)
    source = str(getattr(provenance, "source", "") or "").strip().lower()
    transport_state = str(getattr(provenance, "transport_state", "") or "").strip().lower()
    if (
        actual_address != account_address
        or broker_id != "hyperliquid"
        or environment != "testnet"
        or source not in _APPROVED_MARKET_SOURCES
        or transport_state != "external_testnet"
    ):
        raise TestnetAutomationProofError("account_identity_mismatch")
    equity = getattr(account, "equity", None)
    if equity is None:
        raise TestnetAutomationProofError("account_equity_unavailable")
    positions = _fact_data(reconciliation, "positions")
    open_orders = _fact_data(reconciliation, "open_orders")
    if positions or open_orders:
        raise TestnetAutomationProofError("account_not_clean_for_proof")
    return account, reconciliation


def _authoritative_market(
    broker: object,
    *,
    market: Mapping[str, Any],
    instrument_id: str,
) -> dict[str, Any]:
    try:
        observed_at = datetime.now(timezone.utc)
        raw = broker.market_fact(instrument_id=instrument_id, now=observed_at)
    except Exception as exc:  # noqa: BLE001 - redact upstream details at the proof boundary.
        raise TestnetAutomationProofError("market_fact_unavailable") from exc
    if not isinstance(raw, Mapping):
        raise TestnetAutomationProofError("market_fact_invalid")
    source = str(raw.get("source") or "").strip().lower()
    transport_state = str(raw.get("transport_state") or "").strip().lower()
    if (
        source not in _APPROVED_MARKET_SOURCES
        or transport_state != "external_testnet"
        or str(raw.get("instrument_id") or "") != instrument_id
        or str(raw.get("freshness") or "").lower() != "fresh"
    ):
        raise TestnetAutomationProofError("market_fact_identity_invalid")
    broker_price = _decimal(raw.get("price"), "broker_market_price", positive=True)
    supplied_mid = _decimal(market.get("mid"), "market_mid", positive=True)
    if broker_price != supplied_mid:
        raise TestnetAutomationProofError("market_price_mismatch")
    broker_observed = _aware_datetime(raw.get("observed_at"), "broker_market_observed_at")
    supplied_observed = _aware_datetime(market.get("observed_at"), "market_observed_at")
    if abs((broker_observed - supplied_observed).total_seconds()) > 5:
        raise TestnetAutomationProofError("market_observation_mismatch")
    return {
        **dict(market),
        "broker_market_fact": {
            "instrument_id": instrument_id,
            "price": str(broker_price),
            "freshness": "fresh",
            "observed_at": broker_observed.isoformat(),
            "source": source,
            "transport_state": transport_state,
            "mapping_revision": str(raw.get("mapping_revision") or ""),
        },
    }


def _snapshot(
    plan: Mapping[str, Any],
    *,
    account_address: str,
    account: object,
    reconciliation: object,
    market: Mapping[str, Any],
) -> PortfolioSnapshot:
    session_id = str(plan.get("strategy_session_id") or "").strip()
    if not session_id:
        raise TestnetAutomationProofError("strategy_session_id_required")
    equity = _decimal(getattr(account, "equity", None), "account_equity", positive=True)
    exposure = _decimal(getattr(account, "exposure", None) or 0, "account_exposure")
    margin_used = _decimal(getattr(account, "margin_used", None) or 0, "account_margin_used")
    observed_at = getattr(reconciliation, "observed_at", None)
    if not isinstance(observed_at, datetime) or observed_at.tzinfo is None:
        raise TestnetAutomationProofError("account_observation_invalid")
    observation_age = (
        datetime.now(timezone.utc) - observed_at.astimezone(timezone.utc)
    ).total_seconds()
    coherent = getattr(reconciliation, "passed", False) is True
    fresh = 0 <= observation_age <= 120
    if not coherent or not fresh:
        raise TestnetAutomationProofError("account_snapshot_not_ready")
    provenance = getattr(account, "provenance", None)
    source = str(getattr(provenance, "source", "") or "").strip().lower()
    cursor = getattr(getattr(reconciliation, "cursor", None), "value", "")
    return PortfolioSnapshot(
        snapshot_id=f"snapshot-{str(getattr(reconciliation, 'evidence_digest', '') or session_id)[-32:]}",
        portfolio_session_id=f"portfolio-{session_id}",
        account_id=account_address,
        observed_at=observed_at.isoformat(),
        equity=equity,
        available_cash=_decimal(
            getattr(account, "withdrawable", None)
            or getattr(account, "balance", None)
            or equity,
            "account_available_cash",
        ),
        total_exposure=exposure,
        margin_used=margin_used,
        leverage=(exposure / equity if equity > 0 else Decimal("0")),
        positions=tuple(
            _position_row(item, account_address=account_address, portfolio_session_id=f"portfolio-{session_id}")
            for item in _fact_data(reconciliation, "positions")
        ),
        open_orders=tuple(
            _open_order_row(item, account_address=account_address, portfolio_session_id=f"portfolio-{session_id}")
            for item in _fact_data(reconciliation, "open_orders")
        ),
        coherent=coherent,
        fresh=fresh,
        provenance={
            "broker_id": "hyperliquid",
            "environment": "testnet",
            "account_fingerprint": _fingerprint(account_address),
            "cursor": str(cursor or market["cursor"]),
            "mapping_revision": str(market["mapping_revision"]),
            "universe_revision": str(market["universe_revision"]),
            "connection_epoch": str(market["connection_epoch"]),
            "source": source,
            "evidence_digest": str(getattr(reconciliation, "evidence_digest", "") or ""),
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
                if isinstance(row, Mapping)
                and row.get("event") == "confirmed"
                and str(row.get("plan_digest") or "")
                == str(plan.get("plan_digest") or "")
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
        value.get("event") != "confirmed"
        or value.get("execution_authorized") is not True
        or str(value.get("execution_environment") or "").lower() != "testnet"
        or str(value.get("plan_digest") or "") != str(plan.get("plan_digest") or "")
        or not str(value.get("confirmation_id") or "").strip()
        or value.get("confirmed_at") in (None, "")
        or not str(value.get("proposal_id") or "").strip()
        or not str(value.get("receipt_digest") or "").strip()
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
    _verify_durable_confirmation(
        Path(args.output_root),
        plan=plan,
        confirmation=confirmation,
    )
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
    if current.get("status") != "activated":
        raise TestnetAutomationProofError(
            "activation_state_blocked",
            result={"status": current.get("status"), "blocker": current.get("blocker")},
        )
    if str(confirmation.get("activation_id") or "") != str(current.get("activation_id") or ""):
        raise TestnetAutomationProofError("confirmation_activation_mismatch")
    broker = build_broker_execution_port(_context(args, family=family))
    try:
        market = _authoritative_market(
            broker,
            market=market,
            instrument_id=args.instrument_id,
        )
        preflight = coordinator.preflight(
            broker=broker,
            strategy_family=family,
            market=market,
        )
        if preflight.get("ready") is not True:
            raise TestnetAutomationProofError(
                "broker_preflight_blocked",
                result={
                    "blocker": preflight.get("execution_blocker")
                    or preflight.get("capability_gaps")
                    or preflight.get("blocker"),
                    "preflight_status": preflight.get("broker_preflight"),
                },
            )
        account, reconciliation = _authoritative_account_snapshot(
            broker,
            plan=plan,
            account_address=args.account_address,
        )
        snapshot = _snapshot(
            plan,
            account_address=args.account_address,
            account=account,
            reconciliation=reconciliation,
            market=market,
        )
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
        lifecycle_status = str(result.get("lifecycle", {}).get("status") or "")
        if str(result.get("status") or "") in {"dca_blocked", "grid_blocked"} or lifecycle_status.startswith("blocked"):
            raise TestnetAutomationProofError(
                "lifecycle_blocked",
                result={
                    "blocker": result.get("blocker") or result.get("lifecycle", {}).get("blocker"),
                    "lifecycle_status": lifecycle_status,
                },
            )
        return {
            "action": "start",
            "strategy_family": family,
            "status": result.get("status"),
            "next_action": result.get("next_action"),
            "execution_slice_id": result.get("selected_execution_slice_id"),
            "lifecycle_status": lifecycle_status,
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
    if args.capability_revision != PROTECTED_CAPABILITY_REVISION:
        raise TestnetAutomationProofError("protected_capability_revision_required")
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
