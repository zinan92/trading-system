"""Trusted composition root for broker accounting source adapters."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from schemas.accounting import AccountingSnapshot
from services.accounting_binance_adapter import BinanceUsdMAccountingAdapter
from services.accounting_broker_common import blocked_broker_accounting
from services.accounting_projection_core import AccountingContractError, _required_text
from services.accounting_projection_port import BrokerAccountingPluginDescriptor
from services.accounting_projection_registry import BrokerAccountingPluginRegistry
from services.accounting_tiger_adapter import TigerOpenApiAccountingAdapter


def build_default_broker_accounting_registry() -> BrokerAccountingPluginRegistry:
    return (
        BrokerAccountingPluginRegistry()
        .register(
            BrokerAccountingPluginDescriptor(
                name="binance_usdm_accounting",
                implementation=(
                    "services.accounting_binance_adapter.BinanceUsdMAccountingAdapter"
                ),
                source_names=("binance_usdm", "binance_usdm_futures"),
                source_schema_version="broker-reconciliation-v1",
                default_currency="USDT",
            ),
            BinanceUsdMAccountingAdapter,
        )
        .register(
            BrokerAccountingPluginDescriptor(
                name="tiger_openapi_accounting",
                implementation="services.accounting_tiger_adapter.TigerOpenApiAccountingAdapter",
                source_names=("tiger_openapi",),
                source_schema_version="tiger-account-sync-v1",
                default_currency="USD",
            ),
            TigerOpenApiAccountingAdapter,
        )
        .freeze()
    )


BROKER_ACCOUNTING_PLUGINS = build_default_broker_accounting_registry()


def project_broker_accounting(
    source: Mapping[str, Any],
    *,
    registry: BrokerAccountingPluginRegistry = BROKER_ACCOUNTING_PLUGINS,
) -> AccountingSnapshot:
    if not isinstance(source, Mapping):
        raise AccountingContractError("broker accounting source must be an object")
    provider = _required_text(source.get("provider"), "broker provider").lower()
    try:
        registry.descriptor_for_source(provider)
    except KeyError as exc:
        raise AccountingContractError(
            f"unsupported broker accounting provider: {provider}"
        ) from exc
    return registry.project(provider, source)


def project_broker_accounting_fail_honest(
    source: Mapping[str, Any],
    *,
    registry: BrokerAccountingPluginRegistry = BROKER_ACCOUNTING_PLUGINS,
) -> AccountingSnapshot:
    try:
        return project_broker_accounting(source, registry=registry)
    except Exception as exc:
        descriptor = None
        if isinstance(source, Mapping):
            provider = str(source.get("provider") or "").strip().lower()
            try:
                descriptor = registry.descriptor_for_source(provider)
            except (KeyError, RuntimeError):
                descriptor = None
        return blocked_broker_accounting(source, exc, descriptor=descriptor)


def broker_accounting_snapshot_payload(
    source: Mapping[str, Any],
    *,
    registry: BrokerAccountingPluginRegistry = BROKER_ACCOUNTING_PLUGINS,
) -> dict[str, Any]:
    try:
        return project_broker_accounting_fail_honest(source, registry=registry).to_dict()
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
