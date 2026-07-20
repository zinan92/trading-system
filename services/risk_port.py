"""Canonical pre-execution risk contracts and request composition helpers.

Nautilus still supplies its mature order-level RiskEngine (precision, order
limits, reduce-only and trading-state checks).  This port sits one level above
execution engines: it owns StrategyPlan/portfolio policy and emits the same
decision contract no matter which engine later executes the commands.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from schemas.accounting import ACCOUNTING_SNAPSHOT_SCHEMA, build_accounting_snapshot
from schemas.risk import (
    RiskDecision,
    RiskRequest,
    build_risk_request,
    validate_risk_decision,
    validate_risk_request,
)
from services.accounting_projection_core import AccountingContractError, project_execution_accounting
from services.risk_policy_core import (
    CLOSE_EVENTS,
    OPEN_ORDER_STATES,
    blocked_message as _blocked_message,
    digest as _digest,
    finite_positive as _finite_positive,
)


@runtime_checkable
class RiskDecisionPort(Protocol):
    name: str

    def evaluator_metadata(self) -> Mapping[str, Any]:
        ...

    def resolve_policy(self, config: Mapping[str, Any]) -> Mapping[str, Any]:
        ...

    def evaluate(self, request: RiskRequest | Mapping[str, Any]) -> RiskDecision:
        ...


@runtime_checkable
class RiskDecisionStorePort(Protocol):
    name: str

    def persist(
        self,
        decision: RiskDecision | Mapping[str, Any],
    ) -> dict[str, Any]:
        ...


def build_grid_risk_request(
    *,
    checked_at: str,
    action_class: str,
    intent: str,
    plan: Mapping[str, Any],
    commands: list[Mapping[str, Any]],
    account_context: Mapping[str, Any],
    market: Mapping[str, Any],
    execution_snapshot: Mapping[str, Any],
    execution_reconciliation: Mapping[str, Any],
    policy: Mapping[str, Any],
    evaluator: Mapping[str, Any],
    replaced_order_ids: list[str] | None = None,
) -> RiskRequest:
    candidate = _grid_candidate(
        plan,
        commands,
        intent=intent,
        replaced_order_ids=replaced_order_ids or [],
    )
    return build_risk_request(
        checked_at=checked_at,
        scope="paper_grid",
        action_class=action_class,
        candidate=candidate,
        account=canonical_account_risk_state(account_context),
        market=canonical_market_risk_state(market),
        execution=canonical_execution_risk_state(execution_snapshot, execution_reconciliation),
        policy=policy,
        evaluator=evaluator,
    )


def normalize_manual_order_command(
    command: Mapping[str, Any],
    *,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve the exact linear paper quantity before risk and execution."""

    from services.dualtrack_execution_contract import normalize_execution_command

    normalized = normalize_execution_command(dict(command), dict(config or {}))
    if action_class_for_command(normalized) != "increase_exposure":
        return normalized
    price = _finite_positive(normalized.get("price") or normalized.get("market_price"))
    quantity = _finite_positive(normalized.get("quantity") or normalized.get("contracts"))
    notional = _finite_positive(normalized.get("notional"))
    if price is not None and quantity is None and notional is not None:
        normalized["quantity"] = notional / price
    elif price is not None and notional is None and quantity is not None:
        normalized["notional"] = price * quantity
    return normalized


