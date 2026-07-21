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
    plan: Mapping[str, Any],
) -> dict[str, Any]:
    orders = _project_orders(source.get("orders"), plan=plan)
    open_orders = [row for row in orders if row["is_open"]]
    accepted_orders = [row for row in orders if row["is_accepted"]]
    current_positions = _json_copy(_list(current_accounting.get("positions")))
    open_positions = [
        row
        for row in current_positions
        if str(_mapping(row).get("status") or "") == "open"
    ]
    canonical_fills = _json_copy(_list(history_accounting.get("fills")))
    canonical_trades = _project_trade_lifecycles(
        history_accounting.get("trades"),
        fills=canonical_fills,
    )
    canonical_counts = _mapping(history_accounting.get("counts"))
    canonical_pnl = _json_copy(_mapping(history_accounting.get("pnl")))
    canonical_account = _json_copy(_mapping(history_accounting.get("account")))
    current_account = _json_copy(_mapping(source.get("account")))
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
        "unknown_order_count": unknown_order_count,
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


def _project_orders(value: Any, *, plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [_project_order(_mapping(row), plan=plan) for row in _list(value)]


def _project_order(row: Mapping[str, Any], *, plan: Mapping[str, Any]) -> dict[str, Any]:
    state = _normalize_order_state(row.get("state") or row.get("status"))
    rank, label = _ORDER_STATE_PRESENTATION.get(state, (0, "未知状态"))
    return {
        **_json_copy(row),
        "state": state or "unknown",
        "state_label": label,
        "state_rank": rank,
        "state_revision": _order_state_revision(row, state),
        "state_known": state in _KNOWN_ORDER_STATES,
        "state_source": "current_execution_snapshot",
        "is_open": state in OPEN_ORDER_STATES,
        "is_accepted": state in _ACCEPTED_ORDER_STATES,
        "is_terminal": state in TERMINAL_STATES,
        "protection": _project_order_protection(row, plan=plan),
    }


def _project_order_protection(
    row: Mapping[str, Any],
    *,
    plan: Mapping[str, Any],
) -> dict[str, Any]:
    plan_id = str(plan.get("strategy_plan_id") or "").strip()
    order_plan_id = str(row.get("strategy_plan_id") or "").strip()
    unknown = {"status": "unknown", "tp": None, "sl": None, "source": None}
    if not plan_id or not order_plan_id:
        return {**unknown, "reason": "strategy_plan_id_missing"}
    if order_plan_id != plan_id:
        return {**unknown, "reason": "strategy_plan_id_mismatch"}
    identity_valid, _preview_id = _plan_order_identity(row, plan=plan)
    if not identity_valid:
        return {**unknown, "reason": "strategy_plan_order_identity_mismatch"}

    tp = _positive_finite_or_none(row.get("tp"))
    sl = _positive_finite_or_none(row.get("sl"))
    source = "execution_snapshot" if tp is not None or sl is not None else None
    if tp is None or sl is None:
        match = _matching_plan_order(row, plan=plan)
        if match is not None:
            tp = tp if tp is not None else _positive_finite_or_none(match.get("tp"))
            sl = sl if sl is not None else _positive_finite_or_none(match.get("sl"))
            source = "strategy_plan" if source is None else "execution_snapshot+strategy_plan"
    if tp is None or sl is None:
        return {**unknown, "reason": "strategy_plan_protection_incomplete"}
    return {"status": "known", "tp": tp, "sl": sl, "source": source, "reason": None}


def _matching_plan_order(
    row: Mapping[str, Any],
    *,
    plan: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    candidates = [_mapping(item) for item in _list(_mapping(plan.get("grid")).get("orders"))]
    identity_valid, preview_id = _plan_order_identity(row, plan=plan)
    if not identity_valid:
        return None
    if preview_id:
        identified = [item for item in candidates if str(item.get("preview_order_id") or "") == preview_id]
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
    explicit_preview_id = str(row.get("preview_order_id") or "").strip()
    source_fill_id = str(row.get("source_fill_id") or "").strip()
    source_preview_id = ""
    if source_fill_id:
        identity = source_fill_id.split(":")
        identity_valid = (
            len(identity) == 3
            and identity[0] == "strategy-grid"
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
        candidates = _list(_mapping(plan.get("grid")).get("orders"))
        matches = [
            item
            for item in candidates
            if str(_mapping(item).get("preview_order_id") or "") == preview_id
        ]
        if len(matches) != 1:
            return False, ""
    return True, preview_id


def _same_price(left: float, right: Any) -> bool:
    parsed = _finite_or_none(right)
    return parsed is not None and abs(left - parsed) <= max(0.00000001, abs(left) * 0.000000001)


def _project_trade_lifecycles(value: Any, *, fills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    fills_by_id = {
        str(row.get("fill_id") or ""): row
        for row in fills
        if str(row.get("fill_id") or "")
    }
    projected: list[dict[str, Any]] = []
    for raw in _list(value):
        trade = _json_copy(_mapping(raw))
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
