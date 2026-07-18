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
_OPEN_ORDER_STATES = {
    "accepted",
    "open",
    "partially_filled",
    "pending",
    "submitted",
    "working",
}


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
    )
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
        risk_status=risk["status"],
        completeness_issues=completeness_issues,
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
        },
        "research": {
            "strategy_shadows": _json_copy(_list(source.get("strategy_shadows"))),
            "execution_shadow": _json_copy(_mapping(source.get("execution_shadow"))),
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
) -> dict[str, Any]:
    orders = _json_copy(_list(source.get("orders")))
    open_orders = [row for row in orders if _is_open_order(row)]
    current_positions = _json_copy(_list(current_accounting.get("positions")))
    open_positions = [
        row
        for row in current_positions
        if str(_mapping(row).get("status") or "") == "open"
    ]
    canonical_trades = _json_copy(_list(history_accounting.get("trades")))
    canonical_counts = _mapping(history_accounting.get("counts"))
    current_counts = _mapping(current_accounting.get("counts"))
    canonical_pnl = _json_copy(_mapping(history_accounting.get("pnl")))
    canonical_account = _json_copy(_mapping(history_accounting.get("account")))
    current_account = _json_copy(_mapping(source.get("account")))
    total_pnl = _finite_or_none(canonical_pnl.get("total_pnl"))
    starting_balance = _finite_or_none(canonical_account.get("starting_balance"))
    return_pct = None
    if total_pnl is not None and starting_balance not in (None, 0.0):
        return_pct = round(total_pnl / starting_balance * 100.0, 8)

    counts = {
        "open_order_count": len(open_orders),
        "open_position_count": _integer_or_none(current_counts.get("open_position_count")),
        "trade_count": _integer_or_none(canonical_counts.get("trade_count")),
        "open_trade_count": _integer_or_none(canonical_counts.get("open_trade_count")),
        "completed_trade_count": _integer_or_none(canonical_counts.get("completed_trade_count")),
        "completed_round_trip_count": _integer_or_none(canonical_counts.get("completed_trade_count")),
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
        "order_summary": {
            "open_buy_order_count": sum(
                1 for row in open_orders if str(_mapping(row).get("side") or "").lower() == "buy"
            ),
            "open_sell_order_count": sum(
                1 for row in open_orders if str(_mapping(row).get("side") or "").lower() == "sell"
            ),
        },
        "positions": current_positions,
        "open_positions": open_positions,
        "trades": canonical_trades,
        "fills": _json_copy(_list(history_accounting.get("fills"))),
        "counts": counts,
        "scopes": {
            "orders_and_positions": {
                "kind": "current_execution_cycle",
                "cycle_id": source.get("cycle_id"),
                "accounting_snapshot_id": current_accounting.get("snapshot_id"),
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
    risk_status: str,
    completeness_issues: list[str],
) -> dict[str, Any]:
    source = _json_copy(_mapping(runtime_value))
    desired = str(source.get("desired_state") or "stopped")
    actual = str(source.get("actual_state") or desired)
    plan_id = str(plan.get("strategy_plan_id") or "")
    runtime_plan_id = str(source.get("strategy_plan_id") or "")
    inconsistent = False
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
        "can_start_when_authorized": actual in {"stopped", "error"} and bool(plan_id) and market.get("trusted") is True,
        "can_stop_when_authorized": actual in {"starting", "running", "replanning", "stopping"},
    }


def _project_strategy_summary(
    plan: Mapping[str, Any],
    completeness_issues: list[str],
) -> dict[str, Any]:
    if not plan:
        return {}
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
    leverage = _finite_or_none(grid.get("leverage"))
    if leverage is None:
        leverage = _finite_or_none(risk_budget.get("leverage"))
    range_label = f"{_format_number(low)}–{_format_number(high)}"
    display_label = (
        f"{direction_label} · {style_label} · {mode_label} · {range_label} · "
        f"{count if count is not None else '—'} 格 · 每格 {_format_number(notional)} USD"
    )
    return {
        "strategy_id": str(plan.get("strategy_id") or "production_grid"),
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
        "leverage": leverage,
        "display_label": display_label,
    }


def _is_open_order(value: Any) -> bool:
    row = _mapping(value)
    state = str(row.get("state") or row.get("status") or "").lower()
    return state in _OPEN_ORDER_STATES


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
    if value is None or isinstance(value, (str, int, float, bool)):
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
