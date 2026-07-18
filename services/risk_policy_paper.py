"""Pure grid/portfolio risk policy adapter."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from schemas.risk import (
    RiskDecision,
    RiskRequest,
    build_risk_decision,
    validate_risk_request,
)
from services.risk_policy_core import (
    blocker,
    digest,
    finite_positive,
    fraction,
    notice,
    rounded,
    rounded_mapping,
    safe_action_identity_blockers,
)


GRID_RISK_POLICY_SCHEMA = "strategy-grid-risk-policy-v1"
GRID_RISK_EVALUATOR_VERSION = "paper-grid-risk-v1"
EXPOSURE_ACTIONS = frozenset({"increase_exposure", "replace_pending"})


class PaperGridRiskDecisionPort:
    """Evaluate one exact grid batch against current portfolio facts."""

    name = "paper_grid_risk"

    def evaluator_metadata(self) -> Mapping[str, Any]:
        return grid_risk_evaluator()

    def resolve_policy(self, config: Mapping[str, Any]) -> Mapping[str, Any]:
        return grid_risk_policy(config)

    def evaluate(self, request: RiskRequest | Mapping[str, Any]) -> RiskDecision:
        current = validate_risk_request(request)
        payload = current.to_dict()
        expected_evaluator = dict(self.evaluator_metadata())
        if payload["evaluator"] != expected_evaluator:
            return build_risk_decision(
                current,
                blockers=[
                    blocker(
                        "risk_evaluator_mismatch",
                        "risk_request.evaluator",
                        "risk request evaluator does not match the selected policy plugin",
                        {
                            "expected_name": expected_evaluator.get("name"),
                            "expected_version": expected_evaluator.get("version"),
                            "received_name": payload["evaluator"].get("name"),
                            "received_version": payload["evaluator"].get("version"),
                        },
                    )
                ],
            )
        action = str(payload["action_class"])
        if action == "cancel":
            blockers = safe_action_identity_blockers(payload, expected="cancel")
            return build_risk_decision(current, blockers=blockers)
        if action == "reduce_only":
            blockers = safe_action_identity_blockers(payload, expected="reduce_only")
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

        equity = finite_positive(account.get("equity"))
        if account.get("status") != "ready" or equity is None:
            blockers.append(
                blocker(
                    "account_snapshot_unavailable",
                    "canonical_account",
                    "canonical account equity is unavailable or untrusted",
                    {
                        "status": account.get("status"),
                        "reason": account.get("reason"),
                    },
                )
            )
        elif account.get("reconciliation_status") != "pass":
            blockers.append(
                blocker(
                    "account_reconciliation_drift",
                    "canonical_account.reconciliation",
                    "canonical accounting reconciliation is not pass",
                    {
                        "status": account.get("reconciliation_status"),
                        "issues": account.get("reconciliation_issues", []),
                    },
                )
            )

        price = finite_positive(market.get("price"))
        if (
            market.get("status") not in {"ready", "derived"}
            or market.get("fresh") is not True
            or market.get("is_synthetic") is not False
            or not str(market.get("provider") or "").strip()
            or price is None
        ):
            blockers.append(
                blocker(
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
                )
            )

        if execution.get("status") != "ready":
            blockers.append(
                blocker(
                    "execution_state_unavailable",
                    "canonical_execution",
                    "current execution state cannot be normalized safely",
                    {
                        "status": execution.get("status"),
                        "reason": execution.get("reason"),
                    },
                )
            )
        elif (
            execution.get("accounting_reconciliation_status") != "pass"
            or execution.get("engine_reconciliation_status") != "ok"
        ):
            blockers.append(
                blocker(
                    "execution_reconciliation_drift",
                    "canonical_execution.reconciliation",
                    "execution reconciliation is not clean",
                    {
                        "accounting_status": execution.get(
                            "accounting_reconciliation_status"
                        ),
                        "engine_status": execution.get(
                            "engine_reconciliation_status"
                        ),
                        "issues": execution.get("reconciliation_issues", []),
                    },
                )
            )

        candidate_kind = str(candidate.get("kind") or "")
        if candidate_kind == "strategy_plan_grid":
            low = finite_positive(candidate.get("range_low"))
            high = finite_positive(candidate.get("range_high"))
            if low is None or high is None or high <= low:
                blockers.append(
                    blocker(
                        "candidate_range_invalid",
                        "strategy_plan.range",
                        "candidate range is invalid",
                        {
                            "low": candidate.get("range_low"),
                            "high": candidate.get("range_high"),
                        },
                    )
                )
            elif price is not None and not (low <= price <= high):
                blockers.append(
                    blocker(
                        "market_price_outside_range",
                        "strategy_plan.range",
                        "current market price is outside the candidate range",
                        {"price": price, "low": low, "high": high},
                    )
                )
        elif candidate_kind != "manual_order":
            blockers.append(
                blocker(
                    "candidate_kind_invalid",
                    "risk_request.candidate",
                    "exposure-increasing candidate kind is unsupported",
                    {"kind": candidate_kind},
                )
            )

        commands = (
            candidate.get("commands")
            if isinstance(candidate.get("commands"), list)
            else []
        )
        candidate_economics = _command_economics(commands)
        blockers.extend(candidate_economics["blockers"])
        candidate_by_side = candidate_economics["notional_by_side"]
        candidate_loss_by_side = candidate_economics["loss_by_side"]
        if not commands:
            blockers.append(
                blocker(
                    "candidate_commands_missing",
                    "strategy_plan.commands",
                    "candidate has no exposure-increasing commands",
                    {},
                )
            )

        positions = (
            execution.get("open_positions")
            if isinstance(execution.get("open_positions"), list)
            else []
        )
        existing = _position_economics(positions, mark_price=price)
        blockers.extend(existing["blockers"])
        warnings.extend(existing["warnings"])

        accepted = (
            execution.get("open_entry_orders")
            if isinstance(execution.get("open_entry_orders"), list)
            else []
        )
        accepted_ids = {
            str(row.get("order_id") or "")
            for row in accepted
            if str(row.get("order_id") or "")
        }
        replaced_ids = {
            str(value)
            for value in candidate.get("replaced_order_ids") or []
            if str(value)
        }
        if payload["action_class"] == "increase_exposure":
            if accepted_ids:
                blockers.append(
                    blocker(
                        "existing_entry_orders_present",
                        "canonical_execution.open_orders",
                        "new grid start cannot layer over existing pending entries",
                        {"order_ids": sorted(accepted_ids)},
                    )
                )
            if positions and candidate.get("intent") == "start_grid":
                blockers.append(
                    blocker(
                        "existing_positions_require_regrid",
                        "canonical_execution.open_positions",
                        "new grid start cannot layer over existing positions; use running regrid or close first",
                        {
                            "position_ids": sorted(
                                str(
                                    row.get("position_id")
                                    or row.get("trade_id")
                                    or ""
                                )
                                for row in positions
                            )
                        },
                    )
                )
        else:
            unmanaged = sorted(accepted_ids - replaced_ids)
            missing = sorted(replaced_ids - accepted_ids)
            if unmanaged or missing:
                blockers.append(
                    blocker(
                        "replacement_order_set_mismatch",
                        "canonical_execution.open_orders",
                        "regrid replacement set does not exactly match current pending entries",
                        {
                            "unmanaged_order_ids": unmanaged,
                            "missing_order_ids": missing,
                        },
                    )
                )

        requested_leverage = finite_positive(candidate.get("leverage"))
        max_leverage = finite_positive(policy.get("max_leverage"))
        max_loss_pct = fraction(policy.get("max_plan_loss_pct"))
        utilization = fraction(policy.get("margin_utilization_cap"))
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
            blockers.append(
                blocker(
                    "risk_policy_invalid",
                    "grid_risk_policy",
                    "resolved grid risk policy is missing valid limits",
                    {"fields": invalid_policy_fields},
                )
            )
        if requested_leverage is None:
            blockers.append(
                blocker(
                    "candidate_leverage_invalid",
                    "strategy_plan.grid.leverage",
                    "candidate leverage is missing or invalid",
                    {"value": candidate.get("leverage")},
                )
            )
        elif (
            max_leverage is not None
            and requested_leverage > max_leverage + 1e-12
        ):
            blockers.append(
                blocker(
                    "leverage_limit_exceeded",
                    "grid_risk_policy.max_leverage",
                    "requested leverage exceeds policy limit",
                    {"requested": requested_leverage, "limit": max_leverage},
                )
            )

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
        risk_budget = (
            equity * max_loss_pct
            if equity is not None and max_loss_pct is not None
            else None
        )
        margin_budget = (
            equity * utilization
            if equity is not None and utilization is not None
            else None
        )
        actual_leverage = projected_notional / equity if equity is not None else None
        estimated_margin = (
            projected_notional / requested_leverage
            if requested_leverage is not None
            else None
        )

        if risk_budget is not None and projected_loss > risk_budget + 1e-8:
            blockers.append(
                blocker(
                    "plan_loss_budget_exceeded",
                    "grid_risk_policy.max_plan_loss",
                    "candidate maximum loss exceeds the configured plan-loss budget",
                    {
                        "projected_max_loss": round(projected_loss, 8),
                        "max_loss_budget": round(risk_budget, 8),
                    },
                )
            )
        if (
            actual_leverage is not None
            and max_leverage is not None
            and actual_leverage > max_leverage + 1e-8
        ):
            blockers.append(
                blocker(
                    "projected_leverage_exceeded",
                    "grid_risk_policy.max_leverage",
                    "projected position leverage exceeds policy limit",
                    {
                        "projected_leverage": round(actual_leverage, 8),
                        "limit": max_leverage,
                    },
                )
            )
        if (
            estimated_margin is not None
            and margin_budget is not None
            and estimated_margin > margin_budget + 1e-8
        ):
            blockers.append(
                blocker(
                    "projected_margin_exceeded",
                    "grid_risk_policy.margin_utilization",
                    "projected margin exceeds the configured account budget",
                    {
                        "estimated_margin": round(estimated_margin, 8),
                        "margin_budget": round(margin_budget, 8),
                    },
                )
            )

        recommendation = _notional_recommendation(
            candidate,
            candidate_loss_by_side=candidate_loss_by_side,
            existing_loss_by_side=existing_loss,
            risk_budget=risk_budget,
        )
        metrics = {
            "calculation_source": "exact_commands_plus_canonical_accounting",
            "preview_risk_fields_used": False,
            "equity": rounded(equity),
            "candidate_notional_by_side": rounded_mapping(candidate_by_side),
            "existing_notional_by_side": rounded_mapping(existing_notional),
            "projected_notional_by_side": rounded_mapping(projected_by_side),
            "projected_max_side_notional": rounded(projected_notional),
            "candidate_loss_by_side": rounded_mapping(candidate_loss_by_side),
            "existing_stop_loss_by_side": rounded_mapping(existing_loss),
            "projected_loss_by_side": rounded_mapping(projected_loss_by_side),
            "projected_max_loss": rounded(projected_loss),
            "projected_actual_leverage": rounded(actual_leverage),
            "projected_margin": rounded(estimated_margin),
            "open_position_count": len(positions),
            "open_entry_order_count": len(accepted),
        }
        limits = {
            "max_plan_loss_pct": max_loss_pct,
            "max_plan_loss": rounded(risk_budget),
            "max_leverage": max_leverage,
            "margin_utilization_cap": utilization,
            "margin_budget": rounded(margin_budget),
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


def grid_risk_policy(config: Mapping[str, Any]) -> dict[str, Any]:
    strategy = (
        config.get("strategy_grid")
        if isinstance(config.get("strategy_grid"), Mapping)
        else {}
    )
    body = {
        "schema_version": GRID_RISK_POLICY_SCHEMA,
        "max_leverage": finite_positive(config.get("max_leverage")),
        "max_plan_loss_pct": fraction(strategy.get("max_plan_loss_pct")),
        "margin_utilization_cap": fraction(
            strategy.get("capital_utilization_cap")
        ),
        "market_must_be_inside_range": True,
        "existing_position_stop_required": True,
        "old_pending_orders_excluded_after_replace": True,
        "unknown_facts_block_new_exposure": True,
    }
    body["policy_id"] = f"grid-risk-policy-{digest(body)}"
    return body


def grid_risk_evaluator() -> dict[str, Any]:
    root = Path(__file__).resolve().parents[1]
    paths = (
        root / "services" / "risk_policy_paper.py",
        root / "services" / "risk_policy_core.py",
        root / "schemas" / "risk.py",
        root / "services" / "accounting_projection_core.py",
    )
    hashes = {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in paths
    }
    return {
        "name": "paper_grid_risk",
        "version": GRID_RISK_EVALUATOR_VERSION,
        "source_hashes": hashes,
        "code_sha256": digest(hashes),
    }


def _command_economics(commands: list[Any]) -> dict[str, Any]:
    notionals = {"buy": 0.0, "sell": 0.0}
    losses = {"buy": 0.0, "sell": 0.0}
    blockers: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, command in enumerate(commands):
        if not isinstance(command, Mapping):
            blockers.append(
                blocker(
                    "candidate_command_invalid",
                    "strategy_plan.commands",
                    "candidate command is not an object",
                    {"index": index},
                )
            )
            continue
        identity = str(command.get("command_id") or "")
        if not identity or identity in seen:
            blockers.append(
                blocker(
                    "candidate_command_identity_invalid",
                    "strategy_plan.commands",
                    "candidate command identity is missing or duplicated",
                    {"index": index, "command_id": identity},
                )
            )
        seen.add(identity)
        side = str(command.get("side") or "")
        price = finite_positive(command.get("price"))
        quantity = finite_positive(command.get("quantity"))
        stop = finite_positive(command.get("sl"))
        take_profit = finite_positive(command.get("tp"))
        claimed_notional = finite_positive(command.get("notional"))
        if (
            side not in {"buy", "sell"}
            or price is None
            or quantity is None
            or stop is None
            or take_profit is None
        ):
            blockers.append(
                blocker(
                    "candidate_command_economics_invalid",
                    "strategy_plan.commands",
                    "candidate command is missing side, price, quantity, SL, or TP",
                    {"index": index, "command_id": identity},
                )
            )
            continue
        if (side == "buy" and not (stop < price < take_profit)) or (
            side == "sell" and not (take_profit < price < stop)
        ):
            blockers.append(
                blocker(
                    "candidate_protection_geometry_invalid",
                    "strategy_plan.commands",
                    "candidate SL/TP geometry does not protect the entry",
                    {
                        "command_id": identity,
                        "side": side,
                        "price": price,
                        "sl": stop,
                        "tp": take_profit,
                    },
                )
            )
            continue
        economic_notional = price * quantity
        if claimed_notional is None or abs(
            economic_notional - claimed_notional
        ) > max(0.05, claimed_notional * 1e-5):
            blockers.append(
                blocker(
                    "candidate_notional_mismatch",
                    "strategy_plan.commands",
                    "candidate command notional disagrees with price times quantity",
                    {
                        "command_id": identity,
                        "declared": claimed_notional,
                        "computed": round(economic_notional, 8),
                    },
                )
            )
        notionals[side] += economic_notional
        losses[side] += abs(price - stop) * quantity
    return {
        "notional_by_side": notionals,
        "loss_by_side": losses,
        "blockers": blockers,
    }


def _position_economics(
    positions: list[Any],
    *,
    mark_price: float | None,
) -> dict[str, Any]:
    notionals = {"buy": 0.0, "sell": 0.0}
    losses = {"buy": 0.0, "sell": 0.0}
    blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    for row in positions:
        if not isinstance(row, Mapping):
            blockers.append(
                blocker(
                    "open_position_invalid",
                    "canonical_execution.open_positions",
                    "open position is not an object",
                    {},
                )
            )
            continue
        identity = str(row.get("position_id") or row.get("trade_id") or "")
        side_value = str(row.get("side") or "").lower()
        side = (
            "buy"
            if side_value in {"buy", "long"}
            else "sell"
            if side_value in {"sell", "short"}
            else ""
        )
        quantity = finite_positive(row.get("remaining_quantity"))
        entry = finite_positive(row.get("entry_price"))
        stop = finite_positive(row.get("sl"))
        if not identity or not side or quantity is None or entry is None:
            blockers.append(
                blocker(
                    "open_position_risk_unknown",
                    "canonical_execution.open_positions",
                    "open position identity, side, remaining quantity, or entry is unknown",
                    {"position_id": identity},
                )
            )
            continue
        if stop is None or (side == "buy" and stop >= entry) or (
            side == "sell" and stop <= entry
        ):
            blockers.append(
                blocker(
                    "open_position_protection_unknown",
                    "canonical_execution.open_positions",
                    "open position has no valid stop-loss evidence",
                    {
                        "position_id": identity,
                        "side": side,
                        "entry_price": entry,
                        "sl": row.get("sl"),
                    },
                )
            )
            continue
        if finite_positive(row.get("tp")) is None:
            warnings.append(
                notice(
                    "open_position_take_profit_unknown",
                    "canonical_execution.open_positions",
                    "open position take-profit evidence is unavailable",
                    {"position_id": identity},
                )
            )
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
    current = finite_positive(candidate.get("notional_per_grid"))
    if current is None or risk_budget is None:
        return {
            "available": False,
            "reason": "candidate_notional_or_risk_budget_unknown",
        }
    scales = []
    for side in ("buy", "sell"):
        candidate_loss = float(candidate_loss_by_side.get(side) or 0.0)
        if candidate_loss <= 0:
            continue
        available = max(
            0.0,
            risk_budget - float(existing_loss_by_side.get(side) or 0.0),
        )
        scales.append(available / candidate_loss)
    if not scales:
        return {"available": False, "reason": "candidate_loss_rate_unknown"}
    recommended = max(0.0, current * min(scales))
    recommended_cents = math.floor(min(current, recommended) * 100.0) / 100.0
    return {
        "available": recommended_cents > 0,
        "action": "recalculate_notional_by_risk_budget",
        "requested_notional_per_grid": round(current, 2),
        "recommended_notional_per_grid": recommended_cents,
        "applied_automatically": False,
    }