def build_manual_order_risk_request(
    *,
    checked_at: str,
    command: Mapping[str, Any],
    account_context: Mapping[str, Any],
    market: Mapping[str, Any],
    execution_snapshot: Mapping[str, Any],
    execution_reconciliation: Mapping[str, Any],
    config: Mapping[str, Any],
    policy: Mapping[str, Any],
    evaluator: Mapping[str, Any],
) -> RiskRequest:
    action_class = action_class_for_command(command)
    if action_class in {"reduce_only", "cancel"}:
        candidate = {
            "kind": "manual_order",
            "intent": "manual_safe_action",
            "cycle_id": str(command.get("cycle_id") or ""),
            "event": str(command.get("event") or "").lower(),
            "trade_id": str(command.get("trade_id") or ""),
            "position_id": str(command.get("position_id") or ""),
        }
    else:
        exact = normalize_manual_order_command(command, config=config)
        risk_command = _normalized_risk_command(exact)
        candidate = {
            "kind": "manual_order",
            "intent": "manual_entry",
            "cycle_id": str(exact.get("cycle_id") or ""),
            "strategy_plan_id": str(exact.get("strategy_plan_id") or ""),
            "strategy_plan_version": exact.get("strategy_plan_version"),
            "direction": str(exact.get("side") or "").lower(),
            "notional_per_grid": risk_command.get("notional"),
            "leverage": _finite_positive(exact.get("leverage")) or _finite_positive(config.get("max_leverage")),
            "commands": [risk_command],
            "replaced_order_ids": [],
        }
    return build_risk_request(
        checked_at=checked_at,
        scope="paper_manual_order",
        action_class=action_class,
        candidate=candidate,
        account=canonical_account_risk_state(account_context),
        market=canonical_market_risk_state(market),
        execution=canonical_execution_risk_state(execution_snapshot, execution_reconciliation),
        policy=policy,
        evaluator=evaluator,
    )


def assert_matching_risk_decision(
    port: RiskDecisionPort,
    prior: RiskDecision | Mapping[str, Any],
    current_request: RiskRequest | Mapping[str, Any],
) -> RiskDecision:
    """Re-evaluate current facts; a stored/prior allow is never trusted alone."""

    prior_decision = validate_risk_decision(prior)
    request = validate_risk_request(current_request)
    if prior_decision.request_id != request.request_id:
        raise ValueError("risk decision stale: current request binding changed")
    current = port.evaluate(request)
    if current.decision_id != prior_decision.decision_id:
        raise ValueError("risk decision stale: current evaluation changed")
    return require_risk_permission(current)


def require_exposure_permission(decision: RiskDecision | Mapping[str, Any]) -> RiskDecision:
    current = validate_risk_decision(decision)
    if not current.allow_exposure_increase:
        raise ValueError(_blocked_message(current))
    return current


def require_risk_permission(decision: RiskDecision | Mapping[str, Any]) -> RiskDecision:
    current = validate_risk_decision(decision)
    allowed = {
        "increase_exposure": current.allow_exposure_increase,
        "replace_pending": current.allow_exposure_increase,
        "reduce_only": current.allow_reduce_only,
        "cancel": current.allow_cancel,
    }.get(current.action_class, False)
    if not allowed:
        raise ValueError(_blocked_message(current))
    return current


def canonical_live_risk_allows_exposure(result: Mapping[str, Any] | None) -> bool:
    if not isinstance(result, Mapping):
        return False
    try:
        decision = validate_risk_decision(result.get("risk_decision") or {})
    except (TypeError, ValueError):
        return False
    return decision.allow_exposure_increase


