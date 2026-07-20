"""Provider-free projection from execution-engine facts to canonical accounting truth."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import Any

from schemas.accounting import AccountingSnapshot, build_accounting_snapshot


EXECUTION_SNAPSHOT_SCHEMA = "dualtrack-execution-v1"
ENTRY_EVENTS = {"entry"}
EXIT_EVENTS = {"exit", "stop", "target", "flatten"}
OPEN_ORDER_STATES = {"accepted", "new", "open", "pending", "submitted", "working", "partially_filled"}
_MONEY_DECIMALS = 8
_QUANTITY_DECIMALS = 10


class AccountingContractError(ValueError):
    """Raised when source facts cannot be projected without choosing a truth."""


def project_execution_accounting(
    source: Mapping[str, Any],
    *,
    currency: str | None = None,
    source_type: str = "execution_engine",
    scope: Mapping[str, Any] | None = None,
) -> AccountingSnapshot:
    """Project one Legacy or Nautilus execution snapshot without side effects."""

    if not isinstance(source, Mapping):
        raise AccountingContractError("execution accounting source must be an object")
    schema_version = _required_text(source.get("schema_version"), "execution schema_version")
    if schema_version != EXECUTION_SNAPSHOT_SCHEMA:
        raise AccountingContractError(f"unsupported execution accounting schema: {schema_version}")
    engine = _required_text(source.get("engine"), "execution engine")
    cycle_id = _required_text(source.get("cycle_id"), "execution cycle_id")
    raw_orders = _required_rows(source, "orders")
    raw_fills = _required_rows(source, "fills")
    raw_positions = _required_rows(source, "positions")

    issues: list[dict[str, Any]] = []
    orders = _canonical_orders(raw_orders, issues)
    fills = _canonical_fills(raw_fills, issues)
    positions = _canonical_positions(raw_positions, fills, issues)
    trades = _canonical_trades(positions, fills, issues)
    counts = _counts(orders, fills, positions, trades)
    pnl, pnl_observation = _execution_pnl(source, fills, positions, issues)
    account, account_observation = _execution_account(source, pnl, issues)
    completeness = _execution_completeness(
        source,
        pnl_observation=pnl_observation,
        account_observation=account_observation,
        positions=positions,
    )
    reconciliation = {
        "status": "pass" if not issues else "drift",
        "issues": _sorted_issues(issues),
        "identity_policy": "dedupe_exact_reject_conflict",
        "accounting_identity": (
            "net_realized_pnl = gross_realized_pnl - fees + funding; "
            "ending_cash = starting_balance + net_realized_pnl; "
            "equity = ending_cash + unrealized_pnl"
        ),
    }
    source_account = source.get("account") if isinstance(source.get("account"), Mapping) else {}
    resolved_currency = str(currency or source_account.get("currency") or "USDT").strip().upper()
    return build_accounting_snapshot(
        source_type=source_type,
        source_name=engine,
        source_schema_version=schema_version,
        scope=dict(scope or {"cycle_id": cycle_id}),
        currency=resolved_currency,
        orders=orders,
        fills=fills,
        positions=positions,
        trades=trades,
        counts=counts,
        pnl=pnl,
        account=account,
        completeness=completeness,
        reconciliation=reconciliation,
    )


def _canonical_orders(rows: list[Mapping[str, Any]], issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        order_id = _required_text(row.get("order_id"), "order_id")
        state = _order_state(row.get("state", row.get("status")))
        canonical = _drop_none({
            "order_id": order_id,
            "state": state,
            "side": _side(row.get("side"), allow_blank=True),
            "event": _event(row.get("event"), allow_unknown=True),
            "order_type": str(row.get("order_type") or row.get("type") or "").strip().lower(),
            "price": _optional_number(row.get("price"), "order price", decimals=_MONEY_DECIMALS),
            "quantity": _optional_number(row.get("quantity"), "order quantity", decimals=_QUANTITY_DECIMALS),
            "ts": _optional_text(row.get("ts")),
            "strategy_plan_id": _optional_text(row.get("strategy_plan_id")),
            "strategy_plan_version": _optional_int(row.get("strategy_plan_version"), "strategy_plan_version"),
        })
        _insert_identity(by_id, order_id, canonical, "order", issues)
    return sorted(by_id.values(), key=lambda row: (str(row.get("ts") or ""), row["order_id"]))


def _canonical_fills(rows: list[Mapping[str, Any]], issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        fill_id = _required_text(row.get("fill_id") or row.get("id"), "fill_id")
        price = _positive_number(row.get("price"), "fill price", decimals=_MONEY_DECIMALS)
        quantity = _fill_quantity(row, price=price)
        event = _event(row.get("event"), allow_unknown=True)
        trade_id = _optional_text(row.get("trade_id"))
        if event in ENTRY_EVENTS | EXIT_EVENTS and not trade_id:
            issues.append({"code": "fill_trade_id_missing", "fill_id": fill_id, "event": event})
        fee_present = row.get("cost") not in (None, "") or row.get("commission") not in (None, "")
        fee = _optional_number(
            row.get("cost", row.get("commission")),
            "fill fee",
            decimals=_MONEY_DECIMALS,
        )
        if fee is not None and fee < 0:
            raise AccountingContractError("fill fee must be non-negative")
        net_realized = _optional_number(row.get("realized_pnl"), "fill realized_pnl", decimals=_MONEY_DECIMALS)
        gross_realized = _optional_number(row.get("gross_pnl"), "fill gross_pnl", decimals=_MONEY_DECIMALS)
        if gross_realized is None and net_realized is not None and fee is not None:
            gross_realized = _money(net_realized + fee)
        matched_trade_ids = sorted({
            str(item.get("trade_id") or "").strip()
            for item in (row.get("matched_entries") or [])
            if isinstance(item, Mapping) and str(item.get("trade_id") or "").strip()
        })
        canonical = _drop_none({
            "fill_id": fill_id,
            "order_id": _optional_text(row.get("order_id") or row.get("external_order_id")),
            "source_fill_id": _optional_text(row.get("source_fill_id")),
            "trade_id": trade_id,
            "event": event,
            "side": _side(row.get("side"), allow_blank=False),
            "price": price,
            "quantity": quantity,
            "notional": _optional_number(row.get("notional"), "fill notional", decimals=_MONEY_DECIMALS)
            or _money(price * quantity),
            "gross_realized_pnl": gross_realized,
            "fee": fee,
            "fee_observed": fee_present,
            "net_realized_pnl": net_realized,
            "slippage": _optional_number(row.get("slippage"), "fill slippage", decimals=_MONEY_DECIMALS),
            "ts": _optional_text(row.get("ts") or row.get("time")),
            "matched_trade_ids": matched_trade_ids,
            "strategy_plan_id": _optional_text(row.get("strategy_plan_id")),
            "strategy_plan_version": _optional_int(row.get("strategy_plan_version"), "strategy_plan_version"),
        })
        _insert_identity(by_id, fill_id, canonical, "fill", issues)
    priority = {"entry": 0, "exit": 1, "target": 1, "stop": 1, "flatten": 1, "unknown": 9}
    return sorted(
        by_id.values(),
        key=lambda row: (str(row.get("ts") or ""), priority.get(str(row.get("event")), 9), row["fill_id"]),
    )


def _canonical_positions(
    rows: list[Mapping[str, Any]],
    fills: list[dict[str, Any]],
    issues: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    entry_fills: dict[str, list[dict[str, Any]]] = {}
    exit_fills: dict[str, list[dict[str, Any]]] = {}
    for fill in fills:
        trade_id = str(fill.get("trade_id") or "")
        if not trade_id:
            continue
        target = entry_fills if fill.get("event") in ENTRY_EVENTS else exit_fills if fill.get("event") in EXIT_EVENTS else None
        if target is not None:
            target.setdefault(trade_id, []).append(fill)

    by_trade: dict[str, dict[str, Any]] = {}
    for row in rows:
        trade_id = _required_text(row.get("trade_id") or row.get("position_id"), "position trade_id")
        position_id = _required_text(row.get("position_id") or trade_id, "position_id")
        status = str(row.get("status") or row.get("position_status") or "open").strip().lower()
        if status not in {"open", "closed"}:
            raise AccountingContractError(f"unsupported position status: {status}")
        entries = entry_fills.get(trade_id, [])
        exits = exit_fills.get(trade_id, [])
        quantity = _first_number(
            row,
            ("units", "quantity", "entry_quantity"),
            "position quantity",
            decimals=_QUANTITY_DECIMALS,
        )
        if quantity is None and entries:
            quantity = _quantity(sum(float(item["quantity"]) for item in entries))
        remaining = _first_number(
            row,
            ("remaining_units", "remaining_quantity"),
            "position remaining quantity",
            decimals=_QUANTITY_DECIMALS,
        )
        if remaining is None:
            remaining = 0.0 if status == "closed" else quantity
        if quantity is None:
            quantity = remaining
        if quantity is None:
            raise AccountingContractError(f"position {trade_id} has no quantity evidence")
        if remaining is None:
            raise AccountingContractError(f"position {trade_id} has no remaining quantity evidence")
        if remaining < 0:
            issues.append({"code": "negative_position_quantity", "trade_id": trade_id, "remaining_quantity": remaining})
        if status == "closed" and remaining != 0:
            issues.append({"code": "closed_trade_has_remaining_quantity", "trade_id": trade_id, "remaining_quantity": remaining})
        if status == "open" and remaining <= 0:
            issues.append({"code": "open_trade_has_no_remaining_quantity", "trade_id": trade_id})
        canonical = _drop_none({
            "position_id": position_id,
            "trade_id": trade_id,
            "status": status,
            "side": _position_side(row.get("side")),
            "quantity": quantity,
            "remaining_quantity": remaining,
            "entry_price": _optional_number(row.get("entry_price"), "position entry_price", decimals=_MONEY_DECIMALS),
            "exit_price": _optional_number(row.get("exit_price"), "position exit_price", decimals=_MONEY_DECIMALS),
            "entry_ts": _optional_text(row.get("entry_ts")),
            "exit_ts": _optional_text(row.get("exit_ts")),
            "realized_pnl": _optional_number(row.get("realized_pnl"), "position realized_pnl", decimals=_MONEY_DECIMALS),
            "unrealized_pnl": _optional_number(row.get("unrealized_pnl"), "position unrealized_pnl", decimals=_MONEY_DECIMALS),
            "entry_fill_ids": [item["fill_id"] for item in entries],
            "exit_fill_ids": [item["fill_id"] for item in exits],
            "strategy_plan_id": _optional_text(row.get("strategy_plan_id")),
            "strategy_plan_version": _optional_int(row.get("strategy_plan_version"), "strategy_plan_version"),
        })
        _insert_identity(by_trade, trade_id, canonical, "position", issues)
    return sorted(by_trade.values(), key=lambda row: (str(row.get("entry_ts") or ""), row["trade_id"]))


def _canonical_trades(
    positions: list[dict[str, Any]],
    fills: list[dict[str, Any]],
    issues: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    position_by_trade = {str(row["trade_id"]): row for row in positions}
    for fill in fills:
        event = str(fill.get("event") or "")
        trade_id = str(fill.get("trade_id") or "")
        if not trade_id or event not in ENTRY_EVENTS | EXIT_EVENTS:
            continue
        if trade_id not in position_by_trade:
            issues.append({
                "code": "orphan_exit_fill" if event in EXIT_EVENTS else "entry_fill_without_position",
                "fill_id": fill["fill_id"],
                "trade_id": trade_id,
            })

    trades = []
    for position in positions:
        entry_ids = list(position.get("entry_fill_ids") or [])
        exit_ids = list(position.get("exit_fill_ids") or [])
        if not entry_ids:
            issues.append({"code": "trade_entry_fill_missing", "trade_id": position["trade_id"]})
        if position["status"] == "closed" and not exit_ids:
            issues.append({"code": "closed_trade_exit_fill_missing", "trade_id": position["trade_id"]})
        quantity = float(position.get("quantity") or 0.0)
        remaining = float(position.get("remaining_quantity") or 0.0)
        trades.append(_drop_none({
            "trade_id": position["trade_id"],
            "position_id": position["position_id"],
            "status": position["status"],
            "side": position.get("side"),
            "entry_fill_ids": entry_ids,
            "exit_fill_ids": exit_ids,
            "entry_fill_count": len(entry_ids),
            "exit_fill_count": len(exit_ids),
            "entry_quantity": _quantity(quantity),
            "closed_quantity": _quantity(max(0.0, quantity - remaining)),
            "remaining_quantity": _quantity(remaining),
            "entry_price": position.get("entry_price"),
            "exit_price": position.get("exit_price"),
            "entry_ts": position.get("entry_ts"),
            "exit_ts": position.get("exit_ts"),
            "realized_pnl": position.get("realized_pnl"),
            "unrealized_pnl": position.get("unrealized_pnl"),
            "strategy_plan_id": position.get("strategy_plan_id"),
            "strategy_plan_version": position.get("strategy_plan_version"),
        }))
    return trades


def _counts(
    orders: list[dict[str, Any]],
    fills: list[dict[str, Any]],
    positions: list[dict[str, Any]],
    trades: list[dict[str, Any]],
) -> dict[str, int]:
    return {
        "order_count": len(orders),
        "open_order_count": sum(1 for row in orders if row.get("state") in OPEN_ORDER_STATES),
        "fill_count": len(fills),
        "entry_fill_count": sum(1 for row in fills if row.get("event") in ENTRY_EVENTS),
        "exit_fill_count": sum(1 for row in fills if row.get("event") in EXIT_EVENTS),
        "trade_count": len(trades),
        "open_trade_count": sum(1 for row in trades if row.get("status") == "open"),
        "completed_trade_count": sum(1 for row in trades if row.get("status") == "closed"),
        "position_count": len(positions),
        "open_position_count": sum(1 for row in positions if row.get("status") == "open"),
    }


def _execution_pnl(
    source: Mapping[str, Any],
    fills: list[dict[str, Any]],
    positions: list[dict[str, Any]],
    issues: list[dict[str, Any]],
) -> tuple[dict[str, float | None], dict[str, bool]]:
    source_pnl = source.get("pnl") if isinstance(source.get("pnl"), Mapping) else {}
    source_account = source.get("account") if isinstance(source.get("account"), Mapping) else {}
    realized = _optional_number(
        source_pnl.get("realized", source_account.get("realized_pnl")),
        "execution realized P&L",
        decimals=_MONEY_DECIMALS,
    )
    fill_realized_known = all(row.get("net_realized_pnl") is not None for row in fills)
    fill_realized = _money(sum(float(row.get("net_realized_pnl") or 0.0) for row in fills)) if fill_realized_known else None
    position_realized_known = all(row.get("realized_pnl") is not None for row in positions)
    position_realized = (
        _money(sum(float(row.get("realized_pnl") or 0.0) for row in positions))
        if position_realized_known
        else None
    )
    if realized is None:
        realized = fill_realized if fill_realized is not None else position_realized
    if realized is None:
        raise AccountingContractError("execution snapshot has no realized P&L evidence")
    if fill_realized is not None and fill_realized != realized:
        issues.append({"code": "fill_realized_pnl_mismatch", "fills": fill_realized, "snapshot": realized})

    fill_fees_known = all(row.get("fee_observed") is True and row.get("fee") is not None for row in fills)
    fill_fees = _money(sum(float(row.get("fee") or 0.0) for row in fills)) if fill_fees_known else None
    account_fees = _optional_number(source_account.get("fees"), "execution fees", decimals=_MONEY_DECIMALS)
    fees = account_fees if account_fees is not None else fill_fees
    if account_fees is not None and fill_fees is not None and account_fees != fill_fees:
        issues.append({"code": "account_fees_mismatch", "account": account_fees, "fills": fill_fees})

    funding_present = "funding" in source_account and source_account.get("funding") not in (None, "")
    funding = _optional_number(source_account.get("funding"), "execution funding", decimals=_MONEY_DECIMALS)
    fill_gross_known = all(row.get("gross_realized_pnl") is not None for row in fills)
    fill_gross = _money(sum(float(row.get("gross_realized_pnl") or 0.0) for row in fills)) if fill_gross_known else None
    gross = fill_gross
    if gross is None and fees is not None and funding is not None:
        gross = _money(realized + fees - funding)
    if gross is not None and fees is not None and funding is not None:
        formula_net = _money(gross - fees + funding)
        if formula_net != realized:
            issues.append({"code": "net_realized_pnl_formula_mismatch", "formula": formula_net, "snapshot": realized})

    unrealized = _optional_number(source_pnl.get("unrealized"), "execution unrealized P&L", decimals=_MONEY_DECIMALS)
    unrealized_derived = False
    if unrealized is None and all(row.get("unrealized_pnl") is not None for row in positions if row.get("status") == "open"):
        unrealized = _money(sum(
            float(row.get("unrealized_pnl") or 0.0)
            for row in positions
            if row.get("status") == "open"
        ))
        unrealized_derived = True
    account_slippage = _optional_number(source_account.get("slippage"), "execution slippage", decimals=_MONEY_DECIMALS)
    fill_slippage_known = all(row.get("slippage") is not None for row in fills)
    fill_slippage = _money(sum(float(row.get("slippage") or 0.0) for row in fills)) if fill_slippage_known else None
    slippage = account_slippage if account_slippage is not None else fill_slippage
    if account_slippage is not None and fill_slippage is not None and account_slippage != fill_slippage:
        issues.append({"code": "account_slippage_mismatch", "account": account_slippage, "fills": fill_slippage})
    return (
        {
            "gross_realized_pnl": gross,
            "fees": fees,
            "funding": funding,
            "net_realized_pnl": realized,
            "unrealized_pnl": unrealized,
            "net_pnl": None if unrealized is None else _money(realized + unrealized),
            "slippage": slippage,
        },
        {
            "realized_observed": source_pnl.get("realized") not in (None, "") or source_account.get("realized_pnl") not in (None, "") or fill_realized is not None or position_realized is not None,
            "unrealized_observed": source_pnl.get("unrealized") is not None or unrealized_derived,
            "fees_observed": account_fees is not None or fill_fees is not None,
            "funding_observed": funding_present,
            "slippage_observed": account_slippage is not None or fill_slippage is not None,
        },
    )


def _execution_account(
    source: Mapping[str, Any],
    pnl: Mapping[str, float | None],
    issues: list[dict[str, Any]],
) -> tuple[dict[str, float | str | None], dict[str, bool]]:
    raw = source.get("account") if isinstance(source.get("account"), Mapping) else {}
    starting = _optional_number(raw.get("starting_cash", raw.get("starting_balance")), "starting balance", decimals=_MONEY_DECIMALS)
    ending = _optional_number(raw.get("ending_cash"), "ending cash", decimals=_MONEY_DECIMALS)
    equity = _optional_number(raw.get("equity"), "equity", decimals=_MONEY_DECIMALS)
    margin = _optional_number(raw.get("margin"), "margin", decimals=_MONEY_DECIMALS)
    exposure = _optional_number(raw.get("exposure"), "exposure", decimals=_MONEY_DECIMALS)
    available = _optional_number(raw.get("available_balance", raw.get("available")), "available balance", decimals=_MONEY_DECIMALS)
    leverage = _optional_number(raw.get("leverage"), "leverage", decimals=_MONEY_DECIMALS)
    account_realized = _optional_number(raw.get("realized_pnl"), "account realized P&L", decimals=_MONEY_DECIMALS)
    realized = pnl.get("net_realized_pnl")
    unrealized = pnl.get("unrealized_pnl")
    if account_realized is not None and realized is not None and account_realized != realized:
        issues.append({"code": "account_realized_pnl_mismatch", "account": account_realized, "snapshot": realized})
    if starting is not None and realized is not None:
        expected_ending = _money(starting + realized)
        if ending is None:
            ending = expected_ending
        elif ending != expected_ending:
            issues.append({"code": "account_ending_cash_mismatch", "account": ending, "expected": expected_ending})
    if ending is not None and unrealized is not None:
        expected_equity = _money(ending + unrealized)
        if equity is None:
            equity = expected_equity
        elif equity != expected_equity:
            issues.append({"code": "account_equity_mismatch", "account": equity, "expected": expected_equity})
    if margin is not None and margin < 0:
        issues.append({"code": "negative_margin", "margin": margin})
    if exposure is not None and exposure < 0:
        issues.append({"code": "negative_exposure", "exposure": exposure})
    return (
        {
            "starting_balance": starting,
            "ending_cash": ending,
            "equity": equity,
            "available_balance": available,
            "margin": margin,
            "exposure": exposure,
            "leverage": leverage,
        },
        {
            "starting_balance_observed": starting is not None,
            "ending_cash_observed": raw.get("ending_cash") not in (None, ""),
            "equity_observed": raw.get("equity") is not None,
            "margin_observed": raw.get("margin") is not None,
            "exposure_observed": raw.get("exposure") is not None,
        },
    )


def _execution_completeness(
    source: Mapping[str, Any],
    *,
    pnl_observation: Mapping[str, bool],
    account_observation: Mapping[str, bool],
    positions: list[dict[str, Any]],
) -> dict[str, Any]:
    observed = {
        "orders_observed": isinstance(source.get("orders"), list),
        "fills_observed": isinstance(source.get("fills"), list),
        "positions_observed": isinstance(source.get("positions"), list),
        "trades_observed": isinstance(source.get("positions"), list) and all(row.get("trade_id") for row in positions),
        **dict(pnl_observation),
        **dict(account_observation),
    }
    limitations = sorted(key.removesuffix("_observed") for key, value in observed.items() if not value)
    return {
        "status": "complete" if not limitations else "partial",
        "observed": observed,
        "limitations": limitations,
        "unknown_is_not_zero": True,
    }


def _insert_identity(
    target: dict[str, dict[str, Any]],
    identity: str,
    row: dict[str, Any],
    label: str,
    issues: list[dict[str, Any]],
) -> None:
    previous = target.get(identity)
    if previous is None:
        target[identity] = row
        return
    if _stable_json(previous) != _stable_json(row):
        raise AccountingContractError(f"conflicting {label} identity: {identity}")
    issues.append({"code": f"duplicate_{label}_id", f"{label}_id": identity})


def _required_rows(source: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    value = source.get(key)
    if not isinstance(value, list):
        raise AccountingContractError(f"execution {key} must be a list")
    if any(not isinstance(row, Mapping) for row in value):
        raise AccountingContractError(f"execution {key} rows must be objects")
    return list(value)


def _fill_quantity(row: Mapping[str, Any], *, price: float) -> float:
    for key in ("quantity", "pnl_units", "units", "contracts"):
        if row.get(key) not in (None, ""):
            return _positive_number(row.get(key), "fill quantity", decimals=_QUANTITY_DECIMALS)
    if row.get("notional") not in (None, ""):
        notional = _positive_number(row.get("notional"), "fill notional", decimals=_MONEY_DECIMALS)
        return _quantity(notional / price)
    raise AccountingContractError("fill quantity is required")


def _first_number(
    row: Mapping[str, Any],
    keys: tuple[str, ...],
    label: str,
    *,
    decimals: int,
) -> float | None:
    for key in keys:
        if row.get(key) not in (None, ""):
            return _optional_number(row.get(key), label, decimals=decimals)
    return None


def _optional_number(value: Any, label: str, *, decimals: int) -> float | None:
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise AccountingContractError(f"{label} must be numeric") from exc
    if not math.isfinite(parsed):
        raise AccountingContractError(f"{label} must be finite")
    return round(parsed, decimals)


def _positive_number(value: Any, label: str, *, decimals: int) -> float:
    parsed = _optional_number(value, label, decimals=decimals)
    if parsed is None or parsed <= 0:
        raise AccountingContractError(f"{label} must be positive")
    return parsed


def _optional_int(value: Any, label: str) -> int | None:
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise AccountingContractError(f"{label} must be an integer") from exc
    return parsed


def _required_text(value: Any, label: str) -> str:
    rendered = str(value or "").strip()
    if not rendered:
        raise AccountingContractError(f"{label} is required")
    return rendered


def _optional_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    rendered = str(value).strip()
    return rendered or None


def _event(value: Any, *, allow_unknown: bool) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_")
    aliases = {"open": "entry", "close": "exit", "closed": "exit", "take_profit": "target", "tp": "target", "sl": "stop"}
    normalized = aliases.get(normalized, normalized)
    if normalized in ENTRY_EVENTS | EXIT_EVENTS:
        return normalized
    if allow_unknown and not normalized:
        return "unknown"
    if allow_unknown and normalized == "unknown":
        return normalized
    raise AccountingContractError(f"unsupported fill event: {normalized or '<missing>'}")


def _side(value: Any, *, allow_blank: bool) -> str:
    normalized = str(value or "").strip().lower()
    aliases = {"long": "buy", "short": "sell"}
    normalized = aliases.get(normalized, normalized)
    if normalized in {"buy", "sell"}:
        return normalized
    if allow_blank and not normalized:
        return ""
    raise AccountingContractError(f"unsupported order side: {normalized or '<missing>'}")


def _position_side(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    aliases = {"buy": "long", "sell": "short"}
    normalized = aliases.get(normalized, normalized)
    if normalized not in {"long", "short"}:
        raise AccountingContractError(f"unsupported position side: {normalized or '<missing>'}")
    return normalized


def _order_state(value: Any) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_")
    aliases = {"canceled": "cancelled", "partiallyfilled": "partially_filled"}
    return aliases.get(normalized, normalized or "unknown")


def _money(value: float) -> float:
    return round(float(value), _MONEY_DECIMALS)


def _quantity(value: float) -> float:
    return round(float(value), _QUANTITY_DECIMALS)


def _drop_none(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if value is not None}


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _sorted_issues(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(issues, key=_stable_json)
