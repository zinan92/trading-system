"""Tiger aggregate account receipt to canonical accounting adapter."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from schemas.accounting import AccountingSnapshot, build_accounting_snapshot
from services.accounting_broker_common import _broker_counts
from services.accounting_projection_core import (
    _money,
    _optional_number,
    _required_text,
)


_MONEY_DECIMALS = 8


class TigerOpenApiAccountingAdapter:
    name = "tiger_openapi_accounting"
    source_names = ("tiger_openapi",)

    def project(self, source: Mapping[str, Any]) -> AccountingSnapshot:
        provider = _required_text(source.get("provider"), "broker provider").lower()
        if provider not in self.source_names:
            raise ValueError(f"unsupported Tiger accounting provider: {provider}")
        return _project_tiger_accounting(source)


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