def canonical_account_risk_state(context: Mapping[str, Any]) -> dict[str, Any]:
    source = context.get("accounting_snapshot") if isinstance(context.get("accounting_snapshot"), Mapping) else context
    if not isinstance(source, Mapping) or source.get("schema_version") != ACCOUNTING_SNAPSHOT_SCHEMA:
        return {"status": "unknown", "reason": "accounting_snapshot_missing", "equity": None}
    try:
        rebuilt = build_accounting_snapshot(
            source_type=source.get("source_type"),
            source_name=source.get("source_name"),
            source_schema_version=source.get("source_schema_version"),
            scope=source.get("scope") or {},
            currency=source.get("currency"),
            orders=source.get("orders") or [],
            fills=source.get("fills") or [],
            positions=source.get("positions") or [],
            trades=source.get("trades") or [],
            counts=source.get("counts") or {},
            pnl=source.get("pnl") or {},
            account=source.get("account") or {},
            completeness=source.get("completeness") or {},
            reconciliation=source.get("reconciliation") or {},
        ).to_dict()
    except (TypeError, ValueError) as exc:
        return {"status": "unknown", "reason": f"accounting_snapshot_invalid:{type(exc).__name__}", "equity": None}
    if str(source.get("snapshot_id") or "") != rebuilt["snapshot_id"]:
        return {"status": "drift", "reason": "accounting_snapshot_identity_mismatch", "equity": None}
    account = rebuilt.get("account") if isinstance(rebuilt.get("account"), dict) else {}
    equity = _finite_positive(account.get("equity"))
    reconciliation = rebuilt.get("reconciliation") if isinstance(rebuilt.get("reconciliation"), dict) else {}
    return {
        "status": "ready" if equity is not None else "unknown",
        "reason": "" if equity is not None else "account_equity_missing_or_non_positive",
        "snapshot_id": rebuilt["snapshot_id"],
        "source_name": rebuilt.get("source_name"),
        "equity": equity,
        "ending_cash": account.get("ending_cash"),
        "available_balance": account.get("available_balance"),
        "reconciliation_status": reconciliation.get("status"),
        "reconciliation_issues": reconciliation.get("issues") or [],
        "completeness_status": (rebuilt.get("completeness") or {}).get("status"),
    }


def canonical_market_risk_state(market: Mapping[str, Any]) -> dict[str, Any]:
    trust = market.get("trust") if isinstance(market.get("trust"), Mapping) else {}
    envelope = market.get("market_data_envelope") if isinstance(market.get("market_data_envelope"), Mapping) else {}
    return {
        "status": str(market.get("status") or ""),
        "fresh": market.get("fresh") is True,
        "is_synthetic": market.get("is_synthetic"),
        "provider": str(market.get("provider") or ""),
        "source_mode": str(market.get("source_mode") or ""),
        "symbol": str(market.get("symbol") or ""),
        "timeframe": str(market.get("timeframe") or ""),
        "price": _finite_positive(market.get("latest_close", market.get("price"))),
        "latest_timestamp": str(market.get("latest_timestamp") or market.get("timestamp") or ""),
        "batch_id": str(envelope.get("batch_id") or market.get("batch_id") or ""),
        "trust_status": str(trust.get("status") or ""),
    }


