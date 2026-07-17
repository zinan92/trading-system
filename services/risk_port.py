"""Canonical pre-execution risk port and paper-grid adapter.

Nautilus still supplies its mature order-level RiskEngine (precision, order
limits, reduce-only and trading-state checks).  This port sits one level above
execution engines: it owns StrategyPlan/portfolio policy and emits the same
decision contract no matter which engine later executes the commands.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from schemas.accounting import ACCOUNTING_SNAPSHOT_SCHEMA, build_accounting_snapshot
from schemas.risk import (
    RiskDecision,
    RiskRequest,
    build_risk_decision,
    build_risk_request,
    validate_risk_decision,
    validate_risk_request,
)
from services.accounting_projection import AccountingContractError, project_execution_accounting
from services.journal_store import load_json, write_json


GRID_RISK_POLICY_SCHEMA = "strategy-grid-risk-policy-v1"
GRID_RISK_EVALUATOR_VERSION = "paper-grid-risk-v1"
LIVE_MONEY_RISK_EVALUATOR_VERSION = "live-money-risk-bridge-v1"
OPEN_ORDER_STATES = frozenset({"accepted", "new", "open", "pending", "submitted", "working", "partially_filled"})
EXPOSURE_ACTIONS = frozenset({"increase_exposure", "replace_pending"})
CLOSE_EVENTS = frozenset({"exit", "stop", "target", "flatten"})


@runtime_checkable
class RiskDecisionPort(Protocol):
    name: str

    def evaluate(self, request: RiskRequest | Mapping[str, Any]) -> RiskDecision:
        ...


class PaperGridRiskDecisionPort:
    """Pure policy adapter for one grid batch plus current portfolio facts."""

    name = "paper_grid_risk"

    def evaluate(self, request: RiskRequest | Mapping[str, Any]) -> RiskDecision:
        current = validate_risk_request(request)
        payload = current.to_dict()
        action = str(payload["action_class"])
        if action == "cancel":
            blockers = _safe_action_identity_blockers(payload, expected="cancel")
            return build_risk_decision(current, blockers=blockers)
        if action == "reduce_only":
            blockers = _safe_action_identity_blockers(payload, expected="reduce_only")
            return build_risk_decision(current, blockers=blockers)
        if action not in EXPOSURE_ACTIONS:
            raise ValueError(f"paper grid risk does not support {action}")
        return self._evaluate_exposure(current)

    def _evaluate_exposure(self, request: RiskRequest) -> RiskDecision:
        payload = request.to_dict()
        account = dict(payload["account"])
        market = dict(payload["market"])
        execution = dict(payload["execution"])
        candidate = dict(payload["candidate"])
        policy = dict(payload["policy"])
        blockers: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []

        equity = _finite_positive(account.get("equity"))
        if account.get("status") != "ready" or equity is None:
            blockers.append(_blocker(
                "account_snapshot_unavailable",
                "canonical_account",
                "canonical account equity is unavailable or untrusted",
                {"status": account.get("status"), "reason": account.get("reason")},
            ))
        elif account.get("reconciliation_status") != "pass":
            blockers.append(_blocker(
                "account_reconciliation_drift",
                "canonical_account.reconciliation",
                "canonical accounting reconciliation is not pass",
                {"status": account.get("reconciliation_status"), "issues": account.get("reconciliation_issues", [])},
            ))

        price = _finite_positive(market.get("price"))
        if (
            market.get("status") not in {"ready", "derived"}
            or market.get("fresh") is not True
            or market.get("is_synthetic") is not False
            or not str(market.get("provider") or "").strip()
            or price is None
        ):
            blockers.append(_blocker(
                "market_untrusted",
                "trusted_market",
                "market state is stale, synthetic, missing, or untrusted",
                {
                    "status": market.get("status"),
                    "fresh": market.get("fresh"),
                    "is_synthetic": market.get("is_synthetic"),
                    "provider": market.get("provider"),
                    "price": market.get("price"),
                },
            ))

        if execution.get("status") != "ready":
            blockers.append(_blocker(
                "execution_state_unavailable",
                "canonical_execution",
                "current execution state cannot be normalized safely",
                {"status": execution.get("status"), "reason": execution.get("reason")},
            ))
        elif (
            execution.get("accounting_reconciliation_status") != "pass"
            or execution.get("engine_reconciliation_status") != "ok"
        ):
            blockers.append(_blocker(
                "execution_reconciliation_drift",
                "canonical_execution.reconciliation",
                "execution reconciliation is not clean",
                {
                    "accounting_status": execution.get("accounting_reconciliation_status"),
                    "engine_status": execution.get("engine_reconciliation_status"),
                    "issues": execution.get("reconciliation_issues", []),
                },
            ))

        candidate_kind = str(candidate.get("kind") or "")
        if candidate_kind == "strategy_plan_grid":
            low = _finite_positive(candidate.get("range_low"))
            high = _finite_positive(candidate.get("range_high"))
            if low is None or high is None or high <= low:
                blockers.append(_blocker(
                    "candidate_range_invalid",
                    "strategy_plan.range",
                    "candidate range is invalid",
                    {"low": candidate.get("range_low"), "high": candidate.get("range_high")},
                ))
            elif price is not None and not (low <= price <= high):
                blockers.append(_blocker(
                    "market_price_outside_range",
                    "strategy_plan.range",
                    "current market price is outside the candidate range",
                    {"price": price, "low": low, "high": high},
                ))
        elif candidate_kind != "manual_order":
            blockers.append(_blocker(
                "candidate_kind_invalid",
                "risk_request.candidate",
                "exposure-increasing candidate kind is unsupported",
                {"kind": candidate_kind},
            ))

        commands = candidate.get("commands") if isinstance(candidate.get("commands"), list) else []
        candidate_economics = _command_economics(commands)
        blockers.extend(candidate_economics["blockers"])
        candidate_by_side = candidate_economics["notional_by_side"]
        candidate_loss_by_side = candidate_economics["loss_by_side"]
        if not commands:
            blockers.append(_blocker(
                "candidate_commands_missing",
                "strategy_plan.commands",
                "candidate has no exposure-increasing commands",
                {},
            ))

        positions = execution.get("open_positions") if isinstance(execution.get("open_positions"), list) else []
        existing = _position_economics(positions, mark_price=price)
        blockers.extend(existing["blockers"])
        warnings.extend(existing["warnings"])

        accepted = execution.get("open_entry_orders") if isinstance(execution.get("open_entry_orders"), list) else []
        accepted_ids = {str(row.get("order_id") or "") for row in accepted if str(row.get("order_id") or "")}
        replaced_ids = {str(value) for value in candidate.get("replaced_order_ids") or [] if str(value)}
        if payload["action_class"] == "increase_exposure":
            if accepted_ids:
                blockers.append(_blocker(
                    "existing_entry_orders_present",
                    "canonical_execution.open_orders",
                    "new grid start cannot layer over existing pending entries",
                    {"order_ids": sorted(accepted_ids)},
                ))
            if positions and candidate.get("intent") == "start_grid":
                blockers.append(_blocker(
                    "existing_positions_require_regrid",
                    "canonical_execution.open_positions",
                    "new grid start cannot layer over existing positions; use running regrid or close first",
                    {"position_ids": sorted(str(row.get("position_id") or row.get("trade_id") or "") for row in positions)},
                ))
        else:
            unmanaged = sorted(accepted_ids - replaced_ids)
            missing = sorted(replaced_ids - accepted_ids)
            if unmanaged or missing:
                blockers.append(_blocker(
                    "replacement_order_set_mismatch",
                    "canonical_execution.open_orders",
                    "regrid replacement set does not exactly match current pending entries",
                    {"unmanaged_order_ids": unmanaged, "missing_order_ids": missing},
                ))

        requested_leverage = _finite_positive(candidate.get("leverage"))
        max_leverage = _finite_positive(policy.get("max_leverage"))
        max_loss_pct = _fraction(policy.get("max_plan_loss_pct"))
        utilization = _fraction(policy.get("margin_utilization_cap"))
        invalid_policy_fields = [
            field
            for field, value in (
                ("max_leverage", max_leverage),
                ("max_plan_loss_pct", max_loss_pct),
                ("margin_utilization_cap", utilization),
            )
            if value is None
        ]
        if invalid_policy_fields:
            blockers.append(_blocker(
                "risk_policy_invalid",
                "grid_risk_policy",
                "resolved grid risk policy is missing valid limits",
                {"fields": invalid_policy_fields},
            ))
        if requested_leverage is None:
            blockers.append(_blocker(
                "candidate_leverage_invalid",
                "strategy_plan.grid.leverage",
                "candidate leverage is missing or invalid",
                {"value": candidate.get("leverage")},
            ))
        elif max_leverage is not None and requested_leverage > max_leverage + 1e-12:
            blockers.append(_blocker(
                "leverage_limit_exceeded",
                "grid_risk_policy.max_leverage",
                "requested leverage exceeds policy limit",
                {"requested": requested_leverage, "limit": max_leverage},
            ))

        existing_notional = existing["notional_by_side"]
        existing_loss = existing["loss_by_side"]
        projected_by_side = {
            side: existing_notional[side] + candidate_by_side[side]
            for side in ("buy", "sell")
        }
        projected_loss_by_side = {
            side: existing_loss[side] + candidate_loss_by_side[side]
            for side in ("buy", "sell")
        }
        projected_notional = max(projected_by_side.values())
        projected_loss = max(projected_loss_by_side.values())
        risk_budget = equity * max_loss_pct if equity is not None and max_loss_pct is not None else None
        margin_budget = equity * utilization if equity is not None and utilization is not None else None
        actual_leverage = projected_notional / equity if equity is not None else None
        estimated_margin = projected_notional / requested_leverage if requested_leverage is not None else None

        if risk_budget is not None and projected_loss > risk_budget + 1e-8:
            blockers.append(_blocker(
                "plan_loss_budget_exceeded",
                "grid_risk_policy.max_plan_loss",
                "candidate maximum loss exceeds the configured plan-loss budget",
                {"projected_max_loss": round(projected_loss, 8), "max_loss_budget": round(risk_budget, 8)},
            ))
        if actual_leverage is not None and max_leverage is not None and actual_leverage > max_leverage + 1e-8:
            blockers.append(_blocker(
                "projected_leverage_exceeded",
                "grid_risk_policy.max_leverage",
                "projected position leverage exceeds policy limit",
                {"projected_leverage": round(actual_leverage, 8), "limit": max_leverage},
            ))
        if estimated_margin is not None and margin_budget is not None and estimated_margin > margin_budget + 1e-8:
            blockers.append(_blocker(
                "projected_margin_exceeded",
                "grid_risk_policy.margin_utilization",
                "projected margin exceeds the configured account budget",
                {"estimated_margin": round(estimated_margin, 8), "margin_budget": round(margin_budget, 8)},
            ))

        recommendation = _notional_recommendation(
            candidate,
            candidate_loss_by_side=candidate_loss_by_side,
            existing_loss_by_side=existing_loss,
            risk_budget=risk_budget,
        )
        metrics = {
            "calculation_source": "exact_commands_plus_canonical_accounting",
            "preview_risk_fields_used": False,
            "equity": _rounded(equity),
            "candidate_notional_by_side": _rounded_mapping(candidate_by_side),
            "existing_notional_by_side": _rounded_mapping(existing_notional),
            "projected_notional_by_side": _rounded_mapping(projected_by_side),
            "projected_max_side_notional": _rounded(projected_notional),
            "candidate_loss_by_side": _rounded_mapping(candidate_loss_by_side),
            "existing_stop_loss_by_side": _rounded_mapping(existing_loss),
            "projected_loss_by_side": _rounded_mapping(projected_loss_by_side),
            "projected_max_loss": _rounded(projected_loss),
            "projected_actual_leverage": _rounded(actual_leverage),
            "projected_margin": _rounded(estimated_margin),
            "open_position_count": len(positions),
            "open_entry_order_count": len(accepted),
        }
        limits = {
            "max_plan_loss_pct": max_loss_pct,
            "max_plan_loss": _rounded(risk_budget),
            "max_leverage": max_leverage,
            "margin_utilization_cap": utilization,
            "margin_budget": _rounded(margin_budget),
            "market_must_be_inside_range": True,
            "existing_position_stop_required": True,
        }
        return build_risk_decision(
            request,
            blockers=blockers,
            warnings=warnings,
            metrics=metrics,
            limits=limits,
            recommendation=recommendation,
        )


class RiskDecisionStore:
    """Append-only audit adapter; never read by an authorization path."""

    def __init__(self, output_root: Path) -> None:
        self.output_root = Path(output_root)

    def persist(self, decision: RiskDecision | Mapping[str, Any]) -> dict[str, Any]:
        current = validate_risk_decision(decision).to_dict()
        root = self._root(str(current.get("scope") or ""))
        candidate = current.get("request", {}).get("candidate", {})
        identity = _safe_filename(str(candidate.get("cycle_id") or candidate.get("run_date") or "unscoped"))
        history_path = root / f"{identity}.json"
        rows = [row for row in load_json(history_path) if isinstance(row, dict)]
        if not any(str(row.get("decision_id") or "") == current["decision_id"] for row in rows):
            rows.append(current)
            write_json(history_path, rows)
        write_json(root / "current.json", [current])
        return current

    def _root(self, scope: str) -> Path:
        if scope.startswith("paper_"):
            return self.output_root / "dualtrack" / "risk_decisions"
        return self.output_root / "risk_decisions"


class LiveMoneyRiskDecisionAdapter:
    """Bridge the mature venue guardrails into the canonical risk contract.

    The legacy guardrail remains the policy authority.  This adapter cannot
    make its answer looser: any legacy blocker, malformed result, or explicit
    denial becomes a canonical blocker in the same order.
    """

    name = "live_money_risk_bridge"

    def __init__(
        self,
        output_root: Path,
        *,
        broker_config: Mapping[str, Any] | None = None,
        legacy_guardrails=None,
        store: RiskDecisionStore | None = None,
    ) -> None:
        self.output_root = Path(output_root)
        self.broker_config = dict(broker_config or {})
        if legacy_guardrails is None:
            from services.live_money_guardrails import LiveMoneyGuardrails

            legacy_guardrails = LiveMoneyGuardrails(self.output_root, broker_config=self.broker_config)
        self.legacy_guardrails = legacy_guardrails
        self.store = store or RiskDecisionStore(self.output_root)

    def evaluate_order(
        self,
        run_date: str,
        *,
        ticket: Mapping[str, Any],
        symbol: str,
        side: str,
        requested_price: float,
        quantity: float,
        source: str,
        reconciliation: Mapping[str, Any] | None = None,
        action_class: str = "increase_exposure",
        checked_at: str | None = None,
    ) -> dict[str, Any]:
        resolved_action = str(action_class or "").strip().lower()
        timestamp = checked_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        if resolved_action != "increase_exposure":
            return self._safe_action(
                run_date,
                ticket=ticket,
                symbol=symbol,
                source=source,
                action_class=resolved_action,
                checked_at=timestamp,
            )

        legacy = self.legacy_guardrails.evaluate_order(
            run_date,
            ticket=dict(ticket),
            symbol=symbol,
            side=side,
            requested_price=requested_price,
            quantity=quantity,
            source=source,
            reconciliation=dict(reconciliation or {}),
        )
        return self.record_legacy_result(
            run_date,
            ticket=ticket,
            symbol=symbol,
            side=side,
            requested_price=requested_price,
            quantity=quantity,
            source=source,
            reconciliation=reconciliation,
            legacy=legacy,
            checked_at=timestamp,
        )

    def record_legacy_result(
        self,
        run_date: str,
        *,
        ticket: Mapping[str, Any],
        symbol: str,
        side: str,
        requested_price: float,
        quantity: float,
        source: str,
        reconciliation: Mapping[str, Any] | None,
        legacy: Mapping[str, Any] | None,
        checked_at: str | None = None,
    ) -> dict[str, Any]:
        result = dict(legacy) if isinstance(legacy, Mapping) else {}
        timestamp = str(
            result.get("checked_at")
            or checked_at
            or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        )
        request = _live_money_risk_request(
            checked_at=timestamp,
            run_date=run_date,
            ticket=ticket,
            symbol=symbol,
            side=side,
            requested_price=requested_price,
            quantity=quantity,
            source=source,
            reconciliation=reconciliation or {},
            legacy=result,
            action_class="increase_exposure",
        )
        blockers = _canonical_live_blockers(result)
        decision = build_risk_decision(
            request,
            blockers=blockers,
            metrics={
                "legacy_status": str(result.get("status") or ""),
                "legacy_allows_new_order": result.get("allows_new_order") is True,
                "candidate_notional": (result.get("candidate") or {}).get("notional"),
                "projected_total_notional": (result.get("exposure") or {}).get("projected_total_notional"),
                "daily_loss_pct": (result.get("daily_loss") or {}).get("loss_pct"),
            },
            limits=dict(result.get("limits") or {}),
        )
        persisted = self.store.persist(decision)
        return {**result, "risk_decision": persisted}

    def _safe_action(
        self,
        run_date: str,
        *,
        ticket: Mapping[str, Any],
        symbol: str,
        source: str,
        action_class: str,
        checked_at: str,
    ) -> dict[str, Any]:
        candidate = {
            "kind": "venue_safe_action",
            "run_date": str(run_date),
            "cycle_id": str(ticket.get("cycle_id") or run_date),
            "event": str(ticket.get("event") or "").lower(),
            "trade_id": str(ticket.get("trade_id") or ""),
            "position_id": str(ticket.get("position_id") or ""),
            "symbol": str(symbol),
            "source": str(source),
        }
        request = build_risk_request(
            checked_at=checked_at,
            scope="venue_safe_action",
            action_class=action_class,
            candidate=candidate,
            account={"status": "not_required"},
            market={"status": "not_required"},
            execution={"status": "not_required"},
            policy={"policy_id": "safe-action-identity-v1"},
            evaluator=live_money_risk_evaluator(),
        )
        decision = PaperGridRiskDecisionPort().evaluate(request)
        persisted = self.store.persist(decision)
        return {
            "run_date": run_date,
            "status": "SAFE_ACTION_ALLOWED" if decision.to_dict()["outcome"] == "allow" else "SAFE_ACTION_BLOCKED",
            "allows_new_order": False,
            "blockers": decision.to_dict()["blockers"],
            "primary_blocker": decision.to_dict()["primary_blocker"],
            "risk_decision": persisted,
        }


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
    config: Mapping[str, Any],
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
        policy=grid_risk_policy(config),
        evaluator=grid_risk_evaluator(),
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
        policy=grid_risk_policy(config),
        evaluator=grid_risk_evaluator(),
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


def grid_risk_policy(config: Mapping[str, Any]) -> dict[str, Any]:
    strategy = config.get("strategy_grid") if isinstance(config.get("strategy_grid"), Mapping) else {}
    body = {
        "schema_version": GRID_RISK_POLICY_SCHEMA,
        "max_leverage": _finite_positive(config.get("max_leverage")),
        "max_plan_loss_pct": _fraction(strategy.get("max_plan_loss_pct")),
        "margin_utilization_cap": _fraction(strategy.get("capital_utilization_cap")),
        "market_must_be_inside_range": True,
        "existing_position_stop_required": True,
        "old_pending_orders_excluded_after_replace": True,
        "unknown_facts_block_new_exposure": True,
    }
    body["policy_id"] = f"grid-risk-policy-{_digest(body)}"
    return body


def grid_risk_evaluator() -> dict[str, Any]:
    root = Path(__file__).resolve().parents[1]
    paths = (
        root / "services" / "risk_port.py",
        root / "schemas" / "risk.py",
        root / "services" / "accounting_projection.py",
    )
    hashes = {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    return {
        "name": "paper_grid_risk",
        "version": GRID_RISK_EVALUATOR_VERSION,
        "source_hashes": hashes,
        "code_sha256": _digest(hashes),
    }


def live_money_risk_evaluator() -> dict[str, Any]:
    root = Path(__file__).resolve().parents[1]
    paths = (
        root / "services" / "risk_port.py",
        root / "schemas" / "risk.py",
        root / "services" / "live_money_guardrails.py",
    )
    hashes = {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    return {
        "name": "live_money_risk_bridge",
        "version": LIVE_MONEY_RISK_EVALUATOR_VERSION,
        "source_hashes": hashes,
        "code_sha256": _digest(hashes),
    }


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


def _live_money_risk_request(
    *,
    checked_at: str,
    run_date: str,
    ticket: Mapping[str, Any],
    symbol: str,
    side: str,
    requested_price: float,
    quantity: float,
    source: str,
    reconciliation: Mapping[str, Any],
    legacy: Mapping[str, Any],
    action_class: str,
) -> RiskRequest:
    candidate = dict(legacy.get("candidate") or {})
    candidate.update({
        "kind": "venue_entry",
        "run_date": str(run_date),
        "ticket_id": str(ticket.get("ticket_id") or candidate.get("ticket_id") or ""),
        "symbol": str(symbol),
        "side": str(side),
        "requested_price": requested_price,
        "quantity": quantity,
        "source": str(source),
    })
    policy = {
        "schema_version": "live-money-risk-policy-v1",
        "limits": dict(legacy.get("limits") or {}),
        "legacy_policy_authority": "LiveMoneyGuardrails",
        "unknown_facts_block_new_exposure": True,
    }
    policy["policy_id"] = f"live-money-risk-policy-{_digest(policy)}"
    return build_risk_request(
        checked_at=checked_at,
        scope="venue_entry",
        action_class=action_class,
        candidate=candidate,
        account={
            "daily_loss": dict(legacy.get("daily_loss") or {}),
            "halt": dict(legacy.get("halt") or {}),
        },
        market={
            "symbol": str(symbol),
            "price": requested_price,
            "source": str(source),
        },
        execution={
            "exposure": dict(legacy.get("exposure") or {}),
            "daily_entry_orders": dict(legacy.get("daily_entry_orders") or {}),
            "reconciliation": dict(reconciliation),
        },
        policy=policy,
        evaluator=live_money_risk_evaluator(),
    )


def _canonical_live_blockers(legacy: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = legacy.get("blockers") if isinstance(legacy.get("blockers"), list) else []
    blockers: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            blockers.append(_blocker(
                "legacy_guardrail_blocker_invalid",
                "LiveMoneyGuardrails",
                "legacy live-money blocker is malformed",
                {"index": index},
            ))
            continue
        blockers.append({
            "code": str(row.get("code") or "legacy_guardrail_blocked"),
            "source": str(row.get("source") or "LiveMoneyGuardrails"),
            "message": str(row.get("message") or row.get("status") or "legacy live-money guardrail blocked entry"),
            "evidence": dict(row.get("evidence") or {}),
            **({"legacy_status": str(row.get("status"))} if row.get("status") else {}),
        })
    if legacy.get("allows_new_order") is not True and not blockers:
        blockers.append(_blocker(
            "legacy_guardrail_denied_without_blocker",
            "LiveMoneyGuardrails",
            "legacy live-money guardrail did not grant entry permission",
            {"status": legacy.get("status"), "allows_new_order": legacy.get("allows_new_order")},
        ))
    return blockers


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


def _command_economics(commands: list[Any]) -> dict[str, Any]:
    notionals = {"buy": 0.0, "sell": 0.0}
    losses = {"buy": 0.0, "sell": 0.0}
    blockers: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, command in enumerate(commands):
        if not isinstance(command, Mapping):
            blockers.append(_blocker("candidate_command_invalid", "strategy_plan.commands", "candidate command is not an object", {"index": index}))
            continue
        identity = str(command.get("command_id") or "")
        if not identity or identity in seen:
            blockers.append(_blocker("candidate_command_identity_invalid", "strategy_plan.commands", "candidate command identity is missing or duplicated", {"index": index, "command_id": identity}))
        seen.add(identity)
        side = str(command.get("side") or "")
        price = _finite_positive(command.get("price"))
        quantity = _finite_positive(command.get("quantity"))
        stop = _finite_positive(command.get("sl"))
        take_profit = _finite_positive(command.get("tp"))
        claimed_notional = _finite_positive(command.get("notional"))
        if side not in {"buy", "sell"} or price is None or quantity is None or stop is None or take_profit is None:
            blockers.append(_blocker("candidate_command_economics_invalid", "strategy_plan.commands", "candidate command is missing side, price, quantity, SL, or TP", {"index": index, "command_id": identity}))
            continue
        if (side == "buy" and not (stop < price < take_profit)) or (side == "sell" and not (take_profit < price < stop)):
            blockers.append(_blocker("candidate_protection_geometry_invalid", "strategy_plan.commands", "candidate SL/TP geometry does not protect the entry", {"command_id": identity, "side": side, "price": price, "sl": stop, "tp": take_profit}))
            continue
        economic_notional = price * quantity
        if claimed_notional is None or abs(economic_notional - claimed_notional) > max(0.05, claimed_notional * 1e-5):
            blockers.append(_blocker("candidate_notional_mismatch", "strategy_plan.commands", "candidate command notional disagrees with price times quantity", {"command_id": identity, "declared": claimed_notional, "computed": round(economic_notional, 8)}))
        notionals[side] += economic_notional
        losses[side] += abs(price - stop) * quantity
    return {"notional_by_side": notionals, "loss_by_side": losses, "blockers": blockers}


def _position_economics(positions: list[Any], *, mark_price: float | None) -> dict[str, Any]:
    notionals = {"buy": 0.0, "sell": 0.0}
    losses = {"buy": 0.0, "sell": 0.0}
    blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    for row in positions:
        if not isinstance(row, Mapping):
            blockers.append(_blocker("open_position_invalid", "canonical_execution.open_positions", "open position is not an object", {}))
            continue
        identity = str(row.get("position_id") or row.get("trade_id") or "")
        side_value = str(row.get("side") or "").lower()
        side = "buy" if side_value in {"buy", "long"} else "sell" if side_value in {"sell", "short"} else ""
        quantity = _finite_positive(row.get("remaining_quantity"))
        entry = _finite_positive(row.get("entry_price"))
        stop = _finite_positive(row.get("sl"))
        if not identity or not side or quantity is None or entry is None:
            blockers.append(_blocker("open_position_risk_unknown", "canonical_execution.open_positions", "open position identity, side, remaining quantity, or entry is unknown", {"position_id": identity}))
            continue
        if stop is None or (side == "buy" and stop >= entry) or (side == "sell" and stop <= entry):
            blockers.append(_blocker("open_position_protection_unknown", "canonical_execution.open_positions", "open position has no valid stop-loss evidence", {"position_id": identity, "side": side, "entry_price": entry, "sl": row.get("sl")}))
            continue
        if _finite_positive(row.get("tp")) is None:
            warnings.append(_notice("open_position_take_profit_unknown", "canonical_execution.open_positions", "open position take-profit evidence is unavailable", {"position_id": identity}))
        notionals[side] += quantity * (mark_price or entry)
        losses[side] += quantity * abs(entry - stop)
    return {
        "notional_by_side": notionals,
        "loss_by_side": losses,
        "blockers": blockers,
        "warnings": warnings,
    }


def _notional_recommendation(
    candidate: Mapping[str, Any],
    *,
    candidate_loss_by_side: Mapping[str, float],
    existing_loss_by_side: Mapping[str, float],
    risk_budget: float | None,
) -> dict[str, Any]:
    current = _finite_positive(candidate.get("notional_per_grid"))
    if current is None or risk_budget is None:
        return {"available": False, "reason": "candidate_notional_or_risk_budget_unknown"}
    scales = []
    for side in ("buy", "sell"):
        candidate_loss = float(candidate_loss_by_side.get(side) or 0.0)
        if candidate_loss <= 0:
            continue
        available = max(0.0, risk_budget - float(existing_loss_by_side.get(side) or 0.0))
        scales.append(available / candidate_loss)
    if not scales:
        return {"available": False, "reason": "candidate_loss_rate_unknown"}
    recommended = max(0.0, current * min(scales))
    # Recommendations are executable ceilings. Round down to cents so a UI
    # applying the explicit operator choice cannot exceed the budget through
    # ordinary display rounding.
    recommended_cents = math.floor(min(current, recommended) * 100.0) / 100.0
    return {
        "available": recommended_cents > 0,
        "action": "recalculate_notional_by_risk_budget",
        "requested_notional_per_grid": round(current, 2),
        "recommended_notional_per_grid": recommended_cents,
        "applied_automatically": False,
    }


def _safe_action_identity_blockers(payload: Mapping[str, Any], *, expected: str) -> list[dict[str, Any]]:
    candidate = payload.get("candidate") if isinstance(payload.get("candidate"), Mapping) else {}
    if expected == "cancel":
        if str(candidate.get("cycle_id") or ""):
            return []
        return [_blocker("cancel_identity_invalid", "risk_request.candidate", "cancel requires cycle identity", {})]
    event = str(candidate.get("event") or "").lower()
    has_position = bool(candidate.get("trade_id") or candidate.get("position_id"))
    if event in CLOSE_EVENTS and has_position:
        return []
    return [_blocker("close_identity_invalid", "risk_request.candidate", "reduce-only action requires close event and position identity", {"event": event})]


def _blocked_message(decision: RiskDecision) -> str:
    blocker = decision.to_dict().get("primary_blocker") or {}
    code = str(blocker.get("code") or "risk_blocked")
    message = str(blocker.get("message") or "risk decision blocks new exposure")
    return f"risk decision blocked: {code}: {message}"


def _blocker(code: str, source: str, message: str, evidence: Mapping[str, Any]) -> dict[str, Any]:
    return {"code": code, "source": source, "message": message, "evidence": dict(evidence)}


def _notice(code: str, source: str, message: str, evidence: Mapping[str, Any]) -> dict[str, Any]:
    return {"code": code, "source": source, "message": message, "evidence": dict(evidence)}


def _finite_positive(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0 else None


def _fraction(value: Any) -> float | None:
    parsed = _finite_positive(value)
    return parsed if parsed is not None and parsed <= 1 else None


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(float(value), 8)


def _rounded_mapping(values: Mapping[str, float]) -> dict[str, float]:
    return {key: round(float(value), 8) for key, value in values.items()}


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8")
    ).hexdigest()


def _safe_filename(value: str) -> str:
    rendered = "".join(character for character in value if character.isalnum() or character in {"-", "_"})
    return rendered or "unscoped"
