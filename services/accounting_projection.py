"""Pure adapters from execution-engine facts to canonical accounting truth."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from datetime import datetime, timezone
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


def project_broker_accounting(source: Mapping[str, Any]) -> AccountingSnapshot:
    """Project a read-only venue receipt without inventing lifecycle evidence."""

    if not isinstance(source, Mapping):
        raise AccountingContractError("broker accounting source must be an object")
    provider = _required_text(source.get("provider"), "broker provider").lower()
    if provider in {"binance_usdm", "binance_usdm_futures"}:
        return _project_binance_accounting(source, provider=provider)
    if provider == "tiger_openapi":
        return _project_tiger_accounting(source)
    raise AccountingContractError(f"unsupported broker accounting provider: {provider}")


def project_broker_accounting_fail_honest(source: Mapping[str, Any]) -> AccountingSnapshot:
    """Return a blocked canonical receipt when venue facts cannot be projected.

    Broker reconciliation and account synchronization own safety-critical
    persistence. Their additive accounting read model must never suppress that
    write, while malformed evidence must never be converted into zeroes.
    """

    try:
        return project_broker_accounting(source)
    except Exception as exc:
        return _blocked_broker_accounting(source, exc)


def broker_accounting_snapshot_payload(source: Mapping[str, Any]) -> dict[str, Any]:
    """Serialize broker accounting without ever suppressing its source receipt."""

    try:
        return project_broker_accounting_fail_honest(source).to_dict()
    except Exception as exc:
        return {
            "schema_version": "accounting-projection-unavailable-v1",
            "status": "blocked",
            "error_type": type(exc).__name__,
            "completeness": {
                "status": "blocked",
                "unknown_is_not_zero": True,
            },
            "reconciliation": {
                "status": "blocked",
                "issues": [{
                    "code": "accounting_projection_unavailable",
                    "error_type": type(exc).__name__,
                }],
            },
        }


def _blocked_broker_accounting(
    source: Mapping[str, Any],
    error: Exception,
) -> AccountingSnapshot:
    provider = str(source.get("provider") or "unknown_broker").strip().lower() or "unknown_broker"
    run_date = str(source.get("run_date") or "unknown").strip() or "unknown"
    balance = source.get("exchange_balance") if isinstance(source.get("exchange_balance"), Mapping) else {}
    observation = source.get("account_observation") if isinstance(source.get("account_observation"), Mapping) else {}
    default_currency = "USD" if provider == "tiger_openapi" else "USDT"
    currency = str(
        balance.get("asset")
        or observation.get("base_currency")
        or default_currency
    ).strip().upper() or default_currency
    source_schema_version = (
        "tiger-account-sync-v1"
        if provider == "tiger_openapi"
        else "broker-reconciliation-v1"
    )
    observed = {
        "orders_observed": False,
        "fills_observed": False,
        "positions_observed": False,
        "trade_lifecycles_observed": False,
        "balance_observed": False,
        "realized_observed": False,
        "unrealized_observed": False,
        "fees_observed": False,
        "funding_observed": False,
        "slippage_observed": False,
    }
    issue = {
        "code": "accounting_projection_failed",
        "error_type": type(error).__name__,
    }
    if isinstance(error, AccountingContractError):
        issue["detail"] = str(error)[:500]
    return build_accounting_snapshot(
        source_type="broker_reconciliation",
        source_name=provider,
        source_schema_version=source_schema_version,
        scope={
            "run_date": run_date,
            "provider": provider,
            "mode": str(source.get("mode") or ""),
        },
        currency=currency,
        orders=[],
        fills=[],
        positions=[],
        trades=[],
        counts=_broker_counts(),
        pnl={
            "gross_realized_pnl": None,
            "fees": None,
            "funding": None,
            "net_realized_pnl": None,
            "unrealized_pnl": None,
            "net_pnl": None,
            "slippage": None,
        },
        account={
            "starting_balance": None,
            "ending_cash": None,
            "equity": None,
            "available_balance": None,
            "margin": None,
            "exposure": None,
            "leverage": None,
        },
        completeness={
            "status": "blocked",
            "observed": observed,
            "limitations": sorted(key.removesuffix("_observed") for key in observed),
            "unknown_is_not_zero": True,
        },
        reconciliation={
            "status": "blocked",
            "issues": [issue],
            "identity_policy": "projection_failed_no_facts_admitted",
        },
    )


def _project_binance_accounting(source: Mapping[str, Any], *, provider: str) -> AccountingSnapshot:
    run_date = _required_text(source.get("run_date"), "broker run_date")
    error = str(source.get("error") or "").strip()
    observation = source.get("account_observation") if isinstance(source.get("account_observation"), Mapping) else {}
    balance = source.get("exchange_balance") if isinstance(source.get("exchange_balance"), Mapping) else {}
    accounting = source.get("exchange_accounting") if isinstance(source.get("exchange_accounting"), Mapping) else {}
    currency = str(
        balance.get("asset")
        or accounting.get("net_realized_pnl_asset")
        or "USDT"
    ).strip().upper()
    venue_state_observed = not error
    fills_observed = venue_state_observed and observation.get("fills_observed") is True
    income_observed = venue_state_observed and observation.get("income_observed") is True
    balance_observed = venue_state_observed and observation.get("balance_present") is True
    account_observed = venue_state_observed and observation.get("account_observed") is True
    issues: list[dict[str, Any]] = []

    orders = _broker_orders(source.get("exchange_open_orders") if venue_state_observed else [], issues)
    fills = _binance_fills(
        source.get("exchange_fills") if fills_observed else [],
        currency=currency,
        issues=issues,
    )
    positions = _binance_positions(source.get("exchange_positions") if venue_state_observed else [], issues)

    gross = _optional_number(accounting.get("realized_pnl_from_fills"), "broker gross realized P&L", decimals=_MONEY_DECIMALS) if account_observed else None
    commission = accounting.get("commission_by_asset") if isinstance(accounting.get("commission_by_asset"), Mapping) else None
    funding_rows = accounting.get("funding_by_asset") if isinstance(accounting.get("funding_by_asset"), Mapping) else None
    fees = _optional_number(commission.get(currency, 0.0), "broker fees", decimals=_MONEY_DECIMALS) if account_observed and commission is not None else None
    funding = _optional_number(funding_rows.get(currency, 0.0), "broker funding", decimals=_MONEY_DECIMALS) if account_observed and income_observed and funding_rows is not None else None
    net_realized = _optional_number(accounting.get("net_realized_pnl_estimate"), "broker net realized P&L", decimals=_MONEY_DECIMALS) if account_observed else None
    if all(value is not None for value in (gross, fees, funding, net_realized)):
        formula = _money(float(gross) - float(fees) + float(funding))
        if formula != net_realized:
            issues.append({"code": "broker_net_realized_pnl_mismatch", "formula": formula, "reported": net_realized})
    unrealized = (
        _money(sum(float(row.get("unrealized_pnl") or 0.0) for row in positions))
        if venue_state_observed and all(row.get("unrealized_pnl") is not None for row in positions)
        else None
    )
    wallet_balance = _optional_number(balance.get("balance"), "broker wallet balance", decimals=_MONEY_DECIMALS) if balance_observed else None
    available = _optional_number(balance.get("available"), "broker available balance", decimals=_MONEY_DECIMALS) if balance_observed else None
    exposure = (
        _money(sum(float(row["entry_notional"]) for row in positions))
        if venue_state_observed and all(row.get("entry_notional") is not None for row in positions)
        else None
    )
    counts = _broker_counts(
        order_count=len(orders) if venue_state_observed else None,
        open_order_count=len(orders) if venue_state_observed else None,
        fill_count=len(fills) if fills_observed else None,
        position_count=len(positions) if venue_state_observed else None,
        open_position_count=len(positions) if venue_state_observed else None,
    )
    observed = {
        "orders_observed": venue_state_observed,
        "fills_observed": fills_observed,
        "positions_observed": venue_state_observed,
        "balance_observed": balance_observed,
        "realized_observed": net_realized is not None,
        "unrealized_observed": unrealized is not None,
        "fees_observed": fees is not None,
        "funding_observed": funding is not None,
        "slippage_observed": False,
        "trade_lifecycles_observed": False,
    }
    limitations = {
        "trade_lifecycle_classification",
        "starting_balance",
        "margin",
        "slippage",
    }
    limitations.update(key.removesuffix("_observed") for key, value in observed.items() if not value)
    if commission is not None and any(str(asset).upper() != currency and float(value or 0.0) != 0.0 for asset, value in commission.items()):
        limitations.add("non_margin_asset_fee_conversion")
    if error:
        issues.append({"code": "broker_observation_failed", "error": error})
    for drift in source.get("drifts") or []:
        if isinstance(drift, Mapping):
            issues.append({
                "code": str(drift.get("reason_code") or "broker_reconciliation_drift"),
                "reason": str(drift.get("reason") or ""),
                "symbol": str(drift.get("exchange_symbol") or drift.get("symbol") or ""),
            })
    source_status = "blocked" if error or str(source.get("confirmation_status") or "") == "cannot_confirm" else (
        "drift" if int(source.get("drift_count") or 0) > 0 or source.get("reconciled") is not True else "pass"
    )
    if issues and source_status == "pass":
        source_status = "drift"
    return build_accounting_snapshot(
        source_type="broker_reconciliation",
        source_name=provider,
        source_schema_version="broker-reconciliation-v1",
        scope={"run_date": run_date, "provider": provider},
        currency=currency,
        orders=orders,
        fills=fills,
        positions=positions,
        trades=[],
        counts=counts,
        pnl={
            "gross_realized_pnl": gross,
            "fees": fees,
            "funding": funding,
            "net_realized_pnl": net_realized,
            "unrealized_pnl": unrealized,
            "net_pnl": None if net_realized is None or unrealized is None else _money(net_realized + unrealized),
            "slippage": None,
        },
        account={
            "starting_balance": None,
            "ending_cash": wallet_balance,
            "equity": None if wallet_balance is None or unrealized is None else _money(wallet_balance + unrealized),
            "available_balance": available,
            "margin": None,
            "exposure": exposure,
            "leverage": None,
        },
        completeness={
            "status": "partial",
            "observed": observed,
            "limitations": sorted(limitations),
            "unknown_is_not_zero": True,
        },
        reconciliation={
            "status": source_status,
            "issues": _sorted_issues(issues),
            "source_confirmation_status": str(source.get("confirmation_status") or ""),
            "identity_policy": "dedupe_exact_reject_conflict",
        },
    )


def _project_tiger_accounting(source: Mapping[str, Any]) -> AccountingSnapshot:
    run_date = _required_text(source.get("run_date"), "broker run_date")
    error = str(source.get("error") or "").strip()
    observation = source.get("account_observation") if isinstance(source.get("account_observation"), Mapping) else {}
    balance = source.get("exchange_balance") if isinstance(source.get("exchange_balance"), Mapping) else {}
    accounting = source.get("exchange_accounting") if isinstance(source.get("exchange_accounting"), Mapping) else {}
    currency = str(balance.get("asset") or observation.get("base_currency") or "USD").strip().upper()
    account_observed = not error and observation.get("account_observed") is True
    balance_observed = account_observed and observation.get("balance_present") is True
    accounting_observed = account_observed and observation.get("accounting_observed") is True
    net_realized = _optional_number(accounting.get("net_realized_pnl_estimate"), "Tiger net realized P&L", decimals=_MONEY_DECIMALS) if accounting_observed else None
    unrealized = _optional_number(accounting.get("unrealized_pnl_estimate"), "Tiger unrealized P&L", decimals=_MONEY_DECIMALS) if accounting_observed else None
    net_liquidation = _optional_number(balance.get("balance"), "Tiger net liquidation", decimals=_MONEY_DECIMALS) if balance_observed else None
    available = _optional_number(balance.get("available"), "Tiger available balance", decimals=_MONEY_DECIMALS) if balance_observed else None
    issues = [{"code": "broker_observation_failed", "error": error}] if error else []
    status = "pass" if str(source.get("sync_status") or "") == "synced" and account_observed else "blocked"
    observed = {
        "orders_observed": False,
        "fills_observed": False,
        "positions_observed": False,
        "trade_lifecycles_observed": False,
        "balance_observed": balance_observed,
        "realized_observed": net_realized is not None,
        "unrealized_observed": unrealized is not None,
        "fees_observed": False,
        "funding_observed": False,
        "slippage_observed": False,
    }
    return build_accounting_snapshot(
        source_type="broker_reconciliation",
        source_name="tiger_openapi",
        source_schema_version="tiger-account-sync-v1",
        scope={"run_date": run_date, "provider": "tiger_openapi", "mode": str(source.get("mode") or "")},
        currency=currency,
        orders=[],
        fills=[],
        positions=[],
        trades=[],
        counts=_broker_counts(),
        pnl={
            "gross_realized_pnl": None,
            "fees": None,
            "funding": None,
            "net_realized_pnl": net_realized,
            "unrealized_pnl": unrealized,
            "net_pnl": None if net_realized is None or unrealized is None else _money(net_realized + unrealized),
            "slippage": None,
        },
        account={
            "starting_balance": None,
            "ending_cash": None,
            "equity": net_liquidation,
            "available_balance": available,
            "margin": None,
            "exposure": None,
            "leverage": None,
        },
        completeness={
            "status": "partial",
            "observed": observed,
            "limitations": sorted({
                "orders",
                "fills",
                "positions",
                "trade_lifecycle_classification",
                "gross_realized_pnl",
                "fees",
                "funding",
                "starting_balance",
                "ending_cash",
                "margin",
                "exposure",
                "slippage",
                *(key.removesuffix("_observed") for key, value in observed.items() if not value),
            }),
            "unknown_is_not_zero": True,
        },
        reconciliation={
            "status": status,
            "issues": issues,
            "source_sync_status": str(source.get("sync_status") or ""),
            "identity_policy": "aggregate_account_evidence_only",
        },
    )


def _broker_orders(value: Any, issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = value if isinstance(value, list) else []
    by_id: dict[str, dict[str, Any]] = {}
    for raw in rows:
        if not isinstance(raw, Mapping):
            raise AccountingContractError("broker open order row must be an object")
        source_order_id = _required_text(raw.get("order_id"), "broker order_id")
        client_order_id = _optional_text(raw.get("client_order_id"))
        order_id = f"{source_order_id}:{client_order_id}" if client_order_id else source_order_id
        row = _drop_none({
            "order_id": order_id,
            "source_order_id": source_order_id,
            "client_order_id": client_order_id,
            "state": _order_state(raw.get("status") or "open"),
            "side": _side(raw.get("side"), allow_blank=False),
            "order_type": str(raw.get("type") or "").strip().lower(),
            "quantity": _optional_number(raw.get("orig_qty"), "broker order quantity", decimals=_QUANTITY_DECIMALS),
            "instrument_id": _optional_text(raw.get("symbol")),
            "reduce_only": bool(raw.get("reduce_only")),
            "close_position": bool(raw.get("close_position")),
            "source": _optional_text(raw.get("source")),
        })
        _insert_identity(by_id, order_id, row, "order", issues)
    return sorted(by_id.values(), key=lambda row: row["order_id"])


def _binance_fills(value: Any, *, currency: str, issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = value if isinstance(value, list) else []
    by_id: dict[str, dict[str, Any]] = {}
    for raw in rows:
        if not isinstance(raw, Mapping):
            raise AccountingContractError("broker fill row must be an object")
        symbol = _required_text(raw.get("symbol"), "broker fill symbol")
        raw_fill_id = _required_text(raw.get("id"), "broker fill id")
        fill_id = f"{symbol}:{raw_fill_id}"
        price = _positive_number(raw.get("price"), "broker fill price", decimals=_MONEY_DECIMALS)
        quantity = _positive_number(raw.get("qty"), "broker fill quantity", decimals=_QUANTITY_DECIMALS)
        commission_asset = str(raw.get("commission_asset") or "").strip().upper()
        fee = (
            _optional_number(raw.get("commission"), "broker fill commission", decimals=_MONEY_DECIMALS)
            if commission_asset == currency
            else None
        )
        gross = _optional_number(raw.get("realized_pnl"), "broker fill realized P&L", decimals=_MONEY_DECIMALS)
        row = _drop_none({
            "fill_id": fill_id,
            "source_fill_id": raw_fill_id,
            "order_id": _optional_text(raw.get("order_id")),
            "trade_id": None,
            "event": "unknown",
            "classification": "entry_or_exit_unknown_from_current_userTrades_projection",
            "side": _side(raw.get("side"), allow_blank=False),
            "price": price,
            "quantity": quantity,
            "notional": _optional_number(raw.get("quote_qty"), "broker fill quote quantity", decimals=_MONEY_DECIMALS) or _money(price * quantity),
            "gross_realized_pnl": gross,
            "fee": fee,
            "commission_asset": commission_asset,
            "net_realized_pnl": None if gross is None or fee is None else _money(gross - fee),
            "ts": _broker_timestamp(raw.get("time")),
            "instrument_id": symbol,
        })
        _insert_identity(by_id, fill_id, row, "fill", issues)
    return sorted(by_id.values(), key=lambda row: (str(row.get("ts") or ""), row["fill_id"]))


def _binance_positions(value: Any, issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = value if isinstance(value, list) else []
    by_id: dict[str, dict[str, Any]] = {}
    for raw in rows:
        if not isinstance(raw, Mapping):
            raise AccountingContractError("broker position row must be an object")
        symbol = _required_text(raw.get("symbol"), "broker position symbol")
        amount = _optional_number(raw.get("position_amt"), "broker position amount", decimals=_QUANTITY_DECIMALS)
        if amount is None or amount == 0:
            continue
        entry = _optional_number(raw.get("entry_price"), "broker position entry price", decimals=_MONEY_DECIMALS)
        if entry is None or entry <= 0:
            issues.append({"code": "broker_position_entry_price_invalid", "symbol": symbol})
            entry = None
        position_id = f"{symbol}:{'long' if amount > 0 else 'short'}"
        row = _drop_none({
            "position_id": position_id,
            "trade_id": None,
            "status": "open",
            "side": "long" if amount > 0 else "short",
            "quantity": _quantity(abs(amount)),
            "remaining_quantity": _quantity(abs(amount)),
            "entry_price": entry,
            "entry_notional": None if entry is None else _money(abs(amount) * entry),
            "unrealized_pnl": _optional_number(raw.get("unrealized_pnl"), "broker position unrealized P&L", decimals=_MONEY_DECIMALS),
            "instrument_id": symbol,
        })
        _insert_identity(by_id, position_id, row, "position", issues)
    return sorted(by_id.values(), key=lambda row: row["position_id"])


def _broker_counts(
    *,
    order_count: int | None = None,
    open_order_count: int | None = None,
    fill_count: int | None = None,
    position_count: int | None = None,
    open_position_count: int | None = None,
) -> dict[str, int | None]:
    return {
        "order_count": order_count,
        "open_order_count": open_order_count,
        "fill_count": fill_count,
        "entry_fill_count": None,
        "exit_fill_count": None,
        "trade_count": None,
        "open_trade_count": None,
        "completed_trade_count": None,
        "position_count": position_count,
        "open_position_count": open_position_count,
    }


def _broker_timestamp(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        milliseconds = float(value)
    except (TypeError, ValueError):
        return _optional_text(value)
    if not math.isfinite(milliseconds):
        raise AccountingContractError("broker timestamp must be finite")
    return datetime.fromtimestamp(milliseconds / 1000.0, tz=timezone.utc).isoformat()


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