def canonical_execution_risk_state(
    snapshot: Mapping[str, Any],
    reconciliation: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        accounting = project_execution_accounting(snapshot).to_dict()
    except (AccountingContractError, TypeError, ValueError) as exc:
        return {
            "status": "unknown",
            "reason": f"execution_accounting_invalid:{type(exc).__name__}",
            "open_positions": [],
            "open_entry_orders": [],
        }
    raw_positions: dict[str, Mapping[str, Any]] = {}
    conflicts: list[str] = []
    for row in snapshot.get("positions") or []:
        if not isinstance(row, Mapping):
            continue
        keys = {str(row.get("trade_id") or ""), str(row.get("position_id") or "")} - {""}
        for key in keys:
            if key in raw_positions and raw_positions[key] != row:
                conflicts.append(key)
            raw_positions[key] = row
    positions = []
    for row in accounting.get("positions") or []:
        if str(row.get("status") or "") != "open":
            continue
        raw = raw_positions.get(str(row.get("trade_id") or "")) or raw_positions.get(str(row.get("position_id") or "")) or {}
        positions.append({
            "position_id": row.get("position_id"),
            "trade_id": row.get("trade_id"),
            "side": row.get("side"),
            "remaining_quantity": row.get("remaining_quantity"),
            "entry_price": row.get("entry_price"),
            "sl": raw.get("sl"),
            "tp": raw.get("tp"),
            "strategy_plan_id": row.get("strategy_plan_id"),
            "strategy_plan_version": row.get("strategy_plan_version"),
        })
    orders = [
        row
        for row in accounting.get("orders") or []
        if str(row.get("state") or "") in OPEN_ORDER_STATES and str(row.get("event") or "entry") == "entry"
    ]
    accounting_reconciliation = accounting.get("reconciliation") if isinstance(accounting.get("reconciliation"), dict) else {}
    engine_status = str(reconciliation.get("status") or "")
    issues = list(accounting_reconciliation.get("issues") or []) + list(reconciliation.get("issues") or [])
    if conflicts:
        issues.append({"code": "execution_position_identity_conflict", "identities": sorted(set(conflicts))})
    body = {
        "status": "ready" if not conflicts else "drift",
        "reason": "" if not conflicts else "execution_position_identity_conflict",
        "source_engine": accounting.get("source_name"),
        "accounting_snapshot_id": accounting.get("snapshot_id"),
        "accounting_reconciliation_status": accounting_reconciliation.get("status"),
        "engine_reconciliation_status": engine_status,
        "reconciliation_issues": issues,
        "open_positions": positions,
        "open_entry_orders": orders,
    }
    body["state_id"] = f"execution-risk-{_digest(body)}"
    return body


def action_class_for_command(command: Mapping[str, Any]) -> str:
    """Derive economic intent server-side; caller source never grants a bypass."""

    event = str(command.get("event") or "entry").strip().lower()
    if event == "cancel":
        return "cancel"
    if event in CLOSE_EVENTS:
        return "reduce_only"
    return "increase_exposure"


def _normalized_risk_command(command: Mapping[str, Any]) -> dict[str, Any]:
    identity_payload = {
        "cycle_id": str(command.get("cycle_id") or ""),
        "strategy_plan_id": str(command.get("strategy_plan_id") or ""),
        "strategy_plan_version": command.get("strategy_plan_version"),
        "side": str(command.get("side") or "").lower(),
        "event": str(command.get("event") or "entry").lower(),
        "order_type": str(command.get("order_type") or "").lower(),
        "price": _finite_positive(command.get("price") or command.get("market_price")),
        "quantity": _finite_positive(command.get("quantity") or command.get("contracts")),
        "notional": _finite_positive(command.get("notional")),
        "sl": _finite_positive(command.get("sl")),
        "tp": _finite_positive(command.get("tp")),
        "ts": str(command.get("ts") or ""),
        "symbol": str(command.get("symbol") or ""),
        "source": str(command.get("source") or ""),
    }
    identity = str(command.get("source_fill_id") or command.get("command_id") or "")
    return {
        "command_id": identity or f"manual-command-{_digest(identity_payload)}",
        **identity_payload,
    }


def _grid_candidate(
    plan: Mapping[str, Any],
    commands: list[Mapping[str, Any]],
    *,
    intent: str,
    replaced_order_ids: list[str],
) -> dict[str, Any]:
    grid = plan.get("grid") if isinstance(plan.get("grid"), Mapping) else {}
    price_range = plan.get("range") if isinstance(plan.get("range"), Mapping) else {}
    normalized_commands = []
    for command in commands:
        normalized = _normalized_risk_command(command)
        normalized["command_id"] = str(command.get("source_fill_id") or command.get("command_id") or "")
        normalized_commands.append(normalized)
    normalized_commands.sort(key=lambda row: (row["command_id"], row["side"], row["price"] or 0.0))
    return {
        "kind": "strategy_plan_grid",
        "intent": str(intent or ""),
        "cycle_id": str(plan.get("cycle_id") or ""),
        "strategy_plan_id": str(plan.get("strategy_plan_id") or ""),
        "strategy_plan_version": plan.get("version"),
        "preview_id": str(plan.get("preview_id") or ""),
        "direction": str(plan.get("direction") or ""),
        "range_low": _finite_positive(price_range.get("low")),
        "range_high": _finite_positive(price_range.get("high")),
        "grid_mode": str(grid.get("mode") or ""),
        "grid_count": grid.get("count"),
        "notional_per_grid": _finite_positive(grid.get("notional_per_grid")),
        "leverage": _finite_positive(grid.get("leverage")),
        "commands": normalized_commands,
        "replaced_order_ids": sorted({str(value) for value in replaced_order_ids if str(value)}),
    }
