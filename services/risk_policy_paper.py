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


GRID_RISK_POLICY_SCHEMA = "strategy-grid-risk-policy-v2"
GRID_RISK_EVALUATOR_VERSION = "paper-grid-risk-v2"
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
        min_grid_count = _positive_integer(policy.get("min_grid_count"))
        max_grid_count = _positive_integer(policy.get("max_grid_count"))
        if candidate_kind == "strategy_plan_grid":
            direction = str(candidate.get("direction") or "neutral").lower()
            if direction in {"long", "short"}:
                min_grid_count = (
                    max(2, math.ceil(min_grid_count / 2))
                    if min_grid_count is not None
                    else None
                )
                max_grid_count = (
                    max(2, math.ceil(max_grid_count / 2))
                    if max_grid_count is not None
                    else None
                )
            raw_grid_count = candidate.get("grid_count")
            grid_count = _positive_integer(raw_grid_count)
            if (
                grid_count is None
                or min_grid_count is None
                or max_grid_count is None
                or not min_grid_count <= grid_count <= max_grid_count
            ):
                blockers.append(
                    blocker(
                        "candidate_grid_count_out_of_bounds",
                        "strategy_plan.grid.count",
                        "candidate grid count is outside the configured operating band",
                        {
                            "count": raw_grid_count,
                            "direction": direction,
                            "minimum": min_grid_count,
                            "maximum": max_grid_count,
                        },
                    )
                )
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
        cost_per_side_bp = _non_negative_number(policy.get("cost_per_side_bp"))
        candidate_economics = _command_economics(
            commands,
            cost_per_side_rate=(cost_per_side_bp or 0.0) / 10_000.0,
        )
        blockers.extend(candidate_economics["blockers"])
        candidate_by_side = candidate_economics["notional_by_side"]
        candidate_loss_by_side = candidate_economics["loss_by_side"]
        empty_replacement_allowed = (
            payload["action_class"] == "replace_pending"
            and bool(candidate.get("replaced_order_ids"))
            and not candidate.get("retained_order_ids")
        )
        if not commands and not empty_replacement_allowed:
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
        accepted_by_id = {
            str(row.get("order_id") or ""): row
            for row in accepted
            if isinstance(row, Mapping) and str(row.get("order_id") or "")
        }
        replaced_ids = {
            str(value)
            for value in candidate.get("replaced_order_ids") or []
            if str(value)
        }
        retained_ids = {
            str(value)
            for value in candidate.get("retained_order_ids") or []
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
            overlapping = sorted(replaced_ids & retained_ids)
            accounted_ids = replaced_ids | retained_ids
            unmanaged = sorted(accepted_ids - accounted_ids)
            missing = sorted(accounted_ids - accepted_ids)
            retained_command_ids = [
                str(row.get("existing_order_id") or "")
                for row in commands
                if isinstance(row, Mapping) and str(row.get("existing_order_id") or "")
            ]
            duplicated_retained_commands = sorted({
                order_id
                for order_id in retained_command_ids
                if retained_command_ids.count(order_id) > 1
            })
            unbound_retained = sorted(retained_ids - set(retained_command_ids))
            unexpected_retained = sorted(set(retained_command_ids) - retained_ids)
            retained_economics_mismatch = sorted(
                str(row.get("existing_order_id") or "")
                for row in commands
                if isinstance(row, Mapping)
                and str(row.get("existing_order_id") or "") in accepted_by_id
                and not _same_retained_order_economics(
                    row,
                    accepted_by_id[str(row.get("existing_order_id") or "")],
                )
            )
            if (
                overlapping
                or unmanaged
                or missing
                or duplicated_retained_commands
                or unbound_retained
                or unexpected_retained
                or retained_economics_mismatch
            ):
                blockers.append(
                    blocker(
                        "replacement_order_set_mismatch",
                        "canonical_execution.open_orders",
                        "regrid retained and replaced sets do not exactly match current pending entries",
                        {
                            "overlapping_order_ids": overlapping,
                            "unmanaged_order_ids": unmanaged,
                            "missing_order_ids": missing,
                            "duplicated_retained_command_ids": duplicated_retained_commands,
                            "unbound_retained_order_ids": unbound_retained,
                            "unexpected_retained_order_ids": unexpected_retained,
                            "retained_economics_mismatch_order_ids": retained_economics_mismatch,
                        },
                    )
                )

        requested_leverage = finite_positive(candidate.get("leverage"))
        max_leverage = finite_positive(policy.get("max_leverage"))
        required_leverage = finite_positive(policy.get("required_leverage"))
        min_net_profit = finite_positive(
            policy.get("min_net_profit_per_grid_usd")
        )
        utilization = fraction(policy.get("margin_utilization_cap"))
        invalid_policy_fields = [
            field
            for field, value in (
                ("max_leverage", max_leverage),
                ("required_leverage", required_leverage),
                ("min_net_profit_per_grid_usd", min_net_profit),
                ("min_grid_count", min_grid_count),
                ("max_grid_count", max_grid_count),
                ("margin_utilization_cap", utilization),
                ("cost_per_side_bp", cost_per_side_bp),
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
        elif max_leverage is not None and requested_leverage > max_leverage + 1e-12:
            blockers.append(
                blocker(
                    "leverage_limit_exceeded",
                    "grid_risk_policy.max_leverage",
                    "requested leverage exceeds policy limit",
                    {"requested": requested_leverage, "limit": max_leverage},
                )
            )
        elif required_leverage is not None and abs(requested_leverage - required_leverage) > 1e-12:
            blockers.append(
                blocker(
                    "required_leverage_mismatch",
                    "grid_risk_policy.required_leverage",
                    "grid leverage must equal the fixed policy leverage",
                    {"requested": requested_leverage, "required": required_leverage},
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

        planned_net_profits = list(candidate_economics["net_profit_usd"])
        minimum_planned_net_profit = min(planned_net_profits) if planned_net_profits else None
        if (
            candidate_kind == "strategy_plan_grid"
            and commands
            and min_net_profit is not None
            and (minimum_planned_net_profit is None or minimum_planned_net_profit + 1e-8 < min_net_profit)
        ):
            blockers.append(
                blocker(
                    "grid_profit_target_not_met",
                    "grid_risk_policy.min_net_profit_per_grid_usd",
                    "at least one completed grid is below the planned net-profit target",
                    {
                        "minimum_planned_net_profit_usd": rounded(minimum_planned_net_profit),
                        "target_net_profit_per_grid_usd": rounded(min_net_profit),
                        "calculation": "modeled_entry_exit_fees_funding_excluded",
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

        warnings.append(
            notice(
                "plan_max_loss_advisory",
                "strategy_plan.commands",
                "maximum stop loss is reported for operator awareness and does not block sizing",
                {"projected_max_loss": round(projected_loss, 8)},
            )
        )
        recommendation = {
            "available": False,
            "reason": "profit_target_requires_grid_geometry_or_capital_change",
            "applied_automatically": False,
        }
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
            "minimum_planned_net_profit_per_grid_usd": rounded(minimum_planned_net_profit),
            "projected_actual_leverage": rounded(actual_leverage),
            "projected_margin": rounded(estimated_margin),
            "open_position_count": len(positions),
            "open_entry_order_count": len(accepted),
            "retained_entry_order_count": len(retained_ids),
            "replaced_entry_order_count": len(replaced_ids),
        }
        limits = {
            "max_leverage": max_leverage,
            "required_leverage": required_leverage,
            "min_net_profit_per_grid_usd": min_net_profit,
            "cost_per_side_bp": cost_per_side_bp,
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
        "required_leverage": finite_positive(
            strategy.get("required_leverage") or config.get("max_leverage")
        ),
        "min_net_profit_per_grid_usd": finite_positive(
            strategy.get("min_net_profit_per_grid_usd") or 10.0
        ),
        "min_grid_count": _positive_integer(strategy.get("min_grid_count") or 30),
        "max_grid_count": _positive_integer(strategy.get("max_grid_count") or 70),
        "cost_per_side_bp": _non_negative_number(
            config.get("cost_per_side_bp", 0.5)
        ),
        "margin_utilization_cap": fraction(
            strategy.get("capital_utilization_cap")
        ),
        "market_must_be_inside_range": True,
        "existing_position_stop_required": True,
        "old_pending_orders_excluded_after_replace": True,
        "retained_pending_orders_bound_to_candidate": True,
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


def _same_retained_order_economics(
    candidate: Mapping[str, Any],
    accepted: Mapping[str, Any],
) -> bool:
    if str(candidate.get("side") or "").lower() != str(accepted.get("side") or "").lower():
        return False
    for field in ("price", "quantity"):
        candidate_value = finite_positive(candidate.get(field))
        accepted_value = finite_positive(accepted.get(field))
        if candidate_value is None or accepted_value is None:
            return False
        if abs(candidate_value - accepted_value) > max(1e-8, accepted_value * 1e-8):
            return False
    candidate_plan = str(candidate.get("strategy_plan_id") or "")
    accepted_plan = str(accepted.get("strategy_plan_id") or "")
    if accepted_plan and candidate_plan != accepted_plan:
        return False
    candidate_version = candidate.get("strategy_plan_version")
    accepted_version = accepted.get("strategy_plan_version")
    return accepted_version in (None, "") or candidate_version == accepted_version


def _command_economics(
    commands: list[Any],
    *,
    cost_per_side_rate: float,
) -> dict[str, Any]:
    notionals = {"buy": 0.0, "sell": 0.0}
    losses = {"buy": 0.0, "sell": 0.0}
    net_profits: list[float] = []
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
        gross_profit = abs(take_profit - price) * quantity
        modeled_fees = (price + take_profit) * quantity * cost_per_side_rate
        net_profits.append(gross_profit - modeled_fees)
    return {
        "notional_by_side": notionals,
        "loss_by_side": losses,
        "net_profit_usd": net_profits,
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


def _non_negative_number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed >= 0 else None


def _positive_integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed > 0 and math.isfinite(numeric) and numeric == parsed else None
