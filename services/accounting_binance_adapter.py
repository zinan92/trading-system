"""Binance USD-M broker receipt to canonical accounting adapter."""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from schemas.accounting import AccountingSnapshot, build_accounting_snapshot
from services.accounting_broker_common import _broker_counts
from services.accounting_projection_core import (
    AccountingContractError,
    _drop_none,
    _insert_identity,
    _money,
    _optional_number,
    _optional_text,
    _order_state,
    _positive_number,
    _quantity,
    _required_text,
    _side,
    _sorted_issues,
)


_MONEY_DECIMALS = 8
_QUANTITY_DECIMALS = 10


class BinanceUsdMAccountingAdapter:
    name = "binance_usdm_accounting"
    source_names = ("binance_usdm", "binance_usdm_futures")

    def project(self, source: Mapping[str, Any]) -> AccountingSnapshot:
        provider = _required_text(source.get("provider"), "broker provider").lower()
        if provider not in self.source_names:
            raise AccountingContractError(f"unsupported Binance accounting provider: {provider}")
        return _project_binance_accounting(source, provider=provider)


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


