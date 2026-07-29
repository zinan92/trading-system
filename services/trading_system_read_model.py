"""Provider-neutral, immutable operator read model for the trading system.

The projector in this module is deliberately pure.  It accepts already-read
source facts, copies canonical accounting truth, and adds presentation-ready
labels.  It never reads artifacts, calls a venue, or authorizes a command.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any

from services.accounting_projection_core import OPEN_ORDER_STATES
from services.order_lifecycle import LEGAL_TRANSITIONS, ORDER_STATES, TERMINAL_STATES


TRADING_SYSTEM_READ_MODEL_SCHEMA = "trading-system-read-model-v1"

_DIRECTION_LABELS = {
    "neutral": "中性",
    "long": "做多",
    "short": "做空",
}
_STYLE_LABELS = {
    "steady": "稳健",
    "aggressive": "激进",
}
_GRID_MODE_LABELS = {
    "arithmetic": "等价差",
    "geometric": "等比例",
}
_ORDER_STATE_PRESENTATION = {
    "entry": (10, "已创建"),
    "submitting": (20, "提交中"),
    "submitted": (21, "提交中"),
    "new": (22, "待接受"),
    "pending": (22, "待接受"),
    "accepted": (30, "已接受"),
    "open": (30, "已接受"),
    "working": (30, "已接受"),
    "partially_filled": (40, "部分成交"),
    "filled": (50, "已成交"),
    "cancelled": (50, "已撤单"),
    "rejected": (50, "已拒绝"),
    "expired": (50, "已过期"),
    # Protection can legally recover in either direction.  Keep both states in
    # one presentation phase and use the authoritative transition revision to
    # distinguish a real recovery from a delayed snapshot.
    "protective_attached": (60, "保护已挂"),
    "protective_failed": (60, "保护异常"),
    "closed": (70, "已关闭"),
    "reconciled": (80, "已对账"),
}
_KNOWN_ORDER_STATES = ORDER_STATES | OPEN_ORDER_STATES
_ACCEPTED_ORDER_STATES = {"accepted", "open", "working", "partially_filled"}
_TP_EVENTS = {"target", "take_profit", "tp"}
_SL_EVENTS = {"stop", "stop_loss", "sl"}
_MANUAL_EXIT_EVENTS = {"exit", "flatten", "manual"}


@dataclass(frozen=True)
class TradingSystemReadModel:
    """Deep-frozen, JSON-safe read model with a thawing API boundary."""

    payload: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return _thaw(self.payload)


def project_trading_system_read_model(
    console_snapshot: Mapping[str, Any] | None,
    *,
    risk_decision: Mapping[str, Any] | None = None,
    broker: Mapping[str, Any] | None = None,
    generated_at: str | None = None,
) -> TradingSystemReadModel:
    """Project one request-scoped source snapshot into the stable contract."""

    source = _mapping(console_snapshot)
    cycle = _json_copy(_mapping(source.get("cycle")))
    market = project_market_read_model(source.get("market"))
    plan = _json_copy(_mapping(source.get("production_plan")))
    plan_history = _json_copy(_list(source.get("production_plan_history")))
    proposals = _json_copy(_list(source.get("proposals")))
    execution_source = _mapping(source.get("production_execution"))
    current_accounting = _json_copy(_mapping(execution_source.get("accounting_snapshot")))
    history_accounting_value = execution_source.get("production_history_accounting_snapshot")
    accounting = _json_copy(
        _mapping(history_accounting_value)
        if "production_history_accounting_snapshot" in execution_source
        else current_accounting
    )
    completeness_issues: list[str] = []

    if not plan:
        completeness_issues.append("production_plan_missing")
    if not accounting:
        completeness_issues.append("canonical_accounting_missing")
    elif str(accounting.get("schema_version") or "") != "accounting-snapshot-v1":
        completeness_issues.append("canonical_accounting_schema_mismatch")
    if not market.get("provider"):
        completeness_issues.append("market_provider_missing")
    if market.get("trusted") is not True:
        completeness_issues.append("market_not_trusted")

    execution = _project_execution(
        execution_source,
        history_accounting=accounting,
        current_accounting=current_accounting,
        plan=plan,
        plan_history=plan_history,
        completeness_issues=completeness_issues,
        dca_lifecycle_source=source.get("dca_lifecycle"),
        grid_lifecycle_source=execution_source.get("grid_lifecycle"),
    )
    unknown_order_count = execution["counts"]["unknown_order_count"]
    if unknown_order_count:
        completeness_issues.append("execution_order_state_unknown")
    risk = _project_risk(
        source.get("runtime"),
        risk_decision,
        plan,
        completeness_issues,
    )
    runtime = _project_runtime(
        source.get("runtime"),
        plan=plan,
        market=market,
        open_order_count=execution["counts"]["open_order_count"],
        unknown_order_count=unknown_order_count,
        risk_status=risk["status"],
        completeness_issues=completeness_issues,
    )
    runtime["utilization"] = _json_copy(
        _mapping(source.get("runtime_utilization"))
    )
    runtime["cycle_decision"] = _json_copy(
        _mapping(source.get("cycle_decision"))
    )
    strategy_summary = _project_strategy_summary(plan, completeness_issues)
    broker_view = _json_copy(_mapping(broker))
    if not broker_view:
        completeness_issues.append("broker_read_model_missing")

    core = {
        "cycle": cycle,
        "market": market,
        "strategy": {
            "plan": plan,
            "summary": strategy_summary,
            "proposals": proposals,
            "proposal_diff": _json_copy(_mapping(source.get("proposal_diff"))),
            "migration": _json_copy(_mapping(source.get("migration"))),
        },
        "runtime": runtime,
        "execution": {
            **execution,
            "broker": broker_view,
        },
        "risk": risk,
        "review": {
            "ledger": _json_copy(_mapping(source.get("ledger"))),
            "cycle_packages": _json_copy(_list(source.get("cycle_packages"))),
            "selected_cycle_id": str(source.get("review_cycle_id") or "") or None,
        },
        "research": {
            "strategy_shadows": _json_copy(_list(source.get("strategy_shadows"))),
            "strategy_shadow_promotion": _json_copy(_mapping(source.get("strategy_shadow_promotion"))),
            "execution_shadow": _json_copy(_mapping(source.get("execution_shadow"))),
        },
        "operations": {
            "safe_repair_queue": _json_copy(_mapping(source.get("safe_repair_queue"))),
            "cloud_health": _json_copy(_mapping(source.get("cloud_health"))),
        },
        "ui_capabilities": _json_copy(_mapping(source.get("ui_capabilities"))),
        "safety": {
            **_json_copy(_mapping(source.get("safety"))),
            "read_only": True,
            "command_authority": False,
        },
    }
    snapshot_id = _snapshot_id(core)
    payload = {
        "contract": {
            "schema_version": TRADING_SYSTEM_READ_MODEL_SCHEMA,
            "snapshot_id": snapshot_id,
            "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
            "read_only": True,
            "source_identities": {
                "cycle_id": cycle.get("cycle_id"),
                "market_snapshot_id": market.get("snapshot_id"),
                "strategy_plan_id": plan.get("strategy_plan_id"),
                "strategy_plan_version": plan.get("version"),
                "accounting_snapshot_id": accounting.get("snapshot_id"),
                "current_accounting_snapshot_id": current_accounting.get("snapshot_id"),
                "risk_decision_id": risk.get("displayed_decision_id"),
            },
        },
        **core,
        "completeness": {
            "status": "complete" if not completeness_issues else "degraded",
            "issues": list(dict.fromkeys(completeness_issues)),
        },
    }
    return TradingSystemReadModel(payload=_freeze(_json_copy(payload)))


def project_market_read_model(value: Any) -> dict[str, Any]:
    """Normalize one market envelope for display without selecting a provider."""

    source = _mapping(value)
    status = str(source.get("status") or "missing")
    fresh = source.get("fresh") is True
    synthetic = source.get("is_synthetic") is True
    provider = str(source.get("provider") or source.get("source_mode") or "").strip()
    trusted = status in {"ready", "derived"} and fresh and not synthetic and bool(provider)
    return {
        **_json_copy(source),
        "provider": provider or None,
        "provider_label": _display_name(provider),
        "trusted": trusted,
    }


def _project_execution(
    source: Mapping[str, Any],
    *,
    history_accounting: Mapping[str, Any],
    current_accounting: Mapping[str, Any],
    plan: Mapping[str, Any],
    plan_history: list[Any],
    completeness_issues: list[str],
    dca_lifecycle_source: Any,
    grid_lifecycle_source: Any,
) -> dict[str, Any]:
    orders = _project_orders(source.get("orders"), plan=plan, plan_history=plan_history)
    open_orders = [row for row in orders if row["is_open"]]
    accepted_orders = [row for row in orders if row["is_accepted"]]
    orders_by_id = {
        str(row.get("order_id") or ""): row
        for row in orders
        if str(row.get("order_id") or "")
    }
    current_positions = [
        _project_position(_mapping(row), orders_by_id=orders_by_id)
        for row in _list(current_accounting.get("positions"))
    ]
    open_positions = [
        row
        for row in current_positions
        if str(_mapping(row).get("status") or "") == "open"
    ]
    canonical_fills = _json_copy(_list(history_accounting.get("fills")))
    history_reconciliation = _mapping(history_accounting.get("reconciliation"))
    chronology_invalid_trade_ids = {
        str(issue.get("trade_id") or "")
        for issue in (
            _list(history_reconciliation.get("quarantined"))
            + _list(history_reconciliation.get("issues"))
        )
        if str(_mapping(issue).get("code") or "") == "closed_trade_exit_before_entry"
        and str(_mapping(issue).get("trade_id") or "")
    }
    canonical_trades = _project_trade_lifecycles(
        history_accounting.get("trades"),
        fills=canonical_fills,
        chronology_invalid_trade_ids=chronology_invalid_trade_ids,
    )
    canonical_counts = _mapping(history_accounting.get("counts"))
    canonical_pnl = _json_copy(_mapping(history_accounting.get("pnl")))
    canonical_account = _json_copy(_mapping(history_accounting.get("account")))
    current_account = _json_copy(_mapping(source.get("account")))
    dca_lifecycle = _project_dca_lifecycle(
        dca_lifecycle_source,
        plan=plan,
        completeness_issues=completeness_issues,
    )
    grid_lifecycle = _project_grid_lifecycle(grid_lifecycle_source)
    total_pnl = _finite_or_none(canonical_pnl.get("total_pnl"))
    starting_balance = _finite_or_none(canonical_account.get("starting_balance"))
    return_pct = None
    if total_pnl is not None and starting_balance not in (None, 0.0):
        return_pct = round(total_pnl / starting_balance * 100.0, 8)

    counts = {
        "order_count": len(orders),
        "open_order_count": len(open_orders),
        "accepted_order_count": len(accepted_orders),
        "unknown_order_count": sum(1 for row in orders if not row["state_known"]),
        "open_position_count": len(open_positions),
        "trade_count": _integer_or_none(canonical_counts.get("trade_count")),
        "open_trade_count": _integer_or_none(canonical_counts.get("open_trade_count")),
        "completed_trade_count": sum(1 for row in canonical_trades if row.get("status") == "closed"),
        "completed_round_trip_count": sum(1 for row in canonical_trades if row.get("status") == "closed"),
        "chronology_invalid_trade_count": len(chronology_invalid_trade_ids),
        "fill_count": _integer_or_none(canonical_counts.get("fill_count")),
        "entry_fill_count": _integer_or_none(canonical_counts.get("entry_fill_count")),
        "exit_fill_count": _integer_or_none(canonical_counts.get("exit_fill_count")),
    }
    engine = str(source.get("engine") or "").strip()
    return {
        "schema_version": str(source.get("schema_version") or ""),
        "engine": engine or None,
        "engine_label": _display_name(engine),
        "cycle_id": source.get("cycle_id"),
        "orders": orders,
        "open_orders": open_orders,
        "accepted_orders": accepted_orders,
        "order_summary": {
            "open_buy_order_count": sum(
                1 for row in open_orders if str(_mapping(row).get("side") or "").lower() == "buy"
            ),
            "open_sell_order_count": sum(
                1 for row in open_orders if str(_mapping(row).get("side") or "").lower() == "sell"
            ),
            "accepted_buy_order_count": sum(
                1 for row in accepted_orders if str(_mapping(row).get("side") or "").lower() == "buy"
            ),
            "accepted_sell_order_count": sum(
                1 for row in accepted_orders if str(_mapping(row).get("side") or "").lower() == "sell"
            ),
        },
        "positions": current_positions,
        "open_positions": open_positions,
        "trades": canonical_trades,
        "fills": canonical_fills,
        "dca_lifecycle": dca_lifecycle,
        "grid_lifecycle": grid_lifecycle,
        "counts": counts,
        "scopes": {
            "orders_and_positions": {
                "kind": "current_execution_cycle",
                "cycle_id": source.get("cycle_id"),
                "accounting_snapshot_id": current_accounting.get("snapshot_id"),
                "order_state_source": "current_execution_snapshot",
            },
            "trades_fills_and_pnl": {
                "kind": "all_versioned_production_plans",
                "accounting_snapshot_id": history_accounting.get("snapshot_id"),
            },
        },
        "metrics": {
            "total_notional": _mapping(source.get("trade_summary")).get("total_notional"),
        },
        "pnl": {
            "currency": history_accounting.get("currency"),
            "gross_realized": canonical_pnl.get("gross_realized_pnl"),
            "commission": canonical_pnl.get("commission"),
            "funding": canonical_pnl.get("funding"),
            "net_realized": canonical_pnl.get("net_realized_pnl"),
            "unrealized": canonical_pnl.get("unrealized_pnl"),
            "total": total_pnl,
            "return_pct": return_pct,
        },
        "account": {
            **canonical_account,
            "exposure": current_account.get("exposure"),
            "margin": current_account.get("margin"),
            "slippage": current_account.get("slippage"),
        },
        "accounting": _json_copy(history_accounting),
        "current_accounting": _json_copy(current_accounting),
        "reconciliation": _json_copy(_mapping(source.get("reconciliation"))),
    }


def _project_grid_lifecycle(value: Any) -> dict[str, Any]:
    """Expose only audit-backed Grid lifecycle claims to the operator read model."""

    source = _mapping(value)
    lines = [
        _json_copy(_mapping(row))
        for row in _list(source.get("lines"))
        if _mapping(row)
    ]
    return {
        "schema_version": str(source.get("schema_version") or ""),
        "status": str(source.get("status") or "unavailable"),
        "completed_rearmed_count": _integer_or_none(source.get("completed_rearmed_count")) or 0,
        "unverified_count": _integer_or_none(source.get("unverified_count")) or 0,
        "reconciliation_status": str(source.get("reconciliation_status") or "unknown"),
        "lines": lines,
        "synthetic_candle_fill_inference": False,
    }


def _project_dca_lifecycle(
    value: Any,
    *,
    plan: Mapping[str, Any],
    completeness_issues: list[str],
) -> dict[str, Any] | None:
    if str(plan.get("strategy_type") or "grid").lower() != "dca":
        return None
    lifecycle = _mapping(value)
    if not lifecycle:
        return {
            "status": "not_started",
            "status_label": "尚未建立 DCA 生命周期",
            "active_target": None,
            "target_generations": [],
            "additions_filled": 0,
            "open_quantity": 0.0,
        }
    plan_id = str(plan.get("strategy_plan_id") or "")
    if str(lifecycle.get("strategy_plan_id") or "") != plan_id:
        completeness_issues.append("dca_lifecycle_plan_identity_mismatch")
        return {
            "status": "identity_mismatch",
            "status_label": "DCA 生命周期计划不匹配",
            "active_target": None,
            "target_generations": [],
            "additions_filled": 0,
            "open_quantity": None,
        }

    def target(row: Any) -> dict[str, Any]:
        item = _mapping(row)
        return {
            "target_id": item.get("target_id"),
            "generation": _integer_or_none(item.get("generation")),
            "status": item.get("status"),
            "side": item.get("side"),
            "price": _positive_finite_or_none(item.get("price")),
            "quantity": _positive_finite_or_none(item.get("quantity")),
            "average_entry_price": _positive_finite_or_none(item.get("average_entry_price")),
            "created_at": item.get("created_at"),
            "retired_at": item.get("retired_at"),
            "retire_reason": item.get("retire_reason"),
            "triggered_at": item.get("triggered_at"),
        }

    active = _mapping(lifecycle.get("active_target"))
    status = str(lifecycle.get("status") or "unknown")
    labels = {
        "waiting_entry": "等待加仓成交",
        "open": "聚合止盈已保护",
        "target_triggered": "整轮止盈执行中",
        "target_closed": "整轮止盈已完成",
        "stop_closed": "整轮止损已完成",
        "flattened": "已手动平仓",
    }
    return {
        "status": status,
        "status_label": labels.get(status, "DCA 生命周期状态未知"),
        "round_id": lifecycle.get("round_id"),
        "additions_filled": _integer_or_none(lifecycle.get("additions_filled")) or 0,
        "open_quantity": _finite_or_none(lifecycle.get("open_quantity")),
        "average_entry_price": _positive_finite_or_none(lifecycle.get("average_entry_price")),
        "active_target": target(active) if active else None,
        "target_generations": [target(row) for row in _list(lifecycle.get("target_generations"))],
        "updated_at": lifecycle.get("updated_at"),
        "protection_semantics": "event_driven_aggregate_target_not_entry_order",
    }


def _project_risk(
    runtime_value: Any,
    decision_value: Mapping[str, Any] | None,
    plan: Mapping[str, Any],
    completeness_issues: list[str],
) -> dict[str, Any]:
    runtime = _mapping(runtime_value)
    decision = _json_copy(_mapping(decision_value))
    expected_id = str(runtime.get("risk_decision_id") or "")
    observed_id = str(decision.get("decision_id") or "")
    if decision and expected_id and observed_id == expected_id:
        status = "current"
        current = decision
    elif decision:
        status = "historical"
        current = {}
        completeness_issues.append("risk_decision_id_mismatch")
    else:
        status = "missing"
        current = {}
        completeness_issues.append("risk_decision_missing")
    return {
        "status": status,
        "displayed_decision_id": observed_id or None,
        "expected_decision_id": expected_id or None,
        "current_decision": current,
        "latest_observation": decision,
        "outcome": current.get("outcome"),
        "metrics": _json_copy(_mapping(current.get("metrics"))),
        "limits": _json_copy(_mapping(current.get("limits"))),
        "blockers": _json_copy(_list(current.get("blockers"))),
        "warnings": _json_copy(_list(current.get("warnings"))),
        "plan_budget": _json_copy(_mapping(plan.get("risk_budget"))),
        "authorizes_new_order": False,
    }


def _project_runtime(
    runtime_value: Any,
    *,
    plan: Mapping[str, Any],
    market: Mapping[str, Any],
    open_order_count: int | None,
    unknown_order_count: int,
    risk_status: str,
    completeness_issues: list[str],
) -> dict[str, Any]:
    source = _json_copy(_mapping(runtime_value))
    desired = str(source.get("desired_state") or "stopped")
    actual = str(source.get("actual_state") or desired)
    plan_id = str(plan.get("strategy_plan_id") or "")
    runtime_plan_id = str(source.get("strategy_plan_id") or "")
    inconsistent = unknown_order_count > 0
    execution_tick_health = _json_copy(_mapping(source.get("execution_tick_health")))
    if desired != actual:
        inconsistent = True
        completeness_issues.append("runtime_desired_actual_mismatch")
    if actual == "running" and not plan_id:
        inconsistent = True
        completeness_issues.append("running_without_strategy_plan")
    if actual == "running" and runtime_plan_id and runtime_plan_id != plan_id:
        inconsistent = True
        completeness_issues.append("runtime_strategy_plan_mismatch")
    if actual == "running" and market.get("trusted") is not True:
        inconsistent = True
        completeness_issues.append("running_with_untrusted_market")
    if actual == "running" and execution_tick_health.get("status") == "blocked":
        inconsistent = True
        completeness_issues.append("running_with_execution_tick_unavailable")
    del risk_status
    known_statuses = {
        "starting": "启动中",
        "running": "运行中",
        "replanning": "调整网格中",
        "stopping": "停止中",
        "stopped": "已停止",
        "error": "运行异常",
    }
    status = "degraded" if inconsistent else (actual if actual in known_statuses else "stopped")
    return {
        **source,
        "status": status,
        "status_label": "异常" if status == "degraded" else known_statuses[status],
        "open_order_count": open_order_count,
        "unknown_order_count": unknown_order_count,
        "execution_tick_health": execution_tick_health,
        "liveness_degraded": actual == "running" and execution_tick_health.get("status") == "blocked",
        "can_start_when_authorized": (
            actual in {"stopped", "error"}
            and bool(plan_id)
            and market.get("trusted") is True
            and unknown_order_count == 0
        ),
        "can_stop_when_authorized": (
            actual in {"starting", "running", "replanning", "stopping"}
            or unknown_order_count > 0
        ),
    }


def _project_strategy_summary(
    plan: Mapping[str, Any],
    completeness_issues: list[str],
) -> dict[str, Any]:
    if not plan:
        return {}
    strategy_type = str(plan.get("strategy_type") or "grid").lower()
    if strategy_type == "dca":
        dca = _mapping(plan.get("dca"))
        risk = _mapping(plan.get("risk_budget"))
        entries = [_mapping(row) for row in _list(dca.get("entries"))]
        direction = str(plan.get("direction") or "").lower()
        direction_label = _DIRECTION_LABELS.get(direction, direction or "未知")
        target = _finite_or_none(dca.get("target_price"))
        stop = _finite_or_none(dca.get("stop_price"))
        notional = _finite_or_none(dca.get("notional_per_addition"))
        count = _integer_or_none(dca.get("max_additions"))
        if not entries or target is None or stop is None or count is None:
            completeness_issues.append("strategy_dca_specification_incomplete")
        return {
            "strategy_id": str(plan.get("strategy_id") or "production_dca"),
            "strategy_type": "dca",
            "strategy_type_label": "DCA",
            "plan_id": plan.get("strategy_plan_id"),
            "plan_version": plan.get("version"),
            "status": plan.get("status"),
            "direction": direction or None,
            "direction_label": direction_label,
            "dca_entry_count": count,
            "dca_entry_levels": [
                _finite_or_none(row.get("price")) for row in entries
            ],
            "notional_per_addition": notional,
            "target_price": target,
            "stop_price": stop,
            "loop_enabled": dca.get("loop_enabled") is True,
            "total_possible_notional": _finite_or_none(
                dca.get("total_possible_notional")
            ),
            "max_loss": _finite_or_none(
                risk.get("maximum_loss_at_full_depth")
            ),
            "leverage": _finite_or_none(risk.get("selected_leverage")),
            "actual_leverage": _finite_or_none(
                risk.get("actual_leverage_at_full_depth")
            ),
            "display_label": (
                f"{direction_label} · DCA · 最多 {count if count is not None else '—'} 次 · "
                f"每次 {_format_number(notional)} USD · 目标 {_format_number(target)}"
            ),
        }
    range_value = _mapping(plan.get("range"))
    grid = _mapping(plan.get("grid"))
    risk_budget = _mapping(plan.get("risk_budget"))
    low = _finite_or_none(range_value.get("low"))
    high = _finite_or_none(range_value.get("high"))
    count = _integer_or_none(grid.get("count"))
    width = round(high - low, 8) if low is not None and high is not None else None
    mode = str(grid.get("mode") or "arithmetic").lower()
    spacing = _finite_or_none(grid.get("spacing"))
    spacing_ratio = _finite_or_none(grid.get("spacing_ratio"))
    if mode == "arithmetic" and spacing is None and width is not None and count not in (None, 0):
        spacing = round(width / count, 8)
    if mode == "geometric" and spacing_ratio is None and low not in (None, 0.0) and high is not None and count not in (None, 0):
        spacing_ratio = round((high / low) ** (1.0 / count), 10)
    if low is None or high is None or count is None:
        completeness_issues.append("strategy_geometry_incomplete")

    direction = str(plan.get("direction") or "").lower()
    style = str(plan.get("style") or "").lower()
    direction_label = _DIRECTION_LABELS.get(direction, direction or "未知")
    style_label = _STYLE_LABELS.get(style, style or "未知")
    mode_label = _GRID_MODE_LABELS.get(mode, mode or "未知")
    notional = _finite_or_none(grid.get("notional_per_grid"))
    notional_mode = str(grid.get("notional_mode") or "").lower()
    notional_mode_label = {
        "auto": "自动利润目标",
        "manual": "手动设定",
    }.get(notional_mode, "来源未知")
    leverage = _finite_or_none(grid.get("leverage"))
    if leverage is None:
        leverage = _finite_or_none(risk_budget.get("leverage"))
    actual_leverage = _finite_or_none(risk_budget.get("actual_leverage"))
    min_net_profit = _finite_or_none(grid.get("min_net_profit_per_grid_usd"))
    target_net_profit = _finite_or_none(
        grid.get("target_net_profit_per_grid_usd")
    )
    range_label = f"{_format_number(low)}–{_format_number(high)}"
    display_label = (
        f"{direction_label} · {style_label} · {mode_label} · {range_label} · "
        f"{count if count is not None else '—'} 格 · 每格 {_format_number(notional)} USD"
    )
    return {
        "strategy_id": str(plan.get("strategy_id") or "production_grid"),
        "strategy_type": "grid",
        "strategy_type_label": "Grid",
        "plan_id": plan.get("strategy_plan_id"),
        "plan_version": plan.get("version"),
        "status": plan.get("status"),
        "direction": direction or None,
        "direction_label": direction_label,
        "style": style or None,
        "style_label": style_label,
        "grid_mode": mode or None,
        "grid_mode_label": mode_label,
        "range_low": low,
        "range_high": high,
        "range_width": width,
        "grid_count": count,
        "spacing": spacing,
        "spacing_ratio": spacing_ratio,
        "notional_per_grid": notional,
        "notional_mode": notional_mode or None,
        "notional_mode_label": notional_mode_label,
        "max_loss": _finite_or_none(risk_budget.get("max_loss")),
        "leverage": leverage,
        "actual_leverage": actual_leverage,
        "min_net_profit_per_grid_usd": min_net_profit,
        "target_net_profit_per_grid_usd": target_net_profit,
        "display_label": display_label,
    }


def _project_orders(
    value: Any,
    *,
    plan: Mapping[str, Any],
    plan_history: list[Any],
) -> list[dict[str, Any]]:
    return [
        _project_order(_mapping(row), plan=plan, plan_history=plan_history)
        for row in _list(value)
    ]


def _project_order(
    row: Mapping[str, Any],
    *,
    plan: Mapping[str, Any],
    plan_history: list[Any],
) -> dict[str, Any]:
    state = _normalize_order_state(row.get("state") or row.get("status"))
    rank, label = _ORDER_STATE_PRESENTATION.get(state, (0, "未知状态"))
    order_type = str(row.get("order_type") or row.get("type") or "").strip().lower()
    price = _finite_or_none(row.get("price"))
    requested_price = _finite_or_none(row.get("requested_price"))
    source_plan, _source_issue = _source_plan_for_order(
        row,
        plan=plan,
        plan_history=plan_history,
    )
    plan_order = (
        _matching_plan_order(row, plan=source_plan)
        if source_plan is not None
        else None
    )
    return {
        **_json_copy(row),
        "price": price,
        "requested_price": requested_price,
        "price_status": (
            "known"
            if price is not None
            else "missing_market_order_execution_price"
            if order_type == "market"
            else "missing"
        ),
        "state": state or "unknown",
        "state_label": label,
        "state_rank": rank,
        "state_revision": _order_state_revision(row, state),
        "state_known": state in _KNOWN_ORDER_STATES,
        "state_source": "current_execution_snapshot",
        "is_open": state in OPEN_ORDER_STATES,
        "is_accepted": state in _ACCEPTED_ORDER_STATES,
        "is_terminal": state in TERMINAL_STATES,
        "planned_net_profit_usd": _finite_or_none(
            _mapping(plan_order).get("planned_net_profit_usd")
        ),
        "protection": _project_order_protection(
            row,
            plan=plan,
            plan_history=plan_history,
        ),
    }


def _project_order_protection(
    row: Mapping[str, Any],
    *,
    plan: Mapping[str, Any],
    plan_history: list[Any],
) -> dict[str, Any]:
    unknown = {"status": "unknown", "tp": None, "sl": None, "source": None}
    source_plan, source_issue = _source_plan_for_order(
        row,
        plan=plan,
        plan_history=plan_history,
    )
    if source_plan is None:
        return {**unknown, "reason": source_issue}

    tp = _positive_finite_or_none(row.get("tp"))
    sl = _positive_finite_or_none(row.get("sl"))
    source = "execution_snapshot" if tp is not None or sl is not None else None
    if tp is None or sl is None:
        match = _matching_plan_order(row, plan=source_plan)
        if match is not None:
            tp = tp if tp is not None else _positive_finite_or_none(match.get("tp"))
            sl = sl if sl is not None else _positive_finite_or_none(match.get("sl"))
            source = "strategy_plan" if source is None else "execution_snapshot+strategy_plan"
    if str(source_plan.get("strategy_type") or "grid").lower() == "dca":
        dca = _mapping(source_plan.get("dca"))
        tp = tp if tp is not None else _positive_finite_or_none(dca.get("target_price"))
        sl = sl if sl is not None else _positive_finite_or_none(dca.get("stop_price"))
        if tp is not None and sl is not None and source is None:
            source = "strategy_plan_aggregate_dca"
    if tp is None or sl is None:
        return {**unknown, "reason": "strategy_plan_protection_incomplete"}
    return {"status": "known", "tp": tp, "sl": sl, "source": source, "reason": None}


def _source_plan_for_order(
    row: Mapping[str, Any],
    *,
    plan: Mapping[str, Any],
    plan_history: list[Any],
) -> tuple[Mapping[str, Any] | None, str | None]:
    plan_id = str(plan.get("strategy_plan_id") or "").strip()
    order_plan_id = str(row.get("strategy_plan_id") or "").strip()
    if not plan_id or not order_plan_id:
        return None, "strategy_plan_id_missing"
    source_plan = plan
    if order_plan_id != plan_id:
        inherited_ids = {
            str(value)
            for value in plan.get("inherited_plan_ids") or []
            if str(value)
        }
        if order_plan_id not in inherited_ids:
            return None, "strategy_plan_id_mismatch"
        matches = [
            _mapping(candidate)
            for candidate in plan_history
            if str(_mapping(candidate).get("strategy_plan_id") or "") == order_plan_id
        ]
        if len(matches) != 1:
            return None, "inherited_strategy_plan_missing"
        source_plan = matches[0]
    identity_valid, _preview_id = _plan_order_identity(row, plan=source_plan)
    if not identity_valid:
        return None, "strategy_plan_order_identity_mismatch"
    return source_plan, None


def _project_position(
    row: Mapping[str, Any],
    *,
    orders_by_id: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    projected = _json_copy(row)
    order_id = str(
        row.get("entry_order_id")
        or row.get("source_order_id")
        or row.get("trade_id")
        or ""
    )
    order = _mapping(orders_by_id.get(order_id))
    protection = _json_copy(_mapping(order.get("protection")))
    if not protection:
        tp = _positive_finite_or_none(row.get("tp"))
        sl = _positive_finite_or_none(row.get("sl"))
        protection = {
            "status": "known" if tp is not None and sl is not None else "unknown",
            "tp": tp,
            "sl": sl,
            "source": "position" if tp is not None and sl is not None else None,
            "reason": None if tp is not None and sl is not None else "entry_order_protection_unavailable",
        }
    remaining_quantity = row.get("remaining_quantity")
    if remaining_quantity is None:
        remaining_quantity = row.get("remaining_units")
    if remaining_quantity is None:
        remaining_quantity = row.get("quantity")
    return {
        **projected,
        "remaining_quantity": remaining_quantity,
        "protection": protection,
    }


def _matching_plan_order(
    row: Mapping[str, Any],
    *,
    plan: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    is_dca = str(plan.get("strategy_type") or "grid").lower() == "dca"
    candidates = [
        _mapping(item)
        for item in _list(
            _mapping(plan.get("dca")).get("entries")
            if is_dca
            else _mapping(plan.get("grid")).get("orders")
        )
    ]
    identity_valid, preview_id = _plan_order_identity(row, plan=plan)
    if not identity_valid:
        return None
    if preview_id:
        identity_field = "preview_entry_id" if is_dca else "preview_order_id"
        identified = [item for item in candidates if str(item.get(identity_field) or "") == preview_id]
        return identified[0] if len(identified) == 1 else None

    side = str(row.get("side") or "").strip().lower()
    price = _finite_or_none(row.get("price"))
    if not side or price is None:
        return None
    matched = [
        item
        for item in candidates
        if str(item.get("side") or "").strip().lower() == side
        and _same_price(price, item.get("price"))
    ]
    return matched[0] if len(matched) == 1 else None


def _plan_order_identity(
    row: Mapping[str, Any],
    *,
    plan: Mapping[str, Any],
) -> tuple[bool, str]:
    is_dca = str(plan.get("strategy_type") or "grid").lower() == "dca"
    explicit_preview_id = str(
        (
            row.get("preview_entry_id")
            if is_dca
            else row.get("preview_order_id")
        )
        or ""
    ).strip()
    source_fill_id = str(row.get("source_fill_id") or "").strip()
    source_preview_id = ""
    if source_fill_id:
        identity = source_fill_id.split(":")
        identity_valid = (
            len(identity) == 3
            and identity[0] == ("strategy-dca" if is_dca else "strategy-grid")
            and identity[1] == str(plan.get("strategy_plan_id") or "")
            and bool(identity[2])
        )
        if not identity_valid:
            return False, ""
        source_preview_id = identity[2]
    if explicit_preview_id and source_preview_id and explicit_preview_id != source_preview_id:
        return False, ""
    preview_id = explicit_preview_id or source_preview_id
    if preview_id:
        candidates = _list(
            _mapping(plan.get("dca")).get("entries")
            if is_dca
            else _mapping(plan.get("grid")).get("orders")
        )
        identity_field = "preview_entry_id" if is_dca else "preview_order_id"
        matches = [
            item
            for item in candidates
            if str(_mapping(item).get(identity_field) or "") == preview_id
        ]
        if len(matches) != 1:
            return False, ""
    return True, preview_id


def _same_price(left: float, right: Any) -> bool:
    parsed = _finite_or_none(right)
    return parsed is not None and abs(left - parsed) <= max(0.00000001, abs(left) * 0.000000001)


def _project_trade_lifecycles(
    value: Any,
    *,
    fills: list[dict[str, Any]],
    chronology_invalid_trade_ids: set[str],
) -> list[dict[str, Any]]:
    fills_by_id = {
        str(row.get("fill_id") or ""): row
        for row in fills
        if str(row.get("fill_id") or "")
    }
    projected: list[dict[str, Any]] = []
    for raw in _list(value):
        trade = _json_copy(_mapping(raw))
        if str(trade.get("trade_id") or "") in chronology_invalid_trade_ids:
            projected.append({
                **trade,
                "status": "chronology_invalid",
                "chronology_status": "invalid",
                "chronology_issue": "closed_trade_exit_before_entry",
                "close_reason": None,
                "close_reason_label": "时间异常",
            })
            continue
        exit_ids = _list(trade.get("exit_fill_ids"))
        if exit_ids:
            matching_fills = [_mapping(fills_by_id.get(str(fill_id))) for fill_id in exit_ids]
        else:
            trade_id = str(trade.get("trade_id") or "")
            matching_fills = [
                row
                for row in fills
                if trade_id and str(row.get("trade_id") or "") == trade_id
            ]
        exit_events = [
            str(row.get("event") or "").strip().lower()
            for row in matching_fills
            if str(row.get("event") or "").strip().lower() not in {"", "entry"}
        ]
        event = next((item for item in reversed(exit_events) if item), "")
        if event in _TP_EVENTS:
            reason, label = "tp", "TP"
        elif event in _SL_EVENTS:
            reason, label = "sl", "SL"
        elif event in _MANUAL_EXIT_EVENTS:
            reason, label = "manual", "手动平仓"
        else:
            reason, label = None, "未知" if str(trade.get("status") or "") == "closed" else "持仓中"
        projected.append({**trade, "close_reason": reason, "close_reason_label": label})
    return projected


def _order_state_revision(row: Mapping[str, Any], state: str) -> int | None:
    """Return a comparable revision only when transition history proves it."""

    transitions = row.get("transitions")
    if not isinstance(transitions, (list, tuple)) or not transitions:
        return None
    previous = ""
    for index, transition in enumerate(transitions):
        if not isinstance(transition, Mapping):
            return None
        from_state = _normalize_order_state(transition.get("from"))
        to_state = _normalize_order_state(transition.get("to"))
        if index == 0:
            if from_state or to_state != "entry":
                return None
        elif from_state != previous or to_state not in LEGAL_TRANSITIONS.get(from_state, set()):
            return None
        previous = to_state
    if previous != state:
        return None
    return len(transitions)


def _normalize_order_state(value: Any) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_")
    aliases = {
        "canceled": "cancelled",
        "partiallyfilled": "partially_filled",
    }
    return aliases.get(normalized, normalized)


def _snapshot_id(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        _json_copy(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return f"trading-system-{hashlib.sha256(encoded).hexdigest()}"


def _display_name(value: str) -> str | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    return " ".join(part.capitalize() for part in normalized.replace("-", "_").split("_") if part)


def _format_number(value: Any) -> str:
    parsed = _finite_or_none(value)
    if parsed is None:
        return "—"
    return f"{parsed:.8f}".rstrip("0").rstrip(".")


def _finite_or_none(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _positive_finite_or_none(value: Any) -> float | None:
    parsed = _finite_or_none(value)
    return parsed if parsed is not None and parsed > 0 else None


def _integer_or_none(value: Any) -> int | None:
    parsed = _finite_or_none(value)
    return int(parsed) if parsed is not None and parsed >= 0 and parsed.is_integer() else None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _json_copy(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_copy(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_copy(item) for item in value]
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if value is None or isinstance(value, (str, int, bool)):
        return value
    return str(value)


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value
