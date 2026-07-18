"""Provider-neutral fail-honest broker accounting helpers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from schemas.accounting import AccountingSnapshot, build_accounting_snapshot
from services.accounting_projection_core import AccountingContractError
from services.accounting_projection_port import BrokerAccountingPluginDescriptor


def blocked_broker_accounting(
    source: Mapping[str, Any],
    error: Exception,
    *,
    descriptor: BrokerAccountingPluginDescriptor | None = None,
) -> AccountingSnapshot:
    provider = str(source.get("provider") or "unknown_broker").strip().lower() or "unknown_broker"
    run_date = str(source.get("run_date") or "unknown").strip() or "unknown"
    balance = source.get("exchange_balance") if isinstance(source.get("exchange_balance"), Mapping) else {}
    observation = source.get("account_observation") if isinstance(source.get("account_observation"), Mapping) else {}
    default_currency = descriptor.default_currency if descriptor is not None else "USDT"
    currency = str(
        balance.get("asset")
        or observation.get("base_currency")
        or default_currency
    ).strip().upper() or default_currency
    source_schema_version = (
        descriptor.source_schema_version
        if descriptor is not None
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
